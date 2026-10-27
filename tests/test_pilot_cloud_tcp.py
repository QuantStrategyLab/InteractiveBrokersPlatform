"""Synthetic control-plane/HTTP/socket results only; no external IO."""
import ast
import copy
from dataclasses import replace
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest

from runtime_config_support import AccountGroupConfig
from scripts import deploy_pilot_no_traffic as pilot
from scripts import pilot_cloud_tcp_server as server
from scripts import run_pilot_cloud_tcp as cloud
from tests.test_pilot_no_traffic_release import baseline, environment, SERVING


@pytest.fixture
def lifecycle(monkeypatch):
    ctx = pilot.context(environment())
    before = baseline()
    before["service"]["spec"]["traffic"] = [{"revisionName": SERVING, "percent": 100}]
    before["service"]["metadata"].update(namespace="synthetic-project", resourceVersion="1")
    image = "us-central1-docker.pkg.dev/synthetic-project/runtime/app@" + cloud.BASE
    for node in (before["service"]["spec"]["template"], before["serving_revision"]):
        node["spec"]["containers"][0]["image"] = image
        node["metadata"]["annotations"]["run.googleapis.com/network-interfaces"] = '[{"network":"synthetic-network"}]'
    group = AccountGroupConfig(execution_backend="gateway", ib_gateway_instance_name="192.0.2.1",
        ib_gateway_mode="live", ib_client_id=7, ib_gateway_port=4001, account_ids=tuple(ctx.target["account_selector"]))
    state = SimpleNamespace(current=copy.deepcopy(before), before=before, ctx=ctx, group=group,
        calls=[], fail=None, drift=False, log_reads=0, progress=cloud.result(),
        ready_pointer_stays_serving=False, controller_revision_change=False)
    monkeypatch.setattr(cloud.serving, "_read", lambda *a, **k: {"group_declarations": []})
    monkeypatch.setattr(cloud.gateway, "read_group", lambda *a, **k: state.group)

    def snapshot(*args):
        return copy.deepcopy(state.current)

    def run(args, **kwargs):
        if args[:4] == ["gcloud", "run", "revisions", "describe"]:
            revision = copy.deepcopy(state.current["service"]["spec"]["template"])
            revision["status"] = {"conditions": [{"type": "Ready", "status": "True"}]}
            return json.dumps(revision)
        if args[:3] == ["gcloud", "logging", "read"]:
            state.log_reads += 1
            if state.fail == "measurement_log_unavailable":
                raise pilot.ReleaseError("measurement_log_unavailable")
            if state.drift:
                state.current["iam"]["bindings"] = []
            return json.dumps([{"jsonPayload": dict(schema_version="ibkr.cloud_tcp_probe.v1",
                tcp_connected=True, failure_class=None, broker_called=False,
                broker_protocol_sent=False, strategy_imported=False)}])
        assert args[:2] == ["gcloud", "auth"]
        return "private-synthetic-token"

    def api(url, *, method, token, body=None, stage=None):
        state.calls.append((method, stage))
        assert token == "private-synthetic-token"
        if state.fail == stage:
            raise cloud.CloudError(stage, 403)
        if "dryRun=all" in url:
            return copy.deepcopy(body)
        if method == "PUT":
            if body["spec"]["template"]["metadata"]["name"] == SERVING:
                previous_latest = state.current["service"]["status"]["latestCreatedRevisionName"]
                state.current = copy.deepcopy(before)
                state.current["service"]["metadata"]["resourceVersion"] = "3"
                if state.fail == "latest_pointer_retained":
                    state.current["service"]["status"]["latestCreatedRevisionName"] = previous_latest
                if state.controller_revision_change:
                    state.current["serving_revision"]["metadata"]["resourceVersion"] = "controller-change"
            else:
                service = copy.deepcopy(body)
                candidate = body["spec"]["template"]["metadata"]["name"]
                service["metadata"]["resourceVersion"] = "2"
                service["status"] = {**copy.deepcopy(before["service"]["status"]),
                    "latestCreatedRevisionName": candidate,
                    "latestReadyRevisionName": SERVING if state.ready_pointer_stays_serving else candidate,
                    "traffic": [dict(row, **({"url": "https://" + row["tag"] + "---synthetic.run.app"} if row.get("tag") else {}))
                        for row in body["spec"]["traffic"]]}
                state.current["service"] = service
                state.current["traffic"], _ = pilot._traffic(service)
                if state.fail == "deployment_response_lost":
                    raise cloud.CloudError("deployment_response_unknown")
            return {}
        if method == "GET":
            raise cloud.CloudError(stage, 404)
        assert method == "DELETE"
        return {}

    state.execute = lambda: cloud.execute(ctx, "123", run=run, snapshot=snapshot, api=api,
        pause=lambda _: None, progress=state.progress)
    return state


