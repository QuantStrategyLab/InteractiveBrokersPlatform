#!/usr/bin/env python3
"""Reconcile Cloud Run and Cloud Scheduler runtime drift.

The script intentionally only auto-fixes infrastructure drift that is safe to
repair: Cloud Run traffic pointing at an older revision and known legacy
Scheduler jobs. Runtime code errors, secret gaps, and live/paper policy
conflicts must still fail fast in the owning workflow.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


@dataclass(frozen=True)
class RuntimeTarget:
    service_name: str
    region: str = ""
    account_scope: str = ""


class ReconcileError(RuntimeError):
    pass


def _load_json_object(raw: str, *, field_name: str) -> Mapping[str, Any] | list[Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReconcileError(f"{field_name} must be valid JSON: {exc}") from exc
    if not isinstance(value, (Mapping, list)):
        raise ReconcileError(f"{field_name} must decode to an object or list")
    return value


def _runtime_target(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = entry.get("runtime_target") or entry.get("runtime_target_json")
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, str) and raw.strip():
        loaded = _load_json_object(raw, field_name="runtime_target_json")
        if isinstance(loaded, Mapping):
            return loaded
    return {}


def _target_from_entry(entry: Mapping[str, Any]) -> RuntimeTarget | None:
    runtime_target = _runtime_target(entry)
    service = (
        entry.get("service_name")
        or entry.get("service")
        or entry.get("cloud_run_service")
        or runtime_target.get("service_name")
    )
    service_name = str(service or "").strip()
    if not service_name:
        return None
    account_scope = str(
        entry.get("ACCOUNT_GROUP")
        or entry.get("account_scope")
        or runtime_target.get("account_scope")
        or ""
    ).strip()
    region = str(entry.get("region") or entry.get("cloud_run_region") or "").strip()
    return RuntimeTarget(service_name=service_name, region=region, account_scope=account_scope)


def _dedupe_targets(targets: Iterable[RuntimeTarget]) -> list[RuntimeTarget]:
    by_service: dict[str, RuntimeTarget] = {}
    for target in targets:
        current = by_service.get(target.service_name)
        if current is None:
            by_service[target.service_name] = target
            continue
        by_service[target.service_name] = RuntimeTarget(
            service_name=target.service_name,
            region=current.region or target.region,
            account_scope=current.account_scope or target.account_scope,
        )
    return list(by_service.values())


def load_targets(*, env: Mapping[str, str]) -> list[RuntimeTarget]:
    targets: list[RuntimeTarget] = []

    raw_plan = str(env.get("SYNC_PLAN_JSON") or "").strip()
    if raw_plan:
        plan = _load_json_object(raw_plan, field_name="SYNC_PLAN_JSON")
        entries = plan.get("targets") if isinstance(plan, Mapping) else plan
        if not isinstance(entries, list):
            raise ReconcileError("SYNC_PLAN_JSON.targets must be a list")
        for entry in entries:
            if isinstance(entry, Mapping):
                target = _target_from_entry(entry)
                if target:
                    targets.append(target)

    raw_targets = str(env.get("CLOUD_RUN_SERVICE_TARGETS_JSON") or "").strip()
    if raw_targets:
        payload = _load_json_object(raw_targets, field_name="CLOUD_RUN_SERVICE_TARGETS_JSON")
        entries = payload.get("targets") if isinstance(payload, Mapping) else payload
        if not isinstance(entries, list):
            raise ReconcileError("CLOUD_RUN_SERVICE_TARGETS_JSON.targets must be a list")
        for entry in entries:
            if isinstance(entry, Mapping):
                target = _target_from_entry(entry)
                if target:
                    targets.append(target)

    raw_services = str(env.get("CLOUD_RUN_SERVICES") or env.get("CLOUD_RUN_SERVICE") or "").strip()
    for chunk in raw_services.replace(";", ",").replace("\n", ",").split(","):
        service_name = chunk.strip()
        if service_name:
            targets.append(RuntimeTarget(service_name=service_name))

    return _dedupe_targets(targets)


def _run(args: Sequence[str], *, json_output: bool = False, dry_run: bool = False) -> Any:
    printable = " ".join(args)
    if dry_run:
        print(f"DRY-RUN {printable}")
        return {} if json_output else ""
    completed = subprocess.run(
        list(args),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise ReconcileError(f"Command failed ({completed.returncode}): {printable}\n{detail}")
    if json_output:
        try:
            return json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise ReconcileError(f"Command did not return JSON: {printable}") from exc
    return completed.stdout


def _run_optional(args: Sequence[str], *, dry_run: bool = False) -> bool:
    printable = " ".join(args)
    if dry_run:
        print(f"DRY-RUN {printable}")
        return True
    completed = subprocess.run(
        list(args),
        check=False,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return completed.returncode == 0


def _traffic_on_latest(service: Mapping[str, Any], latest_revision: str) -> bool:
    traffic = service.get("status", {}).get("traffic") or []
    if not isinstance(traffic, list):
        return False
    for item in traffic:
        if not isinstance(item, Mapping):
            continue
        try:
            percent = int(item.get("percent") or 0)
        except (TypeError, ValueError):
            percent = 0
        if percent == 100 and (item.get("latestRevision") is True or item.get("revisionName") == latest_revision):
            return True
    return False


def _revision_commit(*, project: str, region: str, revision: str, dry_run: bool) -> str:
    payload = _run(
        [
            "gcloud",
            "run",
            "revisions",
            "describe",
            revision,
            f"--project={project}",
            f"--region={region}",
            "--format=json",
        ],
        json_output=True,
        dry_run=dry_run,
    )
    labels = payload.get("metadata", {}).get("labels") or {}
    return str(labels.get("commit-sha") or "").strip()


def ensure_latest_traffic(
    *,
    project: str,
    region: str,
    targets: Sequence[RuntimeTarget],
    expected_commit: str,
    dry_run: bool,
) -> None:
    for target in targets:
        target_region = target.region or region
        if not target_region:
            raise ReconcileError(f"Region is required for {target.service_name}")
        service = _run(
            [
                "gcloud",
                "run",
                "services",
                "describe",
                target.service_name,
                f"--project={project}",
                f"--region={target_region}",
                "--format=json",
            ],
            json_output=True,
            dry_run=dry_run,
        )
        status = service.get("status", {}) if isinstance(service, Mapping) else {}
        latest = str(status.get("latestReadyRevisionName") or "").strip()
        if not latest:
            raise ReconcileError(f"Unable to resolve latest ready revision for {target.service_name}")
        if expected_commit:
            actual_commit = _revision_commit(
                project=project,
                region=target_region,
                revision=latest,
                dry_run=dry_run,
            )
            if actual_commit != expected_commit:
                raise ReconcileError(
                    f"{target.service_name} latest revision {latest} commit {actual_commit!r} "
                    f"does not match expected {expected_commit!r}"
                )
        if not _traffic_on_latest(service, latest):
            print(f"Updating {target.service_name} traffic to latest revision {latest}.")
            _run(
                [
                    "gcloud",
                    "run",
                    "services",
                    "update-traffic",
                    target.service_name,
                    f"--project={project}",
                    f"--region={target_region}",
                    "--to-latest",
                    "--quiet",
                ],
                dry_run=dry_run,
            )
        verified = _run(
            [
                "gcloud",
                "run",
                "services",
                "describe",
                target.service_name,
                f"--project={project}",
                f"--region={target_region}",
                "--format=json",
            ],
            json_output=True,
            dry_run=dry_run,
        )
        verified_latest = str(verified.get("status", {}).get("latestReadyRevisionName") or "").strip()
        if not verified_latest:
            raise ReconcileError(f"Unable to resolve latest ready revision for {target.service_name}")
        if not _traffic_on_latest(verified, verified_latest):
            raise ReconcileError(f"{target.service_name} traffic is not 100% on latest revision")
        print(f"Cloud Run traffic OK for {target.service_name}: {verified_latest}")


def _legacy_jobs_for_target(platform: str, target: RuntimeTarget) -> list[str]:
    service = target.service_name
    jobs = [f"{service}-probe-scheduler", f"{service}-precheck-scheduler"]
    if service.endswith("-service"):
        base = service[: -len("-service")]
        jobs.extend([f"{base}-probe-scheduler", f"{base}-precheck-scheduler"])

    if platform == "longbridge":
        scope = service
        if scope.startswith("longbridge-quant-"):
            scope = scope[len("longbridge-quant-") :]
        if scope.endswith("-service"):
            scope = scope[: -len("-service")]
        if scope and scope != service:
            jobs.append(f"lb-{scope}-backup-execution")
    elif platform == "ibkr":
        canonical_precheck = f"{service.removesuffix('-service')}-precheck-scheduler"
        jobs.remove(canonical_precheck)
        jobs.append("interactive-brokers-monitor-dispatcher-scheduler")
        prefix = "interactive-brokers-quant-live-"
        suffix = service
        if suffix.startswith(prefix):
            suffix = suffix[len(prefix) :]
        if suffix.endswith("-service"):
            suffix = suffix[: -len("-service")]
        if suffix.startswith("u"):
            jobs.append(f"ibkr-{suffix}-backup-execution")
            jobs.append(f"ibkr-{suffix}-pre-market-dry-run")

    return list(dict.fromkeys(jobs))


def _scheduler_locations(*, region: str, scheduler_location: str, targets: Sequence[RuntimeTarget], env: Mapping[str, str]) -> list[str]:
    locations = [scheduler_location or region]
    raw_extra = str(env.get("CLOUD_SCHEDULER_LEGACY_LOCATIONS") or "").strip()
    for chunk in raw_extra.replace(";", ",").split(","):
        if chunk.strip():
            locations.append(chunk.strip())
    for target in targets:
        if target.region:
            locations.append(target.region)
    return [item for item in dict.fromkeys(locations) if item]


def delete_legacy_schedulers(
    *,
    platform: str,
    project: str,
    region: str,
    scheduler_location: str,
    targets: Sequence[RuntimeTarget],
    env: Mapping[str, str],
    dry_run: bool,
) -> None:
    locations = _scheduler_locations(
        region=region,
        scheduler_location=scheduler_location,
        targets=targets,
        env=env,
    )
    for target in targets:
        for job in _legacy_jobs_for_target(platform, target):
            for location in locations:
                if not _run_optional(
                    [
                        "gcloud",
                        "scheduler",
                        "jobs",
                        "describe",
                        job,
                        f"--project={project}",
                        f"--location={location}",
                    ],
                    dry_run=dry_run,
                ):
                    continue
                print(f"Deleting legacy Cloud Scheduler job {job} in {location}.")
                _run(
                    [
                        "gcloud",
                        "scheduler",
                        "jobs",
                        "delete",
                        job,
                        f"--project={project}",
                        f"--location={location}",
                        "--quiet",
                    ],
                    dry_run=dry_run,
                )


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("ibkr", "longbridge"), required=True)
    parser.add_argument("--project", default=os.environ.get("GCP_PROJECT_ID", ""))
    parser.add_argument("--region", default=os.environ.get("CLOUD_RUN_REGION", ""))
    parser.add_argument("--scheduler-location", default=os.environ.get("CLOUD_SCHEDULER_LOCATION", ""))
    parser.add_argument("--expected-commit", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--ensure-latest-traffic", action="store_true")
    parser.add_argument("--delete-legacy-schedulers", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if not args.project:
        raise ReconcileError("--project or GCP_PROJECT_ID is required")
    targets = load_targets(env=os.environ)
    if not targets:
        raise ReconcileError("No Cloud Run targets resolved from SYNC_PLAN_JSON, CLOUD_RUN_SERVICE_TARGETS_JSON, or CLOUD_RUN_SERVICE")
    if args.ensure_latest_traffic:
        ensure_latest_traffic(
            project=args.project,
            region=args.region,
            targets=targets,
            expected_commit=args.expected_commit,
            dry_run=args.dry_run,
        )
    if args.delete_legacy_schedulers:
        delete_legacy_schedulers(
            platform=args.platform,
            project=args.project,
            region=args.region,
            scheduler_location=args.scheduler_location,
            targets=targets,
            env=os.environ,
            dry_run=args.dry_run,
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReconcileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
