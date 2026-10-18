"""Exercise workflow CLI commands without editable installs or inherited credentials."""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github/workflows/execution-report-heartbeat.yml").read_text()
SELECT_COMMANDS = re.findall(
    r"(?ms)^      - name: Select protected additional target before cloud authentication\n"
    r"(?:(?!^      - name:).)*?^        run: (.+?)$",
    WORKFLOW,
)
DIGEST_WORKFLOW = (ROOT / ".github/workflows/digest-candidates-emit.yml").read_text()
DIGEST_SELECT_COMMANDS = re.findall(
    r"(?ms)^      - name: Select protected additional target before cloud authentication\n"
    r"(?:(?!^      - name:).)*?^        run: (.+?)$",
    DIGEST_WORKFLOW,
)
ALL_SELECT_COMMANDS = SELECT_COMMANDS + DIGEST_SELECT_COMMANDS
PUBLISH_COMMANDS = re.findall(
    r"(?m)^      - name: Publish one validated report\n        run: (.+)$", WORKFLOW
)


@pytest.fixture
def cli_workspace(tmp_path):
    # Only these stdlib-only modules are copied; no installed project can hide a
    # missing package root, and no application/broker code is loaded.
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in (
        "__init__.py", "console_config.py", "configure_account_facts_target.py",
        "publish_account_facts_from_report.py",
    ):
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "synthetic-account-facts-cli"\nversion = "0.0.0"\n'
        'requires-python = ">=3.11"\n[tool.uv]\npackage = false\n'
    )
    environment = tmp_path / ".venv"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
    site_packages = environment / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    blocked = tmp_path / "external-call-attempted"
    # Even a regression in the CLI cannot contact cloud, HTTP or broker services.
    (site_packages / "sitecustomize.py").write_text(
        "import pathlib, sys\n"
        "def deny_external(event, args):\n"
        "    if event.startswith('socket.') or event in ('subprocess.Popen', 'os.system'):\n"
        f"        pathlib.Path({str(blocked)!r}).write_text(event)\n"
        "        raise RuntimeError('external calls forbidden in synthetic CLI test')\n"
        "sys.addaudithook(deny_external)\n"
    )
    uv = shutil.which("uv")
    assert uv is not None, "uv is required to exercise the workflow command"
    env = {
        "PATH": os.pathsep.join((str(environment / "bin"), str(Path(uv).parent), os.defpath)),
        "UV_NO_CONFIG": "1",
        "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
        "UV_OFFLINE": "1",
        "UV_PYTHON_DOWNLOADS": "never",
        "GITHUB_ENV": str(tmp_path / "github-env"),
        "IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON": '["U00000001"]',
        "IBKR_ACCOUNT_FACTS_TARGET_ID": "synthetic-primary",
        "IBKR_ACCOUNT_FACTS_REPORT_PREFIX": "gs://example-bucket/reports/primary",
    }
    targets = {
        f"additional-{index}": {
            "project_id": f"example-project-{index}",
            "report_prefix": f"gs://example-bucket/reports/additional-{index}",
            "target_id": f"synthetic-target-{index}",
            "service_name": f"synthetic-service-{index}",
            "runtime_revision": f"synthetic-revision-{index}",
            "account_scope": f"synthetic-scope-{index}",
            "account_selector": [f"U0000001{index}"],
            "deployment_selector": f"synthetic-deployment-{index}",
        }
        for index in range(1, 4)
    }

    def run(command, *, target, config=targets):
        child_env = {**env, "IBKR_ACCOUNT_FACTS_TARGET": target}
        if config is not None:
            child_env["IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON"] = json.dumps(config)
        result = subprocess.run(
            shlex.split(command), cwd=tmp_path, env=child_env,
            capture_output=True, text=True, check=False, timeout=30,
        )
        assert not blocked.exists(), "CLI attempted an external call"
        return result

    return run, Path(env["GITHUB_ENV"]), targets


def test_workflow_has_both_selector_steps_and_one_publisher():
    assert len(SELECT_COMMANDS) == 2
    assert len(DIGEST_SELECT_COMMANDS) == 1
    assert len(PUBLISH_COMMANDS) == 1


@pytest.mark.parametrize("command", ALL_SELECT_COMMANDS)
@pytest.mark.parametrize("index", [1, 2, 3])
def test_workflow_selector_cli_exports_only_selected_project(cli_workspace, command, index):
    run, env_path, _ = cli_workspace
    result = run(command, target=f"additional-{index}")

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"::add-mask::example-project-{index}\n"
    assert env_path.read_text().splitlines() == [
        f"GCP_PROJECT_ID=example-project-{index}",
        f"IBKR_ACCOUNT_FACTS_PROJECT_ID=example-project-{index}",
    ]


@pytest.mark.parametrize("failure", ["missing", "duplicate_identity", "primary_collision", "wrong_target"])
@pytest.mark.parametrize("command", ALL_SELECT_COMMANDS)
def test_workflow_selector_cli_rejects_invalid_config_without_export(cli_workspace, failure, command):
    run, env_path, config = cli_workspace
    target = "additional-1"
    if failure == "missing":
        config = None
    elif failure == "duplicate_identity":
        config["additional-2"]["account_selector"] = config["additional-1"]["account_selector"]
    elif failure == "primary_collision":
        config["additional-1"]["account_selector"] = ["U00000001"]
    else:
        target = "primary-live"

    result = run(command, target=target, config=config)

    assert result.returncode == 1, result.stderr
    reason = "target_config_unavailable" if failure == "missing" else "target_config_invalid"
    assert result.stdout == f"skipped:{reason}\n"
    assert not env_path.exists()


@pytest.mark.parametrize("target,code,reason", [
    ("disabled", 0, "target_disabled"),
    ("primary-live", 1, "target_config_unavailable"),
])
def test_workflow_publisher_cli_imports_and_fails_closed(cli_workspace, target, code, reason):
    run, env_path, _ = cli_workspace
    result = run(PUBLISH_COMMANDS[0], target=target)

    assert result.returncode == code, result.stderr
    assert result.stdout == f"skipped:{reason}\n"
    assert not env_path.exists()
