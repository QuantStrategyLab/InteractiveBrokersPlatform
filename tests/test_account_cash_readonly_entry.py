"""Synthetic target binding and existing maintenance reader integration."""
import json
import logging
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from scripts import account_cash_readonly_entry as entry
from tests.test_maintenance_cash_job_entry import ENV, SummaryBroker


def setup_case(profile):
    account = "DU00000001"
    target = SimpleNamespace(platform_id="ibkr", strategy_profile=profile,
        service_name="private-service", account_scope="private-scope",
        deployment_selector="private-deployment", account_selector=(account,), execution_mode="live")
    calls, report = [], {"diagnostics": {}}
    broker = SummaryBroker([dict(account=account, tag="CashBalance", currency="USD", value="12.3400")])
    broker.disconnect = lambda: calls.append("disconnect")

    def connect(**kwargs):
        calls.append(("connect", kwargs))
        return broker

    def finalize(value, **kwargs):
        if "diagnostics" in kwargs:
            value["diagnostics"].update(kwargs.pop("diagnostics"))
        value.update(kwargs)

    runtime = SimpleNamespace(
        RUNTIME_SETTINGS=SimpleNamespace(runtime_target=target, runtime_target_enabled=False),
        STRATEGY_PROFILE=profile, ACCOUNT_IDS=(account,), IB_CLIENT_ID=7,
        build_composer=lambda **_: SimpleNamespace(build_reporting_adapters=lambda: SimpleNamespace(
            build_log_context=lambda: object())),
        build_execution_report=lambda *_, **__: report,
        connect_ib=connect,
        build_portfolio_snapshot=lambda _: object(),
        _account_facts_match_runtime_target=lambda facts: facts["account_ids"] == [account],
        finalize_runtime_report=finalize,
        persist_execution_report=lambda value, **_: calls.append(("save", value.copy())) or "gs://synthetic/private/maintenance-cash/report.json")
    env = {**ENV, "CASH_READONLY_PROFILE": profile,
           "CASH_READONLY_TARGET_SHA256": entry.target_binding_sha256(target),
           "EXECUTION_REPORT_GCS_URI": "gs://synthetic/private",
           "CASH_READONLY_REPORT_PREFIX_SHA256": entry.report_prefix_sha256("gs://synthetic/private")}
    return runtime, env, broker, calls, report


@pytest.mark.parametrize("profile", ["global_etf_rotation", "russell_top50_leader_rotation"])
def test_existing_summary_path_for_each_exact_target(profile, capsys):
    runtime, env, broker, calls, report = setup_case(profile)
    try:
        result = entry.run(runtime, lambda _: {"account_ids": ["DU00000001"]}, env)
        assert result == 0
        public = json.loads(capsys.readouterr().out)
        assert public["status"] == "observation_saved" and len(public["report_uri_sha256"]) == 64
        assert calls[0] == ("connect", {"read_only": True, "validate_trading_permissions": False})
        assert calls[-1][0] == "save" and "disconnect" in calls
        assert broker.calls == [("request", 17, "All", "$LEDGER:USD"), ("cancel", 17)]
        assert "broker_funding_inputs" not in report["summary"]
        assert report["summary"]["broker_account_summary"]["broker_cash_observations"]["values"][0]["value"] == "12.3400"
    finally:
        broker.loop.close()


@pytest.mark.parametrize("failure", ["other_profile", "account", "binding", "prefix", "enabled", "client", "job"])
def test_invalid_binding_never_connects(failure, capsys):
    runtime, env, broker, calls, _ = setup_case("global_etf_rotation")
    if failure == "other_profile":
        env["CASH_READONLY_PROFILE"] = "russell_top50_leader_rotation"
    elif failure == "account":
        runtime.ACCOUNT_IDS = ("DU00000002",)
    elif failure == "binding":
        env["CASH_READONLY_TARGET_SHA256"] = "0" * 64
    elif failure == "prefix":
        env["EXECUTION_REPORT_GCS_URI"] = "gs://other/private"
    elif failure == "enabled":
        runtime.RUNTIME_SETTINGS.runtime_target_enabled = True
    elif failure == "client":
        runtime.IB_CLIENT_ID = 0
    else:
        env["CLOUD_RUN_TASK_ATTEMPT"] = "1"
    try:
        assert entry.run(runtime, lambda _: {"account_ids": ["DU00000001"]}, env) == 1
        captured = capsys.readouterr()
        assert json.loads(captured.out) == {"status": "failed"}
        assert captured.err == "" and calls == [] and broker.calls == []
    finally:
        broker.loop.close()


