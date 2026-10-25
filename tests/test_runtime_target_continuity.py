import copy
import json

import pytest

from quant_platform_kit.common.live_continuity import runtime_target_fingerprint
from scripts.runtime_target_continuity import verify_target_continuity, verify_traffic_revision


def frozen_target():
    target = {
        "service_name": "unit-service", "strategy_profile": "tqqq_growth_income",
        "execution_mode": "live", "dry_run_only": False,
        "strategy_release": {"strategy_revision": "a" * 40},
        "runtime_risk_limits": {"binding": {"ues_revision": "a" * 40}},
    }
    target["live_continuity"] = {
        "state": "ACTIVE_LKG", "baseline_kind": "legacy_authorized",
        "baseline_id": "synthetic-baseline", "captured_at": "2026-10-01",
        "baseline_target_sha256": runtime_target_fingerprint(target),
    }
    return target


def candidate():
    return {
        "metadata": {"name": "unit-service-00001", "labels": {"commit-sha": "a" * 40}},
        "spec": {"containers": [{
            "startupProbe": {"httpGet": {"path": "/health", "port": 8080}},
            "env": [{"name": "RUNTIME_TARGET_JSON", "value": json.dumps(frozen_target())}],
        }]},
        "status": {"conditions": [
            {"type": "Ready", "status": "True"},
            {"type": "ContainerHealthy", "status": "True"},
        ]},
    }


@pytest.mark.parametrize("path", [
    ("strategy_release", "strategy_revision"), ("runtime_risk_limits", "binding", "ues_revision"),
])
def test_changed_version_reference_rejects_without_rewriting_baseline(path):
    target = frozen_target()
    node = target
    for name in path[:-1]:
        node = node[name]
    node[path[-1]] = "b" * 40
    before = copy.deepcopy(target)
    with pytest.raises(ValueError, match="continuity"):
        verify_target_continuity(target)
    assert target == before

@pytest.mark.parametrize("failure", ["tcp", "stale_fingerprint"])
def test_reconciler_refuses_candidate_before_updating_traffic(monkeypatch, failure):
    from scripts import reconcile_cloud_runtime as reconciler

    revision = candidate()
    if failure == "tcp":
        revision["spec"]["containers"][0]["startupProbe"] = {"tcpSocket": {"port": 8080}}
    else:
        target = frozen_target()
        target["strategy_release"]["strategy_revision"] = "b" * 40
        revision["spec"]["containers"][0]["env"][0]["value"] = json.dumps(target)
    calls = []

    def fake_cloud(args, **kwargs):
        calls.append(args)
        if "update-traffic" in args:
            pytest.fail("unverified candidate reached traffic mutation")
        if "describe" in args and "revisions" in args:
            return revision
        if "describe" in args and "services" in args:
            return {"status": {"traffic": [{"revisionName": "unit-service-old", "percent": 100}]}}
        if "list" in args and "revisions" in args:
            return [revision]
        pytest.fail("unexpected cloud command")

    monkeypatch.setattr(reconciler, "_run", fake_cloud)
    with pytest.raises(ValueError):
        reconciler.ensure_latest_traffic(
            project="unit-project", region="unit-region",
            targets=[reconciler.RuntimeTarget(service_name="unit-service")],
            expected_commit="a" * 40, dry_run=False,
        )
    assert any("revisions" in cmd and "describe" in cmd for cmd in calls)
    assert not any("update-traffic" in cmd for cmd in calls)



def test_continuity_uses_shared_canonical_hash_and_allows_safe_state_change():
    target = frozen_target()
    target["live_continuity"]["state"] = "PAUSED"
    assert verify_target_continuity(target) == runtime_target_fingerprint(target)


def test_target_without_continuity_keeps_runtime_contract():
    target = {"execution_mode": "paper"}
    assert verify_target_continuity(target) == runtime_target_fingerprint(target)
    assert "live_continuity" not in target


def test_healthy_http_candidate_passes():
    verify_traffic_revision(candidate(), expected_revision="unit-service-00001", expected_commit="a" * 40)


