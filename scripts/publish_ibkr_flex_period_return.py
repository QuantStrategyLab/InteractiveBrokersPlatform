"""Opt-in, environment-only native interval publisher; fixed safe stdout only."""

from __future__ import annotations

import json
import hashlib
import os
import re
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Any
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import requests

from application.ibkr_flex_source import (
    FlexReportPending, FlexSourceError, collect_pending_activity_flex_ledger,
    import_activity_flex_ledger, import_activity_flex_ledger_from_request,
    request_activity_flex_report,
    collect_activity_flex_xml,
)
from application.ibkr_flex_ledger import build_flex_ledger
from application.ibkr_period_return import (
    _ACCOUNT as _PERIOD_ACCOUNT,
    _BINDING as _PERIOD_BINDING,
    _SCOPE as _PERIOD_SCOPE,
    _TARGET as _PERIOD_TARGET,
    PeriodReturnError, build_ibkr_period_return, validate_period_return_scope,
)
from application.ibkr_flex_executions import build_ibkr_executions
from application.ibkr_external_flow import build_ibkr_external_flow
from scripts.publish_account_facts_from_report import (
    _NoRedirect, _additional_target_configs, _bound_source_id, _selector,
)


from scripts.console_config import console_base_url


SYNC_URL = console_base_url() + "/api/account-facts/period-return/sync"
EXECUTIONS_SYNC_URL = console_base_url() + "/api/account-facts/executions/sync"
EXTERNAL_FLOW_SYNC_URL = console_base_url() + "/api/account-facts/external-flow/sync"
_ACK_LIMIT = 8192
_TOKEN_ALIASES = (
    "ACCOUNT_FACTS_SYNC_TOKEN", "SCHWAB_ACCOUNT_FACTS_SYNC_TOKEN", "FIRSTRADE_ACCOUNT_FACTS_SYNC_TOKEN",
    "BINANCE_ACCOUNT_FACTS_SYNC_TOKEN", "EXECUTION_EVIDENCE_SYNC_TOKEN", "STRATEGY_SWITCH_SYNC_TOKEN",
    "RECONCILIATION_RECOVERY_SYNC_TOKEN", "RECONCILIATION_RECOVERY_CONTROLLER_TOKEN",
    "CYCLE_HEALTH_PROVISIONER_TOKEN",
)
_PREFLIGHT_FIELDS = (
    "IBKR_PERIOD_RETURN_TARGET_ID", "IBKR_PERIOD_RETURN_SOURCE_BINDING_ID",
    "IBKR_PERIOD_RETURN_ACCOUNT_SCOPE", "IBKR_PERIOD_RETURN_ACCOUNT_KEY",
    "IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON", "IBKR_FLEX_TOKEN", "IBKR_FLEX_QUERY_ID",
    "IBKR_ACCOUNT_FACTS_SYNC_TOKEN",
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
    historical_date = env.get("IBKR_NATIVE_RETURN_DATE") or None
    if historical_date is not None:
        try:
            parsed = date.fromisoformat(historical_date)
            if parsed.isoformat() != historical_date or not 0 <= (datetime.now(timezone.utc).date() - parsed).days <= 365:
                raise ValueError
        except (TypeError, ValueError):
            raise PeriodReturnError("historical_date_invalid") from None
        config["report_date"] = historical_date
    config["allowed_report_account_ids"] = _configured_report_accounts(env, config["expected_account_ids"])
    config["additional_returns"] = _additional_returns(env, config)
    return config


def _additional_returns(env, primary):
    """Opt in only named protected slots; never infer a receiver key from XML."""
    raw = env.get("IBKR_PERIOD_RETURN_ADDITIONAL_ACCOUNT_KEYS_JSON")
    additional_one = env.get("IBKR_PERIOD_RETURN_ADDITIONAL1_ACCOUNT_KEY")
    if not raw and not additional_one:
        return {}
    try:
        keys = json.loads(raw, object_pairs_hook=_unique_keys) if raw else {}
        if not isinstance(keys, dict):
            raise ValueError
        if additional_one:
            if "additional-1" in keys and keys["additional-1"] != additional_one:
                raise ValueError
            keys["additional-1"] = additional_one
        configs = _additional_target_configs(env.get("IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON"))
        if not isinstance(keys, dict) or not keys or not set(keys) <= set(configs):
            raise ValueError
        if tuple(primary["expected_account_ids"]) != _selector(json.loads(env.get("IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON"))):
            raise ValueError
        seen = {name: {primary[name]} for name in ("account_key", "target_id", "source_binding_id")}
        result = {}
        for slot, key in sorted(keys.items()):
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", key, re.ASCII):
                raise ValueError
            source = configs[slot]
            selected = {"account_key": key, "target_id": source["target_id"],
                "account_scope": source["account_scope"], "expected_account_ids": source["account_selector"],
                "source_binding_id": _bound_source_id(**{name: source[name] for name in (
                    "project_id", "service_name", "runtime_revision", "account_scope", "account_selector", "deployment_selector")}),
                "sync_token": primary["sync_token"]}
            validate_period_return_scope(**{name: selected[name] for name in (
                "target_id", "source_binding_id", "account_scope", "expected_account_ids")})
            if (not primary["allowed_report_account_ids"]
                    or set(selected["expected_account_ids"]) & set(primary["expected_account_ids"])):
                raise ValueError
            for name, values in seen.items():
                if selected[name] in values:
                    raise ValueError
                values.add(selected[name])
            if primary.get("report_date"):
                selected["report_date"] = primary["report_date"]
            result[slot] = selected
        return result
    except Exception:
        raise PeriodReturnError("additional_return_configuration_invalid") from None


def _configured_report_accounts(env, expected):
    raw = env.get("IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON")
    primary = env.get("IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON")
    if not raw and not primary:
        return None  # Standalone callers retain exact single-account scope.
    try:
        primary_ids = _selector(json.loads(primary))
        configs = _additional_target_configs(raw)
        allowed = (*primary_ids, *(c["account_selector"][0] for c in configs.values()))
        if (len(primary_ids) != 1 or len(allowed) != len(configs) + 1 or len(set(allowed)) != len(allowed)
                or any(not _PERIOD_ACCOUNT.fullmatch(a) for a in allowed) or not set(expected) <= set(allowed)):
            raise ValueError
    except Exception:
        raise PeriodReturnError("report_account_scope_invalid") from None
    return allowed


def _preflight_result(env: Mapping[str, str], *, status: str, fields: Mapping[str, str]) -> dict[str, Any]:
    source_sha = env.get("GITHUB_SHA")
    result = {
        "schema_version": "ibkr.flex_configuration_preflight.v1",
        "source_sha": source_sha if isinstance(source_sha, str) and re.fullmatch(r"[a-fA-F0-9]{40}", source_sha) else None,
        "status": status, "fields": dict(fields),
    }
    if status == "configuration_shape_valid":
        result["binding_fingerprints"] = {
            f"{name}_sha256": hashlib.sha256(env[f"IBKR_PERIOD_RETURN_{name.upper()}"].encode()).hexdigest()
            for name in ("target_id", "source_binding_id")
        }
        if env.get("IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON"):
            result["approved_report_account_count"] = len(json.loads(env["IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON"])) + 1
        if env.get("IBKR_PERIOD_RETURN_ADDITIONAL_ACCOUNT_KEYS_JSON") or env.get("IBKR_PERIOD_RETURN_ADDITIONAL1_ACCOUNT_KEY"):
            result["additional_return_fingerprints"] = {slot: {
                f"{name}_sha256": hashlib.sha256(target[name].encode()).hexdigest()
                for name in ("account_key", "target_id", "source_binding_id")}
                for slot, target in _config(env)["additional_returns"].items()}
    return result


def _configuration_preflight(env: Mapping[str, str]) -> dict[str, Any]:
    """Inspect local shape only; the existing _config remains authoritative."""
    fields = {}
    for name in _PREFLIGHT_FIELDS:
        if env.get(name) in (None, ""):
            fields[name] = "missing"
            continue
        try:
            _required(env, name)
        except PeriodReturnError:
            fields[name] = "invalid"
        else:
            fields[name] = "configuration_shape_valid"

    patterns = {
        "IBKR_PERIOD_RETURN_TARGET_ID": _PERIOD_TARGET,
        "IBKR_PERIOD_RETURN_SOURCE_BINDING_ID": _PERIOD_BINDING,
        "IBKR_PERIOD_RETURN_ACCOUNT_SCOPE": _PERIOD_SCOPE,
        "IBKR_PERIOD_RETURN_ACCOUNT_KEY": re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", re.ASCII),
        "IBKR_FLEX_QUERY_ID": re.compile(r"[0-9]+", re.ASCII),
    }
    for name, pattern in patterns.items():
        if fields[name] == "configuration_shape_valid" and not pattern.fullmatch(env[name]):
            fields[name] = "invalid"
    name = "IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON"
    if fields[name] == "configuration_shape_valid":
        try:
            accounts = json.loads(env[name])
            valid = (isinstance(accounts, list) and len(accounts) == 1
                and isinstance(accounts[0], str) and _PERIOD_ACCOUNT.fullmatch(accounts[0]))
        except Exception:
            # Oversized integer/deep JSON failures must not emit a traceback.
            valid = False
        if not valid:
            fields[name] = "invalid"
    name = "IBKR_ACCOUNT_FACTS_SYNC_TOKEN"
    if fields[name] == "configuration_shape_valid" and (
        any(char.isspace() for char in env[name])
        or env[name] == env.get("IBKR_FLEX_TOKEN")
        or any(env[name] == env.get(alias) for alias in _TOKEN_ALIASES)
    ):
        fields[name] = "invalid"
    try:
        _config(env)
    except Exception as exc:
        status = "missing" if "missing" in fields.values() else "invalid"
        if isinstance(exc, PeriodReturnError) and str(exc) == "report_account_scope_invalid":
            fields["approved_report_account_scope"] = "invalid"
    else:
        status = "configuration_shape_valid"
    return _preflight_result(env, status=status, fields=fields)


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_ack_key")
        result[key] = value
    return result


def _rejection_status(code: object, diagnostics: dict[str, Any] | None, field: str) -> str:
    try:
        rejected = 300 <= code < 500
    except TypeError:
        return _unknown(diagnostics, field.replace("_http_status", "_sync_reason"), "http_response")
    if diagnostics is not None:
        if type(code) is int and 100 <= code <= 599:
            diagnostics[field] = code
        if not rejected:
            diagnostics[field.replace("_http_status", "_sync_reason")] = "http_response"
    return "sync_rejected" if rejected else "sync_unknown"


def _unknown(diagnostics, field, reason):
    if diagnostics is not None:
        diagnostics[field] = reason
    return "sync_unknown"


def publish_period_return(payload: Mapping[str, Any], *, sync_token: str, expected_account_key: str, historical: bool = False, diagnostics: dict[str, Any] | None = None) -> str:
    """One POST. A lost/invalid ACK is unknown, never success or an automatic retry."""
    request = Request(SYNC_URL + ("?historical=true" if historical else ""), data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={"Authorization": f"Bearer {sync_token}", "Content-Type": "application/json",
            "User-Agent": "QSL-IBKR-PeriodReturn/1.0"}, method="POST")
    try:
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=15) as response:
            if response.status != 200:
                return _rejection_status(response.status, diagnostics, "period_return_http_status")
            body = response.read(_ACK_LIMIT + 1)
            if not isinstance(body, bytes) or len(body) > _ACK_LIMIT:
                return _unknown(diagnostics, "period_return_sync_reason", "invalid_ack")
            try:
                ack = json.loads(body, object_pairs_hook=_unique_keys)
            except (ValueError, UnicodeError):
                return _unknown(diagnostics, "period_return_sync_reason", "invalid_ack")
            if (
                not isinstance(ack, dict) or set(ack) != {"ok", "stored", "unchanged", "account_key", "period", "currency", "method"}
                or ack.get("ok") is not True or ack.get("stored") is not True
                or type(ack.get("unchanged")) is not bool or ack.get("account_key") != expected_account_key
                or ack.get("period") != payload["period"] or ack.get("currency") != payload["currency"]
                or ack.get("method") != payload["method"]
            ):
                return _unknown(diagnostics, "period_return_sync_reason", "invalid_ack")
            return "unchanged" if ack["unchanged"] else "published"
    except HTTPError as exc:
        return _rejection_status(exc.code, diagnostics, "period_return_http_status")
    except Exception:
        return _unknown(diagnostics, "period_return_sync_reason", "transport_error")


