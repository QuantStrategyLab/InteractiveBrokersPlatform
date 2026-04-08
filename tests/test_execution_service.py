from types import SimpleNamespace

from application.execution_service import check_order_submitted, execute_rebalance
from quant_platform_kit.common.models import OrderIntent


def _weight_allocation(targets, *, risk_symbols=(), income_symbols=(), safe_haven_symbols=()):
    ordered_symbols = tuple(targets.keys())
    return {
        "target_mode": "weight",
        "strategy_symbols": ordered_symbols,
        "risk_symbols": tuple(risk_symbols),
        "income_symbols": tuple(income_symbols),
        "safe_haven_symbols": tuple(safe_haven_symbols),
        "targets": dict(targets),
    }


def _signal_metadata(
    targets,
    *,
    risk_symbols=(),
    income_symbols=(),
    safe_haven_symbols=(),
    **extra,
):
    payload = dict(extra)
    payload["allocation"] = _weight_allocation(
        targets,
        risk_symbols=risk_symbols,
        income_symbols=income_symbols,
        safe_haven_symbols=safe_haven_symbols,
    )
    return payload


def translate(key, **kwargs):
    templates = {
        "submitted": "submitted {order_id}",
        "failed": "failed {reason}",
        "market_sell": "sell {symbol} {qty}",
        "limit_buy": "buy {symbol} {qty} @{price}",
        "target_diff": "target_diff {symbol}: current={current} target={target} delta={delta}",
        "execution_profile_detail": "profile={profile}",
        "regime_detail": "regime={value}",
        "breadth_detail": "breadth={value}",
        "target_stock_detail": "target_stock={value}",
        "realized_stock_detail": "realized_stock={value}",
        "snapshot_as_of_detail": "snapshot_as_of={value}",
        "trade_date_detail": "trade_date={value}",
        "pending_orders_detected": "pending_orders_detected profile={profile} symbols={symbols}",
        "same_day_fills_detected": "same_day_fills_detected profile={profile} mode={mode} symbols={symbols} trade_date={trade_date}",
        "same_day_execution_locked": "same_day_execution_locked profile={profile} mode={mode} trade_date={trade_date} snapshot_date={snapshot_date} target_hash={target_hash} lock_path={lock_path}",
        "execution_lock_acquired": "execution_lock_acquired mode={mode} trade_date={trade_date} snapshot_date={snapshot_date} lock_path={lock_path}",
        "dry_run_snapshot_prices": "dry_run_snapshot_prices count={count} symbols={symbols}",
        "no_equity": "❌ No equity",
    }
    template = templates[key]
    return template.format(**kwargs) if kwargs else template


def test_check_order_submitted_accepts_submitted_like_status():
    report = SimpleNamespace(broker_order_id="123", status="Submitted")
    ok, message = check_order_submitted(report, translator=translate)
    assert ok is True
    assert "submitted 123" in message


def test_execute_rebalance_submits_limit_buy_for_underweight_position(monkeypatch, tmp_path):
    class FakeIB:
        def openTrades(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="5000")]

    submitted = []

    def fake_submit_order_intent(_ib, intent):
        submitted.append(intent)
        return SimpleNamespace(broker_order_id="1", status="Submitted")

    def fake_fetch_quote_snapshots(_ib, symbols):
        return {symbol: SimpleNamespace(last_price=100.0) for symbol in symbols}

    monkeypatch.setattr("application.execution_service.time.sleep", lambda _seconds: None)

    trade_logs = execute_rebalance(
        FakeIB(),
        {"VOO": 1.0},
        {},
        {"equity": 1000.0, "buying_power": 1000.0},
        fetch_quote_snapshots=fake_fetch_quote_snapshots,
        submit_order_intent=fake_submit_order_intent,
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["VOO", "BIL"],
        strategy_profile="tech_pullback_cash_buffer",
        signal_metadata=_signal_metadata(
            {"VOO": 1.0},
            risk_symbols=("VOO",),
            safe_haven_symbols=("BIL",),
            regime="risk_on",
            breadth_ratio=0.6,
            target_stock_weight=0.8,
            realized_stock_weight=0.8,
            trade_date="2026-04-01",
            snapshot_as_of="2026-03-31",
        ),
        dry_run_only=False,
        cash_reserve_ratio=0.03,
        rebalance_threshold_ratio=0.02,
        limit_buy_premium=1.005,
        sell_settle_delay_sec=0,
        execution_lock_dir=tmp_path,
    )

    assert len(submitted) == 1
    assert submitted[0].side == "buy"
    assert submitted[0].symbol == "VOO"
    assert submitted[0].order_type == "limit"
    assert any(log.startswith("buy VOO") for log in trade_logs)


