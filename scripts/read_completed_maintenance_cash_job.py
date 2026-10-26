"""Read only the already completed, admitted Job; never create or rerun it.

Execution/image/entry/cleanup provenance is the value-free receipt from
https://github.com/QuantStrategyLab/InteractiveBrokersPlatform/actions/runs/40254243027.
These fixed identities deliberately cannot select another account or run.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace

from scripts import observe_maintenance_cash_job as jobs

RUN = "40254243027"
EXECUTION_SHA256 = "d7dd2238680e0c2fb148f0a545ab304c64f01ddedd8fe4245e74394bed5a11c5"
STARTED = datetime(2026, 10, 26, 1, 26, 38, tzinfo=timezone.utc)

# Already completed, saved and cleaned execution; this mode only reads logs.
SUMMARY_RUN = "40260521387"
SUMMARY_EXECUTION_SHA256 = "0c570ff2381f1a91d2be7969120437ff0ee409591fe8d5015c8e9f4c4b8f25e6"
SUMMARY_REPORT_SHA256 = "045710a7bf31c3df0887ea0c417fd08327441883444ae652e95d1feb868eeb78"
SUMMARY_LOG_START = "2026-10-26T02:43:43+00:00"
SUMMARY_LOG_END = "2026-10-26T02:47:10+00:00"


def summary_log_evidence(expected, env, *, run=jobs.pilot.command, load_report=jobs.publisher._load_gcs_report):
    """Inspect fixed historical logs, never reinterpret them as current funds."""
    if (env.get("GITHUB_REF") != "refs/heads/main" or env.get("GITHUB_RUN_ATTEMPT") != "1"
            or env.get("GITHUB_EVENT_NAME") != "workflow_dispatch" or env.get("GITHUB_REPOSITORY") != jobs.pilot.REPOSITORY
            or env.get("MAINTENANCE_COMPLETED_SUMMARY_LOGS") != "true"
            or env.get("MAINTENANCE_SOURCE_COMMIT") or env.get("MAINTENANCE_JOB") == "true"
            or env.get("MAINTENANCE_JOB_READBACK") == "true"):
        raise jobs.pilot.ReleaseError("summary_logs_dispatch_not_admitted")
    job = "ibkr-cash-read-" + SUMMARY_RUN
    query = (f'resource.type="cloud_run_job" AND resource.labels.job_name="{job}" '
        f'AND timestamp>="{SUMMARY_LOG_START}" AND timestamp<="{SUMMARY_LOG_END}" '
        'AND labels."run.googleapis.com/execution_name":*')
    rows = jobs.pilot._json(run(["gcloud", "logging", "read", query,
        "--project=" + expected["expected_project_id"], "--limit=1000", "--order=asc", "--format=json"],
        stage="summary_logs_read_failed"))
    if not isinstance(rows, list) or not rows or len(rows) > 1000:
        raise jobs.pilot.ReleaseError("summary_logs_unavailable")
    records, reports = [], set()
    for row in rows:
        if (not isinstance(row, dict) or row.get("resource", {}).get("type") != "cloud_run_job"
                or row.get("resource", {}).get("labels", {}).get("job_name") != job
                or row.get("resource", {}).get("labels", {}).get("project_id") != expected["expected_project_id"]
                or row.get("resource", {}).get("labels", {}).get("location") != env.get("CLOUD_RUN_REGION")
                or jobs.observer.digest(row.get("labels", {}).get("run.googleapis.com/execution_name", "")) != SUMMARY_EXECUTION_SHA256):
            raise jobs.pilot.ReleaseError("summary_log_execution_unverified")
        stamp = jobs.publisher._observed_timestamp(row.get("timestamp"))
        if not (jobs.publisher._observed_timestamp(SUMMARY_LOG_START) <= stamp
                <= jobs.publisher._observed_timestamp(SUMMARY_LOG_END)):
            raise jobs.pilot.ReleaseError("summary_log_time_unverified")
        message = row.get("textPayload", row.get("jsonPayload", {}).get("message", ""))
        if not isinstance(message, str):
            raise jobs.pilot.ReleaseError("summary_log_shape_unverified")
        records.append((stamp, message))
        match = re.search(r"execution_report (gs://[^\s]+)", message)
        if match:
            uri = match[1]
            if uri.count("/maintenance-cash/") != 1:
                raise jobs.pilot.ReleaseError("summary_report_namespace_unverified")
            jobs.publisher._source_report_uri(uri.replace("/maintenance-cash/", "/", 1), expected["expected_report_prefix"])
            reports.add(uri)
    if len(reports) != 1:
        raise jobs.pilot.ReleaseError("summary_report_not_unique")
    report = load_report(reports.pop(), project_id=expected["expected_project_id"])
    if (jobs.observer.digest(json.dumps(report, sort_keys=True)) != SUMMARY_REPORT_SHA256
            or any(report.get(key) != expected["expected_" + key] for key in ("project_id", "service_name", "account_scope"))
            or tuple(report.get("summary", {}).get("account_facts", {}).get("account_ids", ())) != expected["expected_account_selector"]):
        raise jobs.pilot.ReleaseError("summary_report_identity_unverified")
    summary = report["summary"]["broker_account_summary"]
    first, last = [jobs.publisher._observed_timestamp(summary[key]) for key in ("observed_started_at", "observed_finished_at")]
    if not jobs.publisher._observed_timestamp(SUMMARY_LOG_START) <= first <= last <= jobs.publisher._observed_timestamp(SUMMARY_LOG_END):
        raise jobs.pilot.ReleaseError("summary_report_interval_unverified")
    all_codes, interval_codes, timeouts = [], [], 0
    for stamp, message in records:
        found = re.search(r"\bError ([0-9]{1,8}), reqId -?[0-9]+:", message)
        if found:
            code = int(found[1])
            all_codes.append(code)
            if first <= stamp <= last:
                interval_codes.append(code)
        timeouts += bool(re.search(r"account updates(?: for [^\n]+)? request timed out", message))
    return dict(status="HISTORICAL_LOGS_INSPECTED", source_workflow_run=SUMMARY_RUN,
        execution_sha256=SUMMARY_EXECUTION_SHA256, report_sha256=SUMMARY_REPORT_SHA256,
        scope="FIXED_HISTORICAL_EXECUTION_LOG_ENTRIES_ONLY", log_entry_count=len(rows),
        log_limit_reached=len(rows) == 1000, sdk_error_codes=sorted(set(all_codes)),
        sdk_error_entry_count=len(all_codes), summary_interval_error_codes=sorted(set(interval_codes)),
        account_update_timeout_entry_count=timeouts, exact_summary_request_error_attribution_verified=False,
        historical_summary_end_verified=False, current_funds_verified=False,
        readback_only=True, broker_called=False, job_create_requested=False,
        job_execute_requested=False, configuration_write_requested=False)


def readback(expected, env, *, run=jobs.pilot.command, snapshot=jobs.pilot.snapshot,
             load_report=jobs.publisher._load_gcs_report, now=lambda: datetime.now(timezone.utc)):
    if (env.get("GITHUB_REF") != "refs/heads/main" or env.get("GITHUB_RUN_ATTEMPT") != "1"
            or env.get("GITHUB_EVENT_NAME") != "workflow_dispatch" or env.get("GITHUB_REPOSITORY") != jobs.pilot.REPOSITORY
            or env.get("MAINTENANCE_SOURCE_COMMIT") != jobs.SOURCE or env.get("MAINTENANCE_JOB") == "true"
            or env.get("MAINTENANCE_JOB_READBACK") != "true"):
        raise jobs.pilot.ReleaseError("readback_dispatch_not_admitted")
    ctx = SimpleNamespace(project=expected["expected_project_id"], region=env["CLOUD_RUN_REGION"], service=expected["expected_service_name"])
    def read(args):
        return jobs.pilot._json(run(["gcloud", *args, "--format=json"], stage="readback_source_read_failed"))
    before = snapshot(ctx, run)
    candidate = before["service"]["status"].get("latestCreatedRevisionName")
    if not jobs.pilot.NAME.fullmatch(candidate or ""):
        raise jobs.pilot.ReleaseError("readback_candidate_unverified")
    revision = read(["run", "revisions", "describe", candidate, "--project=" + ctx.project, "--region=" + ctx.region])
    jobs.observer.admit(expected, before, revision, jobs.SOURCE)
    jobs.job_document(revision, "ibkr-cash-read-" + RUN, jobs.ENTRY.read_text())
    namespace = jobs.report_namespace(revision, expected)
    logs = read(["logging", "read", f'resource.type="cloud_run_job" AND resource.labels.job_name="ibkr-cash-read-{RUN}" AND timestamp>="{STARTED.isoformat()}" AND textPayload:"execution_report gs://"',
        "--project=" + ctx.project, "--limit=10"])
    matches = set()
    for row in logs:
        execution = row.get("labels", {}).get("run.googleapis.com/execution_name", "")
        match = re.search(r"execution_report (gs://[^\s]+)", row.get("textPayload", ""))
        if match and jobs.observer.digest(execution) == EXECUTION_SHA256:
            matches.add((execution, jobs.publisher._source_report_uri(match[1], namespace)))
    if len(matches) != 1:
        raise jobs.pilot.ReleaseError("completed_job_report_not_unique")
    execution, uri = matches.pop()
    result = jobs.inspect_report(load_report(uri, project_id=ctx.project), uri, expected,
        job="ibkr-cash-read-" + RUN, execution=execution, started=STARTED, now=now(), namespace=namespace)
    jobs.pilot.unchanged(before, snapshot(ctx, run))
    result.update(readback_only=True, source_workflow_run=RUN, execution_sha256=EXECUTION_SHA256,
        baseline_unchanged=True, runtime_adopted=False, publisher_binding_changed=False, broker_called=False,
        source_commit=jobs.SOURCE, image_digest=jobs.IMAGE_DIGEST)
    return result


def main(argv=None):
    result = dict(status="STOPPED", reason="readback_not_completed", readback_only=True, broker_called=False)
    try:
        args = list(sys.argv[1:] if argv is None else argv)
        if args not in ([], ["--summary-logs"]):
            raise jobs.pilot.ReleaseError("readback_arguments_invalid")
        reader = summary_log_evidence if args else readback
        result = reader(jobs.identity.resolve_expected(), os.environ)
    except Exception as error:
        result["reason"] = str(error) if isinstance(error, jobs.pilot.ReleaseError) else "readback_validation_failed"
        # A fixed exception class is enough to locate a parser/provenance error;
        # its message can contain a private URI and is never forwarded.
        result["error_type"] = type(error).__name__
    path = Path(os.environ["RUNNER_TEMP"]) / "maintenance-cash.safe.json"
    path.write_text(json.dumps(result, indent=2) + "\n")
    path.chmod(0o600)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] in {"OBSERVED_JOB_ONLY", "HISTORICAL_LOGS_INSPECTED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
