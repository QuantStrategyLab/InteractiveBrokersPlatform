"""Synthetic source observations; no broker, account, cloud or runtime reads."""
from copy import deepcopy
import hashlib
import json
import socket
import subprocess

import pytest

from scripts.historical_source_evidence import validate_source_evidence


FILES = {
    "QuantPlatformKit:src/quant_platform_kit/common/runtime_reports.py": "7f5b33f84810d414cc665ce33d2741497b8f54fcb300488c51f54dbd08bdcf42",
    "QuantPlatformKit:src/quant_platform_kit/common/execution_receipts.py": "5b7f5b44c8aa4bda640e72cc5a58f04b617404afcf9f55ceb314a1721be6d93b",
    "QuantPlatformKit:src/quant_platform_kit/common/strategy_release.py": "773547422aa537eb03bf223a4d9038db457c514211b0387e4601afdcef9727b9",
    "QuantPlatformKit:src/quant_platform_kit/common/runtime_target.py": "17fa7505e1c8e6e375699d44120526c8c9af8c586a274fd7d7dd03009131c5b3",
    "InteractiveBrokersPlatform:application/runtime_reporting_adapters.py": "d2394847474ef78c98cc51c3970fd539481bf13d60ac5cc941fd03d33850dace",
    "InteractiveBrokersPlatform:application/execution_receipt_adapter.py": "a2bd939380947374854cf373ef27c7266c7b97150f0742e6b90191d7e5a278c0",
    "InteractiveBrokersPlatform:application/account_facts.py": "e4513e5ecc8f494c3a305007ebae21804b07ac1407d761c1af3c2ce9d53af4d1",
    "InteractiveBrokersPlatform:application/decision_cycle.py": "ad938a55aa439764112e96ac7e80a4c661ee14823292e7cf99f1df2bcc863311",
    "InteractiveBrokersPlatform:main.py": "f13b55f501c404ef249bb3344bd69136beb8e0ead382e827d082217180b46637",
}
FIELD_SOURCES = {
    "profile": "source.payload#/strategy_profile",
    "run_id": "source.payload#/run_id",
    "logical_account_scope": "source.payload#/account_scope",
    "runtime_revision": "source.payload#/diagnostics/runtime_revision",
    "release": "source.payload#/runtime_release_receipt/strategy_release",
    "started_at": "source.payload#/started_at",
    "finished_at": "source.payload#/finished_at",
    "decision_at": "source.payload#/decision_at",
    "account_domain": "account_binding.payload#/account_domain",
    "receipt": "source.payload#/execution_receipt",
    "execution_lane": ["source.payload#/dry_run", "source.payload#/runtime_target/execution_environment",
                       "source.payload#/runtime_target/execution_mode", "source.payload#/diagnostics/ib_gateway_mode"],
}
ACCOUNT = "acct_" + "1" * 32
RUN = "run_" + "2" * 32
START = "2026-10-30T14:00:00.500000+00:00"
DECISION = "2026-10-30T14:00:00Z"
FINISH = "2026-10-30T14:00:10Z"
CAPTURED = "2026-10-31T12:00:00Z"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def seal_receipt(receipt):
    receipt["receipt_id"] = "execution-receipt." + digest(
        {k: v for k, v in receipt.items() if k != "receipt_id"})[:32]


def envelope(source_id, payload, number):
    return {"source_id": source_id, "digest_domain": "sanitized_json",
            "locator": {"object_alias": "obj_" + str(number) * 32,
                        "version": "version-1", "raw_bytes_sha256": str(number + 1) * 64},
            "captured_at": CAPTURED, "payload": payload, "payload_sha256": digest(payload)}


def reseal(document):
    source, binding, normalized = document["source"], document["account_binding"], document["normalized"]
    source["payload_sha256"] = digest(source["payload"])
    binding["payload"]["report_payload_sha256"] = source["payload_sha256"]
    binding["payload_sha256"] = digest(binding["payload"])
    normalized["source_payload_sha256"] = source["payload_sha256"]
    normalized["account_binding_payload_sha256"] = binding["payload_sha256"]
    if "daily_projection" in document:
        daily = document["daily_projection"]
        daily["payload_sha256"] = digest(daily["payload"])
        normalized["daily_projection"]["source_payload_sha256"] = daily["payload_sha256"]
    document["normalized_sha256"] = digest(normalized)


