"""One temporary zero-default-traffic revision, original network/identity, no SDK.

This changes the admitted service only after explicit deployment authorization.
All resource names, configuration, tokens and native output stay in the runner.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

from scripts import deploy_pilot_no_traffic as pilot
from scripts import diagnose_pilot_serving_install as serving
from scripts import diagnose_runner_gateway_access as gateway
from scripts import diagnose_serving_image_identity as identity

BASE = "sha256:714c510974267e055d35dcb129a4ce2afc628b069daff1987127627d80573bd2"


def result():
    return dict(schema_version="ibkr.cloud_tcp_lifecycle.v1", status="unavailable", stage="preflight",
        observed_at=datetime.now(timezone.utc).isoformat(), source_stable=False,
        binding_verified=False, restore_dry_run_verified=False, deployment_dry_run_verified=False,
        deployment_attempted=False, candidate_ready=False, measurement_read=False,
        observation_count=None, tcp_connected=None, tcp_failure_class=None,
        cloud_http_status=None, cleanup_stage=None,
        restore_attempted=False, configuration_restored=False, traffic_restored=False,
        latest_revision_released=False,
        revision_delete_attempted=False, revision_deleted=False, baseline_unchanged=False,
        iam_changed=False, broker_called=False, broker_protocol_sent=False,
        strategy_imported=False, runtime_ready=False, probe_code_sha256=None)


class CloudError(pilot.ReleaseError):
    def __init__(self, stage, status=None):
        super().__init__(stage)
        self.http_status = status


def request(url, *, method, token, body=None, stage="cloud_request_failed"):
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), gateway.NoRedirect())
    try:
        with opener.open(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=30) as response:
            raw = response.read(4_000_001)
        if len(raw) > 4_000_000:
            raise ValueError()
        return pilot._json(raw.decode()) if raw else {}
    except urllib.error.HTTPError as error:
        raise CloudError(stage, error.code) from None
    except Exception:
        raise CloudError(stage) from None


def document(service, *, version=None):
    metadata = copy.deepcopy(service["metadata"])
    kept = {key: metadata[key] for key in ("name", "namespace", "labels", "annotations", "resourceVersion") if key in metadata}
    kept["annotations"] = {key: value for key, value in kept.get("annotations", {}).items() if key not in pilot.TOOL_ANNOTATIONS}
    if version is not None:
        kept["resourceVersion"] = version
    out = dict(apiVersion="serving.knative.dev/v1", kind="Service", metadata=kept,
        spec=copy.deepcopy(service["spec"]))
    # Pin the exact existing template name so restoration can reuse that revision.
    out["spec"]["template"]["metadata"]["name"] = (
        service["spec"]["template"]["metadata"].get("name") or service["status"]["latestCreatedRevisionName"])
    return out


def prepared(ctx, before, group, run_id):
    if (group.execution_backend != "gateway" or group.ib_gateway_mode != "live"
            or tuple(group.account_ids) != tuple(ctx.target["account_selector"])
            or type(group.ib_client_id) is not int or group.ib_client_id <= 0
            or group.ib_gateway_project_id or group.ib_gateway_zone
            or not isinstance(group.ib_gateway_instance_name, str) or not group.ib_gateway_instance_name
            or type(group.ib_gateway_port) is not int or not 1 <= group.ib_gateway_port <= 65535):
        raise pilot.ReleaseError("direct_binding_unverified")
    base, _ = identity.immutable_image(before["serving_revision"])
    # The approved probe image is frozen independently of normal deployments.
    # Keep the existing repository and never adopt a newer serving digest.
    base = base.rsplit("@", 1)[0] + "@" + BASE
    candidate = ctx.service + "-p" + hashlib.sha256(run_id.encode()).hexdigest()[:8]
    if not pilot.NAME.fullmatch(candidate):
        raise pilot.ReleaseError("temporary_name_unavailable")
    # A dynamic latest target could resolve to the temporary revision during
    # restoration. Require an already-pinned baseline rather than changing it.
    if any(row.get("latestRevision") is True or not row.get("revisionName")
            for row in before["service"]["spec"].get("traffic", [])):
        raise pilot.ReleaseError("latest_traffic_restore_unverified")
    out = document(before["service"])
    template = {"metadata": pilot._metadata(before["serving_revision"]),
        "spec": copy.deepcopy(before["serving_revision"]["spec"])}
    template["metadata"]["name"] = candidate
    for key in pilot.BUILD_LABELS:
        template["metadata"]["labels"].pop(key, None)
    template["metadata"]["labels"].pop("cloud.googleapis.com/location", None)
    for key in (pilot.INGRESS_ANNOTATION,):
        template["metadata"]["annotations"].pop(key, None)
    template["metadata"]["annotations"].update({"autoscaling.knative.dev/minScale": "0",
        "autoscaling.knative.dev/maxScale": "1", "run.googleapis.com/health-check-disabled": "false"})
    spec = template["spec"]
    if spec.get("volumes") or len(spec.get("containers", [])) != 1:
        raise pilot.ReleaseError("container_isolation_unverified")
    code = Path(__file__).with_name("pilot_cloud_tcp_server.py").read_text()
    spec.update(containerConcurrency=1, timeoutSeconds=60)
    container = copy.deepcopy(spec["containers"][0])
    container.update(image=base, command=["/usr/local/bin/python"], args=["-I", "-S", "-c", code],
        env=[dict(name="PILOT_TCP_HOST", value=group.ib_gateway_instance_name),
             dict(name="PILOT_TCP_PORT", value=str(group.ib_gateway_port))])
    spec["containers"] = [container]
    out["spec"]["template"] = template
    out["spec"]["traffic"] = [dict(revisionName=row["revision"], percent=row["percent"],
        **({"tag": row["tag"]} if row["tag"] else {})) for row in before["traffic"]]
    return out, candidate, hashlib.sha256(code.encode()).hexdigest()


def global_configuration(service):
    metadata = pilot._metadata(service)
    # Cloud Run generates this URL projection; routing is compared separately.
    metadata["annotations"].pop("run.googleapis.com/urls", None)
    return metadata


def baseline_checks(before, current):
    traffic = current["traffic"]
    return dict(serving_equal=before["serving"] == current["serving"],
        serving_configuration_equal=pilot._configuration(before["serving_revision"]) == pilot._configuration(current["serving_revision"]),
        serving_image_equal=identity.immutable_image(before["serving_revision"])[0] == identity.immutable_image(current["serving_revision"])[0],
        iam_equal=before["iam"] == current["iam"], scheduler_equal=before["scheduler"] == current["scheduler"],
        traffic_equal=traffic == before["traffic"],
        service_spec_equal={k: v for k, v in before["service"]["spec"].items() if k not in ("template", "traffic")} ==
            {k: v for k, v in current["service"]["spec"].items() if k not in ("template", "traffic")},
        global_configuration_equal=global_configuration(before["service"]) == global_configuration(current["service"]))


def protected_baseline(before, current):
    if not all(baseline_checks(before, current).values()):
        raise pilot.ReleaseError("production_baseline_changed")


def probe_configuration(template):
    config = pilot._configuration(template)
    annotations = config["metadata"]["annotations"]
    # Cloud Run omits the enabled (false disabled flag) default in its response.
    # Normalize only that representation on the probe, never production state.
    if annotations.get("run.googleapis.com/health-check-disabled") == "false":
        annotations.pop("run.googleapis.com/health-check-disabled")
    return config


def execute(ctx, run_id, *, run=pilot.command, snapshot=pilot.snapshot, api=request, pause=time.sleep, progress=None):
    out = result() if progress is None else progress
    before = snapshot(ctx, run)
    pilot._no_manual_scaling(before["service"])
    pilot._verify_ingress_projection(before["service"], before["serving_revision"])
    actual_target = pilot._runtime_target(before["serving_revision"])
    if any(actual_target.get(key) != ctx.target.get(key) for key in pilot.IDENTITY_FIELDS):
        raise pilot.ReleaseError("inventory_serving_mismatch")
    def adapt(args, reason):
        return run(list(args), stage=reason)
    source = serving._read(ctx, adapt, include_group_declarations=True)
    group = gateway.read_group(ctx, source["group_declarations"], adapt)
    desired, candidate, code_hash = prepared(ctx, before, group, run_id)
    out.update(binding_verified=True, probe_code_sha256=code_hash)
    token = run(["gcloud", "auth", "print-access-token", "--quiet"], stage="original_identity_unavailable").strip()
    endpoint = f"https://{ctx.region}-run.googleapis.com/apis/serving.knative.dev/v1/namespaces/{ctx.project}"
    service_url = endpoint + "/services/" + ctx.service
    backup = document(before["service"])
    # Validate the original existing revision name/configuration without persisting.
    restored = api(service_url + "?dryRun=all", method="PUT", token=token, body=backup, stage="restore_dry_run_failed")
    if pilot._configuration(restored["spec"]["template"]) != pilot._configuration(backup["spec"]["template"]):
        raise pilot.ReleaseError("restore_dry_run_mismatch")
    out["restore_dry_run_verified"] = True
    validated = api(service_url + "?dryRun=all", method="PUT", token=token, body=desired, stage="deployment_dry_run_failed")
    actual, expected = pilot._configuration(validated["spec"]["template"]), pilot._configuration(desired["spec"]["template"])
    out["deployment_health_check_defaulted"] = (
        "run.googleapis.com/health-check-disabled" not in actual["metadata"]["annotations"])
    if probe_configuration(validated["spec"]["template"]) != probe_configuration(desired["spec"]["template"]):
        ac, ec = actual["spec"]["containers"][0], expected["spec"]["containers"][0]
        aa, ea = actual["metadata"]["annotations"], expected["metadata"]["annotations"]
        health = "run.googleapis.com/health-check-disabled"
        out["deployment_template_checks"] = dict(
            labels_equal=actual["metadata"]["labels"] == expected["metadata"]["labels"],
            non_build_labels_equal={k: v for k, v in actual["metadata"]["labels"].items() if k not in pilot.BUILD_LABELS} ==
                {k: v for k, v in expected["metadata"]["labels"].items() if k not in pilot.BUILD_LABELS},
            annotations_equal=aa == ea,
            non_health_annotations_equal={k: v for k, v in aa.items() if k != health} == {k: v for k, v in ea.items() if k != health},
            health_flag_enabled=aa.get(health) == "false", health_flag_absent=health not in aa,
            other_spec_equal={k: v for k, v in actual["spec"].items() if k != "containers"} == {k: v for k, v in expected["spec"].items() if k != "containers"},
            container_equal=ac == ec,
            **{key + "_equal": ac.get(key) == ec.get(key) for key in ("image", "command", "args", "env", "resources", "startupProbe")})
        raise pilot.ReleaseError("deployment_dry_run_mismatch")
    out["deployment_dry_run_verified"] = True
    fresh = snapshot(ctx, run)
    protected_baseline(before, fresh)
    if pilot._configuration(before["service"]["spec"]["template"]) != pilot._configuration(fresh["service"]["spec"]["template"]):
        raise pilot.ReleaseError("production_baseline_changed")
    if before["service"]["metadata"].get("resourceVersion") != fresh["service"]["metadata"].get("resourceVersion"):
        raise pilot.ReleaseError("production_baseline_changed")
    out["source_stable"] = True
    try:
        out["deployment_attempted"] = True
        api(service_url, method="PUT", token=token, body=desired, stage="deployment_response_unknown")
        current = None
        for _ in range(20):
            current = snapshot(ctx, run)
            protected_baseline(before, current)
            revision = pilot._json(run(["gcloud", "run", "revisions", "describe", candidate,
                f"--project={ctx.project}", f"--region={ctx.region}", "--format=json"], stage="candidate_read_unavailable"))
            if revision.get("metadata", {}).get("name") != candidate:
                raise pilot.ReleaseError("candidate_identity_unverified")
            if pilot._ready(revision):
                break
            pause(2)
        else:
            raise pilot.ReleaseError("candidate_not_ready")
        if probe_configuration(current["service"]["spec"]["template"]) != probe_configuration(desired["spec"]["template"]):
            raise pilot.ReleaseError("candidate_configuration_changed")
        out["candidate_ready"] = True
        filter_text = ('resource.type="cloud_run_revision" AND resource.labels.revision_name="' + candidate
            + '" AND jsonPayload.schema_version="ibkr.cloud_tcp_probe.v1"')
        for _ in range(20):
            observations = pilot._json(run(["gcloud", "logging", "read", filter_text,
                f"--project={ctx.project}", "--limit=2", "--format=json(jsonPayload)"], stage="measurement_log_unavailable"))
            if not isinstance(observations, list):
                raise pilot.ReleaseError("probe_response_invalid")
            if observations:
                break
            pause(2)
        out["observation_count"] = len(observations)
        if len(observations) != 1 or not isinstance(observations[0], dict):
            raise pilot.ReleaseError("measurement_not_unique")
        measured = observations[0].get("jsonPayload")
        if not isinstance(measured, dict):
            raise pilot.ReleaseError("probe_response_invalid")
        expected_keys = {"schema_version", "tcp_connected", "failure_class", "broker_called", "broker_protocol_sent", "strategy_imported"}
        if (set(measured) != expected_keys or measured["schema_version"] != "ibkr.cloud_tcp_probe.v1"
                or type(measured["tcp_connected"]) is not bool
                or measured["failure_class"] not in (None, "name_resolution_failed", "timeout", "connection_refused", "network_error")
                or measured["tcp_connected"] != (measured["failure_class"] is None)
                or any(measured[key] is not False for key in ("broker_called", "broker_protocol_sent", "strategy_imported"))):
            raise pilot.ReleaseError("probe_response_invalid")
        out.update(status="observed", stage="port_observed", measurement_read=True,
            tcp_connected=measured["tcp_connected"], tcp_failure_class=measured["failure_class"])
    except CloudError as error:
        out.update(stage=str(error), cloud_http_status=error.http_status)
    except pilot.ReleaseError as error:
        out["stage"] = str(error)
    finally:
        try:
            current = snapshot(ctx, run)
            protected_baseline(before, current)
            if probe_configuration(current["service"]["spec"]["template"]) != probe_configuration(desired["spec"]["template"]):
                raise pilot.ReleaseError("cleanup_concurrent_change")
            backup["metadata"]["resourceVersion"] = current["service"]["metadata"]["resourceVersion"]
            out["restore_attempted"] = True
            api(service_url, method="PUT", token=token, body=backup, stage="restore_response_unknown")
            for _ in range(20):
                restored_state = snapshot(ctx, run)
                try:
                    out["restoration_checks"] = baseline_checks(before, restored_state)
                    protected_baseline(before, restored_state)
                    if pilot._configuration(restored_state["service"]["spec"]["template"]) != pilot._configuration(before["service"]["spec"]["template"]):
                        raise pilot.ReleaseError("restore_readback_mismatch")
                    out.update(configuration_restored=True, traffic_restored=True, baseline_unchanged=True)
                    status = restored_state["service"].get("status", {})
                    out["latest_revision_released"] = candidate not in (
                        status.get("latestCreatedRevisionName"), status.get("latestReadyRevisionName"))
                    if out["latest_revision_released"]:
                        break
                except pilot.ReleaseError:
                    pass
                pause(2)
            else:
                raise pilot.ReleaseError("temporary_revision_still_latest" if out["configuration_restored"] else "restore_readback_mismatch")
            if any(row["revision"] == candidate for row in restored_state["traffic"]):
                raise pilot.ReleaseError("temporary_revision_still_referenced")
            out["revision_delete_attempted"] = True
            api(endpoint + "/revisions/" + candidate, method="DELETE", token=token, stage="revision_delete_unknown")
            try:
                api(endpoint + "/revisions/" + candidate, method="GET", token=token, stage="revision_delete_readback")
            except CloudError as error:
                if error.http_status != 404:
                    raise
                out["revision_deleted"] = True
            if not out["revision_deleted"]:
                raise pilot.ReleaseError("revision_delete_unverified")
        except pilot.ReleaseError as error:
            out.update(status="unavailable", cleanup_stage=str(error))
        except Exception:
            out.update(status="unavailable", cleanup_stage="cleanup_unverified")
        if out["restore_attempted"] and not out["revision_delete_attempted"]:
            recovery = result()
            try:
                cleanup_existing(ctx, run_id, run=run, snapshot=snapshot, api=api, progress=recovery)
            except pilot.ReleaseError as error:
                out["resource_cleanup_stage"] = str(error)
            except Exception:
                out["resource_cleanup_stage"] = "unverified"
            out["revision_delete_attempted"] = recovery["revision_delete_attempted"]
            out["revision_deleted"] = recovery["revision_deleted"]
            out["resource_cleanup_baseline_unchanged"] = recovery["baseline_unchanged"]
    return out


def cleanup_existing(ctx, run_id, *, run=pilot.command, snapshot=pilot.snapshot, api=request, progress=None):
    """Remove only an owned, unused, non-latest probe; never deploy or restore."""
    out = result() if progress is None else progress
    out["schema_version"] = "ibkr.cloud_tcp_cleanup.v1"
    out.update(baseline_scope="cleanup_before_after", traffic_restored=None,
        configuration_check_scope="current_template_vs_named_existing_revision", configuration_restored=None)
    before = snapshot(ctx, run)
    target = pilot._runtime_target(before["serving_revision"])
    if any(target.get(key) != ctx.target.get(key) for key in pilot.IDENTITY_FIELDS):
        raise pilot.ReleaseError("inventory_serving_mismatch")
    candidate = ctx.service + "-p" + hashlib.sha256(run_id.encode()).hexdigest()[:8]
    revision = pilot._json(run(["gcloud", "run", "revisions", "describe", candidate,
        f"--project={ctx.project}", f"--region={ctx.region}", "--format=json"], stage="cleanup_revision_unavailable"))
    base, container = identity.immutable_image(revision)
    code_hash = hashlib.sha256(Path(__file__).with_name("pilot_cloud_tcp_server.py").read_bytes()).hexdigest()
    args = container.get("args", [])
    annotations = revision.get("metadata", {}).get("annotations", {})
    if (revision.get("metadata", {}).get("name") != candidate or not base.endswith("@" + BASE)
            or revision.get("metadata", {}).get("deletionTimestamp")
            or container.get("command") != ["/usr/local/bin/python"] or len(args) != 4
            or args[:3] != ["-I", "-S", "-c"] or hashlib.sha256(args[3].encode()).hexdigest() != code_hash
            or {row.get("name") for row in container.get("env", [])} != {"PILOT_TCP_HOST", "PILOT_TCP_PORT"}
            or annotations.get("autoscaling.knative.dev/minScale") not in (None, "0")
            or annotations.get("autoscaling.knative.dev/maxScale") != "1"):
        raise pilot.ReleaseError("cleanup_ownership_unverified")
    status = before["service"].get("status", {})
    template = before["service"]["spec"]["template"]
    if (any(row["revision"] == candidate for row in before["traffic"])
            or template.get("metadata", {}).get("name") == candidate
            or candidate in (status.get("latestCreatedRevisionName"), status.get("latestReadyRevisionName"))):
        raise pilot.ReleaseError("cleanup_revision_still_referenced")
    out.update(binding_verified=True, probe_code_sha256=code_hash, latest_revision_released=True,
        source_stable=True, stage="owned_unused_revision_verified")
    # Inspect restoration separately; a comparison failure must not retain an
    # otherwise proven unused probe. No original financial configuration is emitted.
    name = template.get("metadata", {}).get("name") or status.get("latestCreatedRevisionName")
    if isinstance(name, str) and pilot.NAME.fullmatch(name):
        try:
            original = pilot._json(run(["gcloud", "run", "revisions", "describe", name,
                f"--project={ctx.project}", f"--region={ctx.region}", "--format=json"], stage="restored_revision_unavailable"))
            actual, expected = pilot._configuration(template), pilot._configuration(original)
            ac, ec = actual["spec"]["containers"][0], expected["spec"]["containers"][0]
            out["restored_template_checks"] = dict(metadata_equal=actual["metadata"] == expected["metadata"],
                spec_equal=actual["spec"] == expected["spec"], env_equal=ac.get("env") == ec.get("env"),
                image_equal=ac.get("image") == ec.get("image"))
            out["configuration_restored"] = actual == expected
        except Exception:
            out["restored_template_checks"] = None
    token = run(["gcloud", "auth", "print-access-token", "--quiet"], stage="original_identity_unavailable").strip()
    url = (f"https://{ctx.region}-run.googleapis.com/apis/serving.knative.dev/v1/namespaces/{ctx.project}"
        + "/revisions/" + candidate)
    # Recheck the direct deletion conditions after reads, before the one mutation.
    fresh = snapshot(ctx, run)
    pilot.unchanged(before, fresh)
    if (pilot._configuration(before["service"]["spec"]["template"]) != pilot._configuration(fresh["service"]["spec"]["template"])
            or fresh["service"]["spec"]["template"].get("metadata", {}).get("name") == candidate
            or candidate in (fresh["service"].get("status", {}).get("latestCreatedRevisionName"),
                fresh["service"].get("status", {}).get("latestReadyRevisionName"))):
        raise pilot.ReleaseError("cleanup_revision_still_referenced")
    out["revision_delete_attempted"] = True
    api(url, method="DELETE", token=token, stage="revision_delete_unknown")
    try:
        api(url, method="GET", token=token, stage="revision_delete_readback")
    except CloudError as error:
        if error.http_status != 404:
            raise
        out["revision_deleted"] = True
    if not out["revision_deleted"]:
        raise pilot.ReleaseError("revision_delete_unverified")
    after = snapshot(ctx, run)
    pilot.unchanged(before, after)
    out.update(status="observed", stage="owned_revision_deleted", baseline_unchanged=True,
        traffic_unchanged=True)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    out = result()
    try:
        ctx = pilot.context(os.environ)
        cleanup_id = os.environ.get("PILOT_CLEANUP_RUN_ID", "")
        if cleanup_id:
            if not cleanup_id.isdigit() or int(cleanup_id) <= 0 or cleanup_id == os.environ["GITHUB_RUN_ID"]:
                raise pilot.ReleaseError("cleanup_run_id_invalid")
            out = cleanup_existing(ctx, cleanup_id, progress=out)
        else:
            out = execute(ctx, os.environ["GITHUB_RUN_ID"], progress=out)
    except CloudError as error:
        out.update(stage=str(error), cloud_http_status=error.http_status)
    except pilot.ReleaseError as error:
        # All ReleaseError strings originate in trusted helpers as fixed codes.
        out["stage"] = str(error)
    except Exception:
        out["stage"] = "preflight_unavailable"
    args.receipt.write_text(json.dumps(out, sort_keys=True) + "\n")
    args.receipt.chmod(0o600)
    print(json.dumps(out, sort_keys=True))
    return 0 if out["status"] == "observed" and out["revision_deleted"] and out["baseline_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