def test_network_observation_requires_exact_restore_and_delete_readbacks(lifecycle):
    out = lifecycle.execute()
    assert out["status"] == "observed" and out["tcp_connected"]
    assert out["configuration_restored"] and out["traffic_restored"] and out["revision_deleted"]
    assert out["baseline_unchanged"] and out["restore_dry_run_verified"]
    assert not out["broker_called"] and not out["strategy_imported"] and not out["runtime_ready"]
    text = json.dumps(out)
    for private in ("192.0.2.1", "private-synthetic-token", "12345.67", "synthetic-project"):
        assert private not in text
    assert [stage for method, stage in lifecycle.calls if method == "PUT"] == [
        "restore_dry_run_failed", "deployment_dry_run_failed", "deployment_response_unknown", "restore_response_unknown"]


def test_unused_template_can_differ_and_is_restored_without_becoming_probe_input(lifecycle):
    for state in (lifecycle.before, lifecycle.current):
        state["service"]["spec"]["template"]["spec"]["containers"][0]["env"][1]["value"] = "synthetic-pending-change"
    out = lifecycle.execute()
    assert out["baseline_unchanged"] and out["configuration_restored"] and out["revision_deleted"]
    assert lifecycle.current["service"]["spec"]["template"]["spec"]["containers"][0]["env"][1]["value"] == "synthetic-pending-change"


def test_ready_exact_revision_is_usable_even_when_service_latest_ready_stays_old(lifecycle):
    lifecycle.ready_pointer_stays_serving = True
    out = lifecycle.execute()
    assert out["candidate_ready"]
    assert out["measurement_read"] and out["revision_deleted"]


def test_probe_keeps_approved_digest_and_restores_newer_normal_deployment(lifecycle):
    newer = "us-central1-docker.pkg.dev/synthetic-project/runtime/app@sha256:" + "f" * 64
    for state in (lifecycle.before, lifecycle.current):
        for node in (state["serving_revision"], state["service"]["spec"]["template"]):
            node["spec"]["containers"][0]["image"] = newer
    body, _, _ = cloud.prepared(lifecycle.ctx, lifecycle.before, lifecycle.group, "123")
    template = body["spec"]["template"]
    assert template["spec"]["containers"][0]["image"].endswith("@" + cloud.BASE)
    assert not (set(template["metadata"]["labels"]) & pilot.BUILD_LABELS)
    out = lifecycle.execute()
    assert out["baseline_unchanged"] and out["revision_deleted"]
    assert lifecycle.current["service"]["spec"]["template"]["spec"]["containers"][0]["image"] == newer


def test_controller_revision_version_is_diagnostic_not_runtime_configuration(lifecycle):
    lifecycle.controller_revision_change = True
    out = lifecycle.execute()
    assert out["baseline_unchanged"] and out["configuration_restored"] and out["revision_deleted"]
    assert all(out["restoration_checks"].values())