@pytest.mark.parametrize("failure", ["tcp", "path", "port", "unhealthy", "not_ready", "commit", "name", "stale_target", "missing_target"])
def test_traffic_gate_rejects_unbootable_or_unverified_revision(failure):
    revision = candidate()
    container = revision["spec"]["containers"][0]
    if failure == "tcp":
        container["startupProbe"] = {"tcpSocket": {"port": 8080}}
    elif failure in {"path", "port"}:
        container["startupProbe"]["httpGet"][failure] = "/run" if failure == "path" else 9090
    elif failure in {"unhealthy", "not_ready"}:
        revision["status"]["conditions"][1 if failure == "unhealthy" else 0]["status"] = "False"
    elif failure == "commit":
        revision["metadata"]["labels"]["commit-sha"] = "b" * 40
    elif failure == "name":
        revision["metadata"]["name"] = "different-revision"
    elif failure == "missing_target":
        container["env"] = []
    else:
        target = frozen_target()
        target["strategy_release"]["strategy_revision"] = "b" * 40
        container["env"][0]["value"] = json.dumps(target)
    with pytest.raises(ValueError):
        verify_traffic_revision(revision, expected_revision="unit-service-00001", expected_commit="a" * 40)


def test_sync_plan_rejects_stale_baseline_before_serializing_target():
    from scripts import build_cloud_run_env_sync_plan as planner

    target = frozen_target()
    target["strategy_release"]["strategy_revision"] = "b" * 40
    before = copy.deepcopy(target)
    with pytest.raises(ValueError, match="continuity"):
        planner._build_target_plan(
            target={"service_name": "unit-service", "runtime_target": target},
            defaults={}, env={}, per_service_mode=True, platform_config={},
            status_rows={"tqqq_growth_income": {"eligible": True, "enabled": True}},
        )
    assert target == before


def selection_plan(monkeypatch, *, selected="unit-service", stale_selected=False):
    from scripts import build_cloud_run_env_sync_plan as planner

    good = frozen_target()
    stale = frozen_target()
    stale["strategy_release"]["strategy_revision"] = "b" * 40
    entries = [
        {"service_name": "unit-service", "runtime_target": stale if stale_selected else good},
        {"service_name": "sibling-service", "runtime_target": stale},
    ]
    monkeypatch.setattr(planner, "_load_platform_config", lambda env: {})
    monkeypatch.setattr(planner, "get_platform_profile_status_matrix", lambda: [])

    def validate_target(**kwargs):
        entry = kwargs["target"]
        verify_target_continuity(entry["runtime_target"])
        return {"service_name": entry["service_name"]}

    monkeypatch.setattr(planner, "_build_target_plan", validate_target)
    env = {"WORKFLOW_TARGET": "configured", "CLOUD_RUN_SERVICE_TARGETS_JSON": json.dumps(entries)}
    if selected is not None:
        env["INPUT_CONFIGURED_SERVICE"] = selected
    return planner, env


def test_single_service_plan_does_not_admit_or_validate_unselected_sibling(monkeypatch):
    planner, env = selection_plan(monkeypatch)
    assert planner.build_sync_plan(env)["targets"] == [{"service_name": "unit-service"}]


def test_single_service_plan_keeps_selected_continuity_validation(monkeypatch):
    planner, env = selection_plan(monkeypatch, stale_selected=True)
    with pytest.raises(ValueError, match="continuity"):
        planner.build_sync_plan(env)


def test_unselected_plan_still_validates_entire_inventory(monkeypatch):
    planner, env = selection_plan(monkeypatch, selected=None)
    with pytest.raises(ValueError, match="continuity"):
        planner.build_sync_plan(env)


@pytest.mark.parametrize("case", ["missing", "duplicate", "legacy"])
def test_single_service_selection_requires_one_inventory_match(monkeypatch, case):
    planner, env = selection_plan(monkeypatch)
    if case == "missing":
        env["INPUT_CONFIGURED_SERVICE"] = "missing-service"
    elif case == "duplicate":
        entries = json.loads(env["CLOUD_RUN_SERVICE_TARGETS_JSON"])
        entries.append(entries[0])
        env["CLOUD_RUN_SERVICE_TARGETS_JSON"] = json.dumps(entries)
    else:
        env.pop("CLOUD_RUN_SERVICE_TARGETS_JSON")
        env["CLOUD_RUN_SERVICE"] = "unit-service"
    with pytest.raises(ValueError, match="configured_service"):
        planner.build_sync_plan(env)


