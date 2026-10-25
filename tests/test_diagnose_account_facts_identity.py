from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import backup_primary_account_facts_state as backup
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


def _primary_binding_preview_context(monkeypatch, *, target="primary-live"):
    _environment(monkeypatch, target=target)
    expected = diagnostic.resolve_expected()
    report = _report(expected)
    revision = PRIVATE + "-serving-revision"
    report["diagnostics"]["runtime_revision"] = revision
    report["runtime_release_receipt"]["runtime_revision"] = revision
    metadata = _metadata(expected)
    metadata["status"].update({
        "latestReadyRevisionName": revision,
        "latestCreatedRevisionName": revision,
        "traffic": [{"revisionName": revision, "percent": 100}],
    })
    uri = expected["expected_report_prefix"] + "/2026-10/20261013T115958Z.json"
    monkeypatch.setattr(publisher, "_latest_report_uri", lambda **_kwargs: uri)
    monkeypatch.setattr(publisher, "_load_gcs_report", lambda *_args, **_kwargs: report)
    monkeypatch.setattr(diagnostic, "_read_cloud_run_metadata", lambda _expected: metadata)
    return expected, report, metadata


def test_primary_binding_preview_has_only_hashes_and_preserves_revision_rejection(monkeypatch, capsys):
    expected, report, _metadata_row = _primary_binding_preview_context(monkeypatch)
    original_expected = dict(expected)
    assert diagnostic.main([], now=NOW) == 1
    body = json.loads(capsys.readouterr().out)
    assert body["projection_passed"] is False
    assert body["reason"] == "runtime_target_mismatch"
    assert body["mismatched_fields"] == ["runtime_revision"]
    preview = body["source_binding_preview"]
    assert preview["status"] == "comparison_only"
    assert preview["slot"] == "primary-live"
    assert preview["difference_fields"] == ["runtime_revision"]
    assert preview["publication_authority_granted"] is False
    configured = publisher._bound_source_id(
        project_id=expected["expected_project_id"], service_name=expected["expected_service_name"],
        runtime_revision=expected["expected_runtime_revision"], account_scope=expected["expected_account_scope"],
        account_selector=tuple(expected["expected_account_selector"]),
        deployment_selector=expected["expected_deployment_selector"],
    )
    observed = publisher._bound_source_id(
        project_id=expected["expected_project_id"], service_name=expected["expected_service_name"],
        runtime_revision=report["diagnostics"]["runtime_revision"], account_scope=expected["expected_account_scope"],
        account_selector=tuple(expected["expected_account_selector"]),
        deployment_selector=expected["expected_deployment_selector"],
    )
    def sha(value):
        return hashlib.sha256(value.encode()).hexdigest()
    assert preview["target_id_sha256"] == sha(expected["target_id"])
    assert preview["configured_source_binding_id_sha256"] == sha(configured)
    assert preview["candidate_source_binding_id_sha256"] == sha(observed)
    assert preview["configured_source_binding_id_sha256"] != preview["candidate_source_binding_id_sha256"]
    assert preview["report_runtime_revision_sha256"] == preview["serving_runtime_revision_sha256"]
    assert all(len(value) == 64 and all(char in "0123456789abcdef" for char in value)
               for key, value in preview.items() if key.endswith("_sha256"))
    serialized = json.dumps(body)
    assert PRIVATE not in serialized and "U00000001" not in serialized
    assert configured not in serialized and observed not in serialized
    assert "12345.6700" not in serialized
    assert expected == original_expected
    assert diagnostic.os.environ["IBKR_ACCOUNT_FACTS_RUNTIME_REVISION"] == expected["expected_runtime_revision"]


@pytest.mark.parametrize("failure", ["stale", "source", "account", "currency", "receipt", "split", "missing_traffic"])
def test_primary_binding_preview_requires_existing_verified_scope_and_fresh_serving_report(monkeypatch, capsys, failure):
    _expected, report, metadata = _primary_binding_preview_context(monkeypatch)
    if failure == "stale":
        report["summary"]["account_facts"]["observed_at"] = (NOW - timedelta(hours=37)).isoformat()
        report["started_at"] = (NOW - timedelta(hours=38)).isoformat()
    elif failure == "source":
        report["service_name"] = PRIVATE + "-foreign-service"
    elif failure == "account":
        report["summary"]["account_facts"]["account_ids"] = ["U00000002"]
    elif failure == "currency":
        report["summary"]["account_facts"]["currency"] = "EUR"
    elif failure == "receipt":
        report["runtime_release_receipt"]["runtime_revision"] = PRIVATE + "-foreign-revision"
    elif failure == "split":
        metadata["status"]["traffic"] = [
            {"revisionName": report["diagnostics"]["runtime_revision"], "percent": 50},
            {"revisionName": PRIVATE + "-other", "percent": 50},
        ]
    else:
        metadata["status"].pop("traffic")
    assert diagnostic.main([], now=NOW) == 1
    body = json.loads(capsys.readouterr().out)
    assert "source_binding_preview" not in body
    assert PRIVATE not in json.dumps(body)


@pytest.mark.parametrize("target", ["additional-1", "additional-2", "additional-3"])
def test_primary_binding_preview_does_not_expand_to_other_slots(monkeypatch, capsys, target):
    _primary_binding_preview_context(monkeypatch, target=target)
    assert diagnostic.main([], now=NOW) == 1
    assert "source_binding_preview" not in json.loads(capsys.readouterr().out)


def test_primary_binding_preview_does_not_use_ready_or_created_as_serving_fallback(monkeypatch, capsys):
    _expected, _report_row, metadata = _primary_binding_preview_context(monkeypatch)
    metadata["status"]["latestReadyRevisionName"] = PRIVATE + "-new-tag-only"
    metadata["status"]["latestCreatedRevisionName"] = PRIVATE + "-new-tag-only"
    assert diagnostic.main([], now=NOW) == 1
    body = json.loads(capsys.readouterr().out)
    preview = body["source_binding_preview"]
    assert preview["report_runtime_revision_sha256"] == preview["serving_runtime_revision_sha256"]
    assert body["revision_equalities"]["report_equals_ready"] is False


def test_primary_binding_preview_preserves_original_legacy_receipt_contract(monkeypatch, capsys):
    _expected, report, _metadata_row = _primary_binding_preview_context(monkeypatch)
    report.pop("runtime_release_receipt")
    assert diagnostic.main([], now=NOW) == 1
    body = json.loads(capsys.readouterr().out)
    assert body["source_binding_preview"]["status"] == "comparison_only"
    assert body["revision_equalities"]["receipt_equals_report"] is None
    assert body["projection_passed"] is False


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
        if command[1:4] == ("storage", "objects", "list"):
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
        if command[1:4] == ("storage", "objects", "list"):
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
    assert workflow.count("uses: actions/upload-artifact@") == 1
    artifact = workflow.split("      - name: Preserve only the standard GitHub sealed recovery values\n", 1)[1].split("\n      - name:", 1)[0]
    assert "path: ${{ runner.temp }}/primary-recovery-secrets.json" in artifact
    assert "if-no-files-found: ignore" in artifact and "retention-days: 1" in artifact
    assert "contents: read" in workflow
    assert workflow.index("scripts.diagnose_account_facts_identity --preflight") < workflow.index("google-github-actions/auth")
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


@pytest.mark.parametrize("scenario,status,report_positive,configured_positive,report_only", [
    ("report_single", "single", True, True, True),
    ("configured_single_report_ready_only", "single", False, True, False),
    ("report_single_configured_old", "single", True, False, True),
    ("split_both", "split", True, True, False),
    ("split_neither", "split", False, False, False),
    ("same_revision_components", "single", True, True, True),
    ("zero_tag", "single", False, True, False),
])
def test_serving_traffic_uses_effective_positive_distribution_without_ready_fallback(
    monkeypatch, scenario, status, report_positive, configured_positive, report_only,
):
    _environment(monkeypatch)
    expected = diagnostic.resolve_expected()
    configured = expected["expected_runtime_revision"]
    report = _report(expected)
    cloud = _metadata(expected)
    report_revision = configured
    if scenario in {"configured_single_report_ready_only", "split_both", "zero_tag"}:
        report_revision = PRIVATE + "-new-report"
    if scenario == "report_single_configured_old":
        report_revision = PRIVATE + "-new-report"
    report["diagnostics"]["runtime_revision"] = report_revision
    report["runtime_release_receipt"]["runtime_revision"] = report_revision
    cloud["status"]["latestReadyRevisionName"] = report_revision
    cloud["status"]["latestCreatedRevisionName"] = report_revision
    traffic = {
        "report_single": [{"revisionName": report_revision, "percent": 100}],
        "configured_single_report_ready_only": [{"revisionName": configured, "percent": 100}],
        "report_single_configured_old": [{"revisionName": report_revision, "percent": 100}],
        "split_both": [{"revisionName": configured, "percent": 25}, {"revisionName": report_revision, "percent": 75}],
        "split_neither": [{"revisionName": PRIVATE + "-other-1", "percent": 20}, {"revisionName": PRIVATE + "-other-2", "percent": 80}],
        "same_revision_components": [{"revisionName": configured, "percent": 40}, {"revisionName": configured, "percent": 60}],
        "zero_tag": [{"revisionName": configured, "percent": 100}, {"revisionName": report_revision, "percent": 0, "tag": PRIVATE, "url": "https://" + PRIVATE}],
    }[scenario]
    cloud["status"]["traffic"] = traffic
    result = _diagnose(report, expected, cloud_run_metadata=cloud)
    assert result["revision_equalities"]["report_equals_ready"] is True
    assert result["serving_traffic"] == {
        "status": status,
        "report_has_positive_traffic": report_positive,
        "configured_has_positive_traffic": configured_positive,
        "report_is_only_serving_revision": report_only,
    }
    assert PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("traffic", [None, []])
def test_missing_traffic_keeps_all_serving_comparisons_unknown(monkeypatch, traffic):
    _environment(monkeypatch)
    expected = diagnostic.resolve_expected()
    cloud = _metadata(expected)
    if traffic is not None:
        cloud["status"]["traffic"] = traffic
    result = _diagnose(_report(expected), expected, cloud_run_metadata=cloud)
    assert result["serving_traffic"] == {
        "status": "absent", "report_has_positive_traffic": None,
        "configured_has_positive_traffic": None, "report_is_only_serving_revision": None,
    }


