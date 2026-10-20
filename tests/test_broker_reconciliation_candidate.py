from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from application.broker_reconciliation_candidate import (
    SOURCE_RECEIPT_RECORD_SCHEMA_VERSION,
    SourceReceiptExpectation,
    build_reconciliation_candidate_v2,
    calculate_source_receipts_sha256,
    canonical_source_receipt_records_json,
)
from quant_platform_kit.common.broker_reconciliation import (
    build_broker_reconciliation_evidence,
)
from quant_platform_kit.common.broker_reconciliation_enrollment import (
    BROKER_RECONCILIATION_BASELINE_CANDIDATE_V2_SCHEMA_VERSION,
    evaluate_broker_reconciliation_baseline_enrollment,
)


_DIGEST = "a" * 64
_OTHER_DIGEST = "b" * 64
_HEAD_SHA = "c" * 40
_SERVICE_COMMIT_SHA = "d" * 40


def _evidences():
    observed_at = datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc)
    shared = {
        "platform_id": "ibkr",
        "strategy_profile": "global_etf_rotation",
        "account_scope_sha256": _DIGEST,
        "baseline_id": "legacy-live-baseline",
        "baseline_target_sha256": _DIGEST,
        "runtime_target_sha256": _DIGEST,
        "positions_sha256": _DIGEST,
        "cash_sha256": _DIGEST,
        "open_orders_sha256": _DIGEST,
        "recent_executions_sha256": _DIGEST,
        "local_execution_ledger_sha256": _DIGEST,
        "broker_connected": True,
        "account_identity_match": True,
        "positions_match": True,
        "cash_match": True,
        "open_orders_match": True,
        "recent_executions_match": True,
        "local_execution_ledger_match": True,
    }
    return (
        build_broker_reconciliation_evidence(observed_at=observed_at, **shared),
        build_broker_reconciliation_evidence(
            observed_at=observed_at + timedelta(minutes=2), **shared
        ),
    )


def _candidate():
    evaluation = evaluate_broker_reconciliation_baseline_enrollment(
        _evidences(), now=datetime(2026, 9, 4, 8, 3, tzinfo=timezone.utc),
    )
    assert evaluation.candidate is not None
    return evaluation.candidate


def _expectations() -> tuple[SourceReceiptExpectation, ...]:
    return (
        SourceReceiptExpectation(
            strategy_profile="global_etf_rotation",
            repository="QuantStrategyLab/InteractiveBrokersPlatform",
            workflow_path=".github/workflows/collect-reconciliation-evidence.yml",
            workflow_ref="refs/heads/main",
            workflow_conclusion="success",
            workflow_run_id="10",
            workflow_run_attempt="1",
            workflow_head_sha=_HEAD_SHA,
            artifact_id="100",
            artifact_name="ibkr-reconciliation-global_etf_rotation-10",
            artifact_sha256=_DIGEST,
            evidence_sha256=_candidate().source_evidence_sha256[0],
            service_name="interactive-brokers-platform",
            service_revision="ibkr-service-00002",
            service_revision_commit_sha=_SERVICE_COMMIT_SHA,
            service_deploy_run_id="deploy-10",
            scheduler_job_sha256="e" * 64,
        ),
        SourceReceiptExpectation(
            strategy_profile="global_etf_rotation",
            repository="QuantStrategyLab/InteractiveBrokersPlatform",
            workflow_path=".github/workflows/collect-reconciliation-evidence.yml",
            workflow_ref="refs/heads/main",
            workflow_conclusion="success",
            workflow_run_id="20",
            workflow_run_attempt="1",
            workflow_head_sha=_HEAD_SHA,
            artifact_id="200",
            artifact_name="ibkr-reconciliation-global_etf_rotation-20",
            artifact_sha256=_OTHER_DIGEST,
            evidence_sha256=_candidate().source_evidence_sha256[1],
            service_name="interactive-brokers-platform",
            service_revision="ibkr-service-00002",
            service_revision_commit_sha=_SERVICE_COMMIT_SHA,
            service_deploy_run_id="deploy-20",
            scheduler_job_sha256="f" * 64,
        ),
    )


