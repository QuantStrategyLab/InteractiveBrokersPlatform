"""Publish only account-bound IBKR Flex period-end cash for the private digest."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from application.ibkr_flex_ledger import build_flex_period_cash_facts
from application.ibkr_flex_source import _xml_root, verify_report_accounts

_JOB = re.compile(r"projects/[a-z0-9-]+/locations/[a-z0-9-]+/jobs/[a-z0-9-]+")
_SLOT = re.compile(r"[a-z0-9][a-z0-9_-]{0,40}")
_AMOUNT = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")
_SCHEMA = "ibkr_digest_settled_cash.v1"


def _bindings(job_refs, destination, session):
    refs = json.loads(job_refs)
    if (not isinstance(refs, list) or len(refs) != 4
            or any(not isinstance(ref, str) or not _JOB.fullmatch(ref) for ref in refs)
            or len(set(refs)) != 4):
        raise ValueError("digest_cash_jobs_invalid")
    bindings = {}
    for ref in refs:
        response = session.get("https://run.googleapis.com/v2/" + ref, timeout=10, allow_redirects=False)
        response.raise_for_status()
        job = response.json()
        container, = job["template"]["template"]["containers"]
        env = {row["name"]: row.get("value") for row in container["env"]}
        runtime = json.loads(env["RUNTIME_TARGET_JSON"])
        account, = runtime["account_selector"]
        slot = env["DIGEST_STORE_TARGET"]
        uid, target = env["DIGEST_OPAQUE_ACCOUNT_UID"], env["DIGEST_TARGET_ID"]
        if (job["name"] != ref or env["DIGEST_CANDIDATES_GCS_URI"].rstrip("/") != destination
                or runtime["platform_id"] != "ibkr" or runtime["dry_run_only"] is not False
                or not re.fullmatch(r"U[0-9]+", account) or account in bindings or not _SLOT.fullmatch(slot)
                or not all(isinstance(value, str) and 0 < len(value) <= 128 for value in (uid, target))):
            raise ValueError("digest_cash_binding_invalid")
        bindings[account] = (slot, uid, target)
    if any(len(set(values)) != 4 for values in zip(*bindings.values(), strict=True)):
        raise ValueError("digest_cash_binding_duplicate")
    return bindings


def project_digest_cash(report, *, expected_primary, allowed_accounts, bindings, latest_day: date):
    """Project verified Cash Report values without reading other statement sections."""
    if len(bindings) != 4 or set(bindings) != set(allowed_accounts):
        raise ValueError("digest_cash_account_scope_mismatch")
    verify_report_accounts(report, expected_account_ids=expected_primary, allowed_report_account_ids=allowed_accounts)
    root = _xml_root(report.content, endpoint="GetStatement")
    if (root.get("type") or "").strip() not in {"", "AF"}:
        raise ValueError("digest_cash_activity_report_required")
    from application.ibkr_flex_executions import _day

    payloads = {}
    for statement in root.findall("./FlexStatements/FlexStatement"):
        account = statement.get("accountId", "").strip()
        slot, uid, target = bindings[account]
        period = {"from": _day(statement.get("fromDate")), "to": _day(statement.get("toDate"))}
        day = period["to"]
        if day is None or period["from"] != day or date.fromisoformat(day) > latest_day:
            continue
        if any(row.get("accountId", "").strip() not in {"", account}
               for section in statement.findall("CashReport") for row in section.iter()):
            continue
        facts = build_flex_period_cash_facts(statement, source_ref=report.content_sha256, period=period)
        if facts["status"] != "available_period_end":
            continue
        amount = facts["values"]["endingSettledCash"]
        if len(amount) > 64 or not _AMOUNT.fullmatch(amount):
            continue
        payloads[slot] = {"schema_version": _SCHEMA, "target_id": target, "opaque_account_uid": uid,
                          "statement_date": day, "currency": "USD", "amount": amount}
    return payloads


def publish_digest_cash(report, *, expected_primary, allowed_accounts, job_refs, destination):
    import google.auth
    from google.api_core.exceptions import NotFound
    from google.auth.transport.requests import AuthorizedSession
    from google.cloud import storage

    destination = destination.rstrip("/")
    uri = urlsplit(destination)
    if uri.scheme != "gs" or not uri.netloc or not uri.path.strip("/") or uri.query or uri.fragment:
        raise ValueError("digest_cash_store_invalid")
    credentials, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    with AuthorizedSession(credentials) as session:
        bindings = _bindings(job_refs, destination, session)
    payloads = project_digest_cash(report, expected_primary=expected_primary, allowed_accounts=allowed_accounts, bindings=bindings,
                                   latest_day=datetime.now(ZoneInfo("America/New_York")).date())
    bucket = storage.Client(credentials=credentials, project=project).bucket(uri.netloc)
    count = 0
    for slot, payload in payloads.items():
        blob = bucket.blob(f"{uri.path.strip('/')}/ibkr/flex-cash/{slot}.json")
        try:
            blob.reload(timeout=10, retry=None)
        except NotFound:
            generation = 0
        else:
            generation = int(blob.generation)
            current = json.loads(blob.download_as_bytes(if_generation_match=generation, timeout=10, retry=None))
            if (current["target_id"], current["opaque_account_uid"]) != (payload["target_id"], payload["opaque_account_uid"]):
                raise ValueError("digest_cash_stored_binding_mismatch")
            if current["statement_date"] > payload["statement_date"]:
                continue
            if current == payload:
                count += 1
                continue
        blob.upload_from_string(json.dumps(payload, sort_keys=True), content_type="application/json",
                                if_generation_match=generation, timeout=10, retry=None)
        count += 1
    return count
