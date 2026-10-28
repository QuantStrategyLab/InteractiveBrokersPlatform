"""One temporary Job on the original network/identity; never alter the service."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time

from scripts import deploy_pilot_no_traffic as pilot
from scripts import diagnose_pilot_serving_install as serving
from scripts import diagnose_runner_gateway_access as gateway
from scripts import diagnose_serving_image_identity as identity
from scripts import pilot_app_isolation as app_isolation
from scripts import run_pilot_cloud_tcp as cloud

ENTRY = Path(__file__).with_name("pilot_cloud_tcp_job.py")
NETWORK_KEYS = {"run.googleapis.com/network-interfaces", "run.googleapis.com/vpc-access-connector",
    "run.googleapis.com/vpc-access-egress", "run.googleapis.com/execution-environment",
    "run.googleapis.com/encryption-key", "run.googleapis.com/encryption-key-shutdown-hours",
    "run.googleapis.com/encryption-key-revocation-action"}


def result():
    return dict(schema_version="ibkr.job_tcp_lifecycle.v1", status="unavailable", stage="preflight",
        observed_at=datetime.now(timezone.utc).isoformat(), source_stable=False, binding_verified=False,
        deployment_dry_run_verified=False, job_create_attempted=False, job_created=False,
        job_execute_attempted=False, execution_completed=False, measurement_read=False,
        observation_count=None, tcp_connected=None, tcp_failure_class=None, cloud_http_status=None,
        job_delete_attempted=False, job_deleted=False, cleanup_stage=None, baseline_unchanged=False,
        iam_changed=False, broker_called=False, broker_protocol_sent=False, strategy_imported=False,
        runtime_ready=False, probe_code_sha256=None)


def prepared(ctx, before, group, run_id):
    if (group.execution_backend != "gateway" or group.ib_gateway_mode != "live"
            or tuple(group.account_ids) != tuple(ctx.target["account_selector"])
            or type(group.ib_client_id) is not int or group.ib_client_id <= 0
            or group.ib_gateway_project_id or group.ib_gateway_zone
            or not isinstance(group.ib_gateway_instance_name, str) or not group.ib_gateway_instance_name
            or type(group.ib_gateway_port) is not int or not 1 <= group.ib_gateway_port <= 65535):
        raise pilot.ReleaseError("direct_binding_unverified")
    revision = before["serving_revision"]
    spec = revision["spec"]
    image, container = identity.immutable_image(revision)
    if (not spec.get("serviceAccountName") or spec.get("volumes")
            or container.get("resources", {}).get("limits") not in (
                {"cpu": "1", "memory": "512Mi"}, {"cpu": "1000m", "memory": "512Mi"})):
        raise pilot.ReleaseError("job_runtime_unverified")
    annotations = revision.get("metadata", {}).get("annotations", {})
    network = {key: value for key, value in annotations.items() if key in NETWORK_KEYS}
    if not (network.get("run.googleapis.com/network-interfaces") or network.get("run.googleapis.com/vpc-access-connector")):
        raise pilot.ReleaseError("job_network_unverified")
    code = ENTRY.read_text()
    name = "ibkr-p4-" + hashlib.sha256(run_id.encode()).hexdigest()[:16]
    task = dict(serviceAccountName=spec["serviceAccountName"], maxRetries=0, timeoutSeconds="60",
        containers=[dict(name="tcp", image=image.rsplit("@", 1)[0] + "@" + cloud.BASE,
            command=["/usr/local/bin/python"], args=["-I", "-S", "-c", code],
            resources=dict(limits=copy.deepcopy(container["resources"]["limits"])),
            env=[dict(name="PILOT_TCP_HOST", value=group.ib_gateway_instance_name),
                 dict(name="PILOT_TCP_PORT", value=str(group.ib_gateway_port))])])
    return dict(apiVersion="run.googleapis.com/v1", kind="Job",
        metadata=dict(name=name, namespace=ctx.project, labels={"ibkr-p4-probe": name}),
        spec=dict(template=dict(metadata=dict(annotations=network),
            spec=dict(taskCount=1, parallelism=1, template=dict(spec=task)))))


def verify_template(actual, desired):
    """Compare task configuration, allowing only documented scalar defaults."""
    left, right = copy.deepcopy(actual["spec"]), copy.deepcopy(desired["spec"])
    for spec in (left, right):
        spec.setdefault("taskCount", 1)
        spec.setdefault("parallelism", 1)
        task = spec["template"]["spec"]
        task.setdefault("maxRetries", 0)
        task["timeoutSeconds"] = str(task.get("timeoutSeconds"))
        for row in task["containers"]:
            if row.get("resources", {}).get("limits", {}).get("cpu") == "1000m":
                row["resources"]["limits"]["cpu"] = "1"
    if left != right:
        raise pilot.ReleaseError("job_template_readback_unverified")
    annotations = actual.get("metadata", {}).get("annotations", {})
    expected = desired.get("metadata", {}).get("annotations", {})
    for key in NETWORK_KEYS:
        left, right = annotations.get(key), expected.get(key)
        # Jobs always use gen2. The API can materialize that mandatory default
        # even when the original service leaves its execution environment unset.
        if key == "run.googleapis.com/execution-environment" and left == "gen2" and right is None:
            continue
        if key == "run.googleapis.com/network-interfaces" and left and right:
            left, right = pilot._json(left), pilot._json(right)
        if left != right:
            raise pilot.ReleaseError("job_network_readback_unverified")


def verify_job(actual, desired):
    name = desired["metadata"]["name"]
    if (actual.get("metadata", {}).get("name") != name
            or actual.get("metadata", {}).get("labels", {}).get("ibkr-p4-probe") != name):
        raise pilot.ReleaseError("job_ownership_unverified")
    verify_template(actual["spec"]["template"], desired["spec"]["template"])


def template_difference_categories(actual, desired):
    """Return fixed categories only; never expose template fields or values."""
    try:
        left = copy.deepcopy(actual["spec"]["template"]["spec"])
        right = copy.deepcopy(desired["spec"]["template"]["spec"])
        for spec in (left, right):
            spec.setdefault("taskCount", 1)
            spec.setdefault("parallelism", 1)
            task = spec["template"]["spec"]
            task.setdefault("maxRetries", 0)
            task["timeoutSeconds"] = str(task.get("timeoutSeconds"))
            for container in task["containers"]:
                if container.get("resources", {}).get("limits", {}).get("cpu") == "1000m":
                    container["resources"]["limits"]["cpu"] = "1"
        categories = []
        for key, label in (("taskCount", "task_count"), ("parallelism", "parallelism")):
            if left.get(key) != right.get(key):
                categories.append(label)
        a, b = left["template"]["spec"], right["template"]["spec"]
        for key, label in (("serviceAccountName", "service_account"), ("maxRetries", "retries"),
                           ("timeoutSeconds", "timeout")):
            if a.get(key) != b.get(key):
                categories.append(label)
        if len(a["containers"]) != 1 or len(b["containers"]) != 1:
            categories.append("container_count")
        else:
            ca, cb = a["containers"][0], b["containers"][0]
            for key in ("name", "image", "command", "args", "resources"):
                if ca.get(key) != cb.get(key):
                    categories.append("container_" + key)
            if ca.get("env") != cb.get("env"):
                categories.append("container_env")
                def lifecycle(rows):
                    matches = [x for x in rows if isinstance(x, dict) and x.get("name") == "LIFECYCLE_PERFORMANCE_BUCKET"]
                    return matches[0] if len(matches) == 1 else None
                x, y = lifecycle(ca.get("env", [])), lifecycle(cb.get("env", []))
                if x != y:
                    if x == {"name": "LIFECYCLE_PERFORMANCE_BUCKET"} and y == {"name": "LIFECYCLE_PERFORMANCE_BUCKET", "value": ""}:
                        categories.append("lifecycle_empty_value_omitted")
                    else:
                        categories.append("lifecycle_env_missing" if x is None else "lifecycle_env_changed")
            known = {"name", "image", "command", "args", "resources", "env"}
            if {k: v for k, v in ca.items() if k not in known} != {k: v for k, v in cb.items() if k not in known}:
                categories.append("other_container")
        task_known = {"serviceAccountName", "maxRetries", "timeoutSeconds", "containers"}
        if {k: v for k, v in a.items() if k not in task_known} != {k: v for k, v in b.items() if k not in task_known}:
            categories.append("other_task")
        top_known = {"taskCount", "parallelism", "template"}
        if ({k: v for k, v in left.items() if k not in top_known} != {k: v for k, v in right.items() if k not in top_known}
                or left["template"].get("metadata") != right["template"].get("metadata")):
            categories.append("other_template")
        return categories
    except (KeyError, TypeError, AttributeError):
        return ["unclassified"]


def verify_execution(actual, desired):
    job = desired["metadata"]["name"]
    name = actual.get("metadata", {}).get("name", "")
    if (not re.fullmatch(re.escape(job) + r"-[a-z0-9]+", name)
            or actual.get("metadata", {}).get("labels", {}).get("run.googleapis.com/job") != job):
        raise pilot.ReleaseError("execution_identity_unverified")
    verify_template(actual, desired["spec"]["template"])
    return name


def measurement(rows):
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise pilot.ReleaseError("measurement_not_unique")
    value = rows[0].get("jsonPayload")
    keys = {"schema_version", "tcp_connected", "failure_class", "broker_called", "broker_protocol_sent", "strategy_imported"}
    if (not isinstance(value, dict) or set(value) != keys
            or value["schema_version"] != "ibkr.cloud_tcp_probe.v1" or type(value["tcp_connected"]) is not bool
            or value["failure_class"] not in (None, "name_resolution_failed", "timeout", "connection_refused", "network_error")
            or value["tcp_connected"] != (value["failure_class"] is None)
            or any(value[key] is not False for key in ("broker_called", "broker_protocol_sent", "strategy_imported"))):
        raise pilot.ReleaseError("probe_response_invalid")
    return value


def production_checks(before, after):
    return dict(**cloud.baseline_checks(before, after),
        template_equal=pilot._configuration(before["service"]["spec"]["template"]) ==
            pilot._configuration(after["service"]["spec"]["template"]))


def execute(ctx, run_id, *, run=pilot.command, snapshot=pilot.snapshot, api=cloud.request,
            pause=time.sleep, clock=time.monotonic, progress=None, application_source=None, application_phase="isolation",
            diagnostic_only=False):
    out = result() if progress is None else progress
    deadline = clock() + 600  # Leave cleanup time inside the 15-minute workflow.
    before = snapshot(ctx, run)
    target = pilot._runtime_target(before["serving_revision"])
    if any(target.get(key) != ctx.target.get(key) for key in pilot.IDENTITY_FIELDS):
        raise pilot.ReleaseError("inventory_serving_mismatch")
    def adapt(args, reason):
        return run(list(args), stage=reason)
    source = serving._read(ctx, adapt, include_group_declarations=True)
    group = gateway.read_group(ctx, source["group_declarations"], adapt)
    desired = prepared(ctx, before, group, run_id)
    schema = "ibkr.cloud_tcp_probe.v1"
    if application_source is not None:
        desired = (app_isolation.prepared(ctx, before, group, desired, application_source, phase="cycle")
            if application_phase == "cycle" else app_isolation.prepared(ctx, before, group, desired, application_source))
        schema = "ibkr.guarded_cycle.v1" if application_phase == "cycle" else "ibkr.application_isolation.v1"
        out["schema_version"] = "ibkr.job_guarded_cycle_lifecycle.v1" if application_phase == "cycle" else "ibkr.job_application_isolation_lifecycle.v1"
    name = desired["metadata"]["name"]
    out.update(binding_verified=True, probe_code_sha256=hashlib.sha256(
        desired["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["args"][3].encode()).hexdigest())
    token = run(["gcloud", "auth", "print-access-token", "--quiet"], stage="original_identity_unavailable").strip()
    endpoint = f"https://{ctx.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/{ctx.project}"
    url = endpoint + "/jobs/" + name
    def call(url, method="GET", body=None, stage="job_read_unavailable"):
        return api(url, method=method, token=token, body=body, stage=stage)
    def executions():
        return pilot._json(run(["gcloud", "run", "jobs", "executions", "list", "--job=" + name,
            f"--project={ctx.project}", f"--region={ctx.region}", "--format=json"], stage="executions_read_unavailable"))
    jobs = pilot._json(run(["gcloud", "run", "jobs", "list", f"--project={ctx.project}",
        f"--region={ctx.region}", "--format=json"], stage="jobs_read_unavailable"))
    if not isinstance(jobs, list) or any(row.get("metadata", {}).get("name", "").startswith("ibkr-p4-") for row in jobs):
        raise pilot.ReleaseError("prior_p4_job_remaining")
    verified = call(endpoint + "/jobs?dryRun=all", "POST", desired, "job_dry_run_failed")
    actual_annotations = verified.get("spec", {}).get("template", {}).get("metadata", {}).get("annotations", {})
    expected_annotations = desired["spec"]["template"]["metadata"]["annotations"]
    out["dry_run_network_checks"] = {key.rsplit("/", 1)[-1] + "_equal":
        (pilot._json(actual_annotations[key]) == pilot._json(expected_annotations[key])
            if key.endswith("/network-interfaces") and actual_annotations.get(key) and expected_annotations.get(key)
            else actual_annotations.get(key) == expected_annotations.get(key)) for key in sorted(NETWORK_KEYS)}
    out["job_execution_env_defaulted_gen2"] = (
        actual_annotations.get("run.googleapis.com/execution-environment") == "gen2"
        and "run.googleapis.com/execution-environment" not in expected_annotations)
    if diagnostic_only:
        try:
            verify_job(verified, desired)
            match, reason = True, "match"
        except pilot.ReleaseError as error:
            match = False
            reason = str(error) if str(error) in {"job_template_readback_unverified", "job_network_readback_unverified",
                                                   "job_ownership_unverified"} else "unclassified"
        out.update(status="observed", stage="template_diagnostic", template_match=match, template_reason=reason,
                   template_difference_categories=template_difference_categories(verified, desired))
        return out
    verify_job(verified, desired)
    out["deployment_dry_run_verified"] = True
    if not all(production_checks(before, snapshot(ctx, run)).values()):
        raise pilot.ReleaseError("production_baseline_changed")
    out["source_stable"] = True
    if clock() >= deadline:
        raise pilot.ReleaseError("operation_deadline_reached")
    uid = None
    try:
        out["job_create_attempted"] = True
        call(endpoint + "/jobs", "POST", desired, "job_create_unknown")
        for _ in range(30):
            actual = call(url)
            verify_job(actual, desired)
            uid = actual.get("metadata", {}).get("uid")
            if uid and pilot._ready(actual):
                break
            if clock() >= deadline:
                raise pilot.ReleaseError("operation_deadline_reached")
            pause(2)
        else:
            raise pilot.ReleaseError("job_not_ready")
        out["job_created"] = True
        if executions() or not all(production_checks(before, snapshot(ctx, run)).values()):
            raise pilot.ReleaseError("job_preexecution_unverified")
        out["job_execute_attempted"] = True
        execution = call(url + ":run", "POST", {}, "job_execute_unknown")
        execution_name = verify_execution(execution, desired)
        for _ in range(180 if application_source is not None and application_phase == "cycle" else 90):
            execution = call(endpoint + "/executions/" + execution_name)
            verify_execution(execution, desired)
            if execution.get("status", {}).get("completionTime"):
                break
            if clock() >= deadline:
                raise pilot.ReleaseError("operation_deadline_reached")
            pause(2)
        else:
            raise pilot.ReleaseError("execution_completion_unverified")
        out["execution_completed"] = True
        status = execution["status"]
        task_ok = status.get("succeededCount") == 1 and not any(status.get(key, 0) for key in (
                "failedCount", "cancelledCount", "retriedCount", "runningCount"))
        if not task_ok and application_source is None:
            raise pilot.ReleaseError("job_task_failed")
        query = (f'resource.type="cloud_run_job" AND resource.labels.job_name="{name}" '
            f'AND labels."run.googleapis.com/execution_name"="{execution_name}" '
            f'AND jsonPayload.schema_version="{schema}"')
        for _ in range(20):
            rows = pilot._json(run(["gcloud", "logging", "read", query, f"--project={ctx.project}",
                "--limit=2", "--format=json(jsonPayload)"], stage="measurement_log_unavailable"))
            if rows or clock() >= deadline:
                break
            pause(2)
        out["observation_count"] = len(rows) if isinstance(rows, list) else None
        if application_source is None:
            value = measurement(rows)
            out.update(status="observed", stage="port_observed", measurement_read=True,
                tcp_connected=value["tcp_connected"], tcp_failure_class=value["failure_class"])
        else:
            if application_phase == "cycle":
                value = app_isolation.cycle_measurement(rows)
                out.update(status="observed" if task_ok and value["cycle"]["cycle_completed"] else "unavailable",
                    stage=value["cycle"]["stage"], measurement_read=True, guarded_cycle=value,
                    broker_called=value["cycle"]["broker_called"], broker_protocol_sent=None,
                    strategy_imported=value["isolation"]["application_imported"])
            else:
                value = app_isolation.measurement(rows)
                out.update(status="observed" if task_ok and value["isolation_passed"] else "unavailable",
                    stage="application_isolation_observed", measurement_read=True, application_isolation=value,
                    strategy_imported=value["application_imported"])
    except cloud.CloudError as error:
        out.update(stage=str(error), cloud_http_status=error.http_status)
    except pilot.ReleaseError as error:
        out["stage"] = str(error)
    except Exception:
        out["stage"] = "job_operation_unavailable"
    finally:
        cleanup_deadline = clock() + 120
        try:
            actual = call(url, stage="cleanup_job_read_unavailable")
            verify_job(actual, desired)
            if uid and actual.get("metadata", {}).get("uid") != uid:
                raise pilot.ReleaseError("cleanup_job_identity_changed")
            runs = executions()
            if not isinstance(runs, list) or len(runs) > 1:
                raise pilot.ReleaseError("cleanup_execution_ambiguous")
            for execution in runs:
                execution_name = verify_execution(execution, desired)
                if not execution.get("status", {}).get("completionTime"):
                    call(endpoint + "/executions/" + execution_name + ":cancel", "POST", {}, "job_cancel_unknown")
                    for _ in range(12):
                        execution = call(endpoint + "/executions/" + execution_name)
                        verify_execution(execution, desired)
                        if execution.get("status", {}).get("completionTime"):
                            break
                        if clock() >= cleanup_deadline:
                            raise pilot.ReleaseError("cleanup_deadline_reached")
                        pause(2)
                    else:
                        raise pilot.ReleaseError("cleanup_execution_still_active")
            out["job_delete_attempted"] = True
            call(url, "DELETE", stage="job_delete_unknown")
            for _ in range(12):
                try:
                    call(url, stage="job_delete_readback")
                except cloud.CloudError as error:
                    if error.http_status != 404:
                        raise
                    out["job_deleted"] = True
                    break
                if clock() >= cleanup_deadline:
                    raise pilot.ReleaseError("cleanup_deadline_reached")
                pause(2)
            if not out["job_deleted"]:
                raise pilot.ReleaseError("job_delete_unverified")
        except cloud.CloudError as error:
            if error.http_status == 404 and not out["job_execute_attempted"]:
                out["job_deleted"] = True
            else:
                out.update(status="unavailable", cleanup_stage=str(error), cleanup_http_status=error.http_status)
        except Exception as error:
            out.update(status="unavailable", cleanup_stage=str(error) if isinstance(error, pilot.ReleaseError) else "cleanup_unverified")
        try:
            out["production_checks"] = production_checks(before, snapshot(ctx, run))
            out["baseline_unchanged"] = all(out["production_checks"].values())
        except Exception:
            out["baseline_unchanged"] = False
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--application-source", type=Path)
    args = parser.parse_args(argv)
    out = result()
    try:
        ctx = pilot.context(os.environ)
        if os.environ.get("PILOT_JOB_NETWORK_PREFLIGHT") != "true" or os.environ.get("PILOT_CLEANUP_RUN_ID"):
            raise pilot.ReleaseError("job_mode_not_admitted")
        phase = os.environ.get("PILOT_JOB_PHASE", "tcp")
        if phase not in {"tcp", "isolation", "cycle"} or (phase != "tcp") != (args.application_source is not None):
            raise pilot.ReleaseError("job_phase_not_admitted")
        diagnostic = os.environ.get("PILOT_TEMPLATE_DIAGNOSTIC", "false")
        if diagnostic not in {"true", "false"} or (diagnostic == "true" and phase != "cycle"):
            raise pilot.ReleaseError("template_diagnostic_mode_invalid")
        execute(ctx, os.environ["GITHUB_RUN_ID"], progress=out, application_source=args.application_source,
                application_phase=phase, diagnostic_only=diagnostic == "true")
    except cloud.CloudError as error:
        out.update(stage=str(error), cloud_http_status=error.http_status)
    except pilot.ReleaseError as error:
        out["stage"] = str(error)
    except Exception:
        out["stage"] = "preflight_unavailable"
    args.receipt.write_text(json.dumps(out, sort_keys=True) + "\n")
    args.receipt.chmod(0o600)
    print(json.dumps(out, sort_keys=True))
    measured = (out["tcp_connected"] or out.get("application_isolation", {}).get("isolation_passed")
        or out.get("guarded_cycle", {}).get("cycle", {}).get("cycle_completed"))
    diagnostic_complete = out.get("stage") == "template_diagnostic" and out.get("status") == "observed"
    return 0 if diagnostic_complete or (out["status"] == "observed" and out["measurement_read"] and measured and out["job_deleted"] and out["baseline_unchanged"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
