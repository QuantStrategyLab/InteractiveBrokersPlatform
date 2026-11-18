"""Pure account-facts snapshot validation and position projection helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Any

SNAPSHOT_SCHEMA = "ibkr_account_snapshot.v1"
_ACCOUNT_ID = re.compile(r"^(?:U|DU)\d+$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_DECIMAL_TEXT = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$")
_RUNTIME_TARGET_MISMATCH_FIELDS = (
    "project_id",
    "service_name",
    "runtime_revision",
    "account_scope",
    "deployment_selector",
    "account_selector",
    "runtime_release_receipt.runtime_revision",
)


def _safe_mismatched_fields(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    present = {
        field for field in value
        if isinstance(field, str) and field in _RUNTIME_TARGET_MISMATCH_FIELDS
    }
    return [field for field in _RUNTIME_TARGET_MISMATCH_FIELDS if field in present]


class _ProjectionError(ValueError):
    def __init__(self, reason: str, *, mismatched_fields: Sequence[str] = ()) -> None:
        super().__init__(reason)
        self.reason = reason
        self.mismatched_fields = _safe_mismatched_fields(mismatched_fields)


def _observed_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise _ProjectionError("observation_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise _ProjectionError("observation_invalid") from None


def _decimal_text(value: object, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or _DECIMAL_TEXT.fullmatch(value) is None:
        raise _ProjectionError("account_facts_invalid")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise _ProjectionError("account_facts_invalid") from None
    if not amount.is_finite():
        raise _ProjectionError("account_facts_invalid")
    exponent = amount.as_tuple().exponent
    scale = max(-exponent, 0)
    integer_digits = max(len(amount.as_tuple().digits) + exponent, 0)
    if scale > 8 or integer_digits + scale > 15:
        raise _ProjectionError("account_facts_invalid")
    return value


POSITIONS_SCOPE = "stocks_only"
# QRS accepts these optional fields. Upload requires an explicit opt-in;
# archived projections remain available to existing offline consumers.
POSITIONS_KEYS = ("broker_reported_positions", "broker_reported_positions_scope", "broker_reported_positions_complete")
_POSITION_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9./ -]{0,31}\Z", re.ASCII)
_MAX_POSITIONS = 64
_POSITION_KEYS = frozenset({"symbol", "quantity", "market_value", "currency", "avg_cost"})


def _project_positions(facts: Mapping[str, Any]) -> list[dict[str, str]] | None:
    """Validate runtime ``broker_reported_positions``; None (omit) on any doubt."""
    raw = facts.get("broker_reported_positions")
    if facts.get("broker_reported_positions_scope") != POSITIONS_SCOPE:
        return None
    complete = facts.get("broker_reported_positions_complete")
    if "broker_reported_positions_complete" in facts and complete is not True:
        return None
    if not isinstance(raw, list) or (not raw and complete is not True) or len(raw) > _MAX_POSITIONS:
        return None
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping) or set(item) - _POSITION_KEYS:
            return None
        symbol = item.get("symbol")
        currency = item.get("currency")
        if (
            not isinstance(symbol, str)
            or _POSITION_SYMBOL.fullmatch(symbol) is None
            or symbol in seen
            or not isinstance(currency, str)
            or _CURRENCY.fullmatch(currency) is None
        ):
            return None
        seen.add(symbol)
        try:
            row = {
                "symbol": symbol,
                "quantity": _decimal_text(item.get("quantity")),
                "market_value": _decimal_text(item.get("market_value")),
                "currency": currency,
            }
            if "avg_cost" in item:
                row["avg_cost"] = _decimal_text(item.get("avg_cost"))
        except _ProjectionError:
            return None
        rows.append(row)  # type: ignore[arg-type]
    return rows