@pytest.mark.parametrize("traffic", [
    PRIVATE,
    {"revisionName": PRIVATE, "percent": 100},
    [PRIVATE],
    [{"percent": 100}],
    [{"revisionName": PRIVATE, "percent": None}],
    [{"revisionName": PRIVATE}],
    [{"revisionName": PRIVATE, "percent": True}],
    [{"revisionName": PRIVATE, "percent": "100"}],
    [{"revisionName": PRIVATE, "percent": 100.0}],
    [{"revisionName": PRIVATE, "percent": -1}],
    [{"revisionName": PRIVATE, "percent": 101}],
    [{"revisionName": PRIVATE, "percent": 99}],
    [{"revisionName": PRIVATE, "percent": 0, "tag": PRIVATE}],
    [{"revisionName": PRIVATE, "percent": 75}, {"revisionName": PRIVATE + "-other", "percent": 75}],
    [{"revisionName": {"nested": PRIVATE}, "percent": 100}],
    [{"revisionName": "", "percent": 100}],
    [{"revisionName": PRIVATE + "\nrevision", "percent": 100}],
])
def test_invalid_traffic_never_guesses_serving_or_emits_private_values(monkeypatch, traffic):
    _environment(monkeypatch)
    expected = diagnostic.resolve_expected()
    cloud = _metadata(expected)
    cloud["status"]["traffic"] = traffic
    result = _diagnose(_report(expected), expected, cloud_run_metadata=cloud)
    assert result["serving_traffic"] == {
        "status": "invalid", "report_has_positive_traffic": None,
        "configured_has_positive_traffic": None, "report_is_only_serving_revision": None,
    }
    assert PRIVATE not in json.dumps(result)


def test_private_extra_traffic_fields_do_not_change_fixed_projection(monkeypatch):
    _environment(monkeypatch)
    expected = diagnostic.resolve_expected()
    cloud = _metadata(expected)
    cloud["status"]["traffic"] = [{
        "revisionName": expected["expected_runtime_revision"], "percent": 100,
        "private_account": {"nested": PRIVATE}, "url": PRIVATE, "tag": PRIVATE,
    }]
    result = _diagnose(_report(expected), expected, cloud_run_metadata=cloud)
    assert result["serving_traffic"]["status"] == "single"
    assert result["serving_traffic"]["report_is_only_serving_revision"] is True
    assert PRIVATE not in json.dumps(result)


def test_traffic_with_wrong_service_metadata_cannot_prove_serving(monkeypatch):
    _environment(monkeypatch)
    expected = diagnostic.resolve_expected()
    cloud = _metadata(expected)
    cloud["metadata"]["name"] = PRIVATE + "-other-service"
    cloud["status"]["traffic"] = [{"revisionName": expected["expected_runtime_revision"], "percent": 100}]
    result = _diagnose(_report(expected), expected, cloud_run_metadata=cloud)
    assert result["cloud_run_metadata_status"] == "invalid"
    assert result["serving_traffic"] == {
        "status": "invalid", "report_has_positive_traffic": None,
        "configured_has_positive_traffic": None, "report_is_only_serving_revision": None,
    }


