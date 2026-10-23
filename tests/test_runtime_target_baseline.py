import copy
import json
from pathlib import Path

import pytest

from quant_platform_kit.common.live_continuity import runtime_target_fingerprint
from scripts import verify_runtime_target_baseline as guard


def target():
    value = {
        "service_name": "unit-service",
        "strategy_release": {"strategy_revision": "a" * 40},
        "runtime_risk_limits": {"binding": {"ues_revision": "a" * 40}},
        "scheduler": {"precheck_time": "0 12 * * 1-5"},
    }
    value["live_continuity"] = {
        "state": "PAUSED", "baseline_kind": "legacy_authorized",
        "baseline_id": "synthetic-baseline", "captured_at": "2026-10-01",
        "baseline_target_sha256": runtime_target_fingerprint(value),
    }
    return value


def plan(value):
    return {"targets": [{"service_name": "unit-service", "env": {
        "RUNTIME_TARGET_JSON": json.dumps(value), "RUNTIME_TARGET_ENABLED": "false",
    }}]}


def service(value):
    return {"spec": {"template": {"spec": {"containers": [{"env": [
        {"name": "RUNTIME_TARGET_JSON", "value": json.dumps(value)},
    ]}]}}}}


def test_matching_baseline_passes_without_mutation():
    payload = plan(target())
    before = copy.deepcopy(payload)
    assert guard.verify_plan(payload, deployed_reader=lambda _: service(target())) == 1
    assert payload == before


@pytest.mark.parametrize("field", ["strategy_release", "runtime_risk_limits", "scheduler"])
@pytest.mark.parametrize("source", ["plan", "deployed"])
def test_rca_target_changes_require_matching_baseline(field, source):
    changed = target()
    changed[field] = {"changed": True}
    payload = plan(changed if source == "plan" else target())
    with pytest.raises(ValueError, match="continuity"):
        guard.verify_plan(payload, deployed_reader=lambda _: service(changed))
    # Explicit, external baseline update makes the exact same target consistent.
    changed["live_continuity"]["baseline_target_sha256"] = runtime_target_fingerprint(changed)
    assert guard.verify_plan(plan(changed), deployed_reader=lambda _: service(changed)) == 1


@pytest.mark.parametrize("failure", ["continuity", "null", "digest", "empty", "malformed"])
@pytest.mark.parametrize("source", ["plan", "deployed"])
def test_missing_baseline_fails_even_when_runtime_disabled(failure, source):
    value = target()
    if failure == "continuity":
        del value["live_continuity"]
    elif failure == "null":
        value["live_continuity"] = None
    elif failure == "digest":
        del value["live_continuity"]["baseline_target_sha256"]
    else:
        value["live_continuity"]["baseline_target_sha256"] = "" if failure == "empty" else "invalid"
    with pytest.raises(ValueError):
        guard.verify_plan(
            plan(value if source == "plan" else target()),
            deployed_reader=lambda _: service(value),
        )


@pytest.mark.parametrize("raw", [None, "", "{private-broken-json", "null", "[]"])
def test_unreadable_plan_target_fails(raw):
    payload = plan(target())
    payload["targets"][0]["env"]["RUNTIME_TARGET_JSON"] = raw
    with pytest.raises(ValueError):
        guard.verify_plan(payload)


@pytest.mark.parametrize("payload", [None, {}, {"targets": []}, {"targets": [None]}, {"targets": [{}]}])
def test_missing_plan_or_target_fails(payload):
    with pytest.raises(ValueError):
        guard.verify_plan(payload)


@pytest.mark.parametrize("failure", ["permission", "os", "invalid_json", "missing", "secret_ref"])
def test_cli_read_failure_returns_nonzero_without_private_output(monkeypatch, capsys, failure):
    monkeypatch.setenv("SYNC_PLAN_JSON", json.dumps(plan(target())))

    def reader(**kwargs):
        if failure == "permission":
            raise ValueError("private-cloud-error")
        if failure == "os":
            raise OSError("private-cloud-error")
        payload = service(target())
        env = payload["spec"]["template"]["spec"]["containers"][0]["env"]
        if failure == "invalid_json":
            env[0]["value"] = "private-broken-json"
        elif failure == "missing":
            env.clear()
        else:
            env[0] = {"name": "RUNTIME_TARGET_JSON", "valueFrom": {"secretKeyRef": {"name": "private-secret"}}}
        return payload

    monkeypatch.setattr(guard, "_describe_service", reader)
    assert guard.main(["--check-deployed", "--project=unit", "--region=unit"]) == 1
    output = capsys.readouterr()
    assert "preflight failed" in output.err
    assert "private" not in output.err + output.out


def test_cli_success_without_deployed_read_for_env_only(monkeypatch, capsys):
    monkeypatch.setenv("SYNC_PLAN_JSON", json.dumps(plan(target())))
    monkeypatch.setattr(guard, "_describe_service", lambda **_: pytest.fail("unexpected cloud read"))
    assert guard.main([]) == 0
    assert "preflight passed: 1" in capsys.readouterr().out


@pytest.mark.parametrize("raw", [None, "private-broken-json", "{}"])
def test_cli_unreadable_plan_returns_nonzero(monkeypatch, capsys, raw):
    if raw is None:
        monkeypatch.delenv("SYNC_PLAN_JSON", raising=False)
    else:
        monkeypatch.setenv("SYNC_PLAN_JSON", raw)
    assert guard.main([]) == 1
    assert "private" not in capsys.readouterr().err


def test_workflow_guard_covers_image_and_env_before_writes():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/sync-cloud-run-env-protected.yml").read_text()
    start = workflow.index("      - name: Verify runtime target baselines before deployment writes")
    step = workflow[start:workflow.index("      - name:", start + 1)]
    assert "if: steps.config.outputs.enabled == 'true'" in step
    assert "SYNC_PLAN_JSON: ${{ steps.strategy_requirements.outputs.sync_plan_json }}" in step
    assert "CHECK_DEPLOYED_TARGET: ${{ steps.config.outputs.deploy_enabled }}" in step
    assert "--check-deployed" in step
    assert "PYTHONPATH=. uv run --no-sync python scripts/verify_runtime_target_baseline.py" in step
    assert "set -euo pipefail" in step
    assert "continue-on-error" not in step
    for name in ["Build, push, and deploy Cloud Run image", "Sync Cloud Run environment", "Reconcile Cloud Run traffic", "Sync Cloud Scheduler schedule"]:
        assert start < workflow.index("      - name: " + name)
