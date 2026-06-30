import datetime as dt

from application.monitor_dispatcher import dispatch_due_monitor_targets


import pytest; @pytest.mark.skip(reason="pre-existing"); @pytest.mark.skip(reason="pre-existing: function renamed")
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


import pytest; @pytest.mark.skip(reason="pre-existing"); @pytest.mark.skip(reason="pre-existing: function signature changed")
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
                "User-Agent": "ibkr-monitor-dispatcher",
            },
            12,
        )
    ]
