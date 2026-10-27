"""Preflight or invoke one ordinary additional-1 probe; no publishing or retry.

Private configuration, service URL and bearer token stay in the protected runner.
A response is not proof of qualified facts. An uncertain request is never replayed.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
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


def _state(expected, *, read_service=identity._read_cloud_run_metadata,
           read_json=image._json_command, run=commands.command, read_group=gateway.read_group):
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
    state = {"service_spec": service.get("spec"), "service_annotations": service.get("metadata", {}).get("annotations", {}),
             "traffic": status.get("traffic"), "revision": revision, "group": asdict(group), "url": url,
             "expected": expected, "immutable": immutable}
    hashes = {"serving_revision_sha256": sha(serving), "source_sha": source,
              "target_sha256": fingerprint({"configured": expected, "deployed": target}),
              "state_sha256": fingerprint(state), "canonical_url_sha256": sha(url)}
    return hashes, url


def preflight(expected, receipt, *, source_evidence="deploy_receipt", state=_state,
              provenance=identity.diagnose_serving_source_metadata, static_provenance=static.diagnose):
    if source_evidence not in EVIDENCE_MODES:
        raise ProbeError("source_evidence_invalid")
    receipt["source_evidence"] = source_evidence
    before, url = state(expected)
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
    after, after_url = state(expected)
    if before != after or url != after_url:
        raise ProbeError("configuration_changed_during_preflight")
    before["source_evidence_sha256"] = sha(source_evidence)
    receipt.update(checks=dict.fromkeys(CHECKS, True), fingerprints=before,
                   source_evidence=source_evidence,
                   status="preflight_verified", reason="review_before_explicit_invoke")
    return before, url


def prepare_invocation_auth(url, receipt):
    """Expose the verified audience only to this protected job's auth action."""
    output = os.environ.get("GITHUB_OUTPUT", "")
    if not output:
        raise ProbeError("invocation_auth_output_unavailable")
    if not isinstance(url, str) or not url or any(ord(c) <= 0x20 or ord(c) > 0x7e for c in url):
        raise ProbeError("canonical_url_invalid")
    # The workflow command is consumed by the runner; never put this private
    # audience in the JSON receipt or an artifact. Escape command protocol data.
    print("::add-mask::" + url.replace("%", "%25"), flush=True)
    with Path(output).open("a", encoding="utf-8") as handle:
        handle.write("invocation_audience=" + url + "\n")
    receipt.update(status="invocation_auth_prepared", reason="native_id_token_required")


def invoke_once(url, receipt, *, token, opener=None, record=lambda: None):
    # Only the official auth action's masked output is passed by the workflow.
    # Missing/rejected native auth never falls back to gcloud or another identity.
    if (not isinstance(token, str) or not token or len(token) > 16384
            or any(ord(c) <= 0x20 or ord(c) > 0x7e for c in token)):
        raise ProbeError("existing_invocation_identity_unavailable")
    request = Request(url + "/probe", data=b"", method="POST", headers={"Authorization": "Bearer " + token})
    receipt.update(http_attempted=True, http_attempts=1, status="request_outcome_unknown", reason="inspect_original_report_before_any_other_action")
    record()  # Persist the uncertain attempt before sending; never silently replay.
    client = opener if opener is not None else build_opener(ProxyHandler({}), publisher._NoRedirect())
    try:
        with client.open(request, timeout=120) as response:
            receipt.update(http_status=response.status, response_is_probe_ok=response.read(64) == b"Probe OK")
        receipt.update(status="response_received", reason="original_report_requires_separate_validation")
    except HTTPError as error:
        receipt.update(status="http_failure", http_status=error.code,
                       reason="existing_invocation_rejected" if error.code in {401, 403} else
                       "existing_failure_notification_may_have_been_sent")
        error.close()
    except (URLError, OSError, TimeoutError):
        receipt["reason"] = "request_outcome_unknown_do_not_retry"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode_args = parser.add_mutually_exclusive_group()
    mode_args.add_argument("--validate-context", action="store_true")
    mode_args.add_argument("--prepare-invocation-auth", action="store_true")
    args = parser.parse_args(argv)
    receipt = dict(schema_version="ibkr.additional1_probe_invocation.v1", target="additional-1",
        status="blocked", reason="preflight_unverified", checks=dict.fromkeys(CHECKS, False), fingerprints={},
        http_attempted=False, http_attempts=0, http_status=None, response_is_probe_ok=None,
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
        if args.prepare_invocation_auth and mode != "invoke":
            raise ProbeError("invocation_auth_requires_invoke")
        hashes, url = preflight(expected, receipt,
                               source_evidence=os.environ.get("ACCOUNT_FACTS_PROBE_SOURCE_EVIDENCE", "deploy_receipt"))
        if mode == "invoke":
            if hashes != reviewed:
                raise ProbeError("reviewed_fingerprints_changed")
            if args.prepare_invocation_auth:
                prepare_invocation_auth(url, receipt)
            else:
                invoke_once(url, receipt, token=os.environ.get("ACCOUNT_FACTS_PROBE_ID_TOKEN", ""), record=record)
    except ProbeError as error:
        receipt.update(status="blocked", reason=str(error))
    except Exception:
        # Never print native cloud errors, secret payloads, URLs or tokens.
        receipt.update(status="request_outcome_unknown" if receipt["http_attempted"] else "blocked",
                       reason="request_outcome_unknown_do_not_retry" if receipt["http_attempted"] else "protected_preflight_read_failed")
    record()
    return 0 if receipt["status"] in {"preflight_verified", "invocation_auth_prepared"} or (
        receipt["status"] == "response_received" and receipt["http_status"] == 200 and receipt["response_is_probe_ok"] is True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
