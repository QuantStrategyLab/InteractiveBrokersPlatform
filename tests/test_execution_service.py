from types import SimpleNamespace

from application.execution_service import check_order_submitted, execute_rebalance
from quant_platform_kit.common.models import OrderIntent


def translate(key, **kwargs):
    templates = {
        "submitted": "submitted {order_id}",
        "failed": "failed {reason}",
        "market_sell": "sell {symbol} {qty}",
        "limit_buy": "buy {symbol} {qty} @{price}",
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
        signal_metadata={
            "regime": "risk_on",
            "breadth_ratio": 0.6,
            "target_stock_weight": 0.8,
            "realized_stock_weight": 0.8,
            "trade_date": "2026-04-01",
            "snapshot_as_of": "2026-03-31",
        },
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
        signal_metadata={},
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
        signal_metadata={
            "regime": "risk_on",
            "breadth_ratio": 0.6,
            "target_stock_weight": 0.8,
            "realized_stock_weight": 0.8,
            "trade_date": "2026-04-01",
            "snapshot_as_of": "2026-03-31",
        },
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
        signal_metadata={"trade_date": "2026-04-01"},
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
        signal_metadata={
            "regime": "risk_on",
            "breadth_ratio": 0.6,
            "target_stock_weight": 0.8,
            "realized_stock_weight": 0.8,
            "safe_haven_weight": 0.2,
            "safe_haven_symbol": "BOXX",
            "trade_date": "2026-04-01",
            "snapshot_as_of": "2026-03-31",
        },
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
