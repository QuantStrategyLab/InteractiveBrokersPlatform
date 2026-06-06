from __future__ import annotations

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
