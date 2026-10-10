"""Small, source-preserving account facts for IBKR runtime reports."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Any

from application.ibkr_portfolio import MARKET_CURRENCY_CASH_TAG_PRIORITY


def _finite_decimal_text(value: Any) -> str | None:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite():
        return None
    return format(amount, "f")


def _verified_usd_net_assets(rows: tuple[Mapping[str, Any], ...], metadata: Mapping[str, Any]) -> str | None:
    if metadata.get("total_equity_source") != "broker_net_liquidation":
        return None
    metadata_value = _finite_decimal_text(metadata.get("broker_net_liquidation"))
    if metadata_value is None:
        return None
    expected = Decimal(metadata_value)
    for row in rows:
        if str(row.get("currency") or "").strip().upper() != "USD":
            continue
        value = _finite_decimal_text(row.get("NetLiquidation"))
        if value is not None and Decimal(value) == expected:
            return metadata_value
    return None


def _position_decimal_text(value: Any) -> str | None:
    """Finite decimal text bounded to 8 fractional / 15 total digits, else None."""
    if isinstance(value, bool):
        return None
    text = _finite_decimal_text(value)
    if text is None:
        return None
    amount = Decimal(text).quantize(Decimal("0.00000001")).normalize()
    if amount == 0:
        amount = Decimal(0)
    text = format(amount, "f")
    whole, _, fraction = text.lstrip("-").partition(".")
    if len(whole) + len(fraction) > 15:
        return None
    return text


POSITIONS_SCOPE = "stocks_only"
_POSITION_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9./ -]{0,31}\Z", re.ASCII)
_MAX_POSITIONS = 64


def build_broker_reported_positions(snapshot: Any) -> list[dict[str, Any]] | None:
    """Project STK positions the broker already returned; None means omit.

    IBKR snapshots only carry ``secType=STK`` rows (options live in metadata),
    so the scope label is ``stocks_only``. Empty or malformed sets return None
    so an unknown inventory is never written as an empty list.
    """
    try:
        positions = getattr(snapshot, "positions", None)
        if not isinstance(positions, (tuple, list)) or not positions:
            return None
        if len(positions) > _MAX_POSITIONS:
            return None
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for position in positions:
            symbol = getattr(position, "symbol", None)
            if not isinstance(symbol, str):
                return None
            symbol = symbol.strip().upper()
            if _POSITION_SYMBOL.fullmatch(symbol) is None or symbol in seen:
                return None
            seen.add(symbol)
            quantity = _position_decimal_text(getattr(position, "quantity", None))
            market_value = _position_decimal_text(getattr(position, "market_value", None))
            currency = str(getattr(position, "currency", "") or "").strip().upper()
            if quantity is None or market_value is None or not re.fullmatch(r"[A-Z]{3}", currency):
                return None
            row: dict[str, Any] = {
                "symbol": symbol,
                "quantity": quantity,
                "market_value": market_value,
                "currency": currency,
            }
            raw_avg_cost = getattr(position, "average_cost", None)
            if raw_avg_cost is not None and not isinstance(raw_avg_cost, bool):
                avg_cost = _position_decimal_text(raw_avg_cost)
                if avg_cost is not None:
                    row["avg_cost"] = avg_cost
            rows.append(row)
        rows.sort(key=lambda item: str(item["symbol"]))
        return rows
    except Exception:
        return None


def build_ibkr_account_facts(snapshot: Any) -> dict[str, Any]:
    """Project verified facts already present in one portfolio snapshot."""
    metadata = getattr(snapshot, "metadata", None)
    if not isinstance(metadata, Mapping):
        return {}
    raw_account_ids = metadata.get("account_ids") or ()
    if isinstance(raw_account_ids, str):
        raw_account_ids = (raw_account_ids,)
    account_ids = tuple(
        str(value).strip() for value in raw_account_ids
        if str(value or "").strip()
    )
    rows = tuple(metadata.get("cash_balances") or ())
    if len(account_ids) != 1 or not rows:
        return {}
    account_id = account_ids[0]
    if any(
        not isinstance(row, Mapping)
        or str(row.get("account_id") or "").strip() != account_id
        for row in rows
    ):
        return {}

    observed_at = getattr(snapshot, "as_of", None)
    if not isinstance(observed_at, datetime):
        return {}
    try:
        if observed_at.utcoffset() is None:
            return {}
        observed_at = observed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, ValueError):
        return {}

    cash = []
    seen_currencies = set()
    for row in rows:
        currency = str(row.get("currency") or "").strip().upper()
        if currency == "BASE":
            continue
        if not re.fullmatch(r"[A-Z]{3}", currency) or currency in seen_currencies:
            return {}
        seen_currencies.add(currency)
        for tag in MARKET_CURRENCY_CASH_TAG_PRIORITY:
            if tag not in row:
                continue
            amount = _finite_decimal_text(row[tag])
            if amount is not None:
                cash.append(
                    {"currency": currency, "cash_balance": amount, "source_tag": tag}
                )
            break
    if not cash:
        return {}

    net_assets = _verified_usd_net_assets(rows, metadata)

    facts: dict[str, Any] = {
        "schema_version": "ibkr_account_snapshot.v1",
        "account_ids": [account_id],
        # This currency labels net_assets (IBKR USD NetLiquidation), not cash.
        "currency": "USD",
        "observed_at": observed_at,
        "net_assets": net_assets,
        "net_assets_semantics": "USD NetLiquidation; null unless broker source is verified",
        "cash_semantics": "IBKR accountValues cash balance; source tag retained; not available funds",
        "cash": cash,
    }
    # Optional, fail-soft: omitted entirely when unknown or malformed.
    positions = build_broker_reported_positions(snapshot)
    if positions:
        facts["broker_reported_positions"] = positions
        facts["broker_reported_positions_scope"] = POSITIONS_SCOPE
    return facts
