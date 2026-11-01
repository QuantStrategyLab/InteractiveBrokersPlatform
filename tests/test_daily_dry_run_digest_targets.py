import copy
import json

import pytest

from scripts.daily_dry_run_digest import _targets, covers_market_weekdays
from scripts.build_cloud_run_env_sync_plan import (
    _build_target_plan,
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


@pytest.fixture(params=["global_etf_rotation", "russell_top50_leader_rotation"])
def non_drill_target(request):
    service = f"synthetic-{request.param.replace('_', '-')}-service"
    return {
        "service": service,
        "drill_precheck_enabled": False,
        # Retained drill metadata must not enroll an explicitly opted-out target.
        "drill_label": "Synthetic former drill",
        "include_reconciliation": True,
        "env": {"RUNTIME_TARGET_ENABLED": "false"},
        "runtime_target": {
            "service_name": service,
            "strategy_profile": request.param,
            "account_scope": "synthetic-scope",
            "execution_mode": "live",
            "dry_run_only": False,
            "scheduler": {
                "precheck_time": "45 9 * * 1-5",
                "timezone": "America/New_York",
            },
        },
    }


@pytest.mark.parametrize("wrapped", [False, True])
def test_global_etf_and_russell_opt_out_preserves_other_drill_targets(
    monkeypatch, non_drill_target, wrapped,
):
    enrolled = json.loads(_payload("45 9 * * 1-5"))[0]
    entries = [non_drill_target, enrolled]
    payload = {"targets": entries} if wrapped else entries
    monkeypatch.setenv("CLOUD_RUN_SERVICE_TARGETS_JSON", json.dumps(payload))

    targets = _targets()

    assert [(target.service, target.profile) for target in targets] == [
        (enrolled["service"], enrolled["runtime_target"]["strategy_profile"]),
    ]


def _plan_non_drill_target(target):
    return _build_target_plan(
        target=target,
        defaults={
            "GLOBAL_TELEGRAM_CHAT_ID": "synthetic-chat",
            "NOTIFY_LANG": "en",
            "IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME": "synthetic-account-groups",
        },
        env={},
        status_rows={},
        per_service_mode=True,
        platform_config={},
    )


def test_global_etf_and_russell_opt_out_skips_drill_only_enrollment(non_drill_target):
    before = copy.deepcopy(non_drill_target)

    plan = _plan_non_drill_target(non_drill_target)

    assert plan["drill_precheck_enabled"] is False
    assert plan["env"]["RUNTIME_TARGET_ENABLED"] == "false"
    assert plan["standard_execution_enabled"] is False
    assert plan["scheduler"]["precheck_time"] == "45 9 * * 1-5"
    assert non_drill_target == before


def test_global_etf_and_russell_explicit_opt_in_still_requires_drill_only_runtime(
    non_drill_target,
):
    non_drill_target["drill_precheck_enabled"] = True

    with pytest.raises(ValueError, match="daily drill requires disabled RECONCILE_ONLY runtime"):
        _plan_non_drill_target(non_drill_target)
