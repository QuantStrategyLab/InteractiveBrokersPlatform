"""Execute one independently verified account-cash Job in the protected runner.

This module does not schedule itself, change the serving service or publish a
report. Expected account configuration and the prior build input must already
be present in the runner's protected configuration.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
from types import SimpleNamespace

from scripts import deploy_pilot_no_traffic as pilot
from scripts import diagnose_account_facts_identity as identity
from scripts import inspect_account_facts_archive as archive
from scripts import observe_maintenance_cash_job as maintenance
from scripts import prepare_account_cash_job as preparation
from scripts import read_account_cash_job as report_reader


_CANDIDATE_REPOSITORY = "us-central1-docker.pkg.dev/interactivebrokersquant/cloud-run-source-deploy/interactivebrokersplatform/cloud-run"
CANDIDATE_DIGESTS = {
    "global_etf_rotation": "sha256:5a8add8f59583c01f78b17b68b9c61a1563cec09140fea7801dd11640c8409dd",
    "russell_top50_leader_rotation": "sha256:8a4b8efaedc9cdfb9167f19d2fda11a668883b077ae488d5ffc6163a50d9af97",
}


def execute(env, receipt, *, run=pilot.command, snapshot=pilot.snapshot,
            prepare=preparation.prepare, client_factory=None, sleep=time.sleep,
            now=lambda: datetime.now(timezone.utc)):
    """Run exactly one verified definition; use no caller-provided Job template."""
    if (env.get("GITHUB_REF") != "refs/heads/main" or env.get("GITHUB_RUN_ATTEMPT") != "1"
            or env.get("GITHUB_EVENT_NAME") != "workflow_dispatch" or env.get("GITHUB_REPOSITORY") != pilot.REPOSITORY
            or not re.fullmatch(r"[0-9]+", env.get("GITHUB_RUN_ID", "")) or env.get("MAINTENANCE_JOB") != "true"):
        raise pilot.ReleaseError("dispatch_not_admitted")
    profile = env.get("REQUESTED_TARGET")
    if profile not in CANDIDATE_DIGESTS:
        raise pilot.ReleaseError("target_not_supported")
    if env.get("CASH_CANDIDATE_IMAGE") != _CANDIDATE_REPOSITORY + "@" + CANDIDATE_DIGESTS[profile]:
        raise pilot.ReleaseError("candidate_image_mismatch")
    selected = dict(env)
    selected["IBKR_ACCOUNT_FACTS_TARGET"] = archive.resolve_profile_slot(selected)
    # resolve_expected reads the protected runner environment, not a caller's
    # proposed Job or a digest derived from the serving service itself.
    previous = os.environ.get("IBKR_ACCOUNT_FACTS_TARGET")
    try:
        os.environ["IBKR_ACCOUNT_FACTS_TARGET"] = selected["IBKR_ACCOUNT_FACTS_TARGET"]
        expected = identity.resolve_expected()
    finally:
        if previous is None:
            os.environ.pop("IBKR_ACCOUNT_FACTS_TARGET", None)
        else:
            os.environ["IBKR_ACCOUNT_FACTS_TARGET"] = previous
    region = selected.get("GCP_REGION", "")
    if not region or region != selected.get("CLOUD_RUN_REGION"):
        raise pilot.ReleaseError("region_mismatch")
    ctx = SimpleNamespace(project=expected["expected_project_id"], region=region,
                          service=expected["expected_service_name"])
    selected["CASH_CANDIDATE_JOB"] = "ibkr-cash-read-" + selected["GITHUB_RUN_ID"]
    before = snapshot(ctx, run)
    _, _, _, document = prepare(selected, expected)
    if document is None:
        raise pilot.ReleaseError("job_definition_unavailable")
    container = document["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]
    namespace = maintenance.report_namespace({"spec": {"containers": [container]}}, expected)
    job = document["metadata"]["name"]
    result = dict(status="STOPPED", reason="job_not_completed", runtime_adopted=False,
                  publisher_binding_changed=False, broker_write_requested=False, strategy_executed=False,
                  image_evidence_mode=selected.get("CASH_IMAGE_EVIDENCE_MODE", "prebuild"),
                  job_create_attempted=False, job_execute_attempted=False, job_cleanup_verified=False,
                  baseline_unchanged=False)

    def collect(read, actual_job, execution, name, started):
        for _ in range(12):
            logs = read(["logging", "read", f'resource.type="cloud_run_job" AND resource.labels.job_name="{job}" '
                         f'AND labels."run.googleapis.com/execution_name"="{name}" '
                         f'AND timestamp>="{started.isoformat()}" AND '
                         '(jsonPayload.status="observation_saved" OR textPayload:"observation_saved")',
                         "--project=" + ctx.project, "--limit=1000"])
            if logs:
                factory = client_factory
                if factory is None:
                    from google.cloud import storage
                    factory = storage.Client
                return report_reader.readback(expected, actual_job=actual_job, document=document,
                    execution=execution, logs=logs, namespace=namespace, region=ctx.region,
                    client=factory(), now=now())
            sleep(5)
        raise pilot.ReleaseError("job_report_not_observed")

    return maintenance.execute_job(ctx, receipt, document=document, before=before, collect=collect,
                                   result=result, run=run, snapshot=snapshot, sleep=sleep, now=now)


def main():
    try:
        receipt = Path(os.environ["RUNNER_TEMP"]) / "account-cash.safe.json"
        result = execute(os.environ, receipt)
    except Exception:
        result = {"status": "STOPPED", "reason": "preflight_failed"}
    print(json.dumps(result, sort_keys=True))
    return 0 if (result.get("status") == "OBSERVED_JOB_ONLY" and result.get("job_cleanup_verified")
                 and result.get("baseline_unchanged")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
