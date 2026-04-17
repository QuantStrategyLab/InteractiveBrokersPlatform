from types import SimpleNamespace

from quant_platform_kit.common.models import OrderIntent

from application.paper_liquidation_service import build_liquidation_intents, execute_paper_liquidation


def test_build_liquidation_intents_sells_longs_and_buys_shorts():
    positions = {
        "AAPL": {"symbol": "AAPL", "quantity": 3},
        "TSLA": {"symbol": "TSLA", "quantity": -2},
        "CASH": {"symbol": "CASH", "quantity": 0},
    }

    intents = build_liquidation_intents(positions, order_intent_cls=OrderIntent)

    assert [(intent.symbol, intent.side, intent.quantity) for intent in intents] == [
        ("AAPL", "sell", 3),
        ("TSLA", "buy", 2),
    ]


def test_execute_paper_liquidation_supports_dry_run():
    positions = {"AAPL": {"symbol": "AAPL", "quantity": 3}}

    summary = execute_paper_liquidation(
        object(),
        positions,
        submit_order_intent=lambda *_args: None,
        order_intent_cls=OrderIntent,
        dry_run_only=True,
    )

    assert summary["mode"] == "dry_run"
    assert summary["execution_status"] == "dry_run"
    assert summary["orders_submitted"] == [
        {"symbol": "AAPL", "side": "sell", "quantity": 3, "status": "dry_run"}
    ]


def test_execute_paper_liquidation_submits_market_orders():
    submitted = []

    def submit(_ib, intent):
        submitted.append(intent)
        return SimpleNamespace(
            symbol=intent.symbol,
            side=intent.side,
            quantity=intent.quantity,
            status="Submitted",
            broker_order_id="1",
        )

    summary = execute_paper_liquidation(
        object(),
        {"AAPL": {"symbol": "AAPL", "quantity": 3}},
        submit_order_intent=submit,
        order_intent_cls=OrderIntent,
        dry_run_only=False,
    )

    assert [(intent.symbol, intent.side, intent.quantity) for intent in submitted] == [("AAPL", "sell", 3)]
    assert summary["execution_status"] == "submitted"
    assert summary["orders_submitted"][0]["symbol"] == "AAPL"
