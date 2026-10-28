"""Synthetic cloud responses only; never read configuration or open a network."""
import copy
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest

from runtime_config_support import AccountGroupConfig
from scripts import deploy_pilot_no_traffic as pilot
from scripts import pilot_cloud_tcp_job as entry
from scripts import run_pilot_cloud_tcp as cloud
from scripts import run_pilot_job_tcp as job
from tests.test_pilot_no_traffic_release import baseline, environment


@pytest.fixture
def lifecycle(monkeypatch):
    ctx = pilot.context(environment())
    before = baseline()
    revision = before["serving_revision"]
    revision["spec"]["serviceAccountName"] = "synthetic-runtime@synthetic-project.iam.gserviceaccount.com"
    revision["spec"]["containers"][0]["resources"] = {"limits": {"cpu": "1", "memory": "512Mi"}}
    for node in (revision, before["service"]["spec"]["template"]):
        node["spec"]["containers"][0]["image"] = (
            "us-central1-docker.pkg.dev/synthetic-project/runtime/app@" + cloud.BASE)
    revision["metadata"]["annotations"]["run.googleapis.com/network-interfaces"] = '[{"network":"synthetic-network"}]'
    group = AccountGroupConfig(execution_backend="gateway", ib_gateway_instance_name="192.0.2.1",
        ib_gateway_mode="live", ib_client_id=7, ib_gateway_port=4001, account_ids=tuple(ctx.target["account_selector"]))
    state = SimpleNamespace(ctx=ctx, before=before, current=copy.deepcopy(before), group=group,
        actual=None, execution=None, calls=[], fail=None, progress=job.result(), logs=None, drift=False, prior=False)
    monkeypatch.setattr(job.serving, "_read", lambda *a, **k: {"group_declarations": []})
    monkeypatch.setattr(job.gateway, "read_group", lambda *a, **k: state.group)
    def run(args, **kwargs):
        if args[:2] == ["gcloud", "auth"]:
            return "private-synthetic-token"
        if args[:4] == ["gcloud", "run", "jobs", "list"]:
            return json.dumps([{"metadata": {"name": "ibkr-p4-old"}}] if state.prior else [])
        if args[:5] == ["gcloud", "run", "jobs", "executions", "list"]:
            return json.dumps([state.execution] if state.execution else [])
        assert args[:3] == ["gcloud", "logging", "read"]
        assert 'resource.type="cloud_run_job"' in args[3] and 'execution_name' in args[3]
        if state.drift:
            state.current["iam"]["bindings"] = []
        return json.dumps(state.logs if state.logs is not None else [{"jsonPayload": dict(
            schema_version="ibkr.cloud_tcp_probe.v1", tcp_connected=True, failure_class=None,
            broker_called=False, broker_protocol_sent=False, strategy_imported=False)}])
    def api(url, *, method, token, body=None, stage=None):
        state.calls.append((method, stage))
        assert token == "private-synthetic-token"
        if state.fail == stage:
            raise cloud.CloudError(stage, 403)
        if "dryRun=all" in url:
            value = copy.deepcopy(body)
            if state.fail == "bad_dry_run":
                value["spec"]["template"]["spec"]["template"]["spec"]["maxRetries"] = 1
            if state.fail == "defaults":
                task = value["spec"]["template"]["spec"]["template"]["spec"]
                task.pop("maxRetries")
                task["timeoutSeconds"] = 60
                task["containers"][0]["resources"]["limits"]["cpu"] = "1000m"
            if state.fail == "gen2_default":
                value["spec"]["template"]["metadata"]["annotations"]["run.googleapis.com/execution-environment"] = "gen2"
            return value
        if method == "POST" and url.endswith("/jobs"):
            state.actual = copy.deepcopy(body)
            state.actual["metadata"]["uid"] = "synthetic-uid"
            state.actual["status"] = {"conditions": [{"type": "Ready", "status": "True"}]}
            if state.fail == "create_response_lost":
                raise cloud.CloudError("job_create_unknown")
            return state.actual
        if method == "POST" and url.endswith(":run"):
            state.execution = copy.deepcopy(state.actual["spec"]["template"])
            state.execution["metadata"].update(name=state.actual["metadata"]["name"] + "-abc12",
                labels={"run.googleapis.com/job": state.actual["metadata"]["name"]})
            state.execution["status"] = {"completionTime": "2026-10-27T00:00:00Z", "succeededCount": 1}
            if state.fail == "task_failed":
                state.execution["status"].update(succeededCount=0, failedCount=1)
            if state.fail in ("active", "execute_response_lost"):
                state.execution["status"] = {}
            if state.fail == "execute_response_lost":
                raise cloud.CloudError("job_execute_unknown")
            return state.execution
        if method == "POST" and url.endswith(":cancel"):
            state.execution["status"] = {"completionTime": "2026-10-27T00:00:00Z", "cancelledCount": 1}
            return state.execution
        if method == "DELETE":
            if state.fail != "delete_not_visible":
                state.actual = None
            return {}
        assert method == "GET"
        if "/executions/" in url:
            return copy.deepcopy(state.execution)
        if state.actual is None:
            raise cloud.CloudError(stage, 404)
        value = copy.deepcopy(state.actual)
        if state.fail == "changed_uid" and stage == "cleanup_job_read_unavailable":
            value["metadata"]["uid"] = "other-uid"
        return value
    state.execute = lambda **kwargs: job.execute(ctx, "123", run=run, snapshot=lambda *a: copy.deepcopy(state.current),
        api=api, pause=lambda _: None, progress=state.progress, **kwargs)
    return state


