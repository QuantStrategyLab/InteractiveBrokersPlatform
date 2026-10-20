#!/usr/bin/env python3
"""One fixed IBKR notification release; no trading, IAM, Scheduler or traffic shift.

The restricted runner holds the original template only in memory. Reports contain
hashes/booleans, never environment values, target bodies or secret references.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import time

from scripts.verify_cloud_run_no_traffic_deploy import _active_traffic, _digest
from scripts.verify_image_ues_release_binding import GuardError, check, new_report, revision_from_lockfile

SOURCE = "07a98e1559ec1acf7199f419facfe5c3ad5f124d"
PROJECT = "interactivebrokersquant"
REGION = "us-central1"
DEPLOYER = f"ibkr-platform-deploy@{PROJECT}.iam.gserviceaccount.com"
RUNTIME = f"ibkr-platform-runtime@{PROJECT}.iam.gserviceaccount.com"
REPOSITORY = f"{REGION}-docker.pkg.dev/{PROJECT}/cloud-run-source-deploy/interactivebrokersplatform/cloud-run"
BASE_DIGEST = "sha256:9fca942d37139f2343aae2c1da2d2ba83610e8df0cf63a27e579774ba3940885"
BASE_IMAGE = f"{REPOSITORY}@{BASE_DIGEST}"
IMAGE_TAG = f"{REPOSITORY}:notification-only-40bf-07a98e1559ec"
SERVICE_HASH = "6445e856e49a76d83e1aaf938723166d227ba6c5e83b3e83d5bd259bb641019e"
REVISION_HASH = "bd167f3f9f771ae3c6b5adc5e973b2e6d27b2f23818d19e6a0a3eeb41f0bae5a"
TARGET_HASH = "c9912a3bf99c97605a0791e5ecbcc1d344444daf4f64b2c699d717b2d8b8c8ac"
ALIAS = "IBKR-TQQQ-A"
ROUTING = {"IBKR_UNIFIED_NOTIFICATION_ENABLED": "false", "QSL_ROUTINE_NOTIFICATION_MODE": "immediate", "QSL_DIGEST_ROUTINE_ORDERS": "false"}
PACKAGES = {
    "quant_platform_kit-1.0.0": "bd8d06e50f88927eb525c2c585752435ab24492a",
    "us_equity_strategies-0.7.60": "4a3943883cd6b5bbfe32a559e56a91b40a81b7ce",
    "hk_equity_strategies-0.4.7": "709e5e1cde7841aed538d94eb26b552b46cb7806",
}
SOURCE_FILES = {
    "application/account_new_risk_gate_support.py": "a0c1d19e1a53c1822c7c26266e477e86d793c1fbdb4242fd3aceaf9371b2d16d",
    "application/rebalance_service.py": "4d0f5c8c507164cebc1093f82fb5d32ab6bf24da67e17126e9099ef37e0090ff",
    "main.py": "354ce08e575fd01dcae9e567709fd8b16da4ded19550f8eaddae22740489f7ed",
    "notifications/archive_adapter.py": "a49a506b21de4080d6310737656dade92888211bf79d20cd5405ba7f400eccae",
    "notifications/digest_snapshot/__init__.py": "4aaf6f490cdd9c4ea2733fd5d705dc43ff6fa8ce39e161d717e7832a46977e78",
    "notifications/digest_snapshot/digest_candidates.py": "6272c0ca16ee63eecd9bcb25870df7f575d8fac3f487d3ab1ac69cc3831042a0",
    "notifications/digest_snapshot/digest_routing.py": "60c3f258c4d1a03bc49c7d03b392c02c316541726dfacb0b1d4fae6dbd2e5017",
    "notifications/digest_snapshot/provenance.json": "3bd6c4d01da3ebc439c9395fde5d16b567a0209331c757d3dfce1a028a43397c",
    "notifications/digest_snapshot/unified_contract.py": "7ba48d1d7c74e66be8cd3ff983c2117002776e82cda6675a084cfe0dc51aecc0",
    "notifications/display_names.json": "b453f762fff0186342c4eb2b60e95a24cce56bf988d6cfaeea47afe8b8077b54",
    "notifications/display_names.py": "20e26298cf48c77431257a4d73d2c9ef5ce0eda2a88323a23852d0119bdacf8f",
    "notifications/renderers.py": "71039d79ee536efa6f99d04fe06652ce2bd8230a1147fd3d18b2513f2a3b75b8",
    "notifications/telegram.py": "deea6e99c170cea7d8e37c8294d0af7b7adf24d7b63853f54e77f7a7ff914a62",
    "notifications/unified_adapter.py": "310836f763cf1e71a33adaad646b4a706ac45f34c5e33e0ed494a545577553a4",
    "scripts/daily_dry_run_digest.py": "3fa94cfc70cc10b1081a2083c4563b321b2abefb916a68021f7593919a8cf597",
    "scripts/emit_unified_digest_candidates.py": "7594dfed24c0d5e0ff3a87de8146ff1dcbcfad92f51abe59e5f61f9b002c7178",
    "scripts/execution_report_heartbeat.py": "26c8633d6b497ed866da49d2b1098f91f400da560348bea6e420b545b62a930f",
    "scripts/project_digest_candidates.py": "604fa28e7a5f5101c4954ec2d28ba877b513af9530e210b152e58f35056530d5",
}
VOLATILE_ANNOTATIONS = {
    "serving.knative.dev/creator", "serving.knative.dev/lastModifier",
    "run.googleapis.com/client-name", "run.googleapis.com/client-version",
    "run.googleapis.com/creatorEmail", "run.googleapis.com/lastModifierEmail",
    "run.googleapis.com/operation-id",
}
PROVENANCE_LABELS = {"commit-sha", "github-run-id"}


class ReleaseError(RuntimeError):
    """Only constant, non-sensitive error identifiers may leave this client."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ReleaseError(code)


