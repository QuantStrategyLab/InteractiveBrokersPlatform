"""The observed reusable job layout retains the original provenance checks."""
from copy import deepcopy

import pytest

from scripts import diagnose_account_facts_identity as diagnostic
from tests.test_diagnose_account_facts_identity import _source_metadata_case

NAME = "Deploy / Sync Cloud Run / Deploy / Sync Cloud Run"


def test_exact_reusable_job_name_keeps_verified_deploy_provenance(monkeypatch):
    case = _source_metadata_case(monkeypatch)
    case["jobs"]["jobs"][0]["name"] = NAME
    result = diagnostic.diagnose_serving_source_metadata(case["expected"])
    assert result["deployment_provenance_status"] == "verified"
    assert result["selected_source_sha"] == "b" * 40
    assert result["loaded_source_status"] == "unknown"


@pytest.mark.parametrize("change", ["prefix", "suffix", "duplicate", "step", "digest"])
def test_reusable_layout_does_not_admit_other_names_or_weaken_receipt(monkeypatch, change):
    case = _source_metadata_case(monkeypatch)
    job = case["jobs"]["jobs"][0]
    job["name"] = NAME
    if change == "prefix":
        job["name"] = "Other / " + NAME
    elif change == "suffix":
        job["name"] += " / Other"
    elif change == "duplicate":
        case["jobs"]["jobs"].append(deepcopy(job))
        case["jobs"]["total_count"] = 2
    elif change == "step":
        job["steps"][0]["conclusion"] = "skipped"
    else:
        case["logs"] = case["logs"].replace("EXPECTED_IMAGE_DIGEST: sha256:" + "f" * 64,
                                              "EXPECTED_IMAGE_DIGEST: sha256:" + "e" * 64)
    result = diagnostic.diagnose_serving_source_metadata(case["expected"])
    assert result["deployment_provenance_status"] == "unknown"
    assert result["selected_source_sha"] is None
    assert result["reason"] == ("deployment_image_mismatch" if change == "digest" else "deployment_job_unverified")
