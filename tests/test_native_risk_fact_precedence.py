"""Current unsafe account facts cannot be cleared by strategy projections."""
import pytest

from application import account_new_risk_gate_support as gate
from tests.test_persistent_drawdown_breaker import session_at


@pytest.fixture(autouse=True)
def offline_context(monkeypatch):
    monkeypatch.setenv("PORTFOLIO_LOSS_STOPLOSS_ENABLED", "false")
    for key in ("IBKR_MAX_DAILY_LOSS_USD", "MAX_DAILY_LOSS_USD", "RUNTIME_TARGET_JSON"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(gate, "resolve_production_drift_status_from_store", lambda **_kw: None)


@pytest.mark.parametrize("native,projected", [
    ({"unknown_pending_orders": True}, {"unknown_pending_orders": False}),
    ({"unknown_pending_orders": None}, {"unknown_pending_orders": False}),
    ({"unknown_pending_orders": "false"}, {"unknown_pending_orders": False}),
    ({"production_drift_status": "critical"}, {"production_drift_status": "healthy"}),
    ({"daily_loss_usd": 100}, {"daily_loss_usd": 0, "max_daily_loss_usd": 100}),
    ({"daily_loss_usd": None}, {"daily_loss_usd": 0, "max_daily_loss_usd": 100}),
    ({"realized_vol": float("nan")}, {"realized_vol": .1}),
    ({"realized_vol": None}, {"realized_vol": .1}),
    ({"realized_vol": "unknown"}, {"realized_vol": .1}),
    ({"account_equity_usd": None}, {"equity_usd": 40_000}),
    ({"account_equity_usd": float("nan")}, {"equity_usd": 40_000}),
    ({"observation_status": "STALE"}, {"observation_status": "COMPLETE"}),
    ({"observation_status": "UNKNOWN"}, {"observation_status": "COMPLETE"}),
    ({"reconciliation_status": "UNVERIFIED"}, {"reconciliation_status": "VERIFIED"}),
    ({"durable_circuit_breaker_state": "UNKNOWN"}, {"durable_circuit_breaker_state": "CLOSED"}),
])
def test_current_unsafe_fact_blocks_healthy_projection(native, projected, tmp_path):
    # Include every healthy projected axis: setdefault must not hide native failures.
    metadata = {"account_new_risk_snapshot": {
        "equity_usd": 40_000, "observation_status": "COMPLETE",
        "reconciliation_status": "VERIFIED", "circuit_breaker_state": "CLOSED",
        **projected,
    }}
    result = gate.evaluate_persistent_account_new_risk_admission(
        {"account_equity_usd": 40_000, "equity": 40_000, **native},
        signal_metadata=metadata, session=session_at(tmp_path / "journal"),
    )
    assert gate.new_risk_buy_prohibited(result)
    assert result.reason_codes
    assert result.live_authority_granted is False
    assert gate.scale_targets_for_admission({"TQQQ": .025}, result) == {"TQQQ": .025}


@pytest.mark.parametrize("native", [
    {"unknown_pending_orders": True},
    {"realized_vol": float("nan")},
    {"observation_status": "STALE"},
    {"total_equity": None},
])
def test_direct_portfolio_path_uses_current_facts(native):
    result = gate.evaluate_portfolio_new_risk_admission({
        "total_equity": 40_000, **native,
        "account_new_risk_snapshot": {"equity_usd": 40_000, "realized_vol": .1,
            "unknown_pending_orders": False, "observation_status": "COMPLETE",
            "reconciliation_status": "VERIFIED", "circuit_breaker_state": "CLOSED"},
    })
    assert gate.new_risk_buy_prohibited(result)


def test_missing_current_equity_cannot_be_supplied_by_old_signal(tmp_path):
    result = gate.evaluate_persistent_account_new_risk_admission(
        {}, signal_metadata={"account_new_risk_snapshot": {"equity_usd": 40_000}},
        session=session_at(tmp_path / "journal"),
    )
    assert gate.new_risk_buy_prohibited(result)