def test_execute_rebalance_skips_when_pending_orders_exist():
    class FakeIB:
        def openTrades(self):
            return [
                SimpleNamespace(
                    contract=SimpleNamespace(symbol="VOO"),
                    orderStatus=SimpleNamespace(status="Submitted"),
                )
            ]

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="5000")]

    trade_logs = execute_rebalance(
        FakeIB(),
        {"VOO": 1.0},
        {},
        {"equity": 1000.0, "buying_power": 1000.0},
        fetch_quote_snapshots=lambda *_args, **_kwargs: {"VOO": SimpleNamespace(last_price=100.0)},
        submit_order_intent=lambda *_args, **_kwargs: None,
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["VOO"],
        strategy_profile="tech_pullback_cash_buffer",
        signal_metadata=_signal_metadata({"VOO": 1.0}, risk_symbols=("VOO",)),
        dry_run_only=False,
        cash_reserve_ratio=0.03,
        rebalance_threshold_ratio=0.02,
        limit_buy_premium=1.005,
        sell_settle_delay_sec=0,
    )

    assert trade_logs == ["pending_orders_detected profile=tech_pullback_cash_buffer symbols=VOO"]


def test_execute_rebalance_blocks_same_day_repeat_via_execution_lock(tmp_path, monkeypatch):
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="5000")]

    def fake_fetch_quote_snapshots(_ib, symbols):
        return {symbol: SimpleNamespace(last_price=100.0) for symbol in symbols}

    monkeypatch.setattr("application.execution_service.time.sleep", lambda _seconds: None)

    kwargs = dict(
        fetch_quote_snapshots=fake_fetch_quote_snapshots,
        submit_order_intent=lambda *_args, **_kwargs: SimpleNamespace(broker_order_id="1", status="Submitted"),
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["VOO", "BOXX"],
        strategy_profile="tech_pullback_cash_buffer",
        account_group="default",
        service_name="ibkr-paper",
        account_ids=("DU123",),
        signal_metadata=_signal_metadata(
            {"VOO": 0.8, "BOXX": 0.2},
            risk_symbols=("VOO",),
            safe_haven_symbols=("BOXX",),
            regime="risk_on",
            breadth_ratio=0.6,
            target_stock_weight=0.8,
            realized_stock_weight=0.8,
            trade_date="2026-04-01",
            snapshot_as_of="2026-03-31",
        ),
        cash_reserve_ratio=0.0,
        rebalance_threshold_ratio=0.02,
        limit_buy_premium=1.005,
        sell_settle_delay_sec=0,
        execution_lock_dir=tmp_path,
    )

    first_logs = execute_rebalance(
        FakeIB(),
        {"VOO": 0.8, "BOXX": 0.2},
        {},
        {"equity": 1000.0, "buying_power": 1000.0},
        dry_run_only=True,
        **kwargs,
    )
    second_logs = execute_rebalance(
        FakeIB(),
        {"VOO": 0.8, "BOXX": 0.2},
        {},
        {"equity": 1000.0, "buying_power": 1000.0},
        dry_run_only=True,
        **kwargs,
    )
    paper_logs = execute_rebalance(
        FakeIB(),
        {"VOO": 0.8, "BOXX": 0.2},
        {},
        {"equity": 1000.0, "buying_power": 1000.0},
        dry_run_only=False,
        **kwargs,
    )

    assert any("execution_lock_acquired" in log for log in first_logs)
    assert any("same_day_execution_locked" in log for log in second_logs)
    assert any("execution_lock_acquired" in log for log in paper_logs)