def fixture():
    release = {"release_id": "fixture-release", "manifest_sha256": "a" * 64,
               "strategy_revision": "b" * 40, "config_sha256": "c" * 64,
               "risk_policy_sha256": "d" * 64, "evidence_sha256": "e" * 64,
               "plugin_bundle_sha256": "f" * 64, "effective_session": "2026-10-01"}
    receipt = {"schema_version": "qsl_execution_receipt.v1", "platform": "ibkr",
               "strategy_profile": "tqqq_growth_income", "strategy_revision": "b" * 40,
               "execution_mode": "live", "outcome": "broker_acknowledged",
               "broker_confirmation": "acknowledged", "observed_at": FINISH}
    seal_receipt(receipt)
    report = {"schema_version": "runtime_report.v1", "platform": "interactive_brokers",
              "strategy_profile": "tqqq_growth_income", "run_id": RUN,
              "account_scope": "fixture-scope", "service_name": "fixture-service",
              "run_source": "cloud_run", "status": "ok", "dry_run": False,
              "started_at": START, "finished_at": FINISH, "decision_at": DECISION,
              "runtime_target": {"strategy_profile": "tqqq_growth_income", "execution_mode": "live", "execution_environment": "paper",
                                 "account_scope": "fixture-scope", "account_selector": [ACCOUNT],
                                 "dry_run_only": False, "strategy_release": deepcopy(release)},
              "runtime_release_receipt": {"schema_version": "runtime_loaded_receipt.v1",
                                          "attestation_state": "self_attested", "loaded_at": START,
                                          "runtime_revision": "fixture-revision", "release_id": "fixture-release",
                                          "strategy_release": deepcopy(release)},
              "diagnostics": {"runtime_revision": "fixture-revision", "ib_gateway_mode": "paper"},
              "summary": {"account_ids": [ACCOUNT], "execution_status": "submitted",
                          "account_facts": {"schema_version": "ibkr_account_snapshot.v1", "account_ids": [ACCOUNT], "observed_at": START}},
              "execution_receipt": receipt, "errors": []}
    domain = {"platform": "ibkr", "execution_environment": "paper", "server": "server-fixture",
              "lifecycle_id": "lifecycle-fixture", "account_alias": ACCOUNT}
    source = envelope("report-fixture", report, 3)
    source["producer"] = {"platform_revision": "a" * 40, "qpk_revision": "b" * 40,
                          "files": deepcopy(FILES)}
    source["sanitization"] = {"schema_version": "qsl.ibkr_source_aliases.v1",
                              "namespace": "ibkr:paper:server-fixture:lifecycle-fixture", "substitutions": [
        {"pointer": "/run_id", "alias": RUN, "original_value_sha256": "1" * 64},
        *[{"pointer": path, "alias": ACCOUNT, "original_value_sha256": "2" * 64} for path in (
            "/runtime_target/account_selector/0", "/summary/account_ids/0", "/summary/account_facts/account_ids/0")]]}
    binding = envelope("binding-fixture", {"schema_version": "qsl.sanitized_account_binding.v1",
        "report_source_id": source["source_id"], "report_payload_sha256": source["payload_sha256"],
        "account_domain": domain, "effective_from": "2026-10-01T00:00:00Z",
        "effective_until": "2026-11-01T00:00:00Z"}, 5)
    normalized = {"source_id": source["source_id"], "source_payload_sha256": source["payload_sha256"],
                  "account_binding_payload_sha256": binding["payload_sha256"], "platform": "ibkr",
                  "profile": report["strategy_profile"], "run_id": RUN, "account_domain": deepcopy(domain),
                  "logical_account_scope": "fixture-scope", "runtime_revision": "fixture-revision",
                  "release": deepcopy(release), "started_at": START, "finished_at": FINISH,
                  "decision_at": DECISION, "execution_lane": "paper", "record_kind": "runtime_report",
                  "receipt": deepcopy(receipt), "individual_fills": "UNKNOWN", "pending_orders": "UNKNOWN",
                  "field_sources": deepcopy(FIELD_SOURCES)}
    document = {"schema_version": "qsl.ibkr_source_evidence.v1", "evidence_kind": "synthetic",
                "source": source, "account_binding": binding, "normalized": normalized}
    reseal(document)
    return document


