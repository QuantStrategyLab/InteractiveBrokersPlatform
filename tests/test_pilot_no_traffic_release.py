"""Synthetic release control flow; no cloud, Docker, credentials or broker IO."""
import copy
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import deploy_pilot_no_traffic as pilot

COMMIT = "a" * 40
IMAGE_DIGEST = "sha256:" + "b" * 64
SERVICE = "synthetic-tqqq-service"
SERVING = "synthetic-tqqq-service-00001"
CANDIDATE = "synthetic-tqqq-service-00002"
TARGET = {"platform_id": "ibkr", "service_name": SERVICE, "strategy_profile": pilot.PROFILE,
          "account_selector": ["DU00000001"], "account_scope": "synthetic-scope",
          "deployment_selector": "synthetic-deployment", "strategy_release": {"strategy_revision": COMMIT}}
ROOT = Path(__file__).resolve().parents[1]


def environment():
    return {"GITHUB_REF": "refs/heads/main", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_RUN_ID": "123", "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REPOSITORY": pilot.REPOSITORY,
            "GITHUB_SHA": COMMIT, "PILOT_SOURCE_COMMIT": COMMIT, "PILOT_PROFILE": pilot.PROFILE,
            "PILOT_DEPLOY_GATE": "true", "GCP_PROJECT_ID": "synthetic-project",
            "CLOUD_RUN_REGION": "us-central1", "CLOUD_RUN_SERVICE_TARGETS_JSON": json.dumps({
                "targets": [{"service_name": SERVICE, "runtime_target": TARGET}]}),
            "IBKR_ARTIFACT_REGISTRY_ROLLBACK_DIGESTS": "sha256:" + "c" * 64 + ",sha256:" + "d" * 64}


def baseline():
    template = {"metadata": {"labels": {"commit-sha": "e" * 40},
                             "annotations": {"autoscaling.knative.dev/minScale": "1"}},
                "spec": {"serviceAccountName": "synthetic-runtime", "timeoutSeconds": 300,
                         "containerConcurrency": 1, "containers": [{"image": "synthetic/old@sha256:" + "e" * 64,
                          "env": [{"name": "RUNTIME_TARGET_JSON", "value": json.dumps(TARGET)},
                                  {"name": "PRIVATE_AMOUNT", "value": "12345.67"},
                                  {"name": "TOKEN", "valueFrom": {"secretKeyRef": {"name": "synthetic-secret", "key": "1"}}}],
                          "resources": {"limits": {"memory": "1Gi", "cpu": "1"}},
                          "startupProbe": {"httpGet": {"path": "/health"}}}]}}
    revision = copy.deepcopy(template)
    revision["metadata"]["name"] = SERVING
    revision["status"] = {"conditions": [{"type": "Ready", "status": "True"}]}
    service = {"metadata": {"name": SERVICE, "annotations": {"run.googleapis.com/ingress": "internal",
               "run.googleapis.com/minScale": "2"}}, "spec": {"template": template, "traffic": [{"latestRevision": True, "percent": 100}]},
               "status": {"conditions": [{"type": "Ready", "status": "True"}],
                          "url": "https://synthetic.run.app", "latestCreatedRevisionName": SERVING,
                          "latestReadyRevisionName": SERVING, "traffic": [{"revisionName": SERVING, "percent": 100, "latestRevision": True}]}}
    traffic, serving = pilot._traffic(service)
    return {"service": service, "serving_revision": revision, "serving": serving, "traffic": traffic,
            "iam": {"bindings": [{"role": "roles/run.invoker", "members": ["synthetic-invoker"]}]},
            "scheduler": [{"name": "synthetic-precheck", "state": "PAUSED", "httpTarget": {"uri": "https://synthetic.run.app/dry-run"}}]}