def sha(value: bytes | str) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def run(command: list[str], *, data: bytes | None = None, timeout: int = 60, allow_missing: bool = False) -> bytes:
    env = dict(os.environ, CLOUDSDK_CORE_DISABLE_FILE_LOGGING="true", CLOUDSDK_CORE_LOG_HTTP="false")
    try:
        result = subprocess.run(command, input=data, capture_output=True, timeout=timeout, env=env, check=False)
    except subprocess.TimeoutExpired:
        raise ReleaseError("command_timeout_no_retry") from None
    if result.returncode:
        stderr = result.stderr.decode(errors="replace")
        denied = re.search(r"PERMISSION_DENIED|forbidden|\b403\b", stderr, re.I)
        if allow_missing and not denied and re.search(r"\bNOT_FOUND\b|\bnot found\b|\bHTTP[ /:]*404\b|\(404\)", stderr, re.I):
            return b""
        permissions = sorted(set(re.findall(r"\b(?:run|storage|artifactregistry|iam|logging)\.[A-Za-z.]+", stderr)))
        code = ",".join(permissions) if permissions else "native_command_failed"
        raise ReleaseError(code + ":no_retry")
    return result.stdout


def cloud(args: list[str], *, data: dict | None = None, allow_missing: bool = False) -> dict:
    output = run(["gcloud", *args, f"--project={PROJECT}", f"--region={REGION}", "--format=json", "--quiet"],
                 data=json.dumps(data).encode() if data is not None else None, allow_missing=allow_missing)
    return json.loads(output) if output else {}


def validate_dispatch(env: dict[str, str]) -> str:
    require(env.get("GITHUB_EVENT_NAME") == "workflow_dispatch" and env.get("GITHUB_REF") == "refs/heads/main", "trusted_main_dispatch_required")
    require(env.get("GITHUB_RUN_ATTEMPT") == "1", "rerun_not_authorized")
    require(env.get("ENABLE_GITHUB_CLOUD_RUN_DEPLOY") == "true", "repository_deployment_gate_disabled")
    require(env.get("INPUT_SOURCE_COMMIT") == SOURCE, "fixed_notification_source_required")
    require(env.get("INPUT_TARGET") == "configured" and not env.get("INPUT_CONFIGURED_SERVICE"), "fixed_private_target_alias_required")
    require(env.get("INPUT_DEPLOY_IMAGE") == "true", "image_build_required")
    require(all(env.get(key) == "false" for key in ("INPUT_SYNC_ENV", "INPUT_APPROVE_TRAFFIC_SHIFT", "INPUT_APPROVE_SCHEDULER_SYNC")), "broad_mutation_not_authorized")
    require(env.get("GCP_PROJECT_ID") == PROJECT and env.get("GCP_DEPLOY_SERVICE_ACCOUNT") == DEPLOYER and env.get("GCP_RUNTIME_SERVICE_ACCOUNT") == RUNTIME, "original_identity_binding_required")
    run_id = env.get("GITHUB_RUN_ID", "")
    require(bool(re.fullmatch(r"[0-9]{1,20}", run_id)), "invalid_run_id")
    return run_id


