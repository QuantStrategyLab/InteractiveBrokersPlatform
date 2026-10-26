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

from collections import Counter
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
from application.ibkr_flex_executions import _day, _local_time, _number_shape, project_flex_executions
from application.ibkr_period_return import NAV_CHANGE_COMPONENT_FIELDS
from application.ibkr_external_flow import project_flex_external_flows


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
_CASHFLOW_SECTIONS = (
    ("CashTransactions", "CashTransaction", ("accountId", "currency", "amount", "type", "dateTime", "transactionID")),
    ("StmtFunds", "StatementOfFundsLine", ("accountId", "currency", "amount", "activityCode", "date", "transactionID")),
)
_CASHFLOW_OPTIONAL_FIELDS = ("dateTime", "date", "reportDate", "settleDate", "tradeID", "clientReference",
                             "code", "levelOfDetail", "timeZone", "timezone")
_CASHFLOW_DATE_FIELDS = ("dateTime", "date", "reportDate", "settleDate")
_CASHFLOW_ZONE_FIELDS = ("timeZone", "timezone")
# Fixed XML-name candidates for the public Change in NAV reference fields:
# https://www.ibkrguides.com/reportingreference/reportguide/changeinnav_fq.htm
# A candidate's presence does not establish its meaning, mode or completeness.
_NAV_TEXT_FIELDS = ("accountId", "acctAlias", "model", "currency", "mode", "fromDate", "toDate")
_NAV_NUMERIC_FIELDS = (
    "startingValue", "mtm", "realized", "changeInUnrealized", "depositsWithdrawals",
    "internalCashTransfers", "assetTransfers", "dividends", "withholdingTax", "withholding871m",
    "changeInDividendAccruals", "interest", "changeInInterestAccruals", "advisorFees", "clientFees",
    "otherFees", "feesReceivables", "commissions", "commissionReceivables", "forexCommissions",
    "transactionTax", "taxReceivables", "salesTax", "softDollars", "netFxTrading", "fxTranslation",
    "linkingAdjustments", "other", "endingValue", "twr",
)
# XML-name candidates for the official Cash Report reference, not a qualified
# spending contract. BASE_SUMMARY is never relabelled as a native USD row.
_CASH_REPORT_FIELDS = ("accountId", "model", "currency", "fromDate", "toDate", "reportDate",
                       "endingCash", "endingSettledCash")
CASH_REPORT_MISSING_CODES = frozenset({"cash_report_structure_ambiguous", "usd_cash_row_missing",
    "usd_cash_row_ambiguous", "cash_row_account_unverified", "cash_row_model_scope_unverified",
    "cash_period_mismatch", "cash_report_date_mismatch", "endingCash_missing_or_invalid",
    "endingSettledCash_missing_or_invalid"})


def _period_cash_facts(statement: ElementTree.Element, *, source_ref: str, period: Mapping[str, Any]) -> dict[str, Any]:
    """Private native period-end cash, never a current spending balance."""
    account = statement.get("accountId", "").strip()
    result = {"schema_version": "ibkr.flex_period_cash.v1", "status": "incomplete",
        "source_ref": source_ref, "account_ids": [account], "currency": "USD",
        "period": {key: period[key] for key in ("from", "to")},
        "scope": "STATEMENT_PERIOD_END_ONLY", "values": None, "missing": [],
        "spendable_cash_verified": False, "complete_cash_obligations_verified": False}
    sections = statement.findall("CashReport")
    rows = [row for section in sections for row in section.findall("CashReportCurrency")]
    usd = [row for row in rows if row.get("currency") == "USD"]
    missing = result["missing"]
    if len(sections) != 1 or any(row.tag != "CashReportCurrency" or len(row) for section in sections for row in section):
        missing.append("cash_report_structure_ambiguous")
    if len(usd) != 1:
        missing.append("usd_cash_row_missing" if not usd else "usd_cash_row_ambiguous")
        return result
    row = usd[0]
    if row.get("accountId") != account:
        missing.append("cash_row_account_unverified")
    if row.get("model") != "":
        missing.append("cash_row_model_scope_unverified")
    if (not period["from"] or not period["to"] or period["from"] > period["to"]
            or any(_day(row.get(key)) != period[label] or _day(statement.get(key)) != period[label]
                   for key, label in (("fromDate", "from"), ("toDate", "to")))):
        missing.append("cash_period_mismatch")
    if row.get("reportDate") not in (None, "") and _day(row.get("reportDate")) != period["to"]:
        missing.append("cash_report_date_mismatch")
    for field in ("endingCash", "endingSettledCash"):
        value = row.get(field)
        if not isinstance(value, str) or value != value.strip() or _number_shape(value)["kind"] != "decimal":
            missing.append(field + "_missing_or_invalid")
    if not missing:
        result.update(status="available_period_end", values={field: row.get(field) for field in ("endingCash", "endingSettledCash")})
    return result


