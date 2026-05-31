"""IBKR portfolio snapshot helpers with market-currency awareness."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from quant_platform_kit.common.models import PortfolioSnapshot, Position


def _normalize_account_ids(account_ids: Iterable[str] | str | None) -> tuple[str, ...]:
    if account_ids is None:
        return ()
    if isinstance(account_ids, str):
        candidates = [account_ids]
    else:
        candidates = list(account_ids)
    normalized = []
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text:
            normalized.append(text)
    return tuple(dict.fromkeys(normalized))


def _matches_account(account_id: str | None, selected_account_ids: tuple[str, ...]) -> bool:
    if not selected_account_ids:
        return True
    return str(account_id or "").strip() in selected_account_ids


def fetch_portfolio_snapshot(
    ib: Any,
    *,
    account_ids: Iterable[str] | str | None = None,
    wait_seconds: float = 1.0,
    currency: str = "USD",
) -> PortfolioSnapshot:
    """Fetch stock positions and account values for the configured trading currency.

    QuantPlatformKit's default IBKR helper is USD-oriented.  Keeping this small
    adapter local lets the platform run US and HK services without changing the
    shared package release line.
    """

    selected_account_ids = _normalize_account_ids(account_ids)
    market_currency = str(currency or "USD").strip().upper()
    ib.reqPositions()
    if wait_seconds:
        import time as time_module

        time_module.sleep(wait_seconds)

    positions = []
    option_positions = []
    for raw_position in ib.positions():
        account_id = str(getattr(raw_position, "account", "") or "").strip() or None
        if not _matches_account(account_id, selected_account_ids):
            continue
        if raw_position.position == 0:
            continue
        contract = raw_position.contract
        quantity = float(raw_position.position)
        average_cost = float(raw_position.avgCost)
        contract_currency = str(getattr(contract, "currency", "") or "").strip().upper()
        if not contract_currency:
            contract_currency = market_currency
        if str(getattr(contract, "secType", "") or "").strip().upper() == "OPT":
            option_positions.append(
                {
                    "underlier": str(getattr(contract, "symbol", "") or "").strip().upper(),
                    "local_symbol": str(getattr(contract, "localSymbol", "") or "").strip(),
                    "expiration": str(
                        getattr(contract, "lastTradeDateOrContractMonth", "") or ""
                    ).strip(),
                    "right": str(getattr(contract, "right", "") or "").strip().upper(),
                    "strike": float(getattr(contract, "strike", 0.0) or 0.0),
                    "quantity": quantity,
                    "average_cost": average_cost,
                    "cost_basis": abs(quantity * average_cost),
                    "account_id": account_id,
                    "currency": contract_currency,
                }
            )
            continue
        positions.append(
            Position(
                symbol=str(getattr(contract, "symbol", "") or "").strip().upper(),
                quantity=quantity,
                market_value=quantity * average_cost,
                average_cost=average_cost,
                currency=contract_currency,
            )
        )

    total_equity = 0.0
    buying_power = None
    for account_value in ib.accountValues():
        account_id = str(getattr(account_value, "account", "") or "").strip() or None
        if not _matches_account(account_id, selected_account_ids):
            continue
        if str(getattr(account_value, "currency", "") or "").strip().upper() != market_currency:
            continue
        if account_value.tag == "NetLiquidation":
            total_equity += float(account_value.value)
        elif account_value.tag == "AvailableFunds":
            value = float(account_value.value)
            buying_power = value if buying_power is None else buying_power + value

    return PortfolioSnapshot(
        as_of=datetime.now(timezone.utc),
        total_equity=total_equity,
        buying_power=buying_power,
        positions=tuple(positions),
        metadata={
            "account_ids": selected_account_ids,
            "option_positions": tuple(option_positions),
            "currency": market_currency,
        },
    )
