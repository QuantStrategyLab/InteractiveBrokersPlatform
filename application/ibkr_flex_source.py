"""Read an IBKR Activity Flex report without granting recovery authority.

Callers must supply a token from an approved secret store and a query ID from
the same IBKR username. Raw report bytes stay in memory and must be handled by
a private, account-scoped verifier; this module never publishes or logs them.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import date
from xml.etree import ElementTree

import requests


_BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_USER_AGENT = "InteractiveBrokersPlatform/1.0"


class FlexSourceError(ValueError):
    """A broker statement could not be safely obtained or identified."""

    def __init__(self, message, *, endpoint=None, category=None, code=None, http_status=None):
        super().__init__(message)
        self.safe_diagnostic = {}
        if endpoint in {"SendRequest", "GetStatement"}:
            self.safe_diagnostic["source_endpoint"] = endpoint
        if category in {"http", "transport", "xml", "provider", "size", "scope", "format"}:
            self.safe_diagnostic["source_failure_kind"] = category
        if isinstance(code, str) and re.fullmatch(r"1[0-9]{3}", code):
            self.safe_diagnostic["provider_error_code"] = code
        if type(http_status) is int and 100 <= http_status <= 599:
            self.safe_diagnostic["http_status"] = http_status


@dataclass(frozen=True)
class FlexReport:
    content_sha256: str
    content: bytes = field(repr=False)


@dataclass(frozen=True)
class FlexReportRequest:
    """Private in-memory generation handle; never serialize or log its fields."""

    reference_code: str = field(repr=False)
    token: str = field(repr=False)


class FlexReportPending(FlexSourceError):
    """Generation is pending; an authorized caller may collect this same handle."""

    def __init__(self, request: FlexReportRequest):
        super().__init__("IBKR Flex report generation pending")
        self.request = request


def _read_response(session: requests.Session, endpoint: str, params: dict[str, str]) -> bytes:
    try:
        with session.get(
            f"{_BASE}/{endpoint}",
            params=params,
            headers={"User-Agent": _USER_AGENT},
            timeout=20,
            allow_redirects=False,
            stream=True,
        ) as response:
            if response.status_code != 200:
                raise FlexSourceError(f"IBKR Flex {endpoint} returned HTTP {response.status_code}",
                                      endpoint=endpoint, category="http", http_status=response.status_code)
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                size += len(chunk)
                if size > _MAX_RESPONSE_BYTES:
                    raise FlexSourceError("IBKR Flex response exceeds the private verifier limit", endpoint=endpoint, category="size")
                chunks.append(chunk)
    except requests.RequestException:
        # requests exceptions can contain the query URL, including the token.
        raise FlexSourceError(f"IBKR Flex {endpoint} request failed", endpoint=endpoint, category="transport") from None
    return b"".join(chunks)


def _xml_root(body: bytes, *, endpoint: str) -> ElementTree.Element:
    if not body or b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise FlexSourceError(f"IBKR Flex {endpoint} returned unsupported XML", endpoint=endpoint, category="xml")
    try:
        return ElementTree.fromstring(body)
    except ElementTree.ParseError:
        raise FlexSourceError(f"IBKR Flex {endpoint} returned invalid XML", endpoint=endpoint, category="xml") from None


def request_activity_flex_report(
    *, token: str, query_id: str, session: requests.Session | None = None,
    from_date: str | None = None, to_date: str | None = None,
) -> FlexReportRequest:
    """SendRequest exactly once and retain its private reference in memory."""

    if not token or not re.fullmatch(r"[0-9]+", query_id):
        raise FlexSourceError("IBKR Flex token and numeric query ID are required")
    params = {"t": token, "q": query_id, "v": "3"}
    if from_date is not None or to_date is not None:
        try:
            start_date, end_date = date.fromisoformat(from_date), date.fromisoformat(to_date)
            if start_date.isoformat() != from_date or end_date.isoformat() != to_date or not 0 <= (end_date - start_date).days <= 365:
                raise ValueError
        except (TypeError, ValueError):
            raise FlexSourceError("IBKR Flex date range is invalid") from None
        params.update(fd=start_date.strftime("%Y%m%d"), td=end_date.strftime("%Y%m%d"))
    client = session or requests.Session()
    if session is None:
        client.trust_env = False
    try:
        send = _xml_root(
            _read_response(client, "SendRequest", params),
            endpoint="SendRequest",
        )
        if send.tag != "FlexStatementResponse" or send.findtext("Status") != "Success":
            raise FlexSourceError("IBKR Flex report generation was rejected", endpoint="SendRequest",
                                  category="provider", code=send.findtext("ErrorCode"))
        reference = send.findtext("ReferenceCode") or ""
        if not re.fullmatch(r"[0-9]+", reference):
            raise FlexSourceError("IBKR Flex returned no valid reference code")
        return FlexReportRequest(reference_code=reference, token=token)
    finally:
        if session is None:
            client.close()


def collect_activity_flex_xml(
    request: FlexReportRequest, *, session: requests.Session | None = None,
) -> FlexReport:
    """GetStatement once for this handle; never generate, poll or replace it."""
    if (
        not isinstance(request, FlexReportRequest) or not request.token
        or not isinstance(request.reference_code, str)
        or not re.fullmatch(r"[0-9]+", request.reference_code)
    ):
        raise FlexSourceError("IBKR Flex generation handle is required")
    client = session or requests.Session()
    if session is None:
        client.trust_env = False
    try:
        body = _read_response(client, "GetStatement", {"t": request.token, "q": request.reference_code, "v": "3"})
        report = _xml_root(body, endpoint="GetStatement")
        if report.tag == "FlexStatementResponse":
            if report.findtext("ErrorCode") == "1019":
                raise FlexReportPending(request)
            raise FlexSourceError("IBKR Flex report retrieval was rejected", endpoint="GetStatement",
                                  category="provider", code=report.findtext("ErrorCode"))
        if report.tag != "FlexQueryResponse":
            raise FlexSourceError("IBKR Flex query must return an XML Activity report", endpoint="GetStatement", category="xml")
        return FlexReport(content_sha256=hashlib.sha256(body).hexdigest(), content=body)
    finally:
        if session is None:
            client.close()


def fetch_activity_flex_xml(*, token: str, query_id: str, session: requests.Session | None = None) -> FlexReport:
    """Generate then collect once; pending retains the same handle, without retry."""
    client = session or requests.Session()
    if session is None:
        client.trust_env = False
    try:
        request = request_activity_flex_report(token=token, query_id=query_id, session=client)
        return collect_activity_flex_xml(request, session=client)
    finally:
        if session is None:
            client.close()


def verify_report_accounts(report: FlexReport, *, expected_account_ids: Collection[str],
                           allowed_report_account_ids: Collection[str] | None = None) -> None:
    """Verify report scope and intended targets; extra accounts need explicit approval."""

    expected = {account.strip() for account in expected_account_ids if account.strip()}
    if not expected or len(expected) != len(expected_account_ids):
        raise FlexSourceError("IBKR Flex expected account scope is incomplete")
    root = _xml_root(report.content, endpoint="GetStatement")
    if root.tag != "FlexQueryResponse":
        raise FlexSourceError("IBKR Flex report is not an XML Activity report")
    statements = root.findall("./FlexStatements/FlexStatement")
    actual = [statement.get("accountId", "").strip() for statement in statements]
    allowed = expected if allowed_report_account_ids is None else {a.strip() for a in allowed_report_account_ids if a.strip()}
    valid_allowed = bool(allowed) and expected <= allowed and (
        allowed_report_account_ids is None or len(allowed) == len(allowed_report_account_ids))
    if (not valid_allowed or not actual or any(not account for account in actual)
            or not expected <= set(actual) or not set(actual) <= allowed or len(actual) != len(set(actual))):
        error = FlexSourceError("IBKR Flex report account scope does not match the target", endpoint="GetStatement", category="scope")
        targets = [s for s in statements if s.get("accountId", "").strip() in expected]
        error.safe_diagnostic.update(reported_account_count=len(actual), target_accounts_present=expected <= set(actual),
                                     reported_unique_account_count=len(set(actual)), target_statement_count=len(targets),
                                     target_period_count=len({(s.get("fromDate"), s.get("toDate")) for s in targets}),
                                     unknown_account_count=len(set(actual) - allowed))
        raise error


def import_activity_flex_ledger(
    *,
    token: str,
    query_id: str,
    expected_account_ids: Collection[str],
    session: requests.Session | None = None,
    allowed_report_account_ids: Collection[str] | None = None,
) -> dict[str, object]:
    """Fetch one report and parse its account-scoped ledger in memory.

    The full returned ledger contains private financial facts. Callers must
    keep it in memory or use an already approved private cloud store; public
    diagnostics should use ``diagnose_activity_flex_ledger`` instead.
    """

    report = fetch_activity_flex_xml(token=token, query_id=query_id, session=session)
    # Import lazily so the source module remains the lower-level fetch/verify
    # boundary and the pure parser can continue importing FlexReport.
    from application.ibkr_flex_ledger import build_flex_ledger

    return build_flex_ledger(report, expected_account_ids=expected_account_ids,
                            allowed_report_account_ids=allowed_report_account_ids)


def import_activity_flex_ledger_from_request(
    request: FlexReportRequest, *, expected_account_ids: Collection[str],
    session: requests.Session | None = None,
    allowed_report_account_ids: Collection[str] | None = None,
) -> dict[str, object]:
    """Explicitly collect the original handle once and verify its account scope."""
    report = collect_activity_flex_xml(request, session=session)
    from application.ibkr_flex_ledger import build_flex_ledger

    return build_flex_ledger(report, expected_account_ids=expected_account_ids,
                            allowed_report_account_ids=allowed_report_account_ids)


def collect_pending_activity_flex_ledger(
    request: FlexReportRequest, *, expected_account_ids: Collection[str],
    session: requests.Session | None = None,
    allowed_report_account_ids: Collection[str] | None = None,
) -> dict[str, object]:
    """Collect an already-pending handle at most five more times, on 1019 only.

    This never generates a replacement or persists the private handle. A caller
    may enter here only after GetStatement declared this request pending.
    """
    for _ in range(5):
        time.sleep(3)
        try:
            return import_activity_flex_ledger_from_request(request,
                expected_account_ids=expected_account_ids, session=session,
                allowed_report_account_ids=allowed_report_account_ids)
        except FlexReportPending as pending:
            if pending.request != request:
                raise FlexSourceError("IBKR Flex pending handle changed") from None
    raise FlexReportPending(request)


def diagnose_activity_flex_ledger(
    *,
    token: str,
    query_id: str,
    expected_account_ids: Collection[str],
    session: requests.Session | None = None,
    allowed_report_account_ids: Collection[str] | None = None,
    collect_pending: bool = False,
    cash_report_only: bool = False,
    cashflow_source_only: bool = False,
    execution_source_only: bool = False,
    report_period: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Read one target and return fixed status; bounded collection is opt-in."""

    expected = tuple(expected_account_ids)
    allowed = tuple(allowed_report_account_ids) if allowed_report_account_ids is not None else expected
    if (len(expected) != 1 or any(not isinstance(a, str) or not re.fullmatch(r"(?:U|DU)[0-9]+", a)
            for a in (*expected, *allowed)) or not allowed or len(set(allowed)) != len(allowed)
            or not set(expected) <= set(allowed) or type(collect_pending) is not bool
            or type(cash_report_only) is not bool or type(cashflow_source_only) is not bool
            or type(execution_source_only) is not bool
            or sum((cash_report_only, cashflow_source_only, execution_source_only)) > 1):
        raise FlexSourceError("IBKR Flex diagnostic account scope is invalid")
    if report_period is not None:
        try:
            if (not (cashflow_source_only or execution_source_only) or not isinstance(report_period, Mapping)
                    or set(report_period) != {"from", "to"}):
                raise ValueError
            start, end = (date.fromisoformat(report_period[key]) for key in ("from", "to"))
            if (start.isoformat() != report_period["from"] or end.isoformat() != report_period["to"]
                    or not 0 <= (end - start).days <= 30):
                raise ValueError
        except (TypeError, ValueError):
            raise FlexSourceError("IBKR Flex diagnostic period is invalid") from None
    if execution_source_only:
        if report_period is None:
            raise FlexSourceError("IBKR Flex execution diagnostic requires an explicit period")
        request = request_activity_flex_report(token=token, query_id=query_id, session=session,
            from_date=report_period["from"], to_date=report_period["to"])
        # Space every call on this single request; only 1019 permits more reads.
        time.sleep(1)
        for attempt in range(6):
            try:
                report = collect_activity_flex_xml(request, session=session)
                break
            except FlexReportPending as pending:
                if pending.request != request:
                    raise FlexSourceError("IBKR Flex pending handle changed") from None
                if not collect_pending or attempt == 5:
                    raise
                time.sleep(3)
        from application.ibkr_flex_ledger import build_flex_ledger
        from application.ibkr_flex_executions import FlexExecutionsError, diagnose_flex_execution_source

        ledger = build_flex_ledger(report, expected_account_ids=expected,
            allowed_report_account_ids=allowed_report_account_ids)
        if any(ledger["period"].get(key) != report_period[key] for key in ("from", "to")):
            raise FlexExecutionsError("execution_source_period_mismatch")
        root = _xml_root(report.content, endpoint="GetStatement")
        statement = next(s for s in root.findall("./FlexStatements/FlexStatement")
                         if s.get("accountId", "").strip() == expected[0])
        return diagnose_flex_execution_source(statement, source_ref=report.content_sha256, period=report_period)
    try:
        if report_period is None:
            ledger = import_activity_flex_ledger(token=token, query_id=query_id,
                expected_account_ids=expected, session=session,
                allowed_report_account_ids=allowed_report_account_ids)
        else:
            request = request_activity_flex_report(token=token, query_id=query_id, session=session,
                from_date=report_period["from"], to_date=report_period["to"])
            ledger = import_activity_flex_ledger_from_request(request, expected_account_ids=expected,
                session=session, allowed_report_account_ids=allowed_report_account_ids)
    except FlexReportPending as pending:
        if not collect_pending:
            raise
        ledger = collect_pending_activity_flex_ledger(pending.request,
            expected_account_ids=expected, session=session,
            allowed_report_account_ids=allowed_report_account_ids)
    from application.ibkr_flex_ledger import (
        build_flex_cash_diagnostic, build_flex_cashflow_diagnostic, build_flex_ledger_diagnostic,
    )

    if cashflow_source_only:
        return build_flex_cashflow_diagnostic(ledger)
    return build_flex_cash_diagnostic(ledger) if cash_report_only else build_flex_ledger_diagnostic(ledger)


