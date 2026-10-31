"""Exact producer identities affect local consistency, never execution authority."""
from copy import deepcopy

import pytest

from scripts.historical_source_evidence import validate_source_evidence
from tests.test_historical_source_evidence import FILES, add_daily, fixture, reseal, seal_receipt


MAIN = "InteractiveBrokersPlatform:main.py"
REFACTORED_MAIN_SHA256 = "ef3ab072868dcb3447b19d224c63879cfa11a3d3906db7d25ac51b77171feb04"
DAILY = "InteractiveBrokersPlatform:scripts/project_ibkr_runtime_daily.py"
DAILY_SHA256 = "d5cb50eabc838c9c47064c746f3f15dbdb427e17c95ce1a70f7ff0ee580afb03"


@pytest.fixture(params=(FILES[MAIN], REFACTORED_MAIN_SHA256), ids=("legacy", "refactored"))
def registered_document(request):
    document = fixture()
    document["source"]["producer"]["files"][MAIN] = request.param
    return document


def check(document, local, code=None):
    before = deepcopy(document)
    result = validate_source_evidence(document)
    assert document == before
    assert result["local_consistency"] == local, result
    assert result["status"] == ("FAIL" if local == "FAIL" else "BLOCKED")
    for key in ("execution_authorized", "production_ready", "historical_parity_established",
                "individual_fills_verified", "account_domain_authenticated", "deployment_verified"):
        assert result[key] is False
    if code:
        assert code in {row["code"] for row in result["findings"]}
    assert "SOURCE_AUTHENTICATION_NOT_ESTABLISHED" in result["gaps"]
    assert "PRODUCER_REVISION_AND_DEPLOYMENT_UNVERIFIED" in result["gaps"]
    return result


def test_registered_complete_projection_has_identical_consistency_only_result(registered_document):
    assert check(registered_document, "CONSISTENT") == validate_source_evidence(fixture())


@pytest.mark.parametrize("path", FILES)
@pytest.mark.parametrize("mutation", ("missing", "unknown"))
def test_every_required_file_is_needed_and_unreviewed_mixtures_stay_blocked(registered_document, path, mutation):
    files = registered_document["source"]["producer"]["files"]
    if mutation == "missing":
        del files[path]
        code = "REQUIRED_OBSERVATION_MISSING"
    else:
        # A known main cannot authorize a changed report helper or an unknown main.
        files[path] = "0" * 64
        code = "PRODUCER_VERSION_UNSUPPORTED"
    check(registered_document, "BLOCKED", code)


@pytest.mark.parametrize("value,local,code", (
    (None, "BLOCKED", "REQUIRED_OBSERVATION_MISSING"),
    ([], "FAIL", "SHA256_REQUIRED"),
    ("not-a-digest", "FAIL", "SHA256_REQUIRED"),
))
def test_malformed_required_digest_keeps_its_error_class(registered_document, value, local, code):
    registered_document["source"]["producer"]["files"][MAIN] = value
    check(registered_document, local, code)


@pytest.mark.parametrize("value,local,code", (
    (None, "BLOCKED", "REQUIRED_OBSERVATION_MISSING"),
    ([], "FAIL", "OBJECT_REQUIRED"),
    ("not-a-map", "FAIL", "OBJECT_REQUIRED"),
))
def test_malformed_file_map_keeps_its_error_class(registered_document, value, local, code):
    registered_document["source"]["producer"]["files"] = value
    check(registered_document, local, code)


def test_extra_file_metadata_does_not_change_the_required_projection(registered_document):
    before = deepcopy(registered_document)
    registered_document["source"]["producer"]["files"]["synthetic:extra-metadata.py"] = "1" * 64
    assert check(registered_document, "CONSISTENT") == validate_source_evidence(before)


