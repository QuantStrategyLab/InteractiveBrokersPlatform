"""No network or broker calls: guard the one-day manual entry point."""
from datetime import date

import pytest

from scripts import publish_primary_day_once as entry


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(entry.identity, "resolve_expected", lambda: {
        "expected_report_prefix": "gs://fixture/reports/tqqq_growth_income"})
    return {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REPOSITORY": "QuantStrategyLab/InteractiveBrokersPlatform",
            "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_ATTEMPT": "1", "IBKR_ACCOUNT_FACTS_TARGET": "primary-live",
            "IBKR_RUNTIME_DAILY_BUSINESS_DATE": "2026-10-28",
            "IBKR_RUNTIME_DAILY_TARGET_KEY": "fixture|tqqq_growth_income|fixture|" + "a" * 32}


def test_primary_trading_day_accepted(env):
    entry.preflight(env, today=date(2026, 10, 29))


@pytest.mark.parametrize("key,value", [
    ("GITHUB_EVENT_NAME", "schedule"), ("GITHUB_RUN_ATTEMPT", "2"),
    ("IBKR_ACCOUNT_FACTS_TARGET", "additional1"), ("GITHUB_REF", "refs/heads/other"),
    ("IBKR_RUNTIME_DAILY_BUSINESS_DATE", "2026-10-31"),
    ("IBKR_RUNTIME_DAILY_BUSINESS_DATE", "2025-12-25"),
    ("IBKR_RUNTIME_DAILY_BUSINESS_DATE", "2026-10-30"),
    ("IBKR_RUNTIME_DAILY_TARGET_KEY", "fixture|other|fixture|" + "a" * 32),
])
def test_invalid_dispatch_stops_before_publication(env, monkeypatch, key, value, capsys):
    env[key] = value
    monkeypatch.setattr(entry, "preflight", lambda actual: original_preflight(actual, today=date(2026, 10, 29)))
    monkeypatch.setattr(entry.os, "environ", env)
    monkeypatch.setattr(entry.publisher, "main", lambda: pytest.fail("unexpected publish"))
    assert entry.main() == 1
    assert "blocked" in capsys.readouterr().out


original_preflight = entry.preflight
