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
        "STRATEGY_PLUGIN_ALLOWED_SIGNAL_PREFIXES",
    }
    for path in (root / ".github/workflows").glob("*.yml"):
        text = path.read_text()
        for job in re.split(r"(?m)^  [a-zA-Z0-9_-]+:\n", text)[1:]:
            if "    steps:\n" not in job or not any(f"vars.{name}" in job for name in variable_names):
                continue
            steps = job.split("    steps:\n", 1)[1]
            assert steps.startswith("      - name: Require infrastructure variables\n"), path.name
            guard = steps.split("\n      - ", 1)[0]
            names = set(re.findall(r"vars\.([A-Z_]+)", guard))
            assert names and names <= variable_names
            code = guard.split("        run: |\n", 1)[1]
            code = "\n".join(line[10:] for line in code.splitlines())
            configured = {name: "synthetic" for name in names}
            result = subprocess.run(["/bin/bash", "-c", code], env=configured, capture_output=True, text=True)
            assert result.returncode == 0
            for name in names:
                result = subprocess.run(["/bin/bash", "-c", code], env={k: v for k, v in configured.items() if k != name}, capture_output=True, text=True)
                assert result.returncode == 2
                assert f"{name} is required" in result.stderr
