"""Pure, private projection of native execution rows from the existing Flex read.

This is coverage of reported executions, never proof of complete trading history.
Missing/ambiguous rows invalidate only this projection, not native account TWR.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from xml.etree import ElementTree

from application.ibkr_period_return import validate_period_return_scope


SCHEMA_VERSION = "broker_executions.v1"
_HASH = re.compile(r"[a-f0-9]{64}", re.ASCII)
_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,8})?", re.ASCII)
_FEE_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,18})?", re.ASCII)
_CURRENCY = re.compile(r"[A-Z][A-Z0-9]{2,9}", re.ASCII)
_REQUIRED = ("accountId", "levelOfDetail", "ibExecID", "tradeDate", "buySell", "quantity",
             "tradePrice", "currency", "assetCategory", "symbol")
_ORIGINAL_FIELDS = ("origTradeID", "origExecID", "origTradeDate", "origTradePrice", "origOrderID")


class FlexExecutionsError(ValueError):
    """Safe fixed reason; never includes a private source value."""


def _day(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    for pattern in ("%Y%m%d", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value, pattern).date()
            if parsed.strftime(pattern) == value:
                return parsed.isoformat()
        except ValueError:
            pass
    return None


def _number(value: object, *, fee: bool = False) -> str | None:
    # Preserve the broker's sign and precision; no abs(), inherited currency,
    # scientific notation or nonfinite values enter the consumer contract.
    pattern = _FEE_DECIMAL if fee else _DECIMAL
    return value if isinstance(value, str) and len(value) <= 64 and pattern.fullmatch(value) else None


def _number_shape(value: object) -> dict[str, Any]:
    """Describe a rejected numeric format without retaining its value or sign."""
    if not isinstance(value, str):
        return {"kind": "not_text"}
    if len(value) > 256:
        return {"kind": "oversized"}
    if value.strip().casefold().lstrip("+-") in {"nan", "inf", "infinity"}:
        return {"kind": "nonfinite"}
    match = re.fullmatch(r"[+-]?([0-9]+)(?:\.([0-9]+))?([eE][+-]?[0-9]+)?", value.strip(), re.ASCII)
    if match is None:
        return {"kind": "unsupported"}
    return {"kind": "scientific" if match[3] else "decimal",
            "integer_digits": len(match[1]), "fraction_digits": len(match[2] or ""),
            "has_whitespace": value != value.strip()}


def _text(value: object, limit: int) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= limit and value == value.strip()
            and not any(ord(char) < 0x20 or ord(char) == 0x7F for char in value))


def _local_time(value: str, trade_date: str) -> str | None:
    for pattern in ("%Y%m%d;%H%M%S", "%Y%m%d;%H:%M:%S", "%Y-%m-%d;%H:%M:%S",
                    "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(value, pattern)
            if parsed.strftime(pattern) == value and parsed.date().isoformat() == trade_date:
                return parsed.isoformat()
        except ValueError:
            pass
    return None


def _correction(row: ElementTree.Element) -> bool:
    if {token.casefold() for token in re.split(r"[,;\s]+", row.get("notes", ""))} & {"ca", "co"}:
        return True
    for field in _ORIGINAL_FIELDS:
        value = row.get(field, "").strip()
        if value and not (re.fullmatch(r"0+(?:\.0+)?", value) or value == "0000-00-00"):
            return True
    return False


def project_flex_executions(
    statement: ElementTree.Element, *, source_ref: str, period: Mapping[str, Any],
) -> dict[str, Any]:
    """Read only this selected statement's Trades/Trade rows; no I/O or logging."""
    section = statement.find("Trades")
    rows = [] if section is None else section.findall("Trade")
    levels = sorted({level if level in {"EXECUTION", "ORDER", "SUMMARY"} else "missing" if not level else "other"
                     for row in rows for level in [row.get("levelOfDetail", "")]})
    field_names = sorted({field for row in rows for field in row.attrib
                          if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", field)})
    diagnostic = {"section_present": section is not None, "native_record_count": len(rows),
                  "field_names": field_names[:128], "level_types": levels, "missing_fields": [], "invalid_fields": []}

    def unavailable(reason, *, missing=(), invalid=(), numeric_shapes=None):
        safe_shapes = {"invalid_numeric_shapes": numeric_shapes} if numeric_shapes else {}
        return {"status": "source_unavailable", "records": None,
                "diagnostic": {**diagnostic, "reason": reason, "missing_fields": sorted(set(missing)),
                               "invalid_fields": sorted(set(invalid)), **safe_shapes}}

    if section is None:
        return unavailable("trades_section_missing")
    if len(statement.findall("Trades")) != 1 or any(child.tag != "Trade" for child in section):
        return unavailable("unsupported_trade_rows")
    start, end = _day(period.get("from")), _day(period.get("to"))
    if not start or not end or start > end or (date.fromisoformat(end) - date.fromisoformat(start)).days >= 366:
        return unavailable("execution_period_invalid")
    if not isinstance(source_ref, str) or not _HASH.fullmatch(source_ref):
        return unavailable("execution_report_reference_invalid")
    if len(rows) > 10000:
        return unavailable("execution_record_limit")
    account_id = statement.get("accountId", "").strip()
    records = {}
    for row in rows:
        missing = [field for field in _REQUIRED if not row.get(field)]
        if missing:
            reason = "execution_level_required" if missing == ["levelOfDetail"] else "execution_fields_missing"
            return unavailable(reason, missing=missing)
        if row.get("accountId") != account_id:
            return unavailable("execution_account_mismatch")
        if row.get("levelOfDetail") != "EXECUTION":
            return unavailable("execution_level_required")
        if _correction(row):
            return unavailable("execution_correction_unresolved")
        if not _text(row.get("ibExecID"), 256):
            return unavailable("execution_fields_invalid", invalid=["ibExecID"])
        trade_date = _day(row.get("tradeDate"))
        if not trade_date:
            return unavailable("execution_fields_invalid", invalid=["tradeDate"])
        if not start <= trade_date <= end:
            return unavailable("execution_date_outside_period")
        raw_time = row.get("dateTime")
        local_time = _local_time(raw_time, trade_date) if raw_time else None
        if raw_time and not local_time:
            return unavailable("execution_datetime_invalid")
        quantity, price = _number(row.get("quantity")), _number(row.get("tradePrice"))
        if quantity is None or Decimal(quantity) == 0:
            return unavailable("execution_fields_invalid", invalid=["quantity"])
        if price is None or Decimal(price) < 0:
            return unavailable("execution_fields_invalid", invalid=["tradePrice"])
        if row.get("buySell") not in {"BUY", "SELL"}:
            return unavailable("execution_fields_invalid", invalid=["buySell"])
        for field, pattern in (("symbol", r"[A-Z0-9][A-Z0-9. /_-]{0,63}"),
                               ("assetCategory", r"[A-Z][A-Z0-9_]{0,15}")):
            if not re.fullmatch(pattern, row.get(field), re.ASCII):
                return unavailable("execution_fields_invalid", invalid=[field])
        if not _CURRENCY.fullmatch(row.get("currency")):
            return unavailable("execution_fields_invalid", invalid=["currency"])
        fees = {}
        for field in ("ibCommission", "taxes"):
            raw = row.get(field)
            fees[field] = _number(raw, fee=True) if raw not in (None, "") else None
            if raw not in (None, "") and fees[field] is None:
                return unavailable("execution_fields_invalid", invalid=[field],
                                   numeric_shapes={field: _number_shape(raw)})
        fee_currency = row.get("ibCommissionCurrency") or None
        if fee_currency is not None and not _CURRENCY.fullmatch(fee_currency):
            return unavailable("execution_fields_invalid", invalid=["ibCommissionCurrency"])
        execution_ref = hashlib.sha256(f'{account_id}|{row.get("ibExecID")}'.encode()).hexdigest()
        record = {"execution_ref": execution_ref, "report_ref": source_ref,
                  "trade_date": trade_date, "local_time": local_time, "time_zone": None,
                  "symbol": row.get("symbol"), "asset_category": row.get("assetCategory"),
                  "side": row.get("buySell"), "quantity": quantity, "price": price,
                  "currency": row.get("currency"), "commission": fees["ibCommission"],
                  "commission_currency": fee_currency, "taxes": fees["taxes"]}
        if execution_ref in records and records[execution_ref] != record:
            return unavailable("execution_conflict")
        records[execution_ref] = record
        if len(records) > 256:
            return unavailable("execution_record_limit")
    return {"status": "available", "records": list(records.values()),
            "diagnostic": {**diagnostic, "reason": "reported_executions", "record_count": len(records)}}


def build_ibkr_executions(
    ledger: Mapping[str, Any], *, target_id: str, source_binding_id: str,
    account_scope: str, expected_account_ids: Sequence[str], observed_at: datetime,
) -> dict[str, Any]:
    """Attach independent protected scope to the already parsed private facts."""
    validate_period_return_scope(target_id=target_id, source_binding_id=source_binding_id,
                                account_scope=account_scope, expected_account_ids=expected_account_ids)
    if (not isinstance(ledger, Mapping) or ledger.get("schema_version") != "ibkr_flex_ledger.v1"
            or ledger.get("account_ids") != list(expected_account_ids)):
        raise FlexExecutionsError("identity_mismatch")
    executions = ledger.get("executions")
    if not isinstance(executions, Mapping) or executions.get("status") != "available":
        raise FlexExecutionsError("source_unavailable")
    period = ledger.get("period")
    start = _day(period.get("from")) if isinstance(period, Mapping) else None
    end = _day(period.get("to")) if isinstance(period, Mapping) else None
    if not start or not end or start > end:
        raise FlexExecutionsError("period_invalid")
    if (not isinstance(observed_at, datetime) or observed_at.tzinfo is None
            or observed_at.utcoffset() is None or end > observed_at.astimezone(timezone.utc).date().isoformat()):
        raise FlexExecutionsError("observation_invalid")
    return {"schema_version": SCHEMA_VERSION, "target_id": target_id, "source_binding_id": source_binding_id,
            "account_scope": account_scope, "account_selector": expected_account_ids[0],
            "period": {"from": start, "to": end},
            "observed_at": observed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source": "ibkr_flex", "source_ref": ledger["source_ref"],
            "coverage": "reported_executions", "records": executions["records"]}