# Fixed native schema only; never derive public field names from the report.
CONFIGURED_COVERAGE_SECTIONS = {
    "AccountInformation": None,
    "ChangeInNAV": None,
    "EquitySummaryInBase": "EquitySummaryByReportDateInBase",
    "CashReport": "CashReportCurrency",
    "CashTransactions": "CashTransaction",
    "CorporateActions": "CorporateAction",
    "StmtFunds": "StatementOfFundsLine",
    "Trades": "Trade",
    "OpenPositions": "OpenPosition",
    "ConversionRates": "ConversionRate",
}
CONFIGURED_COVERAGE_SLOTS = ("primary", "additional-1", "additional-2", "additional-3")


RECOVERY_COMPARABILITY_REASONS = frozenset({
    "statement_missing", "positions_section_missing_or_ambiguous", "positions_unsupported_rows",
    "position_account_or_model_unverified", "position_instrument_missing", "position_quantity_invalid",
    "position_cost_invalid", "position_report_date_mismatch", "position_detail_not_summary",
    "position_asset_unsupported", "position_duplicates_ambiguous", "position_scope_unverified",
    "position_time_alignment_unverified", "position_cost_basis_equivalence_unverified",
    "cash_report_structure_ambiguous", "usd_cash_row_missing", "usd_cash_row_ambiguous",
    "cash_row_account_unverified", "cash_row_model_scope_unverified", "cash_period_mismatch",
    "cash_report_date_mismatch", "endingCash_missing_or_invalid", "endingSettledCash_missing_or_invalid",
    "cash_time_alignment_unverified", "cash_balance_semantics_unverified",
})


