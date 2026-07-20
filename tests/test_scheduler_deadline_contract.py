from __future__ import annotations

import re
from pathlib import Path

from scripts import build_cloud_run_env_sync_plan as sync_plan


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "sync-cloud-run-env.yml"


def test_live_gateway_run_deadline_exceeds_cloud_run_timeout() -> None:
    assert sync_plan.RUN_SCHEDULER_ATTEMPT_DEADLINE == "330s"

    assert sync_plan._requires_extended_run_deadline(
        {"execution_mode": "live", "dry_run_only": False},
        {"IBKR_EXECUTION_BACKEND": "gateway"},
    )
    assert not sync_plan._requires_extended_run_deadline(
        {"execution_mode": "paper", "dry_run_only": True},
        {"IBKR_EXECUTION_BACKEND": "gateway"},
    )
    assert not sync_plan._requires_extended_run_deadline(
        {"execution_mode": "live", "dry_run_only": False},
        {"IBKR_EXECUTION_BACKEND": "quantconnect"},
    )

    workflow = WORKFLOW.read_text(encoding="utf-8")
    cloud_run_timeout = re.search(r"--timeout=(\d+)s", workflow)
    assert cloud_run_timeout is not None
    assert int(sync_plan.RUN_SCHEDULER_ATTEMPT_DEADLINE.removesuffix("s")) > int(
        cloud_run_timeout.group(1)
    )


def test_run_deadline_is_plan_scoped_and_warmup_remains_60_seconds() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert 'str(scheduler.get("attempt_deadline") or "")' in workflow
    assert "main_attempt_deadline_args=()" in workflow
    assert 'main_attempt_deadline_args+=(--attempt-deadline="${main_attempt_deadline}")' in workflow
    assert workflow.count('"${main_attempt_deadline_args[@]}"') == 2
    assert workflow.count("--attempt-deadline=60s") == 2
