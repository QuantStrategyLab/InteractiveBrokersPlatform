"""Publish notification candidates from existing archives; disabled by default.

Runs only inside an existing cloud monitor, with its existing identity. It never
invokes a platform service or broker, reads secrets, or enables a runtime target.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from quant_platform_kit.notifications.digest_candidates import project_reports

_SAFE = re.compile(r"[a-z0-9][a-z0-9_-]{0,40}\Z")
_SAFE_REASON = re.compile(r"candidate_[a-z0-9_]{0,60}\Z")


def _read(args):
    result = subprocess.run(["gcloud", "storage", *args], capture_output=True, text=True, check=False)
    if result.returncode:
        raise ValueError("candidate_archive_read_failed")
    return json.loads(result.stdout)


def emit(env, *, now=None, read=_read, upload=None):
    from notifications.unified_adapter import (
        _archived_account_snapshot, effective_mode, enabled as unified_enabled,
        is_probe_report, persist_archived_reports,
    )
    if effective_mode(env) != "daily_digest":
        return {"status": "disabled", "uploaded": False}
    bindings = env.get("DIGEST_TARGET_BINDINGS_JSON", "").strip()
    if bindings:
        values = json.loads(bindings)
        if not isinstance(values, list) or not values or len(values) > 100 or not all(isinstance(v, dict) for v in values):
            raise ValueError("candidate_bindings_invalid")
        results = []
        for value in values:
            results.append(emit({**env, **value, "DIGEST_TARGET_BINDINGS_JSON": ""}, now=now, read=read, upload=upload))
        return {"status": "projected", "uploaded": any(v["uploaded"] for v in results), "targets": len(results)}
    if env.get("RUNTIME_TARGET_ENABLED") == "false":
        return {"status": "target_disabled", "uploaded": False}
    if env.get("RUNTIME_TARGET_ENABLED") != "true":
        raise ValueError("candidate_target_enablement_unknown")
    target = json.loads(env.get("RUNTIME_TARGET_JSON", ""))
    platform = target.get("platform_id", "")
    slot = env.get("DIGEST_STORE_TARGET", "")
    if not _SAFE.fullmatch(platform) or not _SAFE.fullmatch(slot):
        raise ValueError("candidate_store_identity_missing")
    uid, tid = env.get("DIGEST_OPAQUE_ACCOUNT_UID", ""), env.get("DIGEST_TARGET_ID", "")
    if not uid or not tid:
        raise ValueError("candidate_identity_missing")
    prefix = env.get("EXECUTION_REPORT_GCS_URI", "").rstrip("/")
    destination = env.get("DIGEST_CANDIDATES_GCS_URI", "").rstrip("/")
    if any(not re.fullmatch(r"gs://[a-z0-9][a-z0-9._-]+/[^\s?#*]+", value) for value in (prefix, destination)):
        raise ValueError("candidate_store_configuration_missing")
    current = now or datetime.now(timezone.utc)
    if current.astimezone(ZoneInfo("Asia/Singapore")).hour < 6:
        return {"status": "before_cutoff", "uploaded": False}
    day = (current.astimezone(ZoneInfo("Asia/Singapore")) - timedelta(hours=6)).date() - timedelta(days=1)
    # Monitor runs after the fixed 06:00 SGT cut-off. Reject early runs rather
    # than overwriting the previous day's complete candidate with partial facts.
    end = datetime(day.year, day.month, day.day, 6, tzinfo=ZoneInfo("Asia/Singapore")) + timedelta(days=1)
    start = end - timedelta(days=1)
    lookback_start = start - timedelta(days=7)
    months = {start.astimezone(timezone.utc).strftime("%Y-%m"), end.astimezone(timezone.utc).strftime("%Y-%m")}
    source_platform = env.get("RUNTIME_HEARTBEAT_REPORT_PLATFORM") or platform if unified_enabled(env) else platform
    if not _SAFE.fullmatch(source_platform):
        raise ValueError("candidate_report_platform_invalid")
    entries = []
    for month in sorted(months):
        entries.extend(read(["ls", "--json", f"{prefix}/{source_platform}/**/{month}/*.json"]))
    total_listed = len(entries)
    if total_listed > 10000:
        raise ValueError("candidate_archive_budget_exceeded")
    keys = ("platform_id", "strategy_profile", "service_name", "account_scope", "account_selector", "deployment_selector", "dry_run_only")
    reports, probe_reports, lookback_entries, uris = [], [], [], set()
    carryover_snapshot = None

    def _entry_stamp(entry):
        raw_stamp = (entry.get("metadata") or {}).get("updated") or (entry.get("metadata") or {}).get("timeCreated")
        stamp = datetime.fromisoformat(str(raw_stamp).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("candidate_archive_time_missing")
        return stamp

    def _load_matching(uri):
        source = read(["cat", uri])
        source_target = source.get("runtime_target") or {}
        if not isinstance(source_target, dict) or any(source_target.get(key) != target.get(key) for key in keys):
            return None, None
        source_stamp = datetime.fromisoformat(str(source.get("started_at", "")).replace("Z", "+00:00"))
        if source_stamp.tzinfo is None:
            raise ValueError("candidate_source_time_invalid")
        return source, source_stamp

    for entry in entries:
        uri = str(entry.get("url", "")).split("#", 1)[0]
        if uri in uris:
            continue
        if not uri.startswith(f"{prefix}/{source_platform}/"):
            raise ValueError("candidate_source_locator_mismatch")
        uris.add(uri)
        stamp = _entry_stamp(entry)
        if stamp < lookback_start or stamp > current:
            continue
        if stamp < start:
            lookback_entries.append((stamp, uri))
            continue
        source, source_stamp = _load_matching(uri)
        if source is None:
            continue
        if start <= source_stamp < end:
            if not target.get("dry_run_only"):
                snap = _archived_account_snapshot(
                    source, target["account_selector"], source_stamp, current, allow_live_probe=True,
                )
                if snap and datetime.fromisoformat(snap["as_of"].replace("Z", "+00:00")) < end:
                    if carryover_snapshot is None or datetime.fromisoformat(snap["as_of"].replace("Z", "+00:00")) > datetime.fromisoformat(carryover_snapshot["as_of"].replace("Z", "+00:00")):
                        carryover_snapshot = snap
            if is_probe_report(source):
                probe_reports.append(source)
            else:
                reports.append(source)
        elif lookback_start <= source_stamp < start:
            lookback_entries.append((source_stamp, uri))

    schedule_raw = env.get("DIGEST_NOTIFICATION_SCHEDULE_JSON", "").strip()
    if reports and target.get("dry_run_only") is False and schedule_raw:
        from collections.abc import Mapping
        from notifications.unified_adapter import _kind

        def _is_healthy_dry_run(report):
            summary = report.get("summary")
            if not isinstance(summary, Mapping):
                return False
            facts = summary.get("notification_digest")
            receipt = report.get("execution_receipt")
            if isinstance(receipt, Mapping) and receipt.get("outcome") not in {
                None, "no_action", "no_signal", "no_rebalance", "dry_run", "completed",
            }:
                return False
            return (
                report.get("dry_run") is True
                and report.get("status") in {"ok", "completed", "success"}
                and isinstance(facts, Mapping)
                and facts.get("schema_version") == "qsl.notification_cycle.v1"
                and facts.get("dry_run") is True
                and _kind(
                    {**summary, "errors": (report.get("error_summary") or {}).get("errors") or report.get("errors")},
                    dry_run=True,
                ) == "routine_cycle"
            )

        if all(_is_healthy_dry_run(r) for r in reports):
            from quant_platform_kit.common.runtime_heartbeat_policy import filter_due_targets
            due, evaluated = filter_due_targets(
                [json.loads(schedule_raw)], since=start, now=end - timedelta(microseconds=1),
                publication_grace=timedelta(0), warning_logger=lambda message: None,
            )
            if evaluated and not due:
                reports = []

    if not reports:
        if not schedule_raw:
            raise ValueError("candidate_source_absent")
        from quant_platform_kit.common.runtime_heartbeat_policy import filter_due_targets
        schedule = json.loads(schedule_raw)
        due, evaluated = filter_due_targets([schedule], since=start, now=end - timedelta(microseconds=1),
            publication_grace=timedelta(0), warning_logger=lambda message: None)
        if not evaluated or due:
            raise ValueError("candidate_due_source_absent")
        if carryover_snapshot is None and target.get("dry_run_only") is False:
            def _scan_carryover(candidates):
                for _stamp, item_uri in sorted(candidates, key=lambda item: item[0], reverse=True):
                    source, source_stamp = _load_matching(item_uri)
                    if source is None or not (lookback_start <= source_stamp < start):
                        continue
                    snap = _archived_account_snapshot(
                        source, target["account_selector"], source_stamp, current, allow_live_probe=True,
                    )
                    if snap is not None and datetime.fromisoformat(snap["as_of"].replace("Z", "+00:00")) < end:
                        return snap
                return None

            carryover_snapshot = _scan_carryover(lookback_entries)
            if carryover_snapshot is None:
                extra_months = {(lookback_start + timedelta(days=offset)).astimezone(timezone.utc).strftime("%Y-%m")
                                for offset in (0, 4, 7)} - months
                extra_candidates = []
                for month in sorted(extra_months):
                    month_entries = read(["ls", "--json", f"{prefix}/{source_platform}/**/{month}/*.json"])
                    total_listed += len(month_entries)
                    if total_listed > 10000:
                        raise ValueError("candidate_archive_budget_exceeded")
                    for entry in month_entries:
                        uri = str(entry.get("url", "")).split("#", 1)[0]
                        if uri in uris:
                            continue
                        if not uri.startswith(f"{prefix}/{source_platform}/"):
                            raise ValueError("candidate_source_locator_mismatch")
                        uris.add(uri)
                        stamp = _entry_stamp(entry)
                        if lookback_start <= stamp < start:
                            extra_candidates.append((stamp, uri))
                carryover_snapshot = _scan_carryover(extra_candidates)

        idle_row = {
            "platform_id": platform, "strategy_profile": target["strategy_profile"],
            "opaque_account_uid": uid, "target_id": tid, "business_day": day.isoformat(),
            "actually_ran": False, "status": "ok", "scheduled_idle": True,
            "execution_status": "not_run", "reason_code": "not_due",
            "evidence_provenance": "candidates",
        }
        if carryover_snapshot is not None:
            idle_row["account_daily_facts"] = {"snapshots": [carryover_snapshot]}
        payload = {"schema_version": "qsl.digest_candidates.v1", "runs": [idle_row]}
    else:
        if unified_enabled(env):
            unique = {}
            for report in [*reports, *probe_reports]:
                run_id = report.get("run_id")
                if run_id in unique and unique[run_id] != report:
                    raise ValueError("candidate_source_run_conflict")
                unique[run_id] = report
            return persist_archived_reports(list(unique.values()), env, now=current)
        payload = project_reports(reports, expected_target=target, opaque_account_uid=uid, target_id=tid, now=current)
    if upload is None:
        from google.cloud import storage
        def upload(uri, body):
            storage.Blob.from_string(uri, client=storage.Client()).upload_from_string(
                body, content_type="application/json", retry=None, timeout=30)
    # Never retry an unknown write result. Public output contains no identities.
    upload(f"{destination}/{platform}/{day.isoformat()}/{slot}.json", json.dumps(payload, allow_nan=False))
    return {"status": "projected", "uploaded": True, "runs": len(reports)}


def main():
    try:
        print(json.dumps(emit(os.environ), sort_keys=True))
        return 0
    except Exception as error:
        # Surface our own fixed reason tokens; anything else stays generic so a
        # private exception message can never reach the monitor log.
        reason = str(error) if isinstance(error, ValueError) else ""
        if not _SAFE_REASON.fullmatch(reason):
            reason = "candidate_projection_failed"
        print(json.dumps({"status": "blocked", "uploaded": False, "reason": reason}))
        return 1