def test_one_job_observation_cleanup_and_original_service_unchanged(lifecycle):
    out = lifecycle.execute()
    assert out["status"] == "observed" and out["tcp_connected"]
    assert out["execution_completed"] and out["job_deleted"] and out["baseline_unchanged"]
    assert all(out["production_checks"].values()) and not out["runtime_ready"]
    assert lifecycle.current == lifecycle.before
    assert not any(method == "PUT" for method, _ in lifecycle.calls)
    assert [stage for method, stage in lifecycle.calls if method == "POST"] == [
        "job_dry_run_failed", "job_create_unknown", "job_execute_unknown"]
    for private in ("192.0.2.1", "synthetic-project", "private-synthetic-token", "12345.67"):
        assert private not in json.dumps(out)


def test_prepared_job_keeps_original_identity_network_and_frozen_image(lifecycle):
    value = job.prepared(lifecycle.ctx, lifecycle.before, lifecycle.group, "123")
    template = value["spec"]["template"]
    task = template["spec"]["template"]["spec"]
    assert template["metadata"]["annotations"] == {
        "run.googleapis.com/network-interfaces": '[{"network":"synthetic-network"}]'}
    assert task["serviceAccountName"] == lifecycle.before["serving_revision"]["spec"]["serviceAccountName"]
    assert task["maxRetries"] == 0 and task["timeoutSeconds"] == "60"
    container = task["containers"][0]
    assert container["image"].endswith("@" + cloud.BASE)
    assert {row["name"] for row in container["env"]} == {"PILOT_TCP_HOST", "PILOT_TCP_PORT"}
    assert not ({"ports", "startupProbe", "volumeMounts"} & set(container))
    assert container["command"] == ["/usr/local/bin/python"] and container["args"][:3] == ["-I", "-S", "-c"]


@pytest.mark.parametrize("failure", ["job_dry_run_failed", "bad_dry_run"])
def test_dry_run_rejection_never_creates_a_resource(lifecycle, failure):
    lifecycle.fail = failure
    with pytest.raises(pilot.ReleaseError):
        lifecycle.execute()
    assert not lifecycle.progress["job_create_attempted"]
    assert lifecycle.actual is None


def test_documented_scalar_defaults_do_not_change_the_task(lifecycle):
    lifecycle.fail = "defaults"
    assert lifecycle.execute()["job_deleted"]


def test_mandatory_job_gen2_default_is_accepted_without_changing_production(lifecycle):
    lifecycle.fail = "gen2_default"
    out = lifecycle.execute()
    assert out["job_execution_env_defaulted_gen2"] and out["measurement_read"]
    assert out["job_deleted"] and out["baseline_unchanged"]
    assert lifecycle.current == lifecycle.before


@pytest.mark.parametrize("key,value", [("run.googleapis.com/network-interfaces", '[{"network":"other"}]'),
    ("run.googleapis.com/vpc-access-egress", "all-traffic"),
    ("run.googleapis.com/encryption-key", "unexpected-key"), ("run.googleapis.com/execution-environment", "gen1")])
def test_job_default_normalization_still_rejects_network_security_changes(lifecycle, key, value):
    desired = job.prepared(lifecycle.ctx, lifecycle.before, lifecycle.group, "123")
    actual = copy.deepcopy(desired)
    actual["spec"]["template"]["metadata"]["annotations"][key] = value
    with pytest.raises(pilot.ReleaseError, match="job_network_readback_unverified"):
        job.verify_job(actual, desired)