def _records() -> tuple[dict[str, str], ...]:
    return (
        {
            "schema_version": SOURCE_RECEIPT_RECORD_SCHEMA_VERSION,
            "repository": "QuantStrategyLab/InteractiveBrokersPlatform",
            "workflow_path": ".github/workflows/collect-reconciliation-evidence.yml",
            "workflow_run_id": "20",
            "workflow_run_attempt": "1",
            "workflow_head_sha": _HEAD_SHA,
            "artifact_id": "200",
            "artifact_name": "ibkr-reconciliation-global_etf_rotation-20",
            "artifact_sha256": _OTHER_DIGEST,
            "evidence_sha256": _candidate().source_evidence_sha256[1],
            "service_name": "interactive-brokers-platform",
            "service_revision": "ibkr-service-00002",
            "service_revision_commit_sha": _SERVICE_COMMIT_SHA,
            "service_deploy_run_id": "deploy-20",
            "scheduler_job_sha256": "f" * 64,
        },
        {
            "schema_version": SOURCE_RECEIPT_RECORD_SCHEMA_VERSION,
            "repository": "QuantStrategyLab/InteractiveBrokersPlatform",
            "workflow_path": ".github/workflows/collect-reconciliation-evidence.yml",
            "workflow_run_id": "10",
            "workflow_run_attempt": "1",
            "workflow_head_sha": _HEAD_SHA,
            "artifact_id": "100",
            "artifact_name": "ibkr-reconciliation-global_etf_rotation-10",
            "artifact_sha256": _DIGEST,
            "evidence_sha256": _candidate().source_evidence_sha256[0],
            "service_name": "interactive-brokers-platform",
            "service_revision": "ibkr-service-00002",
            "service_revision_commit_sha": _SERVICE_COMMIT_SHA,
            "service_deploy_run_id": "deploy-10",
            "scheduler_job_sha256": "e" * 64,
        },
    )


def test_builds_v2_candidate_from_exactly_two_designated_redacted_source_records() -> (
    None
):
    candidate = _candidate()
    records = _records()

    result = build_reconciliation_candidate_v2(
        candidate, source_receipt_records=records, expectations=_expectations()
    )

    assert (
        result.schema_version
        == BROKER_RECONCILIATION_BASELINE_CANDIDATE_V2_SCHEMA_VERSION
    )
    assert result.source_receipts_sha256 == calculate_source_receipts_sha256(
        tuple(reversed(records)),
        strategy_profile=candidate.strategy_profile,
        expectations=_expectations(),
    )
    assert result.candidate_sha256 != candidate.candidate_sha256
    assert set(json.loads(canonical_source_receipt_records_json(
        records,
        strategy_profile=candidate.strategy_profile,
        expectations=_expectations(),
    ))[0]) == set(records[0])


@pytest.mark.parametrize(
    ("records", "message"),
    (
        (_records()[:1], "artifact IDs must match"),
        ((_records()[0], _records()[0]), "artifact/run pairs must be unique"),
        (({**_records()[0], "unexpected": "field"}, _records()[1]), "invalid fields"),
    ),
)
def test_source_records_fail_closed_on_invalid_count_uniqueness_or_schema(
    records, message
) -> None:
    with pytest.raises(ValueError, match=message):
        build_reconciliation_candidate_v2(
            _candidate(), source_receipt_records=records, expectations=_expectations()
        )