@pytest.mark.parametrize("field", ("platform_revision", "qpk_revision"))
@pytest.mark.parametrize("mutation", ("missing", "invalid"))
def test_known_files_do_not_replace_required_revision_declarations(registered_document, field, mutation):
    producer = registered_document["source"]["producer"]
    if mutation == "missing":
        del producer[field]
        check(registered_document, "BLOCKED", "REQUIRED_OBSERVATION_MISSING")
    else:
        producer[field] = "invalid-revision"
        check(registered_document, "FAIL", "SOURCE_REVISION_REQUIRED")


def test_revision_validation_precedes_malformed_file_validation(registered_document):
    producer = registered_document["source"]["producer"]
    producer["platform_revision"] = "invalid-revision"
    producer["files"] = []
    result = check(registered_document, "FAIL", "SOURCE_REVISION_REQUIRED")
    assert [row for row in result["findings"] if row["scope"] == "producer"] == [
        {"scope": "producer", "status": "FAIL", "code": "SOURCE_REVISION_REQUIRED"}]


def test_revision_strings_remain_declarations_not_authentication(registered_document):
    registered_document["source"]["producer"].update(platform_revision="9" * 40, qpk_revision="8" * 40)
    check(registered_document, "CONSISTENT")


@pytest.mark.parametrize("contradiction,code", (
    ("profile", "NORMALIZATION_MISMATCH"),
    ("source_field", "SOURCE_FIELD_MAPPING_MISMATCH"),
    ("filled_receipt", "RECEIPT_NORMALIZATION_MISMATCH"),
    ("unknown_cycle", "CYCLE_RECEIPT_OUTCOME_CONFLICT"),
    ("unknown_pending", "UNSUPPORTED_EVIDENCE_PROMOTION"),
))
def test_supported_producer_cannot_hide_source_or_unknown_state_conflicts(registered_document, contradiction, code):
    normalized = registered_document["normalized"]
    if contradiction == "profile":
        normalized["profile"] = "soxl_soxx_trend_income"
    elif contradiction == "source_field":
        normalized["field_sources"]["profile"] = "source.payload#/summary/profile"
    elif contradiction == "filled_receipt":
        normalized["receipt"].update(outcome="filled", broker_confirmation="filled")
        seal_receipt(normalized["receipt"])
    elif contradiction == "unknown_cycle":
        registered_document["source"]["payload"]["summary"]["execution_status"] = "unknown"
    else:
        normalized["pending_orders"] = []
    reseal(registered_document)
    result = check(registered_document, "FAIL", code)
    assert "PRODUCER_VERSION_UNSUPPORTED" not in {row["code"] for row in result["findings"]}


def test_missing_account_observation_stays_blocked_with_known_producer(registered_document):
    del registered_document["source"]["payload"]["summary"]["account_facts"]
    reseal(registered_document)
    result = check(registered_document, "BLOCKED", "REQUIRED_OBSERVATION_MISSING")
    assert "PRODUCER_VERSION_UNSUPPORTED" not in {row["code"] for row in result["findings"]}


@pytest.mark.parametrize("daily_kind", ("registered", "unknown", "report_files"))
def test_report_registration_does_not_expand_daily_producers(registered_document, daily_kind):
    add_daily(registered_document)
    daily_files = registered_document["daily_projection"]["producer"]["files"]
    assert daily_files == {DAILY: DAILY_SHA256}
    if daily_kind == "unknown":
        daily_files[DAILY] = REFACTORED_MAIN_SHA256
        result = check(registered_document, "BLOCKED", "PRODUCER_VERSION_UNSUPPORTED")
    elif daily_kind == "report_files":
        registered_document["daily_projection"]["producer"]["files"] = deepcopy(registered_document["source"]["producer"]["files"])
        result = check(registered_document, "BLOCKED", "REQUIRED_OBSERVATION_MISSING")
    else:
        result = check(registered_document, "CONSISTENT")
    assert all(row["scope"] == "daily_projection" for row in result["findings"])
    assert "DAILY_RUN_LINKAGE_UNVERIFIED" in result["gaps"]
