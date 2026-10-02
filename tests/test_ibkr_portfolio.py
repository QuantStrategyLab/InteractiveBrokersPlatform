from types import SimpleNamespace

import pytest

from application import ibkr_portfolio
from application.ibkr_portfolio import fetch_portfolio_snapshot


class SnapshotIB:
    def __init__(self, positions, portfolio, account_values):
        self._positions = positions
        self._portfolio = portfolio
        self._account_values = account_values

    def reqPositions(self):
        pass

    def positions(self):
        return self._positions

    def portfolio(self, account=""):
        return [
            item for item in self._portfolio
            if not account or item.account == account
        ]

    def accountValues(self):
        return self._account_values


def _stock_position(account, symbol, con_id, quantity, average_cost, currency="USD"):
    return SimpleNamespace(
        account=account,
        contract=SimpleNamespace(
            secType="STK", symbol=symbol, currency=currency, conId=con_id
        ),
        position=quantity,
        avgCost=average_cost,
    )


def _stock_mark(account, symbol, con_id, quantity, market_value, currency="USD"):
    return SimpleNamespace(
        account=account,
        contract=SimpleNamespace(
            secType="STK", symbol=symbol, currency=currency, conId=con_id
        ),
        position=quantity,
        marketValue=market_value,
    )


def _usd_account_values(account, cash, nlv):
    return [
        SimpleNamespace(account=account, currency="USD", tag="NetLiquidation", value=str(nlv)),
        SimpleNamespace(account=account, currency="USD", tag="CashBalance", value=str(cash)),
        SimpleNamespace(account=account, currency="USD", tag="AvailableFunds", value=str(cash)),
    ]


