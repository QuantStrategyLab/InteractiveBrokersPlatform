import json
import os
from pathlib import Path
import re
import subprocess
from subprocess import CompletedProcess
import sys
import textwrap

import pytest

from scripts import summarize_historical_execution_cloud as diagnostic


def _private_target():
    return {"service": "private-service", "service_name": "private-service", "include_reconciliation": True,
            "runtime_target": {"strategy_profile": "sample", "execution_mode": "live",
                               "service_name": "private-service"}}


def test_service_selection_uses_private_binding_with_string_or_object_runtime():
    target = _private_target()
    for stringify in (False, True):
        entry = {**target, "runtime_target": json.dumps(target["runtime_target"]) if stringify else target["runtime_target"]}
        for payload in ([entry], {"targets": [entry]}):
            assert diagnostic.resolve_private_service(json.dumps(payload), "sample") == "private-service"


@pytest.mark.parametrize("change", [
    {"include_reconciliation": False},
    {"service_name": "other-service"},
    {"service": "invalid\nservice"},
    {"runtime_target": {"strategy_profile": "sample", "execution_mode": "paper", "service_name": "private-service"}},
    {"runtime_target": {"strategy_profile": "sample", "execution_mode": "live", "service_name": "other-service"}},
])
def test_service_selection_rejects_unbound_or_nonlive_entries(change):
    with pytest.raises(ValueError, match="one bound live service"):
        diagnostic.resolve_private_service(json.dumps([{**_private_target(), **change}]), "sample")


@pytest.mark.parametrize("raw", ["invalid", "{}", "null", "[]"])
def test_service_selection_rejects_absent_or_invalid_inventory(raw):
    with pytest.raises(ValueError, match="one bound live service"):
        diagnostic.resolve_private_service(raw, "sample")


def test_service_selection_rejects_duplicates_instead_of_choosing_first():
    with pytest.raises(ValueError, match="one bound live service"):
        diagnostic.resolve_private_service(json.dumps([_private_target(), _private_target()]), "sample")


def test_history_workflow_selects_and_masks_private_service(tmp_path):
    root = Path(__file__).resolve().parents[1]
    wrapper = (root / ".github/workflows/inspect-historical-execution.yml").read_text()
    assert 'uses: ./.github/workflows/inspect-historical-execution-protected.yml' in wrapper
    assert 'secrets:\n      CLOUD_RUN_SERVICE_TARGETS_JSON: ${{ vars.CLOUD_RUN_SERVICE_TARGETS_JSON || secrets.CLOUD_RUN_SERVICE_TARGETS_JSON }}' in wrapper
    assert 'runs-on:' not in wrapper
    workflow = (root / ".github/workflows/inspect-historical-execution-protected.yml").read_text()
    assert 'secrets.CLOUD_RUN_SERVICE_TARGETS_JSON' in workflow
    assert 'jq -r .service' not in workflow
    script = re.search(r"python3 - <<'PY'\n(.*?)\n          PY", workflow, re.S)
    assert script is not None
    output = tmp_path / "github-output"
    result = subprocess.run([sys.executable, "-"], input=textwrap.dedent(script.group(1)),
                            text=True, capture_output=True, cwd=root, env={**os.environ,
                                "GITHUB_OUTPUT": str(output), "REQUESTED_TARGET": "sample",
                                "CLOUD_RUN_SERVICE_TARGETS_JSON": json.dumps([_private_target()])})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "::add-mask::private-service"
    assert output.read_text() == "service=private-service\n"


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