def publish_executions(payload: Mapping[str, Any], *, sync_token: str, expected_account_key: str, diagnostics: dict[str, Any] | None = None) -> str:
    """One independent execution POST; never retry an uncertain remote write."""
    request = Request(EXECUTIONS_SYNC_URL, data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={"Authorization": f"Bearer {sync_token}", "Content-Type": "application/json",
                 "User-Agent": "QSL-IBKR-Executions/1.0"}, method="POST")
    try:
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=15) as response:
            if response.status != 200:
                return _rejection_status(response.status, diagnostics, "executions_http_status")
            body = response.read(_ACK_LIMIT + 1)
            if not isinstance(body, bytes) or len(body) > _ACK_LIMIT:
                return _unknown(diagnostics, "executions_sync_reason", "invalid_ack")
            try:
                ack = json.loads(body, object_pairs_hook=_unique_keys)
            except (ValueError, UnicodeError):
                return _unknown(diagnostics, "executions_sync_reason", "invalid_ack")
            if (
                not isinstance(ack, dict)
                or set(ack) != {"ok", "stored", "unchanged", "account_key", "period", "record_count"}
                or ack.get("ok") is not True or ack.get("stored") is not True
                or type(ack.get("unchanged")) is not bool or ack.get("account_key") != expected_account_key
                or ack.get("period") != payload["period"] or type(ack.get("record_count")) is not int
                or ack.get("record_count") != len(payload["records"])
            ):
                return _unknown(diagnostics, "executions_sync_reason", "invalid_ack")
            return "unchanged" if ack["unchanged"] else "published"
    except HTTPError as exc:
        return _rejection_status(exc.code, diagnostics, "executions_http_status")
    except Exception:
        return _unknown(diagnostics, "executions_sync_reason", "transport_error")