def resolve_service(raw: str) -> str:
    try:
        inventory = json.loads(raw)
        entries = inventory.get("targets", []) if isinstance(inventory, dict) else inventory
        names = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            target = entry.get("runtime_target") or entry.get("runtime_target_json") or {}
            target = json.loads(target) if isinstance(target, str) else target
            names.append(entry.get("service_name") or entry.get("service") or entry.get("cloud_run_service") or target.get("service_name"))
    except (ValueError, TypeError, AttributeError):
        raise ReleaseError("protected_inventory_unavailable") from None
    matches = [name for name in names if isinstance(name, str) and sha(name) == SERVICE_HASH]
    require(len(matches) == 1 and bool(re.fullmatch(r"[a-z][a-z0-9-]{0,62}", matches[0])), "fixed_target_not_uniquely_resolved")
    return matches[0]


def context(source: Path, destination: Path) -> None:
    require(run(["git", "-C", str(source), "rev-parse", "HEAD"]).decode().strip() == SOURCE, "source_checkout_mismatch")
    require(not destination.exists(), "context_already_exists")
    for relative, expected in SOURCE_FILES.items():
        path = source / relative
        require(path.is_file() and not path.is_symlink() and sha(path.read_bytes()) == expected, "source_file_mismatch")
    for relative in SOURCE_FILES:
        path = destination / "overlay" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((source / relative).read_bytes())
    (destination / "Dockerfile").write_text(f"FROM {BASE_IMAGE}\nCOPY --chown=1000:1000 overlay/ /app/\n")


def copy_file(container: str, path: str) -> bytes:
    raw = run(["docker", "cp", f"{container}:{path}", "-"])
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        members = archive.getmembers()
        require(len(members) == 1 and members[0].isfile() and members[0].size < 2_000_000, "unexpected_image_file")
        return archive.extractfile(members[0]).read()


def verify_image(source: Path) -> dict[str, str]:
    base_layers = json.loads(run(["docker", "image", "inspect", BASE_IMAGE, "--format", "{{json .RootFS.Layers}} "]))
    layers = json.loads(run(["docker", "image", "inspect", IMAGE_TAG, "--format", "{{json .RootFS.Layers}} "]))
    require(layers[:-1] == base_layers and len(layers) == len(base_layers) + 1, "base_layers_changed")
    container = run(["docker", "create", "--network=none", "--entrypoint=/bin/true", IMAGE_TAG]).decode().strip()
    require(bool(re.fullmatch(r"[0-9a-f]{64}", container)), "invalid_static_container")
    try:
        for path, expected in SOURCE_FILES.items():
            require(sha(copy_file(container, "/app/" + path)) == expected, "image_source_file_mismatch")
        installed = {}
        for dist, expected in PACKAGES.items():
            prefix = f"/app/.venv/lib/python3.12/site-packages/{dist}.dist-info/"
            metadata = copy_file(container, prefix + "METADATA").decode()
            direct = json.loads(copy_file(container, prefix + "direct_url.json"))
            require(direct.get("vcs_info", {}).get("commit_id") == expected, "installed_package_commit_changed")
            require("\nVersion: " + dist.rsplit("-", 1)[1] + "\n" in "\n" + metadata, "installed_package_version_changed")
            installed[dist] = expected
        require(revision_from_lockfile(source / "uv.lock") == PACKAGES["us_equity_strategies-0.7.60"], "source_ues_pin_changed")
        return installed
    finally:
        run(["docker", "rm", container])


def env_map(revision: dict) -> dict[str, dict]:
    entries = revision["spec"]["containers"][0].get("env", [])
    require(len({entry["name"] for entry in entries}) == len(entries), "duplicate_environment_name")
    return {entry["name"]: entry for entry in entries}


