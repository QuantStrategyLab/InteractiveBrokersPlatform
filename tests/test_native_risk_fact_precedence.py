"""Current account evidence must not be hidden by old strategy projections."""
from copy import deepcopy

import pytest

from application import account_new_risk_gate_support as gate
from tests.test_persistent_drawdown_breaker import session_at


@pytest.fixture(autouse=True)
def isolated_risk_context(monkeypatch):
    monkeypatch.setenv("PORTFOLIO_LOSS_STOPLOSS_ENABLED", "false")
    for name in ("IBKR_MAX_DAILY_LOSS_USD", "MAX_DAILY_LOSS_USD", "RUNTIME_TARGET_JSON"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(gate, "resolve_production_drift_status_from_store", lambda **_kw: None)


HEALTHY = {
    "equity_usd": 100_000, "peak_equity_usd": 100_000,
    "drawdown_from_peak": 0, "realized_vol": .1,
    "unknown_pending_orders": False, "observation_status": "COMPLETE",
    "reconciliation_status": "VERIFIED", "circuit_breaker_state": "CLOSED",
}


@pytest.mark.parametrize("native", [
    {"unknown_pending_orders": True},
    {"unknown_pending_orders": None},
    {"unknown_pending_orders": "unknown"},
    {"realized_vol": float("nan")},
    {"realized_vol": None},
    {"account_equity_usd": float("nan"), "equity": 100_000},
    {"peak_equity_usd": None},
    {"observation_status": "STALE"},
    {"observation_status": "UNKNOWN"},
    {"reconciliation_status": "UNVERIFIED"},
])
def test_current_unsafe_facts_block_old_healthy_projection(native, tmp_path):
    values = {"equity": 100_000, **native}
    metadata = {"account_new_risk_snapshot": deepcopy(HEALTHY)}
    result = gate.evaluate_persistent_account_new_risk_admission(
        values, signal_metadata=metadata, session=session_at(tmp_path),
    )
    assert gate.new_risk_buy_prohibited(result)
    assert metadata["account_new_risk_snapshot"] == HEALTHY
    assert gate.scale_targets_for_admission({"TQQQ": .1}, result) == {"TQQQ": .1}


def test_missing_current_equity_does_not_reuse_projection(tmp_path):
    result = gate.evaluate_persistent_account_new_risk_admission(
        {}, signal_metadata={"account_new_risk_snapshot": deepcopy(HEALTHY)},
        session=session_at(tmp_path),
    )
    assert gate.new_risk_buy_prohibited(result)


def test_later_native_failure_is_not_hidden_by_prior_cycle_or_restart(tmp_path):
    metadata = {"account_new_risk_snapshot": deepcopy(HEALTHY)}
    result = gate.evaluate_persistent_account_new_risk_admission(
        {"equity": 100_000, "realized_vol": .1}, signal_metadata=metadata,
        session=session_at(tmp_path),
    )
    assert not gate.new_risk_buy_prohibited(result)
    for owner in ("same-process", "restarted-process"):
        result = gate.evaluate_persistent_account_new_risk_admission(
            {"equity": 100_000, "realized_vol": float("nan")},
            signal_metadata=metadata, session=session_at(tmp_path, owner),
        )
        assert gate.new_risk_buy_prohibited(result)
