"""Synthetic RC-01C admission boundary; no broker or network access."""
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest

from application import account_new_risk_gate_support as gate
from application import execution_service
from application.rebalance_service import _snapshot_to_portfolio_view, _strategy_portfolio_view
from quant_platform_kit.common.execution_state import AccountSubmissionSession, ExecutionMarkerStore
from quant_platform_kit.common.models import OrderIntent
from tests.test_execution_service import _signal_metadata, execute_rebalance, translate

PROFILES = [("TQQQ", "tqqq_growth_income"), ("SOXL", "soxl_soxx_trend_income")]


@pytest.fixture(autouse=True)
def offline_context(monkeypatch):
    for key in ("IBKR_MAX_DAILY_LOSS_USD", "MAX_DAILY_LOSS_USD", "RUNTIME_TARGET_JSON"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PORTFOLIO_LOSS_STOPLOSS_ENABLED", "false")
    monkeypatch.setenv("IBKR_DRY_RUN_ONLY", "true")
    monkeypatch.setenv("RUNTIME_TARGET_ENABLED", "false")
    monkeypatch.setattr(gate, "resolve_production_drift_status_from_store", lambda **_kw: None)
    monkeypatch.setattr(execution_service, "maybe_publish_attention_for_admission", lambda *_a, **_k: {})
    gate.set_cycle_snapshot(None)
    yield
    gate.set_cycle_snapshot(None)


def session_at(path, profile="synthetic", account="DU00000001"):
    return AccountSubmissionSession(
        ExecutionMarkerStore(local_dir=path), broker="ibkr", account_id=account,
        owner_id=profile, profile=profile, intent_id="1" * 64, release_id="2" * 64,
    )


def snapshot_for(symbol):
    return NS(
        total_equity=40_000, buying_power=39_000,
        as_of=datetime(2026, 11, 2, tzinfo=timezone.utc),
        positions=(NS(symbol=symbol, quantity=10, average_cost=100),),
        metadata={"peak_equity_usd": 50_000, "drawdown_from_peak": .2,
                  "broker_net_liquidation": 40_000, "total_equity_source": "broker_net_liquidation"},
    )


def preview(symbol, profile, positions, values, targets, path, session):
    metadata = _signal_metadata(
        targets, risk_symbols=tuple(targets), trade_date="2026-11-02",
        effective_date=datetime.now(ZoneInfo("America/New_York")).date().isoformat(),
    )
    calls = []
    ib = NS(openTrades=lambda: [], reqAllOpenOrders=lambda: [], fills=lambda: [],
            accountValues=lambda: [NS(tag="CashBalance", currency="USD", value="39000")])
    _, summary = execute_rebalance(
        ib, targets, positions, values,
        fetch_quote_snapshots=lambda _ib, symbols: {s: NS(last_price=100) for s in symbols},
        submit_order_intent=lambda *_a, **_k: calls.append(True),
        order_intent_cls=OrderIntent, translator=translate, strategy_profile=profile,
        signal_metadata=metadata, dry_run_only=True, cash_reserve_ratio=0,
        rebalance_threshold_ratio=0, limit_buy_premium=1, sell_settle_delay_sec=0,
        min_order_notional=0, execution_lock_dir=path, return_summary=True,
        account_execution_session=session,
    )
    assert calls == []
    assert metadata["allocation"]["targets"] == targets
    return summary


@pytest.mark.parametrize("symbol,profile", PROFILES)
def test_drawdown_survives_native_and_strategy_projection(symbol, profile):
    positions, values = _snapshot_to_portfolio_view(snapshot_for(symbol))
    # Strategy allocation equity changes; account drawdown uses whole-account NAV.
    positions["VOO"] = {"quantity": 10, "avg_cost": 500}
    values["buying_power"] = 10_000
    _, scoped = _strategy_portfolio_view(positions, values, (symbol,))
    assert scoped["equity"] == 11_000
    assert scoped["account_equity_usd"] == 40_000
    observed = gate.build_snapshot_from_portfolio(gate.build_portfolio_from_account_values(scoped))
    assert observed.equity_usd == 40_000
    assert observed.peak_equity_usd == 50_000
    assert observed.drawdown_from_peak == .2
    assert gate.evaluate_account_values_new_risk_admission(scoped).hard_brake_triggered


@pytest.mark.parametrize("symbol,profile", PROFILES)
@pytest.mark.parametrize("holding,target", [(10, .025), (10, .05), (0, .025)])
def test_switch_off_preserves_target_and_blocks_new_positions_and_adds(symbol, profile, holding, target, tmp_path):
    positions, values = _snapshot_to_portfolio_view(snapshot_for(symbol))
    positions[symbol]["quantity"] = holding
    session = session_at(tmp_path / "journal", profile)
    summary = preview(symbol, profile, positions, values, {symbol: target}, tmp_path / "cycle", session)
    assert summary["orders_submitted"] == []
    assert summary["orders_pending"] == []
    assert session.read_state()["circuit_breaker_open"] is True
    # Admission cannot scale the holding target down, even though its scale is zero.
    admission = gate.evaluate_persistent_account_new_risk_admission(values, session=session)
    assert admission.combined_scale == 0
    assert gate.scale_targets_for_admission({symbol: target}, admission) == {symbol: target}
    assert gate.new_risk_buy_prohibited(admission)


@pytest.mark.parametrize("symbol,profile", PROFILES)
def test_recovered_cycles_and_closed_injection_cannot_clear_open(symbol, profile, tmp_path):
    positions, values = _snapshot_to_portfolio_view(snapshot_for(symbol))
    session = session_at(tmp_path / "journal", profile)
    preview(symbol, profile, positions, values, {symbol: .025}, tmp_path / "trip", session)
    for cycle in (1, 2):
        recovered = {"equity": 40_000, "buying_power": 39_000, "drawdown_from_peak": 0,
                     "durable_circuit_breaker_state": "CLOSED"}
        restarted = session_at(tmp_path / "journal", "other-synthetic")
        admission = gate.evaluate_persistent_account_new_risk_admission(
            recovered, session=restarted, signal_metadata={"account_new_risk_snapshot": {
                "circuit_breaker_state": "CLOSED", "durable_circuit_breaker_state": "CLOSED",
                "reset_authorized": True,
            }},
        )
        assert gate.new_risk_buy_prohibited(admission)
        assert "CIRCUIT_BREAKER_OPEN" in admission.reason_codes
        assert admission.circuit_breaker_reset is False
        summary = preview(symbol, profile, positions, recovered, {symbol: .05}, tmp_path / str(cycle), restarted)
        assert summary["orders_submitted"] == []
    assert session.read_state()["generation"] == 1


@pytest.mark.parametrize("symbol,profile", PROFILES)
def test_switch_on_preserves_existing_reductions(symbol, profile, tmp_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_LOSS_STOPLOSS_ENABLED", "true")
    positions, values = _snapshot_to_portfolio_view(snapshot_for(symbol))
    summary = preview(symbol, profile, positions, values, {symbol: .025}, tmp_path / "cycle", session_at(tmp_path / "journal", profile))
    assert summary["orders_submitted"] == [{"symbol": symbol, "side": "sell", "quantity": 10, "status": "dry_run"}]


def test_durable_open_dominates_closed_projection():
    portfolio = {"total_equity": 40_000, "durable_circuit_breaker_state": "OPEN",
                 "account_new_risk_snapshot": {"circuit_breaker_state": "CLOSED", "durable_circuit_breaker_state": "CLOSED"}}
    assert gate.build_account_new_risk_snapshot(portfolio)["circuit_breaker_state"] == "OPEN"


@pytest.mark.parametrize("failure", ["missing", "old_capability", "read", "write", "corruption"])
def test_persistence_failures_prohibit_new_risk(failure, tmp_path, monkeypatch):
    # Rule A requires measured peak evidence; (50,000 - 40,000) / 50,000 = 0.2 exercises the actual journal failure.
    session = session_at(tmp_path)
    if failure == "missing":
        session = None
    elif failure == "old_capability":
        monkeypatch.delattr(AccountSubmissionSession, "record_circuit_breaker_open")
    elif failure in {"read", "write"}:
        method = "read_state" if failure == "read" else "_append"
        def fail(*_args, **_kwargs):
            raise OSError("synthetic persistence failure")
        monkeypatch.setattr(AccountSubmissionSession, method, fail)
    else:
        session.record_circuit_breaker_open(snapshot=gate.build_snapshot_from_portfolio({"peak_equity_usd": 50_000, "total_equity": 40_000, "drawdown_from_peak": .2}))
        session._location(1).write_text("invalid json")
    result = gate.evaluate_persistent_account_new_risk_admission({"peak_equity_usd": 50_000, "equity": 40_000, "drawdown_from_peak": .2}, session=session)
    assert gate.new_risk_buy_prohibited(result)
    assert "PERSISTENT_BREAKER_UNAVAILABLE" in result.reason_codes


def test_no_target_cycle_records_open_before_return_and_never_calls_exit(tmp_path):
    from application.rebalance_service import run_strategy_core
    from application.runtime_dependencies import IBKRRebalanceConfig, IBKRRebalanceRuntime
    from quant_platform_kit.common.port_adapters import CallableNotificationPort
    # Use the actual runtime ports, a local fake account journal and no broker adapter.
    snapshot = snapshot_for("TQQQ")
    config = IBKRRebalanceConfig(
        translator=lambda key, **_kw: key, separator="---", strategy_profile="synthetic",
        account_ids=("DU00000001",), execution_state_store=ExecutionMarkerStore(local_dir=tmp_path / "journal"),
        reconciliation_output_path=tmp_path / "reconciliation.json", notify_no_trade_cycles=False,
        runtime_release_receipt={"attestation_state": "self_attested", "release_id": "synthetic", "strategy_release": {"revision": "synthetic"}},
    )
    calls = []
    ib = NS(isConnected=lambda: True, disconnect=lambda: None)
    runtime = IBKRRebalanceRuntime(
        connect_ib=lambda: ib, portfolio_port_factory=lambda _ib: NS(get_portfolio_snapshot=lambda: snapshot),
        compute_signals=lambda *_a: (None, "", False, "", {"strategy_profile": "synthetic"}),
        execute_rebalance=lambda *_a, **_k: calls.append("execute"),
        notifications=CallableNotificationPort(lambda _text: None),
    )
    result = run_strategy_core(runtime=runtime, config=config, account_brake_submit=lambda *_a, **_k: calls.append("exit"),
                               account_brake_session=session_at(tmp_path / "journal"), account_brake_order_intent_cls=OrderIntent)
    assert result.target_weights is None
    assert result.signal_metadata["account_brake_observation"]["hard_brake_triggered"] is True
    assert calls == []
    assert session_at(tmp_path / "journal", "other-synthetic").read_state()["circuit_breaker_open"] is True


def test_two_ten_share_holdings_never_become_drawdown_sell_plans(tmp_path):
    positions = {symbol: {"quantity": 10, "avg_cost": 100} for symbol in ("TQQQ", "SOXL")}
    values = {"equity": 40_000, "buying_power": 38_000, "drawdown_from_peak": .2, "peak_equity_usd": 50_000}
    targets = {"TQQQ": .025, "SOXL": .025}
    summary = preview("TQQQ", "tqqq_growth_income", positions, values, targets,
                      tmp_path / "cycle", session_at(tmp_path / "journal"))
    assert summary["orders_submitted"] == []
    assert summary["orders_pending"] == []
    assert {row["symbol"]: row["target_weight"] for row in summary["target_vs_current"]} == targets


def test_native_drawdown_facts_outrank_a_stale_healthy_signal():
    _, values = _snapshot_to_portfolio_view(snapshot_for("TQQQ"))
    metadata = {"account_new_risk_snapshot": {"equity_usd": 100_000, "peak_equity_usd": 100_000, "drawdown_from_peak": 0}}
    result = gate.evaluate_account_values_new_risk_admission(values, signal_metadata=metadata)
    assert result.hard_brake_triggered
    assert gate.new_risk_buy_prohibited(result)
    values.pop("drawdown_from_peak")
    assert gate.evaluate_account_values_new_risk_admission(values, signal_metadata=metadata).hard_brake_triggered


@pytest.mark.parametrize("instant", [
    datetime(2026, 1, 15, 2, tzinfo=timezone.utc),
    datetime(2026, 7, 15, 2, tzinfo=timezone.utc),
    datetime(2026, 3, 8, 2, tzinfo=timezone.utc),
    datetime(2026, 11, 1, 2, tzinfo=timezone.utc),
])
def test_tqqq_preview_uses_exchange_date_across_utc_midnight(monkeypatch, tmp_path, instant):
    """The fixture must pass the real date gate in standard and daylight time."""
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

    monkeypatch.setattr(f"{__name__}.datetime", FrozenDateTime)
    monkeypatch.setattr(execution_service, "datetime", FrozenDateTime)
    test_switch_off_preserves_target_and_blocks_new_positions_and_adds(
        "TQQQ", "tqqq_growth_income", 0, .025, tmp_path,
    )
