from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts import sync_run_scheduler_deadlines as sync


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "sync-run-scheduler-deadlines.yml"
SCRIPT = ROOT / "scripts" / "sync_run_scheduler_deadlines.py"


def _job(*, name: str, deadline: str, method: str, path: str) -> dict[str, Any]:
    return {
        "name": f"projects/test/locations/us-central1/jobs/{name}",
        "state": "ENABLED",
        "attemptDeadline": deadline,
        "httpTarget": {
            "httpMethod": method,
            "uri": f"https://service.example.invalid{path}",
        },
    }


class FakeGcloud:
    def __init__(self) -> None:
        self.jobs = {
            name: _job(name=name, deadline="180s", method="POST", path="/run")
            for name in sync.RUN_JOB_NAMES
        }
        self.jobs.update(
            {
                name: _job(name=name, deadline="60s", method="GET", path="/health")
                for name in sync.WARMUP_JOB_NAMES
            }
        )
        self.commands: list[list[str]] = []

    def __call__(
        self,
        command: list[str],
        *,
        text: bool,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert text is True
        assert capture_output is True
        assert check is True
        self.commands.append(command)
        if command[1:4] == ["scheduler", "jobs", "describe"]:
            name = command[4]
            return subprocess.CompletedProcess(command, 0, json.dumps(self.jobs[name]), "")
        if command[1:5] == ["scheduler", "jobs", "update", "http"]:
            name = command[5]
            self.jobs[name]["attemptDeadline"] = "330s"
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(f"Unexpected command: {command!r}")


def test_fixed_allowlist_updates_only_four_run_deadlines() -> None:
    expected_run_jobs = {
        "interactive-brokers-quant-live-u15998061-scheduler",
        "interactive-brokers-quant-live-u16608560-scheduler",
        "interactive-brokers-quant-live-u18336562-scheduler",
        "interactive-brokers-quant-live-u18308207-scheduler",
    }
    expected_warmups = {name.replace("-scheduler", "-warmup-scheduler") for name in expected_run_jobs}
    assert set(sync.RUN_JOB_NAMES) == expected_run_jobs
    assert set(sync.WARMUP_JOB_NAMES) == expected_warmups

    gcloud = FakeGcloud()
    sync.sync_deadlines(project="test", location="us-central1", run_command=gcloud)

    update_commands = [command for command in gcloud.commands if "update" in command]
    assert {command[5] for command in update_commands} == expected_run_jobs
    assert len(update_commands) == 4
    assert all("--attempt-deadline=330s" in command for command in update_commands)
    assert all(command[1:5] == ["scheduler", "jobs", "update", "http"] for command in update_commands)
    assert all("run" not in command[1:5] for command in gcloud.commands)
    assert all(gcloud.jobs[name]["attemptDeadline"] == "60s" for name in expected_warmups)


def test_preflight_rejects_non_run_target_before_any_update() -> None:
    gcloud = FakeGcloud()
    first_run = sync.RUN_JOB_NAMES[0]
    gcloud.jobs[first_run]["httpTarget"]["uri"] = "https://service.example.invalid/health"

    with pytest.raises(sync.ConfigOnlySyncError, match="expected path /run"):
        sync.sync_deadlines(project="test", location="us-central1", run_command=gcloud)

    assert not any("update" in command for command in gcloud.commands)


def test_preflight_rejects_warmup_drift_before_any_update() -> None:
    gcloud = FakeGcloud()
    first_warmup = sync.WARMUP_JOB_NAMES[0]
    gcloud.jobs[first_warmup]["attemptDeadline"] = "61s"

    with pytest.raises(sync.ConfigOnlySyncError, match="expected deadline 60s"):
        sync.sync_deadlines(project="test", location="us-central1", run_command=gcloud)

    assert not any("update" in command for command in gcloud.commands)


def test_manual_workflow_has_no_broad_deploy_or_job_trigger_path() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "if: github.ref == 'refs/heads/main'" in workflow
    assert "python3 scripts/sync_run_scheduler_deadlines.py" in workflow
    for forbidden in (
        "workflow_run:",
        "schedule:",
        "gcloud run",
        "scheduler jobs run",
        "run deploy",
        "run services update",
    ):
        assert forbidden not in workflow
        assert forbidden not in script
