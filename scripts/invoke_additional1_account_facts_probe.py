"""Preflight or invoke one ordinary additional-1 probe; no publishing or retry.

Private configuration and the Google API access token stay in the protected runner.
A response is not proof of qualified facts. An uncertain request is never replayed.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from scripts import deploy_pilot_no_traffic as commands
from scripts import diagnose_account_facts_identity as identity
from scripts import diagnose_image_static_source as static
from scripts import diagnose_runner_gateway_access as gateway
from scripts import diagnose_serving_image_identity as image
from scripts import publish_account_facts_from_report as publisher

# The probe, synchronous deadline cleanup and image CMD were reviewed at this
# exact deployed source. A different source needs a fresh review, not a fallback.
REVIEWED_SOURCE = "f9ace60a88b622ec8ffa0d65d22d063d38129ebf"
CMD = static.REVIEWED_IMAGE_CMD
FINGERPRINTS = {"serving_revision_sha256", "source_sha", "target_sha256", "state_sha256", "canonical_url_sha256",
                "source_evidence_sha256"}
EVIDENCE_MODES = {"deploy_receipt", "static_source"}
BRIDGE_FIELDS = "name,state,schedule,timeZone,attemptDeadline,retryConfig,httpTarget,lastAttemptTime,scheduleTime,status.code"
BRIDGE_CONFIG_FIELDS = ("name", "state", "schedule", "timeZone", "attemptDeadline", "retryConfig", "httpTarget")
READBACK_SECONDS = 195  # Existing 180-second attempt deadline plus control-log grace.

CHECKS = ("source_evidence", "scope_identity", "canonical_url", "single_serving_revision",
          "service_revision_serialized", "single_worker_thread", "positive_client_id", "configuration_stable")


class ProbeError(ValueError):
    """Fixed error codes only; never native client output."""


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def fingerprint(value):
    return sha(json.dumps(value, sort_keys=True, separators=(",", ":")))


def context(mode, reviewed):
    evidence = os.environ.get("ACCOUNT_FACTS_PROBE_SOURCE_EVIDENCE", "deploy_receipt")
    if (os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or os.environ.get("GITHUB_RUN_ATTEMPT") != "1"
            or os.environ.get("GITHUB_REPOSITORY") != identity._SOURCE_REPOSITORY
            or os.environ.get("IBKR_ACCOUNT_FACTS_TARGET") != "additional-1"
            or mode not in {"preflight", "invoke"} or evidence not in EVIDENCE_MODES):
        raise ProbeError("dispatch_not_admitted")
    expected = identity.resolve_expected()
    if mode == "invoke":
        if (not isinstance(reviewed, dict) or set(reviewed) != FINGERPRINTS
                or any(not isinstance(v, str) or re.fullmatch(
                    r"[0-9a-f]{40}" if k == "source_sha" else r"[0-9a-f]{64}", v) is None
                       for k, v in reviewed.items())):
            raise ProbeError("reviewed_fingerprints_invalid")
        if reviewed["source_evidence_sha256"] != sha(evidence):
            raise ProbeError("reviewed_source_evidence_changed")
    elif reviewed:
        raise ProbeError("preflight_does_not_invoke")
    return expected


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        raise ProbeError("scheduler_time_unverified") from None


def _bridge_config(job):
    return {key: job.get(key) for key in BRIDGE_CONFIG_FIELDS}


def existing_bridge(expected, url, *, read_json=image._json_command):
    region = os.environ.get("CLOUD_RUN_REGION", "")
    jobs = read_json(("gcloud", "scheduler", "jobs", "list", "--project", expected["expected_project_id"],
        "--location", region, "--format=json(" + BRIDGE_FIELDS + ")"), "scheduler_bridge_unavailable")
    if not isinstance(jobs, list):
        raise ProbeError("scheduler_bridge_unavailable")
    matches = [job for job in jobs if isinstance(job, dict)
               and job.get("httpTarget", {}).get("uri") == url + "/probe"]
    if len(matches) != 1:
        raise ProbeError("scheduler_bridge_not_unique")
    job = matches[0]
    target, retries = job.get("httpTarget", {}), job.get("retryConfig", {})
    oidc, name = target.get("oidcToken", {}), job.get("name", "")
    email = oidc.get("serviceAccountEmail", "")
    if (not isinstance(name, str)
            or re.fullmatch(re.escape("projects/" + expected["expected_project_id"] + "/locations/" + region + "/jobs/") + r"[A-Za-z0-9_-]+", name) is None
            or job.get("state") != "ENABLED" or target.get("httpMethod") != "POST"
            or oidc.get("audience") != url or not isinstance(email, str)
            or re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.gserviceaccount\.com", email) is None
            or target.get("oauthToken")
            or target.get("body") not in (None, "")
            or job.get("attemptDeadline") != "180s"):
        raise ProbeError("scheduler_bridge_binding_unverified")
    headers = target.get("headers", {})
    defaults = {"user-agent": {"Google-Cloud-Scheduler"},
                "content-type": {"application/octet-stream", "application/json"}}
    if (not isinstance(headers, dict) or any(not isinstance(k, str) or v not in defaults.get(k.lower(), set())
            for k, v in headers.items())):
        raise ProbeError("scheduler_custom_headers_denied")
    count = retries.get("retryCount", 0)
    if type(count) is not int or count != 0 or retries.get("maxRetryDuration", "0s") != "0s":
        raise ProbeError("scheduler_retry_not_disabled")
    _timestamp(job.get("lastAttemptTime"))
    _timestamp(job.get("scheduleTime"))
    return job


def _bridge_window(job, now):
    last, upcoming = _timestamp(job.get("lastAttemptTime")), _timestamp(job.get("scheduleTime"))
    if now - last <= timedelta(seconds=READBACK_SECONDS):
        raise ProbeError("same_service_probe_attempt_recent_or_in_flight")
    if upcoming <= now + timedelta(seconds=READBACK_SECONDS):
        raise ProbeError("natural_probe_window_conflict")


def _state(expected, *, read_service=identity._read_cloud_run_metadata,
           read_json=image._json_command, run=commands.command, read_group=gateway.read_group,
           read_bridge=existing_bridge):
    region = os.environ.get("CLOUD_RUN_REGION", "")
    if re.fullmatch(r"[a-z]+-[a-z]+[0-9]+", region) is None:
        raise ProbeError("region_unavailable")
    service = read_service(expected)
    if not isinstance(service, dict) or service.get("metadata", {}).get("name") != expected["expected_service_name"]:
        raise ProbeError("service_metadata_unavailable")
    status = service.get("status", {})
    serving, _ = image._serving(service, expected)
    if (status.get("latestReadyRevisionName") != serving or status.get("latestCreatedRevisionName") != serving
            or not commands._ready(service)
            or service.get("metadata", {}).get("generation") is None
            or status.get("observedGeneration") != service["metadata"]["generation"]):
        raise ProbeError("deployment_not_settled")
    url = status.get("url")
    parsed = urlsplit(url) if isinstance(url, str) else None
    if (parsed is None or parsed.scheme != "https" or parsed.port is not None
            or not parsed.hostname or not parsed.hostname.endswith(".run.app")
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path):
        raise ProbeError("canonical_url_invalid")
    revision = read_json(("gcloud", "run", "revisions", "describe", serving,
        "--project", expected["expected_project_id"], "--region", region, "--format=json"), "revision_metadata_unavailable")
    if revision.get("metadata", {}).get("name") != serving or not commands._ready(revision):
        raise ProbeError("revision_metadata_invalid")
    immutable, container = image.immutable_image(revision)
    target = commands._runtime_target(revision)
    if (any(v is not True for v in identity._source_scope(target, expected).values())
            or target.get("platform_id") != "ibkr"):
        raise ProbeError("scope_identity_mismatch")
    source = revision.get("metadata", {}).get("labels", {}).get("commit-sha")
    if source != REVIEWED_SOURCE:
        raise ProbeError("source_not_reviewed")
    template = service.get("spec", {}).get("template", {})
    for node, concurrency_reason, scale_reason in (
        (template, "template_concurrency_unverified", "template_max_scale_unverified"),
        (revision, "revision_concurrency_unverified", "revision_max_scale_unverified"),
    ):
        if (type(node.get("spec", {}).get("containerConcurrency")) is not int
                or node["spec"]["containerConcurrency"] != 1):
            raise ProbeError(concurrency_reason)
        if node.get("metadata", {}).get("annotations", {}).get("autoscaling.knative.dev/maxScale") != "1":
            raise ProbeError(scale_reason)
    # Service and revision caps combine by taking the lower limit. A larger
    # or unset service cap cannot raise the verified revision cap above one.
    try:
        commands._no_manual_scaling(service)
    except commands.ReleaseError:
        raise ProbeError("manual_scaling_unverified") from None
    containers = template.get("spec", {}).get("containers")
    if not isinstance(containers, list) or len(containers) != 1:
        raise ProbeError("service_template_invalid")
    for current in (container, containers[0]):
        command, args = current.get("command", []), current.get("args", [])
        if not ((not command and not args) or (command == CMD[:1] and args == CMD[1:])):
            raise ProbeError("single_worker_thread_unverified")
    group = read_group(SimpleNamespace(project=expected["expected_project_id"]), container.get("env", []),
                      lambda args, stage: run(list(args), stage=stage))
    if (group.execution_backend != "gateway" or type(group.ib_client_id) is not int or group.ib_client_id <= 0
            or publisher._selector(group.account_ids) != expected["expected_account_selector"]):
        raise ProbeError("broker_group_identity_unverified")
    bridge = read_bridge(expected, url)
    state = {"bridge_config": _bridge_config(bridge), "service_spec": service.get("spec"), "service_annotations": service.get("metadata", {}).get("annotations", {}),
             "traffic": status.get("traffic"), "revision": revision, "group": asdict(group), "url": url,
             "expected": expected, "immutable": immutable}
    hashes = {"serving_revision_sha256": sha(serving), "source_sha": source,
              "target_sha256": fingerprint({"configured": expected, "deployed": target}),
              "state_sha256": fingerprint(state), "canonical_url_sha256": sha(url)}
    return hashes, {"url": url, "bridge": bridge}


def preflight(expected, receipt, *, source_evidence="deploy_receipt", state=_state,
              provenance=identity.diagnose_serving_source_metadata, static_provenance=static.diagnose):
    if source_evidence not in EVIDENCE_MODES:
        raise ProbeError("source_evidence_invalid")
    receipt["source_evidence"] = source_evidence
    before, invocation = state(expected)
    if source_evidence == "deploy_receipt":
        verified = provenance(expected)
        if (verified.get("deployment_provenance_status") != "verified"
                or verified.get("serving_stable") is not True
                or verified.get("selected_source_sha") != before["source_sha"]):
            raise ProbeError("source_receipt_unverified")
    else:
        verified = static_provenance(expected)
        files, dependencies = verified.get("selected_files"), verified.get("dependencies")
        scope = verified.get("scope_equalities")
        if (verified.get("reason") != "static_files_match"
                or verified.get("matched_candidate_sha") != REVIEWED_SOURCE
                or verified.get("immutable_image_status") != "verified"
                or verified.get("image_content_status") != "verified"
                or verified.get("image_command_matches_reviewed") is not True
                or verified.get("selected_files_match_candidate") is not True
                or verified.get("serving_stable") is not True
                or not isinstance(files, dict) or set(files) != set(static.SOURCE_FILES)
                or any(value is not True for value in files.values())
                or not isinstance(scope, dict) or set(scope) != set(identity._source_scope({}, expected))
                or any(value is not True for value in scope.values())
                or not isinstance(dependencies, dict) or set(dependencies) != set(static.PACKAGES)
                or any(not isinstance(row, dict) or row.get("status") != "observed"
                       or row.get("equals_candidate_lock") is not True for row in dependencies.values())):
            raise ProbeError("static_source_unverified")
    after, after_invocation = state(expected)
    if before != after or invocation != after_invocation:
        raise ProbeError("configuration_changed_during_preflight")
    before["source_evidence_sha256"] = sha(source_evidence)
    receipt.update(checks=dict.fromkeys(CHECKS, True), fingerprints=before,
                   source_evidence=source_evidence,
                   scheduler_bridge={"job_sha256": sha(invocation["bridge"]["name"]),
                       "oidc_identity_sha256": sha(invocation["bridge"]["httpTarget"]["oidcToken"]["serviceAccountEmail"]),
                       "config_sha256": fingerprint(_bridge_config(invocation["bridge"]))},
                   status="preflight_verified", reason="review_before_explicit_invoke")
    return before, invocation


def _attempt_entries(job, kind, since, until, fields, read_json):
    project, region, job_id = job["name"].split("/")[1::2]
    query = ('resource.type="cloud_scheduler_job" AND resource.labels.job_id="' + job_id
             + '" AND resource.labels.location="' + region + '" AND timestamp>="' + since
             + '" AND timestamp<="' + until + '" AND jsonPayload."@type"="'
             + 'type.googleapis.com/google.cloud.scheduler.logging.' + kind + '"')
    return read_json(("gcloud", "logging", "read", query, "--project", project,
                     "--limit=2", "--format=json(" + fields + ")"), "scheduler_terminal_unavailable")


def _current_bridge(job, read_json):
    project, region, job_id = job["name"].split("/")[1::2]
    return read_json(("gcloud", "scheduler", "jobs", "describe", job_id,
        "--project", project, "--location", region, "--format=json(" + BRIDGE_FIELDS + ")"),
        "scheduler_terminal_unavailable")


def previous_terminal(job, now, *, read_json=image._json_command):
    """Require a terminal pair for this job's exact latest attempt, not an age guess."""
    since, until = job["lastAttemptTime"], now.isoformat()
    finished = _attempt_entries(job, "AttemptFinished", since, until, "timestamp", read_json)
    started = _attempt_entries(job, "AttemptStarted", since, until, "timestamp", read_json)
    current = _current_bridge(job, read_json)
    if (not isinstance(finished, list) or len(finished) != 1
            or not isinstance(started, list) or len(started) != 1
            or current.get("lastAttemptTime") != job.get("lastAttemptTime")
            or _bridge_config(current) != _bridge_config(job)):
        return False
    return all(_timestamp(since) <= _timestamp(row.get("timestamp")) <= now
               for row in (started[0], finished[0]))


