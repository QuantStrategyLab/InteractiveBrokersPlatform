#!/usr/bin/env python3
"""Summarize selected private IBKR runtime reports in the cloud without exposing orders."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import date
from urllib.parse import urlsplit


_REPORT_STATUSES = frozenset({"ok", "error", "skipped", "pending"})
_EXECUTION_STATUSES = frozenset({
    "executed", "no_op", "dry_run", "blocked", "error", "failed", "failure",
    "pending_reconciliation", "not_started", "executing",
})
_BROKER_OUTCOMES = frozenset({
    "not_due", "no_action", "no_signal", "no_rebalance", "risk_blocked", "submitted", "broker_acknowledged",
    "partially_filled", "filled", "reconciliation_required", "failed",
})
_BROKER_CONFIRMATIONS = frozenset({
    "not_applicable", "not_observed", "acknowledged", "partially_filled", "filled",
    "reconciliation_required",
})


def _safe_status(value: object, allowed: frozenset[str]) -> str | None:
    if value is None:
        return None
    if type(value) is not str or value not in allowed:
        raise ValueError("historical execution status is unclassified")
    return value


def _gcloud(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["gcloud", *args], capture_output=True, check=False)


def _report_prefix(*, service: str, project: str, region: str) -> tuple[str, str, str]:
    result = _gcloud("run", "services", "describe", service, "--project", project, "--region", region, "--format=json")
    if result.returncode:
        raise RuntimeError("Cloud Run service configuration is unavailable")
    payload = json.loads(result.stdout)
    env = {
        item["name"]: item.get("value")
        for container in payload["spec"]["template"]["spec"]["containers"]
        for item in container.get("env", ())
    }
    root = urlsplit(str(env.get("EXECUTION_REPORT_GCS_URI") or ""))
    if root.scheme != "gs" or not root.netloc or root.query or root.fragment:
        raise ValueError("private execution report URI is unavailable")
    profile = str(env.get("STRATEGY_PROFILE") or "")
    account = str(env.get("ACCOUNT_GROUP") or "")
    if not re.fullmatch(r"[a-z0-9_-]+", profile) or not re.fullmatch(r"[a-z0-9_-]+", account):
        raise ValueError("execution report scope is unavailable")
    prefix = f"gs://{root.netloc}/{root.path.strip('/')}/interactive_brokers/{profile}/{account}/"
    return prefix, profile, account


def _report_uris(prefix: str, *, month: str, project: str, limit: int = 256) -> tuple[str, ...]:
    scoped = prefix + month + "/"
    result = _gcloud("storage", "ls", "--recursive", scoped + "**", "--project", project)
    if result.returncode:
        raise RuntimeError("historical execution report listing failed")
    uris = tuple(line.decode("utf-8").strip() for line in result.stdout.splitlines() if line.strip())
    if len(uris) > limit or any(not uri.startswith(scoped) or not uri.endswith(".json") for uri in uris):
        raise ValueError("historical execution report listing is incomplete or invalid")
    return uris


def summarize(*, service: str, project: str, region: str, dates: tuple[str, ...]) -> dict[str, object]:
    selected_dates = {date.fromisoformat(value).isoformat() for value in dates}
    if not selected_dates or len(selected_dates) > 7:
        raise ValueError("one to seven dates are required")
    prefix, profile, account = _report_prefix(service=service, project=project, region=region)
    reports: list[dict[str, object]] = []
    for month in sorted({value[:7] for value in selected_dates}):
        for uri in _report_uris(prefix, month=month, project=project):
            result = _gcloud("storage", "cat", uri)
            if result.returncode:
                raise RuntimeError("historical execution report read failed")
            payload = json.loads(result.stdout)
            if not isinstance(payload, dict) or any((
                payload.get("platform") != "interactive_brokers",
                payload.get("strategy_profile") != profile,
                payload.get("account_scope") != account,
            )):
                raise ValueError("historical execution report identity mismatch")
            started_at = str(payload.get("started_at") or "")
            if started_at[:10] not in selected_dates:
                continue
            summary = payload.get("summary")
            receipt = payload.get("execution_receipt")
            if not isinstance(summary, dict):
                summary = {}
            if not isinstance(receipt, dict):
                receipt = {}
            submitted = summary.get("orders_submitted_count")
            if submitted is not None and (type(submitted) is not int or submitted < 0):
                raise ValueError("historical order count is invalid")
            dry_run = payload.get("dry_run")
            errors = payload.get("errors")
            if type(dry_run) is not bool or not isinstance(errors, list):
                raise ValueError("historical execution report is incomplete")
            reports.append({
                "date": started_at[:10],
                "time_utc": started_at[11:16],
                "dry_run": dry_run,
                "status": _safe_status(payload.get("status"), _REPORT_STATUSES),
                "execution_status": _safe_status(summary.get("execution_status"), _EXECUTION_STATUSES),
                "orders_submitted_count": submitted,
                "broker_outcome": _safe_status(receipt.get("outcome"), _BROKER_OUTCOMES),
                "broker_confirmation": _safe_status(receipt.get("broker_confirmation"), _BROKER_CONFIRMATIONS),
                "error_count": len(errors),
            })
    return {
        "schema_version": "ibkr_historical_execution_summary.v1",
        "bounded_complete": True,
        "selected_dates": sorted(selected_dates),
        "report_count": len(reports),
        "reports": sorted(reports, key=lambda item: (item["date"], item["time_utc"])),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--dates", nargs="+", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(summarize(
            service=args.service, project=args.project, region=args.region, dates=tuple(args.dates)
        ), sort_keys=True))
    except (KeyError, TypeError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"historical execution diagnostic unavailable: {type(exc).__name__}") from None