def runtime_target(revision: dict) -> dict:
    entry = env_map(revision).get("RUNTIME_TARGET_JSON", {})
    require("value" in entry and "valueFrom" not in entry, "inline_original_target_required_no_secret_read")
    try:
        target = json.loads(entry["value"])
    except (TypeError, ValueError):
        raise ReleaseError("original_target_invalid") from None
    baseline = {key: value for key, value in target.items() if key != "live_continuity"}
    require(sha(json.dumps(baseline, ensure_ascii=True, sort_keys=True, separators=(",", ":"))) == TARGET_HASH, "approved_target_fingerprint_mismatch")
    require(target.get("strategy_profile") == "tqqq_growth_income" and target.get("execution_mode") == "live" and target.get("dry_run_only") is False, "original_profile_mode_guard_mismatch")
    return target


def metadata(value: dict, *, template: bool) -> dict:
    labels = {key: item for key, item in value.get("labels", {}).items()
              if not key.startswith("serving.knative.dev/") and key not in {"cloud.googleapis.com/location", "run.googleapis.com/startupProbeType"}}
    if template:
        labels = {key: item for key, item in labels.items() if key not in PROVENANCE_LABELS}
    return {"labels": labels, "annotations": {key: item for key, item in value.get("annotations", {}).items() if key not in VOLATILE_ANNOTATIONS}}


def nonnotification(revision: dict) -> dict:
    spec = copy.deepcopy(revision["spec"])
    spec.pop("servingState", None)  # Provider lifecycle output, not template configuration.
    require(len(spec.get("containers", [])) == 1, "single_original_container_required")
    container = spec["containers"][0]
    container.pop("image", None)
    container["env"] = sorted((entry for name, entry in env_map(revision).items() if name not in ROUTING), key=lambda entry: entry["name"])
    return {"spec": spec, "metadata": metadata(revision.get("metadata", {}), template=True)}


def routes(service: dict) -> list[dict]:
    traffic = service.get("status", {}).get("traffic", [])
    _active_traffic(traffic)  # Reuse the existing validated percent parser.
    result = []
    for entry in traffic:
        require(bool(entry.get("revisionName")), "concrete_original_routes_required")
        require(0 <= int(entry.get("percent", 0)) <= 100, "invalid_original_traffic_percent")
        if int(entry.get("percent", 0)) > 0 or entry.get("tag"):
            result.append({key: entry[key] for key in ("revisionName", "percent", "tag") if key in entry})
    require(sum(int(entry.get("percent", 0)) for entry in result) == 100, "original_traffic_not_complete")
    return sorted(result, key=lambda entry: json.dumps(entry, sort_keys=True))


def original_revision(service: dict) -> str:
    active = _active_traffic(service.get("status", {}).get("traffic", []))
    require(len(active) == 1 and active[0]["percent"] == 100 and sha(active[0]["revisionName"]) == REVISION_HASH, "original_serving_revision_changed")
    return active[0]["revisionName"]


def budget_guard(service: dict, revision: dict) -> None:
    require(len(revision["spec"].get("containers", [])) == 1, "budget_single_container_required")
    resources = revision["spec"]["containers"][0].get("resources", {}).get("limits", {})
    require(resources.get("cpu") in {"1", "1000m"} and resources.get("memory") == "512Mi", "budget_original_resources_not_covered")
    for item in (service, revision):
        annotations = item.get("metadata", {}).get("annotations", {})
        require(all(annotations.get(key, "0") == "0" for key in ("autoscaling.knative.dev/minScale", "run.googleapis.com/minScale")), "budget_minimum_instances_not_covered")
        require(annotations.get("run.googleapis.com/cpu-throttling", "true") != "false", "budget_always_allocated_cpu_not_covered")


def registry_cost_guard() -> None:
    # A new automatic scan alone exceeds this release's USD 0.10 budget.
    # Read repository metadata, never Container Analysis or its denied endpoint.
    state = run(["gcloud", "artifacts", "repositories", "describe", "cloud-run-source-deploy",
                 f"--project={PROJECT}", f"--location={REGION}",
                 "--format=value(vulnerabilityScanningConfig.enablementState)", "--quiet"]).decode().strip()
    require(state == "SCANNING_DISABLED", "automatic_scan_cost_not_covered_or_unknown")


