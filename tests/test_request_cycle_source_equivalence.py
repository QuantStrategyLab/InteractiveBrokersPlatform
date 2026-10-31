"""Native synthetic component observations; not endpoint or historical replay.

The golden observations were captured from the frozen pre-extraction reporting
block with the repository's pinned native helpers. CI compares the current
component with those observations; it does not execute a second runtime or
authorize producer registration. Runtime ``main`` is parsed, never imported.
"""

from __future__ import annotations

import ast
import builtins
from collections.abc import Mapping
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace
import urllib.request

import pytest


ROOT = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).with_name("fixtures") / "request-cycle-native-observations.json"
PROFILES = ("tqqq_growth_income", "soxl_soxx_trend_income")
SCENARIOS = (
    "submitted_unconfirmed", "expected_guard", "pending_reconciliation",
    "failed_submission", "dry_run_preview", "conflicting_receipt",
)
HELPERS = (
    "_is_execution_failure", "_has_signal_snapshot_details", "_count_orders",
    "_build_cycle_report_summary", "_account_facts_match_runtime_target",
    "_build_notification_delivery_log_for_report", "_build_notification_delivery_summary",
)
COMPONENT = "_finalize_request_cycle_report"
GUARD_PREFIXES = "EXPECTED_EXECUTION_GUARD_REASON_PREFIXES"
START = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
FINISH = datetime(2026, 1, 2, 12, 0, 10, tzinfo=timezone.utc)
ACCOUNT = "synthetic-account"
LEGACY_MAIN_SHA256 = "f13b55f501c404ef249bb3344bd69136beb8e0ead382e827d082217180b46637"
REFACTORED_MAIN_SHA256 = "ef3ab072868dcb3447b19d224c63879cfa11a3d3906db7d25ac51b77171feb04"


def _ast_digest(nodes):
    module = ast.Module(body=nodes, type_ignores=[])
    return hashlib.sha256(ast.dump(module, include_attributes=False).encode()).hexdigest()


def _component_tree(source):
    """Select definitions only; no imports or module initialization from main."""
    tree = ast.parse(source)
    selected = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in (*HELPERS, COMPONENT):
            selected.append(deepcopy(node))
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == GUARD_PREFIXES
            for target in node.targets
        ):
            selected.append(deepcopy(node))
    names = [node.name for node in selected if isinstance(node, ast.FunctionDef)]
    assert sorted(names) == sorted((*HELPERS, COMPONENT)), names
    assert len(selected) == len(HELPERS) + 2
    return selected


def _component_contract(nodes):
    component = next(node for node in nodes if isinstance(node, ast.FunctionDef) and node.name == COMPONENT)
    body = list(component.body)
    if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body.pop(0)
    return {
        "component_body_sha256": _ast_digest(body),
        "helper_definitions_sha256": _ast_digest([node for node in nodes if node is not component]),
        "keyword_parameters": [arg.arg for arg in component.args.kwonlyargs],
        "statement_count": len(body),
    }


def _json_observation(value):
    """Check JSON transportability without dropping any field or value."""
    return json.loads(json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False))


def _assert_observation_equal(actual, expected):
    # Canonical JSON preserves distinctions such as false/0 and 1/1.0 that
    # Python container equality would otherwise hide from receipt digest checks.
    options = {"sort_keys": True, "ensure_ascii": True, "allow_nan": False, "separators": (",", ":")}
    assert json.dumps(actual, **options) == json.dumps(expected, **options)


@contextmanager
def _no_external_effects(monkeypatch):
    """Fail before any external transport or output-file operation can occur."""
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append("external_operation")
        raise AssertionError("External effects are forbidden in component characterization")

    original_import = builtins.__import__
    original_open = builtins.open
    original_io_open = io.open

    def restricted_import(name, *args, **kwargs):
        if name == "main" or name.startswith(("ib_insync", "ibapi", "google.cloud")):
            attempts.append("runtime_or_broker_import")
            raise AssertionError("Runtime or broker imports are forbidden in component characterization")
        return original_import(name, *args, **kwargs)

    def read_only_open(original):
        def checked(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in ("w", "a", "x", "+")):
                return forbidden()
            return original(file, mode, *args, **kwargs)
        return checked

    with monkeypatch.context() as guarded:
        guarded.setattr(builtins, "__import__", restricted_import)
        guarded.setattr(builtins, "open", read_only_open(original_open))
        guarded.setattr(io, "open", read_only_open(original_io_open))
        for attribute in ("connect", "connect_ex", "sendto", "bind", "listen"):
            guarded.setattr(socket.socket, attribute, forbidden)
        guarded.setattr(socket, "create_connection", forbidden)
        guarded.setattr(socket, "getaddrinfo", forbidden)
        guarded.setattr(subprocess, "Popen", forbidden)
        guarded.setattr(urllib.request, "urlopen", forbidden)
        guarded.setattr(Path, "write_text", forbidden)
        guarded.setattr(Path, "write_bytes", forbidden)
        try:
            yield guarded, forbidden
        finally:
            assert not attempts, "Forbidden external operations were attempted during characterization"


