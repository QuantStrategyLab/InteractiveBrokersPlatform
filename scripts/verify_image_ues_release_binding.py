#!/usr/bin/env python3
"""Deploy guard (RS-08 fix 1): block an image whose installed UES revision differs
from the strategy release / runtime-risk binding it would run under.

The live runtime compares the installed UES revision with
``runtime_risk_limits.binding.ues_revision`` and rejects every SOXL rebalance on
mismatch (``rejected:runtime_risk_limits``). Rebuilding from a newer repo pin
without updating ``strategy_release.strategy_revision`` and the binding would
therefore silently stop trading. This check runs after ``docker build`` and before
``docker push`` / ``gcloud run deploy`` and fails the deploy instead.

Sources of the installed revision (all given must agree):
  --image    built image; reads /app/UES_REVISION or the UES direct_url.json
             inside the container (``--network none``, no environment)
  --lockfile uv.lock; reads the pinned us-equity-strategies git commit
  --installed-revision  explicit value (tests)

Expected revisions come from each planned target's RUNTIME_TARGET_JSON
(SYNC_PLAN_JSON targets[].env), else CLOUD_RUN_SERVICE_TARGETS_JSON runtime
targets, else RUNTIME_TARGET_JSON. A target without a strategy_release and
without a binding ues_revision is reported and not blocked. Prints only
ordinal target aliases and validated commit SHAs. Read-only; does not touch Cloud Run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
UES_DIST = "us-equity-strategies"
_IMAGE_PROBE = (
    "import json,pathlib\n"
    "from importlib import metadata as m\n"
    "p=pathlib.Path('/app/UES_REVISION')\n"
    "s=p.read_text().strip() if p.exists() else ''\n"
    "d=json.loads(m.distribution('us-equity-strategies').read_text('direct_url.json') or '{}')\n"
    "print('UES_STAMP='+s)\n"
    "print('UES_DIRECT_URL='+str((d.get('vcs_info') or {}).get('commit_id') or ''))\n"
)


class GuardError(RuntimeError):
    pass


def revision_from_lockfile(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    for block in text.split("[[package]]"):
        if re.search(r'^name = "us-equity-strategies"\s*$', block, re.MULTILINE):
            match = re.search(r'^source = \{ git = "[^"]*#([0-9a-f]{40})"', block, re.MULTILINE)
            if match:
                return match.group(1)
    raise GuardError(f"{path}: no pinned git commit for {UES_DIST}")


def revision_from_image(image: str) -> str:
    result = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--entrypoint", "python", image, "-c", _IMAGE_PROBE],
        capture_output=True, text=True, timeout=300, check=False,
    )
    if result.returncode != 0:
        raise GuardError(f"image probe failed (exit {result.returncode})")
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    stamp, direct = values.get("UES_STAMP", "").strip(), values.get("UES_DIRECT_URL", "").strip()
    if stamp and direct and stamp != direct:
        raise GuardError("image UES stamp differs from installed direct_url")
    revision = stamp or direct
    if not SHA_RE.match(revision):
        raise GuardError("image has no installed UES commit")
    return revision


def _load_json(raw: Any) -> Any:
    if isinstance(raw, str):
        raw = raw.strip()
        return json.loads(raw) if raw else None
    return raw


def runtime_targets_from_env(env: Mapping[str, str]) -> list[tuple[str, Mapping[str, Any]]]:
    plan = _load_json(env.get("SYNC_PLAN_JSON") or "")
    out: list[tuple[str, Mapping[str, Any]]] = []
    if isinstance(plan, Mapping) and plan.get("targets"):
        for target in plan["targets"]:
            runtime_target = _load_json(((target or {}).get("env") or {}).get("RUNTIME_TARGET_JSON") or "") or {}
            service = str(target.get("service_name") or runtime_target.get("service_name") or "").strip()
            out.append((service, runtime_target))
        return out
    targets = _load_json(env.get("CLOUD_RUN_SERVICE_TARGETS_JSON") or "")
    if targets:
        entries = targets.get("targets") if isinstance(targets, Mapping) else targets
        for entry in entries or ():
            if not isinstance(entry, Mapping):
                continue
            runtime_target = _load_json(entry.get("runtime_target") or entry.get("runtime_target_json")) or {}
            service = str(entry.get("service_name") or entry.get("service") or runtime_target.get("service_name") or "")
            out.append((service.strip(), runtime_target))
        return out
    single = _load_json(env.get("RUNTIME_TARGET_JSON") or "")
    if isinstance(single, Mapping):
        service = str(single.get("service_name") or env.get("CLOUD_RUN_SERVICE") or "").strip()
        out.append((service, single))
    return out


def expected_revisions(runtime_target: Mapping[str, Any]) -> dict[str, str]:
    expected: dict[str, str] = {}
    release = runtime_target.get("strategy_release")
    if isinstance(release, Mapping) and release.get("strategy_revision"):
        expected["strategy_release.strategy_revision"] = str(release["strategy_revision"]).strip()
    limits = runtime_target.get("runtime_risk_limits")
    binding = limits.get("binding") if isinstance(limits, Mapping) else None
    if isinstance(binding, Mapping) and binding.get("ues_revision"):
        expected["runtime_risk_limits.binding.ues_revision"] = str(binding["ues_revision"]).strip()
    return expected


STATES = ("verified", "mismatch", "not_covered", "uncomputable", "error")


def new_report() -> dict[str, Any]:
    """Bounded, allowlisted build-time evidence; never a live deployment attestation."""
    return {
        "schema_version": 1,
        "evidence_scope": "built_image_and_planned_targets",
        "status": "uncomputable",
        "target_counts": {state: 0 for state in STATES},
        "installed_revisions": {},
    }


def safe_revision(value: str) -> str | None:
    return value if SHA_RE.fullmatch(value) else None


def check(
    installed: Mapping[str, str], targets: Iterable[tuple[str, Mapping[str, Any]]],
    *, report: dict[str, Any] | None = None,
) -> list[str]:
    """Validate installed revisions and optionally collect sanitized coverage."""
    report = report if report is not None else new_report()
    failures: list[str] = []
    values = set(installed.values())
    targets = list(targets)
    for label in ("lockfile", "image", "explicit"):
        if label in installed:
            revision = safe_revision(installed[label])
            report["installed_revisions"][label] = revision
            print(f"installed_ues_revision[{label}]={revision or 'unavailable'}")
    if not installed or any(not safe_revision(value) for value in installed.values()):
        report["status"] = "error"
        report["target_counts"]["uncomputable"] = len(targets)
        failures.append("installed UES revision is invalid or unavailable")
        return failures
    if len(values) != 1:
        report["status"] = "mismatch"
        report["target_counts"]["uncomputable"] = len(targets)
        failures.append("installed UES revision sources disagree")
        return failures
    (revision,) = values
    if not targets:
        failures.append("no runtime target resolved; cannot verify the strategy release binding")
    for ordinal, (_, runtime_target) in enumerate(targets, 1):
        alias = f"target-{ordinal}"
        expected = expected_revisions(runtime_target or {})
        if not expected:
            report["target_counts"]["not_covered"] += 1
            print(f"{alias}: not_covered (no revision binding; not blocked)")
            continue
        # A matching partial declaration does not establish both bindings.
        # Keep the existing mismatch decisions; this only changes coverage.
        state = "verified" if len(expected) == 2 else "not_covered"
        for field, value in sorted(expected.items()):
            ok = value == revision
            print(f"{alias} {field}={safe_revision(value) or 'unavailable'} match={ok}")
            if not ok:
                state = "mismatch"
                failures.append(f"{alias}: {field} differs from installed revision")
        report["target_counts"][state] += 1
    counts = report["target_counts"]
    report["status"] = (
        "mismatch" if failures and targets else
        "uncomputable" if not targets else
        "not_covered" if counts["not_covered"] else "verified"
    )
    return failures


def write_report(path: Path | None, report: dict[str, Any]) -> None:
    # Reporting is optional and cannot change validation or deployment decisions.
    if path is not None:
        try:
            path.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
        except OSError:
            print("::warning::UES coverage report could not be written", file=sys.stderr)


def summarize_report(path: Path) -> None:
    """Render only validated enum/count fields, never arbitrary report strings."""
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        if report.get("schema_version") != 1:
            raise ValueError("schema")
        status = report["status"]
        counts = report["target_counts"]
        if status not in STATES or any(
            type(counts[state]) is not int or not 0 <= counts[state] <= 1_000_000
            for state in STATES
        ):
            raise ValueError("invalid summary")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print("UES build-time binding coverage: report unavailable.")
        return
    print(f"UES build-time binding coverage: {status}.")
    print("Planned targets: " + ", ".join(f"{state}={counts[state]}" for state in STATES) + ".")
    print("Build-time evidence only; this does not verify a live deployed revision.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image")
    parser.add_argument("--lockfile", type=Path)
    parser.add_argument("--installed-revision")
    parser.add_argument("--report-json", type=Path, help="Optional sanitized coverage output; does not change validation")
    parser.add_argument("--summarize-report", type=Path, help="Render existing coverage output without running validation")
    args = parser.parse_args(argv)
    if args.summarize_report:
        summarize_report(args.summarize_report)
        return 0
    report = new_report()
    try:
        installed: dict[str, str] = {}
        if args.lockfile:
            installed["lockfile"] = revision_from_lockfile(args.lockfile)
        if args.image:
            installed["image"] = revision_from_image(args.image)
        if args.installed_revision:
            installed["explicit"] = args.installed_revision.strip()
        if not installed:
            raise GuardError("give --image, --lockfile or --installed-revision")
        failures = check(installed, runtime_targets_from_env(os.environ), report=report)
    except (GuardError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        report["status"] = "error"
        # Exceptions may contain image names, paths, probe stderr, or runtime JSON.
        print(f"::error::UES release binding guard could not verify ({type(exc).__name__})", file=sys.stderr)
        write_report(args.report_json, report)
        return 1
    write_report(args.report_json, report)
    if failures:
        for line in failures:
            print(f"::error::UES release binding mismatch: {line}", file=sys.stderr)
        print("Deploy blocked: update strategy_release.strategy_revision and runtime_risk_limits.binding."
              "ues_revision together with the UES pin (or keep the pin) before deploying.", file=sys.stderr)
        return 1
    print(f"UES release binding guard: not blocked; coverage={report['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
