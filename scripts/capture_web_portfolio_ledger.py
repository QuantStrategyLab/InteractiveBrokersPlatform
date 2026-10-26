"""Capture two allowlisted CPGW GETs into the existing protected report store.

Run only on the approved Linux host after manual read-only authentication.
This collector does not authenticate, establish a brokerage session, publish
account facts, or call the synthetic research consumer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import ssl
import stat
import sys
from datetime import datetime, timezone
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener
from uuid import UUID

from scripts import capture_pilot_release_source as protected
from scripts.deploy_pilot_no_traffic import ReleaseError

MAX_BYTES = 2 * 1024 * 1024
FIELDS = ("acctcode", "currency", "cashbalance", "settledcash", "timestamp")
REQUEST_FIELDS = frozenset({"schema_version", "account_id", "currency", "gcp_project_id",
                            "report_prefix", "capture_id", "binding_source_sha256"})
BASE_URL = "https://localhost:5000/v1/api"


def result():
    return {"schema_version": "ibkr.web_ledger_capture_result.v1", "status": "unavailable",
            "reason": "capture_error", "accounts_read": False, "account_matched": False,
            "ledger_read": False, "ledger_fields_observed": False,
            "object_create_attempted": False, "object_create_completed": False,
            "object_already_present": False, "readback_verified": False,
            "evidence_sha256": None, "complete_cash_obligations_verified": False,
            "spendable_cash_verified": False, "execution_authorized": False,
            "original_brokerage_session_preserved": None}


def _require(condition, reason):
    if not condition:
        raise ReleaseError(reason)


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        _require(key not in value, "invalid_json")
        value[key] = item
    return value


def _invalid_constant(_):
    raise ReleaseError("invalid_json")


def _json(raw):
    _require(isinstance(raw, bytes) and 0 < len(raw) <= MAX_BYTES, "invalid_json")
    try:
        return json.loads(raw, parse_float=Decimal, parse_constant=_invalid_constant, object_pairs_hook=_pairs)
    except (ValueError, UnicodeError, RecursionError):
        raise ReleaseError("invalid_json") from None


def validate_request(request):
    _require(isinstance(request, dict) and set(request) == REQUEST_FIELDS
             and all(isinstance(value, str) for value in request.values()), "request_invalid")
    _require(request["schema_version"] == "ibkr.web_ledger_capture_request.v1"
             and re.fullmatch(r"[A-Za-z0-9]{1,64}", request["account_id"])
             and re.fullmatch(r"[A-Z]{3}", request["currency"]) and request["currency"] != "BASE"
             and re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", request["gcp_project_id"])
             and re.fullmatch(r"[0-9a-f]{64}", request["binding_source_sha256"]), "request_invalid")
    try:
        _require(str(UUID(request["capture_id"])) == request["capture_id"], "request_invalid")
        prefix = urlsplit(request["report_prefix"])
        _require(prefix.scheme == "gs" and re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]", prefix.netloc)
                 and prefix.path.startswith("/") and len(prefix.path) > 1
                 and not prefix.query and not prefix.fragment
                 and not any(part in ("", ".", "..") for part in prefix.path[1:].split("/")), "request_invalid")
    except ValueError:
        raise ReleaseError("request_invalid") from None
    return request["report_prefix"] + "/web-portfolio-ledger/v1/" + request["capture_id"] + ".json"


def read_request(path):
    # No following a symlink or accepting a world/group-readable private binding.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                 and stat.S_IMODE(info.st_mode) == 0o600, "request_permissions_invalid")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            request = _json(stream.read(MAX_BYTES + 1))
    finally:
        os.close(fd)
    validate_request(request)
    return request


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ReleaseError("http_redirect_blocked")


class LocalPortal:
    def __init__(self, ca_file, account_id):
        self.paths = {"/portfolio/accounts", f"/portfolio/{account_id}/ledger"}
        context = ssl.create_default_context(cafile=ca_file)
        self.opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context), _NoRedirect())

    def get(self, path):
        _require(path in self.paths, "endpoint_not_allowed")
        request = Request(BASE_URL + path, headers={"Accept": "application/json"}, method="GET")
        try:
            with self.opener.open(request, timeout=30) as response:
                _require(response.status == 200, "http_read_failed")
                _require(response.headers.get_content_type() == "application/json", "response_not_json")
                body = response.read(MAX_BYTES + 1)
        except HTTPError as error:
            raise ReleaseError("read_only_session_unavailable" if error.code in (401, 403) else "http_read_failed") from None
        except (URLError, OSError, ValueError):
            raise ReleaseError("http_read_failed") from None
        _require(len(body) <= MAX_BYTES, "response_too_large")
        return body


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def capture(request, portal, *, get_store, out, now=_utc_now):
    destination = validate_request(request)
    started = now()
    accounts_raw = portal.get("/portfolio/accounts")
    out["accounts_read"] = True
    accounts = _json(accounts_raw)
    _require(isinstance(accounts, list) and all(isinstance(row, dict) for row in accounts), "accounts_invalid")
    selected = [row for row in accounts if row.get("accountId") == request["account_id"]
                or row.get("id") == request["account_id"]]
    _require(len(selected) == 1 and all(selected[0].get(key, request["account_id"]) == request["account_id"]
                                      for key in ("accountId", "id")), "account_binding_mismatch")
    out["account_matched"] = True
    ledger_raw = portal.get(f"/portfolio/{request['account_id']}/ledger")
    received = now()
    out["ledger_read"] = True
    ledger = _json(ledger_raw)
    _require(isinstance(ledger, dict) and isinstance(ledger.get(request["currency"]), dict), "ledger_currency_missing")
    row = ledger[request["currency"]]
    _require(all(field in row for field in FIELDS), "ledger_fields_missing")
    _require(row["acctcode"] == request["account_id"] and row["currency"] == request["currency"], "ledger_binding_mismatch")
    _require(type(row["timestamp"]) is int and 0 < row["timestamp"] < 10**11, "ledger_timestamp_invalid")
    amounts = {}
    for field in ("cashbalance", "settledcash"):
        _require(type(row[field]) in (int, Decimal) and Decimal(row[field]).is_finite(), "ledger_amount_invalid")
        amounts[field] = str(row[field])  # Preserve precision and negatives; no eligibility decision.
    out["ledger_fields_observed"] = True
    payload = {"schema_version": "ibkr.web_portfolio_ledger_observation.v1", "synthetic": False,
               "account_id": request["account_id"], "currency": request["currency"],
               "binding_source_sha256": request["binding_source_sha256"],
               "capture_id": request["capture_id"], "started_at": started, "received_at": received,
               "endpoint": "/portfolio/{accountId}/ledger",
               "accounts_response_sha256": hashlib.sha256(accounts_raw).hexdigest(),
               "ledger_response_sha256": hashlib.sha256(ledger_raw).hexdigest(),
               "selected_fields": {"acctcode": row["acctcode"], "currency": row["currency"],
                                   "timestamp": row["timestamp"], **amounts},
               "complete_cash_obligations_verified": False, "spendable_cash_verified": False}
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    store = get_store(request["gcp_project_id"])
    protected._save_once(store, destination, body, out)
    out.update(status="observed", reason="ledger_saved_and_readback_verified",
               evidence_sha256=hashlib.sha256(body).hexdigest())
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-file", required=True)
    parser.add_argument("--ca-file", required=True, help="Trusted local Gateway certificate/CA file; TLS verification stays enabled")
    args = parser.parse_args(argv)
    out = result()
    try:
        _require(sys.platform == "linux", "protected_linux_required")
        request = read_request(args.request_file)
        portal = LocalPortal(args.ca_file, request["account_id"])
        capture(request, portal, get_store=protected.object_store, out=out)
    except ReleaseError as error:
        out["reason"] = str(error)
    except Exception:
        out["reason"] = "capture_error"
    print(json.dumps(out, sort_keys=True))
    return 0 if out["readback_verified"] and out["status"] == "observed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