def publish_external_flow(payload, *, sync_token, expected_account_key, diagnostics=None):
    """One independent POST of date-only facts, with bounded, fixed ACK readback."""
    request = Request(EXTERNAL_FLOW_SYNC_URL, data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={"Authorization": f"Bearer {sync_token}", "Content-Type": "application/json",
                 "User-Agent": "QSL-IBKR-ExternalFlow/1.0"}, method="POST")
    expected_count = len({r["source_ref"] for w in payload["externalFlow"]["windows"] for r in w["records"]})
    try:
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=15) as response:
            if response.status != 200:
                return _rejection_status(response.status, diagnostics, "external_flow_http_status")
            body = response.read(_ACK_LIMIT + 1)
            if not isinstance(body, bytes) or len(body) > _ACK_LIMIT:
                return _unknown(diagnostics, "external_flow_sync_reason", "invalid_ack")
            try:
                ack = json.loads(body, object_pairs_hook=_unique_keys)
            except (ValueError, UnicodeError):
                return _unknown(diagnostics, "external_flow_sync_reason", "invalid_ack")
            if (not isinstance(ack, dict) or set(ack) != {"ok", "stored", "unchanged", "account_key", "window_count", "record_count", "readback_verified"}
                    or ack.get("ok") is not True or ack.get("stored") is not True or ack.get("readback_verified") is not True
                    or type(ack.get("unchanged")) is not bool or ack.get("account_key") != expected_account_key
                    or type(ack.get("record_count")) is not int or ack["record_count"] != expected_count
                    or type(ack.get("window_count")) is not int or ack["window_count"] != len(payload["externalFlow"]["windows"])):
                return _unknown(diagnostics, "external_flow_sync_reason", "invalid_ack")
            return "unchanged" if ack["unchanged"] else "published"
    except HTTPError as exc:
        return _rejection_status(exc.code, diagnostics, "external_flow_http_status")
    except Exception:
        return _unknown(diagnostics, "external_flow_sync_reason", "transport_error")


