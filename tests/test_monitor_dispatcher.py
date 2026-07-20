import datetime as dt

import pytest

from application.monitor_dispatcher import (
    dispatch_due_monitor_targets,
    due_monitor_dispatches,
    load_monitor_targets,
)


def test_load_monitor_targets_reads_ibkr_specific_env(monkeypatch):
    monkeypatch.setenv(
        "IBKR_MONITOR_DISPATCH_TARGETS_JSON",
        '{"targets":[{"service_name":"svc-tqqq"}]}',
    )

    assert load_monitor_targets() == [{"service_name": "svc-tqqq"}]


def test_load_monitor_targets_rejects_non_object_items():
    with pytest.raises(ValueError, match="target at index 1 must be an object"):
        load_monitor_targets('{"targets":[{"service_name":"svc-tqqq"},"bad-target"]}')


def test_due_monitor_dispatches_selects_due_window_and_skips_disabled_target():
    targets = [
        {
            "service_name": "svc-tqqq",
            "service_url": "https://svc-tqqq.example.run.app",
            "strategy_profile": "tqqq_growth_income",
            "runtime_target_enabled": "true",
            "scheduler": {
                "timezone": "America/New_York",
                "probe_time": "35 9,15 * * *",
                "precheck_time": "45 9 * * *",
            },
        },
        {
            "service_name": "svc-disabled",
            "service_url": "https://svc-disabled.example.run.app",
            "runtime_target_enabled": "false",
            "scheduler": {
                "timezone": "America/New_York",
                "probe_time": "35 9,15 * * *",
            },
        },
    ]

    dispatches = due_monitor_dispatches(
        targets,
        now=dt.datetime(2026, 6, 18, 13, 35, tzinfo=dt.timezone.utc),
        lookback_minutes=4,
    )

    assert len(dispatches) == 1
    assert dispatches[0]["window"] == "probe"
    assert dispatches[0]["url"] == "https://svc-tqqq.example.run.app/probe"


def test_dispatch_due_monitor_targets_posts_with_identity_token():
    calls = []

    class FakeResponse:
        status_code = 200
        text = "Probe OK"

    def fake_request(url, *, headers, timeout):
        calls.append((url, headers, timeout))
        return FakeResponse()

    targets = [
        {
            "service_name": "svc-tqqq",
            "service_url": "https://svc-tqqq.example.run.app",
            "scheduler": {
                "timezone": "America/New_York",
                "probe_time": "35 9 * * *",
            },
        }
    ]

    result = dispatch_due_monitor_targets(
        targets,
        now=dt.datetime(2026, 6, 18, 13, 35, tzinfo=dt.timezone.utc),
        request_fn=fake_request,
        token_fetcher=lambda audience: f"token-for:{audience}",
        timeout_seconds=12,
    )

    assert result["dispatches_sent"] == 1
    assert result["results"][0]["ok"] is True
    assert calls == [
        (
            "https://svc-tqqq.example.run.app/probe",
            {
                "Authorization": "Bearer token-for:https://svc-tqqq.example.run.app",
                "User-Agent": "platform-monitor-dispatcher",
            },
            12,
        )
    ]


def test_dispatch_failure_is_exposed_in_result():
    def failing_request(_url, *, headers, timeout):
        raise TimeoutError(f"request exceeded {timeout}s")

    result = dispatch_due_monitor_targets(
        [
            {
                "service_name": "svc-tqqq",
                "service_url": "https://svc-tqqq.example.run.app",
                "scheduler": {
                    "timezone": "America/New_York",
                    "precheck_time": "45 9 * * *",
                },
            }
        ],
        now="2026-06-18T13:45:00+00:00",
        request_fn=failing_request,
        token_fetcher=lambda _audience: "token",
        timeout_seconds=12,
    )

    assert result["ok"] is False
    assert result["dispatches_sent"] == 1
    assert result["results"][0]["error_type"] == "TimeoutError"


def test_local_target_dispatches_in_process_without_self_http_call():
    http_calls = []
    local_calls = []

    result = dispatch_due_monitor_targets(
        [
            {
                "service_name": "host-service",
                "service_url": "https://host-service.example.run.app",
                "scheduler": {
                    "timezone": "America/New_York",
                    "precheck_time": "45 9 * * *",
                },
            }
        ],
        now="2026-06-18T13:45:00+00:00",
        request_fn=lambda *args, **kwargs: http_calls.append((args, kwargs)),
        local_service_name="host-service",
        local_dispatch_fn=lambda dispatch: local_calls.append(dispatch) or {"status_code": 200},
    )

    assert result["ok"] is True
    assert result["dispatches_due"] == 1
    assert result["dispatches_sent"] == 1
    assert result["results"][0]["dispatch_mode"] == "in_process"
    assert local_calls[0]["window"] == "precheck"
    assert http_calls == []
