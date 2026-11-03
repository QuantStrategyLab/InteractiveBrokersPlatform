"""Protected provider propagation and network-free preflight regressions."""
import json
import subprocess
from pathlib import Path

import pytest

from scripts import capture_pilot_release_source as capture
from scripts import deploy_pilot_no_traffic as pilot
from scripts import diagnose_pilot_serving_install as diagnose
from tests.test_pilot_no_traffic_release import environment

ROOT = Path(__file__).resolve().parents[1]
PROVIDER = "projects/123/locations/global/workloadIdentityPools/dummy-pool/providers/dummy-provider"
CALLERS = [
    (pilot, "deploy-pilot-no-traffic", "release", "preflight_only"),
    (capture, "capture-pilot-release-source", "capture", "preflight"),
    (diagnose, "diagnose-pilot-serving-install", "diagnose", "preflight"),
]


@pytest.fixture
def private_context(monkeypatch, tmp_path):
    for key, value in environment().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GCP_WORKLOAD_IDENTITY_PROVIDER", PROVIDER)
    destination = tmp_path / "action-env"
    monkeypatch.setenv("GITHUB_ENV", str(destination))
    monkeypatch.delenv("PILOT_CAPTURE_OPERATION", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("preflight must remain offline"))
    return destination


@pytest.mark.parametrize("module,workflow,job,status", CALLERS)
def test_migrated_provider_reaches_each_preflight_without_public_output(
    module, workflow, job, status, private_context, tmp_path, capsys,
):
    migrated = (ROOT / ".github/workflows/sync-cloud-run-env-protected.yml").read_text()
    assert "  GCP_WORKLOAD_IDENTITY_PROVIDER: ${{ secrets.GCP_WORKLOAD_IDENTITY_PROVIDER }}" in migrated
    receipt = tmp_path / "receipt.json"
    assert module.main(["--preflight", "--bootstrap-identity", "--receipt", str(receipt)]) == 0
    assert json.loads(receipt.read_text())["status"] == status
    assert private_context.read_text() == f"PILOT_WIF_PROVIDER={PROVIDER}\n"
    output = capsys.readouterr()
    assert PROVIDER not in output.out + output.err + receipt.read_text()


@pytest.mark.parametrize("module,workflow,job,status", CALLERS)
@pytest.mark.parametrize("binding", [None, "", "${{ secrets.GCP_WORKLOAD_IDENTITY_PROVIDER }}", "wrong-provider", PROVIDER + "\nINJECT=value"])
def test_each_preflight_refuses_unavailable_or_invalid_binding(
    module, workflow, job, status, binding, private_context, monkeypatch, tmp_path, capsys,
):
    if binding is None:
        monkeypatch.delenv("GCP_WORKLOAD_IDENTITY_PROVIDER")
    else:
        monkeypatch.setenv("GCP_WORKLOAD_IDENTITY_PROVIDER", binding)
    receipt = tmp_path / "receipt.json"
    assert module.main(["--preflight", "--bootstrap-identity", "--receipt", str(receipt)]) == 1
    result = json.loads(receipt.read_text())
    assert result.get("stage", result.get("reason")) == "identity_source_unavailable"
    assert not private_context.exists()
    output = capsys.readouterr()
    assert PROVIDER not in output.out + output.err + receipt.read_text()


@pytest.mark.parametrize("module,workflow,job,status", CALLERS)
def test_each_preflight_retains_target_admission(
    module, workflow, job, status, private_context, monkeypatch, tmp_path,
):
    monkeypatch.setenv("PILOT_DEPLOY_GATE", "false")
    assert module.main(["--preflight", "--bootstrap-identity", "--receipt", str(tmp_path / "receipt.json")]) == 1
    assert not private_context.exists()


@pytest.mark.parametrize("module,workflow,job,status", CALLERS)
def test_each_workflow_passes_required_provider_through_protected_secret(module, workflow, job, status):
    wrapper = (ROOT / f".github/workflows/{workflow}.yml").read_text()
    protected = (ROOT / f".github/workflows/{workflow}-protected.yml").read_text()
    binding = "GCP_WORKLOAD_IDENTITY_PROVIDER"
    expression = "${{ secrets." + binding + " }}"
    assert f"      {binding}: {expression}" in wrapper.split("    secrets:\n", 1)[1]
    assert f"      {binding}:\n        required: true" in protected.split("permissions:", 1)[0]
    preflight = protected.split("env: &private_context\n", 1)[1].split("      - name:", 1)[0]
    assert f"          {binding}: {expression}" in preflight
    assert "--bootstrap-identity" in preflight
    assert "workload_identity_provider: ${{ env.PILOT_WIF_PROVIDER }}" in protected
    assert protected.index("--preflight") < protected.index("/auth@v3")
