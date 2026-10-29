from __future__ import annotations

import json
import hashlib
import pytest
from datetime import datetime, timedelta, timezone

from quant_platform_kit.common.broker_reconciliation import build_broker_reconciliation_evidence
from scripts.build_reconciliation_baseline_candidate import evaluate_receipts
from tests.test_reconciliation_baseline_candidate_script import _sources_for_evidences
from quant_platform_kit.common.reconciliation_recovery import (
    calculate_reconciliation_recovery_confirmation_sha256,
)

from scripts.verify_reconciliation_recovery import (
    read_console_confirmation,
    verify_reconciliation_recovery,
)


def _digest(character: str) -> str:
    return character * 64


def _evidence(*, observed_at: datetime) -> object:
    return build_broker_reconciliation_evidence(
        platform_id="ibkr",
        strategy_profile="soxl_soxx_trend_income",
        account_scope_sha256=_digest("a"),
        baseline_id="soxl-ibkr-lkg-20260830",
        baseline_target_sha256=_digest("b"),
        runtime_target_sha256=_digest("b"),
        observed_at=observed_at,
        broker_connected=True,
        account_identity_match=True,
        positions_match=True,
        cash_match=True,
        open_orders_match=True,
        recent_executions_match=True,
        local_execution_ledger_match=True,
        positions_sha256=_digest("c"),
        cash_sha256=_digest("d"),
        open_orders_sha256=_digest("e"),
        recent_executions_sha256=_digest("f"),
        local_execution_ledger_sha256=_digest("0"),
    )


def _candidate_payload(start: datetime) -> dict[str, object]:
    observations = [_evidence(observed_at=start), _evidence(observed_at=start + timedelta(minutes=2))]
    return evaluate_receipts(
        [{"evidence": item.to_dict()} for item in observations], now=start + timedelta(minutes=3),
        **_sources_for_evidences(observations),
    )


def _candidate_source_inputs(start):
    return _sources_for_evidences([_evidence(observed_at=start), _evidence(observed_at=start + timedelta(minutes=2))])



def _dual_review(candidate_sha256: str) -> dict[str, object]:
    return {
        "trigger": "reconciliation_baseline",
        "strategy_profile": "soxl_soxx_trend_income",
        "primary_review": {"verdict": "approve", "confidence": 0.99},
        "secondary_review": {
            "mode": "dual_api",
            "gpt": {"verdict": "approve", "confidence": 0.98},
            "claude": {"verdict": "approve", "confidence": 0.98},
        },
        "escalated": True,
        "outcome": "pass",
        "evidence_binding_sha256": candidate_sha256,
        "requires_human_recovery_approval": True,
        "recovery_authority": {"human_review_required": True, "final_action": "escalate"},
    }


def _confirmation(candidate_sha256: str, *, confirmed_at: datetime) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "qsl_reconciliation_recovery_confirmation.v1",
        "recovery_id": "ibkr-soxl-live-recovery",
        "candidate_sha256": candidate_sha256,
        "dual_review_binding_sha256": candidate_sha256,
        "confirmed_at": confirmed_at.isoformat().replace("+00:00", "Z"),
        "confirmed_by": "recovery-admin",
        "no_order": True,
        "execution_authority_granted": False,
        "confirmation_sha256": "0" * 64,
    }
    value["confirmation_sha256"] = calculate_reconciliation_recovery_confirmation_sha256(value)
    return value


def _console_response(candidate_sha256: str, *, confirmed_at: datetime) -> dict[str, object]:
    return {
        "ok": True,
        "schema_version": "qsl_reconciliation_recovery_controller_read.v1",
        "recovery": {
            "recovery_id": "ibkr-soxl-live-recovery",
            "platform": "ibkr",
            "strategy_profile": "soxl_soxx_trend_income",
            "environment": "live",
            "reconciliation_state": "RECONCILE_ONLY",
            "candidate_sha256": candidate_sha256,
            "dual_review_binding_sha256": candidate_sha256,
            "evidence_sample_count": 2,
            "first_observed_at": "2026-08-31T01:00:00Z",
            "last_observed_at": "2026-08-31T01:02:00Z",
        },
        "confirmation": _confirmation(candidate_sha256, confirmed_at=confirmed_at),
        "policy": {
            "no_order": True,
            "execution_authority_granted": False,
            "controller_must_reverify": True,
        },
    }


