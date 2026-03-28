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


def test_execute_rebalance_submits_limit_buy_for_underweight_position(monkeypatch):
    class FakeIB:
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
        ranking_pool=["VOO"],
        safe_haven="BIL",
        cash_reserve_ratio=0.03,
        rebalance_threshold_ratio=0.02,
        limit_buy_premium=1.005,
        sell_settle_delay_sec=0,
    )

    assert len(submitted) == 1
    assert submitted[0].side == "buy"
    assert submitted[0].symbol == "VOO"
    assert submitted[0].order_type == "limit"
    assert trade_logs and trade_logs[0].startswith("buy VOO")
