import json
from subprocess import CompletedProcess

import pytest

from scripts import summarize_historical_execution_cloud as diagnostic


def test_summarize_selected_private_reports_without_order_details(monkeypatch):
    root = "gs://private/execution-reports/interactive_brokers/tqqq_growth_income/live-u00000002/2026-08/"
    service = {
        "spec": {"template": {"spec": {"containers": [{"env": [
            {"name": "EXECUTION_REPORT_GCS_URI", "value": "gs://private/execution-reports"},
            {"name": "STRATEGY_PROFILE", "value": "tqqq_growth_income"},
            {"name": "ACCOUNT_GROUP", "value": "live-u00000002"},
        ]}]}}},
    }
    report = {
        "platform": "interactive_brokers",
        "strategy_profile": "tqqq_growth_income",
        "account_scope": "live-u00000002",
        "started_at": "2026-08-04T19:45:00Z",
        "dry_run": False,
        "status": "ok",
        "summary": {"execution_status": "executed", "orders_submitted_count": 1},
        "execution_receipt": {"outcome": "submitted", "broker_confirmation": "not_observed"},
        "errors": [],
        "positions": [{"symbol": "SECRET"}],
    }

    def fake_gcloud(*args):
        if args[:3] == ("run", "services", "describe"):
            output = json.dumps(service).encode()
        elif args[:2] == ("storage", "ls"):
            output = (root + "20260804T194500Z.json\n").encode()
        elif args[:2] == ("storage", "cat"):
            output = json.dumps(report).encode()
        else:
            raise AssertionError(args)
        return CompletedProcess(args, 0, output, b"")

    monkeypatch.setattr(diagnostic, "_gcloud", fake_gcloud)
    result = diagnostic.summarize(
        service="service", project="project", region="region", dates=("2026-08-04",)
    )

    assert result["report_count"] == 1
    assert result["reports"][0]["orders_submitted_count"] == 1
    assert result["reports"][0]["broker_confirmation"] == "not_observed"
    assert "SECRET" not in json.dumps(result)


def test_summarize_rejects_wrong_account_report(monkeypatch):
    root = "gs://private/execution-reports/interactive_brokers/tqqq_growth_income/live-u00000002/2026-08/"
    service = {"spec": {"template": {"spec": {"containers": [{"env": [
        {"name": "EXECUTION_REPORT_GCS_URI", "value": "gs://private/execution-reports"},
        {"name": "STRATEGY_PROFILE", "value": "tqqq_growth_income"},
        {"name": "ACCOUNT_GROUP", "value": "live-u00000002"},
    ]}]}}}}
    report = {
        "platform": "interactive_brokers",
        "strategy_profile": "tqqq_growth_income",
        "account_scope": "other-account",
    }

    def fake_gcloud(*args):
        if args[:3] == ("run", "services", "describe"):
            output = json.dumps(service).encode()
        elif args[:2] == ("storage", "ls"):
            output = (root + "20260804T194500Z.json\n").encode()
        elif args[:2] == ("storage", "cat"):
            output = json.dumps(report).encode()
        else:
            raise AssertionError(args)
        return CompletedProcess(args, 0, output, b"")

    monkeypatch.setattr(diagnostic, "_gcloud", fake_gcloud)
    with pytest.raises(ValueError, match="identity mismatch"):
        diagnostic.summarize(service="service", project="project", region="region", dates=("2026-08-04",))
