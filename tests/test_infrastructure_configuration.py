"""Synthetic configuration checks with no cloud or broker access."""

import importlib
import re
import subprocess
from pathlib import Path

import pytest

from scripts import console_config
from scripts import verify_cloud_run_strategy_plugin_mounts as mounts


@pytest.mark.parametrize("value", ["", "http://example.invalid", "https://user@example.invalid", "https://example.invalid/path", "https://example.invalid?query=1", "https://example.invalid#fragment"])
def test_console_origin_rejects_unsafe_values(monkeypatch, value):
    monkeypatch.setenv("QSL_CONSOLE_BASE_URL", value)
    with pytest.raises(ValueError, match="QSL_CONSOLE_BASE_URL"):
        console_config.console_base_url()


def test_console_default_and_configured_publish_paths(monkeypatch):
    from scripts import publish_account_facts_from_report as facts
    from scripts import publish_ibkr_flex_period_return as period

    monkeypatch.delenv("QSL_CONSOLE_BASE_URL", raising=False)
    assert console_config.console_base_url() == "https://example.invalid"
    try:
        monkeypatch.setenv("QSL_CONSOLE_BASE_URL", "https://console.example.invalid/")
        importlib.reload(facts)
        importlib.reload(period)
        assert facts.IBKR_ACCOUNT_FACTS_SYNC_URL == "https://console.example.invalid/api/account-facts/sync"
        assert period.SYNC_URL == "https://console.example.invalid/api/account-facts/period-return/sync"
    finally:
        monkeypatch.delenv("QSL_CONSOLE_BASE_URL", raising=False)
        importlib.reload(facts)
        importlib.reload(period)


def test_signal_prefixes_require_explicit_configuration(monkeypatch):
    monkeypatch.delenv("STRATEGY_PLUGIN_ALLOWED_SIGNAL_PREFIXES", raising=False)
    with pytest.raises(ValueError, match="STRATEGY_PLUGIN_ALLOWED_SIGNAL_PREFIXES is required"):
        mounts._allowed_signal_prefixes()
    monkeypatch.setenv("STRATEGY_PLUGIN_ALLOWED_SIGNAL_PREFIXES", "gs://example-shared/,gs://example-runtime/")
    assert mounts._allowed_signal_prefixes() == ("gs://example-shared/", "gs://example-runtime/")


def test_workflow_variables_are_checked_before_cloud_actions():
    root = Path(__file__).resolve().parents[1]
    variable_names = {
        "IBKR_GCP_PROJECT_ID", "IBKR_DEPLOY_SERVICE_ACCOUNT", "IBKR_RUNTIME_SERVICE_ACCOUNT",
        "IBKR_SCHEDULER_SERVICE_ACCOUNT", "QSL_CONSOLE_BASE_URL", "QSL_DIGEST_BUCKET",
        "STRATEGY_PLUGIN_ALLOWED_SIGNAL_PREFIXES", "GCP_PROJECT_ID",
        "GCP_WORKLOAD_IDENTITY_SERVICE_ACCOUNT",
    }
    for path in (root / ".github/workflows").glob("*.yml"):
        text = path.read_text()
        for job in re.split(r"(?m)^  [a-zA-Z0-9_-]+:\n", text)[1:]:
            if "    steps:\n" not in job or not any(f"vars.{name}" in job for name in variable_names):
                continue
            steps = job.split("    steps:\n", 1)[1]
            assert steps.startswith("      - name: Require infrastructure variables\n"), path.name
            guard = steps.split("\n      - ", 1)[0]
            names = set(re.findall(r"(?:vars|secrets)\.([A-Z_]+)", guard))
            assert names and names <= variable_names
            code = guard.split("        run: |\n", 1)[1]
            code = "\n".join(line[10:] for line in code.splitlines())
            global_env = text.split("\nenv:\n", 1)[1].split("\njobs:\n", 1)[0] if "\nenv:\n" in text else ""
            bindings = dict(re.findall(r"^\s+([A-Z_]+): \$\{\{ (?:vars|secrets)\.([A-Z_]+) \}\}$", global_env + guard, re.MULTILINE))
            configured = {name: "dummy" for name in bindings}
            result = subprocess.run(["/bin/bash", "-c", code], env=configured, capture_output=True, text=True)
            assert result.returncode == 0
            for name in names:
                missing = {key: value for key, value in configured.items() if bindings[key] != name}
                result = subprocess.run(["/bin/bash", "-c", code], env=missing, capture_output=True, text=True)
                assert result.returncode == 2
                assert any(f"{key} is required" in result.stderr
                           for key, source in bindings.items() if source == name)



@pytest.mark.parametrize("missing_alias", [
    "GCP_PROJECT_ID", "GCP_WORKLOAD_IDENTITY_PROVIDER", "GCP_WORKLOAD_IDENTITY_SERVICE_ACCOUNT",
    "GCP_RUNTIME_SERVICE_ACCOUNT", "GCP_SCHEDULER_SERVICE_ACCOUNT",
])
def test_deploy_preflight_checks_consumed_infrastructure_aliases(missing_alias):
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github/workflows/sync-cloud-run-env-protected.yml").read_text()
    global_env = text.split("\nenv:\n", 1)[1].split("\njobs:\n", 1)[0]
    guard = text.split("      - name: Require infrastructure variables\n", 1)[1].split("\n      - ", 1)[0]
    # Resolve the workflow and step bindings together, as the runner does.
    bindings = dict(re.findall(r"^\s+([A-Z_]+): \$\{\{ (?:vars|secrets)\.([A-Z_]+) \}\}$", global_env + guard, re.MULTILINE))
    configured = {name: "synthetic" for name in bindings}
    assert missing_alias in configured
    code = guard.split("        run: |\n", 1)[1]
    code = "\n".join(line[10:] for line in code.splitlines())
    result = subprocess.run(["/bin/bash", "-c", code], env=configured, capture_output=True, text=True)
    assert result.returncode == 0
    configured[missing_alias] = ""
    result = subprocess.run(["/bin/bash", "-c", code], env=configured, capture_output=True, text=True)
    assert result.returncode == 2
    assert f"{missing_alias} is required" in result.stderr


def test_documented_canonical_secrets_satisfy_preflight_without_legacy_aliases():
    text = Path('.github/workflows/sync-cloud-run-env-protected.yml').read_text()
    global_env = text.split('\nenv:\n', 1)[1].split('\njobs:\n', 1)[0]
    guard = text.split('      - name: Require infrastructure variables\n', 1)[1].split('\n      - ', 1)[0]
    secrets = {name: 'dummy' for name in (
        'GCP_PROJECT_ID', 'GCP_WORKLOAD_IDENTITY_PROVIDER',
        'GCP_WORKLOAD_IDENTITY_SERVICE_ACCOUNT', 'IBKR_RUNTIME_SERVICE_ACCOUNT',
        'IBKR_SCHEDULER_SERVICE_ACCOUNT')}
    bindings = dict(re.findall(r'^\s+([A-Z_]+): \$\{\{ secrets\.([A-Z_]+) \}\}$', global_env + guard, re.MULTILINE))
    env = {key: secrets.get(value, '') for key, value in bindings.items()}
    env['STRATEGY_PLUGIN_ALLOWED_SIGNAL_PREFIXES'] = 'gs://dummy/'
    code = guard.split('        run: |\n', 1)[1]
    code = '\n'.join(line[10:] for line in code.splitlines())
    result = subprocess.run(['/bin/bash', '-c', code], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
