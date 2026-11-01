"""Exercise scheduler enablement from the workflow without cloud access."""

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


@pytest.mark.parametrize("standard_execution_enabled", [False, True])
@pytest.mark.parametrize("drill_precheck_enabled", [False, True])
@pytest.mark.parametrize("initial_state", ["PAUSED", "ENABLED"])
def test_schedule_sync_enablement(
    tmp_path, standard_execution_enabled, drill_precheck_enabled, initial_state
):
    workflow = Path(".github/workflows/sync-cloud-run-env-protected.yml").read_text()
    step = workflow.split("- name: Sync Cloud Scheduler schedule", 1)[1]
    # Execute the actual state reconciliation loop, excluding cloud setup and
    # schedule creation. Only the fake gcloud below is available on PATH.
    source = "managed_scheduler_jobs=" + step.split("managed_scheduler_jobs=", 1)[1]
    source = textwrap.dedent(source.split("\n          done\n", 1)[0])
    jobs = {
        "synthetic-main": standard_execution_enabled,
        "synthetic-warmup": standard_execution_enabled,
        "synthetic-precheck": drill_precheck_enabled,
    }
    state_path = tmp_path / "states.json"
    actions_path = tmp_path / "actions.json"
    state_path.write_text(json.dumps(dict.fromkeys(jobs, initial_state)))
    actions_path.write_text("[]")
    gcloud = tmp_path / "gcloud"
    gcloud.write_text(f"#!{sys.executable}\n" + textwrap.dedent("""\
        import json
        import os
        import sys
        from pathlib import Path

        states_path = Path(os.environ["FAKE_STATES"])
        actions_path = Path(os.environ["FAKE_ACTIONS"])
        states = json.loads(states_path.read_text())
        args = sys.argv[1:]
        assert args[:2] == ["scheduler", "jobs"], args
        action, job = args[2:4]
        assert job in states, args
        if action == "describe":
            print(states[job])
        elif action in ("resume", "pause"):
            actions = json.loads(actions_path.read_text())
            actions.append([action, job])
            actions_path.write_text(json.dumps(actions))
            states[job] = "ENABLED" if action == "resume" else "PAUSED"
            states_path.write_text(json.dumps(states))
        else:
            raise AssertionError(args)
        """))
    gcloud.chmod(0o755)
    result = subprocess.run(
        [shutil.which("bash"), "--noprofile", "--norc", "-c", "set -euo pipefail\n" + source],
        env={
            "PATH": str(tmp_path),
            "FAKE_STATES": str(state_path),
            "FAKE_ACTIONS": str(actions_path),
            "job_name": "synthetic-main",
            "warmup_job_name": "synthetic-warmup",
            "precheck_job_name": "synthetic-precheck",
            "standard_execution_enabled": str(standard_execution_enabled).lower(),
            "drill_precheck_enabled": str(drill_precheck_enabled).lower(),
            "GCP_PROJECT_ID": "synthetic-project",
            "scheduler_location": "synthetic-region",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    expected_states = {job: "ENABLED" if enabled else "PAUSED" for job, enabled in jobs.items()}
    assert json.loads(state_path.read_text()) == expected_states
    assert json.loads(actions_path.read_text()) == [
        ["resume" if enabled else "pause", job]
        for job, enabled in jobs.items()
        if expected_states[job] != initial_state
    ]
