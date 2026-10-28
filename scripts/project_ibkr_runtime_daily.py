"""Pure, single-account runtime-report projection for the website daily reader.

The caller supplies already-read reports and a trusted account binding. This
module neither enumerates storage nor publishes; archive coverage is unknown.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from quant_platform_kit.common.execution_receipts import attach_execution_receipt
from scripts import inspect_account_facts_archive as account_facts_archive
from scripts import publish_account_facts_from_report as account_facts_publisher

_ZONE = ZoneInfo("America/New_York")
_IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_OPAQUE = re.compile(r"^[a-f0-9]{32}$")
_STATUSES = frozenset({"ok", "success", "completed", "skipped", "error", "failed", "failure", "blocked"})
_EXECUTION_STATUSES = frozenset({"no_op", "no_action", "no_signal", "no_rebalance", "skipped", "outside_market_hours", "submitted", "pending_reconciliation", "reconciliation_required", "unknown", "unknown_order", "blocked", "risk_blocked", "error", "failed", "failure", "dry_run", "previewed", "partial", "partially_filled", "filled", "completed"})
_ACTIVITIES = {"no_action": "no_submission", "no_signal": "no_signal", "no_rebalance": "no_rebalance", "submitted": "submitted", "filled": "filled", "reconciliation_required": "reconciliation_required", "risk_blocked": "blocked", "failed": "failed"}


def project_ibkr_runtime_daily_from_archive(
    *, expected: Mapping[str, Any], target_key: str, business_date: str, observed_at: datetime,
) -> dict[str, Any]:
    """Read a bounded set from the existing protected report prefix and project it.

    Filename dates are only a search window, not evidence of complete coverage.
    No object path or raw report is included in the returned projection.
    """
    empty = project_ibkr_runtime_daily([], expected=expected, target_key=target_key,
                                       business_date=business_date, observed_at=observed_at)
    try:
        prefix = expected["expected_report_prefix"]
        project = expected["expected_project_id"]
        if not isinstance(prefix, str) or not isinstance(project, str) or not project.strip():
            raise ValueError
        if any(c in prefix for c in "*[]\\\n\r") or any(part in {".", ".."} for part in prefix.split("/")):
            raise ValueError
        account_facts_publisher._source_report_uri(f"{prefix.rstrip('/')}/2000-01/20000101T000000Z.json", prefix)
    except (KeyError, AttributeError, ValueError, account_facts_publisher._ProjectionError):
        empty["read_errors"].append("list_failed")
        return empty
    day = date.fromisoformat(business_date)
    utc_today = observed_at.astimezone(timezone.utc).date()
    try:
        uris = set()
        for utc_day in (day, day + timedelta(days=1)):
            if utc_day <= utc_today:
                uris.update(account_facts_archive.report_uris(day=utc_day, expected=dict(expected), now=observed_at))
    except account_facts_archive.ArchiveError as error:
        empty["read_errors"].append("read_truncated" if str(error) == "archive_listing_limit" else "list_failed")
        return empty
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        empty["read_errors"].append("list_failed")
        return empty
    if len(uris) > 20:
        empty["read_errors"].append("read_truncated")
        return empty
    reports = []
    read_failed = False
    for uri in sorted(uris):
        try:
            account_facts_publisher._source_report_uri(uri, prefix)
            reports.append(account_facts_publisher._load_gcs_report(uri, project_id=project))
        except (account_facts_publisher._ProjectionError, OSError, UnicodeError, subprocess.TimeoutExpired):
            read_failed = True
    projection = project_ibkr_runtime_daily(reports, expected=expected, target_key=target_key,
                                            business_date=business_date, observed_at=observed_at)
    if read_failed:
        projection["read_errors"].append("report_read_error")
    return projection


def project_ibkr_runtime_daily(
    reports: Sequence[Mapping[str, Any]], *, expected: Mapping[str, Any],
    target_key: str, business_date: str, observed_at: datetime,
) -> dict[str, Any]:
    """Project at most 20 reports for one trusted target and one New York date.

    A caller must separately prove source path and collection coverage before any
    publishing. Input data and account selectors never appear in the output.
    """
    if not isinstance(reports, (list, tuple)) or len(reports) > 20:
        raise ValueError("report_limit_or_invalid_input")
    if not isinstance(target_key, str):
        raise ValueError("invalid_target")
    parts = target_key.split("|")
    if len(parts) != 4 or not all(_IDENT.fullmatch(v) for v in parts[:3]) or not _OPAQUE.fullmatch(parts[3]):
        raise ValueError("invalid_target")
    service, profile, scope, _ = parts
    if service != expected.get("expected_service_name") or scope != expected.get("expected_account_scope"):
        raise ValueError("invalid_target")
    try:
        day = date.fromisoformat(business_date)
    except (TypeError, ValueError):
        raise ValueError("invalid_business_date") from None
    if day.isoformat() != business_date or not isinstance(observed_at, datetime) or observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("invalid_observation")
    observed = observed_at.astimezone(timezone.utc)
    if day > observed.astimezone(_ZONE).date():
        raise ValueError("invalid_business_date")
    iso = observed.isoformat().replace("+00:00", "Z")
    runs, excluded, unmatched, conflicts = [], [], [], []
    seen = set()
    for report in reports:
        try:
            if not isinstance(report, Mapping):
                raise account_facts_publisher._ProjectionError("runtime_report_invalid")
            account_facts_publisher._validate_report_account_identity(report, **expected)
            if report.get("strategy_profile") != profile:
                raise account_facts_publisher._ProjectionError("runtime_target_mismatch")
        except (KeyError, TypeError, account_facts_publisher._ProjectionError):
            unmatched.append({"run_id": None, "platform": None, "service": None,
                              "strategy_profile": None, "account_scope": None, "reason": "identity_unverified"})
            continue
        try:
            start = account_facts_publisher._observed_timestamp(report.get("started_at"))
            end = account_facts_publisher._observed_timestamp(report.get("finished_at"))
            if end < start or end > observed:
                raise account_facts_publisher._ProjectionError("observation_invalid")
        except account_facts_publisher._ProjectionError:
            excluded.append({"run_id": None, "reason": "invalid_run_time"})
            continue
        if start.astimezone(_ZONE).date() != day:
            excluded.append({"run_id": None, "reason": "other_business_date"})
            continue
        run_id = report.get("run_id")
        errors = report.get("errors")
        if not isinstance(run_id, str) or not run_id or len(run_id) > 256 or not isinstance(errors, list):
            excluded.append({"run_id": None, "reason": "invalid_report"})
            continue
        opaque = "run." + hashlib.sha256((target_key + "\0" + run_id).encode()).hexdigest()[:32]
        if opaque in seen:
            if opaque not in conflicts:
                conflicts.append(opaque)
            continue
        seen.add(opaque)
        target = report["runtime_target"]
        mode = target.get("execution_mode")
        dry_run = report.get("dry_run")
        lane = "dry_run" if dry_run is True else mode if dry_run is False and mode in ("paper", "live") else "insufficient"
        receipt = None
        try:
            if report.get("execution_receipt") is not None:
                receipt = attach_execution_receipt(dict(report), report["execution_receipt"])["execution_receipt"]
                receipt_time = account_facts_publisher._observed_timestamp(receipt["observed_at"])
                if not start.replace(microsecond=0) <= receipt_time <= end:
                    receipt = None
        except (TypeError, ValueError, account_facts_publisher._ProjectionError):
            pass
        summary = report.get("summary")
        execution = summary.get("execution_status") if isinstance(summary, Mapping) else None
        execution = execution if isinstance(execution, str) and execution in _EXECUTION_STATUSES else None
        status = report.get("status")
        status = status if isinstance(status, str) and status in _STATUSES else None
        if receipt and (errors or status in ("error", "failed", "failure", "blocked")):
            receipt = None
        if receipt and status == "skipped" and receipt["outcome"] in {"submitted", "filled"}:
            receipt = None
        if receipt and execution in {"error", "failed", "failure", "blocked", "risk_blocked"}:
            expected_outcome = "risk_blocked" if execution in {"blocked", "risk_blocked"} else "failed"
            if receipt["outcome"] != expected_outcome:
                receipt = None
        if receipt and execution in {"skipped", "outside_market_hours", "dry_run", "previewed"}:
            if receipt["outcome"] in {"submitted", "filled"}:
                receipt = None
        if receipt and execution in {"no_op", "no_action", "no_signal", "no_rebalance", "submitted", "partially_filled", "filled"}:
            outcome = receipt["outcome"]
            quiet = {"no_op", "no_action", "no_signal", "no_rebalance"}
            if ((outcome == "filled" and execution != "filled")
                or (outcome == "submitted" and execution != "submitted")
                or (outcome in {"no_action", "no_signal", "no_rebalance"} and execution not in quiet)):
                receipt = None
        activity = _ACTIVITIES.get(receipt["outcome"], "unknown") if receipt and lane in ("live", "paper") else "unknown"
        # The receipt is a separate fact; no order count or broker acknowledgement
        # is inferred from it. Missing errors are rejected above, never set false.
        runs.append({"run_id": opaque, "source_object": None, "source_objects": [],
            "started_at": start.isoformat().replace("+00:00", "Z"),
            "finished_at": end.isoformat().replace("+00:00", "Z"), "object_updated_at": None,
            "report_status": status, "execution_lane": lane, "activity": activity, "run_time_known": True,
            "evidence": {"execution_status": execution, "broker_submission_done": None, "action_done": None,
                "orders_pending_count": None, "errors_present": bool(errors),
                "receipt_outcome": receipt["outcome"] if receipt else None,
                "receipt_broker_confirmation": receipt["broker_confirmation"] if receipt else None}})
    lanes = {run["execution_lane"] for run in runs}
    record = {"platform": "ibkr", "target_key": target_key,
        "target": {"service": service, "strategy_profile": profile, "account_scope": scope},
        "business_date": business_date, "timezone": "America/New_York", "observed_at": iso,
        "status": "conflict" if conflicts else "read_incomplete", "kind": "incomplete",
        "completeness": "incomplete", "execution_lane": next(iter(lanes)) if len(lanes) == 1 else "insufficient",
        "schedule": {"state": "unevaluable", "business_date": business_date, "timezone": "America/New_York",
            "latest_due_at": None, "next_due_at": None, "grace_ends_at": None,
            "publication_grace_ended": None, "expected_window": "unspecified", "reason": None},
        "runs": runs, "excluded_reports": excluded, "conflicts": conflicts,
        "fills": {"source": "not_connected", "records": [], "count": None}}
    return {"platform": "ibkr", "observed_at": iso, "completeness": "incomplete",
            "read_errors": ["coverage_unconfirmed"], "records": [record], "unmatched_reports": unmatched}