def test_source_records_fail_closed_when_profile_or_source_correspondence_mismatches() -> (
    None
):
    wrong_run, valid = _records()
    wrong_run = {**wrong_run, "workflow_run_id": "99"}
    wrong_profile_expectations = (
        replace(
            _expectations()[0],
            strategy_profile="other_profile",
            artifact_name="ibkr-reconciliation-other_profile-10",
        ),
        replace(
            _expectations()[1],
            strategy_profile="other_profile",
            artifact_name="ibkr-reconciliation-other_profile-20",
        ),
    )

    with pytest.raises(ValueError, match="strategy_profile must match"):
        build_reconciliation_candidate_v2(
            _candidate(),
            source_receipt_records=_records(),
            expectations=wrong_profile_expectations,
        )
    with pytest.raises(ValueError, match="provenance does not match"):
        build_reconciliation_candidate_v2(
            _candidate(), source_receipt_records=(wrong_run, valid), expectations=_expectations()
        )


def test_source_records_fail_closed_when_service_revision_provenance_mismatches() -> (
    None
):
    tampered, valid = _records()
    tampered = {**tampered, "service_revision_commit_sha": _HEAD_SHA}

    with pytest.raises(ValueError, match="provenance does not match"):
        build_reconciliation_candidate_v2(
            _candidate(),
            source_receipt_records=(tampered, valid),
            expectations=_expectations(),
        )


def test_source_records_fail_closed_when_frozen_source_record_field_is_missing() -> (
    None
):
    incomplete, valid = _records()
    incomplete = {key: value for key, value in incomplete.items() if key != "repository"}

    with pytest.raises(ValueError, match="invalid fields"):
        build_reconciliation_candidate_v2(
            _candidate(), source_receipt_records=(incomplete, valid), expectations=_expectations()
        )


def test_source_record_expectations_require_main_success_and_workflow_artifact_name() -> (
    None
):
    non_main = replace(_expectations()[0], workflow_ref="refs/heads/feature")
    unsuccessful = replace(_expectations()[0], workflow_conclusion="failure")
    malformed_name = replace(_expectations()[1], artifact_name="unexpected")

    with pytest.raises(ValueError, match="workflow_ref"):
        calculate_source_receipts_sha256(
            _records(), strategy_profile="global_etf_rotation", expectations=(non_main, _expectations()[1])
        )
    with pytest.raises(ValueError, match="workflow_conclusion"):
        calculate_source_receipts_sha256(
            _records(),
            strategy_profile="global_etf_rotation",
            expectations=(unsuccessful, _expectations()[1]),
        )
    with pytest.raises(ValueError, match="artifact_name"):
        calculate_source_receipts_sha256(
            _records(), strategy_profile="global_etf_rotation", expectations=(_expectations()[0], malformed_name)
        )


def test_source_receipts_root_covers_every_frozen_source_record_field() -> None:
    records = _records()
    baseline_root = calculate_source_receipts_sha256(
        records, strategy_profile="global_etf_rotation", expectations=_expectations()
    )
    updated_record = {**records[0], "evidence_sha256": "f" * 64}
    updated_expectation = replace(_expectations()[1], evidence_sha256="f" * 64)

    assert baseline_root != calculate_source_receipts_sha256(
        (updated_record, records[1]),
        strategy_profile="global_etf_rotation",
        expectations=(_expectations()[0], updated_expectation),
    )


def test_v2_builder_rejects_existing_v2_candidate_and_tampered_provenance_root() -> (
    None
):
    candidate = _candidate()
    upgraded = build_reconciliation_candidate_v2(
        candidate, source_receipt_records=_records(), expectations=_expectations()
    )

    with pytest.raises(ValueError, match="only a v1"):
        build_reconciliation_candidate_v2(
            upgraded, source_receipt_records=_records(), expectations=_expectations()
        )

    tampered = upgraded.to_dict()
    tampered["source_receipts_sha256"] = _OTHER_DIGEST
    with pytest.raises(ValueError, match="candidate_sha256 mismatch"):
        type(upgraded).from_dict(tampered)


