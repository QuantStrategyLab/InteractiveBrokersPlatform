from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from quant_platform_kit.common.broker_reconciliation import build_broker_reconciliation_evidence
from quant_platform_kit.common.runtime_reports import persist_runtime_report

from application.reconciliation_reporting import build_persistable_reconciliation_report


def _digest(character: str) -> str:
    return character * 64


def _safe_candidate() -> dict[str, object]:
    evidence = build_broker_reconciliation_evidence(
        platform_id="ibkr",
        strategy_profile="global_etf_rotation",
        account_scope_sha256=_digest("a"),
        baseline_id="baseline-001",
        baseline_target_sha256=_digest("b"),
        runtime_target_sha256=_digest("b"),
        observed_at=datetime(2026, 9, 4, tzinfo=timezone.utc),
        broker_connected=True,
        account_identity_match=False,
        positions_match=False,
        cash_match=False,
        open_orders_match=False,
        recent_executions_match=False,
        local_execution_ledger_match=False,
        positions_sha256=_digest("c"),
        cash_sha256=_digest("d"),
        open_orders_sha256=_digest("e"),
        recent_executions_sha256=_digest("f"),
        local_execution_ledger_sha256=_digest("0"),
    ).to_dict()
    return {
        "schema_version": "ibkr_reconciliation_candidate.v1",
        "permits_active_lkg": False,
        "expected_digests_configured": False,
        "execution_ledger_records_count": 0,
        "recovery_blockers": ["missing_expected_digests"],
        "evidence": evidence,
    }


def test_persistable_reconciliation_report_keeps_only_safe_evidence(tmp_path) -> None:
    marker = "gateway.example.internal:4999 client_id=72 account=demo-account /private/config.json"
    candidate = _safe_candidate()
    report = {
        "schema_version": "runtime_report.v1",
        "platform": "ibkr",
        "deploy_target": "cloud_run",
        "service_name": "private-service",
        "strategy_profile": "global_etf_rotation",
        "runtime_target": {"account_selector": ["demo-account"]},
        "account_scope": "demo-account",
        "account_group": "demo-account",
        "project_id": "private-project",
        "instance_name": marker,
        "account_ids": ["demo-account"],
        "run_id": "run-001",
        "run_source": "cloud_run",
        "status": "error",
        "dry_run": True,
        "started_at": "2026-09-04T00:00:00Z",
        "finished_at": "2026-09-04T00:01:00Z",
        "summary": {
            "broker_reconciliation_permits_active_lkg": False,
            "broker_reconciliation_blockers_count": 1,
            "broker_reconciliation_ledger_records_count": 0,
            "account_ids": ["demo-account"],
        },
        "diagnostics": {
            "broker_reconciliation": candidate,
            "reconciliation_request_id": "853a2e08-9396-4fe8-89ee-59fb17e40a1d",
            "reconciliation_scheduler_job_sha256": "a" * 64,
            "ib_gateway_host": marker,
        },
        "artifacts": {"strategy_config_path": marker},
        "errors": [
            {
                "stage": "broker_reconciliation",
                "message": marker,
                "error_type": "TimeoutError",
                "failure_category": "broker_reconciliation",
            }
        ],
    }

    sanitized = build_persistable_reconciliation_report(report)
    result = persist_runtime_report(sanitized, base_dir=tmp_path)
    payload = json.loads((tmp_path / "ibkr/global_etf_rotation/2026-09/run-001.json").read_text())

    assert result.local_path is not None
    assert payload["diagnostics"]["broker_reconciliation"] == candidate
    assert payload["diagnostics"]["reconciliation_request_id"] == "853a2e08-9396-4fe8-89ee-59fb17e40a1d"
    assert payload["diagnostics"]["reconciliation_scheduler_job_sha256"] == "a" * 64
    assert payload["errors"] == [
        {
            "stage": "broker_reconciliation",
            "error_type": "TimeoutError",
            "failure_category": "broker_reconciliation",
        }
    ]
    assert payload["summary"] == {
        "broker_reconciliation_permits_active_lkg": False,
        "broker_reconciliation_blockers_count": 1,
        "broker_reconciliation_ledger_records_count": 0,
    }
    serialized = json.dumps(payload, sort_keys=True)
    assert marker not in serialized
    assert "demo-account" not in serialized
    assert "runtime_target" not in payload
    assert "account_scope" not in payload
    assert "service_name" not in payload