@pytest.fixture
def controlled_release(monkeypatch):
    from scripts import verify_image_ues_release_binding as binding
    from scripts import refresh_artifact_registry_keep as keep
    monkeypatch.setattr(binding, "revision_from_image", lambda _: COMMIT)
    monkeypatch.setattr(binding, "revision_from_lockfile", lambda _: COMMIT)
    protected = []
    monkeypatch.setattr(keep, "refresh", lambda _run, **kwargs: protected.append(kwargs))
    ctx = pilot.context(environment())
    immutable = f"{ctx.registry_host}/{ctx.project}/cloud-run-source-deploy/interactivebrokersplatform/cloud-run@{IMAGE_DIGEST}"
    before = baseline()
    candidate = copy.deepcopy(before["serving_revision"])
    candidate["metadata"]["name"] = CANDIDATE
    candidate["spec"]["containers"][0]["image"] = immutable
    after = copy.deepcopy(before)
    after["service"]["spec"]["template"] = {key: copy.deepcopy(candidate[key]) for key in ("metadata", "spec")}
    after["service"]["spec"]["traffic"] = [{"revisionName": SERVING, "percent": 100}]
    after["service"]["status"]["latestCreatedRevisionName"] = CANDIDATE
    after["service"]["status"]["latestReadyRevisionName"] = CANDIDATE
    calls, states = [], [before, copy.deepcopy(before), after]

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "git":
            return COMMIT if args[-1] == "HEAD" else ""
        if "image" in args and "inspect" in args:
            return json.dumps([immutable] if "RepoDigests" in args[-1] else {"org.opencontainers.image.revision": COMMIT})
        if args[:5] == ["gcloud", "artifacts", "docker", "images", "describe"]:
            return IMAGE_DIGEST
        if args[:4] == ["gcloud", "run", "services", "update"]:
            return json.dumps({"status": {"latestCreatedRevisionName": CANDIDATE}})
        if args[:4] == ["gcloud", "run", "revisions", "describe"]:
            return json.dumps(candidate)
        return ""

    def execute():
        iterator = iter(states)
        return pilot.release(ctx, Path("synthetic-source"), environment(), run,
                             take_snapshot=lambda *_: next(iterator))

    return SimpleNamespace(ctx=ctx, before=before, after=after, candidate=candidate,
                           calls=calls, states=states, run=run, execute=execute, protected=protected)


def test_image_only_update_preserves_nonzero_minimums_and_live_routing(controlled_release):
    state = controlled_release
    result = state.execute()
    assert result["status"] == "verified_no_traffic" and result["image_digest"] == IMAGE_DIGEST
    updates = [args for args, _ in state.calls if args[:4] == ["gcloud", "run", "services", "update"]]
    assert len(updates) == 1
    assert set(arg.split("=", 1)[0] for arg in updates[0][5:]) == {"--project", "--region", "--image", "--no-traffic", "--quiet", "--format"}
    assert state.protected[0]["candidate"].endswith("@" + IMAGE_DIGEST)
    assert all("scheduler" not in args and "set-iam-policy" not in args and "update-traffic" not in args for args, _ in state.calls)


@pytest.mark.parametrize("key,value", [("PILOT_DEPLOY_GATE", "false"), ("GITHUB_REF", "refs/heads/test"),
    ("GITHUB_EVENT_NAME", "schedule"),
    ("GITHUB_REPOSITORY", "synthetic-fork/InteractiveBrokersPlatform"),
    ("GITHUB_RUN_ATTEMPT", "2"), ("PILOT_SOURCE_COMMIT", "bad"), ("PILOT_SOURCE_COMMIT", "f" * 40),
    ("PILOT_PROFILE", "soxl_soxx_trend_income")])
def test_unqualified_dispatch_cannot_reach_external_calls(key, value):
    env = environment()
    env[key] = value
    with pytest.raises(pilot.ReleaseError, match="dispatch_not_admitted"):
        pilot.context(env)


