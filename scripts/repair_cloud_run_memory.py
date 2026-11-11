"""Apply the confirmed memory-only repair through the protected deployment job."""
from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from scripts.summarize_historical_execution_cloud import resolve_private_service

API = "https://run.googleapis.com/v2/"
PROFILE = "soxl_soxx_trend_income"
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


def repair(request, path, incident_revision, apply=False):
    service = request(path)
    route, = service["traffic"]
    active, = [row for row in service["trafficStatuses"] if row.get("percent", 0) > 0]
    require(not service.get("reconciling") and route["type"] == "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION",
            "traffic_not_pinned")
    require(route["percent"] == active["percent"] == 100 and not route.get("tag") and not active.get("tag"),
            "traffic_not_single_untagged_revision")
    old_path = revision_path(path, active["revision"])
    require(old_path == revision_path(path, route["revision"]) == revision_path(path, incident_revision),
            "incident_revision_no_longer_serving")
    old = request(old_path)
    template = copy.deepcopy(service["template"])
    verify_configuration(template, old)
    container, = template["containers"]
    require(re.search(r"@sha256:[a-f0-9]{64}$", container["image"]), "serving_image_not_immutable")
    env = {row["name"]: row.get("value") for row in container["env"]}
    target = json.loads(env["RUNTIME_TARGET_JSON"])
    require(env["RUNTIME_TARGET_ENABLED"] == "true" and target["execution_mode"] == "live"
            and target["dry_run_only"] is False and target["strategy_profile"] == PROFILE
            and target["service_name"] == path.rsplit("/", 1)[-1], "active_incident_target_mismatch")
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
    new_route = {**route, "revision": new_path.rsplit("/", 1)[-1]}
    wait_for_operation(request, request(path + "?updateMask=traffic", "PATCH",
        {"name": path, "etag": staged["etag"], "traffic": [new_route]}))
    final = request(path)
    current, = [row for row in final["trafficStatuses"] if row.get("percent", 0) > 0]
    require(current["percent"] == 100 and revision_path(path, current["revision"]) == new_path
            and final["template"] == staged["template"] and final["ingress"] == service["ingress"],
            "promotion_readback_mismatch")


def main():
    mode = os.environ["IBKR_OOM_REPAIR"]
    require(mode in {"validate", "apply"}, "mode_invalid")
    project = os.environ["GCP_PROJECT_ID"]
    region = os.environ["CLOUD_RUN_REGION"]
    name = resolve_private_service(os.environ["CLOUD_RUN_SERVICE_TARGETS_JSON"], PROFILE)
    require(all(re.fullmatch(r"[a-z0-9-]+", value) for value in (project, region, name)), "resource_binding_invalid")
    rows = json.loads(gcloud("logging", "read", INCIDENT + f' AND resource.labels.service_name="{name}"',
                            "--project=" + project, "--limit=30", "--format=json"))
    revisions = {
        row["resource"]["labels"]["revision_name"] for row in rows
        if row["resource"]["labels"].get("location") == region
        and MEMORY_ERROR in str(row.get("textPayload") or row.get("jsonPayload") or "")
    }
    require(len(revisions) == 1, "confirmed_memory_incident_missing")
    token = gcloud("auth", "print-access-token")

    def request(path, method="GET", body=None):
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        data = json.dumps(body).encode() if body is not None else None
        with urlopen(Request(API + path, method=method, data=data, headers=headers), timeout=30) as response:
            result = json.load(response)
        require(not result.get("error"), "cloud_run_api_error")
        return result

    repair(request, f"projects/{project}/locations/{region}/services/{name}", revisions.pop(), apply=mode == "apply")
    print(json.dumps({"memory_repair_verified": True, "mode": mode, "memory_limit": "1Gi",
                      "serving_image_preserved": True, "runtime_configuration_preserved": True}))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        result = {"memory_repair_verified": False, "error_type": type(error).__name__}
        if isinstance(error, ValueError) and len(error.args) == 1 and re.fullmatch(r"[a-z_]+", str(error)):
            result["reason"] = str(error)
        if isinstance(error, HTTPError):
            result["http_status"] = error.code
        print(json.dumps(result))
        raise SystemExit(1) from None
