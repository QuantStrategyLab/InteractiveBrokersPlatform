import json

import pytest

from scripts.daily_dry_run_digest import _targets, covers_market_weekdays
from scripts.build_cloud_run_env_sync_plan import (
    _covers_market_weekdays as plan_covers_market_weekdays,
)


def _payload(schedule: str) -> str:
    return json.dumps([
        {
            "service": "interactive-brokers-live-service",
            "drill_precheck_enabled": True,
            "runtime_target": {
                "service_name": "interactive-brokers-live-service",
                "strategy_profile": "tqqq_growth_income",
                "account_scope": "live",
                "scheduler": {
                    "precheck_time": schedule,
                    "timezone": "America/New_York",
                },
            },
        }
    ])


@pytest.mark.parametrize(
    "schedule",
    (
        "45 9 * * 1-5",
        "45 9 * * *",
        "45 9 * * 1,2,3,4,5",
        "45 9 * * 0-6",
    ),
)
def test_targets_accepts_schedules_covering_market_weekdays(monkeypatch, schedule):
    monkeypatch.setenv("CLOUD_RUN_SERVICE_TARGETS_JSON", _payload(schedule))

    targets = _targets()

    assert [target.schedule for target in targets] == [schedule]


@pytest.mark.parametrize(
    "schedule",
    (
        "45 9 * * 2-5",
        "45 9 * * 6,0",
        "45 9 * * 1,3,5",
        "45 9 * *",
        "45 9 * * 1-5/2",
        "90 9 * * 1-5",
        "45 9 * * MON-FRI",
    ),
)
def test_targets_rejects_schedules_without_full_weekday_coverage(monkeypatch, schedule):
    monkeypatch.setenv("CLOUD_RUN_SERVICE_TARGETS_JSON", _payload(schedule))

    with pytest.raises(ValueError):
        _targets()


@pytest.mark.parametrize(
    ("field", "expected"),
    (
        ("*", True),
        ("1-5", True),
        ("MON", False),
        ("0-6", True),
        ("1-5/2", False),
        ("*/2", False),
        ("", False),
        ("5-1", False),
    ),
)
def test_covers_market_weekdays(field, expected):
    assert covers_market_weekdays(field) is expected
    assert plan_covers_market_weekdays(field) is expected
