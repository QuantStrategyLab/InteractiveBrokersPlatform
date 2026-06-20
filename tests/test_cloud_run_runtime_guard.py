from __future__ import annotations

import subprocess
import re

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

