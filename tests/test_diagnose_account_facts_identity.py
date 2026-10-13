from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import diagnose_account_facts_identity as diagnostic
from scripts import publish_account_facts_from_report as publisher


NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
SHA = "a" * 40
PRIVATE = "private-marker-never-public"


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("synthetic diagnostic must not make external calls or publish")

    monkeypatch.setattr(publisher.subprocess, "run", forbidden)
    monkeypatch.setattr(publisher, "publish_ibkr_account_facts_history", forbidden)
    monkeypatch.setattr(publisher, "main", forbidden)
    monkeypatch.setattr(publisher, "build_opener", forbidden)


def _environment(monkeypatch, *, target="primary-live"):
    settings = {
        "IBKR_ACCOUNT_FACTS_TARGET": target,
        "IBKR_ACCOUNT_FACTS_PROJECT_ID": PRIVATE + "-project",
        "IBKR_ACCOUNT_FACTS_REPORT_PREFIX": "gs://" + PRIVATE + "/reports",
        "IBKR_ACCOUNT_FACTS_TARGET_ID": PRIVATE + "-target",
        "IBKR_ACCOUNT_FACTS_SERVICE_NAME": PRIVATE + "-service",
        "IBKR_ACCOUNT_FACTS_RUNTIME_REVISION": PRIVATE + "-revision",
        "IBKR_ACCOUNT_FACTS_ACCOUNT_SCOPE": PRIVATE + "-scope",
        "IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON": '["U00000001"]',
        "IBKR_ACCOUNT_FACTS_DEPLOYMENT_SELECTOR": PRIVATE + "-deployment",
        "CLOUD_RUN_REGION": "us-central1",
        "GITHUB_SHA": SHA,
    }
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    additional = {
        f"additional-{i}": {
            "project_id": f"{PRIVATE}-project-{i}",
            "report_prefix": f"gs://{PRIVATE}/reports-{i}",
            "target_id": f"{PRIVATE}-target-{i}",
            "service_name": f"{PRIVATE}-service-{i}",
            "runtime_revision": f"{PRIVATE}-revision-{i}",
            "account_scope": f"{PRIVATE}-scope-{i}",
            "account_selector": [f"U0000001{i}"],
            "deployment_selector": f"{PRIVATE}-deployment-{i}",
        }
        for i in range(1, 4)
    }
    monkeypatch.setenv(publisher.IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON_ENV, json.dumps(additional))
    return settings, additional


def _report(expected):
    return {
        "schema_version": "runtime_report.v1",
        "platform": "interactive_brokers",
        "deploy_target": "cloud_run",
        "project_id": expected["expected_project_id"],
        "service_name": expected["expected_service_name"],
        "account_scope": expected["expected_account_scope"],
        "runtime_target": {
            "account_selector": list(expected["expected_account_selector"]),
            "deployment_selector": expected["expected_deployment_selector"],
        },
        "diagnostics": {"runtime_revision": expected["expected_runtime_revision"]},
        "runtime_release_receipt": {"runtime_revision": expected["expected_runtime_revision"]},
        "started_at": (NOW - timedelta(seconds=2)).isoformat(),
        "finished_at": NOW.isoformat(),
        "summary": {"account_facts": {
            "schema_version": publisher.SNAPSHOT_SCHEMA,
            "account_ids": list(expected["expected_account_selector"]),
            "currency": "USD",
            "observed_at": (NOW - timedelta(seconds=1)).isoformat(),
            "net_assets": "12345.6700",
            "cash": [{"currency": "USD", "cash_balance": "100.00", "source_tag": "CashBalance"}],
        }},
        "private_free_text": PRIVATE,
    }


def _metadata(expected):
    return {
        "metadata": {"name": expected["expected_service_name"]},
        "status": {
            "latestReadyRevisionName": expected["expected_runtime_revision"],
            "latestCreatedRevisionName": expected["expected_runtime_revision"],
        },
        "spec": {"private_free_text": PRIVATE},
    }