def assert_result(document, local, code=None):
    result = validate_source_evidence(document)
    assert result["local_consistency"] == local, result
    assert result["status"] == ("FAIL" if local == "FAIL" else "BLOCKED")
    for key in ("execution_authorized", "production_ready", "historical_parity_established"):
        assert result[key] is False
    if code:
        assert code in {row["code"] for row in result["findings"]}, result
    assert "SOURCE_AUTHENTICATION_NOT_ESTABLISHED" in result["gaps"]
    assert "CROSS_RUN_STRATEGY_STATE_CONTINUITY_UNVERIFIED" in result["gaps"]
    return result


@pytest.mark.parametrize("kind", ["synthetic", "historical"])
def test_matching_observed_fields_remain_blocked_and_do_not_authenticate_history(kind):
    document = fixture()
    document["evidence_kind"] = kind
    before = deepcopy(document)
    result = assert_result(document, "CONSISTENT")
    assert document == before
    assert result["individual_fills_verified"] is False
    assert result["account_domain_authenticated"] is False
    assert result["deployment_verified"] is False


def test_soxl_report_does_not_invent_a_state_contract():
    document = fixture()
    report = document["source"]["payload"]
    report["strategy_profile"] = report["runtime_target"]["strategy_profile"] = "soxl_soxx_trend_income"
    report["execution_receipt"]["strategy_profile"] = "soxl_soxx_trend_income"
    seal_receipt(report["execution_receipt"])
    document["normalized"]["profile"] = "soxl_soxx_trend_income"
    document["normalized"]["receipt"] = deepcopy(report["execution_receipt"])
    reseal(document)
    assert_result(document, "CONSISTENT")


@pytest.mark.parametrize("owner,key", [("source", "locator"), ("source", "producer"),
    ("source", "sanitization"), ("source", "payload_sha256"), ("account_binding", "payload"),
    ("normalized", "field_sources"), ("normalized", "account_domain")])
def test_missing_observations_are_blocked(owner, key):
    document = fixture()
    del document[owner][key]
    if owner == "normalized":
        document["normalized_sha256"] = digest(document["normalized"])
    assert_result(document, "BLOCKED")


@pytest.mark.parametrize("key", ["version", "raw_bytes_sha256"])
def test_locator_needs_an_immutable_version_and_original_digest(key):
    document = fixture()
    del document["source"]["locator"][key]
    assert_result(document, "BLOCKED")


def test_unsupported_producer_is_blocked_but_does_not_mask_observed_identity_conflict():
    document = fixture()
    document["source"]["producer"]["files"][next(iter(FILES))] = "0" * 64
    assert_result(document, "BLOCKED", "PRODUCER_VERSION_UNSUPPORTED")
    document["normalized"]["profile"] = "soxl_soxx_trend_income"
    reseal(document)
    assert_result(document, "FAIL", "NORMALIZATION_MISMATCH")


@pytest.mark.parametrize("owner,key,value,code", [
    ("source", "payload_sha256", "0" * 64, "PAYLOAD_DIGEST_MISMATCH"),
    ("source", "digest_domain", "original_bytes", "DIGEST_DOMAIN_MISMATCH"),
    ("normalized", "source_payload_sha256", "0" * 64, "SOURCE_REFERENCE_MISMATCH"),
    ("normalized", "run_id", "run_" + "9" * 32, "NORMALIZATION_MISMATCH"),
    ("normalized", "individual_fills", 0, "UNSUPPORTED_EVIDENCE_PROMOTION"),
    ("normalized", "pending_orders", [], "UNSUPPORTED_EVIDENCE_PROMOTION"),
])
def test_digest_identity_and_unobserved_empty_promotions_fail(owner, key, value, code):
    document = fixture()
    document[owner][key] = value
    if owner == "normalized":
        document["normalized_sha256"] = digest(document["normalized"])
    assert_result(document, "FAIL", code)


