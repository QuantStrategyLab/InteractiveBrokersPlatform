"""Run an opt-in, status-only IBKR Flex diagnostic in an approved cloud job."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Sequence
from datetime import date
from typing import Any

from application.account_facts_target_config import account_selector, parse_additional_target_configs
from application.ibkr_flex_source import (
    CONFIGURED_COVERAGE_SECTIONS, CONFIGURED_COVERAGE_SLOTS, FlexReportPending, FlexSourceError,
    diagnose_activity_flex_ledger, diagnose_configured_flex_coverage,
)
from application.ibkr_flex_ledger import CASH_REPORT_MISSING_CODES
from application.ibkr_flex_executions import EXECUTION_SOURCE_FIELDS, FlexExecutionsError


_SCHEMA_VERSION = "ibkr_flex_diagnostic.v1"
_ENABLE_ENV = "IBKR_FLEX_DIAGNOSTIC_ENABLED"
_TOKEN_ENV = "IBKR_FLEX_TOKEN"
_QUERY_ID_ENV = "IBKR_FLEX_QUERY_ID"
_ACCOUNT_SCOPE_ENV = "IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON"
_PERIOD_FROM_ENV = "IBKR_FLEX_EXPECTED_PERIOD_FROM"
_PERIOD_TO_ENV = "IBKR_FLEX_EXPECTED_PERIOD_TO"
_SAFE_MISSING_CODES = frozenset({
    "native_twr_unavailable",
    "return_method_not_wired",
    "ending_valuation_unavailable",
    "external_flows_unavailable",
    "external_flows_incomplete",
    "external_flows_mixed_currency",
})
_SAFE_WARNING_CODES = frozenset({
    "native_twr_period_unavailable",
    "native_twr_period_mismatch",
    "account_base_currency_not_usd",
    "invalid_external_flow",
    "invalid_fee_line",
    "invalid_trade_commission",
    "external_flows_mixed_currency",
    "fees_mixed_currency",
})


def _empty_report(*, status: str, missing: list[str]) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "status": status,
        "method": None,
        "period": {"from": None, "to": None},
        "currency": None,
        "missing": missing,
        "warnings": [],
    }


def _expected_account_ids() -> tuple[str, ...] | None:
    raw = os.environ.get(_ACCOUNT_SCOPE_ENV)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return None
    if (
        not isinstance(value, list)
        or len(value) != 1
        or not isinstance(value[0], str)
        or re.fullmatch(r"(?:U|DU)[0-9]+", value[0]) is None
    ):
        return None
    return tuple(value)


def _expected_period() -> dict[str, str] | None:
    start, end = os.environ.get(_PERIOD_FROM_ENV), os.environ.get(_PERIOD_TO_ENV)
    if start in (None, "") and end in (None, ""):
        return None
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        if first.isoformat() != start or last.isoformat() != end or first > last:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("expected_period_invalid") from None
    return {"from": start, "to": end}


def _approved_report_accounts(expected: tuple[str, ...] | None) -> tuple[str, ...] | None:
    """Use only complete protected configuration; absent configuration is exact-only."""
    primary_raw = os.environ.get("IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON")
    additional_raw = os.environ.get("IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON")
    if not primary_raw and not additional_raw:
        return None
    if not primary_raw or not additional_raw:
        raise ValueError("approved_report_scope_invalid")
    primary = account_selector(json.loads(primary_raw))
    configs = parse_additional_target_configs(additional_raw)
    allowed = (*primary, *(config["account_selector"][0] for config in configs.values()))
    if (len(primary) != 1 or primary != expected or len(allowed) != len(set(allowed))
            or any(re.fullmatch(r"(?:U|DU)[0-9]+", account) is None for account in allowed)):
        raise ValueError("approved_report_scope_invalid")
    return allowed


def _cashflow_request_period() -> dict[str, str] | None:
    start = os.environ.get("IBKR_FLEX_CASHFLOW_REQUEST_FROM", "")
    end = os.environ.get("IBKR_FLEX_CASHFLOW_REQUEST_TO", "")
    if not start and not end:
        return None
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        if first.isoformat() != start or last.isoformat() != end or not 0 <= (last - first).days <= 30:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("cashflow_request_period_invalid") from None
    return {"from": start, "to": end}


def _validated_report(value: object) -> dict[str, Any]:
    """Select the only fields permitted on stdout, even if the caller changes."""

    if not isinstance(value, dict):
        return _empty_report(status="error", missing=["flex_import_failed"])
    raw_period = value.get("period")
    period = raw_period if isinstance(raw_period, dict) else {}
    period_from = period.get("from")
    period_to = period.get("to")
    try:
        safe_from = date.fromisoformat(period_from).isoformat() if isinstance(period_from, str) else None
    except ValueError:
        safe_from = None
    try:
        safe_to = date.fromisoformat(period_to).isoformat() if isinstance(period_to, str) else None
    except ValueError:
        safe_to = None
    if safe_from != period_from:
        safe_from = None
    if safe_to != period_to:
        safe_to = None
    raw_missing = value.get("missing")
    missing = [
        item for item in raw_missing
        if isinstance(item, str) and item in _SAFE_MISSING_CODES
    ] if isinstance(raw_missing, list) else []
    raw_warnings = value.get("warnings")
    warnings = [
        item for item in raw_warnings
        if isinstance(item, str) and item in _SAFE_WARNING_CODES
    ] if isinstance(raw_warnings, list) else []
    raw_currency = value.get("currency")
    currency = raw_currency if isinstance(raw_currency, str) and re.fullmatch(r"[A-Z]{3}", raw_currency) else None
    raw_method = value.get("method")
    method = raw_method if raw_method == "native_ibkr_twr" else None
    raw_status = value.get("status")
    status = raw_status if raw_status in {"available", "incomplete"} else "error"
    if status == "available" and (method is None or currency is None
            or safe_from is None or safe_to is None or safe_from > safe_to):
        return _empty_report(status="error", missing=["flex_diagnostic_result_invalid"])
    return {
        "schema_version": _SCHEMA_VERSION,
        "status": status,
        "method": method,
        "period": {
            "from": safe_from,
            "to": safe_to,
        },
        "currency": currency,
        "missing": sorted(set(missing)),
        "warnings": sorted(set(warnings)),
    }


def _validated_cash_report(value: object) -> dict[str, Any]:
    """Rebuild only fixed keys, bounded counts, dates and a content digest."""
    invalid = _empty_report(status="error", missing=["flex_cash_diagnostic_result_invalid"])
    if not isinstance(value, dict):
        return invalid
    shape, period = value.get("cash_report"), value.get("period")
    if (value.get("schema_version") != "ibkr_flex_cash_diagnostic.v1"
            or value.get("status") != "observed_source_shape"
            or not isinstance(value.get("source_ref"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", value["source_ref"])
            or not isinstance(shape, dict) or not isinstance(period, dict)):
        return invalid
    try:
        safe_period = {key: date.fromisoformat(period[key]).isoformat() for key in ("from", "to")}
    except (KeyError, TypeError, ValueError):
        return invalid
    if (safe_period != period or safe_period["from"] > safe_period["to"]
            or shape.get("schema_version") != "ibkr.flex_cash_report_source_shape.v1"):
        return invalid
    count_keys = ("section_count", "record_count", "usd_record_count", "unsupported_record_count", "unknown_field_count")
    if any(type(shape.get(key)) is not int or not 0 <= shape[key] <= 8 * 1024 * 1024 for key in count_keys):
        return invalid
    counts = {key: shape[key] for key in count_keys}
    fields = shape.get("usd_field_type_counts")
    names = {"accountId", "model", "currency", "fromDate", "toDate", "reportDate", "endingCash", "endingSettledCash"}
    kinds = {"missing", "empty_text", "text", "date_text", "unsupported_text", "decimal_text",
             "scientific_text", "nonfinite_text", "oversized_text"}
    if (counts["usd_record_count"] > counts["record_count"] or not isinstance(fields, dict)
            or set(fields) != (names if counts["usd_record_count"] else set())):
        return invalid
    safe_fields = {}
    for name, types in fields.items():
        if (not isinstance(types, dict) or not types or set(types) - kinds
                or any(type(n) is not int or n <= 0 or n > counts["usd_record_count"] for n in types.values())
                or sum(types.values()) != counts["usd_record_count"]):
            return invalid
        safe_fields[name] = dict(types)
    facts = value.get("period_cash")
    capabilities = value.get("account_capabilities", "MISSING")
    if (not isinstance(capabilities, str)
            or capabilities not in {"CASH", "MARGIN", "PORTFOLIO_MARGIN", "MISSING", "AMBIGUOUS", "UNRECOGNIZED"}):
        return invalid
    if (not isinstance(facts, dict) or facts.get("status") not in {"available_period_end", "incomplete"}
            or not isinstance(facts.get("missing"), list)
            or any(not isinstance(code, str) or code not in CASH_REPORT_MISSING_CODES for code in facts["missing"])
            or (facts["status"] == "available_period_end") != (not facts["missing"])):
        return invalid
    return {
        "schema_version": "ibkr_flex_cash_diagnostic.v1", "status": "observed_source_shape",
        "source_ref": value["source_ref"], "period": safe_period,
        "currency": "USD", "scope": "STATEMENT_PERIOD_END_ONLY",
        "account_capabilities": capabilities,
        "period_cash": {"status": facts["status"], "missing": sorted(set(facts["missing"]))},
        "cash_report": {"schema_version": "ibkr.flex_cash_report_source_shape.v1", **counts,
            "usd_field_type_counts": safe_fields, "coverage": "not_evaluated",
            "spendable_cash_verified": False, "complete_cash_obligations_verified": False,
            "missing_fields_are_zero": False},
    }


def _validated_cashflow_report(value: object) -> dict[str, Any]:
    """Rebuild fixed public field/format counts; never forward dynamic XML keys."""
    invalid = _empty_report(status="error", missing=["flex_cashflow_diagnostic_result_invalid"])
    if not isinstance(value, dict):
        return invalid
    shape, period = value.get("cashflow_source_diagnostic"), value.get("period")
    if (value.get("schema_version") != "ibkr_flex_cashflow_diagnostic.v1"
            or value.get("status") != "observed_source_shape"
            or not isinstance(value.get("source_ref"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", value["source_ref"])
            or not isinstance(shape, dict) or not isinstance(period, dict)):
        return invalid
    try:
        safe_period = {key: date.fromisoformat(period[key]).isoformat() for key in ("from", "to")}
    except (KeyError, TypeError, ValueError):
        return invalid
    if (safe_period != period or safe_period["from"] > safe_period["to"]
            or shape.get("schema_version") != "ibkr.flex_cashflow_source_shape.v1"
            or shape.get("coverage") != "not_evaluated"
            or not isinstance(shape.get("sections"), dict)
            or set(shape["sections"]) != {"CashTransactions", "StmtFunds"}):
        return invalid
    common = {"accountId", "currency", "amount", "transactionID"}
    optional = {"dateTime", "date", "reportDate", "settleDate", "tradeID", "clientReference",
                "code", "levelOfDetail", "timeZone", "timezone"}
    kinds = {"missing", "empty_text", "text", "decimal_text", "scientific_text", "nonfinite_text",
             "oversized_text", "unsupported_text"}
    time_kinds = {"missing", "empty_text", "date_only", "local_datetime", "utc_datetime",
                  "offset_datetime", "named_zone_datetime", "unsupported"}
    zone_kinds = {"missing", "empty_text", "utc_label", "numeric_offset", "abbreviation", "iana_like", "unsupported"}
    sections = {}
    for name in ("CashTransactions", "StmtFunds"):
        row = shape["sections"][name]
        required = common | ({"type", "dateTime"} if name == "CashTransactions" else {"activityCode", "date"})
        counts = ("section_count", "record_count", "unsupported_record_count")
        if (not isinstance(row, dict)
                or any(type(row.get(key)) is not int or not 0 <= row[key] <= 8 * 1024 * 1024 for key in counts)):
            return invalid
        total = row["record_count"]
        safe_row = {key: row[key] for key in counts}
        maps = (("field_type_counts", required | optional, kinds),
                ("date_time_format_counts", {"dateTime", "date", "reportDate", "settleDate"}, time_kinds),
                ("timezone_format_counts", {"timeZone", "timezone"}, zone_kinds))
        for key, fields, allowed in maps:
            field_counts = row.get(key)
            if not isinstance(field_counts, dict) or set(field_counts) != (fields if total else set()):
                return invalid
            safe_row[key] = {}
            for field, formats in field_counts.items():
                if (not isinstance(formats, dict) or not formats or set(formats) - allowed
                        or any(type(n) is not int or not 0 < n <= total for n in formats.values())
                        or sum(formats.values()) != total):
                    return invalid
                safe_row[key][field] = dict(formats)
        missing = row.get("missing_required_fields")
        if (not isinstance(missing, list) or any(not isinstance(field, str) or field not in required for field in missing)
                or len(missing) != len(set(missing))):
            return invalid
        safe_row["missing_required_fields"] = list(missing)
        sections[name] = safe_row
    return {"schema_version": "ibkr_flex_cashflow_diagnostic.v1", "status": "observed_source_shape",
            "source_ref": value["source_ref"], "period": safe_period,
            "cashflow_source_diagnostic": {"schema_version": "ibkr.flex_cashflow_source_shape.v1",
                                           "coverage": "not_evaluated", "sections": sections}}


def _validated_coverage_report(value: object) -> dict[str, Any]:
    """Rebuild only fixed slots, dates, booleans and bounded native-row counts."""
    invalid = _empty_report(status="error", missing=["flex_coverage_diagnostic_result_invalid"])
    if (not isinstance(value, dict) or value.get("schema_version") != "ibkr_flex_configured_coverage.v1"
            or value.get("status") != "observed_source_shape" or value.get("coverage") != "not_evaluated"
            or not isinstance(value.get("source_ref"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", value["source_ref"])
            or not isinstance(value.get("accounts"), dict)
            or set(value["accounts"]) != set(CONFIGURED_COVERAGE_SLOTS)):
        return invalid
    accounts = {}
    for slot in CONFIGURED_COVERAGE_SLOTS:
        row = value["accounts"][slot]
        if (not isinstance(row, dict) or type(row.get("statement_present")) is not bool
                or not isinstance(row.get("period"), dict) or set(row["period"]) != {"from", "to"}
                or not isinstance(row.get("sections"), dict)
                or set(row["sections"]) != set(CONFIGURED_COVERAGE_SECTIONS)):
            return invalid
        period = row["period"]
        for key in ("from", "to"):
            try:
                if period[key] is not None and date.fromisoformat(period[key]).isoformat() != period[key]:
                    return invalid
            except (TypeError, ValueError):
                return invalid
        if period["from"] and period["to"] and period["from"] > period["to"]:
            return invalid
        present, twr = row["statement_present"], row.get("native_twr_field_present")
        if ((present and type(twr) is not bool) or (not present and
                (twr is not None or period != {"from": None, "to": None}))):
            return invalid
        sections = {}
        for name in CONFIGURED_COVERAGE_SECTIONS:
            section = row["sections"][name]
            if not isinstance(section, dict):
                return invalid
            counts = {key: section.get(key) for key in
                      ("section_count", "record_count", "unsupported_record_count")}
            if any(type(counts[key]) is not int or not 0 <= counts[key] <= 8 * 1024 * 1024
                   for key in ("section_count", "unsupported_record_count")):
                return invalid
            total = counts["record_count"]
            if ((counts["section_count"] == 0 and (total is not None or counts["unsupported_record_count"] != 0))
                    or (counts["section_count"] > 0 and
                        (not present or type(total) is not int or not 0 <= total <= 8 * 1024 * 1024))):
                return invalid
            sections[name] = counts
        accounts[slot] = {"statement_present": present, "period": dict(period),
                          "native_twr_field_present": twr, "sections": sections}
    if value.get("period") != accounts["primary"]["period"] or not accounts["primary"]["statement_present"]:
        return invalid
    return {"schema_version": "ibkr_flex_configured_coverage.v1", "status": "observed_source_shape",
            "coverage": "not_evaluated", "source_ref": value["source_ref"],
            "period": accounts["primary"]["period"], "accounts": accounts}


def _validated_execution_report(value: object) -> dict[str, Any]:
    """Rebuild the closed source-evidence schema; never forward native keys."""
    invalid = _empty_report(status="error", missing=["flex_execution_diagnostic_result_invalid"])
    if not isinstance(value, dict):
        return invalid
    fixed = {"schema_version": "ibkr_flex_execution_source.v1", "status": "observed_source_shape",
             "report_evidence": "newly_generated_report", "coverage": "not_evaluated",
             "correction_resolution": "not_applied"}
    if (any(value.get(key) != expected for key, expected in fixed.items())
            or value.get("publication_authority_granted") is not False
            or type(value.get("section_present")) is not bool
            or not isinstance(value.get("source_ref"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", value["source_ref"])):
        return invalid

    def count(number, limit=10000):
        return type(number) is int and 0 <= number <= limit

    period = value.get("period")
    try:
        if not isinstance(period, dict) or set(period) != {"from", "to"}:
            return invalid
        start, end = (date.fromisoformat(period[key]) for key in ("from", "to"))
        if (start.isoformat() != period["from"] or end.isoformat() != period["to"]
                or not 0 <= (end - start).days <= 30):
            return invalid
    except (TypeError, ValueError):
        return invalid
    total = value.get("native_record_count")
    keys = {"execution_rows", "other_level_rows", "missing_identity_rows", "economic_fields_unknown_rows",
            "explicit_correction_signal_rows", "notes_correction_signal_rows", "original_reference_signal_rows",
            "notes_unknown_rows", "original_fields_unknown_rows",
            "repeated_identity_groups", "identity_conflict_groups", "suffix_family_candidate_groups"}
    counts, fields, dates = (value.get(key) for key in ("counts", "field_counts", "date_counts"))
    if (not count(total) or not isinstance(counts, dict) or set(counts) != keys
            or any(not count(number, total) for number in counts.values())
            or counts["execution_rows"] + counts["other_level_rows"] != total
            or not isinstance(fields, dict) or set(fields) != set(EXECUTION_SOURCE_FIELDS)
            or not isinstance(dates, dict) or len(dates) > 31):
        return invalid
    safe_fields = {}
    for key in EXECUTION_SOURCE_FIELDS:
        row = fields[key]
        if (not isinstance(row, dict) or set(row) != {"present", "nonempty"}
                or not count(row["present"], total) or not count(row["nonempty"], row["present"])):
            return invalid
        safe_fields[key] = {name: row[name] for name in ("present", "nonempty")}
    for day, number in dates.items():
        try:
            parsed = date.fromisoformat(day)
            if parsed.isoformat() != day or not start <= parsed <= end or not count(number, total):
                return invalid
        except (TypeError, ValueError):
            return invalid
    if sum(dates.values()) != total:
        return invalid
    groups, omitted = value.get("economic_similarity_groups"), value.get("omitted_similarity_group_count")
    if not isinstance(groups, list) or len(groups) > 32 or not count(omitted):
        return invalid
    safe_groups = []
    for group in groups:
        if (not isinstance(group, dict) or not isinstance(group.get("comparison_sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", group["comparison_sha256"])
                or group.get("date") not in dates or not count(group.get("record_count"), total)
                or group["record_count"] < 2
                or not count(group.get("distinct_native_id_count"), group["record_count"])
                or not count(group.get("suffix_family_candidate_groups"), group["record_count"])
                or not count(group.get("explicit_correction_signal_rows"), group["record_count"])
                or group.get("fee_comparison") not in {"unknown", "same", "different"}):
            return invalid
        safe_groups.append({key: group[key] for key in ("comparison_sha256", "date", "record_count",
                                                       "distinct_native_id_count", "suffix_family_candidate_groups",
                                                       "explicit_correction_signal_rows", "fee_comparison")})
    return {**fixed, "publication_authority_granted": False, "section_present": value["section_present"],
            "source_ref": value["source_ref"], "period": dict(period), "native_record_count": total,
            "field_counts": safe_fields, "counts": {key: counts[key] for key in sorted(keys)},
            "date_counts": dict(dates), "economic_similarity_groups": safe_groups,
            "omitted_similarity_group_count": omitted}


def _safe_source_failure(value: object) -> dict[str, Any]:
    """Project existing structured errors only; never serialize an exception."""
    if not isinstance(value, dict):
        return {}
    result = {}
    endpoint = value.get("source_endpoint")
    kind = value.get("source_failure_kind")
    if isinstance(endpoint, str) and endpoint in {"SendRequest", "GetStatement"}:
        result["source_endpoint"] = endpoint
    if isinstance(kind, str) and kind in {"http", "transport", "xml", "provider", "size", "scope", "format"}:
        result["source_failure_kind"] = kind
    code, status = value.get("provider_error_code"), value.get("http_status")
    if isinstance(code, str) and re.fullmatch(r"1[0-9]{3}", code):
        result["provider_error_code"] = code
    if type(status) is int and 100 <= status <= 599:
        result["http_status"] = status
    for key in ("reported_account_count", "reported_unique_account_count", "target_statement_count",
                "target_period_count", "unknown_account_count"):
        number = value.get(key)
        if type(number) is int and 0 <= number <= 8 * 1024 * 1024:
            result[key] = number
    for key in ("target_accounts_present", "nested_account_mismatch"):
        if type(value.get(key)) is bool:
            result[key] = value[key]
    endpoint, kind = result.get("source_endpoint"), result.get("source_failure_kind")
    if endpoint == "SendRequest":
        result["failure_stage"] = "send_request"
    elif endpoint == "GetStatement" and kind in {"scope", "format"}:
        result["failure_stage"] = "verify_report"
    elif endpoint == "GetStatement" and kind in {"http", "transport", "xml", "provider", "size"}:
        result["failure_stage"] = "get_statement"
    return result


def main(argv: Sequence[str] = ()) -> int:
    args = tuple(argv)
    coverage = args in (("--configured-account-coverage-read-once",),
                        ("--preflight", "--configured-account-coverage-only"))
    execution = args in (("--execution-read-once",), ("--preflight", "--execution-source-only"))
    preflight = args in (("--preflight",), ("--preflight", "--configured-account-coverage-only"),
                        ("--preflight", "--execution-source-only"))
    if args not in ((), ("--preflight",), ("--read-once",), ("--cash-read-once",), ("--cashflow-read-once",)
                    ) and not coverage and not execution:
        print(json.dumps(_empty_report(status="incomplete", missing=["arguments_invalid"]), sort_keys=True))
        return 2
    if not preflight and os.environ.get(_ENABLE_ENV) != "true":
        print(json.dumps(_empty_report(status="disabled", missing=["flex_import_not_enabled"]), sort_keys=True))
        return 2

    missing: list[str] = []
    token = os.environ.get(_TOKEN_ENV, "")
    query_id = os.environ.get(_QUERY_ID_ENV, "")
    account_ids = _expected_account_ids()
    if not token:
        missing.append("flex_token_missing")
    elif token != token.strip() or any(ord(char) < 0x20 or ord(char) == 0x7f for char in token):
        missing.append("flex_token_invalid")
    if not re.fullmatch(r"[0-9]+", query_id):
        missing.append("flex_query_id_missing_or_invalid")
    if account_ids is None:
        missing.append("expected_account_scope_missing_or_invalid")
    expected_period = None
    try:
        expected_period = _expected_period()
    except ValueError:
        missing.append("expected_period_invalid")
    request_period = None
    try:
        request_period = _cashflow_request_period()
        if request_period is not None and (args not in (("--preflight",), ("--cashflow-read-once",))
                or expected_period is not None and expected_period != request_period):
            raise ValueError
    except ValueError:
        missing.append("cashflow_request_period_invalid")
    if execution:
        if (expected_period is None or
                (date.fromisoformat(expected_period["to"]) - date.fromisoformat(expected_period["from"])).days > 30):
            missing.append("execution_request_period_required_or_invalid")
    try:
        allowed_accounts = _approved_report_accounts(account_ids)
        if coverage and (allowed_accounts is None or request_period is not None):
            raise ValueError
    except (ValueError, TypeError, RecursionError):
        missing.append("approved_report_scope_invalid")
    if missing:
        print(json.dumps(_empty_report(status="incomplete", missing=missing), sort_keys=True))
        return 2
    if preflight:
        print(json.dumps(_empty_report(status="configuration_shape_valid", missing=[]), sort_keys=True))
        return 0

    try:
        if coverage:
            result = diagnose_configured_flex_coverage(
                token=token, query_id=query_id, expected_account_ids=account_ids,
                configured_account_ids=dict(zip(CONFIGURED_COVERAGE_SLOTS, allowed_accounts, strict=True)),
            )
            report = _validated_coverage_report(result)
            if expected_period is not None and any(
                row["statement_present"] and row["period"] != expected_period
                for row in report.get("accounts", {}).values()
            ):
                report = _empty_report(status="error", missing=["expected_period_mismatch"])
            print(json.dumps(report, sort_keys=True))
            return 0 if report["status"] == "observed_source_shape" else 1
        kwargs = {"collect_pending": True} if args in (("--read-once",), ("--cash-read-once",), ("--cashflow-read-once",)) else {}
        if args == ("--cash-read-once",):
            kwargs["cash_report_only"] = True
        if args == ("--cashflow-read-once",):
            kwargs["cashflow_source_only"] = True
            if request_period is not None:
                kwargs["report_period"] = request_period
        if execution:
            kwargs.update(execution_source_only=True, report_period=expected_period, collect_pending=True)
        if allowed_accounts is not None:
            kwargs["allowed_report_account_ids"] = allowed_accounts
        result = diagnose_activity_flex_ledger(
            token=token,
            query_id=query_id,
            expected_account_ids=account_ids,
            **kwargs,
        )
        if execution:
            report = _validated_execution_report(result)
        elif args == ("--cashflow-read-once",):
            report = _validated_cashflow_report(result)
        else:
            report = _validated_cash_report(result) if args == ("--cash-read-once",) else _validated_report(result)
        if expected_period is not None and report["period"] != expected_period:
            report = _empty_report(status="error", missing=["expected_period_mismatch"])
        if request_period is not None and report["period"] != request_period:
            report = _empty_report(status="error", missing=["requested_period_mismatch"])
    except FlexReportPending:
        report = _empty_report(status="pending", missing=["flex_generation_pending"])
        print(json.dumps(report, sort_keys=True))
        return 1
    except FlexSourceError as error:
        report = _empty_report(status="error", missing=["flex_import_failed"])
        report.update(_safe_source_failure(error.safe_diagnostic))
    except FlexExecutionsError as error:
        # Only these literal codes belong to this pure diagnostic. No str(error),
        # traceback, class name or native value may enter the public receipt.
        code = error.args[0] if len(error.args) == 1 else None
        allowed = {"execution_source_period_invalid", "execution_source_rows_unsupported",
                   "execution_source_record_limit", "execution_source_account_mismatch",
                   "execution_source_date_invalid", "execution_source_period_mismatch"}
        report = _empty_report(status="error", missing=["flex_import_failed"])
        if type(code) is str and code in allowed:
            report.update(missing=[code], failure_stage="verify_period" if code == "execution_source_period_mismatch"
                          else "execution_shape")
    except Exception:
        # Never print exception text: SDK/request exceptions may carry query
        # URLs, account identifiers, amounts or broker return values.
        report = _empty_report(status="error", missing=["flex_import_failed"])
        print(json.dumps(report, sort_keys=True))
        return 1

    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] in {"available", "observed_source_shape"} else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
