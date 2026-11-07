"""Strict redacted projection for reconciliation; never a complete-ledger claim."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal

from application.ibkr_flex_source import FlexReport, _xml_root, verify_report_accounts


class FlexAmountExportError(ValueError):
    """Fixed failure text only; source values must never enter an exception."""


_SECTIONS = {
    "Trades": ("Trade", ("quantity", "tradePrice", "tradeMoney", "proceeds", "netCash",
                          "ibCommission", "taxes", "fxRateToBase", "cost", "fifoPnlRealized")),
    "CashTransactions": ("CashTransaction", ("amount", "fxRateToBase")),
    "StmtFunds": ("StatementOfFundsLine", ("amount", "debit", "credit", "balance",
                                         "tradeQuantity", "tradeGross", "tradeCommission", "tradeTax")),
}
_DATES = ("date", "dateTime", "tradeDate", "reportDate", "settleDate", "settleDateTarget")
_IDENTITIES = ("transactionID", "tradeID", "ibExecID", "origTradeID", "origExecID")
_TEXT = {
    "currency": r"[A-Z]{3}",
    "ibCommissionCurrency": r"[A-Z]{3}",
    "symbol": r"[A-Z0-9][A-Z0-9 ._/-]{0,31}",
    "assetCategory": r"[A-Z]{1,12}",
    "buySell": r"BUY|SELL",
    "activityCode": r"[A-Z]{1,12}",
    "levelOfDetail": r"EXECUTION|ORDER|CLOSED_LOT|SUMMARY|DETAIL|CURRENCY|ASSET_SUMMARY|SYMBOL_SUMMARY",
}
_CASH_TYPES = frozenset({
    "Deposits & Withdrawals", "Dividends", "Payment In Lieu Of Dividends", "Withholding Tax",
    "Broker Interest Paid", "Broker Interest Received", "Bond Interest Paid", "Bond Interest Received",
    "Other Fees", "Commission Adjustments", "Other Income", "Fees", "Sales Tax",
})


def _reject() -> None:
    raise FlexAmountExportError("flex_amount_export_invalid") from None


def validate_export_scope(account_ids: tuple[str, ...], period: dict[str, str]) -> None:
    if (not account_ids or len(account_ids) != len(set(account_ids))
            or any(not isinstance(a, str) or not re.fullmatch(r"(?:U|DU)[0-9]+", a) for a in account_ids)):
        _reject()
    try:
        first, last = (date.fromisoformat(period[k]) for k in ("from", "to"))
        if (set(period) != {"from", "to"} or first.isoformat() != period["from"]
                or last.isoformat() != period["to"] or not 0 <= (last - first).days <= 30):
            _reject()
    except (KeyError, TypeError, ValueError):
        _reject()


def _date_text(value: str | None) -> str | None:
    if not value:
        return None
    # Keep the broker's date/time precision. Do not invent a timezone or midnight.
    formats = {r"[0-9]{8}": "%Y%m%d", r"[0-9]{4}-[0-9]{2}-[0-9]{2}": "%Y-%m-%d",
               r"[0-9]{8};[0-9]{6}": "%Y%m%d;%H%M%S",
               r"[0-9]{4}-[0-9]{2}-[0-9]{2};[0-9]{2}:[0-9]{2}:[0-9]{2}": "%Y-%m-%d;%H:%M:%S"}
    for pattern, fmt in formats.items():
        if re.fullmatch(pattern, value):
            try:
                parsed = datetime.strptime(value, fmt)
            except ValueError:
                _reject()
            return parsed.isoformat() if ";" in value else parsed.date().isoformat()
    _reject()


def _decimal_text(value: str | None) -> str | None:
    if not value:
        return None
    # No float conversion, rounding, exponent expansion, NaN or Infinity.
    if not re.fullmatch(r"[+-]?[0-9]{1,24}(?:\.[0-9]{1,18})?", value):
        _reject()
    return format(Decimal(value), "f")


def build_redacted_amount_export(
    report: FlexReport, *, account_ids: tuple[str, ...], period: dict[str, str],
) -> dict:
    """All configured accounts and all three sections must validate before export.

    Raw IDs become per-artifact sequential aliases. Rows are never deduplicated,
    netted, summed or matched across the three overlapping source sections.
    """
    validate_export_scope(account_ids, period)
    # Only UTF-8 XML, within the transport's existing bound; reject UTF-16/32
    # before the shared DTD/entity guard (which operates on ASCII markers).
    if len(report.content) > 8 * 1024 * 1024 or b"\x00" in report.content:
        _reject()
    try:
        report.content.decode("utf-8-sig")
    except UnicodeError:
        _reject()
    verify_report_accounts(report, expected_account_ids=account_ids)
    root = _xml_root(report.content, endpoint="GetStatement")
    if root.get("type") != "AF" or len(root.findall("FlexStatements")) != 1:
        _reject()
    statements = root.findall("./FlexStatements/FlexStatement")
    if len(list(root.find("FlexStatements"))) != len(statements):
        _reject()
    by_account = {s.get("accountId"): s for s in statements}
    accounts = []
    row_count = 0
    for index, account_id in enumerate(account_ids, 1):
        statement = by_account[account_id]
        if (statement.get("model", "").strip()
                or {k: _date_text(statement.get(v)) for k, v in (("from", "fromDate"), ("to", "toDate"))} != period):
            _reject()
        # All explicit row identities must agree, including unexported sections.
        if any(e.get("accountId") not in (None, "", account_id) for e in statement.iter()):
            _reject()
        identities: dict[tuple[str, str], str] = {}
        sections = {}
        for section_name, (row_tag, decimal_fields) in _SECTIONS.items():
            sections_found = statement.findall(section_name)
            if len(sections_found) != 1 or sections_found[0].get("model", "").strip():
                _reject()
            rows = []
            for source in sections_found[0]:
                row_count += 1
                if (row_count > 10_000 or source.tag != row_tag or len(source)
                        or source.get("model", "").strip() or (source.text or "").strip()):
                    _reject()
                row = {"row": len(rows) + 1}
                for field, pattern in _TEXT.items():
                    value = source.get(field) or None
                    if value is not None and not re.fullmatch(pattern, value):
                        _reject()
                    row[field] = value
                if row["currency"] is None:
                    _reject()
                row.update({field: _decimal_text(source.get(field)) for field in decimal_fields})
                row.update({field: _date_text(source.get(field)) for field in _DATES})
                required_dates = {"Trades": ("tradeDate", "dateTime"),
                                  "CashTransactions": ("dateTime", "date"),
                                  "StmtFunds": ("date", "reportDate")}[section_name]
                if not any(row[field] for field in required_dates):
                    _reject()
                for field in ("date", "dateTime", "tradeDate", "reportDate"):
                    if row[field] and not period["from"] <= row[field][:10] <= period["to"]:
                        _reject()
                if section_name == "Trades":
                    if (row["levelOfDetail"] != "EXECUTION"
                            or any(row[k] is None for k in ("quantity", "tradePrice", "proceeds", "symbol", "buySell"))):
                        _reject()
                elif row["amount"] is None:
                    # Opening/closing balance-only StmtFunds rows are meaningful.
                    if section_name != "StmtFunds" or row["balance"] is None:
                        _reject()
                for field in _IDENTITIES:
                    native = source.get(field)
                    if native:
                        namespace = {"origTradeID": "tradeID", "origExecID": "ibExecID"}.get(field, field)
                        key = (namespace, native)
                        identities.setdefault(key, f"id-{len(identities) + 1}")
                        row[field] = identities[key]
                    else:
                        row[field] = None
                if section_name == "CashTransactions":
                    native_type = source.get("type")
                    row["type"] = native_type if native_type in _CASH_TYPES else None
                    row["typeRecognized"] = native_type in _CASH_TYPES
                rows.append(row)
            sections[section_name] = rows
        accounts.append({"account": f"account-{index}", "sections": sections})
    return {
        "schema_version": "ibkr_flex_redacted_amounts.v1", "period": dict(period),
        "coverage": "reported_rows_only_not_complete_history", "reconciliation": "not_performed",
        "missing_fields_are_zero": False, "deduplication": "not_applied",
        "identity_scope": "within_account_and_artifact_only", "timezone": None,
        "accounts": accounts,
    }


def serialize_redacted_export(projection: dict, *, forbidden_values: tuple[str, ...]) -> str:
    """Final no-secret check before any bytes are written; no raw content hash."""
    content = json.dumps(projection, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    # Check decoded values too, so JSON escaping cannot evade the secret check.
    def check(value):
        if isinstance(value, dict):
            for key, child in value.items():
                check(key)
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)
        elif isinstance(value, str):
            if re.search(r"(?:DU|U)[0-9]+", value) or any(secret and secret in value for secret in forbidden_values):
                _reject()
    check(projection)
    return content
