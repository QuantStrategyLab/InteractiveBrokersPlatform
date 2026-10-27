"""Public locators stay observable without changing provenance acceptance."""
import json

import pytest

from scripts import diagnose_account_facts_identity as diagnostic
from tests.test_diagnose_account_facts_identity import PRIVATE, _source_metadata_case


@pytest.mark.parametrize("failure", ["job", "digest", "serving"])
def test_valid_labels_survive_later_provenance_failure_without_becoming_verified(monkeypatch, failure):
    case = _source_metadata_case(monkeypatch)
    if failure == "job":
        case["jobs"]["jobs"][0]["name"] = "Different deployment job"
        reason = "deployment_job_unverified"
    elif failure == "digest":
        case["logs"] = case["logs"].replace("EXPECTED_IMAGE_DIGEST: sha256:" + "f" * 64,
                                              "EXPECTED_IMAGE_DIGEST: sha256:" + "e" * 64)
        reason = "deployment_image_mismatch"
    else:
        case["after_service"] = {"metadata": case["service"]["metadata"], "status": {
            "traffic": [{"revisionName": PRIVATE + "-other", "percent": 100}]}}
        # Preserve the existing serving validator's exact rejection category.
        reason = "revision_identity_invalid"
    result = diagnostic.diagnose_serving_source_metadata(case["expected"])
    assert result["reason"] == reason
    assert result["reported_source_sha"] == "b" * 40
    assert result["reported_deployment_run_id"] == "2468"
    assert result["selected_source_sha"] is None
    assert result["deployment_provenance_status"] == "unknown"
    assert result["loaded_source_status"] == result["independent_release_approval"] == "unknown"
    assert PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("label,value,source,run_id", [
    ("commit-sha", PRIVATE, None, "2468"),
    ("github-run-id", PRIVATE, "b" * 40, None),
    ("github-run-id", "0", "b" * 40, None),
    ("github-run-id", 2468, "b" * 40, None),
])
def test_malformed_labels_are_not_echoed_or_used_to_read_github(monkeypatch, label, value, source, run_id):
    case = _source_metadata_case(monkeypatch)
    case["revision"]["metadata"]["labels"][label] = value
    result = diagnostic.diagnose_serving_source_metadata(case["expected"])
    assert result["reason"] == "source_labels_unavailable"
    assert result["reported_source_sha"] == source
    assert result["reported_deployment_run_id"] == run_id
    assert result["selected_source_sha"] is None
    assert not any(command[0] == "gh" for command in case["calls"])
    assert PRIVATE not in json.dumps(result)


def test_success_keeps_reported_labels_separate_from_verified_source(monkeypatch):
    case = _source_metadata_case(monkeypatch)
    result = diagnostic.diagnose_serving_source_metadata(case["expected"])
    assert result["reason"] == "deploy_provenance_verified"
    assert result["reported_source_sha"] == result["selected_source_sha"] == "b" * 40
    assert result["reported_deployment_run_id"] == "2468"
    assert result["deployment_provenance_status"] == "verified"
    assert result["loaded_source_status"] == result["independent_release_approval"] == "unknown"
