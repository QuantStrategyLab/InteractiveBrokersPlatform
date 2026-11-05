"""Offline adverse checks; no cloud or application requests."""
import copy
from unittest import mock

import pytest

from scripts import reconcile_cloud_runtime as rcr
from scripts.runtime_target_continuity import verify_traffic_revision


def candidate():
    return {
        "metadata": {"name": "unit-service-new", "generation": 1, "labels": {"commit-sha": "abc"},
                     "annotations": {"autoscaling.knative.dev/minScale": "1"}},
        "spec": {"containers": [{"image": "fake@sha256:abc",
            "startupProbe": {"httpGet": {"path": "/health", "port": 8080}},
            "env": [{"name": "RUNTIME_TARGET_ENABLED", "value": "true"},
                    {"name": "RUNTIME_TARGET_JSON", "value": "{}"}]}]},
        "status": {"observedGeneration": 1, "imageDigest": "sha256:abc", "conditions": [
            {"type": "Ready", "status": "True", "reason": "Retired"},
            {"type": "Active", "status": "False", "reason": "Retired"},
            {"type": "ContainerReady", "status": "True"}]}}


def service():
    return {"status": {"latestCreatedRevisionName": "unit-service-new",
                       "traffic": [{"revisionName": "unit-service-old", "percent": 100}]}}


@pytest.mark.parametrize("case", ["healthy", "timeout", "false", "unknown", "read_error",
    "identity", "image", "spec", "traffic", "latest", "malformed"])
def test_bounded_candidate_wait_preserves_identity_and_traffic(case):
    original = candidate()
    fresh = copy.deepcopy(original)
    fresh["status"]["conditions"].append({"type": "ContainerHealthy", "status": "True"})
    current_service = service()
    if case == "timeout": fresh = copy.deepcopy(original)
    if case in {"false", "unknown"}:
        fresh["status"]["conditions"][-1]["status"] = "False" if case == "false" else "Unknown"
    if case == "identity": fresh["metadata"]["labels"]["commit-sha"] = "other"
    if case == "image": fresh["status"]["imageDigest"] = "sha256:other"
    if case == "spec": fresh["spec"]["containers"][0]["env"][0]["value"] = "false"
    if case == "traffic": current_service["status"]["traffic"][0]["percent"] = 99
    if case == "latest": current_service["status"]["latestCreatedRevisionName"] = "unit-service-other"
    if case == "malformed": fresh = []
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        assert "update-traffic" not in args
        assert kwargs["timeout"] <= 30
        if "services" in args: return current_service
        if case == "read_error": raise rcr.ReconcileError("candidate read failed")
        return fresh
    with mock.patch.object(rcr, "_run", side_effect=run), mock.patch.object(rcr.time, "sleep"), \
         mock.patch.object(rcr.time, "monotonic", side_effect=range(0, 1000, 10)), \
         mock.patch("scripts.runtime_target_continuity.verify_target_continuity"):
        args = dict(project="<PROJECT_ID>", region="unit-region", service_name="unit-service",
                    service=service(), revision="unit-service-new", commit="abc")
        if case == "healthy":
            rcr._verify_deploy_candidate_with_wait(original, **args)
            assert any("revisions" in call for call in calls)
        else:
            with pytest.raises((rcr.ReconcileError, ValueError)):
                rcr._verify_deploy_candidate_with_wait(original, **args)


def test_strict_health_rejects_disabled_retired_candidate():
    value = candidate()
    value["spec"]["containers"][0]["env"][0]["value"] = "false"
    with mock.patch("scripts.runtime_target_continuity.verify_target_continuity"):
        with pytest.raises(ValueError, match="startup probe"):
            verify_traffic_revision(value, expected_revision="unit-service-new", expected_commit="abc",
                                    require_startup_health=True)


def test_workflow_scopes_revision_minimum_and_bounded_deploy():
    from pathlib import Path
    workflow = Path(".github/workflows/sync-cloud-run-env-protected.yml").read_text()
    # Inspect the deploy block rather than unrelated scheduler loops.
    block = workflow[workflow.index('          candidate_warmup_args=()'):]
    block = block.split("      - name:", 1)[0]
    assert '"${WORKFLOW_TARGET:-configured}" == "configured"' in block
    assert '"${#target_services[@]}" -ne 1' in block
    assert '"${target_services[0]}" != "${INPUT_CONFIGURED_SERVICE:-}"' in block
    assert 'python -m scripts.cloud_run_candidate_warmup' in block
    assert '--min-instances=1' not in block
    assert 'timeout 360s gcloud run deploy' in block
    assert '"${deploy_args[@]}"' in block
    assert '--no-traffic' in block
    assert '--min=' not in block


@pytest.mark.parametrize("health_only", [False, True])
def test_integration_waits_without_traffic_then_routes_only_when_requested(health_only):
    cold = candidate()
    healthy = copy.deepcopy(cold)
    healthy["status"]["conditions"].append({"type": "ContainerHealthy", "status": "True"})
    writes = []
    reads = []
    def run(args, **kwargs):
        if "update-traffic" in args:
            writes.append(args)
            return ""
        if "services" in args:
            current = service()
            if writes: current["status"]["traffic"] = [{"revisionName": "unit-service-new", "percent": 100}]
            return current
        if "list" in args: return [cold]
        reads.append(args)
        return cold if len(reads) == 1 else healthy
    with mock.patch.object(rcr, "_run", side_effect=run), mock.patch.object(rcr.time, "sleep"), \
         mock.patch("scripts.runtime_target_continuity.verify_target_continuity"):
        rcr.ensure_latest_traffic(project="<PROJECT_ID>", region="unit-region",
            targets=[rcr.RuntimeTarget("unit-service")], expected_commit="abc", dry_run=False,
            **({"health_only": True, "expected_image_digest": "sha256:abc"} if health_only else {}))
    assert len(reads) == 4
    assert len(writes) == (0 if health_only else 1)


