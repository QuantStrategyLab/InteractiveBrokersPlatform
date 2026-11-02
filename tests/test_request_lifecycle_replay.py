"""Replay pinned synthetic request sequences without importing runtime main.

The reference contains observations captured from the complete legacy request
handler. External strategy, persistence and delivery boundaries are intercepted;
the native report, receipt, decision-cycle and provenance helpers still execute.
This is request-handler characterization, not authenticated historical replay.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from tests import test_request_cycle_source_equivalence as component


ROOT = Path(__file__).resolve().parents[1]
REFERENCE = Path(__file__).with_name("fixtures") / "request-lifecycle-observations.json"
HANDLER = "_handle_request"
HELPERS = (*component.HELPERS, "attach_signal_data_provenance", HANDLER)


def _handler_nodes(source):
    """Select function definitions and the fixed guard constant only."""
    selected = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name in (*HELPERS, component.COMPONENT):
            selected.append(deepcopy(node))
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == component.GUARD_PREFIXES
            for target in node.targets
        ):
            selected.append(deepcopy(node))
    names = [node.name for node in selected if isinstance(node, ast.FunctionDef)]
    assert len(names) == len(set(names))
    assert set(names) in (set(HELPERS), set((*HELPERS, component.COMPONENT)))
    assert len(selected) == len(names) + 1
    return selected


def _handler_contract(nodes):
    return {"selected_ast_sha256": component._ast_digest(nodes)}


def _replay_requests(nodes, profile, inputs, monkeypatch):
    """Keep per-sequence state, while giving each request a fresh native context."""
    observations = []
    with component._no_external_effects(monkeypatch) as (guarded, forbidden):
        from application import decision_cycle, signal_provenance, signal_snapshot
        from application.cycle_result import StrategyCycleResult, coerce_strategy_cycle_result
        from application.execution_receipt_adapter import (
            attach_cycle_execution_receipt,
            attach_terminal_fallback_execution_receipt,
        )
        from application.ibkr_portfolio import IBKRPortfolioSnapshotUnavailableError
        from application.runtime_broker_adapters import IBKRGatewayUnavailableError, IBKRTradingPermissionError
        from application.runtime_deadline import RuntimeDeadlineExceeded
        from application.runtime_reporting_adapters import IBKRRuntimeReportingAdapters
        from application.runtime_strategy_adapters import IBKRRuntimeStrategyAdapters
        from notifications import unified_adapter
        from quant_platform_kit import cloud
        from quant_platform_kit.common import execution_receipts, runtime_reports
        from quant_platform_kit.common.runtime_assembly import RuntimeAssembly
        from quant_platform_kit.common.runtime_logging import emit_runtime_log
        from quant_platform_kit.common.runtime_target import RuntimeTarget
        from quant_platform_kit.common.strategy_release import StrategyReleaseIdentity

        for name in ("persist_runtime_report", "write_runtime_report_json", "upload_runtime_report_to_cloud"):
            guarded.setattr(runtime_reports, name, forbidden)
        guarded.setattr(cloud, "get_object_store", forbidden)
        # Both runs start from the same explicit empty state. This replacement
        # lasts for the entire sequence, so native resets between requests matter.
        guarded.setattr(signal_provenance, "_RECORDS", {})
        guarded.setattr(unified_adapter, "_RUN", ContextVar("synthetic_notification_replay", default=None))
        assert decision_cycle.current_cycle() is None
        assert not signal_provenance.snapshot()
        state = SimpleNamespace(step=None, now=None, observation=None, report=None, target=None)
        guarded.setattr(signal_snapshot, "_utcnow", lambda: state.now)

        class ReceiptClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return (state.now + timedelta(seconds=9)).astimezone(tz or timezone.utc)

        guarded.setattr(execution_receipts, "datetime", ReceiptClock)

        def event(operation, **values):
            state.observation["events"].append({"operation": operation, **deepcopy(values)})

        class SequenceLock:
            def __init__(self):
                self.lock = threading.Lock()

            def acquire(self, *, blocking):
                assert blocking is False
                acquired = self.lock.acquire(blocking=False)
                event("lock_acquire", acquired=acquired)
                return acquired

            def release(self):
                self.lock.release()
                event("lock_release")

        lock = SequenceLock()
        release = StrategyReleaseIdentity(
            release_id="synthetic-release", manifest_sha256="a" * 64,
            strategy_revision="b" * 40, config_sha256="c" * 64,
            risk_policy_sha256="d" * 64, evidence_sha256="e" * 64,
            plugin_bundle_sha256="f" * 64, effective_session="2026-01-01",
        )
        settings = SimpleNamespace(runtime_target=None, runtime_target_enabled=True)

        def build_context():
            event("build_request_log_context")
            state.target = RuntimeTarget(
                platform_id="ibkr", strategy_profile=profile,
                dry_run_only=bool(state.step["dry_run_only_override"]),
                execution_environment="dry_run" if state.step["dry_run_only_override"] else "paper",
                account_selector=(component.ACCOUNT,), account_scope="synthetic-scope", strategy_release=release,
            )
            settings.runtime_target = state.target
            return RuntimeAssembly(
                platform="interactive_brokers", deploy_target="synthetic-offline",
                service_name="synthetic-service", strategy_profile=profile,
                account_scope="synthetic-scope", runtime_target=state.target,
            ).build_log_context(run_id=state.step["run_id"])

        def build_report(context, *, dry_run_only_override):
            event("build_execution_report")
            assembly = RuntimeAssembly(
                platform="interactive_brokers", deploy_target="synthetic-offline",
                service_name="synthetic-service", strategy_profile=profile,
                account_scope="synthetic-scope", runtime_target=state.target,
            )
            adapters = IBKRRuntimeReportingAdapters(
                runtime_assembly=assembly, strategy_domain="us_equity", runtime_target=state.target,
                extra_context_fields={"account_ids": [component.ACCOUNT]},
                managed_symbols=("TQQQ" if profile == component.PROFILES[0] else "SOXL", "SGOV"),
                signal_source="synthetic-input", status_icon="synthetic", safe_haven="SGOV",
                strategy_display_name="Synthetic strategy", strategy_display_name_localized="Synthetic strategy",
                dry_run=bool(dry_run_only_override), signal_effective_after_trading_days=0,
                strategy_config_source="synthetic-config", runtime_revision="synthetic-runtime-revision",
                ib_gateway_host_resolver=lambda: "synthetic.invalid", ib_gateway_mode="paper",
                run_id_builder=lambda: context.run_id, event_logger=forbidden,
                report_builder=runtime_reports.build_runtime_report_base, report_persister=forbidden,
                trace_extractor=forbidden, printer=forbidden, clock=lambda: state.now,
            )
            state.report = adapters.build_report(context)
            return state.report

        def log_event(context, name, **kwargs):
            serialized = []
            payload = emit_runtime_log(
                context, name, printer=lambda line, **unused: serialized.append(line),
                now=state.now + timedelta(seconds=10), **kwargs,
            )
            assert len(serialized) == 1
            component._assert_observation_equal(json.loads(serialized[0]), payload)
            event("emit_runtime_log", payload=payload)
            return payload

        def load_plugins():
            event("load_strategy_plugin_signals")
            if state.step["fault"] == "plugin_deadline":
                raise RuntimeDeadlineExceeded("synthetic plugin deadline")
            return (), state.step["plugin_error"]

        def attach_plugins(report, *, signals, error):
            event("attach_strategy_plugin_report", error=error)
            return IBKRRuntimeStrategyAdapters.attach_strategy_plugin_report(
                SimpleNamespace(build_strategy_plugin_report_payload_fn=None), report, signals=signals, error=error,
            )

        def core(**kwargs):
            event("run_strategy_core")
            assert unified_adapter._RUN.get() is state.report
            assert kwargs["strategy_plugin_signals"] == ()
            assert kwargs["strategy_plugin_error"] == state.step["plugin_error"]
            for row in state.step["provenance"]:
                signal_provenance.record(**row)
            if state.step["fault"] == "core_deadline":
                raise RuntimeDeadlineExceeded("synthetic strategy deadline")
            kwargs["notification_delivery_events"].extend(deepcopy(state.step["delivery_events"]))
            assert isinstance(state.step["cycle_result"], dict)
            return StrategyCycleResult(**deepcopy(state.step["cycle_result"]))

        def capture(summary, *, dry_run):
            event("capture_cycle_result_evidence", execution_summary=summary, dry_run=dry_run)
            return decision_cycle.capture_cycle_result_evidence(summary, dry_run=dry_run)

        def cycle_receipt(*args, **kwargs):
            event("attach_cycle_execution_receipt")
            return attach_cycle_execution_receipt(*args, **kwargs)

        def fallback_receipt(report):
            event("attach_terminal_fallback_execution_receipt")
            return attach_terminal_fallback_execution_receipt(report)

        def decision_receipt(report):
            event("attach_cycle_decision_receipt")
            return decision_cycle.attach_cycle_decision_receipt(report)

        def finalize(*args, **kwargs):
            event("finalize_runtime_report", fields=kwargs)
            return runtime_reports.finalize_runtime_report(*args, finished_at=state.now + timedelta(seconds=10), **kwargs)

        def append_error(*args, **kwargs):
            event("append_runtime_report_error", fields=kwargs)
            return runtime_reports.append_runtime_report_error(*args, **kwargs)

        def persist(report, **kwargs):
            event("persist_execution_report", request_index=len(state.observation["persistence_requests"]))
            state.observation["persistence_requests"].append({"report": deepcopy(report), "options": kwargs})
            return state.step["persistence_return"]

        def failure_notification(**kwargs):
            event("runtime_failure_notification", detailed_text=kwargs["detailed_text"],
                  compact_text=kwargs["compact_text"], error_type=type(kwargs["exc"]).__name__)

        def coverage_notification(message):
            event("coverage_notification", message=message)
            return state.step["coverage_delivery_acknowledged"]

        namespace = {
            "Mapping": Mapping, "STRATEGY_PROFILE": profile, "RUNTIME_SETTINGS": settings,
            "request": SimpleNamespace(method="POST"), "current_cycle": decision_cycle.current_cycle,
            "signal_provenance": signal_provenance, "build_signal_snapshot": signal_snapshot.build_signal_snapshot,
            "build_request_log_context": build_context, "build_execution_report": build_report,
            "load_strategy_plugin_signals": load_plugins, "attach_strategy_plugin_report": attach_plugins,
            "STRATEGY_RUN_LOCK": lock, "log_runtime_event": log_event,
            "is_market_open_now": lambda **unused: state.step["market_open"],
            "MARKET_CALENDAR": "NYSE", "MARKET_TIMEZONE": "America/New_York", "MARKET": "US",
            "os": SimpleNamespace(getenv=lambda unused: None), "dca_execution_unsupported_reason": lambda unused: None,
            "IBKR_FRACTIONAL_EQUITY_API_UNSUPPORTED_SKIP_REASON": "synthetic-unused-fractional-guard",
            "publish_strategy_plugin_alerts": lambda signals, **unused: event("publish_strategy_plugin_alerts", signals=list(signals)),
            "coerce_strategy_cycle_result": coerce_strategy_cycle_result,
            "with_notification_run": unified_adapter.with_run, "run_strategy_core": core,
            "capture_cycle_result_evidence": capture, "attach_cycle_execution_receipt": cycle_receipt,
            "finalize_runtime_report": finalize, "append_runtime_report_error": append_error,
            "RuntimeDeadlineExceeded": RuntimeDeadlineExceeded, "IBKR_DRY_RUN_DEADLINE_SECONDS": 60,
            "IBKRGatewayUnavailableError": IBKRGatewayUnavailableError,
            "IBKRTradingPermissionError": IBKRTradingPermissionError,
            "IBKRPortfolioSnapshotUnavailableError": IBKRPortfolioSnapshotUnavailableError,
            "attach_terminal_fallback_execution_receipt": fallback_receipt,
            "attach_cycle_decision_receipt": decision_receipt, "persist_execution_report": persist,
            "_publish_runtime_failure_notification": failure_notification,
            "_runtime_error_notification_message": lambda exc: "synthetic failure: " + type(exc).__name__,
            "t": lambda unused: "synthetic runtime failure", "NOTIFY_LANG": "en",
            "send_tg_message": coverage_notification,
            "print": lambda *args, **unused: event("print", values=list(args)),
        }
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                                  *deepcopy(nodes)], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "<synthetic-request-replay>", "exec"), namespace)
        for step in deepcopy(inputs):
            state.step = step
            state.now = datetime.fromisoformat(step["decision_at"])
            state.report = None
            state.observation = {"name": step["name"], "events": [], "persistence_requests": []}
            namespace["request"].method = step["method"]
            settings.runtime_target_enabled = step["runtime_target_enabled"]
            collector = []
            response = exception = None
            with decision_cycle.decision_cycle(decision_time=state.now) as cycle:
                try:
                    response = namespace[HANDLER](
                        dry_run_only_override=step["dry_run_only_override"],
                        response_body="Synthetic Dry Run OK", persist_report=step["persist_report"],
                        report_collector=collector,
                    )
                except RuntimeDeadlineExceeded as exc:
                    exception = {"type": type(exc).__name__, "message": str(exc)}
                execution_evidence = {key: deepcopy(cycle[key]) for key in (
                    "execution_order_records", "execution_fill_records",
                ) if key in cycle}
                registered = cycle.get("report") is state.report if state.report is not None else "report" not in cycle
            assert not cycle["active"]
            state.observation.update(
                response=response, exception=exception, report=deepcopy(state.report),
                collected_reports=deepcopy(collector), execution_evidence=execution_evidence,
                report_registered=registered, lock_held=lock.lock.locked(),
                decision_context_cleared=decision_cycle.current_cycle() is None,
                notification_context_cleared=unified_adapter._RUN.get() is None,
            )
            observations.append(component._json_observation(state.observation))
        return observations


@pytest.fixture(scope="module")
def lifecycle_reference():
    return json.loads(REFERENCE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("profile", component.PROFILES)
def test_request_sequence_matches_frozen_legacy_observations(profile, lifecycle_reference, monkeypatch):
    nodes = _handler_nodes((ROOT / "main.py").read_text(encoding="utf-8"))
    assert _handler_contract(nodes) == lifecycle_reference["candidate_contract"]
    inputs = lifecycle_reference["inputs"][profile]
    before = deepcopy(inputs)
    actual = _replay_requests(nodes, profile, inputs, monkeypatch)
    component._assert_observation_equal(actual, lifecycle_reference["observations"][profile])
    assert inputs == before
    assert all(not item["lock_held"] and item["decision_context_cleared"]
               and item["notification_context_cleared"] and item["report_registered"] for item in actual)


@pytest.mark.parametrize("drift", ("deadline_persistence", "unknown_evidence", "lock_release", "provenance_reset"))
def test_request_replay_detects_lifecycle_drift(drift, lifecycle_reference, monkeypatch):
    """Change only the in-memory handler AST; runtime files remain unchanged."""
    nodes = _handler_nodes((ROOT / "main.py").read_text(encoding="utf-8"))
    handler = next(node for node in nodes if isinstance(node, ast.FunctionDef) and node.name == HANDLER)
    modified = []
    if drift == "deadline_persistence":
        for node in ast.walk(handler):
            if isinstance(node, ast.ExceptHandler) and isinstance(node.type, ast.Name) and node.type.id == "RuntimeDeadlineExceeded":
                assert isinstance(node.body[0], ast.Assign) and node.body[0].targets[0].id == "deadline_exceeded"
                node.body[0].value = ast.Constant(value=False)
                modified.append(node)
    elif drift == "unknown_evidence":
        for node in ast.walk(handler):
            if isinstance(node, ast.If) and node.body and isinstance(node.body[0], ast.Expr):
                call = node.body[0].value
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "capture_cycle_result_evidence":
                    node.body.extend(ast.parse('current_cycle()["execution_order_records"] = []\ncurrent_cycle()["execution_fill_records"] = []').body)
                    modified.append(node)
    else:
        target = "STRATEGY_RUN_LOCK" if drift == "lock_release" else "signal_provenance"
        attribute = "release" if drift == "lock_release" else "reset"
        for node in ast.walk(handler):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                call = node.value.func
                if isinstance(call, ast.Attribute) and isinstance(call.value, ast.Name) and call.value.id == target and call.attr == attribute:
                    node.value = ast.Constant(value=None)
                    modified.append(node)
    assert len(modified) == 1
    profile = component.PROFILES[0]
    expected = lifecycle_reference["observations"][profile]
    actual = _replay_requests(nodes, profile, lifecycle_reference["inputs"][profile], monkeypatch)
    with pytest.raises(AssertionError):
        component._assert_observation_equal(actual, expected)
    if drift in ("deadline_persistence", "unknown_evidence"):
        # A report-only comparator would miss these lifecycle/evidence errors.
        component._assert_observation_equal([item["report"] for item in actual], [item["report"] for item in expected])
        key = "persistence_requests" if drift == "deadline_persistence" else "execution_evidence"
        assert [item[key] for item in actual] != [item[key] for item in expected]
