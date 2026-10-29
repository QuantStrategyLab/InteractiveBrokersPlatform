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
    ("GITHUB_EVENT_NAME", "push"), ("GITHUB_RUN_ATTEMPT", "2"),
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


def test_schedule_accepts_only_current_market_day(env):
    env["GITHUB_EVENT_NAME"] = "schedule"
    with pytest.raises(ValueError, match="date_invalid"):
        entry.preflight(env, today=date(2026, 10, 29))
    env["IBKR_RUNTIME_DAILY_BUSINESS_DATE"] = "2026-10-29"
    entry.preflight(env, today=date(2026, 10, 29))


def test_additional_slot_selects_only_its_explicit_mapping(env, monkeypatch):
    import json
    env["IBKR_ACCOUNT_FACTS_TARGET"] = "additional-1"
    env["IBKR_RUNTIME_DAILY_TARGET_KEY"] = json.dumps({
        "primary-live": {"target_key": "primary|tqqq_growth_income|live|" + "a" * 32, "account_key": "fixture-a"},
        "additional-1": {"target_key": "service|synthetic_profile|live|" + "b" * 32, "account_key": "fixture-b"},
    })
    monkeypatch.setattr(entry.identity, "resolve_expected", lambda: {"expected_report_prefix": "gs://fixture/synthetic_profile"})
    assert entry.preflight(env, today=date(2026, 10, 29)) == ("service|synthetic_profile|live|" + "b" * 32, "fixture-b")


@pytest.mark.parametrize("encoded", ["{", "{}", '{"additional-1":{}}',
    '{"additional-1":{},"additional-1":{}}'])
def test_additional_slot_rejects_missing_or_invalid_protected_mapping(env, monkeypatch, encoded):
    env["IBKR_ACCOUNT_FACTS_TARGET"] = "additional-1"
    env["IBKR_RUNTIME_DAILY_TARGET_KEY"] = encoded
    with pytest.raises(ValueError):
        entry.preflight(env, today=date(2026, 10, 29))


def test_mapping_does_not_cross_slots(env, monkeypatch):
    import json
    env["IBKR_ACCOUNT_FACTS_TARGET"] = "additional-2"
    env["IBKR_RUNTIME_DAILY_TARGET_KEY"] = json.dumps({"additional-1": {
        "target_key": "service|synthetic_profile|live|" + "b" * 32, "account_key": "fixture-b"}})
    with pytest.raises(ValueError):
        entry.preflight(env, today=date(2026, 10, 29))


def test_selected_mapping_is_forwarded_once_to_existing_publisher(env, monkeypatch):
    import json
    env["IBKR_ACCOUNT_FACTS_TARGET"] = "additional-2"
    key = "service|synthetic_profile|live|" + "c" * 32
    env["IBKR_RUNTIME_DAILY_TARGET_KEY"] = json.dumps({"additional-2": {"target_key": key, "account_key": "fixture-c"}})
    monkeypatch.setattr(entry.identity, "resolve_expected", lambda: {"expected_report_prefix": "gs://fixture/reports"})
    monkeypatch.setattr(entry.os, "environ", env)
    monkeypatch.setattr(entry, "preflight", lambda actual: original_preflight(actual, today=date(2026, 10, 29)))
    called = []
    monkeypatch.setattr(entry.publisher, "main", lambda **kwargs: called.append(kwargs) or 0)
    assert entry.main() == 0
    assert called == [{"target_key": key, "account_key": "fixture-c"}]


def test_receiver_token_preflight_never_reads_report_or_publishes(env, monkeypatch, capsys):
    env.update(IBKR_DAILY_TOKEN_PREFLIGHT="true", IBKR_RUNTIME_DAILY_SYNC_TOKEN="fixture-token")
    monkeypatch.setattr(entry.os, "environ", env)
    monkeypatch.setattr(entry.identity, "resolve_expected", lambda: pytest.fail("identity read"))
    monkeypatch.setattr(entry.publisher, "main", lambda **kwargs: pytest.fail("publication"))
    monkeypatch.setattr(entry.publisher, "console_base_url", lambda: "https://fixture.example")
    calls = []
    monkeypatch.setattr(entry.publisher, "verify_receiver_token", lambda **kw: calls.append(kw) or True)
    assert entry.main() == 0 and len(calls) == 1
    assert '"business_writes": 0' in capsys.readouterr().out
    env["GITHUB_RUN_ATTEMPT"] = "2"
    assert entry.main() == 1 and len(calls) == 1
