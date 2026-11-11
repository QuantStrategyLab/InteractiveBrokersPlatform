"""Apply one bounded live maintenance change through the protected deployment job."""
from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from scripts.prepare_legacy_digest_overlay import build_verified_image
from scripts.summarize_historical_execution_cloud import resolve_private_service

API = "https://run.googleapis.com/v2/"
PROFILE = "soxl_soxx_trend_income"
PRIMARY_PROFILE = "tqqq_growth_income"
COMPATIBLE_DIGEST_PROFILE = "global_etf_rotation"
LEGACY_DIGEST_PROFILES = {"soxl": PROFILE, "russell": "russell_top50_leader_rotation"}
DIGEST_BINDINGS = (
    "DIGEST_CANDIDATES_GCS_URI", "DIGEST_STORE_TARGET", "DIGEST_TARGET_ID",
    "DIGEST_OPAQUE_ACCOUNT_UID", "IBKR_NOTIFICATION_MARKER_GCS_URI",
)
DIGEST_ROUTE = {
    "IBKR_UNIFIED_NOTIFICATION_ENABLED": "true",
    "QSL_ROUTINE_NOTIFICATION_MODE": "daily_digest",
    "QSL_DIGEST_ROUTINE_ORDERS": "true",
}
INCIDENT = (
    'resource.type="cloud_run_revision" AND timestamp>="2026-11-10T20:45:00Z" '
    'AND timestamp<"2026-11-10T20:50:00Z" AND severity>=ERROR'
)
MEMORY_ERROR = "Memory limit of 512 MiB exceeded with 514 MiB used"
REVISION_METADATA = {
    "name", "uid", "generation", "createTime", "updateTime", "deleteTime", "expireTime",
    "launchStage", "service", "reconciling", "conditions", "observedGeneration", "logUri",
    "satisfiesPzs", "scalingStatus", "containerStatuses", "creator", "etag",
}
TEMPLATE_METADATA = {"revision", "client", "clientVersion"}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def gcloud(*args):
    return subprocess.check_output(["gcloud", *args], text=True, stderr=subprocess.DEVNULL, timeout=45).strip()


def revision_path(service, revision):
    if re.fullmatch(r"[a-z0-9-]+", revision):
        return service + "/revisions/" + revision
    require(revision.startswith(service + "/revisions/"), "revision_service_mismatch")
    return revision


def verify_configuration(template, revision):
    expected = {k: v for k, v in template.items() if k not in TEMPLATE_METADATA}
    actual = {k: v for k, v in revision.items() if k not in REVISION_METADATA | TEMPLATE_METADATA}
    want, = expected["containers"]
    found, = actual["containers"]
    if "name" not in want:
        actual["containers"] = [{k: v for k, v in found.items() if k != "name"}]
    require(expected == actual, "revision_configuration_mismatch")


def wait_for_operation(request, operation):
    deadline = time.monotonic() + 240
    while not operation.get("done") and time.monotonic() < deadline:
        time.sleep(4)
        operation = request(operation["name"])
    require(operation.get("done") and not operation.get("error"), "cloud_run_operation_failed")


def notification_bindings(request, path, target, archive_prefix=None):
    """Read the exact binding already used by this account's private digest Job."""
    response = request(path.rsplit("/services/", 1)[0] + "/jobs?pageSize=100")
    require(not response.get("nextPageToken"), "digest_job_inventory_truncated")
    matches = []
    for job in response.get("jobs", []):
        containers = job.get("template", {}).get("template", {}).get("containers", [])
        if len(containers) != 1:
            continue
        env = {row["name"]: row.get("value") for row in containers[0].get("env", [])}
        if not all(env.get(key) for key in DIGEST_BINDINGS) or not env.get("RUNTIME_TARGET_JSON"):
            continue
        try:
            job_target = json.loads(env["RUNTIME_TARGET_JSON"])
        except (TypeError, ValueError):
            continue
        if job_target == target and env.get("QSL_ROUTINE_NOTIFICATION_MODE") == "daily_digest":
            require(archive_prefix is None or env.get("EXECUTION_REPORT_GCS_URI") == archive_prefix,
                    "legacy_digest_archive_binding_mismatch")
            require(job.get("terminalCondition", {}).get("state") == "CONDITION_SUCCEEDED", "digest_job_not_ready")
            matches.append({key: env[key] for key in DIGEST_BINDINGS})
    require(len(matches) == 1, "unique_same_account_digest_job_missing")
    return matches[0]


def activate_compatible_digest(request, path, target, container, env):
    require(env.get("IBKR_UNIFIED_NOTIFICATION_ENABLED", {}).get("value", "false") == "false"
            and env.get("QSL_ROUTINE_NOTIFICATION_MODE", {}).get("value", "immediate") == "immediate",
            "legacy_route_changed")
    desired = {**notification_bindings(request, path, target), **DIGEST_ROUTE}
    for key, value in desired.items():
        if key not in env:
            container["env"].append({"name": key, "value": value})
            continue
        entry = env[key]
        require(set(entry) == {"name", "value"} and (key in DIGEST_ROUTE or entry["value"] == value),
                "existing_notification_binding_changed")
        entry["value"] = value