def test_execute_rebalance_skips_when_same_day_fills_detected():
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return [
                SimpleNamespace(
                    contract=SimpleNamespace(symbol="VOO"),
                    execution=SimpleNamespace(time="2026-04-01 10:30:00"),
                )
            ]

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="5000")]

    trade_logs = execute_rebalance(
        FakeIB(),
        {"VOO": 1.0},
        {},
        {"equity": 1000.0, "buying_power": 1000.0},
        fetch_quote_snapshots=lambda *_args, **_kwargs: {"VOO": SimpleNamespace(last_price=100.0)},
        submit_order_intent=lambda *_args, **_kwargs: None,
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["VOO"],
        strategy_profile="tech_pullback_cash_buffer",
        account_group="default",
        service_name="ibkr-paper",
        account_ids=("DU123",),
        signal_metadata=_signal_metadata({"VOO": 1.0}, risk_symbols=("VOO",), trade_date="2026-04-01"),
        dry_run_only=False,
        cash_reserve_ratio=0.0,
        rebalance_threshold_ratio=0.02,
        limit_buy_premium=1.005,
        sell_settle_delay_sec=0,
    )

    assert any("same_day_fills_detected" in log for log in trade_logs)


def test_execute_rebalance_returns_structured_summary_when_requested(monkeypatch, tmp_path):
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="5000")]

    def fake_fetch_quote_snapshots(_ib, symbols):
        return {symbol: SimpleNamespace(last_price=100.0) for symbol in symbols}

    monkeypatch.setattr("application.execution_service.time.sleep", lambda _seconds: None)

    trade_logs, summary = execute_rebalance(
        FakeIB(),
        {"VOO": 0.8, "BOXX": 0.2},
        {},
        {"equity": 1000.0, "buying_power": 1000.0},
        fetch_quote_snapshots=fake_fetch_quote_snapshots,
        submit_order_intent=lambda *_args, **_kwargs: SimpleNamespace(broker_order_id="1", status="Submitted"),
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["VOO", "BOXX"],
        strategy_profile="tech_pullback_cash_buffer",
        account_group="default",
        service_name="ibkr-paper",
        account_ids=("DU123",),
        signal_metadata=_signal_metadata(
            {"VOO": 0.8, "BOXX": 0.2},
            risk_symbols=("VOO",),
            safe_haven_symbols=("BOXX",),
            regime="risk_on",
            breadth_ratio=0.6,
            target_stock_weight=0.8,
            realized_stock_weight=0.8,
            safe_haven_weight=0.2,
            safe_haven_symbol="BOXX",
            trade_date="2026-04-01",
            snapshot_as_of="2026-03-31",
        ),
        dry_run_only=True,
        cash_reserve_ratio=0.0,
        rebalance_threshold_ratio=0.02,
        limit_buy_premium=1.005,
        sell_settle_delay_sec=0,
        execution_lock_dir=tmp_path,
        return_summary=True,
    )

    assert any("execution_lock_acquired" in log for log in trade_logs)
    assert summary["execution_status"] == "executed"
    assert summary["mode"] == "dry_run"
    assert summary["safe_haven_symbol"] == "BOXX"
    assert summary["orders_submitted"]
    assert summary["target_vs_current"]


