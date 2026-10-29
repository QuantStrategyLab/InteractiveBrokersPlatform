from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest

from quant_platform_kit.common.broker_reconciliation import build_broker_reconciliation_evidence
from scripts.build_reconciliation_baseline_candidate import evaluate_receipts
from tests.test_reconciliation_baseline_candidate_script import _sources_for_evidences
from tests.test_reconciliation_recovery_verify_only import _candidate_source_inputs
from scripts.publish_reconciliation_recovery_source import (
    archive_private_evidence_package,
    build_trusted_initial_snapshot,
    build_private_evidence_package,
    build_recovery_source_snapshot,
    publish_recovery_source_snapshot,
)


def _digest(character: str) -> str:
    return character * 64


def test_trusted_initial_source_binds_private_facts_and_current_target(monkeypatch):
    from tests.test_reconciliation_baseline_candidate_script import _payload
    import scripts.diagnose_ibkr_flex_runtime_binding as binding
    import quant_platform_kit.common.live_continuity as continuity

    now = datetime.now(timezone.utc).replace(microsecond=0)
    account_digest = hashlib.sha256(b'{"account_ids":["U123"]}').hexdigest()
    receipt = _payload(observed_at=now, account_scope_sha256=account_digest)
    job_hash = hashlib.sha256(b"ibkr-reconcile-soxl_soxx_trend_income-1-1").hexdigest()
    request_id = "853a2e08-9396-4fe8-89ee-59fb17e40a1d"
    uri = f"gs://synthetic-private/reports/initial-enrollment/{request_id}.json#123"
    run = {"id": 1, "status": "completed", "conclusion": "success", "head_branch": "main", "run_attempt": 1,
           "event": "workflow_dispatch", "path": ".github/workflows/collect-reconciliation-evidence.yml",
           "repository": {"full_name": "QuantStrategyLab/InteractiveBrokersPlatform"}, "head_sha": "c" * 40}
    scope = {"kind": "synthetic", "currency": None, "window_start": None, "window_end": None,
             "limit_per_namespace": None, "gateway_availability": None, "truncated": None}
    facts = {"schema_version": "ibkr_initial_facts.v1", "account_scope_sha256": account_digest,
             "observed_at": now.isoformat().replace("+00:00", "Z"), "surfaces": {
                 name: {"status": "unknown", "items": None, "scope": scope} for name in
                 ("positions", "cash", "open_orders", "recent_executions", "local_execution_ledger")}}
    facts_sha = hashlib.sha256(json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    report = {"diagnostics": {"broker_reconciliation": receipt,
                              "reconciliation_scheduler_job_sha256": job_hash, "reconciliation_request_id": request_id,
                              "initial_enrollment_private": {"facts": facts, "facts_sha256": facts_sha}}}
    monkeypatch.setattr(binding, "configuration", lambda _env: ({}, {"soxl_soxx_trend_income": ("synthetic", {})}, "project", "region"))
    def observe(*_args, private_output, **_kwargs):
        private_output.update(target_id="synthetic-target", runtime_target={"live_continuity": {}},
                              account_selector=("U123",), service_revision="synthetic-revision")
        return {"status": "initial_metadata_verified"}
    monkeypatch.setattr(binding, "observe", observe)
    monkeypatch.setattr(continuity, "build_live_continuity", lambda _value: SimpleNamespace(
        state="RECONCILE_ONLY", baseline_id="ibkr-soxl-lkg-20260830", assert_matches_target=lambda _target: None))
    monkeypatch.setattr(continuity, "runtime_target_fingerprint", lambda _target: _digest("b"))
    result = build_trusted_initial_snapshot({}, "1", read_report=lambda _hash: ([uri], report), read_run=lambda _id: json.dumps(run))
    item = result["recoveries"][0]
    assert item["candidate"]["mode"] == "initial_enrollment"
    assert item["facts"] == facts
    with pytest.raises(ValueError, match="generation unavailable"):
        build_trusted_initial_snapshot({}, "1", read_report=lambda _hash: ([uri.split("#")[0]], report), read_run=lambda _id: json.dumps(run))
    report["diagnostics"]["initial_enrollment_private"]["facts_sha256"] = _digest("f")
    with pytest.raises(ValueError, match="facts digest mismatch"):
        build_trusted_initial_snapshot({}, "1", read_report=lambda _hash: ([uri], report), read_run=lambda _id: json.dumps(run))


def test_trusted_initial_cli_never_prints_private_facts(monkeypatch, capsys):
    import scripts.publish_reconciliation_recovery_source as publisher
    sentinel = "synthetic-private-fact"
    monkeypatch.setattr(publisher, "build_trusted_initial_snapshot", lambda _env, _run: {"facts": sentinel})
    seen = []
    monkeypatch.setattr(publisher, "publish_recovery_source_snapshot", lambda snapshot, **_kwargs: seen.append(snapshot) or {"ok": True})
    assert publisher.main(["--trusted-initial-run", "1", "--publish-url",
                           "https://synthetic.example/api/internal/sync-reconciliation-recovery-source"]) == 0
    assert seen == [{"facts": sentinel}]
    assert sentinel not in capsys.readouterr().out


def _candidate_payload(start: datetime) -> dict[str, object]:
    base = {
        "platform_id": "ibkr",
        "strategy_profile": "soxl_soxx_trend_income",
        "account_scope_sha256": _digest("a"),
        "baseline_id": "soxl-ibkr-lkg-20260830",
        "baseline_target_sha256": _digest("b"),
        "runtime_target_sha256": _digest("b"),
        "broker_connected": True,
        "account_identity_match": True,
        "positions_match": True,
        "cash_match": True,
        "open_orders_match": True,
        "recent_executions_match": True,
        "local_execution_ledger_match": True,
        "positions_sha256": _digest("c"),
        "cash_sha256": _digest("d"),
        "open_orders_sha256": _digest("e"),
        "recent_executions_sha256": _digest("f"),
        "local_execution_ledger_sha256": _digest("0"),
    }
    observations = [
        build_broker_reconciliation_evidence(**base, observed_at=start),
        build_broker_reconciliation_evidence(**base, observed_at=start + timedelta(minutes=2)),
    ]
    return evaluate_receipts(
        [{"evidence": item.to_dict()} for item in observations], now=start + timedelta(minutes=3),
        **_sources_for_evidences(observations),
    )


def _dual_review(candidate_sha256: str, **overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
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
    result.update(overrides)
    return result


def test_builds_redacted_source_only_from_bound_mandatory_dual_review() -> None:
    start = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    candidate = _candidate_payload(start)
    candidate_value = candidate["candidate"]
    assert isinstance(candidate_value, dict)
    snapshot = build_recovery_source_snapshot(
        candidate_payload=candidate,
        **_candidate_source_inputs(start),
        dual_review_payload=_dual_review(str(candidate_value["candidate_sha256"])),
        recovery_id="ibkr-soxl-live-recovery",
        now=start + timedelta(minutes=3),
    )

    recoveries = snapshot["recoveries"]
    assert isinstance(recoveries, list)
    record = recoveries[0]
    assert snapshot["schema_version"] == "qsl_reconciliation_recovery_source_snapshot.v1"
    assert record["readiness"] == "awaiting_human_confirmation"
    assert record["dual_review"] == {
        "outcome": "approved",
        "reviewer_count": 3,
        "evidence_binding_sha256": candidate_value["candidate_sha256"],
    }
    assert "positions_sha256" not in record
    assert "account_scope_sha256" not in record


def test_rejects_audit_receipt_without_human_recovery_boundary() -> None:
    start = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    candidate = _candidate_payload(start)
    candidate_value = candidate["candidate"]
    assert isinstance(candidate_value, dict)
    review = _dual_review(str(candidate_value["candidate_sha256"]), requires_human_recovery_approval=False)

    with pytest.raises(ValueError, match="human recovery approval"):
        build_recovery_source_snapshot(
            candidate_payload=candidate,
        **_candidate_source_inputs(start),
            dual_review_payload=review,
            recovery_id="ibkr-soxl-live-recovery",
            now=start + timedelta(minutes=3),
        )


def test_publish_uses_only_recovery_ingress_and_bearer_token() -> None:
    received: dict[str, object] = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, object]:
            return {"ok": True, "source_id": "ibkr.reconciliation_recovery"}

    def fake_post(*args: object, **kwargs: object) -> FakeResponse:
        received["args"] = args
        received["kwargs"] = kwargs
        return FakeResponse()

    result = publish_recovery_source_snapshot(
        {"schema_version": "qsl_reconciliation_recovery_source_snapshot.v1"},
        publish_url="https://console.example/api/internal/sync-reconciliation-recovery-source",
        token="dedicated-token",
        post=fake_post,
    )

    assert result["ok"] is True
    kwargs = received["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["headers"] == {"Authorization": "Bearer dedicated-token",
                                 "User-Agent": "QSL-IBKR-Reconciliation/1.0"}


def test_publish_rejects_non_recovery_endpoint() -> None:
    with pytest.raises(ValueError, match="reconciliation recovery source ingress"):
        publish_recovery_source_snapshot(
            {},
            publish_url="https://console.example/api/manual-strategy-switch",
            token="dedicated-token",
        )


def test_urllib_publish_sends_service_identity_without_redirects(monkeypatch):
    from scripts import publish_reconciliation_recovery_source as publisher
    seen = {}
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def read(self): return b'{"ok":true}'
    def open_request(request, timeout):
        seen['request'] = request
        assert timeout == 15
        return Response()
    def opener(handler):
        assert handler is publisher._NoRedirect
        return SimpleNamespace(open=open_request)
    monkeypatch.setattr(publisher, 'build_opener', opener)
    assert publish_recovery_source_snapshot({}, publish_url='https://console.example/api/internal/sync-reconciliation-recovery-source', token='dedicated')['ok']
    assert seen['request'].get_header('User-agent') == 'QSL-IBKR-Reconciliation/1.0'


def test_private_evidence_archive_is_create_only_and_never_uses_console_shape() -> None:
    start = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    candidate = _candidate_payload(start)
    candidate_value = candidate["candidate"]
    assert isinstance(candidate_value, dict)
    review = _dual_review(str(candidate_value["candidate_sha256"]))
    snapshot = build_recovery_source_snapshot(
        candidate_payload=candidate,
        **_candidate_source_inputs(start),
        dual_review_payload=review,
        recovery_id="ibkr-soxl-live-recovery",
        now=start + timedelta(minutes=3),
    )
    package = build_private_evidence_package(
        snapshot=snapshot,
        candidate_payload=candidate,
        **_candidate_source_inputs(start),
        dual_review_payload=review,
    )
    observed: dict[str, object] = {}

    class Blob:
        def upload_from_string(self, value: str, **kwargs: object) -> None:
            observed["value"] = value
            observed["kwargs"] = kwargs

    class Bucket:
        @staticmethod
        def blob(name: str) -> Blob:
            observed["object_name"] = name
            return Blob()

    class Client:
        @staticmethod
        def bucket(name: str) -> Bucket:
            observed["bucket_name"] = name
            return Bucket()

    result = archive_private_evidence_package(
        package,
        private_evidence_uri="gs://private-bucket/reconciliation-recovery/ibkr/source/ibkr-soxl-live-recovery/evidence.json",
        storage_client_factory=Client,
    )

    assert result["uri"].startswith("gs://private-bucket/reconciliation-recovery/ibkr/source/")
    assert observed["kwargs"] == {"content_type": "application/json", "if_generation_match": 0}
    assert '"baseline_candidate"' in observed["value"]  # type: ignore[operator]


@pytest.mark.parametrize("outcome", ["fail", "review_unavailable", "pass"])
def test_source_publisher_preserves_advisory_outcome_without_two_reviewers(outcome):
    start = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    candidate = _candidate_payload(start)
    review = _dual_review(candidate["candidate"]["candidate_sha256"], outcome=outcome, escalated=False, secondary_review={})
    snapshot = build_recovery_source_snapshot(
        candidate_payload=candidate, **_candidate_source_inputs(start), dual_review_payload=review,
        recovery_id="ibkr-soxl-live-recovery", now=start + timedelta(minutes=3),
    )
    record = snapshot["recoveries"][0]
    assert record["readiness"] == "awaiting_human_confirmation"
    assert record["dual_review"]["outcome"] == {"fail": "rejected", "review_unavailable": "unavailable", "pass": "approved"}[outcome]
    assert record["dual_review"]["reviewer_count"] == 1


@pytest.mark.parametrize("consumer", ["publisher", "verifier"])
@pytest.mark.parametrize("fault", ["source_changed", "expectation_changed", "member_substitution", "legacy_v1", "review_binding", "missing_source"])
def test_both_consumers_revalidate_sources_and_candidate_members(consumer, fault):
    from dataclasses import replace
    from application.broker_reconciliation_candidate import calculate_source_receipts_sha256
    from quant_platform_kit.common.broker_reconciliation_enrollment import calculate_broker_reconciliation_baseline_candidate_sha256
    from scripts.verify_reconciliation_recovery import verify_reconciliation_recovery
    from tests.test_reconciliation_recovery_verify_only import _console_response, _evidence, _runtime_target

    start = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    candidate = _candidate_payload(start)
    sources = _candidate_source_inputs(start)
    review = None
    if fault == "source_changed":
        sources["source_receipt_records"][0]["service_revision"] = "unapproved-revision"
    elif fault == "expectation_changed":
        sources["expectations"] = (replace(sources["expectations"][0], workflow_head_sha="f" * 40), *sources["expectations"][1:])
    elif fault == "member_substitution":
        sources["source_receipt_records"][0]["evidence_sha256"] = "9" * 64
        sources["expectations"] = (replace(sources["expectations"][0], evidence_sha256="9" * 64), *sources["expectations"][1:])
        value = candidate["candidate"]
        value["source_receipts_sha256"] = calculate_source_receipts_sha256(sources["source_receipt_records"], strategy_profile=value["strategy_profile"], expectations=sources["expectations"])
        value["candidate_sha256"] = calculate_broker_reconciliation_baseline_candidate_sha256(value)
    elif fault == "legacy_v1":
        value = candidate["candidate"]
        value["schema_version"] = "broker_reconciliation_baseline_candidate.v1"
        del value["source_receipts_sha256"]
        value["candidate_sha256"] = calculate_broker_reconciliation_baseline_candidate_sha256(value)
    elif fault == "review_binding":
        review = _dual_review("9" * 64)
    elif fault == "missing_source":
        sources = {"source_receipt_records": [], "expectations": ()}
    with pytest.raises(ValueError):
        if consumer == "publisher":
            build_recovery_source_snapshot(candidate_payload=candidate, **sources, dual_review_payload=review,
                                           recovery_id="ibkr-soxl-live-recovery", now=start + timedelta(minutes=3))
        else:
            verify_reconciliation_recovery(
                candidate_payload=candidate, **sources, dual_review_payload=review,
                confirmation_payload=_console_response(candidate["candidate"]["candidate_sha256"], confirmed_at=start + timedelta(minutes=3)),
                current_receipt_payload={"evidence": _evidence(observed_at=start + timedelta(minutes=4)).to_dict()},
                runtime_target_payload=_runtime_target(), recovery_id="ibkr-soxl-live-recovery", now=start + timedelta(minutes=5),
            )


@pytest.mark.parametrize("attack", ["archive_bytes", "rehashed_candidate", "parsed_only"])
def test_publisher_cli_authenticates_before_archive_or_publish(tmp_path, monkeypatch, capsys, attack):
    import json
    from scripts import build_reconciliation_baseline_candidate as builder
    from scripts import publish_reconciliation_recovery_source as publisher
    from tests.test_reconciliation_baseline_candidate_script import _archive_inputs, _healthy_evidence
    from quant_platform_kit.common.broker_reconciliation_enrollment import calculate_broker_reconciliation_baseline_candidate_sha256
    material = _archive_inputs(tmp_path, [_healthy_evidence()])
    assert builder.main(["--now", "2026-08-30T08:01:00Z", *material["args"]]) == 0
    candidate = json.loads(capsys.readouterr().out)
    if attack == "rehashed_candidate":
        candidate["candidate"]["account_scope_sha256"] = "1" * 64
        candidate["candidate"]["candidate_sha256"] = calculate_broker_reconciliation_baseline_candidate_sha256(candidate["candidate"])
        # The old content-root check alone accepts this internally consistent
        # candidate; the CLI must bind it to the authenticated raw observations.
        publisher.extract_baseline_candidate(candidate, source_receipt_records=material["records"],
                                             expectations=material["expectations"])
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(candidate))
    if attack == "archive_bytes":
        material["archives"][0].write_bytes(b"tampered")
    calls = []
    monkeypatch.setattr(publisher, "archive_private_evidence_package", lambda *_a, **_k: calls.append("archive"))
    monkeypatch.setattr(publisher, "publish_recovery_source_snapshot", lambda *_a, **_k: calls.append("publish"))
    with pytest.raises(SystemExit) as rejected:
        publisher.main([
            "--candidate", str(path), "--recovery-id", "synthetic-recovery",
            "--now", "2026-08-30T08:01:00Z",
            "--publish-url", "https://synthetic.invalid/source", "--archive-gcs-uri", "gs://synthetic/source",
            *(material["args"][:4] if attack == "parsed_only" else material["args"]),
        ])
    assert rejected.value.code == 2
    assert calls == []