def activate_legacy_digest(request, path, target, container, env, *, apply):
    prefix = env.get("EXECUTION_REPORT_GCS_URI", {}).get("value")
    require(isinstance(prefix, str) and prefix.startswith("gs://"), "legacy_execution_archive_missing")
    notification_bindings(request, path, target, archive_prefix=prefix)
    require(env.get("IBKR_UNIFIED_NOTIFICATION_ENABLED", {}).get("value", "false") == "false"
            and env.get("QSL_ROUTINE_NOTIFICATION_MODE", {}).get("value", "immediate") == "immediate",
            "legacy_route_changed")
    for key in ("QSL_ROUTINE_NOTIFICATION_MODE", "QSL_DIGEST_ROUTINE_ORDERS"):
        value = DIGEST_ROUTE[key]
        if key in env:
            require(set(env[key]) == {"name", "value"}, "legacy_notification_setting_not_literal")
            env[key]["value"] = value
        else:
            container["env"].append({"name": key, "value": value})
    if apply:
        container["image"] = build_verified_image(container["image"])


def release_template(request, path, service, active, active_path, legacy_profile):
    template = copy.deepcopy(service["template"])
    prior = None
    if (legacy_profile == LEGACY_DIGEST_PROFILES["russell"]
            and template["containers"][0]["image"] != active["containers"][0]["image"]):
        # Preserve the older, unpromoted image revision. The new candidate
        # inherits the serving configuration, including its source labels.
        prior_path = revision_path(path, service["latestCreatedRevision"])
        require(prior_path != active_path, "legacy_previous_stage_unconfirmed")
        prior_revision = request(prior_path)
        verify_configuration(template, prior_revision)
        prior = (prior_path, prior_revision["uid"])
        template["containers"][0]["image"] = active["containers"][0]["image"]
        if "labels" in active:
            template["labels"] = copy.deepcopy(active["labels"])
        else:
            template.pop("labels", None)
    verify_configuration(template, active)
    return template, prior


def repair(request, path, incident_revision=None, *, order_digest=False, account_digest=False,
           legacy_profile=None, apply=False):
    service = request(path)
    route, = service["traffic"]
    active, = [row for row in service["trafficStatuses"] if row.get("percent", 0) > 0]
    require(not service.get("reconciling") and route["type"] == "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION",
            "traffic_not_pinned")
    require(route["percent"] == active["percent"] == 100 and not route.get("tag") and not active.get("tag"),
            "traffic_not_single_untagged_revision")
    old_path = revision_path(path, active["revision"])
    require(old_path == revision_path(path, route["revision"]), "traffic_readback_mismatch")
    if not (order_digest or account_digest or legacy_profile):
        require(incident_revision and old_path == revision_path(path, incident_revision),
                "incident_revision_no_longer_serving")
    old = request(old_path)
    template, prior_stage = release_template(request, path, service, old, old_path, legacy_profile)
    container, = template["containers"]
    require(re.search(r"@sha256:[a-f0-9]{64}$", container["image"]), "serving_image_not_immutable")
    env = {row["name"]: row for row in container["env"]}
    target = json.loads(env["RUNTIME_TARGET_JSON"]["value"])
    profile = legacy_profile or (COMPATIBLE_DIGEST_PROFILE if account_digest else PRIMARY_PROFILE if order_digest else PROFILE)
    require(env["RUNTIME_TARGET_ENABLED"].get("value") == "true" and target["execution_mode"] == "live"
            and target["dry_run_only"] is False and target["strategy_profile"] == profile
            and target["service_name"] == path.rsplit("/", 1)[-1], "active_incident_target_mismatch")
    if legacy_profile:
        activate_legacy_digest(request, path, target, container, env, apply=apply)
        if apply:
            template.setdefault("labels", {})["qsl-notification-patch"] = "legacy-daily"
    elif account_digest:
        activate_compatible_digest(request, path, target, container, env)
    elif order_digest:
        require(env["IBKR_UNIFIED_NOTIFICATION_ENABLED"].get("value") == "true"
                and env["QSL_ROUTINE_NOTIFICATION_MODE"].get("value") == "daily_digest",
                "unified_daily_route_missing")
        require(all(env.get(key, {}).get("value") for key in DIGEST_BINDINGS), "primary_daily_route_binding_missing")
        key = "QSL_DIGEST_ROUTINE_ORDERS"
        require(env[key] == {"name": key, "value": "false"}, "routine_order_routing_changed")
        env[key]["value"] = "true"
    else:
        limits = container["resources"]["limits"]
        require(limits["memory"] == "512Mi", "incident_memory_limit_changed")
        limits["memory"] = "1Gi"
    template.pop("revision", None)
    body = {"name": path, "etag": service["etag"], "template": template}
    request(path + "?updateMask=template&validateOnly=true", "PATCH", body)
    require(request(path)["etag"] == service["etag"], "baseline_changed")
    if not apply:
        return
    wait_for_operation(request, request(path + "?updateMask=template", "PATCH", body))

    staged = request(path)
    require(staged["traffic"] == service["traffic"] and staged["trafficStatuses"] == service["trafficStatuses"]
            and staged["ingress"] == service["ingress"] and not staged.get("reconciling"), "staging_changed_serving_state")
    new_path = revision_path(path, staged["latestCreatedRevision"])
    require(new_path != old_path, "candidate_revision_missing")
    candidate = request(new_path)
    require(not candidate.get("reconciling") and any(
        condition["type"] == "Ready" and condition["state"] == "CONDITION_SUCCEEDED"
        for condition in candidate.get("conditions", [])
    ), "candidate_not_ready")
    verify_configuration(template, candidate)
    verify_configuration(staged["template"], candidate)
    if prior_stage:
        require(request(prior_stage[0])["uid"] == prior_stage[1], "legacy_previous_stage_missing")
    new_route = {**route, "revision": new_path.rsplit("/", 1)[-1]}
    wait_for_operation(request, request(path + "?updateMask=traffic", "PATCH",
        {"name": path, "etag": staged["etag"], "traffic": [new_route]}))
    final = request(path)
    current, = [row for row in final["trafficStatuses"] if row.get("percent", 0) > 0]
    require(current["percent"] == 100 and revision_path(path, current["revision"]) == new_path
            and final["template"] == staged["template"] and final["ingress"] == service["ingress"],
            "promotion_readback_mismatch")
    if prior_stage:
        require(request(prior_stage[0])["uid"] == prior_stage[1], "legacy_previous_stage_missing")