def _synthetic_cycle(profile, scenario):
    from application.cycle_result import StrategyCycleResult

    symbol, benchmark = ("TQQQ", "QQQ") if profile == PROFILES[0] else ("SOXL", "SOXX")
    order = {"broker_order_id": "synthetic-order-1", "symbol": symbol, "side": "BUY", "quantity": 2}
    execution = {
        "execution_status": "submitted", "orders_submitted": [order],
        "orders_skipped": [], "price_source_mode": "synthetic_quote",
        "quote_snapshot": {symbol: {"price": 100.0, "source": "synthetic_quote"}},
    }
    if scenario == "expected_guard":
        execution.update(execution_status="blocked", no_op_reason="same_day_execution_locked:synthetic", orders_submitted=[])
    elif scenario == "pending_reconciliation":
        execution.update(execution_status="pending_reconciliation", orders_pending=[{**order, "status": "Submitted"}])
    elif scenario == "failed_submission":
        execution.update(execution_status="error", no_op_reason="synthetic_transport_unknown", outcome_unknown=True)
    elif scenario == "dry_run_preview":
        execution.update(execution_status="dry_run", dry_run=True)
    reconciliation = {"execution_status": execution["execution_status"], "orders_submitted": deepcopy(execution["orders_submitted"])}
    metadata = {
        "strategy_profile": profile, "signal_as_of": "2026-01-01",
        "market_date": "2026-01-01", "signal": "synthetic_trend",
        "benchmark_symbol": benchmark, "benchmark_price": 100.0,
        "active_risk_asset": symbol,
    }
    return StrategyCycleResult(
        result="synthetic-cycle-result", signal_metadata=metadata,
        target_weights={symbol: 0.25, "SGOV": 0.75}, execution_summary=execution,
        reconciliation_record=reconciliation,
        reconciliation_record_path="synthetic/reconciliation.json",
        account_facts={
            "schema_version": "ibkr_account_snapshot.v1", "account_ids": [ACCOUNT],
            "observed_at": START.isoformat(), "net_liquidation": 1000.0,
        },
    )


