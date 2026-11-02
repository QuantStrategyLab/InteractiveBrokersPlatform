"""Synthetic drawdown sizing through real planning and journal dispatch."""
from types import SimpleNamespace as NS

import pytest

from application import account_new_risk_gate_support as gate
from application import execution_service
from quant_platform_kit.common.models import ExecutionReport, OrderIntent
from notifications.telegram import build_translator
from tests.test_execution_service import _signal_metadata, execute_rebalance
from tests.test_persistent_drawdown_breaker import preview, session_at

BOUNDARIES = [0.074999, 0.075, 0.075001, 0.10, 0.15, 0.20]
PROFILES = [("TQQQ", "tqqq_growth_income"), ("SOXL", "soxl_soxx_trend_income")]


@pytest.fixture(autouse=True)
def offline_context(monkeypatch):
    for key in ("IBKR_MAX_DAILY_LOSS_USD", "MAX_DAILY_LOSS_USD", "RUNTIME_TARGET_JSON"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ACCOUNT_NEW_RISK_GATE", "1")
    monkeypatch.setenv("PORTFOLIO_LOSS_STOPLOSS_ENABLED", "false")
    monkeypatch.setenv("IBKR_DRY_RUN_ONLY", "true")
    monkeypatch.setenv("RUNTIME_TARGET_ENABLED", "false")
    monkeypatch.setattr(gate, "resolve_production_drift_status_from_store", lambda **_kw: None)
    monkeypatch.setattr(execution_service, "maybe_publish_attention_for_admission", lambda *_a, **_kw: {})
    gate.set_cycle_snapshot(None)
    yield
    gate.set_cycle_snapshot(None)


def values_for(drawdown, *, equity=40_000, realized_vol=None):
    values = {"equity": equity, "account_equity_usd": equity, "buying_power": equity - 1_000,
              "drawdown_from_peak": drawdown, "peak_equity_usd": equity / (1 - drawdown)}
    if realized_vol is not None:
        values["realized_vol"] = realized_vol
    return values


def dispatch_cycle(tmp_path, values, *, symbol="TQQQ", target=.025, snapshot=None):
    calls = []

    def fake_venue(_ib, order):
        calls.append((order.side, order.quantity))
        return ExecutionReport(symbol=order.symbol, side=order.side, quantity=order.quantity,
                               status="Filled", broker_order_id="synthetic",
                               filled_quantity=order.quantity, average_fill_price=100)

    ib = NS(openTrades=lambda: [], reqAllOpenOrders=lambda: [], fills=lambda: [],
            accountValues=lambda: [NS(tag="CashBalance", currency="USD", value="39000")])
    metadata = _signal_metadata({symbol: target}, risk_symbols=(symbol,))
    if snapshot is not None:
        metadata["account_new_risk_snapshot"].update(snapshot)
    _, summary = execute_rebalance(
        ib, {symbol: target}, {symbol: {"quantity": 10, "avg_cost": 100}}, values,
        fetch_quote_snapshots=lambda _ib, symbols: {s: NS(last_price=100) for s in symbols},
        submit_order_intent=fake_venue, order_intent_cls=OrderIntent, translator=build_translator("en"),
        signal_metadata=metadata, dry_run_only=False, execution_mode="paper",
        cash_reserve_ratio=0, rebalance_threshold_ratio=0, limit_buy_premium=1,
        sell_settle_delay_sec=0, min_order_notional=0, execution_lock_dir=tmp_path,
        return_summary=True, acquire_execution_claim=lambda: True,
    )
    return calls, summary


@pytest.mark.parametrize("symbol,profile", PROFILES)
@pytest.mark.parametrize("drawdown", BOUNDARIES)
def test_switch_off_boundary_keeps_matching_holding(symbol, profile, drawdown, tmp_path):
    summary = preview(symbol, profile, {symbol: {"quantity": 10, "avg_cost": 100}},
                      values_for(drawdown), {symbol: .025}, tmp_path / "cycle",
                      session_at(tmp_path / "journal", profile))
    assert summary["orders_submitted"] == []
    assert summary["orders_pending"] == []
    assert {row["symbol"]: row["target_weight"] for row in summary["target_vs_current"]} == {symbol: .025}


@pytest.mark.parametrize("symbol", ["TQQQ", "SOXL"])
@pytest.mark.parametrize("drawdown", BOUNDARIES)
def test_switch_off_fake_dispatch_receives_no_sell(symbol, drawdown, tmp_path):
    calls, summary = dispatch_cycle(tmp_path, values_for(drawdown), symbol=symbol)
    assert calls == []
    assert summary["orders_submitted"] == []
    assert summary["orders_pending"] == []


@pytest.mark.parametrize("drawdown,quantity", [(d, 0 if d < .075 else 5 if d < .15 else 10) for d in BOUNDARIES])
def test_switch_on_fake_dispatch_retains_original_sizing(drawdown, quantity, tmp_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_LOSS_STOPLOSS_ENABLED", "true")
    calls, summary = dispatch_cycle(tmp_path, values_for(drawdown))
    assert calls == ([("sell", quantity)] if quantity else [])
    assert [row["quantity"] for row in summary["orders_filled"]] == ([quantity] if quantity else [])


@pytest.mark.parametrize("equity,vol,scale", [(40_000, .4, .5), (100_000, None, .85), (100_000, .4, .425)])
@pytest.mark.parametrize("drawdown", [0, .10, .20])
def test_switch_off_keeps_independent_capital_and_volatility_sizing(equity, vol, scale, drawdown, tmp_path):
    target = 1_000 / equity
    calls, summary = dispatch_cycle(tmp_path, values_for(drawdown, equity=equity, realized_vol=vol), target=target)
    assert summary["target_vs_current"][0]["target_weight"] == pytest.approx(target * scale)
    assert calls == [("sell", int(10 * (1 - scale)))]


@pytest.mark.parametrize("switch", [None, "", "unknown", "true false", "2"])
@pytest.mark.parametrize("drawdown", [.10, .20])
def test_ambiguous_switch_confers_no_drawdown_exit_authority(switch, drawdown, tmp_path, monkeypatch):
    if switch is None:
        monkeypatch.delenv("PORTFOLIO_LOSS_STOPLOSS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("PORTFOLIO_LOSS_STOPLOSS_ENABLED", switch)
    calls, summary = dispatch_cycle(tmp_path, values_for(drawdown))
    assert calls == []
    assert summary["orders_submitted"] == []
    if drawdown >= .15:
        assert gate.new_risk_buy_prohibited(gate.evaluate_account_values_new_risk_admission(values_for(drawdown)))


def test_unreadable_switch_confers_no_exit_authority(tmp_path, monkeypatch):
    original_get = gate.os.environ.get

    def unreadable(key, default=None):
        if key == "PORTFOLIO_LOSS_STOPLOSS_ENABLED":
            raise OSError("synthetic switch read failure")
        return original_get(key, default)

    monkeypatch.setattr(gate.os.environ, "get", unreadable)
    calls, summary = dispatch_cycle(tmp_path, values_for(.10))
    assert calls == []
    assert summary["orders_submitted"] == []


def test_native_cycle_drawdown_does_not_shrink_a_stale_healthy_projection(tmp_path):
    calls, summary = dispatch_cycle(tmp_path, values_for(.10), snapshot={
        "equity_usd": 100_000, "peak_equity_usd": 100_000, "drawdown_from_peak": 0,
    })
    assert calls == []
    assert summary["target_vs_current"][0]["target_weight"] == .025


def test_restarted_open_keeps_independent_volatility_sizing(tmp_path):
    journal = tmp_path / "journal"
    original = session_at(journal)
    gate.evaluate_persistent_account_new_risk_admission(values_for(.20), session=original)
    restarted = session_at(journal, "other-synthetic")
    assert restarted.read_state()["circuit_breaker_open"] is True
    summary = preview("TQQQ", "tqqq_growth_income", {"TQQQ": {"quantity": 10, "avg_cost": 100}},
                      values_for(.10, realized_vol=.4), {"TQQQ": .025}, tmp_path / "cycle", restarted)
    assert summary["target_vs_current"][0]["target_weight"] == .0125
    assert summary["orders_submitted"] == [{"symbol": "TQQQ", "side": "sell", "quantity": 5, "status": "dry_run"}]
    assert gate.new_risk_buy_prohibited(gate.evaluate_persistent_account_new_risk_admission(
        values_for(.10, realized_vol=.4), session=restarted))
    assert session_at(journal).read_state()["circuit_breaker_open"] is True
