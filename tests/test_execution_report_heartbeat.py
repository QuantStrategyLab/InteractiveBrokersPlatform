from __future__ import annotations

import datetime as dt
import json

from scripts import execution_report_heartbeat as heartbeat


def test_explicit_required_services_override_target_derived_services(monkeypatch):
    monkeypatch.setenv("RUNTIME_HEARTBEAT_REQUIRED_SERVICES", "svc-daily-a,svc-daily-b")
    monkeypatch.setenv(
        "CLOUD_RUN_SERVICE_TARGETS_JSON",
        json.dumps(
            {
                "targets": [
                    {"service": "svc-daily-a"},
                    {"service": "svc-monthly"},
                ]
            }
        ),
    )

    assert heartbeat._load_required_services() == ["svc-daily-a", "svc-daily-b"]


def test_required_services_fall_back_to_cloud_run_targets(monkeypatch):
    monkeypatch.delenv("RUNTIME_HEARTBEAT_REQUIRED_SERVICES", raising=False)
    monkeypatch.setenv(
        "CLOUD_RUN_SERVICE_TARGETS_JSON",
        json.dumps(
            {
                "targets": [
                    {"service": "svc-a"},
                    {"runtime_target": {"service_name": "svc-b"}},
                    {"service": "svc-a"},
                ]
            }
        ),
    )

    assert heartbeat._load_required_services() == ["svc-a", "svc-b"]


def test_scheduler_aware_required_services_only_include_due_main_schedulers(monkeypatch):
    monkeypatch.delenv("RUNTIME_HEARTBEAT_REQUIRED_SERVICES", raising=False)
    monkeypatch.setenv(
        "CLOUD_RUN_SERVICE_TARGETS_JSON",
        json.dumps(
            {
                "targets": [
                    {"service": "svc-daily"},
                    {"service": "svc-monthly"},
                ]
            }
        ),
    )
    monkeypatch.setattr(
        heartbeat,
        "_list_scheduler_jobs",
        lambda **_kwargs: [
            {
                "state": "ENABLED",
                "schedule": "45 15 * * 1-5",
                "timeZone": "America/New_York",
                "httpTarget": {"uri": "https://svc-daily.example.run.app/"},
            },
            {
                "state": "ENABLED",
                "schedule": "45 15 26 * *",
                "timeZone": "America/New_York",
                "httpTarget": {"uri": "https://svc-monthly.example.run.app/"},
            },
            {
                "state": "ENABLED",
                "schedule": "35 9,15 25-30 * *",
                "timeZone": "America/New_York",
                "httpTarget": {"uri": "https://svc-monthly.example.run.app/probe"},
            },
        ],
    )

    required = heartbeat._load_required_services(
        project="project-1",
        since=dt.datetime(2026, 6, 5, 0, 0, tzinfo=dt.timezone.utc),
        now=dt.datetime(2026, 6, 6, 2, 0, tzinfo=dt.timezone.utc),
    )

    assert required == ["svc-daily"]


def test_scheduler_aware_required_services_include_monthly_service_when_due(monkeypatch):
    monkeypatch.delenv("RUNTIME_HEARTBEAT_REQUIRED_SERVICES", raising=False)
    monkeypatch.setenv(
        "CLOUD_RUN_SERVICE_TARGETS_JSON",
        json.dumps({"targets": [{"service": "svc-monthly"}]}),
    )
    monkeypatch.setattr(
        heartbeat,
        "_list_scheduler_jobs",
        lambda **_kwargs: [
            {
                "state": "ENABLED",
                "schedule": "45 15 26 * *",
                "timeZone": "America/New_York",
                "httpTarget": {"uri": "https://svc-monthly.example.run.app/"},
            },
        ],
    )

    required = heartbeat._load_required_services(
        project="project-1",
        since=dt.datetime(2026, 6, 26, 19, 0, tzinfo=dt.timezone.utc),
        now=dt.datetime(2026, 6, 26, 20, 0, tzinfo=dt.timezone.utc),
    )

    assert required == ["svc-monthly"]