@pytest.mark.parametrize("key,value", [("platform", "schwab"), ("execution_environment", "live"),
    ("server", "another-server"), ("lifecycle_id", "another-life"), ("account_alias", "acct_" + "3" * 32)])
def test_normalized_account_domain_cannot_change_any_identity_dimension(key, value):
    document = fixture()
    document["normalized"]["account_domain"][key] = value
    reseal(document)
    assert_result(document, "FAIL", "ACCOUNT_DOMAIN_MISMATCH")


def test_two_report_account_observations_must_match():
    document = fixture()
    document["source"]["payload"]["summary"]["account_facts"]["account_ids"] = ["acct_" + "8" * 32]
    reseal(document)
    assert_result(document, "FAIL", "ACCOUNT_IDENTITY_MISMATCH")


@pytest.mark.parametrize("accounts", [[], [ACCOUNT, "acct_" + "8" * 32]])
def test_absent_or_multiple_physical_accounts_cannot_be_inferred_from_scope(accounts):
    document = fixture()
    document["source"]["payload"]["runtime_target"]["account_selector"] = accounts
    reseal(document)
    assert_result(document, "BLOCKED", "SINGLE_ACCOUNT_OBSERVATION_REQUIRED")


def test_one_alias_cannot_hide_two_original_account_values():
    document = fixture()
    document["source"]["sanitization"]["substitutions"][-1]["original_value_sha256"] = "f" * 64
    assert_result(document, "FAIL", "ALIAS_MAPPING_CONFLICT")


def test_wrong_source_pointer_is_not_accepted_as_normalized_evidence():
    document = fixture()
    document["normalized"]["field_sources"]["profile"] = "source.payload#/summary/profile"
    reseal(document)
    assert_result(document, "FAIL", "SOURCE_FIELD_MAPPING_MISMATCH")


@pytest.mark.parametrize("where", ["normalized", "target", "loaded"])
def test_release_fields_are_compared_in_full(where):
    document = fixture()
    if where == "normalized":
        release = document["normalized"]["release"]
    else:
        parent = "runtime_target" if where == "target" else "runtime_release_receipt"
        release = document["source"]["payload"][parent]["strategy_release"]
    release["config_sha256"] = "0" * 64
    reseal(document)
    assert_result(document, "FAIL", "RELEASE_IDENTITY_MISMATCH")


def test_legacy_loading_receipt_is_missing_release_evidence():
    document = fixture()
    document["source"]["payload"]["runtime_release_receipt"] = {
        "schema_version": "runtime_loaded_receipt.v1", "attestation_state": "legacy_unattested",
        "loaded_at": START, "missing": ["strategy_release"]}
    reseal(document)
    assert_result(document, "BLOCKED", "RELEASE_ATTESTATION_UNAVAILABLE")


def test_receipt_acknowledgement_cannot_be_normalized_into_a_fill():
    document = fixture()
    receipt = document["normalized"]["receipt"]
    receipt.update(outcome="filled", broker_confirmation="filled")
    seal_receipt(receipt)
    reseal(document)
    assert_result(document, "FAIL", "RECEIPT_NORMALIZATION_MISMATCH")


@pytest.mark.parametrize("outcome,confirmation", [("filled", "filled"),
    ("reconciliation_required", "reconciliation_required"), ("failed", "not_observed")])
def test_observed_receipt_outcomes_do_not_create_individual_fills_or_zero_pending(outcome, confirmation):
    document = fixture()
    receipt = document["source"]["payload"]["execution_receipt"]
    receipt.update(outcome=outcome, broker_confirmation=confirmation)
    seal_receipt(receipt)
    document["normalized"]["receipt"] = deepcopy(receipt)
    document["source"]["payload"]["summary"]["execution_status"] = {
        "filled": "filled", "reconciliation_required": "pending_reconciliation", "failed": "failed"}[outcome]
    reseal(document)
    assert_result(document, "CONSISTENT")