def _external_flow_result(ledger, config, diagnostics=None):
    if (ledger.get("external_flow_facts") or {}).get("status") != "available":
        return {"external_flow_status": "source_unavailable"}
    try:
        payload = build_ibkr_external_flow(ledger, **{key: config[key] for key in (
            "target_id", "source_binding_id", "account_scope", "expected_account_ids")})
        windows = payload["externalFlow"]["windows"]
        return {"external_flow_status": publish_external_flow(payload, sync_token=config["sync_token"], expected_account_key=config["account_key"], diagnostics=diagnostics),
                "external_flow_period": ledger["external_flow_facts"]["period"],
                "external_flow_record_count": len({r["source_ref"] for w in windows for r in w["records"]}),
                "external_flow_currencies": sorted(w["currency"] for w in windows), "external_flow_time_precision": "date",
                "external_flow_coverage": "reported"}
    except Exception:
        return {"external_flow_status": "projection_rejected"}


def _cashflow_only():
    """Use the original protected query/lane; never publish balances, TWR or trades."""
    status = "disabled"
    if os.environ.get("IBKR_PERIOD_RETURN_PUBLISH_ENABLED") != "true":
        print(json.dumps({"external_flow_status": status})); return 2
    try:
        config = _config(os.environ)
        start = os.environ.get("IBKR_FLOW_PERIOD_FROM", "")
        end = os.environ.get("IBKR_FLOW_PERIOD_TO", "")
        if (bool(start) != bool(end) or config.get("report_date")
                or start and (date.fromisoformat(start).isoformat() != start or date.fromisoformat(end).isoformat() != end
                              or not 0 <= (date.fromisoformat(end) - date.fromisoformat(start)).days < 31)):
            raise ValueError
        if start:
            config["report_period"] = {"from": start, "to": end}
    except Exception:
        print(json.dumps({"external_flow_status": "configuration_incomplete"})); return 2
    try:
        with requests.Session() as session:
            session.trust_env = False
            ledger = _collect_configured_ledger(config, session)
        if config.get("report_period") and {k: ledger["period"][k] for k in ("from", "to")} != config["report_period"]:
            result = {"external_flow_status": "historical_period_mismatch"}
        else:
            diagnostics = {}
            result = _external_flow_result(ledger, config, diagnostics)
            result.update(diagnostics)
    except FlexReportPending:
        result = {"external_flow_status": "flex_pending"}
    except Exception:
        result = {"external_flow_status": "flex_import_failed"}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["external_flow_status"] in {"published", "unchanged"} else 1


