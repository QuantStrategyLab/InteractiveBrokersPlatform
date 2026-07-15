from __future__ import annotations

import re
from pathlib import Path

from scripts import build_cloud_run_env_sync_plan as sync_plan


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "sync-cloud-run-env.yml"


def test_four_gateway_run_deadline_exceeds_cloud_run_timeout() -> None:
    expected_services = {
        "interactive-brokers-quant-live-u15998061-service",
        "interactive-brokers-quant-live-u16608560-service",
        "interactive-brokers-quant-live-u18336562-service",
        "interactive-brokers-quant-live-u18308207-service",
    }
    assert sync_plan.NEAR_RUN_WARMUP_SERVICES == expected_services
    assert sync_plan.RUN_SCHEDULER_ATTEMPT_DEADLINE == "330s"

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
