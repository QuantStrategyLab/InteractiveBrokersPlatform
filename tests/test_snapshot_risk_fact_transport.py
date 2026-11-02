"""Synthetic native snapshot -> account gate regressions; no broker access."""
from dataclasses import replace

import pytest

from application import account_new_risk_gate_support as gate
from application.ibkr_portfolio import fetch_portfolio_snapshot
from application.rebalance_service import (
    _snapshot_to_portfolio_view,
    _strategy_portfolio_view,
    run_strategy_core,
)
from application.runtime_dependencies import IBKRRebalanceConfig, IBKRRebalanceRuntime
from quant_platform_kit.common.execution_state import ExecutionMarkerStore
from quant_platform_kit.common.port_adapters import CallableNotificationPort
from tests.test_ibkr_portfolio import (
    SnapshotIB, _stock_mark, _stock_position, _usd_account_values,
)
from tests.test_persistent_drawdown_breaker import session_at
from types import SimpleNamespace as NS


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("PORTFOLIO_LOSS_STOPLOSS_ENABLED", "false")
    for key in ("IBKR_MAX_DAILY_LOSS_USD", "MAX_DAILY_LOSS_USD", "RUNTIME_TARGET_JSON"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(gate, "resolve_production_drift_status_from_store", lambda **_kw: None)


def native_snapshot(nlv=32000):
    account = "DU00000001"
    rows = _usd_account_values(account, 1000, nlv)
    if nlv is None:
        rows = [row for row in rows if row.tag != "NetLiquidation"]
    ib = SnapshotIB(
        [_stock_position(account, "TQQQ", 1, 390, 100)],
        [_stock_mark(account, "TQQQ", 1, 390, 39000)], rows,
    )
    return fetch_portfolio_snapshot(ib, account_ids=(account,), wait_seconds=0)


def healthy_projection():
    return {"account_new_risk_snapshot": {
        "equity_usd": 40000, "peak_equity_usd": 40000, "realized_vol": .1,
        "observation_status": "COMPLETE", "reconciliation_status": "VERIFIED",
        "circuit_breaker_state": "CLOSED", "production_drift_status": "healthy",
    }}


def test_native_nlv_survives_strategy_scope_and_restart(tmp_path):
    snapshot = native_snapshot()
    positions, values = _snapshot_to_portfolio_view(snapshot)
    assert values["equity"] == 40000
    assert values["account_equity_usd"] == 32000
    _, scoped = _strategy_portfolio_view(positions, values, ("TQQQ",))
    assert scoped["account_equity_usd"] == 32000
    result = gate.evaluate_persistent_account_new_risk_admission(
        scoped, signal_metadata=healthy_projection(), session=session_at(tmp_path),
    )
    assert "DRAWDOWN_BRAKE_TRIPPED" in result.reason_codes
    assert result.hard_brake_triggered
    assert session_at(tmp_path).read_state()["circuit_breaker_open"] is True
    recovered = gate.evaluate_persistent_account_new_risk_admission(
        {"account_equity_usd": 40000}, signal_metadata=healthy_projection(),
        session=session_at(tmp_path, profile="other-synthetic"),
    )
    assert "CIRCUIT_BREAKER_OPEN" in recovered.reason_codes


@pytest.mark.parametrize("evidence", ["missing", "unqualified", "null", "nan", "zero", "negative", "malformed"])
def test_unavailable_native_nlv_cannot_fall_back_to_strategy_or_projection(tmp_path, evidence):
    snapshot = native_snapshot(None if evidence == "missing" else 40000)
    metadata = dict(snapshot.metadata)
    if evidence == "unqualified":
        metadata["total_equity_source"] = "unverified_net_liquidation"
    elif evidence != "missing":
        metadata["broker_net_liquidation"] = {
            "null": None, "nan": float("nan"), "zero": 0,
            "negative": -1, "malformed": "invalid",
        }[evidence]
    snapshot = replace(snapshot, metadata=metadata)
    positions, values = _snapshot_to_portfolio_view(snapshot)
    assert values["equity"] == 40000
    _, scoped = _strategy_portfolio_view(positions, values, ("TQQQ",))
    result = gate.evaluate_persistent_account_new_risk_admission(
        scoped, signal_metadata=healthy_projection(), session=session_at(tmp_path),
    )
    assert gate.new_risk_buy_prohibited(result)
    assert "EQUITY_UNKNOWN_FAIL_CLOSED" in result.reason_codes


@pytest.mark.parametrize("axis,value,reason", [
    ("realized_vol", float("nan"), "INVALID_REALIZED_VOL_FAIL_CLOSED"),
    ("observation_status", "STALE", "OBSERVATION_NOT_COMPLETE"),
    ("reconciliation_status", "UNVERIFIED", "RECONCILIATION_NOT_VERIFIED"),
    ("circuit_breaker_state", "OPEN", "CIRCUIT_BREAKER_OPEN"),
    ("production_drift_status", "critical", "PRODUCTION_DRIFT_CRITICAL"),
])
def test_native_unsafe_axis_reaches_real_cycle_admission(tmp_path, axis, value, reason):
    snapshot = native_snapshot(40000)
    snapshot = replace(snapshot, metadata={**snapshot.metadata, axis: value})
    calls = []
    store = ExecutionMarkerStore(local_dir=tmp_path / "journal")
    config = IBKRRebalanceConfig(
        translator=lambda key, **_kw: key, separator="---", strategy_profile="synthetic",
        account_ids=("DU00000001",), execution_state_store=store,
        reconciliation_output_path=tmp_path / "reconciliation.json", notify_no_trade_cycles=False,
        runtime_release_receipt={"attestation_state": "self_attested", "release_id": "synthetic",
                                 "strategy_release": {"revision": "synthetic"}},
    )
    runtime = IBKRRebalanceRuntime(
        connect_ib=lambda: NS(isConnected=lambda: True, disconnect=lambda: None),
        portfolio_port_factory=lambda _ib: NS(get_portfolio_snapshot=lambda: snapshot),
        compute_signals=lambda *_a: (None, "", False, "", {"strategy_profile": "synthetic", **healthy_projection()}),
        execute_rebalance=lambda *_a, **_kw: calls.append("execute"),
        notifications=CallableNotificationPort(lambda _text: None),
    )
    result = run_strategy_core(
        runtime=runtime, config=config,
        account_brake_submit=lambda *_a, **_kw: calls.append("exit"),
    )
    assert reason in result.signal_metadata["account_brake_observation"]["reason_codes"]
    assert calls == []