@pytest.mark.parametrize("change", ["duplicate", "missing", "multi_account", "service_mismatch"])
def test_target_scope_cannot_widen(change):
    env = environment()
    inventory = json.loads(env["CLOUD_RUN_SERVICE_TARGETS_JSON"])
    if change == "duplicate":
        inventory["targets"] *= 2
    elif change == "missing":
        inventory["targets"] = []
    elif change == "multi_account":
        inventory["targets"][0]["runtime_target"]["account_selector"].append("DU00000002")
    else:
        inventory["targets"][0]["service_name"] = "synthetic-other"
    env["CLOUD_RUN_SERVICE_TARGETS_JSON"] = json.dumps(inventory)
    with pytest.raises(pilot.ReleaseError):
        pilot.context(env)


@pytest.mark.parametrize("change", ["runtime", "resources", "manual"])
def test_bad_baseline_stops_before_build_or_mutation(controlled_release, change):
    state = controlled_release
    if change == "runtime":
        state.ctx.target["account_scope"] = "changed"
    elif change == "resources":
        state.before["service"]["spec"]["template"]["spec"]["containers"][0]["resources"] = {}
    else:
        state.before["service"]["metadata"]["annotations"]["run.googleapis.com/scalingMode"] = "manual"
    with pytest.raises(pilot.ReleaseError):
        state.execute()
    assert [args[0] for args, _ in state.calls] == ["git", "git"]


@pytest.mark.parametrize("latest_ready", [SERVING, "synthetic-tqqq-service-unused"])
def test_old_unused_or_failed_image_candidate_is_kept_when_config_matches(controlled_release, latest_ready):
    state = controlled_release
    for snapshot in state.states[:2]:
        snapshot["service"]["status"]["latestCreatedRevisionName"] = "synthetic-tqqq-service-unused"
        snapshot["service"]["status"]["latestReadyRevisionName"] = latest_ready
        snapshot["service"]["spec"]["template"]["spec"]["containers"][0]["image"] = "synthetic/unused:old-tag"
    assert state.execute()["status"] == "verified_no_traffic"
    assert all("delete" not in args and "update-traffic" not in args for args, _ in state.calls)


def test_only_two_build_labels_may_differ_from_serving_while_preserved_exactly(controlled_release):
    state = controlled_release
    for snapshot in state.states:
        template = snapshot["service"]["spec"]["template"]
        template["metadata"]["labels"].update({"commit-sha": "f" * 40, "github-run-id": "999"})
    state.candidate["metadata"]["labels"].update({"commit-sha": "f" * 40, "github-run-id": "999"})
    assert state.execute()["status"] == "verified_no_traffic"


def test_other_template_label_difference_is_not_ignored(controlled_release):
    state = controlled_release
    state.before["service"]["spec"]["template"]["metadata"]["labels"]["policy-binding"] = "different"
    with pytest.raises(pilot.ReleaseError, match="template_not_serving"):
        state.execute()


def test_build_labels_cannot_change_during_image_update(controlled_release):
    state = controlled_release
    state.after["service"]["spec"]["template"]["metadata"]["labels"]["github-run-id"] = "unexpected"
    with pytest.raises(pilot.ReleaseError, match="configuration_changed"):
        state.execute()


@pytest.mark.parametrize("change", ["env_value", "secret", "network", "memory", "probe", "iam", "scheduler", "tag", "traffic"])
def test_post_update_drift_never_claims_success(controlled_release, change):
    state = controlled_release
    container = state.after["service"]["spec"]["template"]["spec"]["containers"][0]
    if change == "env_value":
        container["env"][1]["value"] = "999.99"
    elif change == "secret":
        container["env"][2]["valueFrom"]["secretKeyRef"]["key"] = "2"
    elif change == "network":
        state.after["service"]["metadata"]["annotations"]["run.googleapis.com/ingress"] = "all"
    elif change == "memory":
        container["resources"]["limits"]["memory"] = "512Mi"
    elif change == "probe":
        container["startupProbe"]["httpGet"]["path"] = "/different"
    elif change == "iam":
        state.after["iam"]["bindings"] = []
    elif change == "scheduler":
        state.after["scheduler"][0]["state"] = "ENABLED"
    elif change == "tag":
        state.after["traffic"].append({"revision": CANDIDATE, "percent": 0, "tag": "unexpected", "url": "synthetic"})
    else:
        state.after["serving"] = CANDIDATE
    with pytest.raises(pilot.ReleaseError):
        state.execute()
    assert sum(args[:4] == ["gcloud", "run", "services", "update"] for args, _ in state.calls) == 1