class FakeIB:
    def __init__(self):
        self.req_positions_called = 0

    def reqPositions(self):
        self.req_positions_called += 1

    def positions(self):
        return [
            SimpleNamespace(
                account="UHK123",
                contract=SimpleNamespace(secType="STK", symbol="00700", currency="HKD", conId=700),
                position=100,
                avgCost=320.5,
            ),
            SimpleNamespace(
                account="UUS999",
                contract=SimpleNamespace(secType="STK", symbol="AAPL", currency="USD", conId=999),
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
            SimpleNamespace(account="UHK123", currency="HKD", tag="CashBalance", value="0"),
            SimpleNamespace(account="UHK123", currency="USD", tag="NetLiquidation", value="999"),
            SimpleNamespace(account="UUS999", currency="HKD", tag="NetLiquidation", value="123"),
        ]

    def portfolio(self, account=""):
        return [
            SimpleNamespace(
                account=position.account,
                contract=position.contract,
                position=position.position,
                marketValue=position.position * position.avgCost,
                averageCost=position.avgCost,
            )
            for position in self.positions()
            if position.contract.secType == "STK"
            and (not account or position.account == account)
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
    assert snapshot.total_equity == 32050.0
    assert snapshot.buying_power == 0.0
    assert len(snapshot.positions) == 1
    assert snapshot.positions[0].symbol == "00700"
    assert snapshot.positions[0].currency == "HKD"
    assert snapshot.metadata["currency"] == "HKD"
    assert snapshot.metadata["account_ids"] == ("UHK123",)
    assert snapshot.metadata["account_hash"] == "UHK123"
    assert snapshot.metadata["total_equity_source"] == "broker_net_liquidation"
    assert snapshot.metadata["broker_net_liquidation"] == 999.0
    assert isinstance(snapshot.metadata["source_digest_sha256"], str)
    assert len(snapshot.metadata["source_digest_sha256"]) == 64
    assert snapshot.metadata["option_positions"][0]["currency"] == "HKD"


def test_fetch_portfolio_snapshot_prefers_market_currency_cash_balance():
    class MultiCurrencyIB(FakeIB):
        def positions(self):
            return []

        def accountValues(self):
            return [
                SimpleNamespace(account="U00000000", currency="USD", tag="NetLiquidation", value="1130"),
                SimpleNamespace(account="U00000000", currency="USD", tag="AvailableFunds", value="885.99"),
                SimpleNamespace(account="U00000000", currency="USD", tag="CashBalance", value="477.10"),
                SimpleNamespace(account="U00000000", currency="HKD", tag="CashBalance", value="408.98"),
            ]

    snapshot = fetch_portfolio_snapshot(
        MultiCurrencyIB(),
        account_ids=("U00000000",),
        wait_seconds=0,
        currency="USD",
    )

    assert snapshot.total_equity == 477.10
    assert snapshot.buying_power == 477.10
    assert snapshot.metadata["market_currency_cash"] == 477.10
    assert snapshot.metadata["available_funds"] == 885.99


def test_fetch_portfolio_snapshot_supports_ibkr_ledger_cash_balance():
    class LedgerCashIB(FakeIB):
        def positions(self):
            return []

        def accountValues(self):
            return [
                SimpleNamespace(account="U00000000", currency="USD", tag="NetLiquidation", value="1130"),
                SimpleNamespace(account="U00000000", currency="USD", tag="AvailableFunds", value="885.99"),
                SimpleNamespace(
                    account="U00000000",
                    currency="USD",
                    tag="$LEDGER-CashBalance",
                    value="477.10",
                ),
                SimpleNamespace(account="U00000000", currency="USD", tag="TotalCashValue", value="500.00"),
            ]

    snapshot = fetch_portfolio_snapshot(
        LedgerCashIB(),
        account_ids=("U00000000",),
        wait_seconds=0,
        currency="USD",
    )

    assert snapshot.total_equity == 477.10
    assert snapshot.buying_power == 477.10
    assert snapshot.metadata["market_currency_cash"] == 477.10


def test_fetch_portfolio_snapshot_rejects_total_cash_value_as_currency_cash():
    class AggregateCashIB(FakeIB):
        def positions(self):
            return []

        def accountValues(self):
            return [
                SimpleNamespace(account="U00000000", currency="USD", tag="NetLiquidation", value="1130"),
                SimpleNamespace(account="U00000000", currency="USD", tag="TotalCashValue", value="500.00"),
            ]

    with pytest.raises(
        ibkr_portfolio.IBKRPortfolioSnapshotUnavailableError,
        match="cash balance",
    ):
        fetch_portfolio_snapshot(
            AggregateCashIB(),
            account_ids=("U00000000",),
            wait_seconds=0,
            currency="USD",
        )


def test_fetch_portfolio_snapshot_does_not_treat_base_net_liquidation_as_usd():
    class BaseNetLiquidationIB(FakeIB):
        def positions(self):
            return []

        def accountValues(self):
            return [
                SimpleNamespace(account="U00000001", currency="BASE", tag="NetLiquidation", value="371.93"),
                SimpleNamespace(account="U00000001", currency="USD", tag="CashBalance", value="371.93"),
                SimpleNamespace(account="U00000001", currency="USD", tag="AvailableFunds", value="371.93"),
            ]

    snapshot = fetch_portfolio_snapshot(
        BaseNetLiquidationIB(),
        account_ids=("U00000001",),
        wait_seconds=0,
        currency="USD",
    )

    assert snapshot.metadata["total_equity_source"] == "unverified_net_liquidation"
    assert "broker_net_liquidation" not in snapshot.metadata
    assert snapshot.total_equity == 371.93
    assert snapshot.metadata["account_hash"] == "U00000001"
    assert "source_digest_sha256" not in snapshot.metadata


def test_fetch_portfolio_snapshot_reports_usd_nlv_difference_without_mutating_cash():
    class DriftedMarksIB(FakeIB):
        def positions(self):
            return [
                SimpleNamespace(
                    account="U00000001",
                    contract=SimpleNamespace(secType="STK", symbol="SOXL", currency="USD", conId=999),
                    position=3,
                    avgCost=150.0,
                )
            ]

        def accountValues(self):
            return [
                SimpleNamespace(account="U00000001", currency="USD", tag="NetLiquidation", value="472.0"),
                SimpleNamespace(account="U00000001", currency="USD", tag="CashBalance", value="40.0"),
                SimpleNamespace(account="U00000001", currency="USD", tag="AvailableFunds", value="40.0"),
            ]

    snapshot = fetch_portfolio_snapshot(
        DriftedMarksIB(),
        account_ids=("U00000001",),
        wait_seconds=0,
        currency="USD",
        cash_only_execution=True,
    )

    # Position marks plus broker cash remain the strategy book; NLV is separate evidence.
    assert snapshot.metadata["broker_net_liquidation"] == 472.0
    assert snapshot.metadata["strategy_equity_minus_broker_nlv"] == 18.0
    assert snapshot.total_equity == 490.0
    assert snapshot.metadata["market_currency_cash"] == 40.0
    assert snapshot.buying_power == 40.0


@pytest.mark.parametrize(("market_value", "expected_equity"), [(1200.0, 2200.0), (800.0, 1800.0)])
def test_fetch_portfolio_snapshot_uses_current_market_value_without_rewriting_cash(
    market_value, expected_equity
):
    snapshot = fetch_portfolio_snapshot(
        SnapshotIB(
            [_stock_position("U00000001", "XYZ", 123, 10, 100.0)],
            [_stock_mark("U00000001", "XYZ", 123, 10, market_value)],
            _usd_account_values("U00000001", 1000.0, expected_equity),
        ),
        account_ids="U00000001",
        wait_seconds=0,
    )

    assert snapshot.positions[0].market_value == market_value
    assert snapshot.positions[0].average_cost == 100.0
    assert snapshot.total_equity == expected_equity
    assert snapshot.metadata["market_currency_cash"] == 1000.0
    assert snapshot.buying_power == 1000.0
    assert snapshot.metadata["strategy_equity"] == expected_equity
    assert snapshot.metadata["strategy_equity_source"] == "ibkr_cached_portfolio_market_value_plus_account_cash"
    assert snapshot.metadata["position_market_value_source"] == "ibkr_cached_portfolio_market_value"
    assert snapshot.metadata["broker_net_liquidation"] == expected_equity
    assert snapshot.metadata["broker_net_liquidation_source"] == "accountValues:USD:NetLiquidation"
    assert snapshot.metadata["strategy_equity_minus_broker_nlv"] == 0.0
    assert snapshot.metadata["portfolio_mark_observed_at"] == snapshot.as_of.isoformat()


@pytest.mark.parametrize(
    ("marks", "expected_error"),
    [
        ([], "mark is missing"),
        ([_stock_mark("U1", "XYZ", 123, 10, float("nan"))], "finite market value"),
        ([_stock_mark("U1", "XYZ", 123, 10, float("inf"))], "finite market value"),
        ([_stock_mark("U1", "XYZ", 123, 9, 900.0)], "quantities do not match"),
        ([_stock_mark("U2", "XYZ", 123, 10, 1200.0)], "mark is missing"),
        ([_stock_mark("U1", "XYZ", 123, 10, 1200.0, currency="EUR")], "currency does not match"),
    ],
)
def test_fetch_portfolio_snapshot_fails_closed_on_untrusted_mark(marks, expected_error):
    ib = SnapshotIB(
        [_stock_position("U1", "XYZ", 123, 10, 100.0)],
        marks,
        _usd_account_values("U1", 1000.0, 2200.0),
    )

    with pytest.raises(ibkr_portfolio.IBKRPortfolioSnapshotUnavailableError, match=expected_error):
        fetch_portfolio_snapshot(ib, account_ids="U1", wait_seconds=0)


def test_fetch_portfolio_snapshot_preserves_short_market_value_and_quantity():
    snapshot = fetch_portfolio_snapshot(
        SnapshotIB(
            [_stock_position("U1", "XYZ", 123, -10, 50.0)],
            [_stock_mark("U1", "XYZ", 123, -10, -400.0)],
            _usd_account_values("U1", 1000.0, 600.0),
        ),
        account_ids="U1",
        wait_seconds=0,
    )

    assert snapshot.positions[0].quantity == -10.0
    assert snapshot.positions[0].market_value == -400.0
    assert snapshot.total_equity == 600.0


def test_fetch_portfolio_snapshot_aggregates_same_symbol_across_accounts():
    snapshot = fetch_portfolio_snapshot(
        SnapshotIB(
            [
                _stock_position("U1", "XYZ", 123, 10, 100.0),
                _stock_position("U2", "XYZ", 123, 5, 80.0),
            ],
            [
                _stock_mark("U1", "XYZ", 123, 10, 1200.0),
                _stock_mark("U2", "XYZ", 123, 5, 400.0),
            ],
            _usd_account_values("U1", 1000.0, 2200.0)
            + _usd_account_values("U2", 500.0, 900.0),
        ),
        account_ids=("U1", "U2"),
        wait_seconds=0,
    )

    assert len(snapshot.positions) == 1
    assert snapshot.positions[0].quantity == 15.0
    assert snapshot.positions[0].market_value == 1600.0
    assert snapshot.positions[0].average_cost == pytest.approx(1400.0 / 15.0)
    assert snapshot.positions[0].account_id is None
    assert snapshot.total_equity == 3100.0
    assert snapshot.metadata["market_currency_cash"] == 1500.0


def test_fetch_portfolio_snapshot_rejects_mixed_currency_stock_positions():
    ib = SnapshotIB(
        [_stock_position("U1", "XYZ", 123, 10, 100.0, currency="EUR")],
        [_stock_mark("U1", "XYZ", 123, 10, 1100.0, currency="EUR")],
        _usd_account_values("U1", 1000.0, 2100.0),
    )

    with pytest.raises(ibkr_portfolio.IBKRPortfolioSnapshotUnavailableError, match="another or unknown currency"):
        fetch_portfolio_snapshot(ib, account_ids="U1", wait_seconds=0)


def test_fetch_portfolio_snapshot_does_not_treat_futures_as_stock_positions():
    position = SimpleNamespace(
        account="U1",
        contract=SimpleNamespace(secType="FUT", symbol="ES", currency="USD", conId=123),
        position=1,
        avgCost=5000.0,
    )
    ib = SnapshotIB([position], [], _usd_account_values("U1", 1000.0, 6000.0))

    with pytest.raises(ibkr_portfolio.IBKRPortfolioSnapshotUnavailableError, match="does not support position type FUT"):
        fetch_portfolio_snapshot(ib, account_ids="U1", wait_seconds=0)


def test_fetch_portfolio_snapshot_prefers_usd_net_liquidation_over_base():
    class DualNetLiquidationIB(FakeIB):
        def positions(self):
            return []

        def accountValues(self):
            return [
                SimpleNamespace(account="U00000001", currency="BASE", tag="NetLiquidation", value="999.0"),
                SimpleNamespace(account="U00000001", currency="USD", tag="NetLiquidation", value="371.93"),
                SimpleNamespace(account="U00000001", currency="USD", tag="CashBalance", value="371.93"),
            ]

    snapshot = fetch_portfolio_snapshot(
        DualNetLiquidationIB(),
        account_ids=("U00000001",),
        wait_seconds=0,
        currency="USD",
    )

    assert snapshot.metadata["broker_net_liquidation"] == 371.93


def test_fetch_portfolio_snapshot_allows_negative_cash_balance():
    class NegativeCashIB(FakeIB):
        def positions(self):
            return []

        def accountValues(self):
            return [
                SimpleNamespace(account="U00000000", currency="USD", tag="NetLiquidation", value="2160"),
                SimpleNamespace(account="U00000000", currency="USD", tag="AvailableFunds", value="1588.89"),
                SimpleNamespace(account="U00000000", currency="USD", tag="CashBalance", value="-284.0"),
            ]

    snapshot = fetch_portfolio_snapshot(
        NegativeCashIB(),
        account_ids=("U00000000",),
        wait_seconds=0,
        currency="USD",
    )

    assert snapshot.buying_power == -284.0
    assert snapshot.metadata["market_currency_cash"] == -284.0


def test_fetch_portfolio_snapshot_rejects_incomplete_cash_only_account_data():
    class MissingCashIB(FakeIB):
        def positions(self):
            return []

        def accountValues(self):
            return [
                SimpleNamespace(
                    account="U00000000",
                    currency="USD",
                    tag="NetLiquidation",
                    value="1130",
                )
            ]

    with pytest.raises(
        ibkr_portfolio.IBKRPortfolioSnapshotUnavailableError,
        match="cash balance",
    ):
        fetch_portfolio_snapshot(
            MissingCashIB(),
            account_ids=("U00000000",),
            wait_seconds=0,
            currency="USD",
        )


def test_fetch_portfolio_snapshot_rejects_missing_cash_when_positions_exist():
    class MissingCashWithPositionIB(FakeIB):
        def positions(self):
            return [
                SimpleNamespace(
                    account="UUS999",
                    contract=SimpleNamespace(secType="STK", symbol="AAPL", currency="USD", conId=999),
                    position=5,
                    avgCost=190.0,
                )
            ]

        def accountValues(self):
            return [
                SimpleNamespace(
                    account="UUS999",
                    currency="USD",
                    tag="NetLiquidation",
                    value="1130",
                )
            ]

    with pytest.raises(
        ibkr_portfolio.IBKRPortfolioSnapshotUnavailableError,
        match="cash balance",
    ):
        fetch_portfolio_snapshot(
            MissingCashWithPositionIB(),
            account_ids=("UUS999",),
            wait_seconds=0,
            currency="USD",
        )


def test_fetch_portfolio_snapshot_allows_explicit_zero_cash_balance():
    class ZeroCashIB(FakeIB):
        def positions(self):
            return []

        def accountValues(self):
            return [
                SimpleNamespace(
                    account="U00000000",
                    currency="USD",
                    tag="CashBalance",
                    value="0",
                )
            ]

    snapshot = fetch_portfolio_snapshot(
        ZeroCashIB(),
        account_ids=("U00000000",),
        wait_seconds=0,
        currency="USD",
    )

    assert snapshot.total_equity == 0.0
    assert snapshot.metadata["market_currency_cash"] == 0.0
