"""Parse one IBKR Activity Flex XML report into minimal, source-preserving facts.

Pure, in-memory parsing only:

* The caller passes a report already fetched by ``application.ibkr_flex_source``
  plus the approved account scope. The account scope is re-checked before any
  fact is read.
* This module performs no network I/O, writes no files, and never invents an
  amount, date, FX rate, or return the broker did not supply.
* ``currency`` is declared ``"USD"`` only when the statement's own base currency
  is USD; non-USD facts are reported, never relabelled or converted.
* A period return is reported only when IBKR itself supplies an explicit,
  period-matching native TWR (``ChangeInNAV.twr``). No local TWR or
  cash-flow-dated algorithm is created here, so an unavailable return is
  returned as explicit ``missing`` reasons instead of a fabricated number.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Any
from xml.etree import ElementTree

from application.ibkr_flex_source import (
    FlexReport,
    FlexSourceError,
    verify_report_accounts,
)


SCHEMA_VERSION = "ibkr_flex_ledger.v1"
DIAGNOSTIC_SCHEMA_VERSION = "ibkr_flex_diagnostic.v1"

_EXTERNAL_FLOW_CODES = {"DEP": "deposit", "WITH": "withdrawal"}
_FEE_CODES = frozenset({"MFEE", "OFEE", "FRTAX", "STAX", "TTAX"})
_DIAGNOSTIC_CODES = frozenset({
    "native_twr_period_unavailable",
    "native_twr_period_mismatch",
    "account_base_currency_not_usd",
    "invalid_external_flow",
    "invalid_fee_line",
    "invalid_trade_commission",
    "external_flows_mixed_currency",
    "fees_mixed_currency",
})
_MISSING_CODES = frozenset({
    "native_twr_unavailable",
    "return_method_not_wired",
    "ending_valuation_unavailable",
    "external_flows_unavailable",
    "external_flows_incomplete",
    "external_flows_mixed_currency",
})


def _diagnostic_date(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed.isoformat() if parsed.isoformat() == value else None


def build_flex_ledger_diagnostic(ledger: Mapping[str, Any]) -> dict[str, Any]:
    """Project a private ledger into a redacted status-only report.

    This deliberately omits account identifiers, all monetary amounts, and
    the native TWR value. It is suitable for status/reporting surfaces, not
    the downstream performance consumer.
    """

    period_value = ledger.get("period")
    period = period_value if isinstance(period_value, Mapping) else {}
    account_currency = ledger.get("account_base_currency")
    currency = (
        account_currency
        if isinstance(account_currency, str) and re.fullmatch(r"[A-Z]{3}", account_currency)
        else None
    )
    raw_assessment = ledger.get("return_assessment")
    assessment = raw_assessment if isinstance(raw_assessment, Mapping) else {}
    computable = assessment.get("computable") is True
    method = assessment.get("method")
    safe_method = method if method == "native_ibkr_twr" and computable else None
    raw_missing = assessment.get("missing")
    missing = sorted({
        item for item in raw_missing
        if isinstance(item, str) and item in _MISSING_CODES
    }) if isinstance(raw_missing, (list, tuple)) else []
    raw_warnings = ledger.get("warnings")
    warnings = sorted({
        row.get("code") for row in raw_warnings
        if isinstance(row, Mapping)
        and isinstance(row.get("code"), str)
        and row.get("code") in _DIAGNOSTIC_CODES
    }) if isinstance(raw_warnings, (list, tuple)) else []

    return {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "status": "available" if safe_method else "incomplete",
        "method": safe_method,
        "period": {
            "from": _diagnostic_date(period.get("from")),
            "to": _diagnostic_date(period.get("to")),
        },
        "currency": currency,
        "missing": missing,
        "warnings": warnings,
    }


def _decimal_text(value: Any) -> str | None:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite():
        return None
    return format(amount, "f")


def _iso_date(value: Any) -> str | None:
    text = str(value or "").strip()
    for separator in (";", ",", " ", "T"):
        if separator in text:
            text = text.split(separator, 1)[0]
            break
    for date_format in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, date_format).date().isoformat()
        except ValueError:
            continue
    return None


def _statement(root: ElementTree.Element) -> ElementTree.Element:
    statements = root.findall("./FlexStatements/FlexStatement")
    if len(statements) != 1:
        raise FlexSourceError("IBKR Flex ledger requires exactly one statement account")
    return statements[0]


def _single_currency_total(
    entries: list[dict[str, Any]], *, fallback_currency: str | None
) -> dict[str, Any] | None:
    """Total entries only when they share one currency; never merge currencies."""

    currencies = {entry["currency"] for entry in entries}
    if len(currencies) > 1:
        return None
    currency = next(iter(currencies), fallback_currency)
    total = sum((Decimal(entry["amount"]) for entry in entries), Decimal(0))
    return {"amount": format(total, "f"), "currency": currency, "line_count": len(entries)}


def build_flex_ledger(
    report: FlexReport, *, expected_account_ids: Collection[str]
) -> dict[str, Any]:
    """Project a verified Activity Flex report into minimal ledger facts."""

    verify_report_accounts(report, expected_account_ids=expected_account_ids)
    root = ElementTree.fromstring(report.content)
    report_type = (root.get("type") or "").strip() or None
    if report_type is not None and report_type != "AF":
        raise FlexSourceError("IBKR Flex ledger requires an XML Activity report")
    statement = _statement(root)

    account_id = (statement.get("accountId") or "").strip()
    information = statement.find("AccountInformation")
    raw_currency = information.get("currency") if information is not None else ""
    base_currency = (raw_currency or "").strip().upper() or None

    period = {
        "from": _iso_date(statement.get("fromDate")),
        "to": _iso_date(statement.get("toDate")),
        "period": (statement.get("period") or "").strip() or None,
    }
    range_from = period["from"]
    range_to = period["to"]
    period_valid = range_from is not None and range_to is not None and range_from <= range_to

    ending_valuation = None
    if period_valid:
        for row in statement.findall("./EquitySummaryInBase/EquitySummaryByReportDateInBase"):
            if _iso_date(row.get("reportDate")) != range_to:
                continue
            amount = _decimal_text(row.get("total"))
            if amount is None:
                continue
            ending_valuation = {
                "amount": amount,
                "currency": (row.get("currency") or "").strip().upper() or None,
                "report_date": range_to,
                "source": "EquitySummaryByReportDateInBase.total",
            }
            break

    warnings: list[dict[str, Any]] = []
    external_flows: list[dict[str, Any]] = []
    fee_lines: list[dict[str, Any]] = []
    flows_incomplete = False
    fees_incomplete = False

    funds = statement.find("StmtFunds")
    lines = funds.findall("StatementOfFundsLine") if funds is not None else []
    for line in lines:
        code = (line.get("activityCode") or "").strip().upper()
        currency = (line.get("currency") or "").strip().upper() or None
        entry_date = _iso_date(line.get("date")) or _iso_date(line.get("reportDate"))
        if code in _EXTERNAL_FLOW_CODES:
            amount = _decimal_text(line.get("amount"))
            if amount is None or currency is None:
                flows_incomplete = True
                warnings.append({"code": "invalid_external_flow", "activity_code": code})
                continue
            external_flows.append({
                "date": entry_date,
                "activity_code": code,
                "direction": _EXTERNAL_FLOW_CODES[code],
                "currency": currency,
                "amount": amount,
            })
        elif code in _FEE_CODES:
            amount = _decimal_text(line.get("amount"))
            if amount is None or currency is None:
                fees_incomplete = True
                warnings.append({"code": "invalid_fee_line", "activity_code": code})
                continue
            fee_lines.append({
                "date": entry_date,
                "activity_code": code,
                "kind": "fee",
                "currency": currency,
                "amount": amount,
            })
        elif line.get("tradeCommission"):
            commission = _decimal_text(line.get("tradeCommission"))
            if commission is None or currency is None:
                fees_incomplete = True
                warnings.append({"code": "invalid_trade_commission", "activity_code": code or None})
                continue
            if Decimal(commission) != 0:
                fee_lines.append({
                    "date": entry_date,
                    "activity_code": code or None,
                    "kind": "trade_commission",
                    "currency": currency,
                    "amount": commission,
                })

    net_external_flow = None
    if funds is not None and not flows_incomplete:
        net_external_flow = _single_currency_total(external_flows, fallback_currency=base_currency)
        if net_external_flow is None:
            warnings.append({"code": "external_flows_mixed_currency"})
    fee_total = None
    if funds is not None and not fees_incomplete:
        fee_total = _single_currency_total(fee_lines, fallback_currency=base_currency)
        if fee_total is None:
            warnings.append({"code": "fees_mixed_currency"})

    fx_rates: list[dict[str, Any]] = []
    for row in statement.findall("./ConversionRates/ConversionRate"):
        rate = _decimal_text(row.get("rate"))
        from_currency = (row.get("fromCurrency") or "").strip().upper() or None
        to_currency = (row.get("toCurrency") or "").strip().upper() or None
        if rate is None or from_currency is None or to_currency is None:
            continue
        fx_rates.append({
            "from_currency": from_currency,
            "to_currency": to_currency,
            "rate": rate,
            "report_date": _iso_date(row.get("reportDate")),
        })

    native_returns: dict[str, Any] = {}
    change = statement.find("ChangeInNAV")
    if change is not None:
        twr_text = _decimal_text(change.get("twr"))
        change_from = _iso_date(change.get("fromDate"))
        change_to = _iso_date(change.get("toDate"))
        if twr_text is not None:
            if not period_valid:
                warnings.append({"code": "native_twr_period_unavailable"})
            elif change_from == range_from and change_to == range_to:
                native_returns = {
                    "twr": twr_text,
                    "unit": "percent",
                    "from": change_from,
                    "to": change_to,
                    "source": "ChangeInNAV.twr",
                }
            else:
                warnings.append({"code": "native_twr_period_mismatch"})

    unavailable = []
    if ending_valuation is None:
        unavailable.append("ending_valuation_unavailable")
    if funds is None:
        unavailable.append("external_flows_unavailable")
    elif flows_incomplete:
        unavailable.append("external_flows_incomplete")
    elif net_external_flow is None:
        unavailable.append("external_flows_mixed_currency")

    if native_returns:
        # IBKR reports ChangeInNAV.twr as a *percent*; expose the ratio for return use.
        ratio = format(Decimal(native_returns["twr"]) / Decimal(100), "f")
        return_assessment = {
            "computable": True,
            "method": "native_ibkr_twr",
            "value": ratio,
            "unit": "ratio",
            "source": native_returns["source"],
            "missing": [],
        }
    else:
        return_assessment = {
            "computable": False,
            "method": None,
            "value": None,
            "unit": None,
            "source": None,
            "missing": ["native_twr_unavailable", "return_method_not_wired", *unavailable],
        }

    declared_currency = "USD" if base_currency == "USD" else None
    if declared_currency is None:
        warnings.append({"code": "account_base_currency_not_usd"})

    return {
        "schema_version": SCHEMA_VERSION,
        "account_ids": [account_id],
        "currency": declared_currency,
        "account_base_currency": base_currency,
        "period": period,
        "ending_valuation": ending_valuation,
        "external_flows": external_flows,
        "net_external_flow": net_external_flow,
        "fee_lines": fee_lines,
        "fee_total": fee_total,
        "fx_rates": fx_rates,
        "native_returns": native_returns,
        "return_assessment": return_assessment,
        "warnings": warnings,
    }