@pytest.mark.parametrize("field,value,code", [("receipt_id", "execution-receipt." + "0" * 32, "RECEIPT_DIGEST_MISMATCH"),
    ("broker_confirmation", "filled", "RECEIPT_OUTCOME_CONFLICT"),
    ("strategy_revision", "c" * 40, "RECEIPT_IDENTITY_MISMATCH")])
def test_observed_receipt_contradictions_fail(field, value, code):
    document = fixture()
    receipt = document["source"]["payload"]["execution_receipt"]
    receipt[field] = value
    if field != "receipt_id":
        seal_receipt(receipt)
    document["normalized"]["receipt"] = deepcopy(receipt)
    reseal(document)
    assert_result(document, "FAIL", code)


def test_missing_receipt_is_not_synthesized_from_status_or_order_counts():
    document = fixture()
    del document["source"]["payload"]["execution_receipt"]
    document["source"]["payload"]["summary"]["orders_submitted_count"] = 0
    reseal(document)
    assert_result(document, "BLOCKED", "REQUIRED_OBSERVATION_MISSING")


def test_dry_run_and_paper_remain_separate_axes():
    document = fixture()
    report = document["source"]["payload"]
    report["dry_run"] = True
    report["runtime_target"]["dry_run_only"] = True
    report["runtime_target"].update(execution_environment="dry_run", execution_mode="paper")
    receipt = report["execution_receipt"]
    receipt.update(execution_mode="paper", outcome="no_action", broker_confirmation="not_applicable")
    seal_receipt(receipt)
    report["summary"]["execution_status"] = "dry_run"
    document["normalized"].update(execution_lane="dry_run", receipt=deepcopy(receipt))
    reseal(document)
    assert_result(document, "CONSISTENT")
    document["normalized"]["execution_lane"] = "live"
    reseal(document)
    assert_result(document, "FAIL", "EXECUTION_LANE_MISMATCH")


def test_dry_run_acknowledgement_is_a_conflict_not_zero_activity():
    document = fixture()
    document["source"]["payload"]["dry_run"] = True
    document["normalized"]["execution_lane"] = "dry_run"
    reseal(document)
    assert_result(document, "FAIL", "DRY_RUN_RECEIPT_CONFLICT")


def test_explicit_paper_environment_retains_legacy_live_receipt_label():
    document = fixture()
    report = document["source"]["payload"]
    report["runtime_target"].update(execution_environment="paper", execution_mode="live", dry_run_only=False)
    report["execution_receipt"]["execution_mode"] = "live"
    seal_receipt(report["execution_receipt"])
    document["normalized"]["receipt"] = deepcopy(report["execution_receipt"])
    reseal(document)
    result = assert_result(document, "CONSISTENT")
    assert result["account_domain_authenticated"] is False


def test_missing_explicit_environment_cannot_be_recovered_from_legacy_mode():
    document = fixture()
    document["source"]["payload"]["runtime_target"].pop("execution_environment", None)
    reseal(document)
    assert_result(document, "BLOCKED")


def test_unrecognized_account_snapshot_schema_is_not_a_native_account_observation():
    document = fixture()
    document["source"]["payload"]["summary"]["account_facts"]["schema_version"] = "future-account-schema"
    reseal(document)
    assert_result(document, "BLOCKED", "ACCOUNT_SOURCE_SCHEMA_UNSUPPORTED")


@pytest.mark.parametrize("mode", [None, "Paper", "unknown"])
def test_legacy_status_or_unknown_execution_mode_cannot_prove_paper(mode):
    document = fixture()
    report = document["source"]["payload"]
    report["status"] = "Paper"
    report["runtime_target"]["execution_mode"] = mode
    reseal(document)
    assert_result(document, "BLOCKED")


@pytest.mark.parametrize("flag,kind", [("maintenance_cash_observation", "maintenance_account_observation"),
    ("validation_only", "diagnostic")])
def test_diagnostic_observations_are_not_normalized_into_strategy_executions(flag, kind):
    document = fixture()
    document["source"]["payload"]["diagnostics"][flag] = True
    reseal(document)
    assert_result(document, "FAIL", "RECORD_KIND_MISMATCH")
    document["normalized"]["record_kind"] = kind
    reseal(document)
    assert_result(document, "CONSISTENT")