def test_changes_during_build_stop_before_update(controlled_release):
    state = controlled_release
    state.states[1]["scheduler"][0]["state"] = "ENABLED"
    with pytest.raises(pilot.ReleaseError, match="baseline_changed"):
        state.execute()
    assert not any(args[:4] == ["gcloud", "run", "services", "update"] for args, _ in state.calls)


def test_unused_candidate_change_during_build_also_stops_before_update(controlled_release):
    state = controlled_release
    state.states[1]["service"]["spec"]["template"]["spec"]["containers"][0]["image"] = "synthetic/other-unused"
    with pytest.raises(pilot.ReleaseError, match="baseline_changed"):
        state.execute()
    assert not any(args[:4] == ["gcloud", "run", "services", "update"] for args, _ in state.calls)


def test_inventory_optional_financial_fields_are_never_applied(controlled_release):
    state = controlled_release
    state.ctx.target["some_optional_financial_policy"] = {"unapproved_number": 999}
    assert state.execute()["status"] == "verified_no_traffic"
    assert all("unapproved_number" not in str(args) for args, _ in state.calls)


def test_actual_explicit_binding_mismatch_blocks_even_if_inventory_has_no_binding(controlled_release):
    state = controlled_release
    state.ctx.target.pop("strategy_release")
    wrong = copy.deepcopy(TARGET)
    wrong["strategy_release"]["strategy_revision"] = "f" * 40
    for snapshot in state.states:
        for template in (snapshot["service"]["spec"]["template"], snapshot["serving_revision"]):
            template["spec"]["containers"][0]["env"][0]["value"] = json.dumps(wrong)
    with pytest.raises(pilot.ReleaseError, match="image_binding_unverified"):
        state.execute()
    assert not any(args[:2] == ["docker", "push"] or args[:4] == ["gcloud", "run", "services", "update"] for args, _ in state.calls)


def test_absent_actual_binding_keeps_original_not_covered_contract(controlled_release):
    state = controlled_release
    target = copy.deepcopy(TARGET)
    target.pop("strategy_release")
    for snapshot in state.states:
        for template in (snapshot["service"]["spec"]["template"], snapshot["serving_revision"]):
            template["spec"]["containers"][0]["env"][0]["value"] = json.dumps(target)
    state.candidate["spec"]["containers"][0]["env"][0]["value"] = json.dumps(target)
    result = state.execute()
    assert result["status"] == "verified_no_traffic" and result["ues_binding_coverage"] == "not_covered"


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_json_is_never_accepted(constant):
    with pytest.raises(pilot.ReleaseError, match="invalid_json"):
        pilot._json('{"value":' + constant + '}')


def test_original_identity_is_bootstrapped_once_and_masked_before_env(tmp_path, capsys):
    workflow = tmp_path / ".github/workflows"
    workflow.mkdir(parents=True)
    provider = "projects/123/locations/global/workloadIdentityPools/synthetic/providers/synthetic"
    path = workflow / "sync-cloud-run-env-protected.yml"
    path.write_text("env:\n  GCP_WORKLOAD_IDENTITY_PROVIDER: " + provider + "\n")
    destination = tmp_path / "github-env"
    pilot.bootstrap_identity(tmp_path, destination)
    assert capsys.readouterr().out == "::add-mask::" + provider + "\n"
    assert destination.read_text() == "PILOT_WIF_PROVIDER=" + provider + "\n"
    path.write_text(path.read_text() * 2)
    with pytest.raises(pilot.ReleaseError, match="identity_source_unavailable"):
        pilot.bootstrap_identity(tmp_path, destination)