def main():
    mode = os.environ["IBKR_LIVE_SERVICE_MAINTENANCE"]
    legacy_match = re.fullmatch(r"(?:validate|apply)-legacy-(soxl|russell)", mode)
    legacy_profile = LEGACY_DIGEST_PROFILES[legacy_match[1]] if legacy_match else None
    require(legacy_profile or mode in {"validate", "apply", "validate-order-digest", "apply-order-digest",
                     "validate-account-digest", "apply-account-digest"}, "mode_invalid")
    order_digest = mode.endswith("-order-digest")
    account_digest = mode.endswith("-account-digest")
    project = os.environ["GCP_PROJECT_ID"]
    region = os.environ["CLOUD_RUN_REGION"]
    profile = legacy_profile or (COMPATIBLE_DIGEST_PROFILE if account_digest else PRIMARY_PROFILE if order_digest else PROFILE)
    name = resolve_private_service(os.environ["CLOUD_RUN_SERVICE_TARGETS_JSON"], profile)
    require(all(re.fullmatch(r"[a-z0-9-]+", value) for value in (project, region, name)), "resource_binding_invalid")
    incident_revision = None
    if not (order_digest or account_digest or legacy_profile):
        rows = json.loads(gcloud("logging", "read", INCIDENT + f' AND resource.labels.service_name="{name}"',
                                "--project=" + project, "--limit=30", "--format=json"))
        revisions = {
            row["resource"]["labels"]["revision_name"] for row in rows
            if row["resource"]["labels"].get("location") == region
            and MEMORY_ERROR in str(row.get("textPayload") or row.get("jsonPayload") or "")
        }
        require(len(revisions) == 1, "confirmed_memory_incident_missing")
        incident_revision = revisions.pop()
    token = gcloud("auth", "print-access-token")

    def request(path, method="GET", body=None):
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        data = json.dumps(body).encode() if body is not None else None
        with urlopen(Request(API + path, method=method, data=data, headers=headers), timeout=30) as response:
            result = json.load(response)
        require(not result.get("error"), "cloud_run_api_error")
        return result

    repair(request, f"projects/{project}/locations/{region}/services/{name}", incident_revision,
           order_digest=order_digest, account_digest=account_digest, legacy_profile=legacy_profile,
           apply=mode.startswith("apply"))
    change = ("legacy_digest_route" if legacy_profile else "account_digest_route" if account_digest
              else "routine_orders_to_digest" if order_digest else "memory_limit_1Gi")
    print(json.dumps({"maintenance_verified": True, "mode": mode,
                      "serving_image_preserved": not bool(legacy_profile and mode.startswith("apply")),
                      "verified_change": change}))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        result = {"maintenance_verified": False, "error_type": type(error).__name__}
        if isinstance(error, ValueError) and len(error.args) == 1 and re.fullmatch(r"[a-z_]+", str(error)):
            result["reason"] = str(error)
        if isinstance(error, HTTPError):
            result["http_status"] = error.code
        print(json.dumps(result))
        raise SystemExit(1) from None