def terminal_readback(job, fence, *, read_json=None, pause=sleep, clock=monotonic):
    """Observe this attempt inside a fixed time window; missing fields stay unknown."""
    start = _timestamp(fence)
    end = start + timedelta(seconds=READBACK_SECONDS)
    deadline = clock() + READBACK_SECONDS
    def read(args, reason):
        remaining = deadline - clock()
        if remaining <= 0:
            raise ProbeError("scheduler_observation_deadline")
        if read_json is not None:
            return read_json(args, reason)
        return commands._json(commands.command(list(args), stage=reason, timeout=min(60, remaining)))
    unknown = {"scheduler_terminal": False, "scheduler_request_succeeded": None}
    for index in range(40):
        if clock() >= deadline:
            return unknown
        finished = _attempt_entries(job, "AttemptFinished", fence, end.isoformat(),
                                    "timestamp,httpRequest.status", read)
        if clock() >= deadline:
            return unknown
        if not isinstance(finished, list) or len(finished) > 1:
            return unknown
        if finished:
            started = _attempt_entries(job, "AttemptStarted", fence, end.isoformat(), "timestamp", read)
            if clock() >= deadline:
                return unknown
            if not isinstance(started, list) or len(started) != 1:
                return unknown
            first, last = _timestamp(started[0].get("timestamp")), _timestamp(finished[0].get("timestamp"))
            current = _current_bridge(job, read)
            attempted = _timestamp(current.get("lastAttemptTime"))
            if (clock() >= deadline or not start <= attempted <= end
                    or not attempted <= first <= end or not attempted <= last <= end
                    or attempted == _timestamp(job.get("lastAttemptTime"))
                    or _bridge_config(current) != _bridge_config(job)):
                return unknown
            http = finished[0].get("httpRequest", {}).get("status")
            status = current.get("status")
            # Job status is auxiliary; omitted fields remain unknown.
            # The explicit Finished HTTP result is decisive.
            code = status.get("code") if isinstance(status, dict) else None
            code = code if type(code) is int else None
            success = None
            if type(http) is int and 100 <= http <= 599:
                success = 200 <= http < 300
                if success and code not in (None, 0):
                    success = None  # Conflicting metadata is not replay authority.
            return {"scheduler_terminal": True, "scheduler_request_succeeded": success,
                    "scheduler_started_at": first.isoformat(), "scheduler_finished_at": last.isoformat(),
                    "scheduler_http_status": http if type(http) is int else None,
                    "scheduler_job_status_code": code}
        remaining = deadline - clock()
        if index < 39 and remaining > 0:
            pause(min(5, remaining))
    return unknown


