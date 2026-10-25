"""One protected TQQQ image update; no configuration, traffic or broker writes.

All CLI output and financial configuration stay in the authenticated runner.
The public receipt contains only fixed statuses and reviewed code/image identity.
An ambiguous mutation stops immediately; this helper never retries a command.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

PROFILE = "tqqq_growth_income"
REPOSITORY = "QuantStrategyLab/InteractiveBrokersPlatform"
IDENTITY_FIELDS = ("service_name", "strategy_profile", "platform_id", "account_scope", "deployment_selector", "account_selector")
PROGRESS_FIELDS = ("image_push_attempted", "image_push_completed", "keep_attempted", "keep_completed",
                   "service_update_attempted", "service_update_completed", "readback_verified")
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
NAME = re.compile(r"[a-z][a-z0-9-]{0,62}\Z")
TOOL_ANNOTATIONS = frozenset({
    "run.googleapis.com/client-name", "run.googleapis.com/client-version",
    "run.googleapis.com/operation-id", "serving.knative.dev/creator",
    "serving.knative.dev/lastModifier",
})
REVISION_CONTROLLER_LABELS = frozenset({
    "serving.knative.dev/service", "serving.knative.dev/configuration",
    "serving.knative.dev/route", "serving.knative.dev/configurationGeneration",
    "serving.knative.dev/serviceUid",
})
BUILD_LABELS = frozenset({"commit-sha", "github-run-id"})


class ReleaseError(RuntimeError):
    """The message is a fixed stage code, never source text."""


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseError("invalid_json")
        result[key] = value
    return result


def _json(text: str):
    def reject_constant(_):
        raise ReleaseError("invalid_json")
    try:
        return json.loads(text, object_pairs_hook=_pairs, parse_constant=reject_constant)
    except (TypeError, ValueError):
        raise ReleaseError("invalid_json") from None


def command(args: list[str], *, stage: str, timeout: int = 60, env=None) -> str:
    child_env = {**(os.environ if env is None else env),
                 "CLOUDSDK_CORE_DISABLE_FILE_LOGGING": "true", "CLOUDSDK_CORE_LOG_HTTP": "false",
                 "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"}
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=False,
                                timeout=timeout, env=child_env)
    except (OSError, subprocess.TimeoutExpired):
        raise ReleaseError(stage) from None
    if result.returncode:
        raise ReleaseError(stage)
    return result.stdout


@dataclass(frozen=True)
class Context:
    project: str
    region: str
    service: str
    target: dict[str, Any]
    source_commit: str
    registry_host: str
    rollback: tuple[str, ...]


def context(env: Mapping[str, str]) -> Context:
    if (env.get("GITHUB_REF") != "refs/heads/main" or env.get("GITHUB_RUN_ATTEMPT") != "1"
            or env.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or env.get("GITHUB_REPOSITORY") != REPOSITORY
            or env.get("PILOT_DEPLOY_GATE") != "true" or env.get("PILOT_PROFILE") != PROFILE
            or not SHA.fullmatch(env.get("PILOT_SOURCE_COMMIT", ""))
            or env.get("PILOT_SOURCE_COMMIT") != env.get("GITHUB_SHA")
            or not str(env.get("GITHUB_RUN_ID", "")).isdigit()):
        raise ReleaseError("dispatch_not_admitted")
    project, region = env.get("GCP_PROJECT_ID", ""), env.get("CLOUD_RUN_REGION", "")
    if not NAME.fullmatch(project) or not NAME.fullmatch(region):
        raise ReleaseError("infrastructure_unavailable")
    # Reuse the original inventory/default semantics, without building a sync
    # plan, fetching another configuration or touching sibling target admission.
    from scripts.build_cloud_run_env_sync_plan import (
        _load_target_entries, _resolve_runtime_target, _target_field,
    )
    try:
        _json(env.get("CLOUD_RUN_SERVICE_TARGETS_JSON", ""))
        entries, defaults, per_service = _load_target_entries(env)
        if not per_service:
            raise ReleaseError("inventory_unavailable")
        matches = []
        for entry in entries:
            target = _resolve_runtime_target(entry, defaults, {}, True)
            if target.get("strategy_profile") != PROFILE:
                continue
            service = next((str(_target_field(entry, defaults, key)).strip()
                            for key in ("service", "service_name", "cloud_run_service")
                            if _target_field(entry, defaults, key)), target.get("service_name", ""))
            accounts = target.get("account_selector")
            if (not NAME.fullmatch(service) or target.get("service_name") != service
                    or target.get("platform_id") != "ibkr"
                    or not isinstance(accounts, list) or len(accounts) != 1
                    or not isinstance(accounts[0], str) or not re.fullmatch(r"(?:U|DU)[0-9]+", accounts[0])
                    or not target.get("account_scope") or not target.get("deployment_selector")):
                raise ReleaseError("target_not_admitted")
            matches.append((service, target))
        if len(matches) != 1:
            raise ReleaseError("target_not_unique")
    except ReleaseError:
        raise
    except Exception:
        raise ReleaseError("inventory_invalid") from None
    rollback = tuple(chunk.strip() for chunk in env.get("IBKR_ARTIFACT_REGISTRY_ROLLBACK_DIGESTS", "").split(",") if chunk.strip())
    if len(set(rollback)) < 2 or any(not DIGEST.fullmatch(item) for item in rollback):
        raise ReleaseError("rollback_evidence_unavailable")
    host = env.get("GCP_ARTIFACT_REGISTRY_HOSTNAME") or f"{region}-docker.pkg.dev"
    if not re.fullmatch(r"[a-z0-9-]+-docker\.pkg\.dev", host):
        raise ReleaseError("registry_unavailable")
    service, target = matches[0]
    return Context(project, region, service, target, env["PILOT_SOURCE_COMMIT"], host, rollback)


def bootstrap_identity(source: Path, destination: Path) -> None:
    """Use the original fixed WIF declaration; never copy a real mapping here."""
    try:
        text = (source / ".github/workflows/sync-cloud-run-env-protected.yml").read_text(encoding="utf-8")
        matches = re.findall(r"(?m)^  GCP_WORKLOAD_IDENTITY_PROVIDER: (projects/[0-9]+/locations/global/workloadIdentityPools/[A-Za-z0-9_-]+/providers/[A-Za-z0-9_-]+)$", text)
        if len(matches) != 1:
            raise ReleaseError("identity_source_unavailable")
        print(f"::add-mask::{matches[0]}")
        with destination.open("a", encoding="utf-8") as output:
            output.write(f"PILOT_WIF_PROVIDER={matches[0]}\n")
    except OSError:
        raise ReleaseError("identity_source_unavailable") from None


def _metadata(value: dict) -> dict:
    metadata = value.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ReleaseError("configuration_incomplete")
    labels = {k: v for k, v in (metadata.get("labels") or {}).items()
              if k not in REVISION_CONTROLLER_LABELS}
    annotations = {k: v for k, v in (metadata.get("annotations") or {}).items()
                   if k not in TOOL_ANNOTATIONS}
    return {"labels": labels, "annotations": annotations}


def _configuration(template: dict, *, without_image: bool = False) -> dict:
    if not isinstance(template, dict) or not isinstance(template.get("spec"), dict):
        raise ReleaseError("configuration_incomplete")
    spec = copy.deepcopy(template["spec"])
    containers = spec.get("containers")
    if not isinstance(containers, list) or len(containers) != 1 or not isinstance(containers[0], dict):
        raise ReleaseError("container_not_unique")
    if not isinstance(containers[0].get("image"), str) or not containers[0]["image"]:
        raise ReleaseError("image_unavailable")
    if without_image:
        containers[0].pop("image")
    return {"metadata": _metadata(template), "spec": spec}


def _runtime_target(template: dict) -> dict:
    container = _configuration(template)["spec"]["containers"][0]
    rows = container.get("env")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ReleaseError("runtime_target_unavailable")
    env = {}
    for row in rows:
        name = row.get("name")
        if not isinstance(name, str) or name in env:
            raise ReleaseError("runtime_target_unavailable")
        env[name] = row
    values = []
    for name in ("RUNTIME_TARGET_JSON", "QSL_RUNTIME_TARGET_JSON"):
        if name in env:
            row = env[name]
            if "valueFrom" in row or not isinstance(row.get("value"), str):
                raise ReleaseError("runtime_target_unavailable")
            if row["value"]:
                values.append(_json(row["value"]))
    if not values or not isinstance(values[0], dict) or any(value != values[0] for value in values):
        raise ReleaseError("runtime_target_unavailable")
    return values[0]


def _ready(value: dict) -> bool:
    return any(row.get("type") == "Ready" and row.get("status") == "True"
               for row in (value.get("status", {}).get("conditions") or []) if isinstance(row, dict))


def _traffic(service: dict) -> tuple[list[dict], str]:
    rows = service.get("status", {}).get("traffic")
    if not isinstance(rows, list) or not rows:
        raise ReleaseError("traffic_unavailable")
    retained, active, total = [], [], 0
    for row in rows:
        if not isinstance(row, dict) or type(row.get("percent", 0)) is not int:
            raise ReleaseError("traffic_unavailable")
        percent, revision = row.get("percent", 0), row.get("revisionName", "")
        if not 0 <= percent <= 100 or not isinstance(revision, str) or not NAME.fullmatch(revision):
            raise ReleaseError("traffic_unavailable")
        total += percent
        if percent:
            active.append(revision)
        tag = row.get("tag", "")
        if not isinstance(tag, str):
            raise ReleaseError("traffic_unavailable")
        if percent or tag:
            retained.append({"revision": revision, "percent": percent, "tag": tag, "url": row.get("url")})
    if total != 100 or len(active) != 1:
        raise ReleaseError("serving_not_unique")
    return sorted(retained, key=lambda row: (row["revision"], row["tag"])), active[0]


def _service_configuration(service: dict) -> dict:
    spec = copy.deepcopy(service.get("spec"))
    if not isinstance(spec, dict):
        raise ReleaseError("configuration_incomplete")
    spec.pop("traffic", None)
    spec["template"] = _configuration(spec.get("template"), without_image=True)
    return {"metadata": _metadata(service), "spec": spec}


def _no_manual_scaling(service: dict) -> None:
    for node in (service, service.get("spec", {}).get("template", {})):
        annotations = _metadata(node)["annotations"]
        if ("run.googleapis.com/manualInstanceCount" in annotations
                or annotations.get("run.googleapis.com/scalingMode", "automatic") not in {"automatic", "auto"}):
            raise ReleaseError("manual_scaling_requires_review")
        # Nonzero minimums are preserved, not vetoed: an untagged revision with
        # zero traffic is not a revision-minimum allocation target. Unsupported
        # scaling representations still cannot be classified safely here.
        if node.get("scaling") or node.get("spec", {}).get("scaling"):
            raise ReleaseError("scaling_contract_unavailable")


def snapshot(ctx: Context, run=command) -> dict:
    common = [f"--project={ctx.project}", f"--region={ctx.region}"]
    def read(args):
        formatted = args if any(item.startswith("--format=") for item in args) else [*args, "--format=json"]
        return _json(run(["gcloud", *formatted], stage="source_read_failed"))
    service = read(["run", "services", "describe", ctx.service, *common])
    if not isinstance(service, dict) or service.get("metadata", {}).get("name") != ctx.service:
        raise ReleaseError("service_identity_unavailable")
    traffic, serving = _traffic(service)
    revision = read(["run", "revisions", "describe", serving, *common])
    iam = read(["run", "services", "get-iam-policy", ctx.service, *common])
    if (not isinstance(revision, dict) or not _ready(revision)
            or revision.get("metadata", {}).get("name") != serving
            or not isinstance(iam, dict) or not isinstance(iam.get("bindings"), list)):
        raise ReleaseError("source_read_incomplete")
    url = service.get("status", {}).get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ReleaseError("service_url_unavailable")
    summaries = read(["scheduler", "jobs", "list", f"--project={ctx.project}",
                      f"--location={ctx.region}", "--format=json(name,httpTarget.uri)"])
    if not isinstance(summaries, list):
        raise ReleaseError("scheduler_unavailable")
    jobs = []
    service_hosts = {urlsplit(url).netloc}
    service_hosts.update(urlsplit(row["url"]).netloc for row in traffic if isinstance(row.get("url"), str))
    for summary in summaries:
        if not isinstance(summary, dict):
            raise ReleaseError("scheduler_unavailable")
        uri = (summary.get("httpTarget") or {}).get("uri", "")
        if uri and urlsplit(uri).netloc in service_hosts:
            name = str(summary.get("name", "")).rsplit("/", 1)[-1]
            if not NAME.fullmatch(name):
                raise ReleaseError("scheduler_unavailable")
            job = read(["scheduler", "jobs", "describe", name, f"--project={ctx.project}", f"--location={ctx.region}"])
            if not isinstance(job, dict) or not job.get("name") or not job.get("state"):
                raise ReleaseError("scheduler_unavailable")
            jobs.append({key: job.get(key) for key in ("name", "state", "schedule", "timeZone", "httpTarget", "retryConfig", "attemptDeadline")})
    return {"service": service, "serving_revision": revision, "traffic": traffic,
            "serving": serving, "iam": iam, "scheduler": sorted(jobs, key=lambda row: row["name"])}


def validate_baseline(ctx: Context, state: dict) -> dict:
    service = state["service"]
    _no_manual_scaling(service)
    # Keep unused/failed image candidates. Only the complete non-image account
    # configuration must agree with the actual serving revision.
    current = _configuration(service.get("spec", {}).get("template"), without_image=True)
    serving = _configuration(state["serving_revision"], without_image=True)
    for config in (current, serving):
        config["metadata"]["labels"] = {key: value for key, value in config["metadata"]["labels"].items() if key not in BUILD_LABELS}
    if current != serving:
        raise ReleaseError("template_not_serving")
    actual = _runtime_target(state["serving_revision"])
    if any(actual.get(key) != ctx.target.get(key) for key in IDENTITY_FIELDS):
        raise ReleaseError("inventory_serving_mismatch")
    return actual


def unchanged(before: dict, after: dict) -> None:
    for key in ("traffic", "serving", "iam", "scheduler", "serving_revision"):
        if before[key] != after[key]:
            raise ReleaseError("baseline_changed")
    if _service_configuration(before["service"]) != _service_configuration(after["service"]):
        raise ReleaseError("configuration_changed")


def release(ctx: Context, source: Path, env: Mapping[str, str], run=command, take_snapshot=snapshot,
            progress: dict | None = None) -> dict:
    progress = {} if progress is None else progress
    for field in PROGRESS_FIELDS:
        progress.setdefault(field, False)
    if run(["git", "-C", str(source), "rev-parse", "HEAD"], stage="source_commit_unverified").strip() != ctx.source_commit:
        raise ReleaseError("source_commit_unverified")
    if run(["git", "-C", str(source), "status", "--porcelain"], stage="source_commit_unverified").strip():
        raise ReleaseError("source_commit_unverified")
    before = take_snapshot(ctx, run)
    actual_target = validate_baseline(ctx, before)
    # Derived identifiers are masked before a child process can emit them. All
    # child stdout/stderr are nevertheless captured rather than forwarded.
    for value in (ctx.service, *ctx.target["account_selector"]):
        print(f"::add-mask::{value}")
    repository = "cloud-run-source-deploy"
    image_repo = f"{ctx.registry_host}/{ctx.project}/{repository}/interactivebrokersplatform/cloud-run"
    image = f"{image_repo}:{ctx.source_commit}-{env['GITHUB_RUN_ID']}"
    run(["gcloud", "auth", "configure-docker", ctx.registry_host, "--quiet"], stage="docker_auth_failed")
    run(["docker", "build", "--pull", "--label", f"org.opencontainers.image.revision={ctx.source_commit}",
         "-t", image, str(source)], stage="image_build_failed", timeout=900)
    from scripts import verify_image_ues_release_binding as binding
    # The original guard emits only safe source SHAs, but keep even those
    # diagnostic lines private so the sole output remains the fixed receipt.
    import contextlib
    import io
    coverage = binding.new_report()
    try:
        installed = {"image": binding.revision_from_image(image), "lockfile": binding.revision_from_lockfile(source / "uv.lock")}
        with contextlib.redirect_stdout(io.StringIO()):
            failures = binding.check(installed, [(ctx.service, actual_target)], report=coverage)
    except Exception:
        raise ReleaseError("image_binding_unverified") from None
    if failures:
        raise ReleaseError("image_binding_unverified")
    labels = _json(run(["docker", "image", "inspect", image, "--format={{json .Config.Labels}}"], stage="image_source_unverified"))
    if not isinstance(labels, dict) or labels.get("org.opencontainers.image.revision") != ctx.source_commit:
        raise ReleaseError("image_source_unverified")
    progress["image_push_attempted"] = True
    run(["docker", "push", image], stage="image_push_unknown", timeout=300)
    progress["image_push_completed"] = True
    digest = run(["gcloud", "artifacts", "docker", "images", "describe", image,
                  f"--project={ctx.project}", "--format=value(image_summary.digest)"], stage="image_digest_unverified").strip()
    if not DIGEST.fullmatch(digest):
        raise ReleaseError("image_digest_unverified")
    immutable = f"{image_repo}@{digest}"
    repo_digests = _json(run(["docker", "image", "inspect", image, "--format={{json .RepoDigests}}"], stage="image_digest_unverified"))
    if not isinstance(repo_digests, list) or immutable not in repo_digests:
        raise ReleaseError("image_digest_unverified")
    from scripts.refresh_artifact_registry_keep import refresh
    progress["keep_attempted"] = True
    refresh(lambda args: run(["gcloud", *args], stage="keep_unknown"), project=ctx.project,
            region=ctx.region, repository_id=repository, image_repo=image_repo,
            candidate=immutable, rollback=list(ctx.rollback))
    progress["keep_completed"] = True
    fresh = take_snapshot(ctx, run)
    validate_baseline(ctx, fresh)
    unchanged(before, fresh)
    if (_configuration(before["service"]["spec"]["template"]) != _configuration(fresh["service"]["spec"]["template"])
            or before["service"]["status"].get("latestCreatedRevisionName") != fresh["service"]["status"].get("latestCreatedRevisionName")):
        raise ReleaseError("baseline_changed")
    progress["service_update_attempted"] = True
    raw_response = run(["gcloud", "run", "services", "update", ctx.service,
                        f"--project={ctx.project}", f"--region={ctx.region}", f"--image={immutable}",
                        "--no-traffic", "--quiet", "--format=json"], stage="service_update_unknown", timeout=300)
    try:
        response = _json(raw_response)
    except ReleaseError:
        raise ReleaseError("service_update_unknown") from None
    candidate_name = response.get("status", {}).get("latestCreatedRevisionName") if isinstance(response, dict) else None
    if not isinstance(candidate_name, str) or not NAME.fullmatch(candidate_name) or candidate_name == before["serving"]:
        raise ReleaseError("service_update_unknown")
    progress["service_update_completed"] = True
    after = take_snapshot(ctx, run)
    unchanged(before, after)
    if after["service"].get("status", {}).get("latestCreatedRevisionName") != candidate_name:
        raise ReleaseError("candidate_changed")
    candidate = _json(run(["gcloud", "run", "revisions", "describe", candidate_name,
                           f"--project={ctx.project}", f"--region={ctx.region}", "--format=json"], stage="candidate_read_failed"))
    if (not isinstance(candidate, dict) or not _ready(candidate)
            or candidate.get("metadata", {}).get("name") != candidate_name
            or _configuration(candidate, without_image=True) != _configuration(before["service"]["spec"]["template"], without_image=True)
            or _configuration(candidate)["spec"]["containers"][0]["image"] != immutable
            or after["service"]["spec"]["template"]["spec"]["containers"][0]["image"] != immutable):
        raise ReleaseError("candidate_unverified")
    progress["readback_verified"] = True
    return {"status": "verified_no_traffic", "stage": "complete", "image_digest": digest,
            "ues_binding_coverage": coverage["status"],
            "candidate_ready": True, "traffic_tags_unchanged": True, "configuration_unchanged": True,
            "iam_unchanged": True, "scheduler_unchanged": True, "serving_unchanged": True}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--bootstrap-identity", action="store_true")
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    receipt = {"schema_version": "ibkr_pilot_no_traffic.v1", "profile": PROFILE,
               "status": "unavailable", "stage": "dispatch_not_admitted",
               "execution_authorized": False, "risk_policy_applied": False,
               "breaker_reset": False, "traffic_shift_authorized": False}
    receipt.update({field: False for field in PROGRESS_FIELDS})
    code = 1
    try:
        ctx = context(os.environ)
        receipt["source_commit"] = ctx.source_commit
        if args.preflight:
            if args.bootstrap_identity:
                destination = os.environ.get("GITHUB_ENV")
                if not destination:
                    raise ReleaseError("identity_source_unavailable")
                bootstrap_identity(Path(__file__).resolve().parents[1], Path(destination))
            receipt.update(status="preflight_only", stage="preflight")
        elif args.source_dir is None:
            raise ReleaseError("source_commit_unverified")
        else:
            receipt.update(release(ctx, args.source_dir, os.environ, progress=receipt))
        code = 0
    except ReleaseError as exc:
        receipt["stage"] = str(exc)
    except Exception:
        receipt["stage"] = "unexpected_failure"
    try:
        args.receipt.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
        args.receipt.chmod(0o600)
    except OSError:
        receipt.update(status="unavailable", stage="safe_receipt_failed")
        code = 1
    print(json.dumps(receipt, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