@pytest.mark.parametrize("condition", ["Ready", "ContainerReady", "ContainerHealthy"])
def test_strict_health_rejects_false_and_duplicate_conditions(condition):
    value = candidate()
    value["status"]["conditions"] += [{"type": "ContainerHealthy", "status": "True"},
                                       {"type": condition, "status": "False"}]
    with mock.patch("scripts.runtime_target_continuity.verify_target_continuity"):
        with pytest.raises(ValueError, match="failed or ambiguous"):
            verify_traffic_revision(value, expected_revision="unit-service-new", expected_commit="abc",
                                    require_startup_health=True)


def test_cloud_read_failure_does_not_export_provider_diagnostics(capsys):
    with mock.patch.object(rcr, "_run", side_effect=rcr.ReconcileError("<PROJECT_ID> private provider detail")):
        with pytest.raises(rcr.ReconcileError) as error:
            rcr._verify_deploy_candidate_with_wait(candidate(), project="<PROJECT_ID>",
                region="unit-region", service_name="unit-service", service=service(),
                revision="unit-service-new", commit="abc")
    assert str(error.value) == "candidate warmup cloud read failed: COMMAND_FAILED"
    assert error.value.__suppress_context__
    assert capsys.readouterr() == ("", "")


def test_workflow_checks_health_even_without_traffic_approval():
    from pathlib import Path
    workflow = Path(".github/workflows/sync-cloud-run-env-protected.yml").read_text()
    step = workflow.split("      - name: Verify no-traffic candidate startup health", 1)[1].split("      - name:", 1)[0]
    assert "traffic_shift_enabled" not in step
    assert "--verify-candidate-health-only" in step
    assert "timeout 360s uv run" in step
    assert '--expected-image-digest="${EXPECTED_IMAGE_DIGEST}"' in step
    assert workflow.index("Verify no-traffic candidate startup health") < workflow.index("Reconcile Cloud Run traffic")


@pytest.mark.parametrize("extra", [["--ensure-latest-traffic"], ["--delete-legacy-schedulers"],
    ["--dry-run"], ["--runtime-switch-only"], ["--env-sync-evidence-dir=x"]])
def test_health_only_cli_rejects_any_mutation_mode(extra):
    with mock.patch.object(rcr, "_run") as run:
        with pytest.raises(rcr.ReconcileError, match="cannot perform mutations"):
            rcr.main(["--platform=ibkr", "--project=<PROJECT_ID>", "--verify-candidate-health-only", *extra])
    run.assert_not_called()


@pytest.mark.parametrize("healthy", [False, True])
def test_restart_rechecks_health_of_already_serving_candidate_without_writes(healthy):
    value = candidate()
    if healthy: value["status"]["conditions"].append({"type": "ContainerHealthy", "status": "True"})
    current = service()
    current["status"]["traffic"] = [{"revisionName": "unit-service-new", "percent": 100}]
    with mock.patch.object(rcr, "_run") as run, \
         mock.patch("scripts.runtime_target_continuity.verify_target_continuity"):
        args = dict(project="<PROJECT_ID>", region="unit-region", service_name="unit-service",
                    service=current, revision="unit-service-new", commit="abc")
        if healthy: rcr._verify_deploy_candidate_with_wait(value, **args)
        else:
            with pytest.raises(ValueError, match="startup probe"):
                rcr._verify_deploy_candidate_with_wait(value, **args)
    run.assert_not_called()


@pytest.mark.parametrize("target,services,selector,success,minimum", [
    ("configured", ["unit-service"], "unit-service", True, True),
    ("configured", ["unit-service"], "different", False, False),
    ("configured", ["unit-service", "unit-other"], "unit-service", False, False),
    ("hk-verify", ["unit-service"], "unit-service", True, False)])
def test_workflow_executes_minimum_only_for_exact_selected_target(target, services, selector, success, minimum):
    import os
    import shlex
    import subprocess
    import textwrap
    from pathlib import Path
    workflow = Path(".github/workflows/sync-cloud-run-env-protected.yml").read_text()
    block = workflow.split('          candidate_warmup_args=()', 1)[1].split("      - name:", 1)[0]
    script = """set -eu
    gcloud() { printf '%s\\n' "$@"; }
    timeout() { shift; "$@"; }
    uv() { if [[ "${*: -1}" == --eligible ]]; then if [[ "$WORKFLOW_TARGET" == configured ]]; then echo true; else echo false; fi; else printf '%s\\n' --tag=cand-aaaaaaaa --min-instances=1; fi; }
    """ + "target_services=(" + " ".join(shlex.quote(value) for value in services) + ")\n"
    script += textwrap.dedent('          candidate_warmup_args=()' + block)
    env = {**os.environ, "WORKFLOW_TARGET": target, "INPUT_CONFIGURED_SERVICE": selector, "RUNNER_TEMP": "/tmp", "GITHUB_OUTPUT": "/dev/null"}
    for key in ("GCP_PROJECT_ID", "CLOUD_RUN_REGION", "immutable_image", "GCP_RUNTIME_SERVICE_ACCOUNT", "SOURCE_COMMIT", "GITHUB_RUN_ID"):
        env[key] = "fake-value"
    result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert (result.returncode == 0) is success, result.stderr
    assert ("--min-instances=1" in result.stdout) is minimum
    assert "--min=" not in result.stdout
    if not success: assert result.stdout == ""