def test_prior_p4_job_prevents_a_second_job(lifecycle):
    lifecycle.prior = True
    with pytest.raises(pilot.ReleaseError, match="prior_p4_job_remaining"):
        lifecycle.execute()
    assert not lifecycle.calls


@pytest.mark.parametrize("failure", ["create_response_lost", "execute_response_lost", "task_failed", "measurement_log_unavailable"])
def test_failure_or_unknown_write_result_cleans_only_owned_job_without_retry(lifecycle, failure):
    lifecycle.fail = failure
    if failure == "measurement_log_unavailable":
        lifecycle.logs = []
    out = lifecycle.execute()
    assert out["job_deleted"] and out["baseline_unchanged"] and not out["measurement_read"]
    assert sum(stage == "job_create_unknown" for _, stage in lifecycle.calls) == 1
    assert sum(stage == "job_execute_unknown" for _, stage in lifecycle.calls) <= 1
    if failure == "execute_response_lost":
        assert ("POST", "job_cancel_unknown") in lifecycle.calls


@pytest.mark.parametrize("failure", ["changed_uid", "job_delete_unknown", "delete_not_visible"])
def test_cleanup_failure_is_separate_from_a_successful_tcp_observation(lifecycle, failure):
    lifecycle.fail = failure
    out = lifecycle.execute()
    assert out["measurement_read"] and out["tcp_connected"] and not out["job_deleted"]
    assert out["status"] == "unavailable" and out["cleanup_stage"]
    assert sum(stage == "job_delete_unknown" for _, stage in lifecycle.calls) <= 1
    if failure == "changed_uid":
        assert not out["job_delete_attempted"]


def test_production_drift_does_not_retain_an_owned_finished_job_or_claim_baseline(lifecycle):
    lifecycle.drift = True
    out = lifecycle.execute()
    assert out["job_deleted"] and not out["baseline_unchanged"]
    assert not out["production_checks"]["iam_equal"]


def test_duplicate_or_invalid_log_rows_do_not_pass(lifecycle):
    lifecycle.logs = [{"jsonPayload": {}}, {"jsonPayload": {}}]
    out = lifecycle.execute()
    assert out["stage"] == "measurement_not_unique" and not out["measurement_read"] and out["job_deleted"]
    with pytest.raises(pilot.ReleaseError, match="probe_response_invalid"):
        job.measurement([{"jsonPayload": {"schema_version": "ibkr.cloud_tcp_probe.v1"}}])


@pytest.mark.parametrize("failure,category", [(None, None), (TimeoutError(), "timeout"),
    (ConnectionRefusedError(), "connection_refused"), (OSError(), "network_error")])
def test_one_shot_connects_first_address_once_and_always_closes(monkeypatch, failure, category):
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
    assert entry.probe("synthetic.invalid", 4001) == (failure is None, category)
    assert events == [("192.0.2.1", 4001), "closed"]


def test_job_workflow_is_default_off_mutually_exclusive_and_uses_original_lock():
    root = Path(__file__).resolve().parents[1]
    wrapper = (root / ".github/workflows/deploy-pilot-no-traffic.yml").read_text()
    callee = (root / ".github/workflows/deploy-pilot-no-traffic-protected.yml").read_text()
    assert "isolated_job_network_preflight:" in wrapper and "default: false" in wrapper
    assert "group: Deploy Cloud Run-${{ github.ref_name }}" in wrapper
    assert "job_mode_invalid=true" in callee and "PILOT_JOB_NETWORK_PREFLIGHT" in callee
    assert "if: inputs.isolated_job_network_preflight" in callee
    assert "service_account: ${{ secrets.GCP_WORKLOAD_IDENTITY_SERVICE_ACCOUNT }}" in callee


@pytest.mark.parametrize("passed", [True, False])
def test_application_phase_reads_safe_failure_or_success_then_deletes_job(lifecycle, monkeypatch, passed):
    monkeypatch.setattr(job.app_isolation, "prepared", lambda ctx, before, group, desired, source: desired)
    value = dict(schema_version="ibkr.application_isolation.v1", stage="isolation_observed" if passed else "application_import",
        isolation_passed=passed, candidate_source_verified=True, dependencies_verified=True, application_imported=passed,
        binding_verified=passed, outputs_isolated=passed, report_saved_readback=passed, sdk_guards_installed=passed,
        notification_guards_installed=passed, external_effect_attempts=0 if passed else None,
        broker_called=False, strategy_computed=False, production_state_read=False)
    lifecycle.logs = [{"jsonPayload": value}]
    if not passed:
        lifecycle.fail = "task_failed"
    out = lifecycle.execute(application_source=Path("source"))
    assert out["measurement_read"] and out["application_isolation"] == value
    assert out["status"] == ("observed" if passed else "unavailable")
    assert out["job_deleted"] and out["baseline_unchanged"] and out["tcp_connected"] is None