def test_generated_url_annotation_does_not_hide_ingress_or_financial_configuration_changes(lifecycle):
    current = copy.deepcopy(lifecycle.before)
    current["service"]["metadata"]["annotations"]["run.googleapis.com/urls"] = '["https://synthetic-tag.run.app"]'
    assert all(cloud.baseline_checks(lifecycle.before, current).values())
    current["service"]["metadata"]["annotations"]["run.googleapis.com/ingress"] = "all"
    assert not cloud.baseline_checks(lifecycle.before, current)["global_configuration_equal"]
    current = copy.deepcopy(lifecycle.before)
    current["serving_revision"]["spec"]["containers"][0]["env"][1]["value"] = "changed-private-field"
    assert not cloud.baseline_checks(lifecycle.before, current)["serving_configuration_equal"]


@pytest.mark.parametrize("stage", ["restore_dry_run_failed", "deployment_dry_run_failed"])
def test_dry_run_rejection_never_deploys_or_invokes(lifecycle, stage):
    lifecycle.fail = stage
    with pytest.raises(cloud.CloudError, match=stage):
        lifecycle.execute()
    assert all("dry_run" in stage for method, stage in lifecycle.calls if method == "PUT")
    assert lifecycle.current == lifecycle.before


def test_measurement_read_failure_still_restores_and_deletes_without_iam_change(lifecycle):
    lifecycle.fail = "measurement_log_unavailable"
    out = lifecycle.execute()
    assert out["stage"] == "measurement_log_unavailable" and not out["measurement_read"]
    assert out["baseline_unchanged"] and out["revision_deleted"] and not out["iam_changed"]
    assert lifecycle.log_reads == 1


def test_unknown_deployment_is_not_retried_and_only_exact_owned_template_is_restored(lifecycle):
    lifecycle.fail = "deployment_response_lost"
    out = lifecycle.execute()
    assert out["stage"] == "deployment_response_unknown" and not out["measurement_read"]
    assert out["baseline_unchanged"] and out["revision_deleted"]
    assert sum(stage == "deployment_response_unknown" for _, stage in lifecycle.calls) == 1


def test_external_change_is_never_overwritten_by_cleanup(lifecycle):
    lifecycle.drift = True
    out = lifecycle.execute()
    assert out["cleanup_stage"] == "production_baseline_changed"
    assert not out["restore_attempted"] and not out["revision_delete_attempted"]


def test_restore_failure_stops_before_revision_delete_or_retry(lifecycle):
    lifecycle.fail = "restore_response_unknown"
    out = lifecycle.execute()
    assert out["cleanup_stage"] == "restore_response_unknown"
    assert not out["revision_delete_attempted"] and not out["configuration_restored"]
    assert sum(stage == "restore_response_unknown" for _, stage in lifecycle.calls) == 1


def test_restored_template_is_not_enough_to_delete_latest_revision(lifecycle):
    lifecycle.fail = "latest_pointer_retained"
    out = lifecycle.execute()
    assert out["configuration_restored"] and out["traffic_restored"] and out["baseline_unchanged"]
    assert out["cleanup_stage"] == "temporary_revision_still_latest"
    assert not out["latest_revision_released"] and not out["revision_delete_attempted"]


def test_failed_restore_comparison_can_clean_resource_without_claiming_restoration(lifecycle, monkeypatch):
    check = cloud.protected_baseline
    def compare(before, current):
        if current["service"]["metadata"]["resourceVersion"] == "3":
            raise pilot.ReleaseError("production_baseline_changed")
        check(before, current)
    def cleanup(*args, progress, **kwargs):
        progress.update(revision_delete_attempted=True, revision_deleted=True, baseline_unchanged=True)
    monkeypatch.setattr(cloud, "protected_baseline", compare)
    monkeypatch.setattr(cloud, "cleanup_existing", cleanup)
    out = lifecycle.execute()
    assert out["cleanup_stage"] == "restore_readback_mismatch" and out["status"] == "unavailable"
    assert not out["configuration_restored"] and not out["baseline_unchanged"]
    assert out["revision_deleted"] and out["resource_cleanup_baseline_unchanged"]


