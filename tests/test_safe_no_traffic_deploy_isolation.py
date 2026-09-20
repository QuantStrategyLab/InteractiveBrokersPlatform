"""Regression: IBKR deploy must isolate traffic and scheduler from default prep."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "sync-cloud-run-env.yml"
READBACK = ROOT / "scripts" / "verify_cloud_run_no_traffic_deploy.py"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _deploy_block(workflow: str) -> str:
    return workflow.split('gcloud run deploy "${cloud_run_service}"', 1)[1].split("--quiet", 1)[0]


def _env_update_block(workflow: str) -> str:
    marker = 'run services update "${cloud_run_service}"'
    return workflow.split(marker, 1)[1].split("gcloud \"${gcloud_args[@]}\"", 1)[0]


def test_image_deploy_is_no_traffic_only() -> None:
    workflow = _workflow()
    deploy = _deploy_block(workflow)
    assert "--no-traffic" in deploy
    assert "--to-latest" not in deploy
    assert "--to-revisions" not in deploy


def test_env_sync_does_not_imply_traffic_or_scheduler() -> None:
    workflow = _workflow()
    assert "      approve_traffic_shift:" in workflow
    assert "      approve_scheduler_sync:" in workflow
    assert '        default: false\n' in workflow.split("      approve_traffic_shift:", 1)[1].split(
        "      approve_scheduler_sync:", 1
    )[0]
    assert '        default: false\n' in workflow.split("      approve_scheduler_sync:", 1)[1].split(
        "\nenv:", 1
    )[0]

    assert "traffic_shift_enabled=false" in workflow
    assert "scheduler_sync_enabled=false" in workflow
    assert 'steps.config.outputs.traffic_shift_enabled == \'true\'' in workflow
    assert 'steps.config.outputs.scheduler_sync_enabled == \'true\'' in workflow

    traffic_step = workflow.split("      - name: Reconcile Cloud Run traffic\n", 1)[1]
    traffic_header = traffic_step.split("        run:", 1)[0]
    assert "traffic_shift_enabled" in traffic_header

    scheduler_step = workflow.split("      - name: Sync Cloud Scheduler schedule\n", 1)[1]
    scheduler_header = scheduler_step.split("        run:", 1)[0]
    assert "scheduler_sync_enabled" in scheduler_header


def test_ensure_latest_traffic_is_not_tied_to_env_sync_alone() -> None:
    workflow = _workflow()
    traffic_step = workflow.split("      - name: Reconcile Cloud Run traffic\n", 1)[1].split(
        "      - name:", 1
    )[0]
    assert "--ensure-latest-traffic" in traffic_step
    assert "traffic_shift_enabled" in traffic_step.split("        run:", 1)[0]
    # Default env-sync path must not auto-cut traffic.
    env_sync_gate = workflow.split("      - name: Sync Cloud Run environment\n", 1)[1].split(
        "      - name: Reconcile Cloud Run traffic\n", 1
    )[0]
    assert "--ensure-latest-traffic" not in env_sync_gate


def test_env_update_keeps_candidate_off_traffic() -> None:
    workflow = _workflow()
    assert "--no-traffic" in _env_update_block(workflow)


def test_configured_requires_exact_service_before_mutations() -> None:
    workflow = _workflow()
    assert 'configured_service requires an exact private inventory service_name' in workflow
    # Blank configured_service must not silently expand to every inventory target.
    assert 'blank keeps all configured targets' not in workflow.lower()
    assert 'description: "Exact inventory service_name required for configured target."' in workflow


def test_no_traffic_deploy_has_capture_verify_and_fail_closed_script() -> None:
    workflow = _workflow()
    assert "Capture no-traffic deployment baseline" in workflow
    assert "Verify no-traffic deployment readback" in workflow
    assert "scripts/verify_cloud_run_no_traffic_deploy.py capture" in workflow
    assert "scripts/verify_cloud_run_no_traffic_deploy.py verify" in workflow
    assert READBACK.is_file()
    readback = READBACK.read_text(encoding="utf-8")
    assert 'for key in ("traffic", "scheduler", "iam", "configuration"):' in readback
    assert 'raise RuntimeError(f"{key} changed during no-traffic deployment")' in readback
    assert "secrets versions access" not in readback
    assert "containers.env.value," not in readback


def test_scheduler_pause_resume_requires_explicit_approval_and_state_readback() -> None:
    workflow = _workflow()
    scheduler_step = workflow.split("      - name: Sync Cloud Scheduler schedule\n", 1)[1].split(
        "      - name: Prune old Cloud Run revisions\n", 1
    )[0]
    assert "scheduler_sync_enabled" in scheduler_step.split("        run:", 1)[0]
    assert 'gcloud scheduler jobs resume "${managed_job_name}"' in scheduler_step
    assert 'gcloud scheduler jobs pause "${managed_job_name}"' in scheduler_step
    # Fail-closed readback after explicit start/stop.
    assert 'managed_job_state_after=' in scheduler_step or "state readback" in scheduler_step.lower()
    assert 'format=\'value(state)\'' in scheduler_step
