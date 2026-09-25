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


def fetch_activity_flex_xml(*, token: str, query_id: str, session: requests.Session | None = None) -> FlexReport:
    """Fetch one XML query once; no retry, broker trade, or local persistence."""

    if not token or not re.fullmatch(r"[0-9]+", query_id):
        raise FlexSourceError("IBKR Flex token and numeric query ID are required")
    client = session or requests.Session()
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
        body = _read_response(client, "GetStatement", {"t": token, "q": reference, "v": "3"})
        report = _xml_root(body, endpoint="GetStatement")
        if report.tag != "FlexQueryResponse":
            raise FlexSourceError("IBKR Flex query must return an XML Activity report")
        return FlexReport(content_sha256=hashlib.sha256(body).hexdigest(), content=body)
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