def test_unknown_delete_is_never_retried_by_resource_cleanup(lifecycle, monkeypatch):
    lifecycle.fail = "revision_delete_unknown"
    def cleanup(*args, **kwargs):
        pytest.fail("an attempted delete must not be retried")
    monkeypatch.setattr(cloud, "cleanup_existing", cleanup)
    out = lifecycle.execute()
    assert out["revision_delete_attempted"] and not out["revision_deleted"]
    assert out["cleanup_stage"] == "revision_delete_unknown"
    assert sum(method == "DELETE" for method, _ in lifecycle.calls) == 1


@pytest.mark.parametrize("changes", [dict(ib_client_id=0), dict(account_ids=("U2",)),
    dict(ib_gateway_zone="us-central1-a"), dict(ib_gateway_port=0)])
def test_invalid_binding_stops_before_cloud_mutation(lifecycle, changes):
    lifecycle.group = replace(lifecycle.group, **changes)
    with pytest.raises(pilot.ReleaseError, match="direct_binding_unverified"):
        lifecycle.execute()
    assert not any(method in ("PUT", "DELETE") for method, _ in lifecycle.calls)


def test_probe_template_has_no_account_environment_or_application_entrypoint(lifecycle):
    body, _, _ = cloud.prepared(lifecycle.ctx, lifecycle.before, lifecycle.group, "123")
    template = body["spec"]["template"]
    assert template["metadata"]["annotations"]["autoscaling.knative.dev/minScale"] == "0"
    assert template["metadata"]["annotations"]["autoscaling.knative.dev/maxScale"] == "1"
    assert template["metadata"]["annotations"]["run.googleapis.com/health-check-disabled"] == "false"
    assert template["spec"]["serviceAccountName"] == "synthetic-runtime"
    container = template["spec"]["containers"][0]
    assert container["command"] == ["/usr/local/bin/python"]
    assert container["resources"] == lifecycle.before["serving_revision"]["spec"]["containers"][0]["resources"]
    assert container["args"][:3] == ["-I", "-S", "-c"]
    assert {row["name"] for row in container["env"]} == {"PILOT_TCP_HOST", "PILOT_TCP_PORT"}
    assert "PRIVATE_AMOUNT" not in json.dumps(body["spec"]["template"])
    assert all(not row.get("latestRevision") for row in body["spec"]["traffic"])


def test_probe_explicitly_enables_deployment_startup_check_without_changing_original(lifecycle):
    source = lifecycle.before["serving_revision"]["metadata"]["annotations"]
    source["run.googleapis.com/health-check-disabled"] = "true"
    body, _, _ = cloud.prepared(lifecycle.ctx, lifecycle.before, lifecycle.group, "123")
    assert body["spec"]["template"]["metadata"]["annotations"]["run.googleapis.com/health-check-disabled"] == "false"
    assert source["run.googleapis.com/health-check-disabled"] == "true"


def test_dynamic_latest_traffic_is_rejected_before_deployment(lifecycle):
    lifecycle.current["service"]["spec"]["traffic"] = [{"latestRevision": True, "percent": 100}]
    with pytest.raises(pilot.ReleaseError, match="latest_traffic_restore_unverified"):
        lifecycle.execute()
    assert not any(method in ("PUT", "DELETE") for method, _ in lifecycle.calls)


def test_probe_contains_stdlib_imports_and_no_application_or_file_writes():
    tree = ast.parse(Path(server.__file__).read_text())
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imported |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert imported == {"http.server", "json", "os", "socket"}
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open" for node in ast.walk(tree))


@pytest.mark.parametrize("failure,category", [(None, None), (TimeoutError(), "timeout"),
    (ConnectionRefusedError(), "connection_refused"), (OSError(), "network_error")])