def candidate(service: dict, original: dict, image: str, run_id: str) -> dict:
    require(image.startswith(REPOSITORY + "@sha256:") and bool(re.fullmatch(r"[0-9a-f]{64}", image.rsplit(":", 1)[1])), "immutable_image_required")
    service_name = service["metadata"]["name"]
    remaining = 63 - len(service_name) - 1
    require(remaining >= 8, "revision_name_budget_too_short")
    name = service_name + "-" + ("nf" + sha(run_id)[:remaining - 2])
    result = {"apiVersion": "serving.knative.dev/v1", "kind": "Service", "metadata": {
        key: copy.deepcopy(service["metadata"][key]) for key in ("name", "namespace", "resourceVersion", "labels", "annotations") if key in service["metadata"]}, "spec": copy.deepcopy(service["spec"])}
    clean = metadata(original.get("metadata", {}), template=False)
    clean["labels"].update({"commit-sha": SOURCE, "github-run-id": run_id})
    clean["name"] = name
    result["metadata"]["annotations"] = {key: value for key, value in result["metadata"].get("annotations", {}).items() if key not in VOLATILE_ANNOTATIONS}
    spec = copy.deepcopy(original["spec"])
    spec.pop("servingState", None)
    spec["containers"][0]["image"] = image
    entries = copy.deepcopy(env_map(original))
    entries.update({key: {"name": key, "value": value} for key, value in ROUTING.items()})
    spec["containers"][0]["env"] = sorted(entries.values(), key=lambda entry: entry["name"])
    result["spec"]["template"] = {"metadata": clean, "spec": spec}
    result["spec"]["traffic"] = routes(service)
    require(nonnotification(result["spec"]["template"]) == nonnotification(original), "candidate_changed_nonnotification_template")
    require(runtime_target(result["spec"]["template"]) == runtime_target(original), "candidate_changed_target")
    return result


def verify_readback(before: dict, original: dict, after: dict, revision: dict, image: str) -> None:
    require(routes(after) == routes(before), "routes_changed_stop_without_rollback")
    require(metadata(after.get("metadata", {}), template=False) == metadata(before.get("metadata", {}), template=False), "service_metadata_changed")
    require({key: value for key, value in after.get("spec", {}).items() if key not in {"template", "traffic"}} ==
            {key: value for key, value in before.get("spec", {}).items() if key not in {"template", "traffic"}}, "service_spec_changed")
    require(nonnotification(revision) == nonnotification(original), "revision_nonnotification_template_changed")
    require(revision["spec"]["containers"][0]["image"] == image, "revision_image_mismatch")
    require(all(env_map(revision).get(key) == {"name": key, "value": value} for key, value in ROUTING.items()), "routing_literals_not_bound")
    require(runtime_target(revision) == runtime_target(original), "revision_target_changed")
    require(revision.get("metadata", {}).get("labels", {}).get("commit-sha") == SOURCE, "revision_provenance_mismatch")


