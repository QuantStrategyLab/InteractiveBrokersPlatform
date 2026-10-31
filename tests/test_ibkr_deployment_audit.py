import json
import subprocess

import pytest

from scripts import ibkr_deployment_audit as audit


PRIVATE = "private-resource-principal-canary"
IMAGE = "us-central1-docker.pkg.dev/synthetic/app@sha256:" + "a" * 64
REVISION = "synthetic-service-00001-audit"
EXPECTED = {"expected_project_id": "synthetic", "expected_service_name": "synthetic-service"}
METADATA = {"metadata": {"name": REVISION, "creationTimestamp": "2026-10-01T00:00:00Z"}}


def event():
    return {"protoPayload": {"resourceName": "namespaces/synthetic/services/synthetic-service",
        "methodName": "google.cloud.run.v1.Services.ReplaceService",
        "authenticationInfo": {"principalEmail": PRIVATE},
        "request": {"service": {"spec": {"template": {"metadata": {"name": REVISION,
            "labels": {"commit-sha": "b" * 40, "github-run-id": "123", PRIVATE: PRIVATE}},
            "spec": {"containers": [{"image": IMAGE, "env": [{"name": PRIVATE, "value": PRIVATE}]}]}}}}}}}


def run_case(events=None, *, code=0, error="", revision=METADATA):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        assert command[:3] == ("gcloud", "logging", "read")
        assert "--limit=21" in command and "--order=asc" in command
        assert "timestamp>=" in command[3] and "timestamp<=" in command[3]
        assert kwargs["check"] is False and kwargs["timeout"] == 30
        assert kwargs["env"]["CLOUDSDK_CORE_DISABLE_FILE_LOGGING"] == "true"
        return subprocess.CompletedProcess(command, code, json.dumps([] if events is None else events), error)
    return audit.observe(EXPECTED, revision, IMAGE, "us-central1", run=run), calls


def test_exact_revision_audit_projects_code_and_digest_equality_without_resources_or_approval():
    result, calls = run_case([event()])
    assert len(calls) == 1
    assert result["exact_revision_events_count"] == 1 and result["service_events_count"] == 1
    assert result["events"][0] == {"method": "google.cloud.run.v1.Services.ReplaceService",
        "status_code": None,
        "declared_source_sha": "b" * 40, "declared_run_id": "123",
        "request_image_equals_serving_digest": True, "principal_present": True}
    assert result["independent_release_approval"] == "unknown"
    encoded = json.dumps(result)
    assert all(value not in encoded for value in (PRIVATE, IMAGE, REVISION))


@pytest.mark.parametrize("code,expected", [(0, 0), (7, 7), (None, None), ("0", None), (True, None)])
def test_audit_error_status_is_retained_as_attempt_evidence_without_approval(code, expected):
    row = event()
    row["protoPayload"]["status"] = {"code": code}
    result, _ = run_case([row])
    assert result["events"][0]["status_code"] == expected
    assert result["exact_revision_events_count"] == 1
    assert result["independent_release_approval"] == "unknown"


@pytest.mark.parametrize("change", ["other_revision", "missing_revision", "mutable_image", "bad_labels"])
def test_incomplete_or_different_request_cannot_prove_serving_source_or_digest(change):
    row = event(); template = row["protoPayload"]["request"]["service"]["spec"]["template"]
    if change == "other_revision": template["metadata"]["name"] = PRIVATE
    if change == "missing_revision": template["metadata"].pop("name")
    if change == "mutable_image": template["spec"]["containers"][0]["image"] = PRIVATE
    if change == "bad_labels": template["metadata"]["labels"] = {"commit-sha": PRIVATE, "github-run-id": PRIVATE}
    result, _ = run_case([row])
    if change in {"other_revision", "missing_revision"}:
        assert result["exact_revision_events_count"] == 0
    elif change == "mutable_image":
        assert result["events"][0]["request_image_equals_serving_digest"] is None
    else:
        assert result["events"][0]["declared_source_sha"] is None
        assert result["events"][0]["declared_run_id"] is None
    assert PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("rows,code,error,status", [
    ([], 0, "", "no_exact_revision_event"), ([{}] * 21, 0, "", "truncated_or_invalid"),
    ([{"protoPayload": {"resourceName": PRIVATE}}], 0, "", "invalid_scope"),
    ([], 1, "PERMISSION_DENIED " + PRIVATE, "permission_denied"),
    ([], 1, PRIVATE, "read_failed"),
])
def test_empty_truncated_or_failed_audit_is_not_no_deployment_or_approval(rows, code, error, status):
    result, _ = run_case(rows, code=code, error=error)
    assert result["audit_status"] == status
    assert result["independent_release_approval"] == "unknown" and PRIVATE not in json.dumps(result)


def test_missing_original_creation_time_does_not_fall_back_to_current_clock():
    result, calls = run_case(revision={"metadata": {"name": REVISION}})
    assert calls == [] and result["audit_status"] == "unavailable"
