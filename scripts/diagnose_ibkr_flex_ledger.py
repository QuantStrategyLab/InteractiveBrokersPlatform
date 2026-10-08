"""Run an opt-in, status-only IBKR Flex diagnostic in an approved cloud job."""

from __future__ import annotations

import json
import os
import re
from datetime import date
from typing import Any

from application.ibkr_flex_source import diagnose_activity_flex_ledger


_SCHEMA_VERSION = "ibkr_flex_diagnostic.v1"
_ENABLE_ENV = "IBKR_FLEX_DIAGNOSTIC_ENABLED"
_TOKEN_ENV = "IBKR_FLEX_TOKEN"
_QUERY_ID_ENV = "IBKR_FLEX_QUERY_ID"
_ACCOUNT_SCOPE_ENV = "IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON"
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
    except (json.JSONDecodeError, TypeError):
        return None
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item or item != item.strip() for item in value)
        or len(value) != len(set(value))
    ):
        return None
    return tuple(value)


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


def main() -> int:
    if os.environ.get(_ENABLE_ENV) != "true":
        print(json.dumps(_empty_report(status="disabled", missing=["flex_import_not_enabled"]), sort_keys=True))
        return 2

    missing: list[str] = []
    token = os.environ.get(_TOKEN_ENV, "")
    query_id = os.environ.get(_QUERY_ID_ENV, "")
    account_ids = _expected_account_ids()
    if not token:
        missing.append("flex_token_missing")
    if not re.fullmatch(r"[0-9]+", query_id):
        missing.append("flex_query_id_missing_or_invalid")
    if account_ids is None:
        missing.append("expected_account_scope_missing_or_invalid")
    if missing:
        print(json.dumps(_empty_report(status="incomplete", missing=missing), sort_keys=True))
        return 2

    try:
        result = diagnose_activity_flex_ledger(
            token=token,
            query_id=query_id,
            expected_account_ids=account_ids,
        )
        report = _validated_report(result)
    except Exception:
        # Never print exception text: SDK/request exceptions may carry query
        # URLs, account identifiers, amounts or broker return values.
        report = _empty_report(status="error", missing=["flex_import_failed"])
        print(json.dumps(report, sort_keys=True))
        return 1

    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "available" else 1


if __name__ == "__main__":
    raise SystemExit(main())