def _observe_component(nodes, profile, scenario, monkeypatch):
    """Run the reporting component with native helpers and synthetic cycle input."""
    events = []
    with _no_external_effects(monkeypatch) as (guarded, forbidden):
        from application.execution_receipt_adapter import attach_cycle_execution_receipt
        from application.runtime_reporting_adapters import IBKRRuntimeReportingAdapters
        from application import signal_snapshot
        from quant_platform_kit import cloud
        from quant_platform_kit.common import execution_receipts, runtime_reports
        from quant_platform_kit.common.runtime_assembly import RuntimeAssembly
        from quant_platform_kit.common.runtime_logging import emit_runtime_log
        from quant_platform_kit.common.runtime_target import RuntimeTarget
        from quant_platform_kit.common.strategy_release import StrategyReleaseIdentity

        for name in ("persist_runtime_report", "write_runtime_report_json", "upload_runtime_report_to_cloud"):
            guarded.setattr(runtime_reports, name, forbidden)
        guarded.setattr(cloud, "get_object_store", forbidden)
        guarded.setattr(signal_snapshot, "_utcnow", lambda: START)

        class ReceiptClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 1, 2, 12, 0, 9, tzinfo=timezone.utc).astimezone(tz or timezone.utc)

        guarded.setattr(execution_receipts, "datetime", ReceiptClock)
        dry_run = scenario == "dry_run_preview"
        release = StrategyReleaseIdentity(
            release_id="synthetic-release", manifest_sha256="a" * 64,
            strategy_revision="b" * 40, config_sha256="c" * 64,
            risk_policy_sha256="d" * 64, evidence_sha256="e" * 64,
            plugin_bundle_sha256="f" * 64, effective_session="2026-01-01",
        )
        target = RuntimeTarget(
            platform_id="ibkr", strategy_profile=profile, dry_run_only=dry_run,
            execution_environment="dry_run" if dry_run else "paper",
            account_selector=(ACCOUNT,), account_scope="synthetic-scope", strategy_release=release,
        )
        assembly = RuntimeAssembly(
            platform="interactive_brokers", deploy_target="synthetic-offline",
            service_name="synthetic-service", strategy_profile=profile,
            account_scope="synthetic-scope", runtime_target=target,
        )
        context = assembly.build_log_context(run_id=f"synthetic-{profile}-{scenario}")

        def construct(**kwargs):
            events.append({"operation": "build_runtime_report_base"})
            return runtime_reports.build_runtime_report_base(**kwargs)

        adapters = IBKRRuntimeReportingAdapters(
            runtime_assembly=assembly, strategy_domain="us_equity", runtime_target=target,
            extra_context_fields={"account_ids": [ACCOUNT]}, managed_symbols=("TQQQ" if profile == PROFILES[0] else "SOXL", "SGOV"),
            signal_source="synthetic-input", status_icon="synthetic", safe_haven="SGOV",
            strategy_display_name="Synthetic strategy", strategy_display_name_localized="Synthetic strategy",
            dry_run=dry_run, signal_effective_after_trading_days=0,
            strategy_config_source="synthetic-config", runtime_revision="synthetic-runtime-revision",
            ib_gateway_host_resolver=lambda: "synthetic.invalid", ib_gateway_mode="paper",
            run_id_builder=lambda: context.run_id, event_logger=forbidden,
            report_builder=construct, report_persister=forbidden,
            trace_extractor=forbidden, printer=forbidden, clock=lambda: START,
        )
        report = adapters.build_report(context)
        report["decision_at"] = START.isoformat()
        if scenario == "conflicting_receipt":
            execution_receipts.attach_runtime_execution_receipt(report, outcome="no_action", broker_confirmation="not_applicable")
            events.append({"operation": "seed_conflicting_receipt"})

        def log_event(log_context, event, **kwargs):
            captured = []
            payload = emit_runtime_log(log_context, event, printer=lambda line, **unused: captured.append(line), now=FINISH, **kwargs)
            assert len(captured) == 1
            assert json.loads(captured[0]) == payload
            events.append({"operation": "emit_runtime_log", "payload": payload})
            return payload

        def attach(*args, **kwargs):
            events.append({"operation": "attach_cycle_execution_receipt"})
            return attach_cycle_execution_receipt(*args, **kwargs)

        def finalize(*args, **kwargs):
            events.append({"operation": "finalize_runtime_report"})
            return runtime_reports.finalize_runtime_report(*args, finished_at=FINISH, **kwargs)

        def append_error(*args, **kwargs):
            events.append({"operation": "append_runtime_report_error"})
            return runtime_reports.append_runtime_report_error(*args, **kwargs)

        namespace = {
            "Mapping": Mapping, "STRATEGY_PROFILE": profile,
            "RUNTIME_SETTINGS": SimpleNamespace(runtime_target=target),
            "build_signal_snapshot": signal_snapshot.build_signal_snapshot,
            "log_runtime_event": log_event,
            "attach_cycle_execution_receipt": attach,
            "finalize_runtime_report": finalize,
            "append_runtime_report_error": append_error,
        }
        module = ast.Module(
            body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *deepcopy(nodes)],
            type_ignores=[],
        )
        exec(compile(ast.fix_missing_locations(module), "<synthetic-reporting-component>", "exec"), namespace)
        cycle = _synthetic_cycle(profile, scenario)
        deliveries = [{"sink": "synthetic-sink", "delivery_status": "sent", "transport_acknowledged": True,
                       "compact_text_sha256": "1" * 64, "compact_text_length": 17}]
        error = None
        try:
            namespace[COMPONENT](
                cycle_result=cycle, execution_summary=deepcopy(cycle.execution_summary), report=report,
                log_context=context, notification_delivery_events=deliveries,
                execution_window="dry_run" if dry_run else "execution", dry_run_only_override=True if dry_run else None,
            )
        except ValueError as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
        return _json_observation({"report": report, "events": events, "exception": error})