@pytest.mark.parametrize("change", ["unready", "wrong_image", "config"])
def test_candidate_must_be_ready_exact_image_and_same_configuration(controlled_release, change):
    state = controlled_release
    if change == "unready":
        state.candidate["status"]["conditions"][0]["status"] = "False"
    elif change == "wrong_image":
        state.candidate["spec"]["containers"][0]["image"] = "synthetic-wrong"
    else:
        state.candidate["spec"]["timeoutSeconds"] = 120
    with pytest.raises(pilot.ReleaseError, match="candidate_unverified"):
        state.execute()


def test_ambiguous_update_stops_once_and_does_not_retry(controlled_release):
    state = controlled_release
    original = state.run
    def unknown(args, **kwargs):
        if args[:4] == ["gcloud", "run", "services", "update"]:
            state.calls.append((args, kwargs))
            raise pilot.ReleaseError("service_update_unknown")
        return original(args, **kwargs)
    iterator = iter(state.states)
    with pytest.raises(pilot.ReleaseError, match="service_update_unknown"):
        pilot.release(state.ctx, Path("synthetic-source"), environment(), unknown, lambda *_: next(iterator))
    assert sum(args[:4] == ["gcloud", "run", "services", "update"] for args, _ in state.calls) == 1


@pytest.mark.parametrize("unknown_update", [False, True])
def test_failure_receipt_preserves_attempted_and_completed_writes(controlled_release, monkeypatch, tmp_path, capsys, unknown_update):
    state = controlled_release
    for key, value in environment().items():
        monkeypatch.setenv(key, value)
    real_release = pilot.release
    original_run = state.run
    reads = []
    def snapshot(*_):
        reads.append(True)
        if len(reads) == 3:
            raise pilot.ReleaseError("source_read_failed")
        return state.states[len(reads) - 1]
    def run(args, **kwargs):
        if unknown_update and args[:4] == ["gcloud", "run", "services", "update"]:
            state.calls.append((args, kwargs))
            raise pilot.ReleaseError("service_update_unknown")
        return original_run(args, **kwargs)
    monkeypatch.setattr(pilot, "release", lambda ctx, source, env, **kwargs:
                        real_release(ctx, source, env, run, snapshot, **kwargs))
    path = tmp_path / "receipt.json"
    assert pilot.main(["--source-dir", "synthetic-source", "--receipt", str(path)]) == 1
    receipt = json.loads(path.read_text())
    assert receipt["image_push_attempted"] is True and receipt["image_push_completed"] is True
    assert receipt["keep_attempted"] is True and receipt["keep_completed"] is True
    assert receipt["service_update_attempted"] is True
    assert receipt["service_update_completed"] is (not unknown_update)
    assert receipt["readback_verified"] is False
    assert receipt["stage"] == ("service_update_unknown" if unknown_update else "source_read_failed")
    assert len(reads) == (2 if unknown_update else 3)
    assert sum(args[:4] == ["gcloud", "run", "services", "update"] for args, _ in state.calls) == 1
    assert SERVICE not in capsys.readouterr().out.replace("::add-mask::" + SERVICE, "")


@pytest.mark.parametrize("response", ["private invalid response", "{}", '{"status":{},"status":{}}'])
def test_invalid_update_ack_is_unknown_and_never_repeated(controlled_release, response):
    state = controlled_release
    original = state.run
    def invalid(args, **kwargs):
        if args[:4] == ["gcloud", "run", "services", "update"]:
            state.calls.append((args, kwargs))
            return response
        return original(args, **kwargs)
    iterator = iter(state.states)
    with pytest.raises(pilot.ReleaseError, match="service_update_unknown"):
        pilot.release(state.ctx, Path("synthetic-source"), environment(), invalid, lambda *_: next(iterator))
    assert sum(args[:4] == ["gcloud", "run", "services", "update"] for args, _ in state.calls) == 1