def image_only_fixture(monkeypatch):
    from scripts import build_cloud_run_env_sync_plan as planner
    from scripts import verify_deployed_runtime_target_admission as admission

    serving = frozen_target()
    serving["scheduler"] = {"precheck_time": "0 8 * * 1-5"}
    serving["live_continuity"]["baseline_target_sha256"] = runtime_target_fingerprint(serving)
    desired = copy.deepcopy(serving)
    desired["scheduler"]["precheck_time"] = "5 8 * * 1-5"
    container = {"env": [
        {"name": "RUNTIME_TARGET_JSON", "value": json.dumps(serving)},
        {"name": "STRATEGY_PROFILE", "value": "tqqq_growth_income"},
        {"name": "IBKR_DRY_RUN_ONLY", "value": "false"},
    ]}
    service = {"metadata": {"name": "unit-service"},
        "spec": {"template": {"spec": {"containers": [copy.deepcopy(container)]}}},
        "status": {"traffic": [{"revisionName": "unit-service-current", "percent": 100}]}}
    revision = {"metadata": {"name": "unit-service-current"},
        "spec": {"containers": [copy.deepcopy(container)]}}
    calls = []

    def describe(**kwargs):
        calls.append(("service-read", kwargs["service"]))
        return copy.deepcopy(service)

    def run(command):
        calls.append(tuple(command))
        assert command[:4] == ["gcloud", "run", "revisions", "describe"]
        assert command[4] == "unit-service-current"
        return json.dumps(revision)

    monkeypatch.setattr(admission, "_describe_service", describe)
    monkeypatch.setattr(admission, "_run", run)
    monkeypatch.setattr(planner, "_load_platform_config", lambda env: {})
    monkeypatch.setattr(planner, "get_platform_profile_status_matrix", lambda: [
        {"canonical_profile": "tqqq_growth_income", "eligible": True, "enabled": True},
    ])
    env = {
        "WORKFLOW_TARGET": "configured", "INPUT_CONFIGURED_SERVICE": "unit-service",
        "INPUT_DEPLOY_IMAGE": "true", "INPUT_SYNC_ENV": "false",
        "INPUT_APPROVE_TRAFFIC_SHIFT": "false", "INPUT_APPROVE_SCHEDULER_SYNC": "false",
        "GCP_PROJECT_ID": "unit-project", "CLOUD_RUN_REGION": "unit-region",
        "CLOUD_RUN_SERVICE_TARGETS_JSON": json.dumps([
            {"service_name": "unit-service", "runtime_target": desired},
        ]),
    }
    return planner, env, service, revision, serving, calls


def test_image_only_staging_preserves_serving_baseline_without_rebasing_desired(monkeypatch):
    planner, env, service, revision, serving, calls = image_only_fixture(monkeypatch)
    before = copy.deepcopy((env, service, revision))
    plan = planner.build_sync_plan(env)
    assert plan["mode"] == "per_service"
    assert len(plan["targets"]) == 1
    assert plan["targets"][0]["service_name"] == "unit-service"
    assert json.loads(plan["targets"][0]["env"]["RUNTIME_TARGET_JSON"]) == serving
    assert (env, service, revision) == before
    assert calls and all("update" not in call and "update-traffic" not in call for call in calls)


@pytest.mark.parametrize("flag", [
    "INPUT_DEPLOY_IMAGE", "INPUT_SYNC_ENV", "INPUT_APPROVE_TRAFFIC_SHIFT",
    "INPUT_APPROVE_SCHEDULER_SYNC",
])
def test_other_deployment_modes_still_reject_stale_desired_baseline(monkeypatch, flag):
    planner, env, _, _, _, calls = image_only_fixture(monkeypatch)
    env[flag] = "false" if flag == "INPUT_DEPLOY_IMAGE" else "true"
    with pytest.raises(ValueError, match="continuity"):
        planner.build_sync_plan(env)
    assert calls == []


@pytest.mark.parametrize("case", ["split_traffic", "template_drift", "stale_serving"])
def test_image_only_staging_rejects_unverified_incumbent(monkeypatch, case):
    planner, env, service, revision, _, _ = image_only_fixture(monkeypatch)
    if case == "split_traffic":
        service["status"]["traffic"] = [
            {"revisionName": "unit-service-current", "percent": 50},
            {"revisionName": "unit-service-other", "percent": 50},
        ]
    elif case == "template_drift":
        service["spec"]["template"]["spec"]["containers"][0]["env"].append(
            {"name": "IBKR_FORCE_RUN", "value": "true"},
        )
    else:
        for container in [service["spec"]["template"]["spec"]["containers"][0], revision["spec"]["containers"][0]]:
            target = json.loads(container["env"][0]["value"])
            target["strategy_release"]["strategy_revision"] = "b" * 40
            container["env"][0]["value"] = json.dumps(target)
    with pytest.raises(ValueError):
        planner.build_sync_plan(env)


def test_image_only_staging_retains_current_strategy_eligibility(monkeypatch):
    planner, env, _, _, _, _ = image_only_fixture(monkeypatch)
    monkeypatch.setattr(planner, "get_platform_profile_status_matrix", lambda: [
        {"canonical_profile": "tqqq_growth_income", "eligible": False, "enabled": True},
    ])
    with pytest.raises(ValueError):
        planner.build_sync_plan(env)