def _finite_decimal(value: object) -> bool:
    from application.ibkr_flex_executions import _number_shape

    shape = _number_shape(value)
    if shape["kind"] not in {"decimal", "scientific"} or shape.get("has_whitespace", True):
        return False
    return math.isfinite(float(value))


def _recovery_comparability(statement, ledger, account: str, period: Mapping[str, object]) -> dict[str, object]:
    """Classify only fields and known scope; never emit or compare financial values."""
    if statement is None or ledger is None:
        return {"positions": {"source_fields_valid": False, "reasons": ["statement_missing"]},
                "cash": {"source_fields_valid": False, "reasons": ["statement_missing"]}}
    from application.ibkr_flex_executions import _day

    reasons = set()
    containers = statement.findall("OpenPositions")
    if len(containers) != 1:
        reasons.add("positions_section_missing_or_ambiguous")
    rows = [row for container in containers for row in container.findall("OpenPosition")]
    if not period.get("to"):
        reasons.add("position_report_date_mismatch")
    if any(child.tag != "OpenPosition" or len(child) for container in containers for child in container):
        reasons.add("positions_unsupported_rows")
    seen = set()
    for row in rows:
        if row.get("accountId") != account or row.get("model") != "":
            reasons.add("position_account_or_model_unverified")
        symbol, currency = row.get("symbol", "").strip(), row.get("currency", "").strip()
        if not symbol or not currency:
            reasons.add("position_instrument_missing")
        if not _finite_decimal(row.get("position")):
            reasons.add("position_quantity_invalid")
        if not _finite_decimal(row.get("costBasisPrice")):
            reasons.add("position_cost_invalid")
        if not period.get("to") or _day(row.get("reportDate")) != period.get("to"):
            reasons.add("position_report_date_mismatch")
        if row.get("levelOfDetail", "").upper() != "SUMMARY":
            reasons.add("position_detail_not_summary")
        if row.get("assetCategory", "").upper() != "STK":
            reasons.add("position_asset_unsupported")
        key = (symbol.upper(), currency.upper())
        if key in seen:
            reasons.add("position_duplicates_ambiguous")
        seen.add(key)
    positions_valid = not reasons
    reasons.update({"position_scope_unverified", "position_time_alignment_unverified"})
    if rows:
        reasons.add("position_cost_basis_equivalence_unverified")
    cash = ledger["period_cash_facts"]
    cash_reasons = set(cash["missing"])
    cash_valid = cash["status"] == "available_period_end"
    cash_reasons.update({"cash_time_alignment_unverified", "cash_balance_semantics_unverified"})
    return {"positions": {"source_fields_valid": positions_valid, "reasons": sorted(reasons)},
            "cash": {"source_fields_valid": cash_valid, "reasons": sorted(cash_reasons)}}