@pytest.mark.parametrize("failure", [None, "scheduler", "service_identity", "revision_identity", "iam"])
def test_snapshot_reads_full_private_config_but_only_scoped_scheduler(failure):
    ctx = pilot.context(environment())
    state = baseline()
    state["service"]["status"]["traffic"].append({"revisionName": SERVING, "percent": 0,
        "tag": "existing", "url": "https://existing---synthetic.run.app"})
    calls = []
    job = {"name": "projects/synthetic/locations/us-central1/jobs/synthetic-precheck", "state": "PAUSED",
           "schedule": "0 0 * * *", "httpTarget": {"uri": "https://existing---synthetic.run.app/dry-run",
           "headers": {"private-header": "synthetic-private-token"}}}
    if failure == "service_identity":
        state["service"]["metadata"]["name"] = "synthetic-other"
    elif failure == "revision_identity":
        state["serving_revision"]["metadata"]["name"] = "synthetic-other"
    elif failure == "iam":
        state["iam"] = {}
    def read(args, **kwargs):
        calls.append(args)
        if args[:4] == ["gcloud", "run", "services", "describe"]:
            return json.dumps(state["service"])
        if args[:4] == ["gcloud", "run", "revisions", "describe"]:
            return json.dumps(state["serving_revision"])
        if "get-iam-policy" in args:
            return json.dumps(state["iam"])
        if args[:4] == ["gcloud", "scheduler", "jobs", "list"]:
            return "null" if failure == "scheduler" else json.dumps([
                {"name": job["name"], "httpTarget": {"uri": job["httpTarget"]["uri"]}},
                {"name": "synthetic-unrelated", "httpTarget": {"uri": "https://unrelated.invalid/run"}}])
        if args[:4] == ["gcloud", "scheduler", "jobs", "describe"]:
            assert args[4] == "synthetic-precheck"
            return json.dumps(job)
        pytest.fail("unexpected external command")
    if failure:
        with pytest.raises(pilot.ReleaseError):
            pilot.snapshot(ctx, read)
    else:
        captured = pilot.snapshot(ctx, read)
        assert captured["scheduler"][0]["httpTarget"]["headers"] == {"private-header": "synthetic-private-token"}
        assert captured["service"]["spec"]["template"]["spec"]["containers"][0]["env"][1]["value"] == "12345.67"
    assert all(sum(arg.startswith("--format=") for arg in args) == 1 for args in calls)
    assert all("synthetic-unrelated" not in args for args in calls)


def test_failed_unused_revision_does_not_make_healthy_serving_unavailable():
    state = baseline()
    state["service"]["status"]["conditions"][0]["status"] = "False"
    state["service"]["status"]["latestCreatedRevisionName"] = "synthetic-tqqq-service-unused"
    def read(args, **_):
        if "get-iam-policy" in args:
            return json.dumps(state["iam"])
        if "scheduler" in args:
            return "[]"
        return json.dumps(state["serving_revision"] if "revisions" in args else state["service"])
    captured = pilot.snapshot(pilot.context(environment()), read)
    pilot.validate_baseline(pilot.context(environment()), captured)
    assert captured["serving"] == SERVING


def test_preflight_has_no_external_calls_or_binding_output(monkeypatch, tmp_path, capsys):
    for key, value in environment().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(pilot.subprocess, "run", lambda *_, **__: pytest.fail("preflight must not call outside the process"))
    path = tmp_path / "receipt.json"
    assert pilot.main(["--preflight", "--receipt", str(path)]) == 0
    text = capsys.readouterr().out
    assert json.loads(text)["status"] == "preflight_only"
    assert SERVICE not in text and "DU00000001" not in text and "synthetic-project" not in text