@pytest.mark.parametrize("field,value,code", [
    ("decision_at", "2026-10-30T14:00:11Z", "RUN_CLOCK_CONFLICT"),
    ("finished_at", "2026-10-30T13:59:59Z", "RUN_CLOCK_CONFLICT"),
    ("started_at", "2026-10-30T14:00:00", "UTC_TIMESTAMP_REQUIRED"),
])
def test_invalid_or_future_run_clocks_fail(field, value, code):
    document = fixture()
    document["source"]["payload"][field] = value
    document["normalized"][field] = value
    reseal(document)
    assert_result(document, "FAIL", code)


def test_binding_interval_uses_an_exclusive_end():
    document = fixture()
    document["account_binding"]["payload"]["effective_until"] = DECISION
    reseal(document)
    assert_result(document, "FAIL", "ACCOUNT_BINDING_NOT_EFFECTIVE")


@pytest.mark.parametrize("start", ["2026-10-30T14:00:00.500000Z", "2026-10-30T14:00:01.500000Z"])
def test_pre_report_decision_clock_remains_valid_across_initialization_delay(start):
    document = fixture()
    report = document["source"]["payload"]
    report["started_at"] = start
    report["runtime_release_receipt"]["loaded_at"] = start
    report["summary"]["account_facts"]["observed_at"] = start
    document["normalized"]["started_at"] = start
    reseal(document)
    assert_result(document, "CONSISTENT")


def test_loaded_release_clock_cannot_conflict_with_its_native_report_start():
    document = fixture()
    document["source"]["payload"]["runtime_release_receipt"]["loaded_at"] = "2026-10-30T14:00:02Z"
    reseal(document)
    assert_result(document, "FAIL", "RELEASE_CLOCK_CONFLICT")


def test_post_finish_receipt_clock_stays_unverified_without_terminal_path_evidence():
    document = fixture()
    report = document["source"]["payload"]
    report["status"] = "error"
    report["summary"]["execution_status"] = "failed"
    receipt = report["execution_receipt"]
    receipt.update(observed_at="2026-10-30T14:00:11Z", outcome="failed", broker_confirmation="not_observed")
    seal_receipt(receipt)
    document["normalized"]["receipt"] = deepcopy(receipt)
    reseal(document)
    assert_result(document, "BLOCKED", "POST_FINISH_RECEIPT_SOURCE_UNVERIFIED")


def add_daily(document):
    payload = {"platform": "ibkr", "observed_at": CAPTURED, "completeness": "incomplete",
               "read_errors": ["coverage_unconfirmed"], "records": [{
                   "target": {"service": "fixture-service", "strategy_profile": "tqqq_growth_income", "account_scope": "fixture-scope"},
                   "completeness": "incomplete", "runs": [], "excluded_reports": [], "conflicts": [],
                   "fills": {"source": "not_connected", "records": [], "count": None}}]}
    daily = envelope("daily-fixture", payload, 7)
    daily["producer"] = {"platform_revision": "a" * 40, "qpk_revision": "b" * 40, "files": {
        "InteractiveBrokersPlatform:scripts/project_ibkr_runtime_daily.py": "d5cb50eabc838c9c47064c746f3f15dbdb427e17c95ce1a70f7ff0ee580afb03"}}
    document["daily_projection"] = daily
    document["normalized"]["daily_projection"] = {"source_id": daily["source_id"],
        "source_payload_sha256": daily["payload_sha256"], "coverage": "UNKNOWN", "individual_fills": "UNKNOWN"}
    reseal(document)


def test_daily_projection_retains_unknown_coverage_and_not_connected_fill_source():
    document = fixture()
    add_daily(document)
    result = assert_result(document, "CONSISTENT")
    assert "DAILY_RUN_LINKAGE_UNVERIFIED" in result["gaps"]