def diagnose_configured_flex_coverage(
    *, token: str, query_id: str, expected_account_ids: Collection[str],
    configured_account_ids: Mapping[str, str], session: requests.Session | None = None,
) -> dict[str, object]:
    """Observe configured slots from one report; never project or publish facts."""
    if (not isinstance(configured_account_ids, Mapping)
            or set(configured_account_ids) != set(CONFIGURED_COVERAGE_SLOTS)):
        raise FlexSourceError("IBKR Flex configured account scope is invalid")
    allowed = tuple(configured_account_ids[slot] for slot in CONFIGURED_COVERAGE_SLOTS)
    if (any(not isinstance(account, str) or not re.fullmatch(r"(?:U|DU)[0-9]+", account)
            for account in allowed) or tuple(expected_account_ids) != (allowed[0],)
            or len(set(allowed)) != len(allowed)):
        raise FlexSourceError("IBKR Flex configured account scope is invalid")
    try:
        report = fetch_activity_flex_xml(token=token, query_id=query_id, session=session)
    except FlexReportPending as pending:
        request = pending.request
        # Identical bounded 1019 policy to the existing diagnostic. No replacement.
        for _ in range(5):
            time.sleep(3)
            try:
                report = collect_activity_flex_xml(request, session=session)
                break
            except FlexReportPending as still_pending:
                if still_pending.request != request:
                    raise FlexSourceError("IBKR Flex pending handle changed") from None
        else:
            raise FlexReportPending(request) from None

    verify_report_accounts(report, expected_account_ids=expected_account_ids,
                           allowed_report_account_ids=allowed)
    from application.ibkr_flex_ledger import build_flex_ledger

    root = _xml_root(report.content, endpoint="GetStatement")
    statements = {row.get("accountId", "").strip(): row
                  for row in root.findall("./FlexStatements/FlexStatement")}
    accounts = {}
    for slot in CONFIGURED_COVERAGE_SLOTS:
        account = configured_account_ids[slot]
        statement = statements.get(account)
        period = {"from": None, "to": None}
        ledger = None
        if statement is not None:
            # Reuse Activity-report, unique-account and cross-account row checks.
            ledger = build_flex_ledger(report, expected_account_ids=(account,),
                                      allowed_report_account_ids=allowed)
            native_rows = set(CONFIGURED_COVERAGE_SECTIONS) | {
                name for name in CONFIGURED_COVERAGE_SECTIONS.values() if name is not None}
            if any(row.get("accountId", "").strip() not in {"", account}
                   for row in statement.iter() if row.tag in native_rows):
                raise FlexSourceError("IBKR Flex statement row account scope mismatch")
            period = {key: ledger["period"][key] for key in ("from", "to")}
            if period["from"] and period["to"] and period["from"] > period["to"]:
                period = {"from": None, "to": None}
        sections = {}
        for name, row_name in CONFIGURED_COVERAGE_SECTIONS.items():
            containers = [] if statement is None else statement.findall(name)
            rows = (containers if row_name is None else
                    [row for container in containers for row in container.findall(row_name)])
            sections[name] = {
                "section_count": len(containers),
                "record_count": len(rows) if containers else None,
                "unsupported_record_count": sum(
                    child.tag != row_name for container in containers for child in container),
            }
        accounts[slot] = {
            "statement_present": statement is not None, "period": period,
            "native_twr_field_present": (None if statement is None else
                any("twr" in row.attrib for row in statement.findall("ChangeInNAV"))),
            "sections": sections,
            "recovery_comparability": _recovery_comparability(statement, ledger, account, period),
        }
    return {"schema_version": "ibkr_flex_configured_coverage.v1", "status": "observed_source_shape",
            "source_ref": report.content_sha256, "period": accounts["primary"]["period"],
            "coverage": "not_evaluated", "accounts": accounts}