@pytest.mark.parametrize("kind", ["nonzero", "timeout"])
def test_cli_failure_cannot_echo_private_output_or_retry(monkeypatch, capsys, kind):
    calls = []
    def failed(args, **kwargs):
        calls.append((args, kwargs))
        if kind == "timeout":
            raise subprocess.TimeoutExpired(args, 1, output="private DU00000001 amount=12345.67", stderr="private")
        return SimpleNamespace(returncode=1, stdout="private DU00000001", stderr="private amount=12345.67")
    monkeypatch.setattr(pilot.subprocess, "run", failed)
    with pytest.raises(pilot.ReleaseError, match="service_update_unknown"):
        pilot.command(["synthetic"], stage="service_update_unknown")
    assert len(calls) == 1 and capsys.readouterr() == ("", "")
    assert calls[0][1]["env"]["CLOUDSDK_CORE_DISABLE_FILE_LOGGING"] == "true"


def test_receipt_never_contains_binding_values_or_financial_hashes(monkeypatch, tmp_path, capsys):
    for key, value in environment().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(pilot, "release", lambda *_, **__: (_ for _ in ()).throw(pilot.ReleaseError("source_read_failed")))
    path = tmp_path / "receipt.json"
    assert pilot.main(["--source-dir", "synthetic-source", "--receipt", str(path)]) == 1
    out = capsys.readouterr().out
    receipt = json.loads(path.read_text())
    assert receipt["stage"] == "source_read_failed" and receipt["execution_authorized"] is False
    assert all(receipt[field] is False for field in pilot.PROGRESS_FIELDS)
    assert path.stat().st_mode & 0o777 == 0o600
    for private in (SERVICE, "DU00000001", "synthetic-project", "12345.67", "account_scope", "sha256"):
        assert private not in out + path.read_text()


def test_workflow_inputs_and_private_context_are_separated_before_headers():
    wrapper = (ROOT / ".github/workflows/deploy-pilot-no-traffic.yml").read_text()
    callee = (ROOT / ".github/workflows/deploy-pilot-no-traffic-protected.yml").read_text()
    assert "group: Deploy Cloud Run-${{ github.ref_name }}" in wrapper
    assert "github.ref == 'refs/heads/main' && github.run_attempt == 1" in wrapper
    assert "github.ref == 'refs/heads/main' && github.run_attempt == 1" in callee
    assert "github.repository == 'QuantStrategyLab/InteractiveBrokersPlatform'" in wrapper
    assert "github.repository == 'QuantStrategyLab/InteractiveBrokersPlatform'" in callee
    inputs = wrapper.split("    inputs:", 1)[1].split("\npermissions:", 1)[0]
    assert "configured_service" not in inputs and "source_commit:" in inputs and "profile:" in inputs
    assert "vars." not in callee and "concurrency:" not in callee
    assert "projects/303" not in wrapper + callee
    assert "workload_identity_provider: ${{ env.PILOT_WIF_PROVIDER }}" in callee
    assert "env:" not in wrapper and "with:" in wrapper and "secrets:" in wrapper
    assert callee.index("--preflight") < callee.index("google-github-actions/auth")
    for forbidden in ("sync_env", "approve_traffic", "approve_scheduler", "gcloud scheduler", "update-traffic", "secrets versions access"):
        assert forbidden not in callee
    assert callee.count("actions/upload-artifact") == 1
    assert "path: ${{ runner.temp }}/pilot-release.safe.json" in callee