def _runtime_target(*, state: str = "RECONCILE_ONLY") -> dict[str, object]:
    return {
        "platform_id": "ibkr",
        "strategy_profile": "soxl_soxx_trend_income",
        "live_continuity": {
            "state": state,
            "baseline_id": "soxl-ibkr-lkg-20260830",
            "baseline_target_sha256": _digest("b"),
        },
    }


def test_initial_confirmation_consumes_bound_facts_and_same_scope_without_state_write():
    from scripts.build_reconciliation_baseline_candidate import evaluate_initial_receipt
    from application.broker_reconciliation_candidate import initial_private_source_sha256
    start = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    original = _evidence(observed_at=start)
    request_id = "853a2e08-9396-4fe8-89ee-59fb17e40a1d"
    record = {"schema_version": "ibkr_initial_private_source.v1", "repository": "QuantStrategyLab/InteractiveBrokersPlatform",
              "workflow_path": ".github/workflows/collect-reconciliation-evidence.yml", "workflow_run_id": "1",
              "workflow_run_attempt": "1", "workflow_head_sha": "c" * 40, "service_name": "synthetic",
              "service_revision": "synthetic-revision", "scheduler_job_sha256": "a" * 64,
              "report_uri": f"gs://synthetic/reports/initial-enrollment/{request_id}.json#1", "report_generation": "1",
              "request_id": request_id}
    source = {"source_receipt_records": [record], "expectations": []}
    scope = {"kind": "synthetic", "currency": "USD", "window_start": None, "window_end": "one",
             "limit_per_namespace": None, "gateway_availability": None, "truncated": None}
    surfaces = {name: {"status": "complete", "items": [], "scope": scope} for name in
                ("positions", "cash", "open_orders", "recent_executions", "local_execution_ledger")}
    facts = {"schema_version": "ibkr_initial_facts.v1", "account_scope_sha256": _digest("a"),
             "observed_at": "2026-08-31T01:00:00Z", "surfaces": surfaces}
    candidate = evaluate_initial_receipt({"evidence": original.to_dict()}, facts, now=start,
                                         private_source_sha256=initial_private_source_sha256(record))
    candidate_value = candidate["candidate"]
    confirmation = {"schema_version": "qsl_initial_enrollment_confirmation.v1", "mode": "initial_enrollment",
                    "recovery_id": "synthetic-recovery", "candidate_sha256": candidate_value["candidate_sha256"],
                    "facts_sha256": candidate_value["facts_sha256"], "confirmed_at": "2026-08-31T01:01:00Z",
                    "confirmed_by": "synthetic-user", "no_order": True, "execution_authority_granted": False}
    confirmation["confirmation_sha256"] = hashlib.sha256(json.dumps(confirmation, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    response = {"ok": True, "schema_version": "qsl_initial_enrollment_controller_read.v1",
                "recovery": {"mode": "initial_enrollment", "recovery_id": "synthetic-recovery",
                             "candidate_sha256": candidate_value["candidate_sha256"], "facts_sha256": candidate_value["facts_sha256"]},
                "confirmation": confirmation, "policy": {"no_order": True, "execution_authority_granted": False,
                                                           "controller_must_reverify": True}}
    current = _evidence(observed_at=start + timedelta(minutes=2))
    current_facts = {**facts, "observed_at": "2026-08-31T01:02:00Z",
                     "surfaces": {name: {**item, "scope": {**item["scope"], "window_end": "two"}}
                                  for name, item in surfaces.items()}}
    current_digest = hashlib.sha256(json.dumps(current_facts, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    kwargs = dict(candidate_payload=candidate, **source, confirmation_payload=response,
                  current_receipt_payload={"diagnostics": {"broker_reconciliation": {"evidence": current.to_dict()},
                                                           "initial_enrollment_private": {"facts_sha256": current_digest}}},
                  runtime_target_payload=_runtime_target(), initial_facts=facts, current_initial_facts=current_facts,
                  recovery_id="synthetic-recovery", now=start + timedelta(minutes=3))
    result = verify_reconciliation_recovery(**kwargs)
    assert result["ready_for_atomic_state_transition"] is True
    assert result["policy"]["state_write_attempted"] is False
    changed = {**current_facts, "account_scope_sha256": _digest("9")}
    with pytest.raises(ValueError, match="facts binding mismatch"):
        verify_reconciliation_recovery(**{**kwargs, "current_initial_facts": changed})
    changed_scope = {**current_facts, "surfaces": {**current_facts["surfaces"], "cash": {
        **current_facts["surfaces"]["cash"], "scope": {**scope, "currency": "EUR"}}}}
    changed_digest = hashlib.sha256(json.dumps(changed_scope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with pytest.raises(ValueError, match="scope mismatch"):
        verify_reconciliation_recovery(**{**kwargs, "current_initial_facts": changed_scope,
            "current_receipt_payload": {"diagnostics": {"broker_reconciliation": {"evidence": current.to_dict()},
                                                        "initial_enrollment_private": {"facts_sha256": changed_digest}}}})
    altered_values = {k: v for k, v in current.to_dict().items() if k not in {"schema_version", "evidence_sha256"}}
    for field in ("cash_sha256", "positions_sha256", "open_orders_sha256"):
        altered = build_broker_reconciliation_evidence(**{**altered_values, field: _digest("9")})
        result = verify_reconciliation_recovery(**{**kwargs,
            "current_receipt_payload": {"diagnostics": {"broker_reconciliation": {"evidence": altered.to_dict()},
                                                        "initial_enrollment_private": {"facts_sha256": current_digest}}}})
        assert result["ready_for_atomic_state_transition"] is False


def test_verify_only_returns_a_plan_without_attempting_state_write() -> None:
    start = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    candidate = _candidate_payload(start)
    candidate_value = candidate["candidate"]
    assert isinstance(candidate_value, dict)
    candidate_sha256 = str(candidate_value["candidate_sha256"])

    result = verify_reconciliation_recovery(
        candidate_payload=candidate,
        **_candidate_source_inputs(start),
        dual_review_payload=_dual_review(candidate_sha256),
        confirmation_payload=_console_response(candidate_sha256, confirmed_at=start + timedelta(minutes=3)),
        current_receipt_payload={"evidence": _evidence(observed_at=start + timedelta(minutes=4)).to_dict()},
        runtime_target_payload=_runtime_target(),
        recovery_id="ibkr-soxl-live-recovery",
        now=start + timedelta(minutes=5),
    )

    assert result["ready_for_atomic_state_transition"] is True
    assert result["findings"] == []
    assert result["transition_plan"]["next_live_continuity_state"] == "ACTIVE_LKG"  # type: ignore[index]
    assert result["policy"] == {
        "controller_mode": "verify_only",
        "no_order": True,
        "execution_authority_granted": False,
        "state_write_attempted": False,
    }


def test_verify_only_keeps_plan_closed_when_deployed_target_is_not_reconcile_only() -> None:
    start = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    candidate = _candidate_payload(start)
    candidate_value = candidate["candidate"]
    assert isinstance(candidate_value, dict)
    candidate_sha256 = str(candidate_value["candidate_sha256"])

    result = verify_reconciliation_recovery(
        candidate_payload=candidate,
        **_candidate_source_inputs(start),
        dual_review_payload=_dual_review(candidate_sha256),
        confirmation_payload=_console_response(candidate_sha256, confirmed_at=start + timedelta(minutes=3)),
        current_receipt_payload={"evidence": _evidence(observed_at=start + timedelta(minutes=4)).to_dict()},
        runtime_target_payload=_runtime_target(state="PAUSED"),
        recovery_id="ibkr-soxl-live-recovery",
        now=start + timedelta(minutes=5),
    )

    assert result["ready_for_atomic_state_transition"] is False
    assert result["transition_plan"] is None
    assert "reconciliation_recovery_current_state_not_reconcile_only" in result["findings"]


def test_verify_only_rejects_a_current_receipt_with_confirmation_second_timestamp() -> None:
    start = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    candidate = _candidate_payload(start)
    candidate_value = candidate["candidate"]
    assert isinstance(candidate_value, dict)
    candidate_sha256 = str(candidate_value["candidate_sha256"])
    confirmed_at = start + timedelta(minutes=3)

    result = verify_reconciliation_recovery(
        candidate_payload=candidate,
        **_candidate_source_inputs(start),
        dual_review_payload=_dual_review(candidate_sha256),
        confirmation_payload=_console_response(candidate_sha256, confirmed_at=confirmed_at),
        current_receipt_payload={"evidence": _evidence(observed_at=confirmed_at).to_dict()},
        runtime_target_payload=_runtime_target(),
        recovery_id="ibkr-soxl-live-recovery",
        now=start + timedelta(minutes=5),
    )

    assert result["ready_for_atomic_state_transition"] is False
    assert result["transition_plan"] is None
    assert "reconciliation_recovery_evidence_not_reobserved_after_confirmation" in result["findings"]


def test_read_console_confirmation_uses_dedicated_bearer_header() -> None:
    received: dict[str, object] = {}

    class FakeResponse:
        status = 200

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        @staticmethod
        def read() -> bytes:
            return json.dumps({"ok": True}).encode("utf-8")

    def opener(request: object, **kwargs: object) -> FakeResponse:
        received["request"] = request
        received["kwargs"] = kwargs
        return FakeResponse()

    result = read_console_confirmation(
        confirmation_url="https://console.example/api/internal/reconciliation-recovery-confirmation",
        recovery_id="ibkr-soxl-live-recovery",
        token="controller-token",
        opener=opener,
    )

    assert result == {"ok": True}
    request = received["request"]
    assert getattr(request, "get_header")("Authorization") == "Bearer controller-token"
    assert getattr(request, "get_header")("User-agent") == "QSL-IBKR-Reconciliation/1.0"
    assert "recovery_id=ibkr-soxl-live-recovery" in getattr(request, "full_url")


def test_single_source_three_cli_path_needs_no_model_review(tmp_path, monkeypatch, capsys):
    from tests.test_reconciliation_baseline_candidate_script import _archive_inputs
    from scripts import build_reconciliation_baseline_candidate as builder
    from scripts import publish_reconciliation_recovery_source as publisher
    from scripts import verify_reconciliation_recovery as verifier

    start = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    evidence = _evidence(observed_at=start)
    material = _archive_inputs(tmp_path / "original", [evidence])

    def save(name, value):
        path = tmp_path / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    source_args = material["args"]
    assert builder.main(["--now", start.isoformat(), *source_args]) == 0
    candidate = json.loads(capsys.readouterr().out)
    candidate_path = save("candidate.json", candidate)
    digest = candidate["candidate"]["candidate_sha256"]
    assert candidate["candidate"]["schema_version"] == "broker_reconciliation_baseline_candidate.v2"
    assert publisher.main([
        "--candidate", candidate_path, "--recovery-id", "ibkr-soxl-live-recovery",
        "--now", start.isoformat(), *source_args,
    ]) == 0
    snapshot = json.loads(capsys.readouterr().out)
    record = snapshot["recoveries"][0]
    assert record["readiness"] == "awaiting_human_confirmation"
    assert record["evidence_sample_count"] == 1
    assert record["dual_review"] == {"outcome": "unavailable", "reviewer_count": 0, "evidence_binding_sha256": digest}

    response = _console_response(digest, confirmed_at=start + timedelta(minutes=1))
    response["recovery"]["evidence_sample_count"] = 1
    monkeypatch.setattr(verifier, "read_console_confirmation", lambda **_kwargs: response)
    current = _archive_inputs(tmp_path / "current", [_evidence(observed_at=start + timedelta(minutes=2))])
    assert verifier.main([
        "--current-source-archive", str(current["archives"][0]),
        "--current-source-records", str(current["records_path"]),
        "--current-source-expectations", str(current["expectations_path"]),
        "--candidate", candidate_path, "--recovery-id", "ibkr-soxl-live-recovery",
        "--runtime-target", save("runtime.json", _runtime_target()),
        "--confirmation-url", "https://console.example/api/internal/reconciliation-recovery-confirmation",
        "--now", (start + timedelta(minutes=3)).isoformat(), *source_args,
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ready_for_atomic_state_transition"]
    assert result["transition_plan"]["no_order"] is True
    assert result["transition_plan"]["execution_authority_granted"] is False
    assert result["transition_plan"]["requires_atomic_compare_and_set"] is True
    assert result["policy"]["state_write_attempted"] is False


def test_verifier_rejects_console_sample_count_not_matching_candidate():
    import pytest
    start = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    candidate = _candidate_payload(start)
    digest = candidate["candidate"]["candidate_sha256"]
    response = _console_response(digest, confirmed_at=start + timedelta(minutes=3))
    response["recovery"]["evidence_sample_count"] = 1
    with pytest.raises(ValueError, match="sample count"):
        verify_reconciliation_recovery(
            candidate_payload=candidate, **_candidate_source_inputs(start),
            confirmation_payload=response,
            current_receipt_payload={"evidence": _evidence(observed_at=start + timedelta(minutes=4)).to_dict()},
            runtime_target_payload=_runtime_target(), recovery_id="ibkr-soxl-live-recovery",
            now=start + timedelta(minutes=5),
        )



def _raw_verifier_case(tmp_path):
    from tests.test_reconciliation_baseline_candidate_script import _archive_inputs
    start = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    source = _archive_inputs(tmp_path / "source", [_evidence(observed_at=start)])
    current = _archive_inputs(tmp_path / "current", [_evidence(observed_at=start + timedelta(minutes=2))])
    candidate = evaluate_receipts(source["payloads"], now=start,
                                 source_receipt_records=source["records"], expectations=source["expectations"])
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(candidate))
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps(_runtime_target()))
    args = ["--candidate", str(path), "--runtime-target", str(runtime),
            "--recovery-id", "ibkr-soxl-live-recovery", "--confirmation-url", "https://synthetic.invalid/confirmation",
            "--now", (start + timedelta(minutes=3)).isoformat(),
            "--current-source-archive", str(current["archives"][0]),
            "--current-source-records", str(current["records_path"]),
            "--current-source-expectations", str(current["expectations_path"]), *source["args"]]
    return start, source, current, candidate, path, args


def test_verifier_cli_rejects_unmatched_candidate_before_confirmation_read(tmp_path, monkeypatch):
    import pytest
    from scripts import verify_reconciliation_recovery as verifier
    from quant_platform_kit.common.broker_reconciliation_enrollment import calculate_broker_reconciliation_baseline_candidate_sha256
    _, _, _, candidate, path, args = _raw_verifier_case(tmp_path)
    candidate["candidate"]["positions_sha256"] = "1" * 64
    candidate["candidate"]["candidate_sha256"] = calculate_broker_reconciliation_baseline_candidate_sha256(candidate["candidate"])
    path.write_text(json.dumps(candidate))
    calls = []
    monkeypatch.setattr(verifier, "read_console_confirmation", lambda **_k: calls.append(1))
    with pytest.raises(SystemExit):
        verifier.main(args)
    assert not calls


@pytest.mark.parametrize("attack", ["source_bytes", "current_bytes", "current_scheduler", "current_parsed_copy", "missing_current_auth"])
def test_verifier_cli_authenticates_original_and_fresh_receipts_before_console(tmp_path, monkeypatch, attack):
    from scripts import verify_reconciliation_recovery as verifier
    from tests.test_reconciliation_baseline_candidate_script import _rewrite_archive
    _, source, current, _, _, args = _raw_verifier_case(tmp_path)
    if attack in ("source_bytes", "current_bytes"):
        (source if attack == "source_bytes" else current)["archives"][0].write_bytes(b"tampered")
    elif attack == "current_scheduler":
        current["payloads"][0]["reconciliation_scheduler_job_sha256"] = "f" * 64
        _rewrite_archive(current, current["payloads"][0], designate=True)
    elif attack == "current_parsed_copy":
        path = tmp_path / "current-copy.json"
        path.write_text(json.dumps({"evidence": _evidence(observed_at=datetime(2026, 8, 31, 1, 2, tzinfo=timezone.utc)).to_dict()}))
        args += ["--current-receipt", str(path)]
    else:
        index = args.index("--current-source-archive")
        del args[index:index + 2]
    calls = []
    monkeypatch.setattr(verifier, "read_console_confirmation", lambda **_k: calls.append(1))
    with pytest.raises(SystemExit):
        verifier.main(args)
    assert not calls


@pytest.mark.parametrize("mutation", ["account_scope_sha256", "strategy_profile", "observed_at"])
def test_verifier_authenticated_current_receipt_still_needs_existing_activation_contract(tmp_path, monkeypatch, capsys, mutation):
    from scripts import verify_reconciliation_recovery as verifier
    from tests.test_reconciliation_baseline_candidate_script import _archive_inputs
    start, _, _, candidate, _, args = _raw_verifier_case(tmp_path)
    if mutation == "observed_at":
        evidence = _evidence(observed_at=start)  # A valid archive predating human confirmation.
    elif mutation == "account_scope_sha256":
        # Build a fresh valid evidence under a different physical-account digest.
        from quant_platform_kit.common.broker_reconciliation import BrokerReconciliationEvidence, calculate_broker_reconciliation_evidence_sha256
        value = _evidence(observed_at=start + timedelta(minutes=2)).to_dict()
        value[mutation] = "1" * 64
        value["evidence_sha256"] = calculate_broker_reconciliation_evidence_sha256(value)
        evidence = BrokerReconciliationEvidence.from_dict(value)
    else:
        from quant_platform_kit.common.broker_reconciliation import BrokerReconciliationEvidence, calculate_broker_reconciliation_evidence_sha256
        value = _evidence(observed_at=start + timedelta(minutes=2)).to_dict()
        value[mutation] = "other-profile"
        value["evidence_sha256"] = calculate_broker_reconciliation_evidence_sha256(value)
        evidence = BrokerReconciliationEvidence.from_dict(value)
    wrong = _archive_inputs(tmp_path / "other", [evidence])
    for flag, value in (("--current-source-archive", wrong["archives"][0]),
                        ("--current-source-records", wrong["records_path"]),
                        ("--current-source-expectations", wrong["expectations_path"])):
        args[args.index(flag) + 1] = str(value)
    response = _console_response(candidate["candidate"]["candidate_sha256"], confirmed_at=start + timedelta(minutes=1))
    response["recovery"]["evidence_sample_count"] = 1
    monkeypatch.setattr(verifier, "read_console_confirmation", lambda **_k: response)
    assert verifier.main(args) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["ready_for_atomic_state_transition"] is False
    assert result["transition_plan"] is None
    assert result["policy"]["state_write_attempted"] is False


def test_trusted_current_source_uses_protected_binding_and_exact_archive(tmp_path, monkeypatch):
    import pytest
    from scripts import diagnose_ibkr_flex_runtime_binding as binding
    from scripts import verify_reconciliation_recovery as verifier
    _, _, current, _, _, _ = _raw_verifier_case(tmp_path)
    monkeypatch.setattr(binding, "configuration", lambda _env: ({}, {"soxl_soxx_trend_income": ("synthetic-service", {})}, "project", "region"))
    def observe(*_args, private_output, **_kwargs):
        private_output.update(record=current["records"][0], archive=current["archives"][0].read_bytes(), runtime_target=_runtime_target())
        return {"status": "matched_at_observation"}
    monkeypatch.setattr(binding, "observe", observe)
    receipt, target = verifier.read_trusted_current_receipt({"RECONCILIATION_RUN_ID": "123"})
    assert receipt == current["payloads"][0] and target == _runtime_target()
    current["archives"][0].write_bytes(b"invalid")
    with pytest.raises(Exception):
        verifier.read_trusted_current_receipt({"RECONCILIATION_RUN_ID": "123"})


def test_trusted_current_cli_is_consumed_by_existing_recovery_verifier(tmp_path, monkeypatch, capsys):
    from scripts import verify_reconciliation_recovery as verifier
    start, _, current, candidate, _, args = _raw_verifier_case(tmp_path)
    for flag in ("--current-source-archive", "--current-source-records", "--current-source-expectations", "--runtime-target"):
        index = args.index(flag)
        del args[index:index + 2]
    index = args.index("--now")
    del args[index:index + 2]
    args += ["--current-trusted-run-id", "123"]
    monkeypatch.setattr(verifier, "_parse_time", lambda _: start + timedelta(minutes=3))
    monkeypatch.setattr(verifier, "read_trusted_current_receipt", lambda _env: (current["payloads"][0], _runtime_target()))
    response = _console_response(candidate["candidate"]["candidate_sha256"], confirmed_at=start + timedelta(minutes=1))
    response["recovery"]["evidence_sample_count"] = 1
    monkeypatch.setattr(verifier, "read_console_confirmation", lambda **_kwargs: response)
    assert verifier.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ready_for_atomic_state_transition"] is True
    assert result["policy"]["state_write_attempted"] is False


def test_trusted_current_mode_rejects_fixed_clock(tmp_path, monkeypatch):
    import pytest
    from scripts import verify_reconciliation_recovery as verifier
    _, _, _, _, _, args = _raw_verifier_case(tmp_path)
    for flag in ("--current-source-archive", "--current-source-records", "--current-source-expectations", "--runtime-target"):
        index = args.index(flag)
        del args[index:index + 2]
    args += ["--current-trusted-run-id", "123"]
    calls = []
    monkeypatch.setattr(verifier, "read_trusted_current_receipt", lambda _env: calls.append(True))
    with pytest.raises(SystemExit):
        verifier.main(args)
    assert not calls


def test_trusted_source_preflight_emits_no_receipt_or_target(monkeypatch, capsys):
    from scripts import verify_reconciliation_recovery as verifier
    private_receipt = {"evidence": _evidence(observed_at=datetime(2026, 8, 31, 1, 2, tzinfo=timezone.utc)).to_dict(), "private_marker": "must_not_appear"}
    target = {**_runtime_target(), "private_marker": "must_not_appear"}
    monkeypatch.setattr(verifier, "read_trusted_current_receipt", lambda _env: (private_receipt, target))
    assert verifier.main(["--trusted-current-preflight"]) == 0
    output = capsys.readouterr().out
    assert "must_not_appear" not in output
    result = json.loads(output)
    assert result["receipt_fresh"] is False
    assert result["state_write_attempted"] is False and result["recovery_verified"] is False
