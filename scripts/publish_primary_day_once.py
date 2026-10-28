"""Constrain the existing daily publisher to one primary TQQQ trading day."""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import PurePosixPath
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pandas_market_calendars as calendars

from scripts import diagnose_account_facts_identity as identity
from scripts import publish_ibkr_runtime_daily as publisher


def preflight(env, *, today=None):
    if (env.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or env.get("GITHUB_REPOSITORY") != "QuantStrategyLab/InteractiveBrokersPlatform"
            or env.get("GITHUB_REF") != "refs/heads/main" or env.get("GITHUB_RUN_ATTEMPT") != "1"
            or env.get("IBKR_ACCOUNT_FACTS_TARGET") != "primary-live"):
        raise ValueError("dispatch_invalid")
    raw = env.get("IBKR_RUNTIME_DAILY_BUSINESS_DATE", "")
    day = date.fromisoformat(raw)
    if day.isoformat() != raw or day > (today or datetime.now(ZoneInfo("America/New_York")).date()):
        raise ValueError("date_invalid")
    if calendars.get_calendar("NYSE").schedule(start_date=raw, end_date=raw).empty:
        raise ValueError("market_closed")
    expected = identity.resolve_expected()
    prefix = PurePosixPath(urlsplit(expected["expected_report_prefix"]).path).parts
    target = env.get("IBKR_RUNTIME_DAILY_TARGET_KEY", "").split("|")
    if "tqqq_growth_income" not in prefix or len(target) != 4 or target[1] != "tqqq_growth_income":
        raise ValueError("primary_target_invalid")


def main():
    try:
        preflight(os.environ)
    except Exception:
        print(json.dumps({"status": "blocked", "reason": "primary_daily_preflight_failed"}))
        return 1
    return publisher.main()


if __name__ == "__main__":
    raise SystemExit(main())