def _diagnose(report, expected, **kwargs):
    return diagnostic.diagnose_identity(
        report, expected=expected,
        source_report_uri=expected["expected_report_prefix"] + "/2026-10/20261013T115958Z.json",
        now=NOW, source_sha=SHA, **kwargs,
    )


@pytest.mark.parametrize("target", ["primary-live", "additional-1", "additional-2", "additional-3"])
def test_valid_slots_use_original_protected_config_without_environment_replacement(monkeypatch, target):
    settings, additional = _environment(monkeypatch, target=target)
    expected = diagnostic.resolve_expected()
    source = additional.get(target)
    assert expected["expected_project_id"] == (source["project_id"] if source else settings["IBKR_ACCOUNT_FACTS_PROJECT_ID"])
    assert expected["expected_report_prefix"] == (source["report_prefix"] if source else settings["IBKR_ACCOUNT_FACTS_REPORT_PREFIX"])
    assert diagnostic.os.environ["IBKR_ACCOUNT_FACTS_REPORT_PREFIX"] == settings["IBKR_ACCOUNT_FACTS_REPORT_PREFIX"]
    result = _diagnose(_report(expected), expected, cloud_run_metadata=_metadata(expected))
    assert result["reason"] == "identity_verified"
    assert result["projection_passed"] is True
    assert result["freshness_status"] == "fresh"
    assert result["cloud_run_metadata_status"] == "read"
    assert all(value is True for value in result["revision_equalities"].values())
    assert PRIVATE not in json.dumps(result)
    assert "U000000" not in json.dumps(result)
    assert "12345" not in json.dumps(result)


