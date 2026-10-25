"""One existing /probe on a zero-traffic maintenance revision, in the cloud only.

Raw reports stay in runner memory and the original private report store. This
does not publish facts, move normal traffic, run a strategy or authorize cash.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import urlsplit

from scripts import deploy_pilot_no_traffic as pilot
from scripts import diagnose_account_facts_identity as identity
from scripts import publish_account_facts_from_report as publisher


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def configuration(revision):
    result = pilot._configuration(revision, without_image=True)
    result["metadata"]["labels"] = {k: v for k, v in result["metadata"]["labels"].items()
        if k not in pilot.BUILD_LABELS}
    return result


def owned_job(value, *, job, url, caller, audience):
    target = value.get("httpTarget", {})
    return (value.get("name", "").rsplit("/", 1)[-1] == job
        and target.get("uri") == url + "/probe" and target.get("httpMethod") == "POST"
        and target.get("oidcToken", {}).get("serviceAccountEmail") == caller
        and target.get("oidcToken", {}).get("audience") == audience)


def admit(expected, state, candidate, source):
    """Compare full runtime configuration, not just a replacement revision name."""
    service = state["service"]
    name = candidate.get("metadata", {}).get("name")
    status = service.get("status", {})
    if (not pilot.SHA.fullmatch(source) or not pilot.NAME.fullmatch(name or "")
            or name != status.get("latestCreatedRevisionName")
            or name == state["serving"]
            or not pilot._ready(candidate)
            or candidate.get("metadata", {}).get("labels", {}).get("commit-sha") != source
            or configuration(candidate) != configuration(state["serving_revision"])
            or any(row["revision"] == name and row["percent"] for row in state["traffic"])
            or expected["expected_runtime_revision"] != state["serving"]):
        raise pilot.ReleaseError("candidate_not_admitted")
    runtime = pilot._runtime_target(candidate)
    if (runtime != pilot._runtime_target(state["serving_revision"])
            or runtime.get("strategy_profile") != pilot.PROFILE
            or runtime.get("platform_id") != "ibkr"
            or runtime.get("service_name") != expected["expected_service_name"]
            or runtime.get("account_scope") != expected["expected_account_scope"]
            or tuple(runtime.get("account_selector", ())) != expected["expected_account_selector"]
            or runtime.get("deployment_selector") != expected["expected_deployment_selector"]):
        raise pilot.ReleaseError("candidate_identity_mismatch")
    return name


def project(report, uri, expected, *, candidate, started, now):
    """A candidate projection never changes the configured publisher binding."""
    scope = {**expected, "expected_runtime_revision": candidate}
    result = publisher.project_ibkr_account_facts_history(report, source_report_uri=uri, **scope)
    if result.get("schema_version") != publisher.HISTORY_SCHEMA:
        raise pilot.ReleaseError("candidate_report_projection_failed")
    facts = report["summary"]["account_facts"]
    observed = publisher._observed_timestamp(facts.get("observed_at"))
    if (observed < started or observed > now + timedelta(minutes=5)
            or observed < now - publisher.IBKR_ACCOUNT_FACTS_MAX_AGE):
        raise pilot.ReleaseError("candidate_report_time_invalid")
    from application.ibkr_cash_observations import inspect_cash_observations
    selected = [row for row in facts["cash"] if row["currency"] == "USD"]
    attachment = inspect_cash_observations(facts.get("broker_cash_observations"),
        observed_at=facts["observed_at"], selected_cash=selected[0] if len(selected) == 1 else None)
    return dict(status="OBSERVED_CANDIDATE_ONLY", projection_verified=True,
        observed_at=observed.isoformat(), report_sha256=digest(json.dumps(report, sort_keys=True)),
        cash_observations=attachment, runtime_adopted=False, publisher_binding_changed=False,
        spendable_cash_verified=False, complete_obligations_verified=False)


def observe(expected, source, env, receipt, *, run=pilot.command, snapshot=pilot.snapshot,
            sleep=time.sleep, now=lambda: datetime.now(timezone.utc)):
    if (env.get("GITHUB_REF") != "refs/heads/main" or env.get("GITHUB_RUN_ATTEMPT") != "1"
            or env.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or env.get("GITHUB_REPOSITORY") != pilot.REPOSITORY
            or not re.fullmatch(r"[0-9]+", env.get("GITHUB_RUN_ID", ""))
            or not pilot.SHA.fullmatch(source)):
        raise pilot.ReleaseError("dispatch_not_admitted")
    ctx = SimpleNamespace(project=expected["expected_project_id"],
        region=env["CLOUD_RUN_REGION"], service=expected["expected_service_name"])
    common = ["--project=" + ctx.project, "--region=" + ctx.region]
    scheduler = ["--project=" + ctx.project, "--location=" + ctx.region]

    def read(args, stage):
        return pilot._json(run(["gcloud", *args, "--format=json"], stage=stage))

    before = snapshot(ctx, run)
    candidate = before["service"].get("status", {}).get("latestCreatedRevisionName")
    if not pilot.NAME.fullmatch(candidate or ""):
        raise pilot.ReleaseError("candidate_not_admitted")
    revision = read(["run", "revisions", "describe", candidate, *common], "candidate_read_failed")
    candidate = admit(expected, before, revision, source)
    tag = "cash-read-" + env["GITHUB_RUN_ID"]
    job = "ibkr-cash-read-" + env["GITHUB_RUN_ID"]
    service_url = before["service"]["status"]["url"]
    # Reuse the existing warm-up identity. No new IAM, account or caller.
    warmups = [row for row in before["scheduler"] if row.get("state") == "ENABLED"
        and row.get("httpTarget", {}).get("uri") == service_url + "/health"
        and row.get("httpTarget", {}).get("httpMethod") == "GET"]
    if len(warmups) != 1:
        raise pilot.ReleaseError("existing_warmup_identity_unavailable")
    token = warmups[0]["httpTarget"].get("oidcToken", {})
    caller = token.get("serviceAccountEmail")
    if not caller or token.get("audience") != service_url:
        raise pilot.ReleaseError("existing_warmup_identity_unavailable")
    jobs = read(["scheduler", "jobs", "list", *scheduler], "scheduler_read_failed")
    if (any(row.get("name", "").rsplit("/", 1)[-1] == job for row in jobs)
            or any(row["tag"] == tag for row in before["traffic"])):
        raise pilot.ReleaseError("observation_resource_already_exists")
    started = now()
    tag_attempted = job_attempted = False
    result = dict(status="STOPPED", reason="observation_not_completed", source_commit=source,
        candidate_revision_sha256=digest(candidate), candidate_only=True,
        runtime_adopted=False, broker_write_requested=False, strategy_executed=False,
        tag_cleanup_verified=False, job_cleanup_verified=False, baseline_restored=False)

    def save():
        receipt.write_text(json.dumps(result, indent=2) + "\n")
        receipt.chmod(0o600)

    save()
    try:
        tag_attempted = True
        run(["gcloud", "run", "services", "update-traffic", ctx.service, *common,
            "--update-tags=" + tag + "=" + candidate, "--quiet", "--format=json"], stage="tag_update_unknown")
        tagged = read(["run", "services", "describe", ctx.service, *common], "tag_read_failed")
        rows, serving = pilot._traffic(tagged)
        matches = [row for row in rows if row["tag"] == tag and row["revision"] == candidate and row["percent"] == 0]
        if (len(matches) != 1 or serving != before["serving"]
                or [row for row in rows if row["tag"] != tag] != before["traffic"]):
            raise pilot.ReleaseError("tag_routing_unverified")
        url = matches[0]["url"]
        if not isinstance(url, str) or not url.startswith("https://") or not urlsplit(url).hostname.endswith(".run.app"):
            raise pilot.ReleaseError("tag_url_unverified")
        schedule = (started + timedelta(minutes=3)).strftime("%M %H %d %m *")
        job_attempted = True
        run(["gcloud", "scheduler", "jobs", "create", "http", job, *scheduler,
            "--schedule=" + schedule, "--time-zone=Etc/UTC", "--uri=" + url + "/probe",
            "--http-method=POST", "--oidc-service-account-email=" + caller,
            "--oidc-token-audience=" + service_url, "--attempt-deadline=300s",
            "--max-retry-attempts=0", "--max-retry-duration=0s", "--quiet", "--format=json"],
            stage="scheduler_create_unknown")
        created = read(["scheduler", "jobs", "describe", job, *scheduler], "scheduler_readback_failed")
        retry = created.get("retryConfig", {})
        if (not owned_job(created, job=job, url=url, caller=caller, audience=service_url)
                or created.get("state") != "ENABLED" or created.get("schedule") != schedule
                or created.get("timeZone") != "Etc/UTC" or created.get("attemptDeadline") != "300s"
                or retry.get("retryCount", 0) != 0 or retry.get("maxRetryDuration", "0s") != "0s"):
            raise pilot.ReleaseError("scheduler_readback_unverified")
        result["probe_scheduled"] = True
        save()
        # Logs/reports remain in the protected runner. A read failure stops;
        # only successful empty reads wait for the scheduled request.
        for _ in range(84):
            logs = read(["logging", "read", f'resource.type="cloud_run_revision" AND resource.labels.revision_name="{candidate}" AND timestamp>="{started.isoformat()}" AND textPayload:"execution_report gs://"',
                "--project=" + ctx.project, "--limit=10"], "report_log_read_failed")
            uris = set()
            for entry in logs:
                match = re.search(r"execution_report (gs://[^\s]+)", entry.get("textPayload", ""))
                if match:
                    uris.add(publisher._source_report_uri(match[1], expected["expected_report_prefix"]))
            if len(uris) > 1:
                raise pilot.ReleaseError("candidate_report_ambiguous")
            if uris:
                uri = uris.pop()
                report = publisher._load_gcs_report(uri, project_id=ctx.project)
                if report.get("status") != "ok":
                    raise pilot.ReleaseError("candidate_probe_failed")
                result.update(project(report, uri, expected, candidate=candidate, started=started, now=now()))
                break
            sleep(5)
        else:
            raise pilot.ReleaseError("candidate_report_not_observed")
    except Exception as error:
        result.update(status="STOPPED", reason=str(error) if isinstance(error, pilot.ReleaseError) else "observation_failed")
    finally:
        # Each mutation is attempted once. Read back ambiguous creates before
        # deleting only this run's exact owned resource; never rerun the probe.
        cleanup_errors = []
        try:
            if job_attempted:
                jobs = read(["scheduler", "jobs", "list", *scheduler], "cleanup_job_read_failed")
                owned = [row for row in jobs if row.get("name", "").rsplit("/", 1)[-1] == job]
                if owned:
                    if len(owned) != 1 or not owned_job(owned[0], job=job, url=url, caller=caller, audience=service_url):
                        raise pilot.ReleaseError("cleanup_job_ownership_unknown")
                    run(["gcloud", "scheduler", "jobs", "delete", job, *scheduler, "--quiet"], stage="job_delete_unknown")
                jobs = read(["scheduler", "jobs", "list", *scheduler], "cleanup_job_read_failed")
                if any(row.get("name", "").rsplit("/", 1)[-1] == job for row in jobs):
                    raise pilot.ReleaseError("cleanup_job_remaining")
            result["job_cleanup_verified"] = True
        except Exception as error:
            cleanup_errors.append(str(error) if isinstance(error, pilot.ReleaseError) else "job_cleanup_unverified")
        try:
            if tag_attempted:
                state = read(["run", "services", "describe", ctx.service, *common], "cleanup_tag_read_failed")
                rows, _ = pilot._traffic(state)
                owned = [row for row in rows if row["tag"] == tag]
                if owned:
                    if len(owned) != 1 or owned[0]["revision"] != candidate or owned[0]["percent"]:
                        raise pilot.ReleaseError("cleanup_tag_ownership_unknown")
                    run(["gcloud", "run", "services", "update-traffic", ctx.service, *common,
                        "--remove-tags=" + tag, "--quiet", "--format=json"], stage="tag_remove_unknown")
            result["tag_cleanup_verified"] = True
        except Exception as error:
            cleanup_errors.append(str(error) if isinstance(error, pilot.ReleaseError) else "tag_cleanup_unverified")
        try:
            after = snapshot(ctx, run)
            pilot.unchanged(before, after)
            result["baseline_restored"] = True
        except Exception as error:
            cleanup_errors.append(str(error) if isinstance(error, pilot.ReleaseError) else "baseline_unverified")
        if cleanup_errors:
            result.update(status="STOPPED", cleanup_reasons=cleanup_errors)
        save()
    return result


def main():
    receipt = Path(os.environ["RUNNER_TEMP"]) / "maintenance-cash.safe.json"
    result = dict(status="STOPPED", reason="preflight_failed", candidate_only=True, runtime_adopted=False)
    try:
        expected = identity.resolve_expected()
        result = observe(expected, os.environ.get("MAINTENANCE_SOURCE_COMMIT", ""), os.environ, receipt)
    except Exception as error:
        result["reason"] = str(error) if isinstance(error, pilot.ReleaseError) else "preflight_failed"
    receipt.write_text(json.dumps(result, indent=2) + "\n")
    receipt.chmod(0o600)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "OBSERVED_CANDIDATE_ONLY" and result.get("baseline_restored") else 1


if __name__ == "__main__":
    raise SystemExit(main())