def _cash_report_source_diagnostic(statement: ElementTree.Element) -> dict[str, Any]:
    sections = statement.findall("CashReport")
    rows = [row for section in sections for row in section.findall("CashReportCurrency")]
    usd_rows = [row for row in rows if row.get("currency") == "USD"]
    fields = {}
    for field in _CASH_REPORT_FIELDS if usd_rows else ():
        types = Counter()
        for row in usd_rows:
            value = row.get(field)
            kind = "missing" if value is None else "empty_text" if not value else "text"
            if field in {"endingCash", "endingSettledCash"} and value:
                kind = _number_shape(value)["kind"] + "_text"
            if field in {"fromDate", "toDate", "reportDate"} and value:
                kind = "date_text" if _day(value) else "unsupported_text"
            types[kind] += 1
        fields[field] = dict(sorted(types.items()))
    return {
        "schema_version": "ibkr.flex_cash_report_source_shape.v1",
        "section_count": len(sections), "record_count": len(rows), "usd_record_count": len(usd_rows),
        "unsupported_record_count": sum(len(section) for section in sections) - len(rows),
        "unknown_field_count": sum(len(set(row.attrib) - set(_CASH_REPORT_FIELDS)) for row in usd_rows),
        "usd_field_type_counts": fields,
        "coverage": "not_evaluated", "spendable_cash_verified": False,
        "complete_cash_obligations_verified": False, "missing_fields_are_zero": False,
    }


def build_flex_cash_diagnostic(ledger: Mapping[str, Any]) -> dict[str, Any]:
    """Fixed cash source shape only; amounts and account identifiers stay private."""
    period = ledger["period"]
    shape = ledger["cash_report_source_diagnostic"]
    return {
        "schema_version": "ibkr_flex_cash_diagnostic.v1",
        "status": "observed_source_shape",
        "source_ref": ledger["source_ref"],
        "period": {key: _diagnostic_date(period.get(key)) for key in ("from", "to")},
        "currency": "USD", "scope": "STATEMENT_PERIOD_END_ONLY", "cash_report": shape,
        "account_capabilities": ledger.get("account_capabilities", "MISSING"),
        "period_cash": {key: ledger["period_cash_facts"][key] for key in ("status", "missing")},
    }


def build_flex_cashflow_diagnostic(ledger: Mapping[str, Any]) -> dict[str, Any]:
    """Source formats only; no cash-flow values, coverage or timing inference."""
    return {
        "schema_version": "ibkr_flex_cashflow_diagnostic.v1",
        "status": "observed_source_shape",
        "source_ref": ledger["source_ref"],
        "period": {key: _diagnostic_date(ledger["period"].get(key)) for key in ("from", "to")},
        "cashflow_source_diagnostic": ledger["cashflow_source_diagnostic"],
    }


def _cashflow_timezone_format(value: str | None) -> str:
    if value is None:
        return "missing"
    if not value:
        return "empty_text"
    if len(value) > 128:
        return "unsupported"
    if value in {"Z", "UTC", "GMT", "Etc/UTC"}:
        return "utc_label"
    offset = re.fullmatch(r"[+-]([0-9]{2}):?([0-9]{2})", value, re.ASCII)
    if offset and int(offset[1]) < 24 and int(offset[2]) < 60:
        return "numeric_offset"
    if re.fullmatch(r"[A-Za-z]{2,5}", value, re.ASCII):
        return "abbreviation"
    if re.fullmatch(r"[A-Za-z_]+(?:/[A-Za-z0-9_+-]+)+", value, re.ASCII):
        return "iana_like"
    return "unsupported"