def test_execute_rebalance_blocks_when_material_target_has_missing_prices():
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="5000")]

    trade_logs, summary = execute_rebalance(
        FakeIB(),
        {"VOO": 1.0},
        {},
        {"equity": 1000.0, "buying_power": 1000.0},
        fetch_quote_snapshots=lambda *_args, **_kwargs: {},
        submit_order_intent=lambda *_args, **_kwargs: None,
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["VOO"],
        strategy_profile="tech_pullback_cash_buffer",
        signal_metadata=_signal_metadata(
            {"VOO": 1.0},
            risk_symbols=("VOO",),
            trade_date="2026-04-01",
            snapshot_as_of="2026-03-31",
        ),
        dry_run_only=True,
        cash_reserve_ratio=0.0,
        rebalance_threshold_ratio=0.02,
        limit_buy_premium=1.005,
        sell_settle_delay_sec=0,
        return_summary=True,
    )

    assert summary["execution_status"] == "blocked"
    assert summary["no_op_reason"] == "missing_price:VOO"
    assert summary["orders_skipped"] == [{"symbol": "VOO", "reason": "missing_price"}]
    assert "failed missing_price:VOO" in trade_logs[-1]


def test_execute_rebalance_blocks_when_material_target_has_no_buying_power():
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="0")]

    trade_logs, summary = execute_rebalance(
        FakeIB(),
        {"VOO": 1.0},
        {},
        {"equity": 1000.0, "buying_power": 0.0},
        fetch_quote_snapshots=lambda *_args, **_kwargs: {"VOO": SimpleNamespace(last_price=100.0)},
        submit_order_intent=lambda *_args, **_kwargs: None,
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["VOO"],
        strategy_profile="tech_pullback_cash_buffer",
        signal_metadata=_signal_metadata(
            {"VOO": 1.0},
            risk_symbols=("VOO",),
            trade_date="2026-04-01",
            snapshot_as_of="2026-03-31",
        ),
        dry_run_only=True,
        cash_reserve_ratio=0.0,
        rebalance_threshold_ratio=0.02,
        limit_buy_premium=1.005,
        sell_settle_delay_sec=0,
        return_summary=True,
    )

    assert summary["execution_status"] == "blocked"
    assert summary["no_op_reason"] == "insufficient_buying_power:VOO"
    assert summary["skipped_reasons"] == ["insufficient_buying_power:VOO"]
    assert "failed insufficient_buying_power:VOO" in trade_logs[-1]


def test_execute_rebalance_uses_snapshot_prices_for_dry_run_when_quotes_missing(tmp_path):
    class FakeIB:
        def openTrades(self):
            return []

        def fills(self):
            return []

        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="5000")]

    trade_logs, summary = execute_rebalance(
        FakeIB(),
        {"VOO": 0.6, "BOXX": 0.4},
        {},
        {"equity": 1000.0, "buying_power": 1000.0},
        fetch_quote_snapshots=lambda *_args, **_kwargs: {},
        submit_order_intent=lambda *_args, **_kwargs: None,
        order_intent_cls=OrderIntent,
        translator=translate,
        strategy_symbols=["VOO", "BOXX"],
        strategy_profile="tech_pullback_cash_buffer",
        signal_metadata=_signal_metadata(
            {"VOO": 0.6, "BOXX": 0.4},
            risk_symbols=("VOO",),
            safe_haven_symbols=("BOXX",),
            trade_date="2026-04-01",
            snapshot_as_of="2026-03-31",
            dry_run_price_fallbacks={"VOO": 100.0, "BOXX": 100.0},
        ),
        dry_run_only=True,
        cash_reserve_ratio=0.0,
        rebalance_threshold_ratio=0.02,
        limit_buy_premium=1.005,
        sell_settle_delay_sec=0,
        execution_lock_dir=tmp_path,
        return_summary=True,
    )

    assert summary["execution_status"] == "executed"
    assert len(summary["orders_submitted"]) == 2
    assert summary["snapshot_price_fallback_used"] is True
    assert summary["snapshot_price_fallback_count"] == 2
    assert set(summary["snapshot_price_fallback_symbols"]) == {"VOO", "BOXX"}
    assert summary["price_source_mode"] == "mixed_market_quote_snapshot_close"
    assert any(log.startswith("dry_run_snapshot_prices count=2") for log in trade_logs)
    assert any(log.startswith("DRY_RUN buy VOO") for log in trade_logs)
    assert any(log.startswith("DRY_RUN buy BOXX") for log in trade_logs)
