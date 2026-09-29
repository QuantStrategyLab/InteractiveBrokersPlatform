from datetime import datetime, timezone
from types import SimpleNamespace

from application.account_facts import build_ibkr_account_facts


def _snapshot(*, observed_at=None, account_ids=("DU123",), cash_balances=(), **metadata):
    return SimpleNamespace(
        as_of=observed_at,
        metadata={
            "account_ids": account_ids,
            "cash_balances": cash_balances,
            **metadata,
        },
    )


def test_preserves_native_cash_tags_and_trusted_usd_net_assets():
    snapshot = _snapshot(
        observed_at=datetime(2026, 9, 30, 12, tzinfo=timezone.utc),
        total_equity_source="broker_net_liquidation",
        broker_net_liquidation=125.00001,
        cash_balances=(
            {"account_id": "DU123", "currency": "USD", "NetLiquidation": 125.00001, "$LEDGER-CashBalance": 0, "CashBalance": 99},
            {"account_id": "DU123", "currency": "EUR", "$LEDGER-TotalCashBalance": -1.25},
            {"account_id": "DU123", "currency": "JPY", "SettledCash": 1e-8},
            {"account_id": "DU123", "currency": "BASE", "CashBalance": 9},
            {"account_id": "DU123", "currency": "BASE", "CashBalance": 10},
            {"account_id": "DU123", "currency": "GBP", "CashBalance": 2.5},
            {"account_id": "DU123", "currency": "CAD", "TotalCashBalance": 3.5},
        ),
    )
    facts = build_ibkr_account_facts(snapshot)
    assert facts["account_ids"] == ["DU123"]
    assert facts["currency"] == "USD"
    assert facts["net_assets"] == "125.00001"
    assert facts["cash"] == [
        {"currency": "USD", "cash_balance": "0", "source_tag": "$LEDGER-CashBalance"},
        {"currency": "EUR", "cash_balance": "-1.25", "source_tag": "$LEDGER-TotalCashBalance"},
        {"currency": "JPY", "cash_balance": "0.00000001", "source_tag": "SettledCash"},
        {"currency": "GBP", "cash_balance": "2.5", "source_tag": "CashBalance"},
        {"currency": "CAD", "cash_balance": "3.5", "source_tag": "TotalCashBalance"},
    ]


def test_rejects_unbound_or_incomplete_account_facts():
    observed_at = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
    valid_cash = ({"account_id": "DU123", "currency": "USD", "CashBalance": 1},)
    assert build_ibkr_account_facts(_snapshot(observed_at=observed_at, account_ids=("DU123", "DU456"), cash_balances=valid_cash)) == {}
    assert build_ibkr_account_facts(_snapshot(observed_at=observed_at, cash_balances=valid_cash + ({"account_id": "DU456", "currency": "EUR", "CashBalance": 2},))) == {}
    assert build_ibkr_account_facts(_snapshot(observed_at=datetime(2026, 9, 30, 12), cash_balances=valid_cash)) == {}
    assert build_ibkr_account_facts(_snapshot(cash_balances=valid_cash)) == {}
    assert build_ibkr_account_facts(_snapshot(observed_at=observed_at, cash_balances=({"account_id": "DU123", "currency": "USD"},))) == {}


def test_rejects_duplicate_or_invalid_native_currencies():
    observed_at = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
    duplicated = (
        {"account_id": "DU123", "currency": "USD", "CashBalance": 1},
        {"account_id": "DU123", "currency": "USD", "SettledCash": 2},
    )
    invalid = ({"account_id": "DU123", "currency": "US D", "CashBalance": 1},)
    assert build_ibkr_account_facts(_snapshot(observed_at=observed_at, cash_balances=duplicated)) == {}
    assert build_ibkr_account_facts(_snapshot(observed_at=observed_at, cash_balances=invalid)) == {}


def test_unverified_net_assets_remain_null_with_usd_currency_semantics():
    snapshot = _snapshot(
        observed_at=datetime(2026, 9, 30, 12, tzinfo=timezone.utc),
        cash_balances=({"account_id": "DU123", "currency": "USD", "CashBalance": 1},),
        total_equity_source="unverified_net_liquidation",
        broker_net_liquidation=999,
    )
    facts = build_ibkr_account_facts(snapshot)
    assert facts["currency"] == "USD"
    assert facts["net_assets"] is None


def test_usd_net_assets_require_matching_explicit_usd_net_liquidation():
    observed_at = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
    cash = (
        {"account_id": "DU123", "currency": "USD", "NetLiquidation": 100, "CashBalance": 1},
        {"account_id": "DU123", "currency": "HKD", "CashBalance": 2},
    )
    matched = build_ibkr_account_facts(_snapshot(
        observed_at=observed_at,
        cash_balances=cash,
        total_equity_source="broker_net_liquidation",
        broker_net_liquidation=100,
    ))
    assert matched["net_assets"] == "100"

    mismatch = build_ibkr_account_facts(_snapshot(
        observed_at=observed_at,
        cash_balances=cash,
        total_equity_source="broker_net_liquidation",
        broker_net_liquidation=101,
    ))
    assert mismatch["net_assets"] is None
    assert mismatch["cash"] == [
        {"currency": "USD", "cash_balance": "1", "source_tag": "CashBalance"},
        {"currency": "HKD", "cash_balance": "2", "source_tag": "CashBalance"},
    ]

    base_only = build_ibkr_account_facts(_snapshot(
        observed_at=observed_at,
        cash_balances=(
            {"account_id": "DU123", "currency": "BASE", "NetLiquidation": 100},
            {"account_id": "DU123", "currency": "HKD", "CashBalance": 2},
        ),
        total_equity_source="broker_net_liquidation",
        broker_net_liquidation=100,
    ))
    assert base_only["net_assets"] is None
    assert base_only["cash"] == [{"currency": "HKD", "cash_balance": "2", "source_tag": "CashBalance"}]

    missing = build_ibkr_account_facts(_snapshot(
        observed_at=observed_at,
        cash_balances=({"account_id": "DU123", "currency": "USD", "CashBalance": 1},),
        total_equity_source="broker_net_liquidation",
        broker_net_liquidation=100,
    ))
    assert missing["net_assets"] is None
