"""Constrain the existing daily publisher to one configured account and trading day."""
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
            or env.get("IBKR_ACCOUNT_FACTS_TARGET") not in {"primary-live", "additional-1", "additional-2", "additional-3"}):
        raise ValueError("dispatch_invalid")
    raw = env.get("IBKR_RUNTIME_DAILY_BUSINESS_DATE", "")
    day = date.fromisoformat(raw)
    if day.isoformat() != raw or day > (today or datetime.now(ZoneInfo("America/New_York")).date()):
        raise ValueError("date_invalid")
    if calendars.get_calendar("NYSE").schedule(start_date=raw, end_date=raw).empty:
        raise ValueError("market_closed")
    expected = identity.resolve_expected()
    prefix = PurePosixPath(urlsplit(expected["expected_report_prefix"]).path).parts
    slot = env["IBKR_ACCOUNT_FACTS_TARGET"]
    encoded = env.get("IBKR_RUNTIME_DAILY_TARGET_KEY", "")
    if encoded.startswith("{"):
        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("daily_mapping_invalid")
                result[key] = value
            return result
        mapping = json.loads(encoded, object_pairs_hook=unique_pairs)
        if not isinstance(mapping, dict) or not mapping or set(mapping) - {"primary-live", "additional-1", "additional-2", "additional-3"}:
            raise ValueError("daily_mapping_invalid")
        if any(not isinstance(value, dict) or set(value) != {"target_key", "account_key"}
               or not all(isinstance(v, str) and v and not any(c.isspace() for c in v) for v in value.values())
               for value in mapping.values()):
            raise ValueError("daily_mapping_invalid")
        if len({value["target_key"] for value in mapping.values()}) != len(mapping) or len({value["account_key"] for value in mapping.values()}) != len(mapping):
            raise ValueError("daily_mapping_invalid")
        selected = mapping.get(slot)
        if not selected:
            raise ValueError("daily_mapping_invalid")
        target_key, account_key = selected["target_key"], selected["account_key"]
    elif slot == "primary-live":
        target_key, account_key = encoded, env.get("IBKR_RUNTIME_DAILY_ACCOUNT_KEY", "")
    else:
        raise ValueError("daily_mapping_invalid")
    target = target_key.split("|")
    if len(target) != 4 or (slot == "primary-live" and ("tqqq_growth_income" not in prefix or target[1] != "tqqq_growth_income")):
        raise ValueError("daily_target_invalid")
    return target_key, account_key


def main():
    try:
        target_key, account_key = preflight(os.environ)
    except Exception:
        print(json.dumps({"status": "blocked", "reason": "primary_daily_preflight_failed"}))
        return 1
    return publisher.main(target_key=target_key, account_key=account_key)


if __name__ == "__main__":
    raise SystemExit(main())
