from types import SimpleNamespace

from application.ibkr_portfolio import fetch_portfolio_snapshot


class FakeIB:
    def __init__(self):
        self.req_positions_called = 0

    def reqPositions(self):
        self.req_positions_called += 1

    def positions(self):
        return [
            SimpleNamespace(
                account="UHK123",
                contract=SimpleNamespace(secType="STK", symbol="00700", currency="HKD"),
                position=100,
                avgCost=320.5,
            ),
            SimpleNamespace(
                account="UUS999",
                contract=SimpleNamespace(secType="STK", symbol="AAPL", currency="USD"),
                position=5,
                avgCost=190.0,
            ),
            SimpleNamespace(
                account="UHK123",
                contract=SimpleNamespace(
                    secType="OPT",
                    symbol="00700",
                    currency="HKD",
                    localSymbol="TCEHY 260619C00350000",
                    lastTradeDateOrContractMonth="20260619",
                    right="C",
                    strike=350.0,
                ),
                position=1,
                avgCost=12.0,
            ),
        ]

    def accountValues(self):
        return [
            SimpleNamespace(account="UHK123", currency="HKD", tag="NetLiquidation", value="100000"),
            SimpleNamespace(account="UHK123", currency="HKD", tag="AvailableFunds", value="80000"),
            SimpleNamespace(account="UHK123", currency="USD", tag="NetLiquidation", value="999"),
            SimpleNamespace(account="UUS999", currency="HKD", tag="NetLiquidation", value="123"),
        ]


def test_fetch_portfolio_snapshot_filters_account_and_market_currency():
    ib = FakeIB()

    snapshot = fetch_portfolio_snapshot(
        ib,
        account_ids=("UHK123",),
        wait_seconds=0,
        currency="HKD",
    )

    assert ib.req_positions_called == 1
    assert snapshot.total_equity == 100000.0
    assert snapshot.buying_power == 80000.0
    assert len(snapshot.positions) == 1
    assert snapshot.positions[0].symbol == "00700"
    assert snapshot.positions[0].currency == "HKD"
    assert snapshot.metadata["currency"] == "HKD"
    assert snapshot.metadata["account_ids"] == ("UHK123",)
    assert snapshot.metadata["option_positions"][0]["currency"] == "HKD"
