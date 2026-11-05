"""Offline adverse checks for the deploy dry-run boundary."""
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/sync-cloud-run-env-protected.yml"


def _section(name):
    return WORKFLOW.read_text().split("      - name: " + name + "\n", 1)[1].split("\n      - name:", 1)[0]


def test_dry_run_default_and_forwarding():
    caller = (ROOT / ".github/workflows/sync-cloud-run-env.yml").read_text()
    assert "      dry_run:\n" in caller
    assert "dry_run: ${{ inputs.dry_run }}" in caller
    assert "      dry_run:\n        type: boolean\n        required: true" in WORKFLOW.read_text()


def test_environment_credentials_are_job_bound():
    text = WORKFLOW.read_text()
    job = text.split("  sync-cloud-run-env:\n", 1)[1].split("    steps:", 1)[0]
    assert "environment: ${{ vars.IBKR_DEPLOY_ENVIRONMENT }}" in job
    for name in ("GCP_PROJECT_ID", "GCP_WORKLOAD_IDENTITY_PROVIDER", "GCP_WORKLOAD_IDENTITY_SERVICE_ACCOUNT"):
        assert name + ": ${{ secrets." + name + " }}" in job


def test_every_post_validation_step_requires_explicit_execution():
    text = WORKFLOW.read_text().split("      - name: Capture no-traffic deployment baseline", 1)[1].split("\n  notification-only:", 1)[0]
    sections = [text.split("\n      - name:", 1)[0]] + text.split("\n      - name:")[1:]
    assert len(sections) >= 10
    for section in sections:
        condition = next(line.strip() for line in section.splitlines() if line.strip().startswith("if:"))
        assert "inputs.dry_run == false" in condition


def test_invalid_dry_run_fails_before_emitting_enablement(tmp_path):
    section = _section("Check whether Cloud Run automation is enabled")
    body = section.split("        run: |\n", 1)[1]
    script = "\n".join(line[10:] for line in body.splitlines())
    for value in ("unknown", "", "TRUE"):
        output = tmp_path / ("output-" + value)
        result = subprocess.run(["bash", "-euo", "pipefail", "-c", script], env=dict(os.environ, INPUT_DRY_RUN=value, GITHUB_EVENT_NAME="workflow_dispatch", ENABLE_MAIN_PUSH_CLOUD_RUN_AUTOMATION="false", GITHUB_OUTPUT=str(output)), capture_output=True, text=True)
        assert result.returncode != 0
        assert not output.exists()


def test_dry_run_preserves_intended_controls_and_rejects_other_modes(tmp_path):
    body = _section("Check whether Cloud Run automation is enabled").split("        run: |\n", 1)[1]
    script = "\n".join(line[10:] for line in body.splitlines())
    common = dict(os.environ, INPUT_DRY_RUN="true", GITHUB_EVENT_NAME="workflow_dispatch", GITHUB_REF="refs/heads/main", ENABLE_MAIN_PUSH_CLOUD_RUN_AUTOMATION="false", ENABLE_GITHUB_CLOUD_RUN_DEPLOY="true", INPUT_DEPLOY_IMAGE="true", INPUT_SYNC_ENV="false", ENABLE_GITHUB_ENV_SYNC="false", INPUT_APPROVE_TRAFFIC_SHIFT="false", INPUT_APPROVE_SCHEDULER_SYNC="false", INPUT_CONFIGURED_SERVICE="fake-paper-service", INPUT_SOURCE_COMMIT="a" * 40)
    output = tmp_path / "valid"
    result = subprocess.run(["bash", "-euo", "pipefail", "-c", script], env=dict(common, GITHUB_OUTPUT=str(output)), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "deploy_enabled=true" in output.read_text()
    for extra in ({"INPUT_NOTIFICATION_ONLY": "true"}, {"INPUT_RUNTIME_SWITCH_ONLY": "true"}, {"ENABLE_GITHUB_CLOUD_RUN_DEPLOY": "false"}):
        result = subprocess.run(["bash", "-euo", "pipefail", "-c", script], env=dict(common, **extra, GITHUB_OUTPUT=str(tmp_path / "invalid")), capture_output=True, text=True)
        assert result.returncode != 0


def test_missing_environment_selector_blocks_before_authentication(tmp_path):
    body = _section("Validate protected deployment environment").split("        run: |\n", 1)[1]
    script = "\n".join(line[10:] for line in body.splitlines())
    for value, expected in (("", 1), ("fake-protected-environment", 0)):
        result = subprocess.run(["bash", "-euo", "pipefail", "-c", script], env=dict(os.environ, DEPLOY_ENVIRONMENT=value), capture_output=True, text=True)
        assert result.returncode == expected
        assert value not in result.stderr if value else True


def test_scheduler_only_dry_run_validates_required_inputs():
    text = WORKFLOW.read_text().split("      - name: Confirm deployment dry-run validation", 1)[0]
    sections = [section for section in text.split("      - name: ")
                if "if: steps.config.outputs.scheduler_sync_enabled == 'true'" in section]
    assert len(sections) == 1
    body = sections[0].split("        run: |\n", 1)[1]
    script = "\n".join(line[10:] for line in body.splitlines())
    common = {key: value for key, value in os.environ.items()
              if key not in {"CLOUD_RUN_REGION", "CLOUD_SCHEDULER_LOCATION", "GCP_SCHEDULER_SERVICE_ACCOUNT"}}
    cases = [({}, False),
             ({"CLOUD_RUN_REGION": "", "CLOUD_SCHEDULER_LOCATION": ""}, False),
             ({"CLOUD_RUN_REGION": "fake-region"}, False),
             ({"GCP_SCHEDULER_SERVICE_ACCOUNT": "fake-scheduler"}, False),
             ({"CLOUD_RUN_REGION": "fake-region", "GCP_SCHEDULER_SERVICE_ACCOUNT": "fake-scheduler"}, True),
             ({"CLOUD_SCHEDULER_LOCATION": "fake-location", "GCP_SCHEDULER_SERVICE_ACCOUNT": "fake-scheduler"}, True)]
    for extra, valid in cases:
        result = subprocess.run(["bash", "-euo", "pipefail", "-c", script],
                                env=dict(common, **extra), capture_output=True, text=True)
        assert (result.returncode == 0) == valid
        for value in extra.values():
            if value:
                assert value not in result.stderr
