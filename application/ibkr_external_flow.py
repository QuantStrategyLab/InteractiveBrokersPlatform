"""Native date-only deposit/withdrawal facts; never infer intraday timing or TWR."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Any
from xml.etree import ElementTree

from application.ibkr_flex_executions import _day, _number, _text
from application.ibkr_period_return import validate_period_return_scope


class ExternalFlowError(ValueError):
    """Fixed safe error without private values."""


def project_flex_external_flows(statement: ElementTree.Element, *, period: Mapping[str, Any]) -> dict[str, Any]:
    sections = statement.findall("StmtFunds")
    missing = {"status": "source_unavailable", "reason": "funds_section_missing"}
    if not sections:
        return missing
    if len(sections) != 1 or any(row.tag != "StatementOfFundsLine" for row in sections[0]):
        return {**missing, "reason": "unsupported_funds_rows"}
    start, end = _day(period.get("from")), _day(period.get("to"))
    if not start or not end or start > end or (date.fromisoformat(end) - date.fromisoformat(start)).days >= 31:
        return {**missing, "reason": "flow_period_invalid"}
    account = statement.get("accountId", "")
    records = {}
    for row in sections[0]:
        kind = {"DEP": "deposit", "WITH": "withdrawal"}.get(row.get("activityCode"))
        if kind is None:
            continue  # Income, fees, trades, settlement and FX are not external capital.
        native_id, amount, day = row.get("transactionID"), _number(row.get("amount")), _day(row.get("date"))
        currency = row.get("currency", "")
        if (row.get("accountId") != account or row.get("model", "") or not _text(native_id, 256)
                or amount is None or not day or not start <= day <= end
                or len(currency) != 3 or not currency.isascii() or not currency.isupper() or not currency.isalpha()
                or Decimal(amount) == 0 or ((kind == "deposit") != (Decimal(amount) > 0))):
            return {**missing, "reason": "flow_fields_invalid"}
        settlement = row.get("settleDate")
        if settlement and _day(settlement) is None:
            return {**missing, "reason": "flow_settlement_invalid"}
        ref = hashlib.sha256(f"ibkr_flex:StmtFunds:{account}:{native_id}".encode()).hexdigest()
        record = {"currency": currency, "source_ref": ref, "type": kind, "amount": amount,
                  "effective_date": day, "settlement_date": _day(settlement) if settlement else None, "transfer_ref": None}
        if ref in records and records[ref] != record:
            return {**missing, "reason": "flow_identity_conflict"}
        records[ref] = record
        if len(records) > 256:
            return {**missing, "reason": "flow_record_limit"}
    return {"status": "available", "coverage": "reported", "records": list(records.values()),
            "period": {"from": start, "to": end}}


def build_ibkr_external_flow(ledger: Mapping[str, Any], *, target_id: str, source_binding_id: str,
                             account_scope: str, expected_account_ids: Sequence[str]) -> dict[str, Any]:
    validate_period_return_scope(target_id=target_id, source_binding_id=source_binding_id,
                                account_scope=account_scope, expected_account_ids=expected_account_ids)
    if ledger.get("account_ids") != list(expected_account_ids):
        raise ExternalFlowError("flow_account_mismatch")
    facts = ledger.get("external_flow_facts") or {}
    if facts.get("status") != "available":
        raise ExternalFlowError("flow_source_unavailable")
    period = facts["period"]
    # With no reported events, preserve only currencies explicitly in this source.
    # An empty reported window does not certify complete zero-flow coverage.
    currencies = sorted({r["currency"] for r in facts["records"]})
    if not currencies and ledger.get("account_base_currency"):
        currencies = [ledger["account_base_currency"]]
    if not currencies:
        raise ExternalFlowError("flow_currency_missing")
    windows = [{"currency": currency, "date": period["to"], "period_start": period["from"], "period_end": period["to"],
                "time_precision": "date", "coverage": "reported", "source": "ibkr_flex",
                "records": [{k: v for k, v in row.items() if k != "currency"}
                            for row in facts["records"] if row["currency"] == currency]} for currency in currencies]
    return {"schema_version": "account_external_flow.v1", "target_id": target_id, "source_binding_id": source_binding_id,
            "account_scope": account_scope, "account_selector": expected_account_ids[0],
            "externalFlow": {"schema_version": "external_flow.v2", "windows": windows}}
