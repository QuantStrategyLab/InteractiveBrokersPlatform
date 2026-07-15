from __future__ import annotations

import re
from pathlib import Path

from scripts import build_cloud_run_env_sync_plan as sync_plan


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "sync-cloud-run-env.yml"


def test_configured_run_deadline_exceeds_cloud_run_timeout() -> None:
    scheduler = sync_plan._build_scheduler_plan(
        runtime_target={"execution_mode": "live", "scheduler": {}},
        target={},
        defaults={},
        env={},
        env_values={},
        per_service_mode=True,
    )

    workflow = WORKFLOW.read_text(encoding="utf-8")
    cloud_run_timeout = re.search(r"--timeout=(\d+)s", workflow)
    assert cloud_run_timeout is not None
    assert int(scheduler["attempt_deadline"].removesuffix("s")) > int(
        cloud_run_timeout.group(1)
    )


def test_run_deadline_is_plan_scoped_and_warmup_remains_60_seconds() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert 'str(scheduler.get("attempt_deadline") or "")' in workflow
    assert "main_attempt_deadline_args=()" in workflow
    assert 'main_attempt_deadline_args+=(--attempt-deadline="${main_attempt_deadline}")' in workflow
    assert workflow.count('"${main_attempt_deadline_args[@]}"') == 2
    assert workflow.count("--attempt-deadline=60s") == 2
