"""Synthetic fixtures only — no real account numbers or live broker data."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.project_digest_candidates import (  # noqa: E402
    PLATFORM_ID,
    main,
    project_digest_candidates,
)


def _report(
    *,
    activity_outcome: str | None = "no_signal",
    status: str = "ok",
    strategy: str = "global_etf_rotation",
    started_at: str = "2026-10-08T20:01:00Z",
    include_strategy: bool = True,
):
    body: dict = {
        "schema_version": "runtime_report.v1",
        "platform": "interactive_brokers",
        "service_name": "ibkr-synthetic-service",
        "account_scope": "live-synthetic",
        "status": status,
        "started_at": started_at,
        "finished_at": "2026-10-08T20:02:00Z",
        "runtime_target": {
            "deployment_selector": "live-synthetic",
            "account_selector": ["U00000001"],
        },
        "summary": {},
    }
    if include_strategy:
        body["strategy_profile"] = strategy
        body["runtime_target"]["strategy_profile"] = strategy
    if activity_outcome is not None:
        body["execution_receipt"] = {"outcome": activity_outcome}
    return body


def _facts(*, net_assets: str = "12345.67"):
    return {
        "schema_version": "ibkr_account_snapshot_history.v1",
        "broker_reported_balances": [
            {
                "currency": "USD",
                "net_assets": net_assets,
            }
        ],
    }


def test_unknown_fills_stay_null_not_zero():
    payload = project_digest_candidates(
        runtime_reports=_report(),
        opaque_account_uid="acct_opaque_synthetic",
        target_id="ibkr/synthetic-target",
    )
    assert payload["producer_status"] == "projected"
    assert len(payload["runs"]) == 1
    row = payload["runs"][0]
    assert row["platform_id"] == PLATFORM_ID
    assert row["actually_ran"] is True
    assert row["fill_count"] is None
    assert row["order_count"] is None
    assert row["field_status"]["fill_count"] == "counts_unknown"
    assert row["field_status"]["order_count"] == "counts_unknown"
    assert "ibkr_fills_not_projected" in row["reason_code"]
    assert row["cycle_count"] == 1
    assert row["opaque_account_uid"] == "acct_opaque_synthetic"
    assert row["target_id"] == "ibkr/synthetic-target"
    assert row["signal_summary"] == "no_signal"
    assert row["rebalance_kind"] == "no_rebalance"
    assert "equity" not in row
    assert row["strategy_profile"] == "global_etf_rotation"


def test_summary_order_counts_are_not_promoted_to_known_zero():
    report = _report(activity_outcome="no_action")
    report["summary"] = {
        "orders_submitted_count": 0,
        "orders_filled_count": 0,
    }
    payload = project_digest_candidates(
        runtime_reports=report,
        opaque_account_uid="acct_opaque_synthetic",
        target_id="ibkr/synthetic-target",
    )
    row = payload["runs"][0]
    assert row["fill_count"] is None
    assert row["order_count"] is None
    assert row["field_status"]["fill_count"] == "counts_unknown"


def test_missing_identity_marked_not_invented():
    payload = project_digest_candidates(
        runtime_reports=_report(activity_outcome="filled")
    )
    row = payload["runs"][0]
    assert row["opaque_account_uid"] == ""
    assert row["target_id"] == ""
    assert "opaque_account_uid_absent" in row["reason_code"]
    assert "target_id_absent" in row["reason_code"]
    assert row["rebalance_kind"] == "rebalance"
    assert row["status"] == "ok"


def test_equity_only_from_account_facts_never_guessed():
    payload = project_digest_candidates(
        runtime_reports=_report(activity_outcome="no_rebalance"),
        opaque_account_uid="acct_opaque_synthetic",
        target_id="ibkr/synthetic-target",
        account_facts=_facts(),
    )
    row = payload["runs"][0]
    assert row["equity"] == pytest.approx(12345.67)
    assert row["equity_currency"] == "USD"


def test_invalid_equity_text_omitted():
    payload = project_digest_candidates(
        runtime_reports=_report(),
        opaque_account_uid="acct_opaque_synthetic",
        target_id="ibkr/synthetic-target",
        account_facts=_facts(net_assets="not-a-number"),
    )
    assert "equity" not in payload["runs"][0]
    assert "account_facts_equity_absent" in payload["runs"][0]["reason_code"]


def test_account_facts_only_report_without_cycle_yields_empty():
    report = _report(activity_outcome=None, status="")
    report.pop("status", None)
    report["summary"] = {
        "account_facts": {
            "schema_version": "ibkr_account_snapshot.v1",
            "net_assets": "1.00",
            "currency": "USD",
            "observed_at": "2026-10-08T20:01:30Z",
        }
    }
    payload = project_digest_candidates(
        runtime_reports=report,
        opaque_account_uid="acct_opaque_synthetic",
        target_id="ibkr/synthetic-target",
    )
    assert payload["runs"] == []
    assert payload["producer_status"] == "empty"


def test_alert_status_for_failed_activity():
    payload = project_digest_candidates(
        runtime_reports=_report(activity_outcome="failed", status="failed"),
        opaque_account_uid="acct_opaque_synthetic",
        target_id="ibkr/synthetic-target",
    )
    assert payload["runs"][0]["status"] == "alert"


def test_cli_writes_safe_summary(tmp_path, capsys):
    report_path = tmp_path / "report.json"
    facts_path = tmp_path / "facts.json"
    out_path = tmp_path / "candidates.json"
    report_path.write_text(json.dumps(_report()), encoding="utf-8")
    facts_path.write_text(json.dumps(_facts()), encoding="utf-8")
    code = main(
        [
            "--runtime-report",
            str(report_path),
            "--account-facts",
            str(facts_path),
            "--opaque-account-uid",
            "acct_opaque_synthetic",
            "--target-id",
            "ibkr/synthetic-target",
            "--output",
            str(out_path),
        ]
    )
    assert code == 0
    summary = json.loads(capsys.readouterr().out.strip())
    assert summary["status"] == "projected"
    assert summary["runs"] == 1
    assert "12345" not in json.dumps(summary)
    assert "acct_opaque" not in json.dumps(summary)
    dumped = json.loads(out_path.read_text(encoding="utf-8"))
    assert dumped["runs"][0]["fill_count"] is None
    assert dumped["runs"][0]["platform_id"] == "ibkr"
