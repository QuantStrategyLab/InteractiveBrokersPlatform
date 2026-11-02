"""Synthetic HTTP replay with native reports; no historical authentication."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import builtins
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from types import ModuleType, SimpleNamespace
import urllib.request

import pytest


ROOT = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).with_name("fixtures") / "request-endpoint-replay.json"
PROFILES = ("tqqq_growth_income", "soxl_soxx_trend_income")
SCENARIOS = (
    "submitted", "pending", "risk_blocked", "core_failure", "gateway_failure",
    "permission_failure", "lock_busy", "market_closed", "target_disabled",
    "dry_run_post", "dry_run_get", "method_rejected", "persistence_failure",
    "persistence_unconfirmed", "receipt_conflict",
)
LEGACY_MAIN_SHA256 = "f13b55f501c404ef249bb3344bd69136beb8e0ead382e827d082217180b46637"
REFACTORED_MAIN_SHA256 = "ef3ab072868dcb3447b19d224c63879cfa11a3d3906db7d25ac51b77171feb04"
REGISTERED_MAIN_VERSIONS = {LEGACY_MAIN_SHA256: "legacy", REFACTORED_MAIN_SHA256: "refactored"}
START = datetime(2026, 10, 30, 14, 0, 0, tzinfo=timezone.utc)
FINISH = datetime(2026, 10, 30, 14, 0, 10, tzinfo=timezone.utc)
ACCOUNT = "acct_" + "1" * 32
RUN = "run_" + "2" * 32


def canonical(value):
    """Preserve JSON types, absent/null distinctions, and ordered event arrays."""
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@contextmanager
def no_external_effects(monkeypatch):
    absent = object()
    previous_main = sys.modules.get("main", absent)
    attempts = []

    def forbidden(*_args, **_kwargs):
        attempts.append("external_operation")
        raise AssertionError("External effects are forbidden during synthetic endpoint replay")

    def read_only_open(original):
        def checked(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in ("w", "a", "x", "+")):
                return forbidden()
            return original(file, mode, *args, **kwargs)
        return checked

    original_os_open = os.open

    def read_only_os_open(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC):
            return forbidden()
        return original_os_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as guarded:
        guarded.setattr(sys, "dont_write_bytecode", True)
        guarded.setattr(builtins, "open", read_only_open(builtins.open))
        guarded.setattr(io, "open", read_only_open(io.open))
        guarded.setattr(os, "open", read_only_os_open)
        guarded.setattr(os, "system", forbidden)
        for name in ("connect", "connect_ex", "sendto", "bind", "listen"):
            guarded.setattr(socket.socket, name, forbidden)
        guarded.setattr(socket, "create_connection", forbidden)
        guarded.setattr(socket, "getaddrinfo", forbidden)
        guarded.setattr(subprocess, "Popen", forbidden)
        guarded.setattr(urllib.request, "urlopen", forbidden)
        guarded.setattr(Path, "write_text", forbidden)
        guarded.setattr(Path, "write_bytes", forbidden)
        try:
            yield guarded, forbidden
        finally:
            if previous_main is absent:
                sys.modules.pop("main", None)
            else:
                sys.modules["main"] = previous_main
            assert not attempts, "A forbidden external operation was attempted during replay"


def source_fixture():
    path = ROOT / "tests" / "test_historical_source_evidence.py"
    spec = importlib.util.spec_from_file_location("synthetic_endpoint_source_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_document(report, main_sha256):
    """Wrap observed output in the existing synthetic intake fixture, not a real locator."""
    fixture = source_fixture()
    document = fixture.fixture()
    document["source"]["payload"] = deepcopy(report)
    document["source"]["producer"]["files"]["InteractiveBrokersPlatform:main.py"] = main_sha256
    normalized = document["normalized"]
    for key, pointer in fixture.FIELD_SOURCES.items():
        if key in {"account_domain", "receipt", "execution_lane"}:
            continue
        value = report
        for segment in pointer.split("#", 1)[1].strip("/").split("/"):
            value = value[segment]
        normalized[key] = deepcopy(value)
    normalized["receipt"] = deepcopy(report.get("execution_receipt"))
    normalized["execution_lane"] = "dry_run" if report["dry_run"] else "paper"
    fixture.reseal(document)
    return document


@pytest.fixture
def endpoint_replay(strategy_module_factory, monkeypatch):
    observations = []

    def replay(profile, scenario, *, drift=None):
        from application.cycle_result import StrategyCycleResult
        from application.decision_cycle import decision_cycle
        from application.runtime_reporting_adapters import IBKRRuntimeReportingAdapters
        from application import signal_snapshot
        from quant_platform_kit import cloud
        from quant_platform_kit.common import execution_receipts, runtime_reports
        from quant_platform_kit.common.runtime_assembly import RuntimeAssembly
        from quant_platform_kit.common.runtime_logging import emit_runtime_log
        from quant_platform_kit.common.runtime_target import RuntimeTarget
        from quant_platform_kit.common.strategy_release import StrategyReleaseIdentity

        events = []
        reports = []
        source_results = []
        dry_run = scenario.startswith("dry_run_")
        symbol, benchmark = ("TQQQ", "QQQ") if profile == PROFILES[0] else ("SOXL", "SOXX")

        def record(operation, **values):
            # Every sink invocation snapshots the payload before later mutation.
            events.append({"operation": operation, **deepcopy(values)})

        with no_external_effects(monkeypatch) as (guarded, forbidden):
            for name in ("persist_runtime_report", "write_runtime_report_json", "upload_runtime_report_to_cloud"):
                guarded.setattr(runtime_reports, name, forbidden)
            guarded.setattr(cloud, "get_object_store", forbidden)
            guarded.setattr(signal_snapshot, "_utcnow", lambda: START)

            class ReceiptClock(datetime):
                @classmethod
                def now(cls, tz=None):
                    return cls(2026, 10, 30, 14, 0, 9, tzinfo=timezone.utc).astimezone(tz or timezone.utc)

            guarded.setattr(execution_receipts, "datetime", ReceiptClock)
            module = strategy_module_factory(
                STRATEGY_PROFILE=profile, IB_GATEWAY_MODE="paper", NOTIFY_LANG="en",
                RUNTIME_TARGET_ENABLED="false" if scenario == "target_disabled" else "true",
                IBKR_UNIFIED_NOTIFICATION_ENABLED="false", IBKR_FORCE_RUN=None,
            )
            main_sha256 = getattr(module, "__replay_source_sha256__", None)
            if main_sha256 is None:
                main_sha256 = hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
            for name in ("connect_ib", "execute_rebalance", "build_broker_adapters", "recycle_current_process_after_response"):
                guarded.setattr(module, name, forbidden)
            from google.cloud import storage
            guarded.setattr(storage, "Client", forbidden, raising=False)

            release = StrategyReleaseIdentity(
                release_id="fixture-release", manifest_sha256="a" * 64,
                strategy_revision="b" * 40, config_sha256="c" * 64,
                risk_policy_sha256="d" * 64, evidence_sha256="e" * 64,
                plugin_bundle_sha256="f" * 64, effective_session="2026-10-30",
            )
            target = RuntimeTarget(
                platform_id="ibkr", strategy_profile=profile, dry_run_only=dry_run,
                execution_environment="dry_run" if dry_run else "paper",
                account_selector=(ACCOUNT,), account_scope="fixture-scope", strategy_release=release,
            )
            assembly = RuntimeAssembly(
                platform="interactive_brokers", deploy_target="synthetic-offline",
                service_name="fixture-service", strategy_profile=profile,
                account_scope="fixture-scope", runtime_target=target,
            )
            guarded.setattr(module, "RUNTIME_SETTINGS", replace(module.RUNTIME_SETTINGS, runtime_target=target))

            def build_report(**kwargs):
                report = runtime_reports.build_runtime_report_base(**kwargs)
                if scenario == "receipt_conflict":
                    execution_receipts.attach_runtime_execution_receipt(report, outcome="no_action", broker_confirmation="not_applicable")
                reports.append(report)
                record("report_built", report=report)
                return report

            def persist(report, **kwargs):
                record("persistence_attempt", report=report, options=kwargs)
                if scenario == "persistence_failure":
                    raise RuntimeError("synthetic persistence unavailable")
                result = "synthetic-local-report.json" if scenario == "persistence_unconfirmed" else "gs://synthetic-offline/report.json"
                record("persistence_result", result=result)
                return result

            def log(log_context, event, *, printer, **kwargs):
                return emit_runtime_log(log_context, event, printer=printer, now=FINISH, **kwargs)

            adapters = IBKRRuntimeReportingAdapters(
                runtime_assembly=assembly, strategy_domain="us_equity", runtime_target=target,
                extra_context_fields={"account_ids": [ACCOUNT]}, managed_symbols=(symbol, "SGOV"),
                signal_source="synthetic-input", status_icon="synthetic", safe_haven="SGOV",
                strategy_display_name="Synthetic strategy", strategy_display_name_localized="Synthetic strategy",
                dry_run=dry_run, signal_effective_after_trading_days=0,
                strategy_config_source="synthetic-config", runtime_revision="fixture-revision",
                ib_gateway_host_resolver=lambda: "synthetic.invalid", ib_gateway_mode="paper",
                run_id_builder=lambda: RUN, event_logger=log, report_builder=build_report,
                report_persister=persist, trace_extractor=lambda *_args: None,
                printer=lambda line, **_kwargs: record("runtime_log", payload=json.loads(line)),
                clock=lambda: START,
            )
            guarded.setattr(module, "build_composer", lambda **_kwargs: SimpleNamespace(build_reporting_adapters=lambda: adapters))
            guarded.setattr(module, "load_strategy_plugin_signals", lambda: ((), None))
            guarded.setattr(module, "publish_strategy_plugin_alerts", lambda signals, **kwargs: record("plugin_alert_boundary", signals=list(signals)))
            guarded.setattr(module, "is_market_open_now", lambda **_kwargs: scenario != "market_closed")
            guarded.setattr(module, "dca_execution_unsupported_reason", lambda _profile: None)

            class Lock:
                def acquire(self, *, blocking):
                    record("lock_acquire", blocking=blocking)
                    return scenario != "lock_busy"

                def release(self):
                    record("lock_release")

            guarded.setattr(module, "STRATEGY_RUN_LOCK", Lock())

            def notify(**kwargs):
                error = kwargs.pop("exc")
                record("failure_notification_boundary", exception={"type": type(error).__name__, "message": str(error)}, **kwargs)
                return True

            guarded.setattr(module, "_publish_runtime_failure_notification", notify)
            guarded.setattr(module, "send_tg_message", lambda message: record("coverage_notification_boundary", message=message) or True)

            def finalize(report, **kwargs):
                value = runtime_reports.finalize_runtime_report(report, finished_at=FINISH, **kwargs)
                record("report_finalized", report=report)
                return value

            guarded.setattr(module, "finalize_runtime_report", finalize)
            original_attach = module.attach_cycle_decision_receipt

            def attach_decision(report):
                if drift != "omit_decision_finalizer":
                    original_attach(report)
                record("decision_finalizer", report=report)

            guarded.setattr(module, "attach_cycle_decision_receipt", attach_decision)
            if drift == "omit_persistence":
                guarded.setattr(module, "persist_execution_report", lambda *_args, **_kwargs: "gs://synthetic-offline/report.json")

            def core(**kwargs):
                record("strategy_boundary", dry_run_only_override=kwargs["dry_run_only_override"])
                if scenario == "core_failure":
                    raise RuntimeError("synthetic strategy failure")
                if scenario == "gateway_failure":
                    raise module.IBKRGatewayUnavailableError("synthetic gateway failure")
                if scenario == "permission_failure":
                    raise module.IBKRTradingPermissionError("synthetic permission failure")
                order = {"broker_order_id": "synthetic-order-1", "symbol": symbol, "side": "BUY", "quantity": 2}
                execution = {"execution_status": "submitted", "orders_submitted": [order], "orders_skipped": []}
                if scenario == "pending":
                    execution.update(execution_status="pending_reconciliation", orders_pending=[{**order, "status": "Submitted"}])
                elif scenario == "risk_blocked":
                    execution.update(execution_status="blocked", no_op_reason="same_day_execution_locked:synthetic", orders_submitted=[])
                elif dry_run:
                    execution.update(execution_status="dry_run", dry_run=True, orders_submitted=[])
                return StrategyCycleResult(
                    result="Synthetic cycle result", signal_metadata={
                        "strategy_profile": profile, "signal_as_of": "2026-10-29",
                        "market_date": "2026-10-29", "signal": "synthetic_trend",
                        "benchmark_symbol": benchmark, "benchmark_price": 100.0, "active_risk_asset": symbol,
                    }, target_weights={symbol: 0.25, "SGOV": 0.75}, execution_summary=execution,
                    reconciliation_record={"execution_status": execution["execution_status"], "orders_submitted": deepcopy(execution["orders_submitted"])},
                    account_facts={"schema_version": "ibkr_account_snapshot.v1", "account_ids": [ACCOUNT], "observed_at": START.isoformat(), "net_liquidation": 1000.0},
                )

            guarded.setattr(module, "run_strategy_core", core)
            method = "GET" if scenario in {"dry_run_get", "method_rejected"} else "POST"
            route = "/dry-run" if dry_run else "/run"
            with decision_cycle(decision_time=START):
                response = module.app.test_client().open(route, method=method)
            status = response.status_code + (1 if drift == "response_status" else 0)
            headers = sorted((key, ", ".join(sorted(value.split(", "))) if key.lower() == "allow" else value)
                             for key, value in response.headers.items())
            observation = {"request": {"method": method, "path": route},
                           "response": {"status": status, "body": response.get_data(as_text=True), "headers": headers},
                           "reports": deepcopy(reports), "events": deepcopy(events)}
            # Require JSON transportability without discarding any emitted field.
            canonical(observation)
            from scripts.historical_source_evidence import validate_source_evidence
            for report in reports:
                if "execution_receipt" in report:
                    document = source_document(report, main_sha256)
                    source_results.append(validate_source_evidence(document))
            state = {"profile": profile, "scenario": scenario, "main_sha256": main_sha256,
                     "observation": observation, "source_results": source_results}
            observations.append(state)
            return state

    replay.observations = observations
    return replay


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_public_request_matches_legacy_replay(profile, scenario, endpoint_replay):
    state = endpoint_replay(profile, scenario)
    golden = json.loads(GOLDEN.read_text())
    expected = golden["cases"][f"{profile}/{scenario}"]
    assert digest(state["observation"]) == expected["sha256"], canonical(state["observation"])
    assert state["observation"]["response"]["status"] == expected["http_status"]
    producer_kind = REGISTERED_MAIN_VERSIONS.get(state["main_sha256"], "unregistered")
    assert [digest(result) for result in state["source_results"]] == expected["source_validation"][producer_kind]
    for result in state["source_results"]:
        assert result["status"] in {"BLOCKED", "FAIL"}
        assert result["execution_authorized"] is False
        assert result["production_ready"] is False
        assert result["historical_parity_established"] is False
        if state["main_sha256"] not in REGISTERED_MAIN_VERSIONS:
            assert "PRODUCER_VERSION_UNSUPPORTED" in {finding["code"] for finding in result["findings"]}


@pytest.mark.parametrize("drift", ("omit_decision_finalizer", "omit_persistence", "response_status"))
def test_replay_detects_outer_request_boundary_drift(drift, endpoint_replay):
    observed = endpoint_replay(PROFILES[0], "submitted", drift=drift)["observation"]
    expected = json.loads(GOLDEN.read_text())["cases"][f"{PROFILES[0]}/submitted"]
    assert digest(observed) != expected["sha256"]


@pytest.mark.parametrize("operation", ("network", "file"))
def test_replay_rejects_a_suppressed_external_attempt(monkeypatch, operation):
    with pytest.raises(AssertionError, match="forbidden external operation"):
        with no_external_effects(monkeypatch):
            try:
                if operation == "network":
                    urllib.request.urlopen("https://synthetic.invalid")
                else:
                    with open("synthetic-never-written.json", "w"):
                        pass
            except AssertionError:
                pass


def test_replay_comparison_preserves_unknown_fields_and_json_types():
    value = {"status": "unknown", "orders": None, "dry_run": False, "events": ["release", "persist"]}
    for modified in (
        {**value, "orders": []}, {**value, "dry_run": 0},
        {**value, "events": ["persist", "release"]},
        {key: item for key, item in value.items() if key != "orders"},
    ):
        assert digest(modified) != digest(value)


@pytest.mark.parametrize("contradiction", ("profile", "filled_receipt"))
def test_native_unknown_outcome_cannot_be_promoted_by_normalization(contradiction, endpoint_replay):
    from scripts.historical_source_evidence import validate_source_evidence

    state = endpoint_replay(PROFILES[0], "pending")
    document = source_document(state["observation"]["reports"][0], state["main_sha256"])
    fixture = source_fixture()
    if contradiction == "profile":
        document["normalized"]["profile"] = PROFILES[1]
        code = "NORMALIZATION_MISMATCH"
    else:
        receipt = document["normalized"]["receipt"]
        receipt.update(outcome="filled", broker_confirmation="filled")
        fixture.seal_receipt(receipt)
        code = "RECEIPT_NORMALIZATION_MISMATCH"
    fixture.reseal(document)
    result = validate_source_evidence(document)
    assert result["local_consistency"] == result["status"] == "FAIL"
    assert code in {finding["code"] for finding in result["findings"]}
    assert result["execution_authorized"] is False
    assert result["historical_parity_established"] is False
    if state["main_sha256"] not in REGISTERED_MAIN_VERSIONS:
        assert "PRODUCER_VERSION_UNSUPPORTED" in {finding["code"] for finding in result["findings"]}


@pytest.mark.parametrize("conflicting_receipt", (False, True))
def test_unknown_fingerprint_cannot_hide_native_receipt_conflicts(conflicting_receipt, endpoint_replay):
    from scripts.historical_source_evidence import validate_source_evidence

    state = endpoint_replay(PROFILES[0], "pending")
    document = source_document(state["observation"]["reports"][0], "0" * 64)
    if conflicting_receipt:
        document["normalized"]["receipt"].update(outcome="filled", broker_confirmation="filled")
        fixture = source_fixture()
        fixture.seal_receipt(document["normalized"]["receipt"])
        fixture.reseal(document)
    result = validate_source_evidence(document)
    codes = {finding["code"] for finding in result["findings"]}
    assert "PRODUCER_VERSION_UNSUPPORTED" in codes
    assert result["status"] == ("FAIL" if conflicting_receipt else "BLOCKED")
    if conflicting_receipt:
        assert "RECEIPT_NORMALIZATION_MISMATCH" in codes
    else:
        expected = json.loads(GOLDEN.read_text())["cases"][f"{PROFILES[0]}/pending"]
        assert digest(result) == expected["source_validation"]["unregistered"][0]
    for key in ("execution_authorized", "production_ready", "historical_parity_established"):
        assert result[key] is False


@pytest.mark.parametrize("cached", (False, True), ids=("absent", "existing"))
@pytest.mark.parametrize("raises", (False, True), ids=("success", "exception"))
def test_endpoint_replay_restores_main_cache_and_native_aliases(cached, raises, endpoint_replay, monkeypatch):
    from application import runtime_reporting_adapters
    from quant_platform_kit.common import runtime_reports

    names = ("build_runtime_report_base", "finalize_runtime_report", "persist_runtime_report")
    native = {name: getattr(runtime_reports, name) for name in names}
    prior = ModuleType("main")
    for name, function in native.items():
        setattr(prior, name, function)
    if cached:
        monkeypatch.setitem(sys.modules, "main", prior)
    else:
        monkeypatch.delitem(sys.modules, "main", raising=False)

    def assembly_failure(**_kwargs):
        raise RuntimeError("synthetic reporting assembly failure")

    if raises:
        monkeypatch.setattr(runtime_reporting_adapters, "IBKRRuntimeReportingAdapters", assembly_failure)
        with pytest.raises(RuntimeError, match="synthetic reporting assembly failure"):
            endpoint_replay(PROFILES[0], "submitted")
    else:
        endpoint_replay(PROFILES[0], "submitted")
    if cached:
        assert sys.modules["main"] is prior
        assert all(getattr(sys.modules["main"], name) is native[name] for name in names)
    else:
        assert "main" not in sys.modules
    # Inspect function identity only; never call any persistence implementation.
    assert all(getattr(runtime_reports, name) is native[name] for name in names)