@pytest.mark.parametrize("change", ["zero_fill", "normalized_complete", "wrong_profile"])
def test_daily_projection_cannot_create_zero_fills_coverage_or_cross_profile_link(change):
    document = fixture()
    add_daily(document)
    row = document["daily_projection"]["payload"]["records"][0]
    if change == "zero_fill":
        row["fills"]["count"] = 0
    elif change == "normalized_complete":
        document["normalized"]["daily_projection"]["coverage"] = "COMPLETE"
    else:
        row["target"]["strategy_profile"] = "soxl_soxx_trend_income"
    reseal(document)
    assert_result(document, "FAIL")


def test_caller_success_flags_cannot_remove_blockers_or_grant_authority():
    document = fixture()
    document.update(verified=True, complete=True, source_authenticated=True,
                    execution_authorized=True, production_ready=True, historical_parity_established=True)
    assert_result(document, "CONSISTENT")


def test_receipt_has_no_native_run_id_even_when_its_parent_report_is_bound():
    document = fixture()
    result = assert_result(document, "CONSISTENT")
    assert "RECEIPT_HAS_NO_NATIVE_RUN_ID" in result["gaps"]
    assert "ALIAS_TO_ORIGINAL_BINDING_UNVERIFIED" in result["gaps"]
    receipt = document["source"]["payload"]["execution_receipt"]
    receipt["run_id"] = RUN
    seal_receipt(receipt)
    document["normalized"]["receipt"] = deepcopy(receipt)
    reseal(document)
    assert_result(document, "FAIL", "RECEIPT_NATIVE_FIELDS_CONFLICT")


@pytest.mark.parametrize("contradiction", ["duplicate_source", "reused_locator"])
def test_daily_observation_cannot_reuse_a_report_identity(contradiction):
    document = fixture()
    add_daily(document)
    if contradiction == "duplicate_source":
        document["daily_projection"]["source_id"] = document["source"]["source_id"]
        document["normalized"]["daily_projection"]["source_id"] = document["source"]["source_id"]
    else:
        document["daily_projection"]["locator"] = deepcopy(document["source"]["locator"])
    reseal(document)
    assert_result(document, "FAIL", "SOURCE_LOCATOR_REUSED" if contradiction == "reused_locator" else "DUPLICATE_SOURCE_ID")


def test_noncanonical_receipt_clock_cannot_invent_a_different_native_receipt_digest():
    document = fixture()
    receipt = document["source"]["payload"]["execution_receipt"]
    receipt["observed_at"] = "2026-10-30T14:00:10+00:00"
    seal_receipt(receipt)
    document["normalized"]["receipt"] = deepcopy(receipt)
    reseal(document)
    assert_result(document, "FAIL", "RECEIPT_CLOCK_NOT_CANONICAL")


@pytest.mark.parametrize("status", ["pending_reconciliation", "unknown", "unknown_order"])
def test_known_unknown_cycle_cannot_become_acknowledged_receipt(status):
    document = fixture()
    document["source"]["payload"]["summary"]["execution_status"] = status
    reseal(document)
    assert_result(document, "FAIL", "CYCLE_RECEIPT_OUTCOME_CONFLICT")


def test_checker_is_pure_and_emits_no_source_values(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("source checker must not perform I/O")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    document = fixture()
    document["source"]["payload"]["errors"] = [{"message": "PRIVATE-FIXTURE-MARKER"}]
    reseal(document)
    result = validate_source_evidence(document)
    text = json.dumps(result)
    assert "PRIVATE-FIXTURE-MARKER" not in text
    assert ACCOUNT not in text and RUN not in text and "fixture-service" not in text
    assert result["status"] == "BLOCKED"


def test_malformed_diagnostics_return_a_redacted_failure_instead_of_raising():
    document = fixture()
    document["source"]["payload"]["diagnostics"] = "PRIVATE-MALFORMED-FIXTURE"
    reseal(document)
    result = assert_result(document, "FAIL", "OBJECT_REQUIRED")
    assert "PRIVATE-MALFORMED-FIXTURE" not in json.dumps(result)


@pytest.mark.parametrize("value", [None, [], True, "bad", {"schema_version": "future"}])
def test_malformed_or_unsupported_inputs_return_bounded_results(value):
    result = validate_source_evidence(value)
    assert result["status"] in {"FAIL", "BLOCKED"}
    assert result["execution_authorized"] is False
