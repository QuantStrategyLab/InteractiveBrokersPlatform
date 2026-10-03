"""IBKR portfolio snapshot helpers with market-currency awareness."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from quant_platform_kit.common.models import PortfolioSnapshot, Position

MARKET_CURRENCY_CASH_TAG_PRIORITY = (
    "$LEDGER-CashBalance",
    "$LEDGER-TotalCashBalance",
    "CashBalance",
    "TotalCashBalance",
    "SettledCash",
)


class IBKRPortfolioSnapshotUnavailableError(RuntimeError):
    """Raised when IBKR did not return enough account data for safe execution."""


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


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _aggregate_strategy_positions(positions: Iterable[Position]) -> list[Position]:
    """Combine same-symbol holdings for strategy consumers that key by symbol."""
    aggregates: dict[str, dict[str, Any]] = {}
    for position in positions:
        symbol = str(position.symbol).strip().upper()
        row = aggregates.setdefault(
            symbol,
            {
                "quantity": 0.0,
                "market_value": 0.0,
                "weighted_cost": 0.0,
                "absolute_quantity": 0.0,
                "currency": position.currency,
                "accounts": set(),
            },
        )
        if row["currency"] != position.currency:
            raise IBKRPortfolioSnapshotUnavailableError(
                "IBKR same-symbol positions with different currencies cannot be aggregated."
            )
        row["quantity"] += float(position.quantity)
        row["market_value"] += float(position.market_value)
        if position.average_cost is not None:
            absolute_quantity = abs(float(position.quantity))
            row["weighted_cost"] += absolute_quantity * float(position.average_cost)
            row["absolute_quantity"] += absolute_quantity
        if position.account_id:
            row["accounts"].add(position.account_id)

    aggregated = []
    for symbol, row in aggregates.items():
        average_cost = (
            row["weighted_cost"] / row["absolute_quantity"]
            if row["absolute_quantity"] > 0.0
            else None
        )
        accounts = row["accounts"]
        aggregated.append(
            Position(
                symbol=symbol,
                quantity=row["quantity"],
                market_value=row["market_value"],
                average_cost=average_cost,
                currency=row["currency"],
                account_id=next(iter(accounts)) if len(accounts) == 1 else None,
            )
        )
    return aggregated


def _cash_value_for_currency(
    values_by_account_currency: dict[tuple[str | None, str], dict[str, float]],
    *,
    currency: str,
    tag_priority: tuple[str, ...] = MARKET_CURRENCY_CASH_TAG_PRIORITY,
) -> float | None:
    total = 0.0
    matched = False
    market_currency = str(currency or "").strip().upper()
    for (_account_id, value_currency), tag_values in values_by_account_currency.items():
        if value_currency != market_currency:
            continue
        for tag in tag_priority:
            if tag in tag_values:
                total += float(tag_values[tag])
                matched = True
                break
    return total if matched else None


def _broker_net_liquidation_evidence(
    account_values: Iterable[Any],
    *,
    selected_account_ids: tuple[str, ...],
) -> tuple[float | None, str | None]:
    """Return account-scope USD NetLiquidation and a stable source digest.

    Only explicit USD NetLiquidation rows identify USD capital. BASE is
    account-relative and does not establish a currency.
    """

    if not selected_account_ids:
        return None, None
    selected: dict[str, float] = {}
    for account_value in account_values:
        account_id = str(getattr(account_value, "account", "") or "").strip()
        if account_id not in selected_account_ids:
            continue
        if str(getattr(account_value, "tag", "") or "").strip() != "NetLiquidation":
            continue
        currency = str(getattr(account_value, "currency", "") or "").strip().upper()
        if currency != "USD":
            continue
        try:
            value = float(getattr(account_value, "value", None))
        except (TypeError, ValueError):
            return None, None
        if not math.isfinite(value) or value <= 0.0:
            return None, None
        selected[account_id] = value

    if set(selected) != set(selected_account_ids):
        return None, None
    canonical_rows = [
        {"account_id": account_id, "currency": "USD", "value": selected[account_id]}
        for account_id in sorted(selected)
    ]
    source_digest = hashlib.sha256(
        json.dumps(
            canonical_rows,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return sum(float(row["value"]) for row in canonical_rows), source_digest


def fetch_portfolio_snapshot(
    ib: Any,
    *,
    account_ids: Iterable[str] | str | None = None,
    wait_seconds: float = 1.0,
    currency: str = "USD",
    cash_only_execution: bool = True,
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

    # ib_insync's portfolio() exposes the current cached portfolio rows; it
    # does not request quotes or create another broker data path.
    portfolio_fn = getattr(ib, "portfolio", None)
    if not callable(portfolio_fn):
        raise IBKRPortfolioSnapshotUnavailableError(
            "IBKR strategy snapshot requires the cached read-only portfolio market values."
        )
    try:
        portfolio_items = tuple(portfolio_fn() or ())
    except Exception as exc:
        raise IBKRPortfolioSnapshotUnavailableError(
            "IBKR could not load cached read-only portfolio market values."
        ) from exc

    market_values_by_account_con_id: dict[tuple[str, str], tuple[float, float, str]] = {}
    for item in portfolio_items:
        contract = getattr(item, "contract", None)
        if str(getattr(contract, "secType", "") or "").strip().upper() != "STK":
            continue
        account_id = str(getattr(item, "account", "") or "").strip()
        if not _matches_account(account_id or None, selected_account_ids):
            continue
        con_id = str(getattr(contract, "conId", "") or "").strip()
        if not account_id or not con_id or con_id == "0":
            raise IBKRPortfolioSnapshotUnavailableError(
                "IBKR portfolio stock mark is missing its account or contract identity."
            )
        market_value = _as_float(getattr(item, "marketValue", None))
        quantity = _as_float(getattr(item, "position", None))
        currency_value = str(getattr(contract, "currency", "") or "").strip().upper()
        if (
            market_value is None
            or quantity is None
            or not all(math.isfinite(value) for value in (market_value, quantity))
            or not currency_value
        ):
            raise IBKRPortfolioSnapshotUnavailableError(
                "IBKR portfolio stock mark is missing finite market value, quantity, or currency."
            )
        identity = (account_id, con_id)
        if identity in market_values_by_account_con_id:
            raise IBKRPortfolioSnapshotUnavailableError(
                "IBKR returned duplicate portfolio rows for one account and stock contract."
            )
        market_values_by_account_con_id[identity] = (market_value, quantity, currency_value)

    positions = []
    option_positions = []
    for raw_position in ib.positions():
        account_id = str(getattr(raw_position, "account", "") or "").strip() or None
        if not _matches_account(account_id, selected_account_ids):
            continue
        contract = raw_position.contract
        quantity = _as_float(getattr(raw_position, "position", None))
        average_cost = _as_float(getattr(raw_position, "avgCost", None))
        if quantity is None or average_cost is None:
            raise IBKRPortfolioSnapshotUnavailableError(
                "IBKR position is missing its quantity or average cost."
            )
        if quantity == 0:
            continue
        contract_currency = str(getattr(contract, "currency", "") or "").strip().upper()
        sec_type = str(getattr(contract, "secType", "") or "").strip().upper()
        if not math.isfinite(quantity) or not math.isfinite(average_cost):
            raise IBKRPortfolioSnapshotUnavailableError(
                "IBKR position is missing a finite quantity or average cost."
            )
        if sec_type == "OPT":
            if not contract_currency:
                contract_currency = market_currency
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
        if sec_type != "STK":
            raise IBKRPortfolioSnapshotUnavailableError(
                f"IBKR strategy snapshot does not support position type {sec_type or 'UNKNOWN'}."
            )
        if contract_currency != market_currency:
            raise IBKRPortfolioSnapshotUnavailableError(
                "IBKR stock positions in another or unknown currency cannot be mixed into this strategy snapshot."
            )
        con_id = str(getattr(contract, "conId", "") or "").strip()
        if not con_id or con_id == "0":
            raise IBKRPortfolioSnapshotUnavailableError(
                "IBKR stock position is missing its contract identity."
            )
        mark = market_values_by_account_con_id.get((account_id or "", con_id))
        if mark is None:
            raise IBKRPortfolioSnapshotUnavailableError(
                "IBKR current portfolio mark is missing for a selected stock position."
            )
        market_value, portfolio_quantity, portfolio_currency = mark
        if portfolio_currency != contract_currency:
            raise IBKRPortfolioSnapshotUnavailableError(
                "IBKR portfolio mark currency does not match its selected stock position."
            )
        if not math.isclose(quantity, portfolio_quantity, rel_tol=1e-9, abs_tol=1e-9):
            raise IBKRPortfolioSnapshotUnavailableError(
                "IBKR positions and portfolio snapshot quantities do not match."
            )
        symbol = str(getattr(contract, "symbol", "") or "").strip().upper()
        if not symbol:
            raise IBKRPortfolioSnapshotUnavailableError(
                "IBKR stock position is missing its symbol."
            )
        positions.append(
            Position(
                symbol=symbol,
                quantity=quantity,
                market_value=market_value,
                average_cost=average_cost,
                currency=contract_currency,
                account_id=account_id,
            )
        )

    positions = _aggregate_strategy_positions(positions)

    total_equity = 0.0
    available_funds = None
    matched_account_value_count = 0
    matched_market_currency_value_count = 0
    values_by_account_currency: dict[tuple[str | None, str], dict[str, float]] = {}
    account_values = tuple(ib.accountValues())
    observed_at = datetime.now(timezone.utc)
    for account_value in account_values:
        account_id = str(getattr(account_value, "account", "") or "").strip() or None
        if not _matches_account(account_id, selected_account_ids):
            continue
        matched_account_value_count += 1
        value_currency = str(getattr(account_value, "currency", "") or "").strip().upper()
        if value_currency:
            tag_values = values_by_account_currency.setdefault((account_id, value_currency), {})
            numeric_value = _as_float(getattr(account_value, "value", None))
            if numeric_value is not None:
                tag_values[str(getattr(account_value, "tag", "") or "").strip()] = numeric_value
        if value_currency != market_currency:
            continue
        matched_market_currency_value_count += 1
        if account_value.tag == "NetLiquidation":
            total_equity += float(account_value.value)
        elif account_value.tag == "AvailableFunds":
            value = float(account_value.value)
            available_funds = value if available_funds is None else available_funds + value

    market_currency_cash = _cash_value_for_currency(
        values_by_account_currency,
        currency=market_currency,
    )
    if selected_account_ids and matched_account_value_count == 0:
        raise IBKRPortfolioSnapshotUnavailableError(
            "IBKR returned no account values for the configured account selection."
        )
    if selected_account_ids and matched_market_currency_value_count == 0:
        raise IBKRPortfolioSnapshotUnavailableError(
            f"IBKR returned no {market_currency} account values for the configured account selection."
        )
    if cash_only_execution and market_currency_cash is None:
        raise IBKRPortfolioSnapshotUnavailableError(
            f"IBKR cash-only snapshot is missing the {market_currency} cash balance."
        )
    if cash_only_execution:
        buying_power = float(market_currency_cash or 0.0) if market_currency_cash is not None else 0.0
        position_market_values: dict[str, float] = {}
        for position in positions:
            symbol = str(position.symbol).strip().upper()
            position_market_values[symbol] = (
                position_market_values.get(symbol, 0.0) + float(position.market_value)
            )
        from us_equity_strategies.cash_only_equity import compute_strategy_total_equity

        total_equity = compute_strategy_total_equity(
            position_market_values,
            float(market_currency_cash or 0.0) if market_currency_cash is not None else 0.0,
        )
    else:
        buying_power = float(available_funds or 0.0) if available_funds is not None else (
            float(market_currency_cash or 0.0) if market_currency_cash is not None else 0.0
        )

    verified_nlv, source_digest = _broker_net_liquidation_evidence(
        account_values,
        selected_account_ids=selected_account_ids,
    )
    metadata: dict[str, Any] = {
        "account_ids": selected_account_ids,
        "option_positions": tuple(option_positions),
        "currency": market_currency,
        "market_currency_cash": market_currency_cash,
        "available_funds": available_funds,
        "cash_only_execution": cash_only_execution,
        "strategy_equity": float(total_equity),
        "strategy_equity_source": (
            "ibkr_cached_portfolio_market_value_plus_account_cash"
            if cash_only_execution
            else "ibkr_account_values_net_liquidation"
        ),
        "position_market_value_source": "ibkr_cached_portfolio_market_value",
        "portfolio_mark_observed_at": observed_at.isoformat(),
        "snapshot_observed_at": observed_at.isoformat(),
        "cash_balances": tuple(
            {
                "account_id": account_id,
                "currency": currency,
                **tag_values,
            }
            for (account_id, currency), tag_values in sorted(
                values_by_account_currency.items(),
                key=lambda item: ((item[0][0] or ""), item[0][1]),
            )
        ),
        "total_equity_source": (
            "broker_net_liquidation"
            if source_digest is not None
            else "unverified_net_liquidation"
        ),
    }
    if len(selected_account_ids) == 1:
        metadata["account_hash"] = selected_account_ids[0]
    if verified_nlv is not None and source_digest is not None:
        metadata["broker_net_liquidation"] = float(verified_nlv)
        metadata["broker_net_liquidation_source"] = "accountValues:USD:NetLiquidation"
        metadata["broker_net_liquidation_observed_at"] = observed_at.isoformat()
        metadata["source_digest_sha256"] = source_digest
        if market_currency == "USD":
            metadata["strategy_equity_minus_broker_nlv"] = float(total_equity) - float(verified_nlv)

    return PortfolioSnapshot(
        as_of=observed_at,
        total_equity=total_equity,
        buying_power=buying_power,
        positions=tuple(positions),
        metadata=metadata,
    )


def fetch_reconciled_paper_portfolio_snapshot(
    ib: Any,
    *,
    account_ids: Iterable[str] | str | None = None,
    currency: str = "USD",
) -> PortfolioSnapshot:
    """Read a current IBKR portfolio for the isolated paper command consumer.

    ``positions()`` exposes average cost but not a trustworthy current market
    value.  Delayed-command reconciliation must not mistake cost basis for
    live exposure, so this intentionally uses IBKR's read-only ``portfolio``
    snapshot instead.  It is separate from :func:`fetch_portfolio_snapshot`
    to avoid changing the existing strategy execution path.
    """

    selected_account_ids = _normalize_account_ids(account_ids)
    market_currency = str(currency or "USD").strip().upper()
    portfolio_fn = getattr(ib, "portfolio", None)
    if not callable(portfolio_fn):
        raise IBKRPortfolioSnapshotUnavailableError(
            "IBKR paper command reconciliation requires the read-only portfolio snapshot API."
        )

    raw_items = []
    account_queries = selected_account_ids or ("",)
    try:
        for account_id in account_queries:
            raw_items.extend(tuple(portfolio_fn(account_id) or ()))
    except Exception as exc:
        raise IBKRPortfolioSnapshotUnavailableError(
            "IBKR paper command reconciliation could not load current portfolio values."
        ) from exc

    positions: list[Position] = []
    seen_positions: set[tuple[str | None, str, str, float]] = set()
    for item in raw_items:
        account_id = str(getattr(item, "account", "") or "").strip() or None
        if not _matches_account(account_id, selected_account_ids):
            continue
        contract = getattr(item, "contract", None)
        symbol = str(getattr(contract, "symbol", "") or "").strip().upper()
        contract_currency = str(getattr(contract, "currency", "") or "").strip().upper()
        if not symbol or contract_currency != market_currency:
            raise IBKRPortfolioSnapshotUnavailableError(
                "IBKR paper command reconciliation received an incomplete or non-market-currency position."
            )
        quantity = _as_float(getattr(item, "position", None))
        market_value = _as_float(getattr(item, "marketValue", None))
        average_cost = _as_float(getattr(item, "averageCost", None))
        if quantity is None or market_value is None or average_cost is None:
            raise IBKRPortfolioSnapshotUnavailableError(
                "IBKR paper command reconciliation is missing current position market values."
            )
        if quantity == 0.0:
            continue
        dedupe_key = (account_id, symbol, str(getattr(contract, "conId", "") or ""), quantity)
        if dedupe_key in seen_positions:
            continue
        seen_positions.add(dedupe_key)
        positions.append(
            Position(
                symbol=symbol,
                quantity=quantity,
                market_value=market_value,
                average_cost=average_cost,
                currency=contract_currency,
            )
        )

    values_by_account_currency: dict[tuple[str | None, str], dict[str, float]] = {}
    try:
        raw_account_values = tuple(ib.accountValues() or ())
    except Exception as exc:
        raise IBKRPortfolioSnapshotUnavailableError(
            "IBKR paper command reconciliation could not load account cash values."
        ) from exc
    for account_value in raw_account_values:
        account_id = str(getattr(account_value, "account", "") or "").strip() or None
        if not _matches_account(account_id, selected_account_ids):
            continue
        value_currency = str(getattr(account_value, "currency", "") or "").strip().upper()
        numeric_value = _as_float(getattr(account_value, "value", None))
        if value_currency and numeric_value is not None:
            values_by_account_currency.setdefault((account_id, value_currency), {})[
                str(getattr(account_value, "tag", "") or "").strip()
            ] = numeric_value
    market_currency_cash = _cash_value_for_currency(
        values_by_account_currency,
        currency=market_currency,
    )
    if market_currency_cash is None:
        raise IBKRPortfolioSnapshotUnavailableError(
            f"IBKR paper command reconciliation is missing the {market_currency} cash balance."
        )
    total_equity = float(market_currency_cash) + sum(float(position.market_value) for position in positions)
    return PortfolioSnapshot(
        as_of=datetime.now(timezone.utc),
        total_equity=total_equity,
        cash_balance=float(market_currency_cash),
        buying_power=float(market_currency_cash),
        positions=tuple(positions),
        metadata={
            "account_ids": selected_account_ids,
            "currency": market_currency,
            "market_currency_cash": float(market_currency_cash),
            "reconciliation_source": "ibkr_portfolio_market_value",
            "cash_balances": tuple(
                {
                    "account_id": account_id,
                    "currency": value_currency,
                    **tag_values,
                }
                for (account_id, value_currency), tag_values in sorted(
                    values_by_account_currency.items(),
                    key=lambda item: ((item[0][0] or ""), item[0][1]),
                )
            ),
        },
    )