def test_one_designated_source_record_does_not_require_repeated_collection():
    expectation = _expectations()[0]
    record = next(item for item in _records() if item["artifact_id"] == expectation.artifact_id)
    root = calculate_source_receipts_sha256(
        [record], strategy_profile=expectation.strategy_profile, expectations=[expectation],
    )
    assert len(root) == 64


def test_source_records_must_bind_actual_candidate_evidence_members():
    # Well-formed provenance must still bind the actual candidate members.
    records = tuple({**item, "evidence_sha256": _OTHER_DIGEST} for item in _records())
    expectations = tuple(replace(item, evidence_sha256=_OTHER_DIGEST) for item in _expectations())
    with pytest.raises(ValueError, match="evidence"):
        build_reconciliation_candidate_v2(
            _candidate(), source_receipt_records=records, expectations=expectations,
        )


@pytest.mark.parametrize("value", [None, 1, "", "e" * 63, "e" * 65, "E" * 64, "g" * 64, " e" * 32])
@pytest.mark.parametrize("side", ["record", "expectation"])
def test_scheduler_identity_requires_canonical_sha256(value, side):
    records = list(_records())
    expectations = list(_expectations())
    if side == "record":
        records[0] = {**records[0], "scheduler_job_sha256": value}
    else:
        expectations[0] = replace(expectations[0], scheduler_job_sha256=value)
    with pytest.raises(ValueError, match="scheduler_job_sha256"):
        calculate_source_receipts_sha256(
            records, strategy_profile="global_etf_rotation", expectations=expectations,
        )


def test_fourteen_field_record_has_no_implicit_scheduler_compatibility():
    incomplete, valid = _records()
    incomplete = {key: value for key, value in incomplete.items() if key != "scheduler_job_sha256"}
    with pytest.raises(ValueError, match="missing required scheduler_job_sha256"):
        build_reconciliation_candidate_v2(
            _candidate(), source_receipt_records=(incomplete, valid), expectations=_expectations(),
        )


def test_different_valid_scheduler_hash_is_rejected_by_independent_expectation():
    changed, valid = _records()
    changed = {**changed, "scheduler_job_sha256": "0" * 64}
    with pytest.raises(ValueError, match="provenance does not match"):
        build_reconciliation_candidate_v2(
            _candidate(), source_receipt_records=(changed, valid), expectations=_expectations(),
        )


def test_scheduler_identity_is_retained_in_canonical_source_root():
    records = _records()
    expected = _expectations()
    canonical = canonical_source_receipt_records_json(
        records, strategy_profile="global_etf_rotation", expectations=expected,
    )
    assert [item["scheduler_job_sha256"] for item in json.loads(canonical)] == ["e" * 64, "f" * 64]
    changed = ({**records[0], "scheduler_job_sha256": "0" * 64}, records[1])
    changed_expected = (expected[0], replace(expected[1], scheduler_job_sha256="0" * 64))
    assert calculate_source_receipts_sha256(
        records, strategy_profile="global_etf_rotation", expectations=expected,
    ) != calculate_source_receipts_sha256(
        changed, strategy_profile="global_etf_rotation", expectations=changed_expected,
    )


def test_cli_source_input_requires_explicit_scheduler_expectation(tmp_path):
    from dataclasses import asdict
    from scripts.build_reconciliation_baseline_candidate import load_source_inputs

    records_path = tmp_path / "records.json"
    expectations_path = tmp_path / "expectations.json"
    records_path.write_text(json.dumps(_records()))
    expectations_path.write_text(json.dumps([asdict(item) for item in _expectations()]))
    records, expectations = load_source_inputs(records_path, expectations_path)
    assert calculate_source_receipts_sha256(
        records, strategy_profile="global_etf_rotation", expectations=expectations,
    ) == calculate_source_receipts_sha256(
        _records(), strategy_profile="global_etf_rotation", expectations=_expectations(),
    )
    missing = [asdict(item) for item in _expectations()]
    missing[0].pop("scheduler_job_sha256")
    with pytest.raises(TypeError, match="scheduler_job_sha256"):
        SourceReceiptExpectation(**missing[0])
    expectations_path.write_text(json.dumps(missing))
    with pytest.raises(ValueError, match="private source inputs are invalid or unavailable"):
        load_source_inputs(records_path, expectations_path)


