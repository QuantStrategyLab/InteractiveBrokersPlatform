from __future__ import annotations

import re
from pathlib import Path

import pytest

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
    assert sync_plan.CLOUD_RUN_REQUEST_TIMEOUT_SECONDS == int(cloud_run_timeout.group(1))
    assert (
        int(scheduler["attempt_deadline"].removesuffix("s"))
        > sync_plan.CLOUD_RUN_REQUEST_TIMEOUT_SECONDS
    )


def test_run_deadline_is_plan_scoped_and_warmup_remains_60_seconds() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert 'str(scheduler.get("attempt_deadline") or "")' in workflow
    assert "main_attempt_deadline_args=()" in workflow
    assert 'main_attempt_deadline_args+=(--attempt-deadline="${main_attempt_deadline}")' in workflow
    assert workflow.count('"${main_attempt_deadline_args[@]}"') == 2
    assert workflow.count("--attempt-deadline=60s") == 2


@pytest.mark.parametrize(
    "attempt_deadline",
    [None, "", "300s", "1801s", "330", "330.0s", "not-a-duration"],
)
def test_explicit_run_deadline_must_be_valid_and_exceed_cloud_run_timeout(
    attempt_deadline: object,
) -> None:
    with pytest.raises(ValueError, match="scheduler.attempt_deadline"):
        sync_plan._build_scheduler_plan(
            runtime_target={
                "execution_mode": "live",
                "scheduler": {"attempt_deadline": attempt_deadline},
            },
            target={},
            defaults={},
            env={},
            env_values={},
            per_service_mode=True,
        )


@pytest.mark.parametrize(
    ("dry_run_only", "expected_deadline"),
    [(False, "330s"), (True, None)],
)
def test_omitted_execution_mode_uses_dry_run_only(
    dry_run_only: bool,
    expected_deadline: str | None,
) -> None:
    scheduler = sync_plan._build_scheduler_plan(
        runtime_target={"dry_run_only": dry_run_only, "scheduler": {}},
        target={},
        defaults={},
        env={},
        env_values={},
        per_service_mode=True,
    )

    assert scheduler.get("attempt_deadline") == expected_deadline
