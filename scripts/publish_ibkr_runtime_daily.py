"""Publish one protected IBKR daily projection once, with a dedicated identity."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from scripts import diagnose_account_facts_identity as identity
from scripts import project_ibkr_runtime_daily as daily
from scripts import publish_account_facts_from_report as publisher
from scripts.console_config import console_base_url


def publish_once(*, expected, target_key, account_key, business_date, observed_at, token, origin):
    if not all(isinstance(v, str) and v and not any(c.isspace() for c in v)
               for v in (target_key, account_key, token, origin)):
        raise ValueError("daily_config_invalid")
    endpoint = urlsplit(origin)
    if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username is not None
            or endpoint.password is not None or endpoint.path or endpoint.query or endpoint.fragment):
        raise ValueError("daily_config_invalid")
    # Validate target and day before accessing the archive.
    daily.project_ibkr_runtime_daily([], expected=expected, target_key=target_key,
                                     business_date=business_date, observed_at=observed_at)
    source_id = publisher._bound_source_id(**{name: expected["expected_" + name] for name in (
        "project_id", "service_name", "runtime_revision", "account_scope", "account_selector", "deployment_selector")})
    projection = daily.project_ibkr_runtime_daily_from_archive(expected=expected, target_key=target_key,
                                                                business_date=business_date, observed_at=observed_at)
    payload = json.dumps(projection, ensure_ascii=True, separators=(",", ":")).encode()
    request = Request(origin + "/api/runtime-daily/sync", data=payload, method="POST", headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json",
        "X-QSL-Source-Binding-ID": source_id, "User-Agent": publisher.IBKR_ACCOUNT_FACTS_USER_AGENT,
    })
    try:
        with build_opener(publisher._NoRedirect()).open(request, timeout=20) as response:
            raw = response.read(publisher._HTTP_ERROR_BODY_LIMIT + 1)
            if not 200 <= response.status < 300 or len(raw) > publisher._HTTP_ERROR_BODY_LIMIT:
                raise ValueError
            ack = json.loads(raw)
        expected_ack = {"ok": True, "stored": True, "platform": "ibkr", "account_key": account_key,
                        "target_key": target_key, "business_date": business_date, "observed_at": projection["observed_at"]}
        if not isinstance(ack, dict) or any(type(ack.get(k)) is not type(v) or ack[k] != v for k, v in expected_ack.items()):
            raise ValueError
    except Exception:
        return {"status": "unconfirmed", "reason": "daily_publish_outcome_unknown"}
    return {"status": "published", "observed_at": projection["observed_at"], "completeness": "incomplete"}


def verify_receiver_token(*, token, origin):
    """Authenticate against the existing receiver without reaching storage."""
    if not isinstance(token, str) or not token or any(c.isspace() for c in token):
        return False
    endpoint = urlsplit(origin)
    if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username is not None
            or endpoint.password is not None or endpoint.path or endpoint.query or endpoint.fragment):
        return False
    request = Request(origin + "/api/runtime-daily/sync", data=b"{", method="POST", headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json",
        "User-Agent": publisher.IBKR_ACCOUNT_FACTS_USER_AGENT,
    })
    try:
        with build_opener(publisher._NoRedirect()).open(request, timeout=20):
            return False
    except HTTPError as response:
        try:
            body = response.read(1025)
            return response.code == 400 and len(body) <= 1024 and json.loads(body) == {
                "ok": False, "error": "request body must be valid JSON"}
        except (ValueError, OSError):
            return False
    except Exception:
        return False


def main(*, target_key=None, account_key=None):
    try:
        if not os.environ.get("QSL_CONSOLE_BASE_URL"):
            raise ValueError("daily_config_invalid")
        expected = identity.resolve_expected()
        now = datetime.now(timezone.utc)
        result = publish_once(expected=expected, target_key=target_key if target_key is not None else os.environ.get("IBKR_RUNTIME_DAILY_TARGET_KEY", ""),
                              account_key=account_key if account_key is not None else os.environ.get("IBKR_RUNTIME_DAILY_ACCOUNT_KEY", ""),
                              business_date=os.environ.get("IBKR_RUNTIME_DAILY_BUSINESS_DATE", ""), observed_at=now,
                              token=os.environ.get("IBKR_RUNTIME_DAILY_SYNC_TOKEN", ""), origin=console_base_url())
    except Exception:
        result = {"status": "failed", "reason": "daily_projection_or_config_failed"}
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["status"] == "published" else 1


if __name__ == "__main__":
    raise SystemExit(main())