def test_preflight_includes_unknown_serving_diagnostic_without_cloud_calls(monkeypatch, capsys):
    _environment(monkeypatch)
    assert diagnostic.main(["--preflight"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["serving_traffic"]["status"] == "absent"
    assert result["serving_traffic"]["report_has_positive_traffic"] is None


def test_serving_diagnostic_does_not_change_original_cli_exit_gate(monkeypatch, capsys):
    _environment(monkeypatch)
    expected = diagnostic.resolve_expected()
    cloud = _metadata(expected)
    cloud["status"]["traffic"] = [{"revisionName": PRIVATE + "-other", "percent": 100}]
    monkeypatch.setattr(publisher, "_latest_report_uri", lambda **kwargs: expected["expected_report_prefix"] + "/r.json")
    monkeypatch.setattr(publisher, "_load_gcs_report", lambda *args, **kwargs: _report(expected))
    monkeypatch.setattr(diagnostic, "_read_cloud_run_metadata", lambda *args: cloud)
    assert diagnostic.main([], now=NOW) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["projection_passed"] is True
    assert result["serving_traffic"]["report_has_positive_traffic"] is False
    assert result["serving_traffic"]["report_is_only_serving_revision"] is False


def test_revision_rejection_does_not_hide_valid_snapshot_structure(monkeypatch):
    _environment(monkeypatch)
    expected = diagnostic.resolve_expected()
    report = _report(expected)
    report["diagnostics"]["runtime_revision"] = PRIVATE + "-serving"
    report["runtime_release_receipt"]["runtime_revision"] = PRIVATE + "-serving"
    result = _diagnose(report, expected)
    assert result["reason"] == "runtime_target_mismatch"
    assert result["mismatched_fields"] == ["runtime_revision"]
    assert result["projection_passed"] is False
    assert result["account_facts_stages"] == {
        "report_envelope": "verified",
        "source_report_provenance": "verified",
        "scope_identity": "verified",
        "snapshot_schema": "verified",
        "account_identity": "verified",
        "observation_interval": "verified",
        "balance_cash": "verified",
        "positions": "unknown",
    }


_STAGES = (
    "report_envelope", "source_report_provenance", "scope_identity", "snapshot_schema",
    "account_identity", "observation_interval", "balance_cash", "positions",
)


def _staged_report(monkeypatch):
    _environment(monkeypatch)
    expected = diagnostic.resolve_expected()
    report = _report(expected)
    report["diagnostics"]["runtime_revision"] = PRIVATE + "-serving"
    report["runtime_release_receipt"]["runtime_revision"] = PRIVATE + "-serving"
    return expected, report, report["summary"]["account_facts"]


def _assert_fixed_stages(result):
    stages = result["account_facts_stages"]
    assert tuple(stages) == _STAGES
    assert set(stages.values()) <= {"verified", "invalid", "unknown"}
    encoded = json.dumps(result)
    assert all(marker not in encoded for marker in (
        PRIVATE, "U000000", "12345.6700", "100.00", "2026-10-13", "XYZ", "gs://", "scope-secret",
    ))
    assert result["projection_passed"] is False
    return stages


@pytest.mark.parametrize("field", ["project_id", "service_name", "account_scope", "deployment_selector", "account_selector", "receipt_revision"])
def test_other_identity_failures_leave_dependent_fact_stages_unknown(monkeypatch, field):
    expected, report, _ = _staged_report(monkeypatch)
    if field in {"project_id", "service_name", "account_scope"}:
        report[field] = PRIVATE + "-scope-secret"
    elif field in {"deployment_selector", "account_selector"}:
        report["runtime_target"][field] = ["U00000099"] if field == "account_selector" else PRIVATE + "-scope-secret"
    else:
        report["runtime_release_receipt"]["runtime_revision"] = PRIVATE + "-different"
    stages = _assert_fixed_stages(_diagnose(report, expected))
    assert stages["scope_identity"] == "invalid"
    assert all(stages[name] == "unknown" for name in _STAGES[3:])


@pytest.mark.parametrize("field,value", [
    ("schema_version", PRIVATE), ("platform", PRIVATE), ("deploy_target", PRIVATE),
    ("runtime_target", []), ("diagnostics", []), ("runtime_release_receipt", []),
])
def test_invalid_envelope_does_not_attribute_an_independent_snapshot(monkeypatch, field, value):
    expected, report, _ = _staged_report(monkeypatch)
    report[field] = value
    stages = _assert_fixed_stages(_diagnose(report, expected))
    assert stages["report_envelope"] == "invalid"
    assert all(stages[name] == "unknown" for name in _STAGES[2:])


@pytest.mark.parametrize("uri", ["gs://other-private-marker/reports/r.json", "gs://private-marker-never-public/wrong/r.json", "https://private-marker-never-public/reports/r.json"])
def test_provenance_failure_does_not_validate_foreign_account_facts(monkeypatch, uri):
    expected, report, _ = _staged_report(monkeypatch)
    result = diagnostic.diagnose_identity(report, expected=expected, source_report_uri=uri, now=NOW, source_sha=SHA)
    stages = _assert_fixed_stages(result)
    assert stages["source_report_provenance"] == "invalid"
    assert all(stages[name] == "unknown" for name in _STAGES[2:])


@pytest.mark.parametrize("failure", ["schema", "currency", "summary", "facts"])
def test_invalid_snapshot_after_revision_rejection_is_observed_without_accepting_identity(monkeypatch, failure):
    expected, report, facts = _staged_report(monkeypatch)
    if failure == "schema":
        facts["schema_version"] = PRIVATE
    elif failure == "currency":
        facts["currency"] = "EUR"
    elif failure == "summary":
        report["summary"] = []
    else:
        report["summary"]["account_facts"] = None
    result = _diagnose(report, expected)
    stages = _assert_fixed_stages(result)
    assert result["mismatched_fields"] == ["runtime_revision"]
    assert stages["snapshot_schema"] == "invalid"
    assert all(stages[name] == "unknown" for name in _STAGES[4:])


@pytest.mark.parametrize("ids", [None, [], ["U00000099"], ["U00000001", "U00000099"], [" U00000001"], ["default"]])
def test_cross_or_unattributed_account_facts_cannot_verify_amount_or_position_structure(monkeypatch, ids):
    expected, report, facts = _staged_report(monkeypatch)
    facts["account_ids"] = ids
    stages = _assert_fixed_stages(_diagnose(report, expected))
    assert stages["account_identity"] == "invalid"
    assert all(stages[name] == "unknown" for name in _STAGES[5:])


@pytest.mark.parametrize("failure", ["naive", "missing", "reversed", "before", "after"])
def test_invalid_observation_interval_stays_distinct_from_freshness_and_identity(monkeypatch, failure):
    expected, report, facts = _staged_report(monkeypatch)
    if failure == "naive":
        facts["observed_at"] = "2026-10-13T12:00:00"
    elif failure == "missing":
        report.pop("started_at")
    elif failure == "reversed":
        report["finished_at"] = (NOW - timedelta(seconds=3)).isoformat()
    elif failure == "before":
        facts["observed_at"] = (NOW - timedelta(seconds=3)).isoformat()
    else:
        facts["observed_at"] = (NOW + timedelta(seconds=1)).isoformat()
    stages = _assert_fixed_stages(_diagnose(report, expected))
    assert stages["observation_interval"] == "invalid"
    assert stages["balance_cash"] == stages["positions"] == "unknown"


@pytest.mark.parametrize("failure", ["number", "nan", "scale", "magnitude", "empty_cash", "duplicate_currency", "cash_tag", "cash_currency", "cash_number"])
def test_original_decimal_and_native_cash_predicates_reveal_hidden_malformed_facts(monkeypatch, failure):
    expected, report, facts = _staged_report(monkeypatch)
    if failure in {"number", "nan", "scale", "magnitude"}:
        facts["net_assets"] = {"number": 12.3, "nan": "NaN", "scale": "0.000000001", "magnitude": "1234567890123456"}[failure]
    elif failure == "empty_cash":
        facts["cash"] = []
    elif failure == "duplicate_currency":
        facts["cash"] = facts["cash"] * 2
    elif failure == "cash_tag":
        facts["cash"][0]["source_tag"] = PRIVATE
    elif failure == "cash_currency":
        facts["cash"][0]["currency"] = "US"
    else:
        facts["cash"][0]["cash_balance"] = True
    stages = _assert_fixed_stages(_diagnose(report, expected))
    assert stages["balance_cash"] == "invalid"


@pytest.mark.parametrize("net_assets,cash", [(None, "0"), ("-1.20", "-0.01"), ("0.00000001", "0.00000001")])
def test_structural_validation_retains_nullable_negative_zero_and_small_native_amounts(monkeypatch, net_assets, cash):
    expected, report, facts = _staged_report(monkeypatch)
    facts["net_assets"], facts["cash"][0]["cash_balance"] = net_assets, cash
    stages = _assert_fixed_stages(_diagnose(report, expected))
    assert stages["balance_cash"] == "verified"


@pytest.mark.parametrize("failure", ["missing", "unsupported_scope", "empty", "unknown_field", "decimal", "duplicate", "valid"])
def test_positions_only_verify_explicit_projectable_stock_facts(monkeypatch, failure):
    expected, report, facts = _staged_report(monkeypatch)
    if failure != "missing":
        facts["broker_reported_positions_scope"] = publisher.POSITIONS_SCOPE
        facts["broker_reported_positions"] = [{"symbol": "XYZ", "quantity": "-1", "market_value": "0", "currency": "USD", "avg_cost": "1.01"}]
    if failure == "unsupported_scope":
        facts["broker_reported_positions_scope"] = PRIVATE
    elif failure == "empty":
        facts["broker_reported_positions"] = []
    elif failure == "unknown_field":
        facts["broker_reported_positions"][0][PRIVATE] = PRIVATE
    elif failure == "decimal":
        facts["broker_reported_positions"][0]["quantity"] = 1.0
    elif failure == "duplicate":
        facts["broker_reported_positions"] *= 2
    stages = _assert_fixed_stages(_diagnose(report, expected))
    assert stages["positions"] == ("unknown" if failure in {"missing", "unsupported_scope"} else "verified" if failure == "valid" else "invalid")


def test_stage_comparisons_do_not_mutate_report_or_expected_and_original_projection_runs_once(monkeypatch):
    from copy import deepcopy

    expected, report, _ = _staged_report(monkeypatch)
    original_expected, original_report = deepcopy(expected), deepcopy(report)
    project = publisher.project_ibkr_account_facts_history
    calls = []

    def tracked(*args, **kwargs):
        calls.append(kwargs["expected_runtime_revision"])
        return project(*args, **kwargs)

    monkeypatch.setattr(publisher, "project_ibkr_account_facts_history", tracked)
    _assert_fixed_stages(_diagnose(report, expected))
    assert expected == original_expected and report == original_report
    assert calls == [original_expected["expected_runtime_revision"]]


def test_unexpected_optional_probe_exception_leaves_original_rejection_and_redacted_unknown(monkeypatch):
    expected, report, _ = _staged_report(monkeypatch)

    def broken(*args, **kwargs):
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(publisher, "_project_cash", broken)
    result = _diagnose(report, expected)
    stages = _assert_fixed_stages(result)
    assert result["reason"] == "runtime_target_mismatch"
    assert stages["balance_cash"] == stages["positions"] == "unknown"


@pytest.mark.parametrize("field", ["expected_runtime_revision", "expected_service_name", "target_id", "expected_account_selector"])
def test_incomplete_protected_expectation_cannot_create_scope_stage_proof(monkeypatch, field):
    expected, report, _ = _staged_report(monkeypatch)
    expected[field] = [] if field == "expected_account_selector" else ""
    stages = _assert_fixed_stages(_diagnose(report, expected))
    assert stages["scope_identity"] == "unknown"
    assert all(stages[name] == "unknown" for name in _STAGES[3:])


def test_missing_report_revision_cannot_prove_account_fact_scope(monkeypatch):
    expected, report, _ = _staged_report(monkeypatch)
    report["diagnostics"].pop("runtime_revision")
    report["runtime_release_receipt"].pop("runtime_revision")
    stages = _assert_fixed_stages(_diagnose(report, expected))
    assert stages["scope_identity"] == "unknown"
    assert all(stages[name] == "unknown" for name in _STAGES[3:])


@pytest.mark.parametrize("receipt", [None, {"attestation_state": "legacy_unattested"}])
def test_legacy_optional_receipt_does_not_fabricate_release_approval_or_hide_structural_facts(monkeypatch, receipt):
    expected, report, _ = _staged_report(monkeypatch)
    report["runtime_release_receipt"] = receipt
    result = _diagnose(report, expected)
    stages = _assert_fixed_stages(result)
    assert stages["balance_cash"] == "verified"
    assert result["revision_equalities"]["receipt_equals_report"] is None
    assert "release_approved" not in result and "full_guard_pass" not in result


@pytest.mark.parametrize("revision_mismatch", [True, False])
def test_stages_do_not_change_original_cli_gate_or_add_cloud_reads(monkeypatch, capsys, revision_mismatch):
    expected, report, facts = _staged_report(monkeypatch)
    if not revision_mismatch:
        report["diagnostics"]["runtime_revision"] = expected["expected_runtime_revision"]
        report["runtime_release_receipt"]["runtime_revision"] = expected["expected_runtime_revision"]
    facts["broker_reported_positions_scope"] = publisher.POSITIONS_SCOPE
    facts["broker_reported_positions"] = []  # Optional positions remain omitted by the original publisher.
    calls = []

    def read_report(*args, **kwargs):
        calls.append("report")
        return report

    def read_cloud(*args, **kwargs):
        calls.append("cloud")
        return _metadata(expected)

    monkeypatch.setattr(publisher, "_latest_report_uri", lambda **kwargs: expected["expected_report_prefix"] + "/r.json")
    monkeypatch.setattr(publisher, "_load_gcs_report", read_report)
    monkeypatch.setattr(diagnostic, "_read_cloud_run_metadata", read_cloud)
    assert diagnostic.main([], now=NOW) == (1 if revision_mismatch else 0)
    captured = capsys.readouterr()
    assert PRIVATE not in captured.out + captured.err
    result = json.loads(captured.out)
    assert result["projection_passed"] is not revision_mismatch
    assert result["account_facts_stages"]["positions"] == "invalid"
    assert calls == ["report", "cloud"]


def test_preflight_and_failed_reads_keep_all_account_fact_stages_unknown(monkeypatch, capsys):
    _environment(monkeypatch)
    assert diagnostic.main(["--preflight"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["account_facts_stages"] == dict.fromkeys(_STAGES, "unknown")
    monkeypatch.setattr(publisher, "_latest_report_uri", lambda **kwargs: (_ for _ in ()).throw(publisher._ProjectionError("report_unavailable")))
    assert diagnostic.main([], now=NOW) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["account_facts_stages"] == dict.fromkeys(_STAGES, "unknown")


def test_snapshot_issue_distinguishes_missing_facts_without_changing_revision_rejection(monkeypatch):
    expected, report, _ = _staged_report(monkeypatch)
    report["summary"].pop("account_facts")
    result = _diagnose(report, expected)
    assert result["snapshot_issue"] == "account_facts_absent"
    assert result["account_facts_stages"]["snapshot_schema"] == "invalid"
    assert result["projection_passed"] is False
    assert result["reason"] == "runtime_target_mismatch"
    assert result["mismatched_fields"] == ["runtime_revision"]


_SNAPSHOT_ISSUES = frozenset({
    "unknown", "none", "summary_absent", "summary_not_object",
    "account_facts_absent", "account_facts_not_object", "snapshot_schema_missing",
    "snapshot_schema_invalid", "currency_missing", "currency_invalid",
})


@pytest.mark.parametrize("failure,issue", [
    ("summary_absent", "summary_absent"),
    ("summary_null", "summary_not_object"),
    ("summary_list", "summary_not_object"),
    ("summary_private", "summary_not_object"),
    ("facts_absent", "account_facts_absent"),
    ("facts_null", "account_facts_not_object"),
    ("facts_list", "account_facts_not_object"),
    ("facts_private", "account_facts_not_object"),
    ("schema_absent", "snapshot_schema_missing"),
    ("schema_null", "snapshot_schema_invalid"),
    ("schema_boolean", "snapshot_schema_invalid"),
    ("schema_object", "snapshot_schema_invalid"),
    ("schema_private", "snapshot_schema_invalid"),
    ("currency_absent", "currency_missing"),
    ("currency_null", "currency_invalid"),
    ("currency_boolean", "currency_invalid"),
    ("currency_object", "currency_invalid"),
    ("currency_private", "currency_invalid"),
    ("currency_non_usd", "currency_invalid"),
    ("both_invalid", "snapshot_schema_invalid"),
])
def test_snapshot_issue_classifies_only_existing_invalid_snapshot_predicates(monkeypatch, failure, issue):
    from copy import deepcopy

    expected, report, facts = _staged_report(monkeypatch)
    if failure.startswith("summary_"):
        if failure == "summary_absent":
            report.pop("summary")
        else:
            report["summary"] = {"summary_null": None, "summary_list": [], "summary_private": PRIVATE}[failure]
    elif failure.startswith("facts_"):
        if failure == "facts_absent":
            report["summary"].pop("account_facts")
        else:
            report["summary"]["account_facts"] = {"facts_null": None, "facts_list": [], "facts_private": PRIVATE}[failure]
    elif failure.startswith("schema_"):
        if failure == "schema_absent":
            facts.pop("schema_version")
        else:
            facts["schema_version"] = {"schema_null": None, "schema_boolean": True, "schema_object": {PRIVATE: PRIVATE}, "schema_private": PRIVATE}[failure]
    elif failure.startswith("currency_"):
        if failure == "currency_absent":
            facts.pop("currency")
        else:
            facts["currency"] = {"currency_null": None, "currency_boolean": True, "currency_object": {PRIVATE: PRIVATE}, "currency_private": PRIVATE, "currency_non_usd": "EUR"}[failure]
    else:
        facts["schema_version"] = PRIVATE
        facts["currency"] = PRIVATE
    original_report, original_expected = deepcopy(report), deepcopy(expected)
    result = _diagnose(report, expected)
    stages = _assert_fixed_stages(result)
    assert result["snapshot_issue"] == issue
    assert result["snapshot_issue"] in _SNAPSHOT_ISSUES
    assert stages["snapshot_schema"] == "invalid"
    assert all(stages[name] == "unknown" for name in _STAGES[4:])
    assert result["reason"] == "runtime_target_mismatch"
    assert result["mismatched_fields"] == ["runtime_revision"]
    assert report == original_report and expected == original_expected


@pytest.mark.parametrize("failure", ["revision_only", "balance", "account", "observation", "positions"])
def test_snapshot_issue_none_only_means_schema_and_currency_predicates_passed(monkeypatch, failure):
    expected, report, facts = _staged_report(monkeypatch)
    if failure == "balance":
        facts["net_assets"] = PRIVATE
    elif failure == "account":
        facts["account_ids"] = ["U00000099"]
    elif failure == "observation":
        facts["observed_at"] = PRIVATE
    elif failure == "positions":
        facts["broker_reported_positions_scope"] = publisher.POSITIONS_SCOPE
        facts["broker_reported_positions"] = []
    result = _diagnose(report, expected)
    stages = _assert_fixed_stages(result)
    assert result["snapshot_issue"] == "none"
    assert stages["snapshot_schema"] == "verified"
    assert result["reason"] == "runtime_target_mismatch"
    assert "approved" not in json.dumps(result)


@pytest.mark.parametrize("failure", ["scope", "envelope", "receipt", "expected", "report_revision", "provenance"])
def test_snapshot_issue_stays_unknown_without_original_stage_prerequisites(monkeypatch, failure):
    expected, report, _ = _staged_report(monkeypatch)
    report.pop("summary")
    uri = expected["expected_report_prefix"] + "/r.json"
    if failure == "scope":
        report["service_name"] = PRIVATE + "-other"
    elif failure == "envelope":
        report["schema_version"] = PRIVATE
    elif failure == "receipt":
        report["runtime_release_receipt"]["runtime_revision"] = PRIVATE + "-other"
    elif failure == "expected":
        expected["expected_runtime_revision"] = ""
    elif failure == "report_revision":
        report["diagnostics"].pop("runtime_revision")
    else:
        uri = "gs://other-private-marker/r.json"
    result = diagnostic.diagnose_identity(report, expected=expected, source_report_uri=uri, now=NOW, source_sha=SHA)
    stages = _assert_fixed_stages(result)
    assert stages["snapshot_schema"] == "unknown"
    assert result["snapshot_issue"] == "unknown"


def test_snapshot_issue_optional_mapping_error_is_redacted_and_cannot_replace_guard(monkeypatch):
    class BrokenSnapshot(dict):
        def __getitem__(self, key):
            raise RuntimeError(PRIVATE)

    expected, report, facts = _staged_report(monkeypatch)
    facts["schema_version"] = PRIVATE
    report["summary"]["account_facts"] = BrokenSnapshot(facts)
    result = _diagnose(report, expected)
    stages = _assert_fixed_stages(result)
    assert stages["snapshot_schema"] == "invalid"
    assert result["snapshot_issue"] == "unknown"
    assert result["reason"] == "runtime_target_mismatch"


def test_snapshot_issue_preflight_and_failed_report_read_are_unknown(monkeypatch, capsys):
    _environment(monkeypatch)
    assert diagnostic.main(["--preflight"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["snapshot_issue"] == "unknown"
    monkeypatch.setattr(publisher, "_latest_report_uri", lambda **kwargs: (_ for _ in ()).throw(publisher._ProjectionError("report_unavailable")))
    assert diagnostic.main([], now=NOW) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["snapshot_issue"] == "unknown"


@pytest.mark.parametrize("snapshot_invalid", [True, False])
def test_snapshot_issue_preserves_original_cli_failure_or_success_and_cloud_read_count(monkeypatch, capsys, snapshot_invalid):
    _environment(monkeypatch)
    expected = diagnostic.resolve_expected()
    report = _report(expected)
    if snapshot_invalid:
        report["summary"].pop("account_facts")
    reads = []

    def report_read(*args, **kwargs):
        reads.append("report")
        return report

    def cloud_read(*args, **kwargs):
        reads.append("cloud")
        return _metadata(expected)

    monkeypatch.setattr(publisher, "_latest_report_uri", lambda **kwargs: expected["expected_report_prefix"] + "/r.json")
    monkeypatch.setattr(publisher, "_load_gcs_report", report_read)
    monkeypatch.setattr(diagnostic, "_read_cloud_run_metadata", cloud_read)
    assert diagnostic.main([], now=NOW) == (1 if snapshot_invalid else 0)
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["snapshot_issue"] == ("account_facts_absent" if snapshot_invalid else "none")
    assert result["reason"] == ("account_facts_invalid" if snapshot_invalid else "identity_verified")
    assert result["projection_passed"] is not snapshot_invalid
    assert reads == ["report", "cloud"]
    assert PRIVATE not in captured.out + captured.err
    assert not any(field in result for field in ("report_status", "skip_reason", "failure_category"))


@pytest.mark.parametrize("status,field,reason", [
    ("skipped", "skip_reason", "already_running"),
    ("skipped", "skip_reason", "market_closed"),
    ("skipped", "skip_reason", "ibkr_fractional_equity_api_unsupported"),
    ("error", "failure_category", "strategy_execution_blocked"),
    ("error", "failure_category", "strategy_deadline"),
    ("error", "failure_category", "ibkr_gateway_unavailable"),
    ("error", "failure_category", "ibkr_trading_permission"),
    ("error", "failure_category", "ibkr_portfolio_snapshot"),
])
def test_report_stop_reason_is_declared_category_not_inferred_snapshot_cause(monkeypatch, status, field, reason):
    from copy import deepcopy

    expected, report, _ = _staged_report(monkeypatch)
    report["summary"].pop("account_facts")
    report["status"] = status
    report["diagnostics"][field] = reason
    original_report, original_expected = deepcopy(report), deepcopy(expected)
    result = _diagnose(report, expected)
    assert result["report_stop_reason"] == reason
    assert result["snapshot_issue"] == "account_facts_absent"
    assert result["reason"] == "runtime_target_mismatch"
    assert result["projection_passed"] is False
    assert report == original_report and expected == original_expected
    assert PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("status,skip,failure,reason", [
    ("ok", None, None, "no_declared_stop"),
    ("skipped", PRIVATE, None, "unclassified_skip"),
    ("error", None, PRIVATE, "unclassified_error"),
    ("skipped", [], None, "unclassified_skip"),
    ("error", None, {}, "unclassified_error"),
    ("ok", "market_closed", None, "invalid_stop_context"),
    ("skipped", "market_closed", "strategy_deadline", "invalid_stop_context"),
    ("error", "market_closed", "strategy_deadline", "invalid_stop_context"),
    ("pending", None, None, "unknown"),
    (PRIVATE, None, None, "unknown"),
    ([], None, None, "unknown"),
])
def test_report_stop_reason_rejects_conflicts_and_does_not_echo_free_text(monkeypatch, status, skip, failure, reason):
    expected, report, _ = _staged_report(monkeypatch)
    report["status"] = status
    report["diagnostics"].update(skip_reason=skip, failure_category=failure)
    result = _diagnose(report, expected)
    assert result["report_stop_reason"] == reason
    assert PRIVATE not in json.dumps(result)
    assert result["projection_passed"] is False


@pytest.mark.parametrize("broken", ["envelope", "scope", "provenance"])
def test_report_stop_reason_is_unknown_before_existing_scope_proof(monkeypatch, broken):
    expected, report, _ = _staged_report(monkeypatch)
    report["status"] = "skipped"
    report["diagnostics"]["skip_reason"] = "market_closed"
    uri = expected["expected_report_prefix"] + "/r.json"
    if broken == "envelope":
        report["schema_version"] = PRIVATE
    elif broken == "scope":
        report["service_name"] = PRIVATE + "-other"
    else:
        uri = "gs://other-private-marker/r.json"
    result = diagnostic.diagnose_identity(report, expected=expected, source_report_uri=uri, now=NOW, source_sha=SHA)
    assert result["report_stop_reason"] == "unknown"
    assert result["projection_passed"] is False


def test_report_stop_reason_preflight_preserves_zero_cloud_calls(monkeypatch, capsys):
    _environment(monkeypatch)
    assert diagnostic.main(["--preflight"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["report_stop_reason"] == "unknown"


def _source_metadata_case(monkeypatch):
    _environment(monkeypatch)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_REPOSITORY", "QuantStrategyLab/InteractiveBrokersPlatform")
    monkeypatch.setenv("GH_TOKEN", PRIVATE)
    expected = diagnostic.resolve_expected()
    selected_source = "b" * 40
    serving = expected["expected_service_name"] + "-00010-abc"
    target = {
        "service_name": expected["expected_service_name"],
        "account_scope": expected["expected_account_scope"],
        "account_selector": expected["expected_account_selector"],
        "deployment_selector": expected["expected_deployment_selector"],
    }
    service = {"metadata": {"name": expected["expected_service_name"]},
               "status": {"traffic": [{"revisionName": serving, "percent": 100}]}}
    revision = {"metadata": {"name": serving, "labels": {"commit-sha": selected_source, "github-run-id": "2468"}},
                "spec": {"containers": [{"image": "us-central1-docker.pkg.dev/private-image/repo@sha256:" + "f" * 64,
                                          "env": [{"name": "RUNTIME_TARGET_JSON", "value": json.dumps(target)}]}]}}
    run = {"id": 2468, "path": ".github/workflows/sync-cloud-run-env.yml",
           "repository": {"full_name": "QuantStrategyLab/InteractiveBrokersPlatform"},
           "head_branch": "main", "event": "workflow_dispatch", "status": "completed", "conclusion": "success",
           "head_sha": "c" * 40}  # deliberately different from selected image source
    step_names = ("Checkout selected image source", "Verify selected image source",
                  "Build, push, and deploy Cloud Run image", "Verify no-traffic deployment readback")
    jobs = {"total_count": 1, "jobs": [{"id": 2469, "run_id": 2468, "name": "Deploy / Sync Cloud Run",
            "status": "completed", "conclusion": "success",
            "steps": [{"name": n, "status": "completed", "conclusion": "success"} for n in step_names]}]}
    plan = {"targets": [{"service_name": expected["expected_service_name"],
                         "env": {"RUNTIME_TARGET_JSON": json.dumps(target)}}]}
    verify_group = ('##[group]Run set -euo pipefail\n'
                    '  echo "Selected source must be a full commit SHA."\n'
                    'checked_out_sha="$(git -C source rev-parse HEAD)"\n'
                    '  echo "Checked-out image source does not match source_commit."\n'
                    'env:\n    SOURCE_COMMIT: ' + selected_source + '\n##[endgroup]\n')
    build_group = ('##[group]Run set -euo pipefail\n'
                   'docker build --pull -t "${image}" source\n'
                   'gcloud run deploy "${cloud_run_service}" \\\n'
                   'env:\n    SOURCE_COMMIT: ' + selected_source + '\n'
                   '    GCP_PROJECT_ID: ' + expected["expected_project_id"] + '\n'
                   '    CLOUD_RUN_REGION: us-central1\n'
                   '    SYNC_PLAN_JSON: ' + json.dumps(plan) + '\n##[endgroup]\n')
    readback_group = ('##[group]Run set -euo pipefail\n'
                      'python3 scripts/verify_cloud_run_no_traffic_deploy.py verify \\\n'
                      '  --expected-image-digest="${EXPECTED_IMAGE_DIGEST}"\n'
                      'env:\n    EXPECTED_IMAGE_DIGEST: sha256:' + "f" * 64 + '\n##[endgroup]\n')
    case = {"service": service, "revision": revision, "run": run, "jobs": jobs,
            "logs": verify_group + build_group + readback_group + PRIVATE, "calls": [], "expected": expected}

    def fake(args, **_kwargs):
        args = tuple(args)
        case["calls"].append(args)
        if args[:4] == ("gcloud", "run", "services", "describe"):
            value = case.get("after_service", service) if sum(c[:4] == args[:4] for c in case["calls"]) > 1 else service
        elif args[:4] == ("gcloud", "run", "revisions", "describe"):
            value = revision
        elif args[:4] == ("gh", "api", "--hostname", "github.com"):
            route = args[4]
            if route.endswith("/actions/runs/2468"):
                value = run
            elif route.endswith("/actions/runs/2468/jobs?per_page=100"):
                value = jobs
            elif route.endswith("/actions/jobs/2469/logs"):
                return subprocess.CompletedProcess(args, 0, case["logs"], "")
            else:
                pytest.fail("unexpected GitHub locator")
        else:
            pytest.fail("metadata-only mode attempted forbidden command")
        if case.get("read_failure"):
            return subprocess.CompletedProcess(args, 1, PRIVATE, PRIVATE)
        return subprocess.CompletedProcess(args, 0, json.dumps(value), "")

    monkeypatch.setattr(diagnostic.subprocess, "run", fake)
    # Any accidental financial report read fails immediately.
    monkeypatch.setattr(publisher, "_latest_report_uri", lambda **_kw: pytest.fail("report listing forbidden"))
    monkeypatch.setattr(publisher, "_load_gcs_report", lambda *_a, **_kw: pytest.fail("report read forbidden"))
    return case


def test_source_metadata_only_uses_selected_source_target_and_stable_serving(monkeypatch, capsys):
    case = _source_metadata_case(monkeypatch)
    case["logs"] = "    SOURCE_COMMIT: " + case["run"]["head_sha"] + "\n" + case["logs"]
    assert diagnostic.main(["--serving-source-metadata-only"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["reason"] == "deploy_provenance_verified"
    assert out["selected_source_sha"] == "b" * 40 and out["selected_source_sha"] != case["run"]["head_sha"]
    assert all(v is True for v in out["scope_equalities"].values())
    assert out["immutable_image_status"] == "verified" and out["serving_stable"] is True
    assert out["loaded_source_status"] == out["independent_release_approval"] == "unknown"
    assert PRIVATE not in json.dumps(out) and "private-image" not in json.dumps(out)
    assert len(case["calls"]) == 6


def test_source_metadata_only_workflow_wire_preserves_default_report_mode():
    workflow = Path(".github/workflows/account-facts-identity-diagnose.yml").read_text()
    assert "serving_source_metadata_only:\n" in workflow
    assert "        type: boolean\n        required: true\n        default: false" in workflow
    assert "actions: read" in workflow and "actions: write" not in workflow
    old_step = "      - name: Read report and service revision equality\n        if: ${{ !inputs.backup_primary_source_state && !inputs.serving_source_metadata_only && !inputs.registry_source_metadata_only && !inputs.static_source_only }}\n        run: python3 -m scripts.diagnose_account_facts_identity\n"
    assert old_step in workflow
    new_step = workflow.split("      - name: Read serving source metadata without reports or image execution\n", 1)[1].split("\n      - name: Read registry provenance shape without reports or image execution\n", 1)[0]
    assert new_step == ("        if: ${{ inputs.serving_source_metadata_only && !inputs.registry_source_metadata_only && !inputs.static_source_only }}\n"
                        "        env:\n          GH_TOKEN: ${{ github.token }}\n"
                        "        run: python3 -m scripts.diagnose_account_facts_identity --serving-source-metadata-only\n")
    assert "GH_TOKEN" not in workflow.split(old_step)[0]


def test_source_metadata_only_invalid_protected_config_stops_before_cloud(monkeypatch, capsys):
    case = _source_metadata_case(monkeypatch)
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON", "[]")
    assert diagnostic.main(["--serving-source-metadata-only"]) == 1
    assert case["calls"] == []
    out = json.loads(capsys.readouterr().out)
    assert out["reason"] == "target_config_invalid" and out["selected_source_sha"] is None


@pytest.mark.parametrize("failure", ["split", "traffic_total", "target", "missing_target", "revision", "digest",
                                      "label", "run_locator", "run_repo", "run_branch", "run_path", "run_event",
                                      "step", "job", "source", "duplicate_source_group", "plan_target",
                                      "masked_plan", "readback_digest", "retarget", "read_failure"])
def test_source_metadata_only_stops_on_unproven_provenance(monkeypatch, capsys, failure):
    case = _source_metadata_case(monkeypatch)
    if failure == "split":
        case["service"]["status"]["traffic"] = [{"revisionName": PRIVATE + "-r1", "percent": 50},
                                               {"revisionName": PRIVATE + "-r2", "percent": 50}]
    elif failure == "traffic_total":
        case["service"]["status"]["traffic"][0]["percent"] = 99
    elif failure in {"target", "missing_target"}:
        target = json.loads(case["revision"]["spec"]["containers"][0]["env"][0]["value"])
        if failure == "target":
            target["account_selector"] = ["U00000002"]
        else:
            target.pop("account_scope")
        case["revision"]["spec"]["containers"][0]["env"][0]["value"] = json.dumps(target)
    elif failure == "revision":
        case["revision"]["metadata"]["name"] = PRIVATE + "-wrong"
    elif failure == "digest":
        case["revision"]["spec"]["containers"][0]["image"] = "us-central1-docker.pkg.dev/private-image/repo:latest"
    elif failure in {"label", "run_locator"}:
        if failure == "label":
            case["revision"]["metadata"]["labels"].pop("commit-sha")
        else:
            case["revision"]["metadata"]["labels"]["github-run-id"] = "2468/../../private"
    elif failure.startswith("run_"):
        field = {"run_repo": "repository", "run_branch": "head_branch", "run_path": "path", "run_event": "event"}[failure]
        case["run"][field] = {"full_name": PRIVATE} if field == "repository" else PRIVATE
    elif failure == "step":
        case["jobs"]["jobs"][0]["steps"][1]["conclusion"] = "skipped"
    elif failure == "job":
        case["jobs"]["jobs"][0]["name"] = PRIVATE
    elif failure == "source":
        case["logs"] = case["logs"].replace("b" * 40, case["run"]["head_sha"])
    elif failure == "duplicate_source_group":
        case["logs"] += "\n" + case["logs"].split("##[endgroup]", 1)[0] + "##[endgroup]\n"
    elif failure == "plan_target":
        case["logs"] = case["logs"].replace('"service_name": "' + case["expected"]["expected_service_name"] + '"',
                                              '"service_name": "' + PRIVATE + '-other"')
    elif failure == "masked_plan":
        case["logs"] = "\n".join("    SYNC_PLAN_JSON: ***" if "SYNC_PLAN_JSON:" in line else line
                                  for line in case["logs"].splitlines())
    elif failure == "readback_digest":
        case["logs"] = case["logs"].replace("EXPECTED_IMAGE_DIGEST: sha256:" + "f" * 64,
                                              "EXPECTED_IMAGE_DIGEST: sha256:" + "e" * 64)
    elif failure == "retarget":
        case["after_service"] = {"metadata": case["service"]["metadata"],
                                  "status": {"traffic": [{"revisionName": PRIVATE + "-other", "percent": 100}]}}
    else:
        case["read_failure"] = True
    assert diagnostic.main(["--serving-source-metadata-only"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["selected_source_sha"] is None
    assert out["deployment_provenance_status"] != "verified"
    assert PRIVATE not in json.dumps(out)


@pytest.mark.parametrize("name,value", [("GITHUB_REF", "refs/heads/private"), ("GITHUB_REPOSITORY", "private/repo"),
                                       ("GH_TOKEN", "")])
def test_source_metadata_only_rejects_invalid_context_before_cloud_reads(monkeypatch, capsys, name, value):
    case = _source_metadata_case(monkeypatch)
    monkeypatch.setenv(name, value)
    assert diagnostic.main(["--serving-source-metadata-only"]) == 1
    assert case["calls"] == []
    out = capsys.readouterr().out
    assert json.loads(out)["reason"] == "source_metadata_context_invalid"
    assert PRIVATE not in out


def _registry_case(monkeypatch):
    case = _source_metadata_case(monkeypatch)
    # Official installed SDK's ArtifactsDescribeView / summary classes:
    # provenance_summary.provenance and build_details_summary.build_details.
    case["registry"] = {
        "provenance_summary": {"provenance": [{"resourceUri": PRIVATE, "private_payload": PRIVATE}]},
        "build_details_summary": {"build_details": [{"private_payload": PRIVATE}]},
        PRIVATE: PRIVATE,
    }
    original = diagnostic.subprocess.run
    def fake(args, **kwargs):
        args = tuple(args)
        if args[:5] == ("gcloud", "artifacts", "docker", "images", "describe"):
            case["calls"].append(args)
            if case.get("registry_exception"):
                raise OSError(PRIVATE)
            return subprocess.CompletedProcess(args, case.get("registry_exit", 0),
                case.get("registry_raw", json.dumps(case["registry"])), PRIVATE)
        assert args[0] != "gh", "registry mode must not revisit deployment logs"
        return original(args, **kwargs)
    monkeypatch.setattr(diagnostic.subprocess, "run", fake)
    return case


def test_registry_metadata_consumes_exact_digest_and_only_projects_shape(monkeypatch, capsys):
    case = _registry_case(monkeypatch)
    monkeypatch.delenv("GH_TOKEN")
    assert diagnostic.main(["--registry-source-metadata-only"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "registry_metadata_only"
    assert out["reason"] == "registry_provenance_observed"
    assert out["registry_metadata"] == dict.fromkeys(("provenance_present", "provenance_shape_valid",
        "build_details_present", "build_details_shape_valid"), True)
    assert out["selected_source_sha"] is None
    assert out["deployment_provenance_status"] == out["loaded_source_status"] == out["independent_release_approval"] == "unknown"
    assert out["serving_stable"] is True
    assert case["calls"][2] == ("gcloud", "artifacts", "docker", "images", "describe",
        case["revision"]["spec"]["containers"][0]["image"], "--project", case["expected"]["expected_project_id"],
        "--show-provenance", "--show-build-details", "--format=json")
    assert len(case["calls"]) == 4
    assert all(marker not in json.dumps(out) for marker in (PRIVATE, "private-image", "f" * 64, "b" * 40))


@pytest.mark.parametrize("failure", ["absent", "empty", "null", "wrong_wrapper", "wrong_list", "invalid_record",
    "invalid_build", "invalid_top", "invalid_json", "nonzero", "exception", "split", "digest", "scope", "retarget"])
def test_registry_metadata_fail_closed_without_private_values(monkeypatch, capsys, failure):
    case = _registry_case(monkeypatch)
    if failure == "absent": case["registry"].pop("provenance_summary")
    elif failure == "empty": case["registry"]["provenance_summary"]["provenance"] = []
    elif failure == "null": case["registry"]["provenance_summary"] = None
    elif failure == "wrong_wrapper": case["registry"]["provenance_summary"] = PRIVATE
    elif failure == "wrong_list": case["registry"]["provenance_summary"]["provenance"] = PRIVATE
    elif failure == "invalid_record": case["registry"]["provenance_summary"]["provenance"] = [PRIVATE]
    elif failure == "invalid_build": case["registry"]["build_details_summary"] = {"build_details": PRIVATE}
    elif failure == "invalid_top": case["registry"] = [PRIVATE]
    elif failure == "invalid_json": case["registry_raw"] = PRIVATE
    elif failure == "nonzero": case["registry_exit"] = 1
    elif failure == "exception": case["registry_exception"] = True
    elif failure == "split": case["service"]["status"]["traffic"][0]["percent"] = 99
    elif failure == "digest": case["revision"]["spec"]["containers"][0]["image"] = "us-central1-docker.pkg.dev/private-image/repo:latest"
    elif failure == "scope": case["revision"]["spec"]["containers"][0]["env"] = []
    elif failure == "retarget": case["after_service"] = {"metadata": case["service"]["metadata"], "status": {"traffic": [{"revisionName": PRIVATE + "-other", "percent": 100}]}}
    assert diagnostic.main(["--registry-source-metadata-only"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["selected_source_sha"] is None and out["deployment_provenance_status"] == "unknown"
    assert PRIVATE not in json.dumps(out)
    if failure in {"split", "digest", "scope"}:
        assert not any(c[:2] == ("gcloud", "artifacts") for c in case["calls"])


@pytest.mark.parametrize("name,value", [("GITHUB_REF", "refs/heads/private"), ("GITHUB_REPOSITORY", "private/repo"),
    ("IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON", "[]")])
def test_registry_metadata_context_and_protected_preflight_before_reads(monkeypatch, capsys, name, value):
    case = _registry_case(monkeypatch)
    monkeypatch.setenv(name, value)
    assert diagnostic.main(["--registry-source-metadata-only"]) == 1
    assert case["calls"] == []
    assert PRIVATE not in capsys.readouterr().out


def test_registry_metadata_missing_optional_build_details_does_not_claim_source(monkeypatch, capsys):
    case = _registry_case(monkeypatch)
    case["registry"].pop("build_details_summary")
    assert diagnostic.main(["--registry-source-metadata-only"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["registry_metadata"]["build_details_present"] is False
    assert out["registry_metadata"]["build_details_shape_valid"] is True
    assert out["selected_source_sha"] is None


def test_registry_metadata_modes_are_mutually_exclusive(monkeypatch, capsys):
    case = _registry_case(monkeypatch)
    assert diagnostic.main(["--serving-source-metadata-only", "--registry-source-metadata-only"]) == 1
    assert case["calls"] == []
    assert json.loads(capsys.readouterr().out)["reason"] == "arguments_invalid"


def test_registry_metadata_workflow_only_explicit_main_mode_no_token():
    workflow = Path(".github/workflows/account-facts-identity-diagnose.yml").read_text()
    assert "registry_source_metadata_only:\n" in workflow
    assert "if: ${{ github.ref == 'refs/heads/main' && !(inputs.serving_source_metadata_only && inputs.registry_source_metadata_only) && !(inputs.static_source_only && (inputs.serving_source_metadata_only || inputs.registry_source_metadata_only)) && !(inputs.backup_primary_source_state && (inputs.serving_source_metadata_only || inputs.registry_source_metadata_only || inputs.static_source_only)) }}" in workflow
    step = workflow.split("      - name: Read registry provenance shape without reports or image execution\n", 1)[1].split("\n      - name: Compare fixed public source files in the exact serving image\n", 1)[0]
    assert step == ("        if: ${{ inputs.registry_source_metadata_only && !inputs.serving_source_metadata_only && !inputs.static_source_only }}\n"
        "        run: python3 -m scripts.diagnose_account_facts_identity --registry-source-metadata-only\n")
    assert "default: true" not in workflow and "schedule:" not in workflow


# Primary restoration backups exercise only synthetic tuples and mocked storage.


def _backup_case(monkeypatch):
    expected, report, metadata = _primary_binding_preview_context(monkeypatch)
    result = _diagnose(report, expected, cloud_run_metadata=metadata)
    preview = diagnostic._primary_source_binding_preview(report,
        expected=expected, metadata=metadata, result=result)
    hashes = {key: preview[key] for key in ("target_id_sha256",
        "configured_source_binding_id_sha256", "candidate_source_binding_id_sha256")}
    monkeypatch.setenv("IBKR_PRIMARY_BACKUP_HASHES", json.dumps(hashes))
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    return expected, report, metadata, hashes


def test_primary_backup_contains_recoverable_tuple_without_financial_report(monkeypatch):
    expected, report, metadata, hashes = _backup_case(monkeypatch)
    destination, body = backup.prepare_backup(expected, hashes, now=NOW)
    saved = json.loads(body)
    assert saved["old_expected"] == json.loads(json.dumps(expected))
    assert saved["candidate"]["runtime_revision"] == report["diagnostics"]["runtime_revision"]
    assert backup.sha(saved["candidate"]["source_binding_id"]) == hashes["candidate_source_binding_id_sha256"]
    assert saved["comparison_sha256"] == backup.sha(backup.canonical(hashes))
    assert destination.startswith(expected["expected_report_prefix"] + "/_primary_source_backups/")
    assert destination.endswith(backup.sha(body) + ".json")
    assert b"12345.6700" not in body and b"100.00" not in body
    assert "summary" not in saved and "positions" not in saved
    assert diagnostic.diagnose_identity(report, expected=expected,
        source_report_uri=saved["source_report_uri"], now=NOW,
        cloud_run_metadata=metadata)["projection_passed"] is False


@pytest.mark.parametrize("field,value", [
    ("IBKR_ACCOUNT_FACTS_TARGET", "additional-1"), ("GITHUB_REF", "refs/heads/other"),
    ("GITHUB_EVENT_NAME", "schedule"), ("IBKR_PRIMARY_BACKUP_HASHES", "{}"),
    ("IBKR_PRIMARY_BACKUP_HASHES", "invalid-private-marker"),
    ("IBKR_ACCOUNT_FACTS_RUNTIME_REVISION", "changed-revision"),
])
def test_primary_backup_preflight_stops_invalid_scope_before_cloud(monkeypatch, capsys, field, value):
    _backup_case(monkeypatch)
    monkeypatch.setenv(field, value)
    assert backup.main(["--preflight"]) == 1
    output = capsys.readouterr().out
    assert PRIVATE not in output and "invalid-private-marker" not in output
    assert json.loads(output)["status"] == "blocked"


def test_primary_backup_preflight_has_no_cloud_calls(monkeypatch, capsys):
    _backup_case(monkeypatch)
    assert backup.main(["--preflight"]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "preflight_verified"}


@pytest.mark.parametrize("failure", ["stale", "split", "identity", "candidate_changed"])
def test_primary_backup_rejects_unverified_candidate_without_storage(monkeypatch, capsys, failure):
    _expected, report, metadata, hashes = _backup_case(monkeypatch)
    if failure == "stale":
        report["started_at"] = report["finished_at"] = (NOW - timedelta(days=9)).isoformat()
        report["summary"]["account_facts"]["observed_at"] = report["finished_at"]
    elif failure == "split":
        metadata["status"]["traffic"] = [{"revisionName": report["diagnostics"]["runtime_revision"], "percent": 90},
            {"revisionName": PRIVATE + "-other", "percent": 10}]
    elif failure == "identity":
        report["summary"]["account_facts"]["account_ids"] = ["U99999999"]
    else:
        hashes["candidate_source_binding_id_sha256"] = "f" * 64
        monkeypatch.setenv("IBKR_PRIMARY_BACKUP_HASHES", json.dumps(hashes))
    assert backup.main([], now=NOW) == 1
    output = capsys.readouterr().out
    assert PRIVATE not in output
    assert json.loads(output)["reason"] == "backup_comparison_changed"


@pytest.mark.parametrize("upload_outcome", ["success", "nonzero", "timeout"])
def test_primary_backup_one_create_only_write_then_exact_readback(monkeypatch, capsys, upload_outcome):
    expected, _report, _metadata, _hashes = _backup_case(monkeypatch)
    calls = []
    bodies = []
    def command(args, **kwargs):
        calls.append(args)
        assert "publish" not in " ".join(args)
        if args[2] == "cp":
            assert "--if-generation-match=0" in args and args[3] == "-"
            assert kwargs["timeout"] == 60
            bodies.append(kwargs["input"])
            if upload_outcome == "timeout":
                raise subprocess.TimeoutExpired(args, 60, output=PRIVATE)
            return subprocess.CompletedProcess(args, 0 if upload_outcome == "success" else 1, b"", PRIVATE.encode())
        assert args[2] == "cat"
        return subprocess.CompletedProcess(args, 0, bodies[0], b"")
    monkeypatch.setattr(backup.subprocess, "run", command)
    assert backup.main([], now=NOW) == 0
    assert len(calls) == 2 and calls[0][4] == calls[1][3]
    assert json.loads(bodies[0])["old_expected"] == json.loads(json.dumps(expected))
    output = capsys.readouterr().out
    assert PRIVATE not in output and "gs://" not in output and "12345.6700" not in output
    result = json.loads(output)
    assert result["status"] == "backup_verified" and result["publication_authority_granted"] is False


@pytest.mark.parametrize("readback", ["wrong", "nonzero", "timeout"])
def test_primary_backup_uncertain_readback_is_unknown_without_retry(monkeypatch, capsys, readback):
    _backup_case(monkeypatch)
    calls = []
    def command(args, **_kwargs):
        calls.append(args)
        if args[2] == "cat" and readback == "timeout":
            raise subprocess.TimeoutExpired(args, 60, output=PRIVATE)
        return subprocess.CompletedProcess(args, 1 if readback == "nonzero" else 0, PRIVATE.encode(), b"")
    monkeypatch.setattr(backup.subprocess, "run", command)
    assert backup.main([], now=NOW) == 1 and len(calls) == 2
    output = capsys.readouterr().out
    assert PRIVATE not in output
    result = json.loads(output)
    assert result["status"] == "unknown" and result["reason"] == "backup_readback_unknown"
    assert result["backup_reference_sha256"] and result["backup_object_sha256"]
    assert result["readback"]["failure_category"] == ("timeout" if readback == "timeout" else "content_mismatch" if readback == "wrong" else "command_failed")


def test_primary_backup_workflow_is_explicit_primary_only_and_preflights_before_auth():
    workflow = Path(".github/workflows/account-facts-identity-diagnose.yml").read_text()
    assert "inputs.backup_primary_source_state && fromJSON('[\"primary-live\"]')" in workflow
    assert "!(inputs.backup_primary_source_state && (inputs.serving_source_metadata_only" in workflow
    preflight = workflow.index("python3 -m scripts.backup_primary_account_facts_state --preflight")
    assert preflight < workflow.index("uses: google-github-actions/auth@v3")
    assert "if: ${{ !inputs.backup_primary_source_state && !inputs.serving_source_metadata_only" in workflow
    assert "schedule:" not in workflow and "actions: write" not in workflow


@pytest.mark.parametrize("case", ["valid", "absent", "other_head", "other_comparison", "tampered",
    "permission", "read_permission", "command", "timeout", "mismatched_name", "outside_prefix"])
def test_primary_backup_inspection_only_pins_original_run_without_writes(monkeypatch, capsys, case):
    expected, _report, _metadata, hashes = _backup_case(monkeypatch)
    uri, body = backup.prepare_backup(expected, hashes, now=NOW)
    monkeypatch.setenv("IBKR_PRIMARY_BACKUP_ORIGINAL_HEAD_SHA", SHA)
    if case in {"other_head", "other_comparison", "tampered"}:
        saved = json.loads(body)
        if case == "other_head": saved["source_sha"] = "b" * 40
        elif case == "other_comparison": saved["comparison_sha256"] = "f" * 64
        else: saved["old_expected"]["expected_runtime_revision"] = PRIVATE + "-wrong"
        body = backup.canonical(saved)
        uri = expected["expected_report_prefix"] + "/_primary_source_backups/" + backup.sha(body) + ".json"
    if case == "mismatched_name": uri = uri.rsplit("/", 1)[0] + "/" + "0" * 64 + ".json"
    if case == "outside_prefix": uri = "gs://" + PRIVATE + "/outside/" + "0" * 64 + ".json"
    calls = []
    def command(args, **_kwargs):
        calls.append(args)
        assert args[:2] == ("gcloud", "storage") and args[2] in {"ls", "cat"}
        assert "cp" not in args
        if case == "timeout": raise subprocess.TimeoutExpired(PRIVATE, 60, output=PRIVATE)
        if args[2] == "ls":
            if case == "absent": return subprocess.CompletedProcess(args, 1, b"", b"matched no objects: " + PRIVATE.encode())
            if case == "permission": return subprocess.CompletedProcess(args, 1, b"", b"403 permission denied: " + PRIVATE.encode())
            if case == "command": return subprocess.CompletedProcess(args, 2, b"", b"usage: unrecognized arguments " + PRIVATE.encode())
            return subprocess.CompletedProcess(args, 0, (uri + "\n").encode(), b"")
        if case == "read_permission": return subprocess.CompletedProcess(args, 1, b"", b"403 " + PRIVATE.encode())
        return subprocess.CompletedProcess(args, 0, body, b"")
    monkeypatch.setattr(backup.subprocess, "run", command)
    code = backup.main(["--inspect-only"], now=NOW)
    output = capsys.readouterr().out
    assert PRIVATE not in output and "gs://" not in output and "U00000001" not in output
    result = json.loads(output)
    assert result["inspection_only"] is True and result["original_head_sha"] == SHA
    assert result["comparison_sha256"] == backup.sha(backup.canonical(hashes))
    if case == "valid":
        assert code == 0 and result["status"] == "backup_verified" and result["matched_backups"] == 1
        assert result["backup_object_sha256"] == backup.sha(body)
    elif case in {"absent", "other_head", "other_comparison"}:
        assert code == 0 and result["status"] == "backup_absent" and result["matched_backups"] == 0
    else:
        assert code == 1 and result["status"] == "blocked"
        if case in {"permission", "read_permission"}: assert result["failure_category"] == "permission_denied"
        if case == "command": assert result["exit_code"] == 2 and result["failure_category"] == "invalid_argument"
        if case == "timeout": assert result["failure_category"] == "timeout"
    assert len(calls) <= 2


def test_primary_backup_inspection_missing_original_head_stops_before_io(monkeypatch, capsys):
    _backup_case(monkeypatch)
    monkeypatch.delenv("IBKR_PRIMARY_BACKUP_ORIGINAL_HEAD_SHA", raising=False)
    assert backup.main(["--inspect-only"]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "backup_original_head_invalid"


@pytest.mark.parametrize("failure", [None, "reference", "object", "malformed"])
def test_primary_backup_destination_proof_stays_private_and_read_only(monkeypatch, capsys, failure):
    expected, _report, _metadata, hashes = _backup_case(monkeypatch)
    uri, body = backup.prepare_backup(expected, hashes, now=NOW)
    reference = {"backup_object_sha256": backup.sha(body), "backup_reference_sha256": backup.sha(uri)}
    if failure == "reference": reference["backup_reference_sha256"] = "0" * 64
    if failure == "object": reference["backup_object_sha256"] = "1" * 64
    monkeypatch.setenv("IBKR_PRIMARY_BACKUP_REFERENCE_HASHES", "malformed" if failure == "malformed" else json.dumps(reference))
    monkeypatch.setenv("IBKR_PRIMARY_BACKUP_ORIGINAL_HEAD_SHA", SHA)
    calls = []
    def command(args, **_kwargs):
        calls.append(args)
        assert args[2] == "ls"
        return subprocess.CompletedProcess(args, 1, b"", b"matched no objects")
    monkeypatch.setattr(backup.subprocess, "run", command)
    code = backup.main(["--inspect-only"])
    output = capsys.readouterr().out
    assert PRIVATE not in output and "gs://" not in output and "U00000001" not in output
    result = json.loads(output)
    if failure:
        assert code == 1 and result["reason"] == "backup_reference_mismatch" and calls == []
    else:
        assert code == 0 and result["status"] == "backup_absent" and len(calls) == 1
        proof = result["destination_evidence"]
        assert proof["reference_matches"] is True
        assert proof["backup_object_sha256"] == backup.sha(body)
        assert proof["bucket_sha256"] == backup.sha(PRIVATE)
        assert proof["backup_prefix_sha256"] == backup.sha(expected["expected_report_prefix"] + "/_primary_source_backups/")
        resource_prefix = "projects/_/buckets/" + PRIVATE + "/objects/reports/"
        assert backup.sha(resource_prefix) in proof["resource_boundary_prefix_sha256"]
        assert backup.sha("projects/_/buckets/another/objects/reports/") not in proof["resource_boundary_prefix_sha256"]
        assert result["comparison_sha256"] == backup.sha(backup.canonical(hashes))


def test_primary_backup_destination_proof_cannot_enable_upload(monkeypatch, capsys):
    _backup_case(monkeypatch)
    monkeypatch.setenv("IBKR_PRIMARY_BACKUP_REFERENCE_HASHES", "nonempty")
    assert backup.main([]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "backup_reference_inspection_required"


def _recovery_case(monkeypatch, tmp_path):
    import base64
    expected, report, metadata, hashes = _backup_case(monkeypatch)
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    uri, body = backup.prepare_backup(expected, hashes, now=NOW)
    reference = {"backup_object_sha256": backup.sha(body), "backup_reference_sha256": backup.sha(uri)}
    monkeypatch.setenv("IBKR_PRIMARY_BACKUP_REFERENCE_HASHES", json.dumps(reference))
    monkeypatch.setenv("IBKR_PRIMARY_BACKUP_ORIGINAL_HEAD_SHA", "a" * 40)
    monkeypatch.setenv("IBKR_PRIMARY_BACKUP_ENABLED", "true")
    monkeypatch.setenv("IBKR_PRIMARY_BACKUP_INSPECT_ONLY", "false")
    key = base64.b64encode(b"k" * 32).decode()
    monkeypatch.setattr(backup, "RECOVERY_PUBLIC_KEY_HASHES", {"ibkr": backup.sha(b"k" * 32), "qrs": backup.sha(b"k" * 32)})
    monkeypatch.setenv("IBKR_PRIMARY_RECOVERY_PUBLIC_KEYS", json.dumps({
        "ibkr": {"key_id": "123", "key": key}, "qrs": {"key_id": "456", "key": key}}))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    calls = []
    def storage(args, **kwargs):
        calls.append(args)
        assert args[:3] in (("gcloud", "storage", "ls"), ("gcloud", "storage", "cat"))
        value = (uri + "\n").encode() if args[2] == "ls" else body
        return SimpleNamespace(returncode=0, stdout=value, stderr=b"")
    monkeypatch.setattr(backup.subprocess, "run", storage)
    return expected, report, metadata, hashes, calls


@pytest.mark.parametrize("field,value", [
    ("IBKR_PRIMARY_BACKUP_ENABLED", "false"),
    ("IBKR_PRIMARY_BACKUP_INSPECT_ONLY", "true"),
    ("IBKR_PRIMARY_RECOVERY_PUBLIC_KEYS", "{}"),
    ("IBKR_PRIMARY_BACKUP_REFERENCE_HASHES", "{}"),
    ("IBKR_PRIMARY_BACKUP_ORIGINAL_HEAD_SHA", ""),
])
def test_recovery_preflight_rejects_invalid_mode_without_cloud(monkeypatch, tmp_path, capsys, field, value):
    *_, calls = _recovery_case(monkeypatch, tmp_path)
    monkeypatch.setenv(field, value)
    assert backup.main(["--recovery-preflight"], now=NOW) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "recovery_inputs_invalid"
    assert calls == []


def test_recovery_seals_only_exact_two_configuration_values(monkeypatch, tmp_path, capsys):
    expected, report, _metadata, hashes, calls = _recovery_case(monkeypatch, tmp_path)
    values = []
    def seal(key, value):
        values.append(value)
        return b"synthetic-ciphertext-" + backup.sha(value).encode()
    monkeypatch.setattr(backup, "_seal_secret", seal)
    assert backup.main(["--prepare-recovery-secrets"], now=NOW) == 0
    output = capsys.readouterr().out
    assert PRIVATE not in output and "synthetic-ciphertext" not in output
    result = json.loads(output)
    assert result["status"] == "recovery_secrets_sealed"
    assert result["secret_count"] == 2 and result["remote_configuration_writes"] is False
    artifact = (tmp_path / "primary-recovery-secrets.json").read_bytes()
    payload = json.loads(artifact)
    assert PRIVATE.encode() not in artifact and b"12345.6700" not in artifact
    assert payload["comparison_sha256"] == backup.sha(backup.canonical(hashes))
    assert payload["secrets"][0]["name"] == "IBKR_ACCOUNT_FACTS_RUNTIME_REVISION"
    assert payload["secrets"][0]["repository"] == "QuantStrategyLab/InteractiveBrokersPlatform"
    assert payload["secrets"][1]["environment"] == "runtime-strategy-switch"
    assert payload["secrets"][1]["name"] == "IBKR_ACCOUNT_FACTS_SOURCE_ROTATION_JSON"
    assert values[0].decode() == report["diagnostics"]["runtime_revision"]
    rotation = json.loads(values[1])
    assert set(rotation) == {"target_id", "previous_source_binding_id", "next_source_binding_id"}
    assert rotation["target_id"] == expected["target_id"]
    assert backup.sha(rotation["previous_source_binding_id"]) == hashes["configured_source_binding_id_sha256"]
    assert backup.sha(rotation["next_source_binding_id"]) == hashes["candidate_source_binding_id_sha256"]
    assert all(args[2] != "cp" for args in calls)
    assert result["sealed_artifact_sha256"] == backup.sha(artifact)


def test_recovery_rejects_an_unapproved_encryption_recipient(monkeypatch, tmp_path, capsys):
    *_, calls = _recovery_case(monkeypatch, tmp_path)
    monkeypatch.setattr(backup, "RECOVERY_PUBLIC_KEY_HASHES", {"ibkr": "0" * 64, "qrs": "0" * 64})
    assert backup.main(["--recovery-preflight"], now=NOW) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "recovery_inputs_invalid"
    assert calls == [] and not (tmp_path / "primary-recovery-secrets.json").exists()


@pytest.mark.parametrize("failure", [None, "restore_scope", "restore_prefix", "backup_hash", "original_head", "stale", "split"])
def test_recovery_reuses_exact_current_tuple_in_existing_backup(monkeypatch, tmp_path, capsys, failure):
    expected, report, metadata, old_hashes, calls = _recovery_case(monkeypatch, tmp_path)
    current_revision = report["diagnostics"]["runtime_revision"]
    expected["expected_runtime_revision"] = current_revision
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_RUNTIME_REVISION", current_revision)
    next_revision = current_revision + "-native-cache"
    report["diagnostics"]["runtime_revision"] = next_revision
    report["runtime_release_receipt"]["runtime_revision"] = next_revision
    metadata["status"].update({"latestReadyRevisionName": next_revision,
        "latestCreatedRevisionName": next_revision,
        "traffic": [{"revisionName": next_revision, "percent": 100}]})
    result = _diagnose(report, expected, cloud_run_metadata=metadata)
    preview = diagnostic._primary_source_binding_preview(report,
        expected=expected, metadata=metadata, result=result)
    hashes = {key: preview[key] for key in old_hashes}
    monkeypatch.setenv("IBKR_PRIMARY_BACKUP_HASHES", json.dumps(hashes))
    if failure in {"restore_scope", "restore_prefix", "backup_hash"}:
        original_storage = backup.subprocess.run
        body = original_storage(("gcloud", "storage", "cat")).stdout
        saved = json.loads(body)
        if failure == "restore_scope":
            saved["old_expected"]["expected_account_selector"] = [PRIVATE + "-other"]
        elif failure == "restore_prefix":
            saved["old_expected"]["expected_report_prefix"] += "/other"
        body = backup.canonical(saved)
        uri = expected["expected_report_prefix"] + "/_primary_source_backups/" + backup.sha(body) + ".json"
        monkeypatch.setenv("IBKR_PRIMARY_BACKUP_REFERENCE_HASHES", json.dumps({
            "backup_object_sha256": backup.sha(body), "backup_reference_sha256": backup.sha(uri)}))
        if failure == "backup_hash":
            body += b" "
        def changed_storage(args, **kwargs):
            calls.append(args)
            assert args[:3] in (("gcloud", "storage", "ls"), ("gcloud", "storage", "cat"))
            return SimpleNamespace(returncode=0, stdout=(uri + "\n").encode() if args[2] == "ls" else body, stderr=b"")
        monkeypatch.setattr(backup.subprocess, "run", changed_storage)
    elif failure == "original_head":
        monkeypatch.setenv("IBKR_PRIMARY_BACKUP_ORIGINAL_HEAD_SHA", "b" * 40)
    elif failure == "stale":
        report["started_at"] = report["finished_at"] = (NOW - timedelta(days=9)).isoformat()
        report["summary"]["account_facts"]["observed_at"] = report["finished_at"]
    elif failure == "split":
        metadata["status"]["traffic"] = [{"revisionName": next_revision, "percent": 90},
            {"revisionName": PRIVATE + "-other", "percent": 10}]
    sealed = []
    monkeypatch.setattr(backup, "_seal_secret", lambda _key, value: sealed.append(value) or b"synthetic-ciphertext")
    code = backup.main(["--prepare-recovery-secrets"], now=NOW)
    output = capsys.readouterr().out
    assert PRIVATE not in output
    if failure:
        assert code == 1 and json.loads(output)["status"] == "blocked"
        assert sealed == [] and not (tmp_path / "primary-recovery-secrets.json").exists()
        assert all(args[:3] in (("gcloud", "storage", "ls"), ("gcloud", "storage", "cat")) for args in calls)
        return
    assert code == 0
    assert json.loads(output)["expected_runtime_revision_sha256"] == backup.sha(next_revision)
    assert sealed[0].decode() == next_revision
    rotation = json.loads(sealed[1])
    assert backup.sha(rotation["previous_source_binding_id"]) == hashes["configured_source_binding_id_sha256"]
    assert backup.sha(rotation["next_source_binding_id"]) == hashes["candidate_source_binding_id_sha256"]
    assert hashes["configured_source_binding_id_sha256"] == old_hashes["candidate_source_binding_id_sha256"]
    assert all(args[:3] in (("gcloud", "storage", "ls"), ("gcloud", "storage", "cat")) for args in calls)


@pytest.mark.parametrize("failure", ["reference", "stale", "split", "candidate_changed"])
def test_recovery_sealing_preserves_exact_backup_and_live_guards(monkeypatch, tmp_path, capsys, failure):
    _expected, report, metadata, _hashes, _calls = _recovery_case(monkeypatch, tmp_path)
    if failure == "reference":
        ref = json.loads(os.environ["IBKR_PRIMARY_BACKUP_REFERENCE_HASHES"])
        ref["backup_object_sha256"] = "f" * 64
        monkeypatch.setenv("IBKR_PRIMARY_BACKUP_REFERENCE_HASHES", json.dumps(ref))
    elif failure == "stale":
        report["started_at"] = report["finished_at"] = (NOW - timedelta(days=9)).isoformat()
        report["summary"]["account_facts"]["observed_at"] = report["finished_at"]
    elif failure == "split":
        metadata["status"]["traffic"] = [{"revisionName": report["diagnostics"]["runtime_revision"], "percent": 90},
            {"revisionName": PRIVATE + "-other", "percent": 10}]
    else:
        monkeypatch.setattr(backup, "prepare_backup", lambda *_args, **_kwargs: ("unused", backup.canonical({"candidate": {}})))
    monkeypatch.setattr(backup, "_seal_secret", lambda *_: pytest.fail("guard must stop before sealing"))
    assert backup.main(["--prepare-recovery-secrets"], now=NOW) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "blocked"
    assert result["reason"] == {"reference": "backup_reference_mismatch", "stale": "backup_comparison_changed",
        "split": "backup_comparison_changed", "candidate_changed": "recovery_candidate_changed"}[failure]
    assert not (tmp_path / "primary-recovery-secrets.json").exists()


@pytest.mark.parametrize("value,kind", [([], "is_list"), ({}, "is_object"), ("private-value", "is_string"),
    (None, "is_null"), (12345, None)])
def test_positions_source_evidence_has_only_fixed_paths_and_boolean_types(value, kind):
    report = {"summary": {"account_facts": {"broker_reported_positions": value},
        "broker_snapshot": {"positions": value}}, PRIVATE: PRIVATE}
    evidence = diagnostic._positions_source_evidence(report)
    for path in ("summary.account_facts.broker_reported_positions", "summary.broker_snapshot.positions"):
        assert evidence[path]["present"] is True
        for key in ("is_list", "is_object", "is_string", "is_null"):
            assert evidence[path][key] is (key == kind)
    assert evidence["summary.account_facts.broker_reported_positions_scope"] == dict.fromkeys(
        ("present", "is_list", "is_object", "is_string", "is_null"), False)
    assert all(type(flag) is bool for fields in evidence.values() for flag in fields.values())
    serialized = json.dumps(evidence)
    assert PRIVATE not in serialized and "private-value" not in serialized and "12345" not in serialized


def test_positions_source_evidence_does_not_search_weights_or_order_intents():
    report = {"summary": {"target_weights": {"positions": [PRIVATE]}},
        "orders": [{"broker_snapshot": {"positions": [PRIVATE]}}],
        "target_positions": {"positions": [PRIVATE]}}
    evidence = diagnostic._positions_source_evidence(report)
    assert not any(flags["present"] for flags in evidence.values())
    assert PRIVATE not in json.dumps(evidence)


@pytest.mark.parametrize("failure", [None, "stale", "source", "account", "revision", "metadata"])
def test_positions_source_evidence_reuses_original_cli_acceptance_without_extra_reads(monkeypatch, capsys, failure):
    _environment(monkeypatch)
    expected = diagnostic.resolve_expected()
    report, metadata = _report(expected), _metadata(expected)
    if failure == "stale":
        report["started_at"] = report["finished_at"] = (NOW - timedelta(days=9)).isoformat()
        report["summary"]["account_facts"]["observed_at"] = report["finished_at"]
    elif failure == "source":
        report["service_name"] = PRIVATE + "-other"
    elif failure == "account":
        report["summary"]["account_facts"]["account_ids"] = ["U00000002"]
    elif failure == "revision":
        report["diagnostics"]["runtime_revision"] = PRIVATE + "-other"
    elif failure == "metadata":
        metadata = None
    calls = []
    monkeypatch.setattr(publisher, "_latest_report_uri", lambda **_kw: expected["expected_report_prefix"] + "/r.json")
    def read_report(*_args, **_kwargs):
        calls.append("report")
        return report
    def read_metadata(*_args, **_kwargs):
        calls.append("metadata")
        return metadata
    monkeypatch.setattr(publisher, "_load_gcs_report", read_report)
    monkeypatch.setattr(diagnostic, "_read_cloud_run_metadata", read_metadata)
    assert diagnostic.main([], now=NOW) == (0 if failure is None else 1)
    result = json.loads(capsys.readouterr().out)
    assert ("positions_source_evidence" in result) is (failure is None)
    assert calls == ["report", "metadata"]
    assert PRIVATE not in json.dumps(result) and "12345.6700" not in json.dumps(result)
    assert result["account_facts_stages"].keys() == dict.fromkeys(_STAGES).keys()
