from __future__ import annotations

import datetime as dt
import json
import re
import subprocess

from scripts import cloud_run_runtime_guard as guard


def test_scheduler_job_pattern_includes_service_alias():
    pattern = guard._scheduler_job_pattern_for_services(
        ["interactive-brokers-live-u1599-tqqq-service"]
    )

    assert re.search(pattern, "interactive-brokers-live-u1599-tqqq-service-scheduler")
    assert re.search(pattern, "interactive-brokers-live-u1599-tqqq-scheduler")
    assert not re.search(pattern, "interactive-brokers-live-u1660-soxl-scheduler")


def test_telegram_token_falls_back_to_secret_manager(monkeypatch):
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("TG_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_TOKEN_SECRET_NAME", "platform-telegram-token")
    monkeypatch.setenv("GCP_PROJECT_ID", "interactivebrokersquant")
    observed = {}

    def fake_run_gcloud(command):
        observed["command"] = command
        return subprocess.CompletedProcess(command, 0, stdout="secret-token\n", stderr="")

    monkeypatch.setattr(guard, "_run_gcloud", fake_run_gcloud)

    assert guard._telegram_token() == "secret-token"
    assert observed["command"] == [
        "gcloud",
        "secrets",
        "versions",
        "access",
        "latest",
        "--secret",
        "platform-telegram-token",
        "--project",
        "interactivebrokersquant",
    ]


def test_cloud_run_log_since_uses_latest_ready_revision(monkeypatch):
    monkeypatch.setenv("CLOUD_RUN_REGION", "us-central1")
    observed = []

    def fake_run_gcloud(command):
        observed.append(command)
        if command[1:4] == ["run", "services", "describe"]:
            payload = {
                "status": {
                    "latestReadyRevisionName": "interactive-brokers-live-u1599-tqqq-service-00002"
                }
            }
        else:
            payload = {"metadata": {"creationTimestamp": "2026-07-01T06:50:04.123Z"}}
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(guard, "_run_gcloud", fake_run_gcloud)

    fallback = dt.datetime(2026, 7, 1, 6, 0, tzinfo=dt.timezone.utc)
    result = guard._cloud_run_log_since(
        "interactivebrokersquant",
        "interactive-brokers-live-u1599-tqqq-service",
        fallback,
    )

    assert result == dt.datetime(2026, 7, 1, 6, 50, 4, 123000, tzinfo=dt.timezone.utc)
    assert observed[0] == [
        "gcloud",
        "run",
        "services",
        "describe",
        "interactive-brokers-live-u1599-tqqq-service",
        "--project",
        "interactivebrokersquant",
        "--region",
        "us-central1",
        "--format=json",
    ]
    assert observed[1][1:5] == [
        "run",
        "revisions",
        "describe",
        "interactive-brokers-live-u1599-tqqq-service-00002",
    ]


def test_region_for_service_prefers_target_region(monkeypatch):
    monkeypatch.setenv("CLOUD_RUN_REGION", "us-central1")
    monkeypatch.setenv(
        "CLOUD_RUN_SERVICE_TARGETS_JSON",
        json.dumps(
            {
                "targets": [
                    {
                        "service": "interactive-brokers-live-u1599-tqqq-service",
                        "region": "asia-east1",
                    },
                ]
            }
        ),
    )

    assert guard._region_for_service("interactive-brokers-live-u1599-tqqq-service") == "asia-east1"
