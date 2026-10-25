"""Artificial source fields and guarded, status-only protected-read wiring."""
from copy import deepcopy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from application.broker_contract_source_evidence import GAPS, inspect_broker_contract_sources
from scripts import diagnose_account_facts_identity as diagnostic
from scripts import publish_account_facts_from_report as publisher
from tests.test_diagnose_account_facts_identity import NOW, PRIVATE, _environment, _metadata, _report


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("source tests must not read cloud, publish, or connect to a broker")
    monkeypatch.setattr(publisher.subprocess, "run", forbidden)
    monkeypatch.setattr(publisher, "build_opener", forbidden)
    monkeypatch.setattr(publisher, "publish_ibkr_account_facts_history", forbidden)


def report_fixture(monkeypatch):
    _environment(monkeypatch)
    expected = diagnostic.resolve_expected()
    return expected, _report(expected), _metadata(expected)


@pytest.mark.parametrize("tag,settled", [("CashBalance", False), ("TotalCashBalance", False),
    ("$LEDGER-CashBalance", False), ("SettledCash", True)])
def test_native_selected_cash_tag_is_distinct_from_spendable_cash(monkeypatch, tag, settled):
    _, report, _ = report_fixture(monkeypatch)
    report["summary"]["account_facts"]["cash"][0]["source_tag"] = tag
    out = inspect_broker_contract_sources(report)
    assert out["usd_cash_observation"]["source_tag"] == tag
    assert out["usd_cash_observation"]["settled_tag_observed"] is settled
    assert out["usd_cash_observation"]["spendable_cash_verified"] is False
    assert out["whole_account_pending_state"] == "UNKNOWN"
    assert set(GAPS) <= set(out["gaps"])
    text = json.dumps(out)
    assert PRIVATE not in text and "12345.6700" not in text and "100.00" not in text


@pytest.mark.parametrize("cash", [None, [], [{"currency":"HKD", "source_tag":"SettledCash"}],
    [{"currency":"USD", "source_tag": [PRIVATE]}],
    [{"currency":"USD", "source_tag":"SettledCash"}]*2])
def test_missing_other_currency_or_malformed_cash_never_becomes_verified(monkeypatch, cash):
    _, report, _ = report_fixture(monkeypatch)
    report["summary"]["account_facts"]["cash"] = cash
    out = inspect_broker_contract_sources(report)
    assert out["usd_cash_observation"]["settled_tag_observed"] is False
    assert out["usd_cash_observation"]["source_tag"] is None
    assert PRIVATE not in json.dumps(out)


@pytest.mark.parametrize("reason", ["pending_orders_detected:TQQQ", "pending_orders_unavailable",
    "broker_order_outcome_unknown", "broker_order_pending_confirmation", "pending_sell_release"])
def test_negative_cycle_observation_is_retained_without_assuming_account_completeness(monkeypatch, reason):
    _, report, _ = report_fixture(monkeypatch)
    report["summary"]["no_op_reason"] = reason
    out = inspect_broker_contract_sources(report)
    assert out["cycle_pending_or_unknown_recorded"] is True
    assert out["whole_account_pending_state"] == "UNKNOWN"
    assert out["complete_cash_obligations_verified"] is False
    assert "TQQQ" not in json.dumps(out)


def test_zero_orders_success_and_unrecognized_claims_do_not_grant_financial_authority(monkeypatch):
    _, report, _ = report_fixture(monkeypatch)
    report.update(status="ok", execution_receipt={"outcome":"no_action"})
    report["summary"].update(orders_submitted_count=0, orders_pending=[],
        managed_symbols=[PRIVATE], fee_plan=PRIVATE, permissions={"TQQQ":True},
        obligations_complete=True, bounded_complete=True)
    out = inspect_broker_contract_sources(report)
    assert out["cycle_pending_or_unknown_recorded"] is False
    assert out["whole_account_pending_state"] == "UNKNOWN"
    for key in ("account_fee_plan_verified", "security_permissions_verified", "fractional_permission_verified",
            "account_quantity_rules_verified", "complete_cash_obligations_verified", "execution_authorized",
            "settings_applied", "breaker_reset"):
        assert out[key] is False
    assert PRIVATE not in json.dumps(out)
    assert out["source_fields"]["summary.managed_symbols"] == "array"


def test_api_failures_and_program_fractional_block_are_distinct_from_account_permission_grants(monkeypatch):
    _, report, _ = report_fixture(monkeypatch)
    report["diagnostics"].update(failure_category="ibkr_trading_permission",
        skip_reason="ibkr_fractional_equity_api_unsupported")
    out = inspect_broker_contract_sources(report)
    assert out["api_permission_failure_recorded"] and out["fractional_api_block_recorded"]
    assert out["security_permissions_verified"] is False
    assert out["fractional_permission_verified"] is False
    report["diagnostics"].update(failure_category=PRIVATE, skip_reason=PRIVATE)
    assert PRIVATE not in json.dumps(inspect_broker_contract_sources(report))