def _execution_result(ledger, config, observed_at, diagnostics=None):
    """Status/shape only: raw execution values never enter stdout."""
    projection = ledger.get("executions") or {}
    diagnostic = projection.get("diagnostic", {})
    result = {"executions_status": "source_unavailable", "executions_diagnostic": diagnostic}
    if projection.get("status") != "available":
        return result
    try:
        payload = build_ibkr_executions(ledger, observed_at=observed_at,
            **{key: config[key] for key in ("target_id", "source_binding_id", "account_scope", "expected_account_ids")})
        if config.get("report_date") and payload["period"] != {"from": config["report_date"], "to": config["report_date"]}:
            result["executions_status"] = "historical_period_mismatch"
        else:
            result["executions_status"] = publish_executions(payload,
                sync_token=config["sync_token"], expected_account_key=config["account_key"], diagnostics=diagnostics)
    except Exception:
        result["executions_status"] = "projection_rejected"
    return result


def _collect_configured_ledger(config, session):
    """Generate once; only a provider-declared 1019 may re-read that handle."""
    accounts = tuple(config["expected_account_ids"])
    allowed = config.get("allowed_report_account_ids")
    try:
        if config.get("report_period"):
            request = request_activity_flex_report(token=config["flex_token"], query_id=config["query_id"],
                session=session, from_date=config["report_period"]["from"], to_date=config["report_period"]["to"])
            return import_activity_flex_ledger_from_request(request, expected_account_ids=accounts,
                session=session, allowed_report_account_ids=allowed)
        if config.get("report_date"):
            request = request_activity_flex_report(token=config["flex_token"], query_id=config["query_id"],
                session=session, from_date=config["report_date"], to_date=config["report_date"])
            return import_activity_flex_ledger_from_request(request, expected_account_ids=accounts,
                session=session, allowed_report_account_ids=allowed)
        return import_activity_flex_ledger(token=config["flex_token"], query_id=config["query_id"],
                                          expected_account_ids=accounts, session=session, allowed_report_account_ids=allowed)
    except FlexReportPending as pending:
        request = pending.request
    # IBKR report generation is asynchronous. Do not drop the private handle
    # on an ordinary "generating" response or replace it with SendRequest.
    return collect_pending_activity_flex_ledger(request, expected_account_ids=accounts,
        session=session, allowed_report_account_ids=allowed)


