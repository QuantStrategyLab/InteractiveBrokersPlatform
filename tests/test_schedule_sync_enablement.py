"""Exercise scheduler enablement from the workflow without cloud access."""

import shutil
import subprocess
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
    for job in jobs:
        (tmp_path / job).write_text(initial_state + "\n")
    actions_path = tmp_path / "actions.log"
    actions_path.write_text("")
    bash = shutil.which("bash")
    gcloud = tmp_path / "gcloud"
    gcloud.write_text(f"#!{bash}\n" + textwrap.dedent("""\
        set -euo pipefail
        [[ "$1 $2" == "scheduler jobs" ]]
        [[ -f "$FAKE_CLOUD/$4" ]]
        case "$3" in
          describe)
            read -r state < "$FAKE_CLOUD/$4"
            printf '%s\\n' "$state"
            ;;
          resume|pause)
            printf '%s %s\\n' "$3" "$4" >> "$FAKE_ACTIONS"
            state=PAUSED
            if [[ "$3" == resume ]]; then state=ENABLED; fi
            printf '%s\\n' "$state" > "$FAKE_CLOUD/$4"
            ;;
          *) exit 1 ;;
        esac
        """))
    gcloud.chmod(0o755)
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-c", "set -euo pipefail\n" + source],
        env={
            "PATH": str(tmp_path),
            "FAKE_CLOUD": str(tmp_path),
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
    assert {job: (tmp_path / job).read_text().strip() for job in jobs} == expected_states
    assert actions_path.read_text().splitlines() == [
        f"{'resume' if enabled else 'pause'} {job}"
        for job, enabled in jobs.items()
        if expected_states[job] != initial_state
    ]