def _cashflow_datetime_format(value: str | None) -> str:
    if value is None:
        return "missing"
    if not value:
        return "empty_text"
    if len(value) > 128:
        return "unsupported"
    if _day(value):
        return "date_only"
    day = _day(re.split(r"[;T ]", value, maxsplit=1)[0])
    if not day:
        return "unsupported"
    if _local_time(value.replace("T", ";"), day):
        return "local_datetime"
    zoned = re.fullmatch(r"(.+?)(Z|[+-][0-9]{2}:?[0-9]{2}| [A-Za-z]{2,5}| [A-Za-z_]+(?:/[A-Za-z0-9_+-]+)+)", value)
    if zoned and _local_time(zoned[1].replace("T", ";"), day):
        return {"utc_label": "utc_datetime", "numeric_offset": "offset_datetime",
                "abbreviation": "named_zone_datetime", "iana_like": "named_zone_datetime"}.get(
                    _cashflow_timezone_format(zoned[2].lstrip()), "unsupported")
    return "unsupported"


def _cashflow_source_diagnostic(statement: ElementTree.Element) -> dict[str, Any]:
    """Fixed names and aggregate formats only; never echo XML keys or values.

    This observes source shape, not valid identity, effective times, cash-flow
    classification, filter settings or complete coverage. It performs no I/O.
    """
    sections = {}
    for section_name, row_name, required in _CASHFLOW_SECTIONS:
        containers = statement.findall(section_name)
        rows = [row for section in containers for row in section.findall(row_name)]
        fields = tuple(dict.fromkeys((*required, *_CASHFLOW_OPTIONAL_FIELDS)))
        field_types = {}
        for field in fields if rows else ():
            types = Counter()
            for row in rows:
                value = row.get(field)
                kind = "missing" if value is None else "empty_text" if not value else "text"
                if field == "amount" and value:
                    kind = _number_shape(value)["kind"] + "_text"
                types[kind] += 1
            field_types[field] = dict(sorted(types.items()))
        sections[section_name] = {
            "section_count": len(containers), "record_count": len(rows),
            "unsupported_record_count": sum(len(section) for section in containers) - len(rows),
            "field_type_counts": field_types,
            "date_time_format_counts": {field: dict(sorted(Counter(
                _cashflow_datetime_format(row.get(field)) for row in rows).items()))
                for field in _CASHFLOW_DATE_FIELDS if rows},
            "timezone_format_counts": {field: dict(sorted(Counter(
                _cashflow_timezone_format(row.get(field)) for row in rows).items()))
                for field in _CASHFLOW_ZONE_FIELDS if rows},
            "missing_required_fields": sorted(field for field in required
                                              if any(not row.get(field) for row in rows)),
        }
    return {"schema_version": "ibkr.flex_cashflow_source_shape.v1", "coverage": "not_evaluated", "sections": sections}


def _nav_source_diagnostic(statement: ElementTree.Element) -> dict[str, Any]:
    """Count fixed NAV candidates, never values or unknown attribute names.

    This does not authenticate a NAV bridge, mode, base currency, capital-flow
    coverage or absent fields as zero. It does not calculate money P/L.
    """
    rows = statement.findall("ChangeInNAV")
    fields = (*_NAV_TEXT_FIELDS, *_NAV_NUMERIC_FIELDS)
    field_types = {}
    for field in fields if rows else ():
        types = Counter()
        for row in rows:
            value = row.get(field)
            kind = "missing" if value is None else "empty_text" if not value else "text"
            if field in _NAV_NUMERIC_FIELDS and value:
                kind = _number_shape(value)["kind"] + "_text"
            types[kind] += 1
        field_types[field] = dict(sorted(types.items()))
    return {
        "schema_version": "ibkr.flex_nav_source_shape.v1", "coverage": "not_evaluated",
        "nav_bridge": "not_evaluated", "missing_fields_are_zero": False,
        "record_count": len(rows), "unsupported_record_count": sum(len(row) for row in rows),
        "unknown_field_count": sum(len(set(row.attrib) - set(fields)) for row in rows),
        "field_type_counts": field_types,
        "date_time_format_counts": {field: dict(sorted(Counter(
            _cashflow_datetime_format(row.get(field)) for row in rows).items()))
            for field in ("fromDate", "toDate") if rows},
    }


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