def _collect_return_ledgers(config, session):
    """One generation for all selected accounts, with the existing 1019 limit."""
    day = config.get("report_date")
    request = request_activity_flex_report(token=config["flex_token"], query_id=config["query_id"],
        session=session, **({"from_date": day, "to_date": day} if day else {}))
    for attempt in range(6):
        try:
            report = collect_activity_flex_xml(request, session=session)
            break
        except FlexReportPending as pending:
            if pending.request != request:
                raise FlexSourceError("IBKR Flex pending handle changed") from None
            if attempt == 5:
                raise
            time.sleep(3)
    # Validate every selected ledger before any publication. Private XML stays in memory.
    return {slot: build_flex_ledger(report, expected_account_ids=target["expected_account_ids"],
        allowed_report_account_ids=config["allowed_report_account_ids"])
        for slot, target in {"primary": config, **config["additional_returns"]}.items()}


def _publish_additional_return(ledger, config, observed_at, *, diagnostics=None):
    try:
        payload = build_ibkr_period_return(ledger, observed_at=observed_at,
            **{key: config[key] for key in ("target_id", "source_binding_id", "account_scope", "expected_account_ids")})
    except PeriodReturnError as exc:
        return "native_twr_unavailable" if str(exc) == "native_twr_unavailable" else "projection_rejected"
    except Exception:
        return "projection_rejected"
    if config.get("report_date") and payload["period"] != {"from": config["report_date"], "to": config["report_date"]}:
        return "historical_period_mismatch"
    return publish_period_return(payload, sync_token=config["sync_token"],
        expected_account_key=config["account_key"], diagnostics=diagnostics, **({"historical": True} if config.get("report_date") else {}))


