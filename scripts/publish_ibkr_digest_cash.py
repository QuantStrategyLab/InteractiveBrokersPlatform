"""Publish only account-bound IBKR Flex period-end cash for the private digest."""

from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from datetime import date, datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from application.ibkr_flex_ledger import build_flex_period_cash_facts
from application.account_facts_target_config import account_selector, parse_additional_target_configs
from application.ibkr_flex_source import (
    FlexReportPending, FlexSourceError, _xml_root, collect_activity_flex_xml, fetch_activity_flex_xml, verify_report_accounts,
)

_JOB = re.compile(r"projects/[a-z0-9-]+/locations/[a-z0-9-]+/jobs/[a-z0-9-]+")
_SLOT = re.compile(r"[a-z0-9][a-z0-9_-]{0,40}")
_AMOUNT = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")
_SCHEMA = "ibkr_digest_settled_cash.v1"
_SOURCE_VERSION = "flex_source_sha256"


class DigestCashStageError(Exception):
    def __init__(self, stage, cause):
        self.stage = stage
        self.cause = cause
        super().__init__(stage)


@contextmanager
def _stage(name):
    try:
        yield
    except Exception as error:
        raise DigestCashStageError(name, error) from None


def _safe_failure(error):
    from google.api_core.exceptions import GoogleAPICallError
    from requests.exceptions import HTTPError, RequestException

    result = {"settled_cash_status": "failed", "stage": error.stage}
    cause = error.cause
    if isinstance(cause, FlexReportPending):
        result.update(settled_cash_status="flex_pending", source_endpoint="GetStatement", provider_error_code="1019")
    elif isinstance(cause, FlexSourceError):
        # Other Flex callers can attach private account scope evidence.
        allowed = {"source_endpoint", "source_failure_kind", "provider_error_code", "http_status"}
        result.update({key: value for key, value in cause.safe_diagnostic.items() if key in allowed})
        result.setdefault("source_failure_kind", "flex_validation")
    elif isinstance(cause, (GoogleAPICallError, RequestException)):
        result["error_kind"] = "upstream_request_failed"
        if isinstance(cause, HTTPError):
            code = cause.response.status_code if cause.response is not None else None
        else:
            code = getattr(cause, "code", None)
        if isinstance(code, int) and 100 <= code <= 599:
            result["http_status"] = int(code)
    elif isinstance(cause, (KeyError, TypeError, ValueError)):
        result["error_kind"] = "invalid_input"
    else:
        result["error_kind"] = "unexpected_error"
    return result


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
        start, day = period["from"], period["to"]
        if not start or not day or start > day or date.fromisoformat(day) > latest_day:
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


def _archive_cash(bucket, prefix, slot, payload, version):
    from google.api_core.exceptions import PreconditionFailed

    key = f"{prefix}/versions/{slot}/{payload['statement_date']}/{version}.json"
    with _stage("archive_write"):
        try:
            bucket.blob(key).upload_from_string(
                json.dumps(payload, sort_keys=True), content_type="application/json",
                if_generation_match=0, timeout=10, retry=None,
            )
        except PreconditionFailed:
            # A successful earlier run already saved this version.
            pass


def _publish_cash_object(bucket, prefix, slot, payload, source_version):
    from google.api_core.exceptions import NotFound

    with _stage("latest_read"):
        blob = bucket.blob(f"{prefix}/{slot}.json")
        try:
            blob.reload(timeout=10, retry=None)
        except NotFound:
            generation = 0
            current = None
            previous_version = None
        else:
            generation = int(blob.generation)
            current = json.loads(blob.download_as_bytes(if_generation_match=generation, timeout=10, retry=None))
            if (current["target_id"], current["opaque_account_uid"]) != (payload["target_id"], payload["opaque_account_uid"]):
                raise ValueError("digest_cash_stored_binding_mismatch")
            previous_version = (blob.metadata or {}).get(_SOURCE_VERSION)

    _archive_cash(bucket, prefix, slot, payload, source_version)
    if current is not None:
        if current["statement_date"] > payload["statement_date"]:
            return False
        if current == payload and previous_version == source_version:
            return True
        if previous_version is None:
            # Preserve the old financial evidence without assigning it a Flex source it never recorded.
            _archive_cash(bucket, prefix, slot, current, f"legacy-gcs-{generation}")

    with _stage("latest_write"):
        blob.metadata = {**(blob.metadata or {}), _SOURCE_VERSION: source_version}
        blob.upload_from_string(json.dumps(payload, sort_keys=True), content_type="application/json",
                                if_generation_match=generation, timeout=10, retry=None)
    return True


def publish_digest_cash(report, *, expected_primary, allowed_accounts, job_refs, destination):
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    from google.cloud import storage

    if not re.fullmatch(r"[a-f0-9]{64}", report.content_sha256):
        raise ValueError("digest_cash_source_version_invalid")
    destination = destination.rstrip("/")
    uri = urlsplit(destination)
    if uri.scheme != "gs" or not uri.netloc or not uri.path.strip("/") or uri.query or uri.fragment:
        raise ValueError("digest_cash_store_invalid")
    with _stage("account_binding"):
        credentials, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        with AuthorizedSession(credentials) as session:
            bindings = _bindings(job_refs, destination, session)
    with _stage("statement_validation"):
        payloads = project_digest_cash(
            report, expected_primary=expected_primary, allowed_accounts=allowed_accounts, bindings=bindings,
            latest_day=datetime.now(ZoneInfo("America/New_York")).date(),
        )
    with _stage("storage_client"):
        bucket = storage.Client(credentials=credentials, project=project).bucket(uri.netloc)
    prefix = f"{uri.path.strip('/')}/ibkr/flex-cash"
    return sum(_publish_cash_object(bucket, prefix, slot, payload, report.content_sha256)
               for slot, payload in payloads.items())


def main():
    try:
        with _stage("configuration"):
            env = os.environ
            primary = account_selector(json.loads(env["IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON"]))
            if len(primary) != 1 or primary != account_selector(json.loads(env["IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON"])):
                raise ValueError("digest_cash_primary_invalid")
            configs = parse_additional_target_configs(env["IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON"])
            allowed = (*primary, *(value["account_selector"][0] for value in configs.values()))
            if len(set(allowed)) != 4:
                raise ValueError("digest_cash_account_scope_mismatch")
        with _stage("flex_read"):
            try:
                report = fetch_activity_flex_xml(token=env["IBKR_FLEX_TOKEN"], query_id=env["IBKR_FLEX_QUERY_ID"])
            except FlexReportPending as pending:
                handle = pending.request
                for _ in range(5):
                    time.sleep(3)
                    try:
                        report = collect_activity_flex_xml(handle)
                        break
                    except FlexReportPending as error:
                        if error.request != handle:
                            raise
                else:
                    raise FlexReportPending(handle) from None
        count = publish_digest_cash(report, expected_primary=primary, allowed_accounts=allowed,
                                    job_refs=env["IBKR_FLEX_DIGEST_JOB_REFS"],
                                    destination=env["DIGEST_CANDIDATES_GCS_URI"])
        receipt = {"settled_cash_status": "published" if count == 4 else "partial", "settled_cash_accounts": count}
    except DigestCashStageError as error:
        receipt = _safe_failure(error)
    except Exception:
        receipt = {"settled_cash_status": "failed", "stage": "publisher", "error_kind": "unexpected_error"}
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["settled_cash_status"] == "published" else 1


if __name__ == "__main__":
    raise SystemExit(main())
