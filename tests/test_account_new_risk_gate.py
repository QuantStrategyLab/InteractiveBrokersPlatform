"""Tests for IBKR account_new_risk_gate W1 wiring (fail-closed, no broker I/O)."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest import mock

import pytest
from quant_platform_kit.common.models import OrderIntent
from quant_platform_kit.risk.account_new_risk_gate import (
    InjectedReconciliationSnapshot,
    NewRiskDisposition,
)

from application.account_new_risk_gate_support import (
    ACCOUNT_NEW_RISK_GATE_ENV,
    apply_combined_scale_to_target_weights,
    build_account_new_risk_snapshot,
    build_portfolio_from_account_values,
    build_snapshot_from_portfolio,
    evaluate_account_values_new_risk_admission,
    evaluate_cycle_new_risk_admission,
    evaluate_portfolio_new_risk_admission,
    is_account_new_risk_gate_enabled,
    maybe_publish_attention_for_admission,
    new_risk_buy_prohibited,
    reset_attention_sent_keys_for_tests,
    set_cycle_snapshot,
)
from application.ibkr_order_execution import submit_order_intent


@pytest.fixture(autouse=True)
def _clear_cycle_snapshot():
    set_cycle_snapshot(None)
    reset_attention_sent_keys_for_tests()
    for key in (
        "IBKR_MAX_DAILY_LOSS_USD",
        "MAX_DAILY_LOSS_USD",
        "RUNTIME_TARGET_JSON",
    ):
        os.environ.pop(key, None)
    yield
    set_cycle_snapshot(None)
    for key in (
        "IBKR_MAX_DAILY_LOSS_USD",
        "MAX_DAILY_LOSS_USD",
        "RUNTIME_TARGET_JSON",
    ):
        os.environ.pop(key, None)


def test_gate_enabled_by_default():
    with mock.patch.dict(os.environ, {}, clear=True):
        assert is_account_new_risk_gate_enabled() is True


def test_gate_disabled_when_env_zero():
    with mock.patch.dict(os.environ, {ACCOUNT_NEW_RISK_GATE_ENV: "0"}, clear=False):
        assert is_account_new_risk_gate_enabled() is False


def test_missing_equity_prohibits_new_risk():
    # Missing equity keeps observation_ok=False, which still fails closed via
    # EQUITY_UNKNOWN_FAIL_CLOSED in the gate below -- the circuit-breaker axis
    # is not durably OPEN here since there is no explicit unknown-pending /
    # durable-breaker evidence this cycle.
    assert build_account_new_risk_snapshot({}) == {
        "observation_status": "UNAVAILABLE",
        "reconciliation_status": "UNVERIFIED",
        "circuit_breaker_state": "CLOSED",
        "equity_usd": None,
    }
    result = evaluate_account_values_new_risk_admission({"equity": 0.0})
    assert result.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
    assert "EQUITY_UNKNOWN_FAIL_CLOSED" in result.reason_codes
    assert result.live_authority_granted is False


def test_drawdown_brake_prohibits_new_risk():
    portfolio = {
        "total_equity": 80_000.0,
        "peak_equity_usd": 100_000.0,
    }
    result = evaluate_portfolio_new_risk_admission(portfolio)
    assert result.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
    assert result.live_authority_granted is False


def test_healthy_equity_without_explicit_snapshot_allows_new_risk():
    # A plain healthy-equity portfolio (no explicit snapshot, no drawdown)
    # now derives COMPLETE/VERIFIED/CLOSED via cycle-health instead of
    # failing closed on the old blanket UNAVAILABLE/UNVERIFIED/OPEN defaults.
    portfolio = {
        "total_equity": 100_000.0,
        "peak_equity_usd": 100_000.0,
    }
    with mock.patch(
        "application.account_new_risk_gate_support.resolve_production_drift_status_from_store",
        return_value=None,
    ):
        result = evaluate_portfolio_new_risk_admission(portfolio)
    assert result.disposition == NewRiskDisposition.ALLOW_NEW_RISK


def test_explicit_critical_production_drift_prohibits_new_risk():
    portfolio = {
        "total_equity": 100_000.0,
        "peak_equity_usd": 100_000.0,
        "production_drift_status": "critical",
    }
    result = evaluate_portfolio_new_risk_admission(portfolio)
    assert result.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
    assert "PRODUCTION_DRIFT_CRITICAL" in result.reason_codes


def test_store_critical_production_drift_prohibits_new_risk():
    portfolio = {
        "total_equity": 100_000.0,
        "peak_equity_usd": 100_000.0,
    }
    with mock.patch(
        "application.account_new_risk_gate_support.resolve_production_drift_status_from_store",
        return_value="critical",
    ) as store_mock:
        result = evaluate_portfolio_new_risk_admission(portfolio)
    store_mock.assert_called_once()
    assert result.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
    assert "PRODUCTION_DRIFT_CRITICAL" in result.reason_codes


def test_store_probe_failure_is_fail_soft_not_invented_ban():
    portfolio = {
        "total_equity": 100_000.0,
        "peak_equity_usd": 100_000.0,
    }
    with mock.patch(
        "application.account_new_risk_gate_support.resolve_production_drift_status_from_store",
        return_value=None,
    ):
        result = evaluate_portfolio_new_risk_admission(portfolio)
    assert result.disposition == NewRiskDisposition.ALLOW_NEW_RISK


def test_explicit_healthy_snapshot_allows_new_risk():
    portfolio = {
        "total_equity": 100_000.0,
        "peak_equity_usd": 100_000.0,
        "account_new_risk_snapshot": {
            "observation_status": "COMPLETE",
            "reconciliation_status": "VERIFIED",
            "circuit_breaker_state": "CLOSED",
        },
    }
    result = evaluate_portfolio_new_risk_admission(portfolio)
    assert result.disposition == NewRiskDisposition.ALLOW_NEW_RISK
    assert result.live_authority_granted is False


def test_explicit_daily_loss_at_limit_prohibits_buy():
    portfolio = {
        "total_equity": 50_000.0,
        "peak_equity_usd": 50_000.0,
        "account_new_risk_snapshot": {
            "daily_loss_usd": 100.0,
            "max_daily_loss_usd": 100.0,
        },
    }
    with mock.patch(
        "application.account_new_risk_gate_support.resolve_production_drift_status_from_store",
        return_value=None,
    ):
        snapshot = build_snapshot_from_portfolio(portfolio)
        result = evaluate_portfolio_new_risk_admission(portfolio)
    assert snapshot.daily_loss_usd == 100.0
    assert result.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
    assert "DAILY_LOSS_LIMIT_EXCEEDED" in result.reason_codes
    assert new_risk_buy_prohibited(result)


def test_unconfigured_daily_loss_limit_omits_axis():
    portfolio = {
        "total_equity": 50_000.0,
        "peak_equity_usd": 50_000.0,
        # daily_loss fact absent / invalid must not invent a prohibit when
        # no max_daily_loss_usd is configured.
        "account_new_risk_snapshot": {"daily_loss_usd": float("nan")},
    }
    with mock.patch(
        "application.account_new_risk_gate_support.resolve_production_drift_status_from_store",
        return_value=None,
    ):
        result = evaluate_portfolio_new_risk_admission(portfolio)
    assert result.disposition == NewRiskDisposition.ALLOW_NEW_RISK
    assert "DAILY_LOSS_UNKNOWN_FAIL_CLOSED" not in result.reason_codes
    assert "DAILY_LOSS_LIMIT_EXCEEDED" not in result.reason_codes


def test_configured_limit_without_daily_loss_fact_fails_closed():
    portfolio = {
        "total_equity": 50_000.0,
        "peak_equity_usd": 50_000.0,
        "account_new_risk_snapshot": {"max_daily_loss_usd": 100.0},
    }
    with mock.patch(
        "application.account_new_risk_gate_support.resolve_production_drift_status_from_store",
        return_value=None,
    ):
        result = evaluate_portfolio_new_risk_admission(portfolio)
    assert result.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
    assert "DAILY_LOSS_UNKNOWN_FAIL_CLOSED" in result.reason_codes


def test_unknown_pending_orders_prohibits_and_opens_breaker():
    portfolio = {
        "total_equity": 50_000.0,
        "unknown_pending_orders": True,
    }
    snapshot = build_account_new_risk_snapshot(portfolio)
    assert snapshot["observation_status"] == "COMPLETE"
    assert snapshot["reconciliation_status"] == "UNVERIFIED"
    assert snapshot["circuit_breaker_state"] == "OPEN"
    result = evaluate_portfolio_new_risk_admission(portfolio)
    assert result.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
    assert "CIRCUIT_BREAKER_OPEN" in result.reason_codes
    assert "RECONCILIATION_NOT_VERIFIED" in result.reason_codes


def test_durable_circuit_breaker_open_prohibits_new_risk():
    portfolio = {
        "total_equity": 50_000.0,
        "durable_circuit_breaker_state": "OPEN",
    }
    snapshot = build_account_new_risk_snapshot(portfolio)
    assert snapshot["circuit_breaker_state"] == "OPEN"
    result = evaluate_portfolio_new_risk_admission(portfolio)
    assert result.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
    assert "CIRCUIT_BREAKER_OPEN" in result.reason_codes


def test_build_portfolio_from_account_values_maps_equity():
    portfolio = build_portfolio_from_account_values(
        {"equity": 50_000.0},
        signal_metadata={"peak_equity_usd": 55_000.0},
    )
    assert portfolio["total_equity"] == 50_000.0
    assert portfolio["peak_equity_usd"] == 55_000.0
    assert portfolio["account_new_risk_snapshot"] == {
        "observation_status": "COMPLETE",
        "reconciliation_status": "VERIFIED",
        "circuit_breaker_state": "CLOSED",
        "equity_usd": 50_000.0,
    }


def test_combined_scale_halves_target_weights():
    assert apply_combined_scale_to_target_weights({"TQQQ": 0.8, "QQQ": 0.2}, 0.5) == {
        "TQQQ": 0.4,
        "QQQ": 0.1,
    }


def test_missing_combined_scale_leaves_target_weights():
    assert apply_combined_scale_to_target_weights({"TQQQ": 0.8}, None) == {"TQQQ": 0.8}


def test_submit_order_intent_rejects_buy_when_gate_prohibits():
    set_cycle_snapshot(
        InjectedReconciliationSnapshot(
            observation_status="COMPLETE",
            reconciliation_status="VERIFIED",
            circuit_breaker_state="CLOSED",
            equity_usd=None,
        )
    )
    ib = SimpleNamespace()
    with mock.patch(
        "application.ibkr_order_execution._submit_order_intent",
    ) as submit_mock:
        report = submit_order_intent(
            ib,
            OrderIntent(symbol="SPY", side="buy", quantity=1.0),
        )
    submit_mock.assert_not_called()
    assert report.status == "rejected"
    assert report.raw_payload.get("detail") == "account_new_risk_gate"
    assert "EQUITY_UNKNOWN_FAIL_CLOSED" in report.raw_payload.get("reason_codes", [])


def test_submit_order_intent_does_not_scale_buy_quantity():
    """Envelope scale applies to target weights, not submit-time quantity."""
    set_cycle_snapshot(
        InjectedReconciliationSnapshot(
            observation_status="COMPLETE",
            reconciliation_status="VERIFIED",
            circuit_breaker_state="CLOSED",
            equity_usd=40_000.0,
            drawdown_from_peak=0.075,
        )
    )
    expected = SimpleNamespace(status="Submitted")
    with mock.patch(
        "application.ibkr_order_execution._submit_order_intent",
        return_value=expected,
    ) as submit_mock:
        submit_order_intent(
            SimpleNamespace(),
            OrderIntent(symbol="SPY", side="buy", quantity=4.0),
        )
    assert submit_mock.call_args.args[1].quantity == 4.0


def test_submit_order_intent_allows_sell_when_gate_prohibits():
    set_cycle_snapshot(
        InjectedReconciliationSnapshot(
            observation_status="COMPLETE",
            reconciliation_status="VERIFIED",
            circuit_breaker_state="CLOSED",
            equity_usd=None,
        )
    )
    ib = SimpleNamespace()
    expected = SimpleNamespace(status="Submitted")
    with mock.patch(
        "application.ibkr_order_execution._submit_order_intent",
        return_value=expected,
    ) as submit_mock:
        submit_order_intent(
            ib,
            OrderIntent(symbol="SPY", side="sell", quantity=1.0),
        )
    submit_mock.assert_called_once()
    assert submit_mock.call_args.args[1].quantity == 1.0


def test_submit_order_intent_skips_gate_when_disabled():
    set_cycle_snapshot(
        InjectedReconciliationSnapshot(
            observation_status="COMPLETE",
            reconciliation_status="VERIFIED",
            circuit_breaker_state="CLOSED",
            equity_usd=None,
        )
    )
    ib = SimpleNamespace()
    expected = SimpleNamespace(status="Submitted")
    with mock.patch.dict(os.environ, {ACCOUNT_NEW_RISK_GATE_ENV: "0"}, clear=False):
        with mock.patch(
            "application.ibkr_order_execution._submit_order_intent",
            return_value=expected,
        ) as submit_mock:
            submit_order_intent(
                ib,
                OrderIntent(symbol="SPY", side="buy", quantity=1.0),
            )
    submit_mock.assert_called_once()


def test_cycle_gate_without_snapshot_is_fail_closed():
    result = evaluate_cycle_new_risk_admission()
    assert result.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
    assert "EQUITY_UNKNOWN_FAIL_CLOSED" in result.reason_codes

def test_attention_notify_on_new_risk_prohibit_dedupes(monkeypatch):
    import sys
    from pathlib import Path

    qpk = Path("/Users/lisiyi/Projects/.worktrees/qpk-attention-wire-20260918/src")
    if qpk.exists() and str(qpk) not in sys.path:
        sys.path.insert(0, str(qpk))

    reset_attention_sent_keys_for_tests()
    portfolio = {
        "total_equity": 50_000.0,
        "strategy_profile": "soxl_soxx_trend_income",
        "account_id": "U1599999",
        "account_new_risk_snapshot": {"production_drift_status": "critical"},
    }
    admission = evaluate_portfolio_new_risk_admission(portfolio)
    assert new_risk_buy_prohibited(admission)
    snapshot = build_snapshot_from_portfolio(portfolio)
    payloads: list[str] = []

    def _sender(*, text: str, alert_key: str | None = None, **_kwargs) -> bool:
        payloads.append(text)
        return True

    counts = maybe_publish_attention_for_admission(
        admission,
        portfolio=portfolio,
        snapshot=snapshot,
        telegram_sender=_sender,
        log_message=lambda *_a, **_k: None,
    )
    assert counts.get("sent") == 1
    counts2 = maybe_publish_attention_for_admission(
        admission,
        portfolio=portfolio,
        snapshot=snapshot,
        telegram_sender=_sender,
        log_message=lambda *_a, **_k: None,
    )
    assert counts2.get("sent") == 0
    assert counts2.get("skipped") == 1
    assert len(payloads) == 1

