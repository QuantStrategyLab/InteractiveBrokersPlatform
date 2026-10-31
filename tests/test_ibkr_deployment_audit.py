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


def test_direct_service_layout_is_observed_without_changing_existing_wrapper_parser():
    row = event()
    row["protoPayload"]["request"] = row["protoPayload"]["request"]["service"]
    result, _ = run_case([row])
    assert result["exact_revision_events_count"] == 0 and result["events"] == []
    layout = result["event_layouts"][0]
    assert layout["service_wrapper_present"] is False
    assert layout["direct_spec_template_present"] is True
    assert layout["direct_revision_equals_serving"] is True
    assert layout["direct_pinned_image_equals_serving_digest"] is True
    assert layout["response_explicit_revision_present"] is False
    assert PRIVATE not in json.dumps(result) and IMAGE not in json.dumps(result)


def test_wrapper_and_unlinked_direct_layout_remain_distinct_without_raw_fields():
    linked, _ = run_case([event()])
    assert linked["event_layouts"][0]["service_wrapper_present"] is True
    assert linked["event_layouts"][0]["direct_spec_template_present"] is False
    row = event()
    row["protoPayload"]["request"] = {"spec": {"template": {"metadata": {"name": PRIVATE}}}}
    unlinked, _ = run_case([row])
    assert unlinked["event_layouts"][0]["direct_explicit_revision_present"] is True
    assert unlinked["event_layouts"][0]["direct_revision_equals_serving"] is False
    assert PRIVATE not in json.dumps(unlinked)


def operation_pair():
    first = event()
    first["operation"] = {"id": PRIVATE, "producer": PRIVATE + "-producer", "first": True}
    # The request intentionally does not name the auto-generated serving revision.
    first["protoPayload"]["request"]["service"]["spec"]["template"]["metadata"].pop("name")
    last = {"operation": {"id": PRIVATE, "producer": PRIVATE + "-producer", "last": True},
        "protoPayload": {"resourceName": first["protoPayload"]["resourceName"],
            "methodName": first["protoPayload"]["methodName"],
            "response": {"status": {"latestCreatedRevisionName": REVISION}}}}
    return first, last


def test_native_operation_links_unique_first_request_to_last_serving_response_without_exporting_key():
    result, calls = run_case(list(operation_pair()))
    native = result["native_operations"]
    assert len(calls) == 1 and result["exact_revision_events_count"] == 0
    assert result["audit_status"] == "no_exact_revision_event"
    assert native["status"] == "linked" and native["linked_group_count"] == 1
    assert native["links"][0] == {"method": "google.cloud.run.v1.Services.ReplaceService",
        "native_operation_key_equal": True, "unique_first_request_last_serving_response": True,
        "first_status_code": None, "last_status_code": None, "declared_source_sha": "b" * 40,
        "declared_run_id": "123", "request_image_equals_serving_digest": True}
    assert result["independent_release_approval"] == "unknown"
    assert PRIVATE not in json.dumps(result) and IMAGE not in json.dumps(result)


@pytest.mark.parametrize("change", ["id", "producer", "empty_key", "missing_first", "missing_last", "duplicate_first",
    "duplicate_last", "method", "service", "response_revision", "request_conflicting_revision", "string_flag", "one_entry_both_flags"])
def test_missing_mixed_or_ambiguous_native_operation_cannot_create_a_link(change):
    first, last = operation_pair(); rows = [first, last]
    if change in {"id", "producer"}: last["operation"][change] += "-different"
    if change == "empty_key": last["operation"]["producer"] = ""
    if change == "missing_first": first["operation"].pop("first")
    if change == "missing_last": last["operation"].pop("last")
    if change == "duplicate_first": rows.append(first)
    if change == "duplicate_last": rows.append(last)
    if change == "method": last["protoPayload"]["methodName"] = "google.cloud.run.v1.Services.CreateService"
    if change == "service": last["protoPayload"]["resourceName"] = "projects/synthetic/locations/us-central1/services/synthetic-service"
    if change == "response_revision": last["protoPayload"]["response"]["status"]["latestCreatedRevisionName"] = PRIVATE
    if change == "request_conflicting_revision": first["protoPayload"]["request"]["service"]["spec"]["template"]["metadata"]["name"] = PRIVATE
    if change == "string_flag": first["operation"]["first"] = "true"
    if change == "one_entry_both_flags":
        first["operation"]["last"] = True
        first["protoPayload"]["response"] = last["protoPayload"]["response"]
        rows = [first]
    result, _ = run_case(rows)
    assert result["native_operations"]["status"] == "unknown"
    assert result["native_operations"]["links"] == [] and PRIVATE not in json.dumps(result)


def test_failed_native_last_status_is_retained_without_qualifying_release():
    first, last = operation_pair()
    last["protoPayload"]["status"] = {"code": 7}
    result, _ = run_case([first, last])
    assert result["native_operations"]["links"][0]["last_status_code"] == 7
    assert result["independent_release_approval"] == "unknown"


@pytest.mark.parametrize("middle_flags", [{}, {"first": False, "last": False}])
def test_middle_entries_with_missing_or_false_endpoint_flags_do_not_change_unique_native_endpoints(middle_flags):
    first, last = operation_pair()
    middle = {"operation": {"id": PRIVATE, "producer": PRIVATE + "-producer", **middle_flags},
        "protoPayload": {"resourceName": first["protoPayload"]["resourceName"],
            "methodName": first["protoPayload"]["methodName"]}}
    result, calls = run_case([first, middle, last])
    assert len(calls) == 1 and result["native_operations"]["linked_group_count"] == 1
    assert result["native_operations"]["ambiguous_or_unmatched_group_count"] == 0
    assert result["audit_status"] == "no_exact_revision_event" and result["events"] == []
    assert result["independent_release_approval"] == "unknown" and PRIVATE not in json.dumps(result)