def invoke_once(invocation, receipt, *, token, opener=None, record=lambda: None,
                read_terminal=terminal_readback, verify_previous=previous_terminal, now=None):
    if receipt.get("scheduler_dispatch_attempted"):
        raise ProbeError("previous_dispatch_inspect_only")
    if (not isinstance(token, str) or not token or len(token) > 16384
            or any(ord(c) <= 0x20 or ord(c) > 0x7e for c in token)):
        raise ProbeError("existing_invocation_identity_unavailable")
    job = invocation["bridge"]
    current = now if now is not None else datetime.now(timezone.utc)
    _bridge_window(job, current)
    if verify_previous(job, current) is not True:
        raise ProbeError("previous_scheduler_attempt_terminal_unverified")
    current = now if now is not None else datetime.now(timezone.utc)
    _bridge_window(job, current)
    fence = current.isoformat()
    # Only this fixed Google API endpoint is called. The existing Scheduler emits OIDC.
    request = Request("https://cloudscheduler.googleapis.com/v1/" + job["name"] + ":run",
                      data=b"{}", method="POST", headers={"Authorization": "Bearer " + token,
                      "Content-Type": "application/json"})
    receipt.update(scheduler_dispatch_attempted=True, scheduler_dispatch_attempts=1,
        scheduler_dispatch_started_at=fence, status="request_outcome_unknown",
        reason="inspect_original_attempt_before_any_other_action")
    record()
    client = opener if opener is not None else build_opener(ProxyHandler({}), publisher._NoRedirect())
    try:
        with client.open(request, timeout=30) as response:
            receipt["scheduler_run_http_status"] = response.status
            if not 200 <= response.status < 300:
                return
        receipt.update(status="scheduler_dispatched", reason="await_original_terminal_metadata")
        record()
        receipt.update(read_terminal(job, fence))
        if receipt.get("scheduler_terminal") is True:
            receipt.update(status="scheduler_terminal_observed", reason="original_report_requires_separate_validation")
    except HTTPError as error:
        receipt.update(scheduler_run_http_status=error.code,
            reason="existing_scheduler_identity_rejected" if error.code in {401, 403} else "request_outcome_unknown_do_not_retry")
        error.close()
    except (URLError, OSError, TimeoutError):
        receipt["reason"] = "request_outcome_unknown_do_not_retry"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--validate-context", action="store_true")
    args = parser.parse_args(argv)
    receipt = dict(schema_version="ibkr.additional1_probe_invocation.v1", target="additional-1",
        status="blocked", reason="preflight_unverified", checks=dict.fromkeys(CHECKS, False), fingerprints={},
        scheduler_dispatch_attempted=False, scheduler_dispatch_attempts=0, scheduler_run_http_status=None,
        scheduler_terminal=False, scheduler_request_succeeded=None,
        facts_verified=False, published=False, broker_connection_attempts="unknown",
        existing_failure_notification_possible=True)
    receipt.update(loaded_source_status="unknown", independent_release_approval="unknown")
    path = Path(os.environ["RUNNER_TEMP"]) / "additional1-probe.safe.json"

    def record():
        path.write_text(json.dumps(receipt, sort_keys=True) + "\n")
        path.chmod(0o600)
        print(json.dumps(receipt, sort_keys=True), flush=True)

    try:
        mode = os.environ.get("ACCOUNT_FACTS_PROBE_MODE", "")
        reviewed = commands._json(os.environ.get("ACCOUNT_FACTS_PROBE_REVIEWED_FINGERPRINTS", "{}"))
        expected = context(mode, reviewed)
        if args.validate_context:
            return 0
        if mode == "invoke" and path.exists():
            previous = commands._json(path.read_text())
            if previous.get("scheduler_dispatch_attempted") is True:
                print(json.dumps({"status": "request_outcome_unknown", "reason": "previous_dispatch_inspect_only"}))
                return 1
        hashes, invocation = preflight(expected, receipt,
                               source_evidence=os.environ.get("ACCOUNT_FACTS_PROBE_SOURCE_EVIDENCE", "deploy_receipt"))
        if mode == "invoke":
            if hashes != reviewed:
                raise ProbeError("reviewed_fingerprints_changed")
            # The original WIF credentials authorize only this one Google API request.
            token = commands.command(["gcloud", "auth", "print-access-token"], stage="existing_invocation_identity_unavailable").strip()
            invoke_once(invocation, receipt, token=token, record=record)
    except ProbeError as error:
        receipt.update(status="request_outcome_unknown" if receipt["scheduler_dispatch_attempted"] else "blocked",
            reason="request_outcome_unknown_do_not_retry" if receipt["scheduler_dispatch_attempted"] else str(error))
    except Exception:
        # Never print native cloud errors, secret payloads, URLs or tokens.
        receipt.update(status="request_outcome_unknown" if receipt["scheduler_dispatch_attempted"] else "blocked",
                       reason="request_outcome_unknown_do_not_retry" if receipt["scheduler_dispatch_attempted"] else "protected_preflight_read_failed")
    record()
    return 0 if receipt["status"] == "preflight_verified" or (
        receipt["scheduler_terminal"] is True and receipt["scheduler_request_succeeded"] is True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