def test_stdlib_probe_connects_only_first_address_and_closes_without_protocol(monkeypatch, failure, category):
    events = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 4001)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.2", 4001))])
    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            events.append("closed")
        def settimeout(self, value):
            assert value == 5
        def connect(self, address):
            events.append(address)
            if failure:
                raise failure
    monkeypatch.setattr(socket, "socket", lambda *a: Connection())
    assert server.probe("synthetic.example.invalid", 4001) == (failure is None, category)
    assert events == [("192.0.2.1", 4001), "closed"]


def test_existing_release_workflow_keeps_default_mode_and_original_identity_lock():
    root = Path(__file__).resolve().parents[1]
    wrapper = (root / ".github/workflows/deploy-pilot-no-traffic.yml").read_text()
    callee = (root / ".github/workflows/deploy-pilot-no-traffic-protected.yml").read_text()
    assert "isolated_network_preflight:" in wrapper and "default: false" in wrapper
    assert "group: Deploy Cloud Run-${{ github.ref_name }}" in wrapper
    assert "if: ${{ !inputs.isolated_network_preflight }}" in callee
    assert "if: inputs.isolated_network_preflight" in callee
    assert "inputs.isolated_network_preflight && 15 || 25" in callee
    assert "--preflight --bootstrap-identity" in callee
    assert "service_account: ${{ secrets.GCP_WORKLOAD_IDENTITY_SERVICE_ACCOUNT }}" in callee


def cleanup_case(lifecycle, *, changed_code=False, referenced=False, original_read_failure=False):
    body, name, _ = cloud.prepared(lifecycle.ctx, lifecycle.before, lifecycle.group, "123")
    owned = copy.deepcopy(body["spec"]["template"])
    if changed_code:
        owned["spec"]["containers"][0]["args"][-1] += "\n# changed"
    current = copy.deepcopy(lifecycle.before)
    current["service"]["spec"]["template"]["metadata"]["name"] = SERVING
    if referenced:
        current["service"]["status"]["latestCreatedRevisionName"] = name
    calls = []
    def run(args, **kwargs):
        if args[:4] == ["gcloud", "run", "revisions", "describe"]:
            if args[4] == name:
                return json.dumps(owned)
            if original_read_failure:
                raise pilot.ReleaseError("restored_revision_unavailable")
            return json.dumps(lifecycle.before["serving_revision"])
        assert args[:2] == ["gcloud", "auth"]
        return "private-synthetic-token"
    def api(url, *, method, **kwargs):
        calls.append(method)
        if method == "GET":
            raise cloud.CloudError("revision_delete_readback", 404)
        assert method == "DELETE" and url.endswith("/" + name)
        return {}
    return lambda: cloud.cleanup_existing(lifecycle.ctx, "123", run=run,
        snapshot=lambda *a: copy.deepcopy(current), api=api), calls


def test_cleanup_only_deletes_owned_unused_probe_without_new_deployment(lifecycle):
    execute, calls = cleanup_case(lifecycle)
    out = execute()
    assert calls == ["DELETE", "GET"]
    assert out["revision_deleted"] and out["baseline_unchanged"] and out["traffic_unchanged"]
    assert not out["deployment_attempted"] and not out["restore_attempted"]
    assert out["configuration_restored"] and out["baseline_scope"] == "cleanup_before_after"


@pytest.mark.parametrize("changes,reason", [(dict(changed_code=True), "cleanup_ownership_unverified"),
    (dict(referenced=True), "cleanup_revision_still_referenced")])
def test_cleanup_rejects_different_code_or_still_referenced_revision(lifecycle, changes, reason):
    execute, calls = cleanup_case(lifecycle, **changes)
    with pytest.raises(pilot.ReleaseError, match=reason):
        execute()
    assert not calls


def test_unavailable_restore_diagnostic_does_not_retain_proven_unused_resource(lifecycle):
    execute, calls = cleanup_case(lifecycle, original_read_failure=True)
    out = execute()
    assert calls == ["DELETE", "GET"] and out["revision_deleted"]
    assert out["configuration_restored"] is None and out["restored_template_checks"] is None
