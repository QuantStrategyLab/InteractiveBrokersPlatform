"""Reviewed Python entry text passed to the existing digest-pinned image.

Only existing SDK read helpers and the existing isolated report writer run.
This module has no CLI parameters, strategy cycle, order or notification path.
"""
import os
import re


def observe(runtime, build_facts, env):
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
        runtime.finalize_runtime_report(report, status="ok", summary={"account_facts": facts})
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
    raise SystemExit(observe(main, build_ibkr_account_facts, os.environ))