def test_closed_market_cycle_remains_unavailable_after_successful_cleanup(lifecycle, monkeypatch):
    from scripts import pilot_cycle_entry
    monkeypatch.setattr(job.app_isolation, "prepared", lambda *args, **kwargs: args[3])
    app = dict(schema_version="ibkr.application_isolation.v1", stage="isolation_observed", isolation_passed=True,
        candidate_source_verified=True, dependencies_verified=True, application_imported=True, binding_verified=True,
        outputs_isolated=True, report_saved_readback=True, sdk_guards_installed=True, notification_guards_installed=True,
        external_effect_attempts=0, broker_called=False, strategy_computed=False, production_state_read=False)
    cycle = pilot_cycle_entry.result()
    cycle.update(status="waiting_market", stage="market_closed", market_open=False)
    lifecycle.logs = [{"jsonPayload": dict(schema_version="ibkr.guarded_cycle.v1", isolation=app, cycle=cycle)}]
    lifecycle.fail = "task_failed"
    out = lifecycle.execute(application_source=Path("source"), application_phase="cycle")
    assert out["status"] == "unavailable" and out["stage"] == "market_closed"
    assert out["job_deleted"] and out["baseline_unchanged"] and out["measurement_read"]
    assert not out["guarded_cycle"]["cycle"]["cycle_completed"] and not out["broker_called"]


def test_template_diagnostic_stops_after_dry_run_even_when_template_matches(lifecycle, monkeypatch):
    monkeypatch.setattr(job.app_isolation, "prepared", lambda ctx, before, group, desired, source, phase: desired)
    out = lifecycle.execute(diagnostic_only=True, application_source=Path("synthetic"), application_phase="cycle")
    assert out["stage"] == "template_diagnostic" and out["template_match"] is True
    assert [stage for _, stage in lifecycle.calls] == ["job_dry_run_failed"]
    assert not out["job_create_attempted"] and not out["job_execute_attempted"]


def test_template_diagnostic_reports_only_fixed_categories_and_never_creates(lifecycle, monkeypatch):
    monkeypatch.setattr(job.app_isolation, "prepared", lambda ctx, before, group, desired, source, phase: desired)
    lifecycle.fail = "bad_dry_run"
    out = lifecycle.execute(diagnostic_only=True, application_source=Path("synthetic"), application_phase="cycle")
    assert out["template_match"] is False and out["template_reason"] == "job_template_readback_unverified"
    assert out["template_difference_categories"] == ["retries"]
    assert [stage for _, stage in lifecycle.calls] == ["job_dry_run_failed"]
    assert not out["job_create_attempted"] and not out["job_execute_attempted"]
    assert "private-synthetic-token" not in json.dumps(out)


def test_template_difference_never_exposes_env_names_or_values(lifecycle):
    desired = job.prepared(lifecycle.ctx, lifecycle.before, lifecycle.group, "123")
    actual = copy.deepcopy(desired)
    d = desired["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["env"]
    a = actual["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["env"]
    d.append({"name": "LIFECYCLE_PERFORMANCE_BUCKET", "value": ""})
    a.append({"name": "LIFECYCLE_PERFORMANCE_BUCKET"})
    a.append({"name": "PRIVATE_NAME", "value": "PRIVATE_VALUE"})
    result = job.template_difference_categories(actual, desired)
    assert result == ["container_env", "lifecycle_empty_value_omitted"]
    assert "PRIVATE" not in json.dumps(result)
    actual["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["PRIVATE_FIELD"] = "PRIVATE_VALUE"
    result = job.template_difference_categories(actual, desired)
    assert result == ["container_env", "lifecycle_empty_value_omitted", "other_container"]
    assert "PRIVATE" not in json.dumps(result)


def test_diagnostic_cli_rejects_non_cycle_and_invalid_flags_before_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(job.pilot, "context", lambda env: object())
    monkeypatch.setenv("PILOT_JOB_NETWORK_PREFLIGHT", "true")
    monkeypatch.setenv("PILOT_JOB_PHASE", "tcp")
    monkeypatch.setenv("PILOT_TEMPLATE_DIAGNOSTIC", "true")
    monkeypatch.setattr(job, "execute", lambda *a, **k: pytest.fail("must not execute"))
    receipt = tmp_path / "receipt.json"
    assert job.main(["--receipt", str(receipt)]) == 1
    assert json.loads(receipt.read_text())["stage"] == "template_diagnostic_mode_invalid"
