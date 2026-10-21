#!/usr/bin/env python3
"""One fixed IBKR notification release; no trading, IAM, Scheduler or traffic shift.

The restricted runner holds the original template only in memory. Reports contain
hashes/booleans, never environment values, target bodies or secret references.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from scripts.verify_cloud_run_no_traffic_deploy import _active_traffic, _digest
from scripts.verify_image_ues_release_binding import check, new_report, revision_from_lockfile

SOURCE = "07a98e1559ec1acf7199f419facfe5c3ad5f124d"
PROJECT = "interactivebrokersquant"
REGION = "us-central1"
RUN_API = f"https://{REGION}-run.googleapis.com/apis/serving.knative.dev/v1/namespaces/{PROJECT}"
DEPLOYER = f"ibkr-platform-deploy@{PROJECT}.iam.gserviceaccount.com"
RUNTIME = f"ibkr-platform-runtime@{PROJECT}.iam.gserviceaccount.com"
REPOSITORY = f"{REGION}-docker.pkg.dev/{PROJECT}/cloud-run-source-deploy/interactivebrokersplatform/cloud-run"
BASE_DIGEST = "sha256:9fca942d37139f2343aae2c1da2d2ba83610e8df0cf63a27e579774ba3940885"
BASE_IMAGE = f"{REPOSITORY}@{BASE_DIGEST}"
QUALIFIED_DIGEST = "sha256:458a3af3910a0a636841ba3b0d3cb547604ac960c0aa47d03e1c17dc89f2aa3a"
QUALIFIED_IMAGE = f"{REPOSITORY}@{QUALIFIED_DIGEST}"
QUALIFICATION_RUN = 39512119214
READBACK_RUN = "39517314697"
READBACK_REVISION_HASH = "34d03b94a1c4d621cf8db3463817bddf7b21808a9750c047160eaa9687861433"
QUALIFIED_BASELINE = "61c14e3e3382828722a25a09d12a88ef808fa3f43ea3347bfa126bc3a930bc99"
SOURCE_LOCK_HASH = "b77d42e4d5ed6b644a1fa5c5b037c8dc1957a2d9dfc523045e45f0c89fa86f90"
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


def run(command: list[str], *, data: bytes | None = None, timeout: int = 60) -> bytes:
    env = dict(os.environ, CLOUDSDK_CORE_DISABLE_FILE_LOGGING="true", CLOUDSDK_CORE_LOG_HTTP="false")
    try:
        result = subprocess.run(command, input=data, capture_output=True, timeout=timeout, env=env, check=False)
    except subprocess.TimeoutExpired:
        raise ReleaseError("command_timeout_no_retry") from None
    if result.returncode:
        # A CLI method name or message is not a structured HTTP/IAM status.
        raise ReleaseError(f"native_command_exit_{result.returncode}_no_retry")
    return result.stdout


def cloud(args: list[str]) -> dict:
    require(len(args) == 4 and args[:3] == ["run", "services", "describe"], "cloud_service_reader_only")
    return json.loads(run(["gcloud", *args, f"--project={PROJECT}", f"--region={REGION}", "--format=json", "--quiet"]))


def authorized_session():
    """Consume the original Actions ADC through the official SDK, without export."""
    require(bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")), "original_actions_ADC_required")
    sdk = Path(run(["gcloud", "info", "--format=value(installation.sdk_root)"]).decode().strip())
    vendor = sdk / "lib" / "third_party"
    require(vendor.is_dir(), "official_auth_client_unavailable")
    sys.path.insert(0, str(vendor))
    try:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        require(getattr(credentials, "service_account_email", None) == DEPLOYER, "original_ADC_identity_required")
        return AuthorizedSession(credentials, max_refresh_attempts=0)
    except ReleaseError:
        raise
    except Exception:
        raise ReleaseError("official_ADC_client_unavailable") from None


def read_revision(name: str, report: dict, *, phase: str, session, allow_missing: bool = False) -> dict:
    """Only an HTTP 404 with structured NOT_FOUND may establish absence."""
    require(bool(re.fullmatch(r"[a-z][a-z0-9-]{0,62}", name)), "invalid_revision_name")
    require(phase in {"original", "candidate_absence", "candidate_readiness"}, "invalid_revision_query_phase")
    entry = {"phase": phase, "revision_sha256": sha(name), "http_status": None, "sdk_status": "UNKNOWN", "sdk_code": None, "decision": "stop"}
    report.setdefault("revision_queries", []).append(entry)
    try:
        response = session.get(f"{RUN_API}/revisions/{name}",
                               timeout=60, allow_redirects=False)
    except Exception:
        raise ReleaseError("revision_get_transport_failed_no_retry") from None
    status = response.status_code
    require(type(status) is int and 100 <= status <= 599, "revision_get_unknown_HTTP_status_no_retry")
    entry["http_status"] = status
    try:
        payload = response.json()
    except Exception:
        raise ReleaseError(f"revision_get_http_{status}_unknown_payload_no_retry") from None
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        states = {"NOT_FOUND", "PERMISSION_DENIED", "UNAUTHENTICATED", "INVALID_ARGUMENT", "FAILED_PRECONDITION", "ABORTED", "UNAVAILABLE", "INTERNAL", "UNKNOWN", "DEADLINE_EXCEEDED", "RESOURCE_EXHAUSTED"}
        entry["sdk_status"] = error.get("status") if isinstance(error.get("status"), str) and error["status"] in states else "UNKNOWN"
        entry["sdk_code"] = error.get("code") if type(error.get("code")) is int else None
    if status == 200:
        require(isinstance(payload, dict) and "error" not in payload and isinstance(payload.get("metadata"), dict) and payload["metadata"].get("name") == name and isinstance(payload.get("spec"), dict), "revision_get_invalid_success_payload_no_retry")
        entry.update(sdk_status="OK", decision="found")
        return payload
    if status == 404 and entry["sdk_status"] == "NOT_FOUND" and entry["sdk_code"] == 404 and allow_missing:
        entry["decision"] = "not_found"
        return {}
    raise ReleaseError(f"revision_get_http_{status}_{entry['sdk_status']}_no_retry")


def replace_service(body: dict, *, session=None) -> None:
    """Preserve the original CAS token; never retry a write or export its body."""
    name = body["metadata"]["name"]
    require(sha(name) == SERVICE_HASH and bool(body["metadata"].get("resourceVersion")), "fixed_service_and_concurrency_token_required")
    owned = session is None
    if owned:
        session = authorized_session()
    try:
        response = session.put(f"{RUN_API}/services/{name}",
                               json=body, timeout=60, allow_redirects=False)
        status = response.status_code
    except Exception:
        raise ReleaseError("cloud_run_replace_transport_failed_no_retry") from None
    finally:
        if owned:
            session.close()
    require(status == 200, "cloud_run_replace_http_" + str(int(status)) + "_no_retry")


def validate_dispatch(env: dict[str, str]) -> str:
    require(env.get("GITHUB_EVENT_NAME") == "workflow_dispatch" and env.get("GITHUB_REF") == "refs/heads/main", "trusted_main_dispatch_required")
    require(env.get("GITHUB_RUN_ATTEMPT") == "1", "rerun_not_authorized")
    require(env.get("ENABLE_GITHUB_CLOUD_RUN_DEPLOY") == "true", "repository_deployment_gate_disabled")
    require(env.get("INPUT_SOURCE_COMMIT") == SOURCE, "fixed_notification_source_required")
    require(env.get("INPUT_TARGET") == "configured" and not env.get("INPUT_CONFIGURED_SERVICE"), "fixed_private_target_alias_required")
    require(env.get("INPUT_DEPLOY_IMAGE") == "false", "fixed_image_reuse_required_no_rebuild")
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


def verify_source(source: Path) -> None:
    require(run(["git", "-C", str(source), "rev-parse", "HEAD"]).decode().strip() == SOURCE, "source_checkout_mismatch")
    for relative, expected in SOURCE_FILES.items():
        path = source / relative
        require(path.is_file() and not path.is_symlink() and sha(path.read_bytes()) == expected, "source_file_mismatch")
    require(sha((source / "uv.lock").read_bytes()) == SOURCE_LOCK_HASH, "source_lock_changed")
    require(revision_from_lockfile(source / "uv.lock") == PACKAGES["us_equity_strategies-0.7.60"], "source_ues_pin_changed")


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


def verify_readback(before: dict, original: dict, after: dict, revision: dict, expected: dict) -> None:
    require(routes(after) == routes(before), "routes_changed_stop_without_rollback")
    desired_routes = after.get("spec", {}).get("traffic", [])
    require(not any(route.get("latestRevision") for route in desired_routes) and
            routes({"status": {"traffic": desired_routes}}) == routes(before), "desired_routes_changed")
    require(metadata(after.get("metadata", {}), template=False) == metadata(before.get("metadata", {}), template=False), "service_metadata_changed")
    require({key: value for key, value in after.get("spec", {}).items() if key not in {"template", "traffic"}} ==
            {key: value for key, value in before.get("spec", {}).items() if key not in {"template", "traffic"}}, "service_spec_changed")
    baseline = nonnotification(original)
    require(nonnotification(revision) == baseline, "revision_nonnotification_template_changed")
    ingress_key = "run.googleapis.com/ingress"
    ingress = baseline["metadata"]["annotations"].get(ingress_key)
    require(isinstance(ingress, str) and ingress in {"all", "internal", "internal-and-cloud-load-balancing"}, "qualified_ingress_unknown")
    require(all(item.get("metadata", {}).get("annotations", {}).get(ingress_key) == ingress for item in (before, after)),
            "service_ingress_baseline_changed")
    template = after.get("spec", {}).get("template", {})
    template_projection = nonnotification(template)
    expected_projection = copy.deepcopy(baseline)
    # ingress is Service-scoped. The provider omits only this key in RevisionTemplate.
    # Keep the complete Revision and Service-level ingress checks above intact.
    if ingress_key not in template_projection["metadata"]["annotations"]:
        expected_projection["metadata"]["annotations"].pop(ingress_key)
    require(template_projection == expected_projection, "service_template_nonnotification_changed")
    wanted = expected["spec"]["template"]
    for actual in (revision, template):
        require(actual["spec"]["containers"][0]["image"] == wanted["spec"]["containers"][0]["image"], "revision_image_mismatch")
        require(all(env_map(actual).get(key) == {"name": key, "value": value} for key, value in ROUTING.items()), "routing_literals_not_bound")
        require(runtime_target(actual) == runtime_target(original), "revision_target_changed")
        require(actual.get("metadata", {}).get("name") == wanted["metadata"]["name"] and
                all(actual.get("metadata", {}).get("labels", {}).get(key) == wanted["metadata"]["labels"][key]
                    for key in PROVENANCE_LABELS), "revision_provenance_mismatch")


def release(source: Path, report: dict) -> None:
    require(os.environ.get("INPUT_NOTIFICATION_READBACK_ONLY", "false") == "false", "readback_cannot_execute_release")
    run_id = validate_dispatch(dict(os.environ))
    verify_source(source)
    service_name = resolve_service(os.environ.get("CLOUD_RUN_SERVICE_TARGETS_JSON", ""))
    print("::add-mask::" + service_name)
    before = cloud(["run", "services", "describe", service_name])
    revision_name = original_revision(before)
    print("::add-mask::" + revision_name)
    session = authorized_session()
    try:
        original = read_revision(revision_name, report, phase="original", session=session)
        require(original["spec"]["serviceAccountName"] == RUNTIME, "original_runtime_identity_mismatch")
        require(original.get("status", {}).get("imageDigest", original["spec"]["containers"][0]["image"]).endswith(BASE_DIGEST), "original_image_changed")
        target = runtime_target(original)
        baseline = _digest(nonnotification(original))
        require(baseline == QUALIFIED_BASELINE, "qualified_nonnotification_baseline_changed")
        budget_guard(before, original)
        registry_cost_guard()
        # Adopt only the already qualified immutable image. No Docker/build/push path.
        registry_digest = run(["gcloud", "artifacts", "docker", "images", "describe", QUALIFIED_IMAGE, f"--project={PROJECT}", "--format=value(image_summary.digest)", "--quiet"]).decode().strip()
        require(registry_digest == QUALIFIED_DIGEST, "qualified_registry_digest_mismatch")
        report.update({"target": ALIAS, "source": SOURCE, "original_target_fingerprint": TARGET_HASH, "original_nonnotification_sha256": baseline,
                       "image_digest": QUALIFIED_DIGEST, "image_qualification_run": QUALIFICATION_RUN,
                       "image_evidence_scope": "reused_prior_static_qualification_and_immutable_registry_read",
                       "prior_installed_commits": PACKAGES, "build_commands": 0, "push_commands": 0})
        binding = new_report()
        binding["evidence_scope"] = "prior_qualified_image_and_current_original_target"
        require(not check({"image": PACKAGES["us_equity_strategies-0.7.60"], "lockfile": revision_from_lockfile(source / "uv.lock")}, [(ALIAS, target)], report=binding), "original_release_risk_binding_mismatch")
        report["release_risk_binding"] = binding
        fresh = cloud(["run", "services", "describe", service_name])
        require(bool(before.get("metadata", {}).get("resourceVersion")), "service_concurrency_token_required")
        require(fresh.get("metadata", {}).get("resourceVersion") == before.get("metadata", {}).get("resourceVersion") and fresh.get("spec") == before.get("spec") and routes(fresh) == routes(before), "service_changed_during_preflight")
        body = candidate(before, original, QUALIFIED_IMAGE, run_id)
        candidate_name = body["spec"]["template"]["metadata"]["name"]
        print("::add-mask::" + candidate_name)
        require(not read_revision(candidate_name, report, phase="candidate_absence", session=session, allow_missing=True), "candidate_revision_already_exists")
        report["revision_create_attempts"] = 1
        replace_service(body, session=session)
        report["candidate_revision_sha256"] = sha(candidate_name)
        for _ in range(24):
            revision = read_revision(candidate_name, report, phase="candidate_readiness", session=session, allow_missing=True)
            if any(condition.get("type") == "Ready" and condition.get("status") == "True" for condition in revision.get("status", {}).get("conditions", [])):
                after = cloud(["run", "services", "describe", service_name])
                verify_readback(before, original, after, revision, body)
                report.update({"status": "PASS_NO_TRAFFIC", "readiness": True, "traffic_unchanged": True, "nonnotification_unchanged": True, "routing_literals": ROUTING, "actual_loaded_ues": "NOT_ATTESTED"})
                return
            if any(condition.get("type") == "Ready" and condition.get("status") == "False" for condition in revision.get("status", {}).get("conditions", [])):
                raise ReleaseError("candidate_readiness_failed_no_retry")
            time.sleep(10)
        raise ReleaseError("candidate_readiness_timeout_no_retry")
    finally:
        session.close()


def difference_projection(before: dict, after: dict) -> list[dict]:
    """Paths/types only; never serialize runtime values or secret references."""
    result = []
    structural = {"spec", "metadata", "labels", "annotations", "containers", "env", "name", "value", "valueFrom",
                  "secretKeyRef", "key", "resources", "limits", "cpu", "memory", "ports", "containerPort", "protocol",
                  "command", "args", "serviceAccountName", "containerConcurrency", "timeoutSeconds", "volumes", "items",
                  "emptyDir", "medium", "sizeLimit", "secret", "secretName", "volumeMounts", "mountPath", "readOnly",
                  "startupProbe", "livenessProbe", "httpGet", "path", "port", "tcpSocket", "initialDelaySeconds",
                  "periodSeconds", "successThreshold", "failureThreshold", "nodeSelector"}
    provider_keys = {"run.googleapis.com/execution-environment", "run.googleapis.com/cpu-throttling",
                     "run.googleapis.com/startup-cpu-boost", "run.googleapis.com/ingress", "run.googleapis.com/ingress-status",
                     "run.googleapis.com/launch-stage", "run.googleapis.com/sessionAffinity", "run.googleapis.com/minScale",
                     "run.googleapis.com/maxScale", "autoscaling.knative.dev/minScale", "autoscaling.knative.dev/maxScale",
                     "client.knative.dev/user-image"}

    def segment(key: str, index: int) -> str:
        return key.replace("~", "~0").replace("/", "~1") if key in structural or key in provider_keys else f"<unlisted-key-{index}>"

    def walk(left, right, path: str) -> None:
        if not isinstance(left, (dict, list)) and type(left) is type(right) and left == right:
            return
        if isinstance(left, dict) and isinstance(right, dict):
            for index, key in enumerate(sorted(set(left) | set(right))):
                child = path + "/" + segment(key, index)
                if key not in left or key not in right:
                    result.append({"path": child, "change": "added" if key not in left else "removed",
                                   "before_type": type(left[key]).__name__ if key in left else "absent",
                                   "after_type": type(right[key]).__name__ if key in right else "absent"})
                else:
                    walk(left[key], right[key], child)
        elif isinstance(left, list) and isinstance(right, list):
            for index in range(max(len(left), len(right))):
                if index >= min(len(left), len(right)):
                    result.append({"path": path + f"/{index}", "change": "added" if index >= len(left) else "removed",
                                   "before_type": type(left[index]).__name__ if index < len(left) else "absent",
                                   "after_type": type(right[index]).__name__ if index < len(right) else "absent"})
                else:
                    walk(left[index], right[index], path + f"/{index}")
        else:
            entry = {"path": path, "change": "changed", "before_type": type(left).__name__, "after_type": type(right).__name__}
            if re.fullmatch(r"/spec/containers/\d+/resources/limits/(?:cpu|memory)", path) and isinstance(left, str) and isinstance(right, str):
                aliases = {"1", "1000m"} if path.endswith("/cpu") else {"512Mi", "536870912"}
                entry["approved_resource_alias_equivalent"] = left in aliases and right in aliases
            result.append(entry)

    walk(before, after, "")
    return result


def readback_only(source: Path, report: dict) -> None:
    """GET only the fixed service and the two already-authorized revisions."""
    require(os.environ.get("INPUT_NOTIFICATION_READBACK_ONLY") == "true", "explicit_readback_dispatch_required")
    validate_dispatch(dict(os.environ))
    verify_source(source)
    service_name = resolve_service(os.environ.get("CLOUD_RUN_SERVICE_TARGETS_JSON", ""))
    print("::add-mask::" + service_name)
    service = cloud(["run", "services", "describe", service_name])
    original_name = original_revision(service)
    print("::add-mask::" + original_name)
    session = authorized_session()
    try:
        original = read_revision(original_name, report, phase="original", session=session)
        require(_digest(nonnotification(original)) == QUALIFIED_BASELINE, "qualified_nonnotification_baseline_changed")
        runtime_target(original)
        expected = candidate(service, original, QUALIFIED_IMAGE, READBACK_RUN)
        candidate_name = expected["spec"]["template"]["metadata"]["name"]
        require(sha(candidate_name) == READBACK_REVISION_HASH, "fixed_readback_revision_mismatch")
        print("::add-mask::" + candidate_name)
        revision = read_revision(candidate_name, report, phase="candidate_readiness", session=session)
        baseline = nonnotification(original)
        projected = {}
        for label, actual in (("candidate_revision", revision), ("service_template", service["spec"]["template"])):
            normalized = nonnotification(actual)
            differences = difference_projection(baseline, normalized)
            projected[label] = {"nonnotification_sha256": _digest(normalized), "difference_count": len(differences),
                                "differences": differences[:64], "differences_truncated": len(differences) > 64,
                                "image_matches_qualified": actual["spec"]["containers"][0]["image"] == QUALIFIED_IMAGE,
                                "target_entry_unchanged": env_map(actual).get("RUNTIME_TARGET_JSON") == env_map(original).get("RUNTIME_TARGET_JSON"),
                                "routing_literals_match": all(env_map(actual).get(k) == {"name": k, "value": v} for k, v in ROUTING.items())}
        report.update({"status": "READBACK_ONLY_PROJECTED", "mode": "readback_only", "readback_source_run": READBACK_RUN,
                       "candidate_revision_sha256": READBACK_REVISION_HASH, "original_nonnotification_sha256": _digest(baseline),
                       "candidate_ready": any(c.get("type") == "Ready" and c.get("status") == "True" for c in revision.get("status", {}).get("conditions", [])),
                       "original_still_receives_100_percent": True, "service_template_is_fixed_candidate": service["spec"]["template"].get("metadata", {}).get("name") == candidate_name,
                       "projection": projected, "service_reads": 1, "revision_reads": 2, "write_operations": 0,
                       "image_digest": QUALIFIED_DIGEST, "actual_loaded_ues": "NOT_ATTESTED"})
        require(report["candidate_ready"], "fixed_candidate_readiness_not_true")
        require(report["service_template_is_fixed_candidate"], "service_template_revision_changed")
        # Current GET snapshot only; ingress is independently bound to the frozen
        # original Revision above, rather than relying on a self-comparison.
        verify_readback(service, original, service, revision, expected)
        report.update({"status": "READBACK_ONLY_VERIFIED_NO_TRAFFIC", "revision_nonnotification_unchanged": True,
                       "service_ingress_matches_qualified_baseline": True, "service_template_scope_unchanged": True,
                       "routing_literals": ROUTING, "verification_scope": "qualified_original_revision_and_current_service_snapshot"})
    finally:
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--readback-only", action="store_true")
    args = parser.parse_args()
    report = {"status": "FAILED_OR_NOT_EXECUTED", "revision_create_attempts": 0, "automatic_retries": 0, "IAM_or_scheduler_calls": 0,
              "revision_queries": [], "build_commands": 0, "push_commands": 0, "image_pull_commands": 0}
    try:
        require(args.execute != args.readback_only, "one_explicit_mode_required")
        if args.readback_only:
            readback_only(args.source, report)
        else:
            release(args.source, report)
    except ReleaseError as exc:
        report["error_code"] = str(exc)  # ReleaseError accepts only constant codes/permission tokens.
        report["status"] = "FAILED_STOPPED"
        print("Notification-only release stopped; no automatic retry or traffic rollback.")
        return 1
    except Exception:
        # Never print exception objects that might include opaque runtime data.
        report["status"] = "FAILED_STOPPED"
        report["error_code"] = "invalid_local_or_native_data"
        print("Notification-only release stopped; no automatic retry or traffic rollback.")
        return 1
    finally:
        args.report.write_text(json.dumps(report, sort_keys=True) + "\n")
        print("NOTIFICATION_RESULT_JSON=" + json.dumps(report, sort_keys=True))
    if args.readback_only:
        print("Verified existing fixed candidate read-only; no cloud writes.")
    else:
        print("Verified fixed image adoption and zero-traffic revision; production traffic unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
