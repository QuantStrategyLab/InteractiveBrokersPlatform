"""Read an IBKR Activity Flex report without granting recovery authority.

Callers must supply a token from an approved secret store and a query ID from
the same IBKR username. Raw report bytes stay in memory and must be handled by
a private, account-scoped verifier; this module never publishes or logs them.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from xml.etree import ElementTree

import requests


_BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_USER_AGENT = "InteractiveBrokersPlatform/1.0"


class FlexSourceError(ValueError):
    """A broker statement could not be safely obtained or identified."""


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
                raise FlexSourceError(f"IBKR Flex {endpoint} returned HTTP {response.status_code}")
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                size += len(chunk)
                if size > _MAX_RESPONSE_BYTES:
                    raise FlexSourceError("IBKR Flex response exceeds the private verifier limit")
                chunks.append(chunk)
    except requests.RequestException:
        # requests exceptions can contain the query URL, including the token.
        raise FlexSourceError(f"IBKR Flex {endpoint} request failed") from None
    return b"".join(chunks)


def _xml_root(body: bytes, *, endpoint: str) -> ElementTree.Element:
    if not body or b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise FlexSourceError(f"IBKR Flex {endpoint} returned unsupported XML")
    try:
        return ElementTree.fromstring(body)
    except ElementTree.ParseError:
        raise FlexSourceError(f"IBKR Flex {endpoint} returned invalid XML") from None


def request_activity_flex_report(
    *, token: str, query_id: str, session: requests.Session | None = None,
) -> FlexReportRequest:
    """SendRequest exactly once and retain its private reference in memory."""

    if not token or not re.fullmatch(r"[0-9]+", query_id):
        raise FlexSourceError("IBKR Flex token and numeric query ID are required")
    client = session or requests.Session()
    if session is None:
        client.trust_env = False
    try:
        send = _xml_root(
            _read_response(client, "SendRequest", {"t": token, "q": query_id, "v": "3"}),
            endpoint="SendRequest",
        )
        if send.tag != "FlexStatementResponse" or send.findtext("Status") != "Success":
            raise FlexSourceError("IBKR Flex report generation was rejected")
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
            raise FlexSourceError("IBKR Flex report retrieval was rejected")
        if report.tag != "FlexQueryResponse":
            raise FlexSourceError("IBKR Flex query must return an XML Activity report")
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


def verify_report_accounts(report: FlexReport, *, expected_account_ids: Collection[str]) -> None:
    """Reject missing, extra, or unscoped statement accounts before private use."""

    expected = {account.strip() for account in expected_account_ids if account.strip()}
    if not expected or len(expected) != len(expected_account_ids):
        raise FlexSourceError("IBKR Flex expected account scope is incomplete")
    root = _xml_root(report.content, endpoint="GetStatement")
    if root.tag != "FlexQueryResponse":
        raise FlexSourceError("IBKR Flex report is not an XML Activity report")
    statements = root.findall("./FlexStatements/FlexStatement")
    actual = [statement.get("accountId", "").strip() for statement in statements]
    if not actual or any(not account for account in actual) or set(actual) != expected or len(actual) != len(set(actual)):
        raise FlexSourceError("IBKR Flex report account scope does not match the target")


def import_activity_flex_ledger(
    *,
    token: str,
    query_id: str,
    expected_account_ids: Collection[str],
    session: requests.Session | None = None,
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

    return build_flex_ledger(report, expected_account_ids=expected_account_ids)


def import_activity_flex_ledger_from_request(
    request: FlexReportRequest, *, expected_account_ids: Collection[str],
    session: requests.Session | None = None,
) -> dict[str, object]:
    """Explicitly collect the original handle once and verify its account scope."""
    report = collect_activity_flex_xml(request, session=session)
    from application.ibkr_flex_ledger import build_flex_ledger

    return build_flex_ledger(report, expected_account_ids=expected_account_ids)


def diagnose_activity_flex_ledger(
    *,
    token: str,
    query_id: str,
    expected_account_ids: Collection[str],
    session: requests.Session | None = None,
) -> dict[str, object]:
    """Import once and return only the redacted Flex status report."""

    ledger = import_activity_flex_ledger(
        token=token,
        query_id=query_id,
        expected_account_ids=expected_account_ids,
        session=session,
    )
    from application.ibkr_flex_ledger import build_flex_ledger_diagnostic

    return build_flex_ledger_diagnostic(ledger)
