from types import SimpleNamespace

from quant_platform_kit.common.models import OrderIntent

from application.ibkr_order_execution import submit_order_intent


class FakeMarketOrder:
    def __init__(self, side, quantity):
        self.action = side
        self.totalQuantity = quantity
        self.tif = ""


class FakeIB:
    def __init__(self):
        self.placed_order = None

    def qualifyContracts(self, _contract):
        return None

    def placeOrder(self, _contract, order):
        self.placed_order = order
        return SimpleNamespace(
            order=SimpleNamespace(orderId=42),
            orderStatus=SimpleNamespace(status="Submitted", filled=0, avgFillPrice=0),
        )


def fake_stock(symbol, exchange, currency):
    return SimpleNamespace(symbol=symbol, exchange=exchange, currency=currency)


def test_submit_order_intent_sets_default_day_tif_on_market_orders():
    ib = FakeIB()

    report = submit_order_intent(
        ib,
        OrderIntent(symbol="AAPL", side="sell", quantity=3),
        wait_seconds=0,
        stock_factory=fake_stock,
        market_order_factory=FakeMarketOrder,
    )

    assert ib.placed_order.tif == "DAY"
    assert report.status == "Submitted"
    assert report.raw_payload["time_in_force"] == "DAY"


def test_submit_order_intent_preserves_explicit_tif_on_market_orders():
    ib = FakeIB()

    submit_order_intent(
        ib,
        OrderIntent(symbol="AAPL", side="sell", quantity=3, time_in_force="GTC"),
        wait_seconds=0,
        stock_factory=fake_stock,
        market_order_factory=FakeMarketOrder,
    )

    assert ib.placed_order.tif == "GTC"