def _platform_metadata_shape(state):
    compact = '[{"network":"synthetic-network","subnetwork":"synthetic-subnet"}]'
    formatted = '[ { "subnetwork": "synthetic-subnet", "network": "synthetic-network" } ]'
    for snapshot in state.states:
        template = snapshot["service"]["spec"]["template"]
        template["metadata"]["annotations"][pilot.NETWORK_INTERFACES_ANNOTATION] = compact
        revision = snapshot["serving_revision"]
        revision["metadata"]["labels"]["cloud.googleapis.com/location"] = "synthetic-region"
        revision["metadata"]["annotations"][pilot.INGRESS_ANNOTATION] = "internal"
        revision["metadata"]["annotations"][pilot.NETWORK_INTERFACES_ANNOTATION] = formatted
    state.candidate["metadata"]["labels"]["cloud.googleapis.com/location"] = "synthetic-region"
    state.candidate["metadata"]["annotations"][pilot.INGRESS_ANNOTATION] = "internal"
    state.candidate["metadata"]["annotations"][pilot.NETWORK_INTERFACES_ANNOTATION] = formatted


def test_platform_location_ingress_and_network_json_representation_are_equivalent(controlled_release):
    state = controlled_release
    _platform_metadata_shape(state)
    assert state.execute()["status"] == "verified_no_traffic"
    # Revision projection must not remove the Service-global ingress record.
    config = pilot._service_configuration(state.before["service"])
    assert config["metadata"]["annotations"][pilot.INGRESS_ANNOTATION] == "internal"


@pytest.mark.parametrize("where", ["serving", "after_template", "candidate"])
def test_actual_network_interface_change_is_never_normalized_away(controlled_release, where):
    state = controlled_release
    _platform_metadata_shape(state)
    if where == "serving":
        template = state.before["serving_revision"]
    elif where == "after_template":
        template = state.after["service"]["spec"]["template"]
    else:
        template = state.candidate
    template["metadata"]["annotations"][pilot.NETWORK_INTERFACES_ANNOTATION] = '[{"network":"different-network","subnetwork":"synthetic-subnet"}]'
    with pytest.raises(pilot.ReleaseError):
        state.execute()


@pytest.mark.parametrize("change", ["global_ingress", "environment", "unrelated_annotation", "unrelated_label"])
def test_platform_metadata_normalization_keeps_all_other_configuration_checks(controlled_release, change):
    state = controlled_release
    _platform_metadata_shape(state)
    template = state.after["service"]["spec"]["template"]
    if change == "global_ingress":
        state.after["service"]["metadata"]["annotations"][pilot.INGRESS_ANNOTATION] = "all"
    elif change == "environment":
        template["spec"]["containers"][0]["env"][1]["value"] = "different-private-value"
    elif change == "unrelated_annotation":
        template["metadata"]["annotations"]["run.googleapis.com/vpc-access-egress"] = "all-traffic"
    else:
        template["metadata"]["labels"]["policy-binding"] = "different"
    with pytest.raises(pilot.ReleaseError):
        state.execute()


@pytest.mark.parametrize("where", ["serving", "candidate"])
def test_revision_ingress_projection_must_match_service_global(controlled_release, where):
    state = controlled_release
    _platform_metadata_shape(state)
    revision = state.before["serving_revision"] if where == "serving" else state.candidate
    revision["metadata"]["annotations"][pilot.INGRESS_ANNOTATION] = "all"
    with pytest.raises(pilot.ReleaseError, match="ingress_projection_unverified"):
        state.execute()


@pytest.mark.parametrize("network_json", ['not-json', '[{"network":"a","network":"b"}]', '[{"network":NaN}]', '"synthetic-network"'])
def test_network_annotation_requires_strict_known_json_shape(controlled_release, network_json):
    state = controlled_release
    _platform_metadata_shape(state)
    state.before["service"]["spec"]["template"]["metadata"]["annotations"][pilot.NETWORK_INTERFACES_ANNOTATION] = network_json
    with pytest.raises(pilot.ReleaseError):
        state.execute()
    assert [args[0] for args, _ in state.calls] == ["git", "git"]