@pytest.fixture(scope="module")
def native_golden():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_native_component_matches_legacy_observations(profile, scenario, native_golden, monkeypatch):
    nodes = _component_tree((ROOT / "main.py").read_text(encoding="utf-8"))
    assert _component_contract(nodes) == native_golden["component_contract"]
    observed = _observe_component(nodes, profile, scenario, monkeypatch)
    _assert_observation_equal(observed, native_golden["observations"][f"{profile}/{scenario}"])


@pytest.mark.parametrize("drift", ("payload", "receipt", "event_order", "field_type"))
def test_native_observation_comparison_detects_material_drift(drift, native_golden, monkeypatch):
    """Mutate only an in-memory AST; production source and helpers stay intact."""
    nodes = _component_tree((ROOT / "main.py").read_text(encoding="utf-8"))
    component = next(node for node in nodes if isinstance(node, ast.FunctionDef) and node.name == COMPONENT)
    calls = {node.value.func.id: index for index, node in enumerate(component.body)
             if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)}
    if drift == "event_order":
        first, second = calls["attach_cycle_execution_receipt"], calls["finalize_runtime_report"]
        component.body[first], component.body[second] = component.body[second], component.body[first]
    elif drift == "receipt":
        component.body.insert(calls["attach_cycle_execution_receipt"] + 1, ast.parse('report["execution_receipt"]["outcome"] = "filled"').body[0])
    elif drift == "field_type":
        component.body.append(ast.parse('report["dry_run"] = 0').body[0])
    else:
        component.body.append(ast.parse('report["diagnostics"]["synthetic_drift"] = True').body[0])
    observed = _observe_component(nodes, PROFILES[0], "submitted_unconfirmed", monkeypatch)
    expected = native_golden["observations"][f"{PROFILES[0]}/submitted_unconfirmed"]
    with pytest.raises(AssertionError):
        _assert_observation_equal(observed, expected)
    if drift == "event_order":
        assert observed["report"] == expected["report"]
        assert observed["events"] != expected["events"]
    elif drift == "field_type":
        assert observed == expected  # The canonical comparison above still rejects the drift.


def test_suppressed_external_effect_attempt_still_fails_observation(monkeypatch):
    with pytest.raises(AssertionError, match="Forbidden external operations were attempted"):
        with _no_external_effects(monkeypatch):
            try:
                urllib.request.urlopen("https://synthetic.invalid")
            except AssertionError:
                pass


@pytest.mark.parametrize("conflicting_identity", (False, True))
def test_refactored_fingerprint_remains_unsupported_with_conflicts_preserved(conflicting_identity):
    """Reuse the committed synthetic envelope without changing its validator."""
    fixture_path = ROOT / "tests" / "test_historical_source_evidence.py"
    spec = importlib.util.spec_from_file_location("synthetic_source_evidence_fixture", fixture_path)
    fixture_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture_module)
    from scripts import historical_source_evidence

    key = "InteractiveBrokersPlatform:main.py"
    assert historical_source_evidence._REPORT_FILES[key] == LEGACY_MAIN_SHA256
    actual_main_sha256 = hashlib.sha256((ROOT / "main.py").read_bytes()).hexdigest()
    assert actual_main_sha256 != LEGACY_MAIN_SHA256
    document = fixture_module.fixture()
    document["source"]["producer"]["files"][key] = actual_main_sha256
    if conflicting_identity:
        document["normalized"]["runtime_revision"] = "synthetic-conflicting-revision"
        fixture_module.reseal(document)
    before = deepcopy(document)
    result = historical_source_evidence.validate_source_evidence(document)
    assert document == before
    assert result["status"] == ("FAIL" if conflicting_identity else "BLOCKED")
    assert "PRODUCER_VERSION_UNSUPPORTED" in {finding["code"] for finding in result["findings"]}
    if conflicting_identity:
        assert any(finding["status"] == "FAIL" for finding in result["findings"])
    for key in ("execution_authorized", "production_ready", "historical_parity_established"):
        assert result[key] is False