def release(source: Path, destination: Path, report: dict) -> None:
    run_id = validate_dispatch(dict(os.environ))
    service_name = resolve_service(os.environ.get("CLOUD_RUN_SERVICE_TARGETS_JSON", ""))
    print("::add-mask::" + service_name)
    before = cloud(["run", "services", "describe", service_name])
    revision_name = original_revision(before)
    print("::add-mask::" + revision_name)
    original = cloud(["run", "revisions", "describe", revision_name])
    require(original["spec"]["serviceAccountName"] == RUNTIME, "original_runtime_identity_mismatch")
    require(original.get("status", {}).get("imageDigest", original["spec"]["containers"][0]["image"]).endswith(BASE_DIGEST), "original_image_changed")
    target = runtime_target(original)
    budget_guard(before, original)
    registry_cost_guard()
    report.update({"target": ALIAS, "source": SOURCE, "original_target_fingerprint": TARGET_HASH, "original_nonnotification_sha256": _digest(nonnotification(original))})
    # Do not rebuild an image after a successful prior push or an uncertain run.
    existing = run(["gcloud", "artifacts", "docker", "images", "describe", IMAGE_TAG, f"--project={PROJECT}", "--format=value(image_summary.digest)", "--quiet"], allow_missing=True)
    require(not existing.strip(), "candidate_tag_already_exists_do_not_rebuild")
    context(source, destination)
    run(["gcloud", "auth", "configure-docker", f"{REGION}-docker.pkg.dev", "--quiet"])
    # BuildKit's FROM cache alone need not expose the base to image inspect.
    run(["docker", "pull", BASE_IMAGE], timeout=180)
    run(["docker", "build", "--network=none", "--tag", IMAGE_TAG, str(destination)], timeout=600)
    installed = verify_image(source)
    report["installed_commits"] = installed
    binding = new_report()
    require(not check({"image": installed["us_equity_strategies-0.7.60"], "lockfile": revision_from_lockfile(source / "uv.lock")}, [(ALIAS, target)], report=binding), "original_release_risk_binding_mismatch")
    report["release_risk_binding"] = binding
    run(["docker", "push", IMAGE_TAG], timeout=180)
    digest = run(["gcloud", "artifacts", "docker", "images", "describe", IMAGE_TAG, f"--project={PROJECT}", "--format=value(image_summary.digest)", "--quiet"]).decode().strip()
    require(bool(re.fullmatch(r"sha256:[0-9a-f]{64}", digest)), "registry_digest_invalid")
    image = REPOSITORY + "@" + digest
    report["image_digest"] = digest
    fresh = cloud(["run", "services", "describe", service_name])
    require(bool(before.get("metadata", {}).get("resourceVersion")), "service_concurrency_token_required")
    require(fresh.get("metadata", {}).get("resourceVersion") == before.get("metadata", {}).get("resourceVersion") and fresh.get("spec") == before.get("spec") and routes(fresh) == routes(before), "service_changed_during_build")
    body = candidate(before, original, image, run_id)
    candidate_name = body["spec"]["template"]["metadata"]["name"]
    print("::add-mask::" + candidate_name)
    require(not cloud(["run", "revisions", "describe", candidate_name], allow_missing=True), "candidate_revision_already_exists")
    report["revision_create_attempts"] = 1
    cloud(["run", "services", "replace", "-", "--async"], data=body)
    report["candidate_revision_sha256"] = sha(candidate_name)
    for _ in range(24):
        revision = cloud(["run", "revisions", "describe", candidate_name], allow_missing=True)
        if any(condition.get("type") == "Ready" and condition.get("status") == "True" for condition in revision.get("status", {}).get("conditions", [])):
            after = cloud(["run", "services", "describe", service_name])
            verify_readback(before, original, after, revision, image)
            report.update({"status": "PASS_NO_TRAFFIC", "readiness": True, "traffic_unchanged": True, "nonnotification_unchanged": True, "routing_literals": ROUTING, "actual_loaded_ues": "NOT_ATTESTED"})
            return
        if any(condition.get("type") == "Ready" and condition.get("status") == "False" for condition in revision.get("status", {}).get("conditions", [])):
            raise ReleaseError("candidate_readiness_failed_no_retry")
        time.sleep(10)
    raise ReleaseError("candidate_readiness_timeout_no_retry")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--context", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    report = {"status": "FAILED_OR_NOT_EXECUTED", "revision_create_attempts": 0, "automatic_retries": 0, "IAM_or_scheduler_calls": 0}
    try:
        require(args.execute, "explicit_execution_required")
        release(args.source, args.context, report)
    except ReleaseError as exc:
        report["error_code"] = str(exc)  # ReleaseError accepts only constant codes/permission tokens.
        report["status"] = "FAILED_STOPPED"
        print("Notification-only release stopped; no automatic retry or traffic rollback.")
        return 1
    except (ValueError, KeyError, TypeError, OSError, GuardError):
        # Never print exception objects that might include opaque runtime data.
        report["status"] = "FAILED_STOPPED"
        report["error_code"] = "invalid_local_or_native_data"
        print("Notification-only release stopped; no automatic retry or traffic rollback.")
        return 1
    finally:
        args.report.write_text(json.dumps(report, sort_keys=True) + "\n")
    print("Verified fixed notification image and zero-traffic revision; production traffic unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