@pytest.mark.parametrize("target", ["", "additional-4", "disabled", "ingress-diagnostic", PRIVATE])
def test_unknown_slot_fails_before_cloud_or_report_read(monkeypatch, capsys, target):
    _environment(monkeypatch, target=target)
    assert diagnostic.main([]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == "target_disabled"
    assert result["status"] == "unavailable"
    assert PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("failure", ["missing", "malformed_additional", "collision", "bad_prefix", "bad_selector", "multiline", "bad_target_id"])
def test_bad_config_fails_in_network_free_preflight(monkeypatch, capsys, failure):
    _, additional = _environment(monkeypatch)
    if failure == "missing":
        monkeypatch.delenv("IBKR_ACCOUNT_FACTS_PROJECT_ID")
    elif failure == "malformed_additional":
        monkeypatch.setenv("IBKR_ACCOUNT_FACTS_TARGET", "additional-1")
        monkeypatch.setenv(publisher.IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON_ENV, "{bad" + PRIVATE)
    elif failure == "collision":
        monkeypatch.setenv("IBKR_ACCOUNT_FACTS_TARGET", "additional-1")
        additional["additional-2"]["account_selector"] = ["U00000001"]
        monkeypatch.setenv(publisher.IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON_ENV, json.dumps(additional))
    elif failure == "bad_prefix":
        monkeypatch.setenv("IBKR_ACCOUNT_FACTS_REPORT_PREFIX", "https://" + PRIVATE)
    elif failure == "bad_selector":
        monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON", '["default"]')
    elif failure == "multiline":
        monkeypatch.setenv("IBKR_ACCOUNT_FACTS_SERVICE_NAME", PRIVATE + "\nservice")
    else:
        monkeypatch.setenv("IBKR_ACCOUNT_FACTS_TARGET_ID", "Uppercase")
    assert diagnostic.main(["--preflight"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == ("target_config_unavailable" if failure == "missing" else "target_config_invalid")
    assert PRIVATE not in json.dumps(result)


def test_preflight_success_is_not_identity_or_runtime_verification(monkeypatch, capsys):
    _environment(monkeypatch)
    assert diagnostic.main(["--preflight"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "preflight"
    assert result["reason"] == "target_config_valid"
    assert result["projection_passed"] is None
    assert all(value is None for value in result["revision_equalities"].values())


@pytest.mark.parametrize("field", publisher._RUNTIME_TARGET_MISMATCH_FIELDS)
def test_original_guard_rejects_each_mismatch_without_private_values(monkeypatch, field):
    _environment(monkeypatch)
    expected = diagnostic.resolve_expected()
    report = _report(expected)
    if field == "runtime_revision":
        report["diagnostics"][field] = PRIVATE + "-different"
    elif field == "runtime_release_receipt.runtime_revision":
        report["runtime_release_receipt"]["runtime_revision"] = PRIVATE + "-different"
    elif field in {"deployment_selector", "account_selector"}:
        report["runtime_target"][field] = ["U00000099"] if field == "account_selector" else PRIVATE + "-different"
    else:
        report[field] = PRIVATE + "-different"
    result = _diagnose(report, expected, cloud_run_metadata=_metadata(expected))
    assert result["reason"] == "runtime_target_mismatch"
    assert result["mismatched_fields"] == [field]
    assert result["projection_passed"] is False
    assert PRIVATE not in json.dumps(result)


def test_first_guard_failure_and_legacy_receipt_contract_are_preserved(monkeypatch):
    _environment(monkeypatch)
    expected = diagnostic.resolve_expected()
    report = _report(expected)
    report["service_name"] = PRIVATE + "-different"
    report["runtime_release_receipt"]["runtime_revision"] = PRIVATE + "-different"
    assert _diagnose(report, expected)["mismatched_fields"] == ["service_name"]
    report = _report(expected)
    report["runtime_release_receipt"] = {"attestation_state": "legacy_unattested"}
    result = _diagnose(report, expected, cloud_run_metadata=_metadata(expected))
    assert result["projection_passed"] is True
    assert result["revision_equalities"]["receipt_equals_report"] is None


@pytest.mark.parametrize("hours,expected_status", [(36, "fresh"), (36.0001, "stale"), (-1 / 12, "fresh"), (-0.1, "stale")])
def test_freshness_uses_existing_36_hour_and_five_minute_bounds(monkeypatch, hours, expected_status):
    _environment(monkeypatch)
    expected = diagnostic.resolve_expected()
    report = _report(expected)
    observed = NOW - timedelta(hours=hours)
    report["started_at"] = (observed - timedelta(seconds=1)).isoformat()
    report["finished_at"] = (observed + timedelta(seconds=1)).isoformat()
    report["summary"]["account_facts"]["observed_at"] = observed.isoformat()
    result = _diagnose(report, expected)
    assert result["projection_passed"] is True
    assert result["freshness_status"] == expected_status


def test_report_and_config_revision_drift_are_equality_only(monkeypatch):
    _environment(monkeypatch)
    expected = diagnostic.resolve_expected()
    report = _report(expected)
    cloud = _metadata(expected)
    report["diagnostics"]["runtime_revision"] = PRIVATE + "-new"
    report["runtime_release_receipt"]["runtime_revision"] = PRIVATE + "-new"
    cloud["status"]["latestReadyRevisionName"] = PRIVATE + "-new"
    cloud["status"]["latestCreatedRevisionName"] = PRIVATE + "-new"
    result = _diagnose(report, expected, cloud_run_metadata=cloud)
    assert result["revision_equalities"]["report_equals_ready"] is True
    assert result["revision_equalities"]["configured_equals_ready"] is False
    assert result["revision_equalities"]["report_equals_configured"] is False
    assert result["projection_passed"] is False
    assert PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("metadata", [None, [], {}, {"metadata": {"name": PRIVATE}}, {"metadata": {"name": PRIVATE + "-service"}, "status": {}}])
def test_unavailable_or_incomplete_metadata_does_not_create_revision_proof(monkeypatch, metadata):
    _environment(monkeypatch)
    expected = diagnostic.resolve_expected()
    result = _diagnose(_report(expected), expected, cloud_run_metadata=metadata)
    assert result["revision_equalities"]["configured_equals_ready"] is None
    assert result["revision_equalities"]["report_equals_ready"] is None
    assert PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("sha", [PRIVATE, "a" * 39, "g" * 40, {"nested": PRIVATE}, None])
def test_source_sha_only_accepts_full_hex(monkeypatch, sha):
    _environment(monkeypatch)
    expected = diagnostic.resolve_expected()
    result = diagnostic.diagnose_identity(_report(expected), expected=expected,
        source_report_uri=expected["expected_report_prefix"] + "/r.json", now=NOW, source_sha=sha)
    assert result["source_sha"] is None
    assert PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("failure", ["listing", "reading", "cloud", "cloud_json", "unexpected"])
def test_cli_external_failures_are_redacted_and_never_publish(monkeypatch, capsys, failure):
    _environment(monkeypatch)
    expected = diagnostic.resolve_expected()
    report = _report(expected)
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        assert kwargs["capture_output"] is True
        if command[1:3] == ("storage", "ls"):
            if failure == "listing":
                return subprocess.CompletedProcess(command, 1, "", PRIVATE)
            return subprocess.CompletedProcess(command, 0, expected["expected_report_prefix"] + "/2026-10/20261013T115958Z.json\n", "")
        if command[1:3] == ("storage", "cat"):
            if failure == "reading":
                return subprocess.CompletedProcess(command, 1, "", PRIVATE)
            if failure == "unexpected":
                raise RuntimeError(PRIVATE)
            return subprocess.CompletedProcess(command, 0, json.dumps(report), "")
        assert command[1:4] == ("run", "services", "describe")
        return subprocess.CompletedProcess(command, 1 if failure == "cloud" else 0,
            PRIVATE if failure == "cloud_json" else json.dumps(_metadata(expected)), PRIVATE)

    monkeypatch.setattr(publisher.subprocess, "run", fake_run)
    assert diagnostic.main([]) == 1
    captured = capsys.readouterr()
    assert PRIVATE not in captured.out + captured.err
    result = json.loads(captured.out)
    assert result["status"] in {"diagnosed", "unavailable"}
    if failure in {"cloud", "cloud_json"}:
        assert result["projection_passed"] is True
    else:
        assert result["projection_passed"] is None
    assert all(cmd[0] == "gcloud" for cmd in commands)


@pytest.mark.parametrize("target", ["primary-live", "additional-1", "additional-2", "additional-3"])
@pytest.mark.parametrize("age", [1, 37])
def test_full_cli_uses_selected_read_commands_and_requires_fresh_identity(monkeypatch, capsys, target, age):
    _environment(monkeypatch, target=target)
    expected = diagnostic.resolve_expected()
    report = _report(expected)
    observed = NOW - timedelta(hours=age)
    report["started_at"] = (observed - timedelta(seconds=1)).isoformat()
    report["finished_at"] = (observed + timedelta(seconds=1)).isoformat()
    report["summary"]["account_facts"]["observed_at"] = observed.isoformat()
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        assert command[command.index("--project") + 1] == expected["expected_project_id"]
        if command[1:3] == ("storage", "ls"):
            return subprocess.CompletedProcess(command, 0, expected["expected_report_prefix"] + "/2026-10/20261013T110000Z.json\n", "")
        if command[1:3] == ("storage", "cat"):
            return subprocess.CompletedProcess(command, 0, json.dumps(report), "")
        assert command[1:4] == ("run", "services", "describe")
        assert command[4] == expected["expected_service_name"]
        assert command[command.index("--region") + 1] == "us-central1"
        return subprocess.CompletedProcess(command, 0, json.dumps(_metadata(expected)), "")

    monkeypatch.setattr(publisher.subprocess, "run", fake_run)
    assert diagnostic.main([], now=NOW) == (0 if age == 1 else 1)
    captured = capsys.readouterr()
    assert PRIVATE not in captured.out + captured.err
    assert captured.err == ""
    result = json.loads(captured.out)
    assert result["projection_passed"] is True
    assert result["freshness_status"] == ("fresh" if age == 1 else "stale")
    assert len(commands) == 4  # Two bounded month listings, one report read, one service read.


def test_unclassified_projection_metadata_cannot_leak_private_reason_or_fields(monkeypatch):
    _environment(monkeypatch)
    expected = diagnostic.resolve_expected()
    monkeypatch.setattr(publisher, "project_ibkr_account_facts_history", lambda *args, **kwargs: {
        "status": "skipped", "reason": PRIVATE, "mismatched_fields": [PRIVATE],
    })
    result = _diagnose(_report(expected), expected)
    assert result["reason"] == "diagnostic_error"
    assert result["mismatched_fields"] == []
    assert PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("region", [PRIVATE, "--option", "us-central1\n", ""])
def test_invalid_region_cannot_trigger_unscoped_metadata_read(monkeypatch, region):
    _environment(monkeypatch)
    monkeypatch.setenv("CLOUD_RUN_REGION", region)
    assert diagnostic._read_cloud_run_metadata(diagnostic.resolve_expected()) is None


def test_cloud_run_os_error_keeps_read_report_guard_facts(monkeypatch, capsys):
    _environment(monkeypatch)
    expected = diagnostic.resolve_expected()
    monkeypatch.setattr(publisher, "_latest_report_uri", lambda **kwargs: expected["expected_report_prefix"] + "/r.json")
    monkeypatch.setattr(publisher, "_load_gcs_report", lambda *args, **kwargs: _report(expected))
    def unavailable(*args, **kwargs):
        raise OSError(PRIVATE)
    monkeypatch.setattr(publisher.subprocess, "run", unavailable)
    assert diagnostic.main([], now=NOW) == 1
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["projection_passed"] is True
    assert result["cloud_run_metadata_status"] == "unavailable"
    assert PRIVATE not in captured.out + captured.err


def test_missing_region_never_infers_it_from_report_or_queries_cloud_run(monkeypatch, capsys):
    _environment(monkeypatch)
    monkeypatch.delenv("CLOUD_RUN_REGION")
    expected = diagnostic.resolve_expected()
    report = _report(expected)
    report["cloud_run_region"] = "arbitrary-region"
    monkeypatch.setattr(publisher, "_latest_report_uri", lambda **kwargs: expected["expected_report_prefix"] + "/r.json")
    monkeypatch.setattr(publisher, "_load_gcs_report", lambda *args, **kwargs: report)
    assert diagnostic.main([]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["cloud_run_metadata_status"] == "unavailable"
    assert result["revision_equalities"]["report_equals_ready"] is None


def test_workflow_is_main_manual_only_and_has_no_write_or_notification_channel():
    workflow = Path(".github/workflows/account-facts-identity-diagnose.yml").read_text()
    assert "workflow_dispatch:" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "schedule:" not in workflow
    assert "SYNC_TOKEN" not in workflow
    assert "TELEGRAM" not in workflow
    assert "upload-artifact" not in workflow
    assert "contents: read" in workflow
    assert workflow.index("scripts/diagnose_account_facts_identity.py --preflight") < workflow.index("google-github-actions/auth")
    assert all(target in workflow for target in ("primary-live", "additional-1", "additional-2", "additional-3"))


def test_import_is_inert_in_fresh_interpreter(monkeypatch):
    # Use the unpatched executable only for an isolated stdlib import probe.
    code = '''
import socket, subprocess, urllib.request
def forbidden(*args, **kwargs):
    raise AssertionError("external call during import")
socket.socket = forbidden
socket.getaddrinfo = forbidden
subprocess.run = forbidden
urllib.request.urlopen = forbidden
urllib.request.build_opener = forbidden
import scripts.diagnose_account_facts_identity
print("inert import passed")
'''
    monkeypatch.undo()
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "inert import passed"
