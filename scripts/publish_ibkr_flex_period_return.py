"""Opt-in, environment-only native interval publisher; fixed safe stdout only."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import requests

from application.ibkr_flex_source import FlexReportPending, import_activity_flex_ledger
from application.ibkr_period_return import (
    PeriodReturnError, build_ibkr_period_return, validate_period_return_scope,
)
from scripts.publish_account_facts_from_report import _NoRedirect


SYNC_URL = "https://qsl-strategy-switch-console.pigbibi.workers.dev/api/account-facts/period-return/sync"
_ACK_LIMIT = 8192
_TOKEN_ALIASES = (
    "ACCOUNT_FACTS_SYNC_TOKEN", "SCHWAB_ACCOUNT_FACTS_SYNC_TOKEN", "FIRSTRADE_ACCOUNT_FACTS_SYNC_TOKEN",
    "BINANCE_ACCOUNT_FACTS_SYNC_TOKEN", "EXECUTION_EVIDENCE_SYNC_TOKEN", "STRATEGY_SWITCH_SYNC_TOKEN",
    "RECONCILIATION_RECOVERY_SYNC_TOKEN", "RECONCILIATION_RECOVERY_CONTROLLER_TOKEN",
    "CYCLE_HEALTH_PROVISIONER_TOKEN",
)


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if (
        not isinstance(value, str) or not value or value != value.strip()
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    ):
        raise PeriodReturnError("configuration_invalid")
    return value


def _config(env: Mapping[str, str]) -> dict[str, Any]:
    config = {key: _required(env, f"IBKR_PERIOD_RETURN_{key.upper()}") for key in (
        "target_id", "source_binding_id", "account_scope", "account_key",
    )}
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", config["account_key"], re.ASCII):
        raise PeriodReturnError("configuration_invalid")
    config["expected_account_ids"] = json.loads(_required(env, "IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON"))
    validate_period_return_scope(**{key: config[key] for key in (
        "target_id", "source_binding_id", "account_scope", "expected_account_ids",
    )})
    config["flex_token"] = _required(env, "IBKR_FLEX_TOKEN")
    config["query_id"] = _required(env, "IBKR_FLEX_QUERY_ID")
    config["sync_token"] = _required(env, "IBKR_ACCOUNT_FACTS_SYNC_TOKEN")
    if (
        not re.fullmatch(r"[0-9]+", config["query_id"], re.ASCII)
        or any(char.isspace() for char in config["sync_token"])
        or config["sync_token"] == config["flex_token"]
        or any(config["sync_token"] == env.get(key) for key in _TOKEN_ALIASES)
    ):
        raise PeriodReturnError("configuration_invalid")
    return config


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_ack_key")
        result[key] = value
    return result


def publish_period_return(payload: Mapping[str, Any], *, sync_token: str, expected_account_key: str) -> str:
    """One POST. A lost/invalid ACK is unknown, never success or an automatic retry."""
    request = Request(SYNC_URL, data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={"Authorization": f"Bearer {sync_token}", "Content-Type": "application/json",
            "User-Agent": "QSL-IBKR-PeriodReturn/1.0"}, method="POST")
    try:
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=15) as response:
            if response.status != 200:
                return "sync_rejected" if 300 <= response.status < 500 else "sync_unknown"
            body = response.read(_ACK_LIMIT + 1)
            if not isinstance(body, bytes) or len(body) > _ACK_LIMIT:
                return "sync_unknown"
            ack = json.loads(body, object_pairs_hook=_unique_keys)
            if (
                not isinstance(ack, dict) or set(ack) != {"ok", "stored", "unchanged", "account_key", "period", "currency", "method"}
                or ack.get("ok") is not True or ack.get("stored") is not True
                or type(ack.get("unchanged")) is not bool or ack.get("account_key") != expected_account_key
                or ack.get("period") != payload["period"] or ack.get("currency") != payload["currency"]
                or ack.get("method") != payload["method"]
            ):
                return "sync_unknown"
            return "unchanged" if ack["unchanged"] else "published"
    except HTTPError as exc:
        return "sync_rejected" if 300 <= exc.code < 500 else "sync_unknown"
    except Exception:
        return "sync_unknown"


def main() -> int:
    if os.environ.get("IBKR_PERIOD_RETURN_PUBLISH_ENABLED") != "true":
        status = "disabled"
    else:
        try:
            config = _config(os.environ)
        except Exception:
            status = "configuration_incomplete"
        else:
            try:
                with requests.Session() as session:
                    session.trust_env = False
                    ledger = import_activity_flex_ledger(token=config["flex_token"], query_id=config["query_id"],
                        expected_account_ids=tuple(config["expected_account_ids"]), session=session)
            except FlexReportPending:
                # The CLI has no approved cross-process private handle store.
                # Stop; rerunning it would generate a different request.
                status = "flex_pending"
            except Exception:
                status = "flex_import_failed"
            else:
                try:
                    payload = build_ibkr_period_return(ledger, observed_at=datetime.now(timezone.utc),
                        **{key: config[key] for key in ("target_id", "source_binding_id", "account_scope", "expected_account_ids")})
                except PeriodReturnError as exc:
                    status = "native_twr_unavailable" if str(exc) == "native_twr_unavailable" else "projection_rejected"
                except Exception:
                    status = "projection_rejected"
                else:
                    status = publish_period_return(payload, sync_token=config["sync_token"], expected_account_key=config["account_key"])
    print(json.dumps({"status": status}, sort_keys=True))
    return 0 if status in {"published", "unchanged"} else 2 if status in {"disabled", "configuration_incomplete"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
