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

    return {
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