@pytest.mark.parametrize("failure", [None, "stale", "account", "revision", "metadata", "read"])
def test_explicit_mode_reuses_one_guarded_report_read_and_does_not_disguise_unverified_sources(monkeypatch, capsys, failure):
    expected, report, metadata = report_fixture(monkeypatch)
    if failure == "stale": report["summary"]["account_facts"]["observed_at"] = "2000-01-01T00:00:00Z"
    elif failure == "account": report["summary"]["account_facts"]["account_ids"] = ["U00000002"]
    elif failure == "revision": report["diagnostics"]["runtime_revision"] = PRIVATE + "-other"
    elif failure == "metadata": metadata = None
    calls = []
    monkeypatch.setattr(publisher, "_latest_report_uri", lambda **kwargs: expected["expected_report_prefix"]+"/r.json")
    def read(*args, **kwargs):
        calls.append("report")
        if failure == "read": raise RuntimeError(PRIVATE)
        return deepcopy(report)
    def read_metadata(*args, **kwargs):
        calls.append("metadata")
        return metadata
    monkeypatch.setattr(publisher, "_load_gcs_report", read)
    monkeypatch.setattr(diagnostic, "_read_cloud_run_metadata", read_metadata)
    assert diagnostic.main(["--broker-contract-source-evidence"], now=NOW) == (0 if failure is None else 1)
    out = json.loads(capsys.readouterr().out)
    evidence = out["broker_contract_source_evidence"]
    assert evidence["status"] == ("SOURCE_INSPECTED_CONTRACT_GAPS" if failure is None else "NOT_EVALUATED")
    assert calls == (["report"] if failure == "read" else ["report", "metadata"])
    assert PRIVATE not in json.dumps(out) and "12345.6700" not in json.dumps(out)


def test_source_projection_failure_emits_no_partial_private_values(monkeypatch, capsys):
    from application import broker_contract_source_evidence as projection
    expected, report, metadata = report_fixture(monkeypatch)
    monkeypatch.setattr(publisher, "_latest_report_uri", lambda **kwargs: expected["expected_report_prefix"]+"/r.json")
    monkeypatch.setattr(publisher, "_load_gcs_report", lambda *args, **kwargs: report)
    monkeypatch.setattr(diagnostic, "_read_cloud_run_metadata", lambda *args, **kwargs: metadata)
    def broken(*args, **kwargs): raise ValueError(PRIVATE)
    monkeypatch.setattr(projection, "inspect_broker_contract_sources", broken)
    assert diagnostic.main(["--broker-contract-source-evidence"], now=NOW) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["broker_contract_source_evidence"]["gaps"] == ["BROKER_SOURCE_PROJECTION_FAILED"]
    assert PRIVATE not in json.dumps(out)


def test_primary_workflow_keeps_default_report_read_and_explicit_candidate_mode():
    workflow = Path(".github/workflows/primary-account-facts-readonly.yml").read_text()
    assert "scripts.diagnose_account_facts_identity --broker-contract-source-evidence" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow and "workflow_dispatch:" in workflow
    assert "schedule:" not in workflow
    assert "if: inputs.maintenance_source_commit == ''" in workflow
    assert "if: always() && inputs.maintenance_source_commit != ''" in workflow
    assert "path: ${{ runner.temp }}/maintenance-cash.safe.json" in workflow
    assert workflow.count("uses: google-github-actions/auth@") == 1
    assert "IBKR_FLEX_TOKEN" not in workflow and "probe-live-permission" not in workflow
    assert workflow.index("Verify the existing primary scope") < workflow.index("Authenticate with the existing read identity")



def test_protected_runner_projection_requires_only_standard_library(monkeypatch):
    # The protected workflow intentionally installs no application dependencies.
    code = """
import socket, subprocess, urllib.request

def forbidden(*args, **kwargs):
    raise AssertionError('no external call')

socket.socket = forbidden
socket.getaddrinfo = forbidden
subprocess.run = forbidden
urllib.request.urlopen = forbidden
from application.broker_contract_source_evidence import inspect_broker_contract_sources
from scripts.diagnose_account_facts_identity import main
out = inspect_broker_contract_sources({'schema_version':'runtime_report.v1','summary':{}})
assert out['whole_account_pending_state'] == 'UNKNOWN'
assert not out['execution_authorized']
"""
    monkeypatch.undo()
    result = subprocess.run([sys.executable, '-S', '-c', code], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_additional_native_settled_cash_is_visible_but_never_grants_spending_authority(monkeypatch):
    _, report, _ = report_fixture(monkeypatch)
    facts = report["summary"]["account_facts"]
    facts["broker_cash_observations"] = {
        "schema_version": "ibkr.cash_observations.v1", "observed_at": facts["observed_at"],
        "values": [{"currency": "USD", "source_tag": "SettledCash", "value": "73.25"}],
        "invalid_fields": [],
    }
    out = inspect_broker_contract_sources(report)
    assert out["native_cash_fields"]["status"] == "OBSERVED_FIELDS_ONLY"
    assert out["native_cash_fields"]["fields"]["SettledCash"] == "OBSERVED"
    assert out["usd_cash_observation"]["settled_tag_observed"] is True
    assert out["usd_cash_observation"]["source_tag"] == "CashBalance"
    assert "USD_SETTLED_CASH_NOT_OBSERVED_IN_PROJECTED_CASH" not in out["gaps"]
    assert out["usd_cash_observation"]["spendable_cash_verified"] is False
    assert out["complete_cash_obligations_verified"] is False
    assert "73.25" not in json.dumps(out)