def test_scheduler_expectations_reach_real_builder_and_verify_only_consumer():
    from quant_platform_kit.common.reconciliation_recovery import (
        calculate_reconciliation_recovery_confirmation_sha256,
    )
    from scripts.build_reconciliation_baseline_candidate import evaluate_receipts
    from scripts.verify_reconciliation_recovery import verify_reconciliation_recovery

    # This exercises the existing consumer's canonical inner evidence contract.
    # It does not substitute that digest for the workflow's outer file hash.
    evidences = _evidences()
    sources = {"source_receipt_records": _records(), "expectations": _expectations()}
    payloads = [{"evidence": item.to_dict()} for item in evidences]
    now = datetime(2026, 9, 4, 8, 3, tzinfo=timezone.utc)
    candidate = evaluate_receipts(payloads, now=now, **sources)
    assert candidate["ready_for_independent_review"] is True
    digest = candidate["candidate"]["candidate_sha256"]
    confirmation = {
        "schema_version": "qsl_reconciliation_recovery_confirmation.v1",
        "recovery_id": "synthetic-scheduler-binding", "candidate_sha256": digest,
        "dual_review_binding_sha256": digest, "confirmed_at": now.isoformat().replace("+00:00", "Z"),
        "confirmed_by": "synthetic-reviewer", "no_order": True,
        "execution_authority_granted": False, "confirmation_sha256": "0" * 64,
    }
    confirmation["confirmation_sha256"] = calculate_reconciliation_recovery_confirmation_sha256(confirmation)
    console = {
        "ok": True, "schema_version": "qsl_reconciliation_recovery_controller_read.v1",
        "policy": {"no_order": True, "execution_authority_granted": False, "controller_must_reverify": True},
        "recovery": {
            "recovery_id": "synthetic-scheduler-binding", "platform": "ibkr",
            "strategy_profile": "global_etf_rotation", "environment": "live",
            "reconciliation_state": "RECONCILE_ONLY", "candidate_sha256": digest,
            "dual_review_binding_sha256": digest, "evidence_sample_count": 2,
        },
        "confirmation": confirmation,
    }
    current = {**evidences[-1].to_dict(), "observed_at": (now + timedelta(minutes=1)).isoformat()}
    # Rebuild a real content-addressed observation with the later source time.
    current.pop("evidence_sha256")
    current.pop("schema_version")
    current_evidence = build_broker_reconciliation_evidence(**current)
    result = verify_reconciliation_recovery(
        candidate_payload=candidate, **sources, confirmation_payload=console,
        current_receipt_payload={"evidence": current_evidence.to_dict()},
        runtime_target_payload={
            "platform_id": "ibkr", "strategy_profile": "global_etf_rotation",
            "live_continuity": {"state": "RECONCILE_ONLY", "baseline_id": "legacy-live-baseline", "baseline_target_sha256": _DIGEST},
        },
        recovery_id="synthetic-scheduler-binding", now=now + timedelta(minutes=2),
    )
    assert result["ready_for_atomic_state_transition"] is True
    assert result["policy"] == {
        "controller_mode": "verify_only", "no_order": True,
        "execution_authority_granted": False, "state_write_attempted": False,
    }
    with pytest.raises(ValueError, match="provenance does not match"):
        verify_reconciliation_recovery(
            candidate_payload=candidate,
            source_receipt_records=({**_records()[0], "scheduler_job_sha256": "0" * 64}, _records()[1]),
            expectations=_expectations(), confirmation_payload=console,
            current_receipt_payload={"evidence": current_evidence.to_dict()},
            runtime_target_payload={}, recovery_id="synthetic-scheduler-binding",
            now=now + timedelta(minutes=2),
        )