def _statement(root: ElementTree.Element, expected: Collection[str]) -> ElementTree.Element:
    expected = {account.strip() for account in expected}
    statements = [s for s in root.findall("./FlexStatements/FlexStatement") if s.get("accountId", "").strip() in expected]
    if len(statements) != 1:
        raise FlexSourceError("IBKR Flex ledger requires exactly one statement account")
    statement = statements[0]
    account_id = statement.get("accountId", "").strip()
    financial_rows = {"AccountInformation", "EquitySummaryByReportDateInBase", "StatementOfFundsLine", "ConversionRate", "ChangeInNAV", "Trade", "CashReportCurrency"}
    if any(row.get("accountId", "").strip() not in {"", account_id} for row in statement.iter() if row.tag in financial_rows):
        error = FlexSourceError("IBKR Flex statement row account scope mismatch", endpoint="GetStatement", category="scope")
        error.safe_diagnostic["nested_account_mismatch"] = True
        raise error
    return statement


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
    report: FlexReport, *, expected_account_ids: Collection[str],
    allowed_report_account_ids: Collection[str] | None = None,
) -> dict[str, Any]:
    """Project a verified Activity Flex report into minimal ledger facts."""

    verify_report_accounts(report, expected_account_ids=expected_account_ids,
                           allowed_report_account_ids=allowed_report_account_ids)
    root = ElementTree.fromstring(report.content)
    report_type = (root.get("type") or "").strip() or None
    if report_type is not None and report_type != "AF":
        raise FlexSourceError("IBKR Flex ledger requires an XML Activity report", endpoint="GetStatement", category="format")
    statement = _statement(root, expected_account_ids)

    account_id = (statement.get("accountId") or "").strip()
    information = statement.find("AccountInformation")
    raw_currency = information.get("currency") if information is not None else ""
    base_currency = (raw_currency or "").strip().upper() or None
    # AccountType describes the account structure, not cash/margin capability.
    capabilities = "MISSING"
    if len(statement.findall("AccountInformation")) > 1:
        capabilities = "AMBIGUOUS"
    elif information is not None and (raw := information.get("accountCapabilities", "").strip()):
        capabilities = {"CASH": "CASH", "MARGIN": "MARGIN", "PORTFOLIO MARGIN": "PORTFOLIO_MARGIN"}.get(
            raw.upper(), "UNRECOGNIZED")

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

    # Preserve only an unambiguous account-total NAV row. These private source
    # components are independently qualified by the period-return producer;
    # they never enter the safe diagnostics or alter native TWR availability.
    native_nav_change = None
    if (
        change is not None and len(statement.findall("ChangeInNAV")) == 1 and not len(change)
        and len(statement.findall("AccountInformation")) == 1 and period_valid
        and change.get("accountId") == account_id and change.get("model") == ""
        and isinstance(base_currency, str) and re.fullmatch(r"[A-Z]{3}", base_currency)
        and information.get("currency") == base_currency
        and change.get("currency") in {base_currency, "BASE_SUMMARY"}
        and _day(change.get("fromDate")) == range_from and _day(change.get("toDate")) == range_to
    ):
        native_nav_change = {
            "source": "ChangeInNAV", "account_id": account_id, "model": "",
            "currency": change.get("currency"), "period": {"from": range_from, "to": range_to},
            "components": {key: change.get(key) for key in NAV_CHANGE_COMPONENT_FIELDS},
        }

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
        "source_ref": report.content_sha256,
        "executions": project_flex_executions(statement, source_ref=report.content_sha256, period=period),
        "cashflow_source_diagnostic": _cashflow_source_diagnostic(statement),
        "nav_source_diagnostic": _nav_source_diagnostic(statement),
        "cash_report_source_diagnostic": _cash_report_source_diagnostic(statement),
        "period_cash_facts": _period_cash_facts(statement, source_ref=report.content_sha256, period=period),
        "account_ids": [account_id],
        "currency": declared_currency,
        "account_base_currency": base_currency,
        "account_capabilities": capabilities,
        "period": period,
        "ending_valuation": ending_valuation,
        "external_flows": external_flows,
        "external_flow_facts": project_flex_external_flows(statement, period=period),
        "net_external_flow": net_external_flow,
        "fee_lines": fee_lines,
        "fee_total": fee_total,
        "fx_rates": fx_rates,
        "native_returns": native_returns,
        **({"native_nav_change": native_nav_change} if native_nav_change is not None else {}),
        "return_assessment": return_assessment,
        "warnings": warnings,
    }