def main(argv: Sequence[str] = ()) -> int:
    if tuple(argv) == ("--cashflow-only",):
        return _cashflow_only()
    diagnostics = {}
    if tuple(argv) == ("--preflight",):
        result = _configuration_preflight(os.environ)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] == "configuration_shape_valid" else 2
    additional_only = tuple(argv) == ("--additional-returns-only",)
    if argv and not additional_only:
        print(json.dumps(_preflight_result(os.environ, status="invalid", fields={}), sort_keys=True))
        return 2
    if os.environ.get("IBKR_PERIOD_RETURN_PUBLISH_ENABLED") != "true":
        status = "disabled"
    else:
        try:
            config = _config(os.environ)
            if additional_only and not config["additional_returns"]:
                raise PeriodReturnError("additional_return_configuration_invalid")
        except Exception:
            status = "configuration_incomplete"
        else:
            try:
                with requests.Session() as session:
                    session.trust_env = False
                    if config["additional_returns"]:
                        ledgers = _collect_return_ledgers(config, session)
                        ledger = ledgers["primary"]
                    else:
                        ledger = _collect_configured_ledger(config, session)
            except FlexReportPending:
                # The CLI has no approved cross-process private handle store.
                # Stop; rerunning it would generate a different request.
                status = "flex_pending"
            except FlexSourceError as exc:
                status = "flex_import_failed"
                diagnostics = exc.safe_diagnostic
            except Exception as exc:
                status = "flex_import_failed"
                name = type(exc).__name__
                diagnostics = {"source_failure_kind": "runtime", "exception_type": name if name in {
                    "ImportError", "ModuleNotFoundError", "AttributeError", "KeyError",
                    "TypeError", "ValueError", "RuntimeError",
                } else "other"}
            else:
                observed_at = datetime.now(timezone.utc)
                if additional_only:
                    results = {}
                    details = {}
                    for slot, target in config["additional_returns"].items():
                        detail = {}
                        results[slot] = _publish_additional_return(ledgers[slot], target, observed_at, diagnostics=detail)
                        if detail:
                            details[slot] = detail
                        if results[slot] not in {"published", "unchanged"}:
                            break
                    print(json.dumps({"additional_returns": results, **({"additional_return_sync_diagnostics": details} if details else {})}, sort_keys=True))
                    return 0 if len(results) == len(config["additional_returns"]) and all(
                        value in {"published", "unchanged"} for value in results.values()) else 1
                diagnostics["cashflow_source_diagnostic"] = ledger["cashflow_source_diagnostic"]
                diagnostics["nav_source_diagnostic"] = ledger["nav_source_diagnostic"]
                diagnostics["nav_change_status"] = "unavailable"
                try:
                    payload = build_ibkr_period_return(ledger, observed_at=observed_at,
                        **{key: config[key] for key in ("target_id", "source_binding_id", "account_scope", "expected_account_ids")})
                except PeriodReturnError as exc:
                    status = "native_twr_unavailable" if str(exc) == "native_twr_unavailable" else "projection_rejected"
                except Exception:
                    status = "projection_rejected"
                else:
                    diagnostics["nav_change_status"] = "included" if "nav_change" in payload else "unavailable"
                    if config.get("report_date") and payload["period"] != {"from": config["report_date"], "to": config["report_date"]}:
                        status = "historical_period_mismatch"
                    else:
                        kwargs = {"historical": True} if config.get("report_date") else {}
                        status = publish_period_return(payload, sync_token=config["sync_token"], expected_account_key=config["account_key"], diagnostics=diagnostics, **kwargs)
                diagnostics.update(_execution_result(ledger, config, observed_at, diagnostics))
                diagnostics.update(_external_flow_result(ledger, config, diagnostics))
                failed_primary = status not in {"published", "unchanged"} or any(
                    diagnostics.get(name) in {"sync_unknown", "sync_rejected", "projection_rejected", "historical_period_mismatch"}
                    for name in ("executions_status", "external_flow_status"))
                if config["additional_returns"] and failed_primary:
                    diagnostics["additional_returns_status"] = "skipped_primary_failure"
                elif config["additional_returns"]:
                    results = diagnostics["additional_returns"] = {}
                    for slot, target in config["additional_returns"].items():
                        detail = {}
                        results[slot] = _publish_additional_return(ledgers[slot], target, observed_at, diagnostics=detail)
                        if detail:
                            diagnostics.setdefault("additional_return_sync_diagnostics", {})[slot] = detail
                        if results[slot] not in {"published", "unchanged"}:
                            break
    print(json.dumps({"status": status, **diagnostics}, sort_keys=True))
    if any(value not in {"published", "unchanged"} for value in diagnostics.get("additional_returns", {}).values()):
        return 1
    if diagnostics.get("external_flow_status") in {"sync_unknown", "sync_rejected", "projection_rejected"}:
        return 1
    if diagnostics.get("executions_status") in {"sync_unknown", "sync_rejected", "projection_rejected", "historical_period_mismatch"}:
        return 1
    return 0 if status in {"published", "unchanged"} else 2 if status in {"disabled", "configuration_incomplete"} else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
