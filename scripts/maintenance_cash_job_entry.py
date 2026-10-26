"""Reviewed Python entry text passed to the existing digest-pinned image.

Only existing SDK read helpers and the existing isolated report writer run.
This module has no CLI parameters, strategy cycle, order or notification path.
"""
import os
import re
import asyncio
from datetime import datetime, timezone


def read_account_summary_cash(ib, account_ids, *, timeout_seconds=30,
                              clock=lambda: datetime.now(timezone.utc)):
    """One explicit SDK summary, retaining only the selected account's rows.

    The pinned SDK fills accountValues and acctSummary separately. Listen to
    native callbacks before its cache overwrites duplicate fields, and require
    accountSummaryEnd via the SDK future. All is the API's summary group; rows
    for other managed accounts are neither buffered nor persisted.
    """
    from application.ibkr_cash_observations import capture_cash_observation_rows, build_cash_observations
    if (not isinstance(account_ids, (tuple, list)) or len(account_ids) != 1
            or not re.fullmatch(r"(?:U|DU)[0-9]+", account_ids[0])
            or account_ids[0] not in ib.managedAccounts() or ib.wrapper.acctSummary
            or not 0 < timeout_seconds <= 30):
        raise ValueError("account_summary_source_not_fresh_or_scoped")
    rows = []
    def collect(value):
        if getattr(value, "account", None) == account_ids[0]:
            rows.append(value)
    started = clock().isoformat()
    req_id = ib.client.getReqId()
    future = None
    ib.accountSummaryEvent += collect
    try:
        future = ib.wrapper.startReq(req_id)
        ib.client.reqAccountSummary(req_id, "All", "SettledCash,AvailableFunds,FullAvailableFunds")
        ib._run(asyncio.wait_for(future, timeout=timeout_seconds))
        finished = clock().isoformat()
    finally:
        ib.accountSummaryEvent -= collect
        if future is not None and not future.done():
            future.cancel()
        ib.client.cancelAccountSummary(req_id)
    return dict(source_method="ibkr.reqAccountSummary", account_ids=list(account_ids),
        query_completed=True, snapshot_atomic=False, observed_started_at=started,
        observed_finished_at=finished, broker_cash_observations=build_cash_observations(
            capture_cash_observation_rows(rows, account_ids=tuple(account_ids)), observed_at=finished))


def observe(runtime, build_facts, env, *, summary_reader=None):
    job, execution = env.get("CLOUD_RUN_JOB", ""), env.get("CLOUD_RUN_EXECUTION", "")
    if (not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", job)
            or not re.fullmatch(re.escape(job) + r"-[a-z0-9]+", execution)
            or any(env.get(key) != value for key, value in (
                ("CLOUD_RUN_TASK_INDEX", "0"), ("CLOUD_RUN_TASK_ATTEMPT", "0"), ("CLOUD_RUN_TASK_COUNT", "1")))
            or env.get("K_REVISION") or env.get("K_SERVICE")):
        raise ValueError("job_identity_invalid")
    context = runtime.build_composer(dry_run_only_override=True).build_reporting_adapters().build_log_context()
    report = runtime.build_execution_report(context, dry_run_only_override=True)
    report.setdefault("diagnostics", {}).update(
        maintenance_cash_observation=True, execution_kind="cloud_run_job",
        cloud_run_job=job, cloud_run_execution=execution, task_index=0, task_attempt=0, task_count=1)
    ib, result, stage = None, 1, "connection"
    try:
        ib = runtime.connect_ib(read_only=True, validate_trading_permissions=False)
        stage = "snapshot"
        facts = build_facts(runtime.build_portfolio_snapshot(ib))
        stage = "account_identity"
        if not facts or not runtime._account_facts_match_runtime_target(facts):
            raise ValueError("account_identity_unverified")
        summary = {"account_facts": facts}
        if summary_reader is not None:
            stage = "account_summary"
            summary["broker_account_summary"] = summary_reader(ib, facts["account_ids"])
        runtime.finalize_runtime_report(report, status="ok", summary=summary)
        result = 0
    except Exception:
        # Errors may contain broker endpoints or financial values. Preserve a
        # fixed failure state, never copy the exception into output or report.
        runtime.finalize_runtime_report(report, status="error", diagnostics={
            "cash_observation_failed": True, "cash_observation_failure_stage": stage})
    finally:
        if ib is not None:
            try:
                ib.disconnect()
            except Exception:
                result = 1
        uri = runtime.persist_execution_report(report, dry_run_only_override=True)
        if not isinstance(uri, str) or not uri.startswith("gs://"):
            raise ValueError("private_report_save_unverified")
        print("execution_report " + uri, flush=True)
    return result


if __name__ == "__main__":
    import main
    from application.account_facts import build_ibkr_account_facts
    raise SystemExit(observe(main, build_ibkr_account_facts, os.environ, summary_reader=read_account_summary_cash))