def test_summary_failure_does_not_retry_or_expose_private_report(capsys):
    runtime, env, broker, calls, report = setup_case("global_etf_rotation")
    broker.failure = "request"
    try:
        assert entry.run(runtime, lambda _: {"account_ids": ["DU00000001"]}, env) == 1
        captured = capsys.readouterr()
        assert captured.out == '{"status": "failed"}\n' and captured.err == ""
        assert sum(isinstance(x, tuple) and x[0] == "connect" for x in calls) == 1
        assert calls[-1][0] == "save" and report["status"] == "error"
        assert broker.calls == [("request", 17, "All", "$LEDGER:USD"), ("cancel", 17)]
    finally:
        broker.loop.close()


def test_saved_report_outside_bound_namespace_is_rejected(capsys):
    runtime, env, broker, _, _ = setup_case("global_etf_rotation")
    runtime.persist_execution_report = lambda *args, **kwargs: "gs://other/private/report.json"
    try:
        assert entry.run(runtime, lambda _: {"account_ids": ["DU00000001"]}, env) == 1
        output = capsys.readouterr()
        assert json.loads(output.out) == {"status": "failed"}
        assert "gs://" not in output.out + output.err
    finally:
        broker.loop.close()


def test_existing_logging_handler_cannot_expose_runtime_details(capsys):
    runtime, env, broker, _, _ = setup_case("global_etf_rotation")
    logger = logging.getLogger("synthetic.private.runtime")
    handler = logging.StreamHandler(sys.stderr)
    logger.addHandler(handler)
    original_connect = runtime.connect_ib
    def connect(**kwargs):
        logger.error("PRIVATE_GATEWAY_CANARY")
        return original_connect(**kwargs)
    runtime.connect_ib = connect
    previous = logging.root.manager.disable
    try:
        assert entry.run(runtime, lambda _: {"account_ids": ["DU00000001"]}, env) == 0
        output = capsys.readouterr()
        assert "PRIVATE_GATEWAY_CANARY" not in output.out + output.err
        assert logging.root.manager.disable == previous
    finally:
        logger.removeHandler(handler)
        broker.loop.close()


@pytest.mark.parametrize("fail_save", [False, True])
def test_local_report_is_private_and_removed_even_on_save_failure(monkeypatch, tmp_path, fail_save):
    runtime, env, broker, _, _ = setup_case("global_etf_rotation")
    prior = str(tmp_path / "original")
    monkeypatch.setenv("EXECUTION_REPORT_OUTPUT_DIR", prior)
    paths = []

    def save(*args, **kwargs):
        directory = Path(os.environ["EXECUTION_REPORT_OUTPUT_DIR"])
        assert directory.stat().st_mode & 0o077 == 0
        local_report = directory / "synthetic-report.json"
        local_report.write_text("synthetic financial data")
        paths.append(local_report)
        if fail_save:
            raise RuntimeError("synthetic save failure")
        return "gs://synthetic/private/maintenance-cash/report.json"

    runtime.persist_execution_report = save
    try:
        assert entry.run(runtime, lambda _: {"account_ids": ["DU00000001"]}, env) == int(fail_save)
        assert len(paths) == 1 and not paths[0].exists() and not paths[0].parent.exists()
        assert os.environ["EXECUTION_REPORT_OUTPUT_DIR"] == prior
    finally:
        broker.loop.close()
