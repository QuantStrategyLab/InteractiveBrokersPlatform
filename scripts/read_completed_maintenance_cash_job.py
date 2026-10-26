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
from types import SimpleNamespace

from scripts import observe_maintenance_cash_job as jobs

RUN = "40254243027"
EXECUTION_SHA256 = "d7dd2238680e0c2fb148f0a545ab304c64f01ddedd8fe4245e74394bed5a11c5"
STARTED = datetime(2026, 10, 26, 1, 26, 38, tzinfo=timezone.utc)


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


def main():
    result = dict(status="STOPPED", reason="readback_not_completed", readback_only=True, broker_called=False)
    try:
        result = readback(jobs.identity.resolve_expected(), os.environ)
    except Exception as error:
        result["reason"] = str(error) if isinstance(error, jobs.pilot.ReleaseError) else "readback_validation_failed"
        # A fixed exception class is enough to locate a parser/provenance error;
        # its message can contain a private URI and is never forwarded.
        result["error_type"] = type(error).__name__
    path = Path(os.environ["RUNNER_TEMP"]) / "maintenance-cash.safe.json"
    path.write_text(json.dumps(result, indent=2) + "\n")
    path.chmod(0o600)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "OBSERVED_JOB_ONLY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