def test_persistable_reconciliation_report_rejects_untrusted_request_id() -> None:
    report = {
        "schema_version": "runtime_report.v1",
        "platform": "ibkr",
        "deploy_target": "cloud_run",
        "strategy_profile": "global_etf_rotation",
        "run_id": "run-001",
        "run_source": "cloud_run",
        "status": "ok",
        "started_at": "2026-09-04T00:00:00Z",
        "finished_at": "2026-09-04T00:01:00Z",
        "summary": {},
        "diagnostics": {"reconciliation_request_id": "unsafe value\nreport_uri=gs://x"},
        "artifacts": {},
        "errors": [],
    }

    sanitized = build_persistable_reconciliation_report(report)

    assert "reconciliation_request_id" not in sanitized["diagnostics"]


@pytest.mark.parametrize("shape", ["empty", "null", "missing"])
@pytest.mark.parametrize("source_complete", [True, False, None])
def test_persistent_pin_main_raw_flow_roundtrip_preserves_unknowns(
    strategy_module, monkeypatch, tmp_path, shape, source_complete,
):
    import quant_platform_kit.cloud as cloud
    from quant_platform_kit.common import runtime_reports

    module = strategy_module
    assert module.persist_runtime_report is runtime_reports.persist_runtime_report
    phases, local_payloads, cloud_payloads = [], [], []
    original_write = runtime_reports.write_runtime_report_json

    def local_write(report, *, output_path):
        path = original_write(report, output_path=output_path)
        phases.append("local")
        local_payloads.append(json.loads(path.read_text()))
        return path

    def cloud_write(uri, data, **kwargs):
        assert uri.startswith("gs://synthetic-only/")
        phases.append("cloud")
        cloud_payloads.append(json.loads(data))

    monkeypatch.setattr(runtime_reports, "write_runtime_report_json", local_write)
    monkeypatch.setattr(cloud, "get_object_store", lambda **kwargs: SimpleNamespace(write_text=cloud_write))
    flows = {"source_complete": source_complete}
    if shape != "missing":
        flows["transactions"] = [] if shape == "empty" else None
    report = {
        "schema_version": "runtime_report.v1", "platform": "ibkr",
        "strategy_profile": "global_etf_rotation", "run_id": "synthetic-only",
        "started_at": "2026-10-20T00:00:00Z", "account_external_flows": flows,
    }
    path = tmp_path / "report.json"
    module.persist_runtime_report(report, output_path=path, cloud_prefix_uri="gs://synthetic-only/reports")
    assert phases == ["local", "cloud", "local"]
    assert len(local_payloads) == 2 and len(cloud_payloads) == 1
    for payload in [*local_payloads, *cloud_payloads, json.loads(path.read_text())]:
        actual = payload["account_external_flows"]
        if shape == "empty":
            assert actual["transactions"] == []
        else:
            assert actual.get("transactions") is None
        assert actual.get("source_complete") is source_complete
    assert "runtime_report_cloud_uri" not in local_payloads[0]["artifacts"]
    assert local_payloads[-1]["artifacts"]["runtime_report_cloud_uri"].startswith("gs://synthetic-only/")


def test_log_comparison_distinguishes_coverage_from_digest_flags():
    from types import SimpleNamespace
    from application.reconciliation_reporting import reconciliation_log_comparison

    fields = ('positions_match', 'cash_match', 'open_orders_match',
              'recent_executions_match', 'local_execution_ledger_match')

    def candidate(configured, values, blockers, covered=()):
        return SimpleNamespace(expected_digests_configured=configured,
                               comparison_covered=frozenset(covered),
                               evidence=SimpleNamespace(**dict(zip(fields, values, strict=True))),
                               recovery_blockers=[SimpleNamespace(value=value) for value in blockers])

    cash = 'broker_reconciliation_cash_mismatch'
    orders = 'broker_reconciliation_open_orders_mismatch'
    baseline = 'broker_reconciliation_baseline_target_mismatch'
    overall, blockers, states = reconciliation_log_comparison(candidate(False, [False] * 5, [cash, baseline]))
    assert overall == 'not_configured' and blockers == [baseline]
    assert set(states.values()) == {'not_configured'}
    overall, blockers, states = reconciliation_log_comparison(candidate(
        True, [True, False, True, True, True], [cash, baseline], covered={'open_orders'}))
    assert overall == 'not_compared' and blockers == [baseline]
    assert states['cash'] == 'not_compared' and states['open_orders'] == 'matched'
    overall, blockers, states = reconciliation_log_comparison(candidate(
        True, [False, False, False, False, False], [cash, orders], covered={'open_orders'}))
    assert overall == 'mismatched' and blockers == [orders]
    assert states['cash'] == 'not_compared' and states['open_orders'] == 'mismatched'
    all_covered = {field.removesuffix('_match') for field in fields}
    assert reconciliation_log_comparison(candidate(True, [True] * 5, [], all_covered))[0] == 'matched'
    assert reconciliation_log_comparison(candidate(True, [True, None, True, True, True], [], all_covered))[0] == 'not_compared'
