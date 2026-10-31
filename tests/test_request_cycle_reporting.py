"""Characterize the request/report boundary with synthetic cycle results."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from application.cycle_result import StrategyCycleResult
from application.decision_cycle import decision_cycle
import application.signal_snapshot as signal_snapshot_module
import notifications.unified_adapter as unified_notifications


_CLOCK = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
_REPORT_URI = "gs://<bucket>/synthetic-request-report.json"


@pytest.fixture
def reporting_case(strategy_module_factory, monkeypatch):
    """Keep reporting helpers real; replace only cycle/transport boundaries."""
    module = strategy_module_factory(STRATEGY_PROFILE="nasdaq_sp500_smart_dca")
    state = SimpleNamespace(
        module=module,
        trace=[],
        log_events=[],
        report=None,
        collector=[],
        persisted=[],
        failure_notifications=[],
        cycle=None,
        notification_events=[],
        cycle_result=StrategyCycleResult(
            result="synthetic cycle result",
            execution_summary={"execution_status": "executed", "orders_submitted": []},
        ),
        fail_stage=None,
        error=None,
        acquire=True,
        market_open=True,
        unsupported=None,
        plugin_error=None,
        persist_error=None,
        attach_receipt=True,
    )

    def mark(name):
        state.trace.append(name)
        if state.fail_stage == name:
            state.fail_stage = None
            raise state.error

    def forbid(*_args, **_kwargs):
        raise AssertionError("report characterization must not access a broker or send a notification")

    for name in ("connect_ib", "execute_rebalance", "build_broker_adapters", "send_tg_message"):
        monkeypatch.setattr(module, name, forbid)
    monkeypatch.delenv("IBKR_FORCE_RUN", raising=False)
    monkeypatch.setattr(signal_snapshot_module, "_utcnow", lambda: _CLOCK)

    def build_context():
        mark("context")
        return SimpleNamespace(run_id="synthetic-request")

    def build_report(_context, *, dry_run_only_override):
        mark("report")
        state.report = {
            "run_id": "synthetic-request",
            "dry_run": bool(dry_run_only_override),
            "status": "pending",
            "summary": {},
            "diagnostics": {},
        }
        return state.report

    def load_plugins():
        mark("plugins")
        return (), state.plugin_error

    def routine_counts(signals):
        assert signals == ()
        mark("routine_counts")
        return {"synthetic_observation": 0}

    def acquire(*, blocking):
        assert blocking is False
        mark("acquire")
        return state.acquire

    def market_open(**_kwargs):
        mark("market")
        return state.market_open

    def unsupported(_profile):
        mark("unsupported")
        return state.unsupported

    def log_event(_context, name, **fields):
        mark("log:" + name)
        state.log_events.append((name, deepcopy(fields)))

    def run_core(**kwargs):
        mark("core")
        assert kwargs["strategy_plugin_signals"] == ()
        assert kwargs["strategy_plugin_error"] is state.plugin_error
        kwargs["notification_delivery_events"].extend(deepcopy(state.notification_events))
        return state.cycle_result

    def notification_run(report, callback, **kwargs):
        mark("notification_run")
        assert report is state.report
        assert callback is run_core
        return callback(**kwargs)

    def append_error(report, **fields):
        mark("append_error")
        report.setdefault("errors", []).append(deepcopy(fields))

    def finalize(report, **fields):
        mark("finalize:" + fields["status"])
        for key, value in fields.items():
            if key in {"summary", "diagnostics", "artifacts"}:
                report.setdefault(key, {}).update(deepcopy(value))
            else:
                report[key] = value

    def cycle_receipt(report, execution_summary, reconciliation_record, *, execution_failed):
        mark("cycle_receipt")
        state.receipt_inputs = (deepcopy(execution_summary), deepcopy(reconciliation_record), execution_failed)
        if state.attach_receipt:
            # Receipt adapters have separate contract tests. This opaque marker
            # only characterizes whether the handler repeats the fallback call.
            report["execution_receipt"] = {"synthetic_marker": True}

    def fallback_receipt(report):
        mark("fallback_receipt")
        report["execution_receipt"] = {"synthetic_fallback_marker": True}

    def persist(report, **kwargs):
        mark("persist")
        state.persisted.append((deepcopy(report), kwargs))
        if state.persist_error is not None:
            raise state.persist_error
        return _REPORT_URI

    def failure_notification(**kwargs):
        mark("failure_notification")
        state.failure_notifications.append(kwargs)

    monkeypatch.setattr(module, "build_request_log_context", build_context)
    monkeypatch.setattr(module, "build_execution_report", build_report)
    monkeypatch.setattr(module.signal_provenance, "reset", lambda: mark("provenance_reset"))
    monkeypatch.setattr(module, "load_strategy_plugin_signals", load_plugins)
    monkeypatch.setattr(module, "attach_strategy_plugin_report", lambda *_args, **_kwargs: mark("attach_plugins"))
    monkeypatch.setattr(unified_notifications, "routine_observation_counts_from_signals", routine_counts)
    monkeypatch.setattr(module, "STRATEGY_RUN_LOCK", SimpleNamespace(acquire=acquire, release=lambda: mark("release")))
    monkeypatch.setattr(module, "is_market_open_now", market_open)
    monkeypatch.setattr(module, "dca_execution_unsupported_reason", unsupported)
    monkeypatch.setattr(module, "log_runtime_event", log_event)
    monkeypatch.setattr(module, "publish_strategy_plugin_alerts", lambda *_args, **_kwargs: mark("plugin_alerts"))
    monkeypatch.setattr(module, "run_strategy_core", run_core)
    monkeypatch.setattr(module, "with_notification_run", notification_run)
    monkeypatch.setattr(module, "append_runtime_report_error", append_error)
    monkeypatch.setattr(module, "finalize_runtime_report", finalize)
    monkeypatch.setattr(module, "attach_cycle_execution_receipt", cycle_receipt)
    monkeypatch.setattr(module, "attach_terminal_fallback_execution_receipt", fallback_receipt)
    monkeypatch.setattr(module, "attach_signal_data_provenance", lambda _report: mark("provenance"))
    monkeypatch.setattr(module, "persist_execution_report", persist)
    monkeypatch.setattr(module, "_runtime_error_notification_message", lambda _exc: "synthetic failure")
    monkeypatch.setattr(module, "_publish_runtime_failure_notification", failure_notification)

    for function_name, event in (
        ("capture_cycle_result_evidence", "capture_execution"),
        ("build_signal_snapshot", "snapshot"),
        ("_has_signal_snapshot_details", "has_snapshot"),
        ("_build_cycle_report_summary", "summary"),
        ("_build_notification_delivery_log_for_report", "delivery_log"),
        ("_build_notification_delivery_summary", "delivery_summary"),
        ("attach_cycle_decision_receipt", "decision_receipt"),
    ):
        original = getattr(module, function_name)

        def wrapped(*args, _original=original, _event=event, **kwargs):
            mark(_event)
            return _original(*args, **kwargs)

        monkeypatch.setattr(module, function_name, wrapped)

    def run(*, override=None, persist_report=True, method="POST"):
        with decision_cycle(decision_time=_CLOCK) as cycle:
            state.cycle = cycle
            with module.app.test_request_context("/run", method=method):
                return module._handle_request(
                    dry_run_only_override=override,
                    response_body="synthetic dry-run response",
                    persist_report=persist_report,
                    report_collector=state.collector,
                )

    state.run = run
    return state


@pytest.mark.parametrize("override", [None, False, True])
@pytest.mark.parametrize("status,reason,expected_status", [
    ("executed", None, "ok"),
    ("error", None, "error"),
    ("failed", "synthetic_failure", "error"),
    ("failure", None, "error"),
    ("blocked", "no_equity", "error"),
    ("blocked", "pending_orders_detected:AAA", "ok"),
    ("blocked", "same_day_fills_detected:AAA", "ok"),
    ("blocked", "same_day_execution_locked:AAA", "ok"),
])
def test_request_report_status_response_and_finalizer_order(
    reporting_case, override, status, reason, expected_status,
):
    case = reporting_case
    case.cycle_result = replace(case.cycle_result, execution_summary={
        "execution_status": status, "no_op_reason": reason, "orders_submitted": [],
    })
    response = case.run(override=override)

    assert response == ("synthetic dry-run response" if override else "synthetic cycle result", 200)
    assert case.report["status"] == expected_status
    assert case.report["summary"]["execution_status"] == status
    assert case.report["summary"]["no_op_reason"] == reason
    assert case.collector == [case.report]
    assert case.cycle["report"] is case.report
    assert case.cycle["plugin_signals"] == ()
    assert case.report["decision_at"] == _CLOCK.isoformat()
    assert case.report["diagnostics"]["decision_consumption_receipt_status"] == "unavailable"
    assert ("plugin_alerts" in case.trace) is (override is None)
    assert case.trace[-4:] == ["release", "provenance", "decision_receipt", "persist"]
    assert case.trace.index("capture_execution") < case.trace.index("snapshot") < case.trace.index("summary")
    assert case.trace.index("delivery_log") < case.trace.index("delivery_summary") < case.trace.index("cycle_receipt")
    assert case.trace.index("cycle_receipt") < case.trace.index("finalize:" + expected_status)
    assert case.persisted == [(case.report, {} if override is None else {"dry_run_only_override": override})]
    assert case.receipt_inputs[2] is (expected_status == "error")


@pytest.mark.parametrize("supplied_snapshot", [None, {}, {"status": "provided"}])
@pytest.mark.parametrize("price_source", ["execution", "reconciliation", None])
def test_signal_snapshot_copy_fallback_and_source_precedence(reporting_case, supplied_snapshot, price_source):
    case = reporting_case
    metadata = {"strategy_profile": "synthetic-profile", "signal_snapshot": supplied_snapshot}
    execution = {"execution_status": "executed", "snapshot_price_fallback_used": price_source == "execution"}
    reconciliation = {"snapshot_price_fallback_used": price_source == "reconciliation"}
    if price_source == "execution":
        execution["price_source_mode"] = "execution-source"
        execution["snapshot_price_fallback_symbols"] = ["AAA"]
        reconciliation["price_source_mode"] = "reconciliation-source"
        reconciliation["snapshot_price_fallback_symbols"] = ["BBB"]
    elif price_source == "reconciliation":
        reconciliation["price_source_mode"] = "reconciliation-source"
        reconciliation["snapshot_price_fallback_symbols"] = ["BBB"]
    case.cycle_result = replace(case.cycle_result, signal_metadata=metadata,
                                execution_summary=execution, reconciliation_record=reconciliation)
    before = deepcopy((metadata, execution, reconciliation))

    assert case.run()[1] == 200
    assert (metadata, execution, reconciliation) == before
    assert ("snapshot" in case.trace) is (not supplied_snapshot)
    diagnostics = case.report["diagnostics"]
    if supplied_snapshot or price_source:
        snapshot = diagnostics["signal_snapshot"]
        if supplied_snapshot:
            assert snapshot["status"] == "provided"
        else:
            assert snapshot["strategy_profile"] == "synthetic-profile"
            assert snapshot["generated_at"] == _CLOCK.isoformat()
        if price_source:
            assert snapshot["latest_price_source"] == price_source + "-source"
            assert snapshot["data_freshness_warning"] == "snapshot_price_fallback_used"
    else:
        assert "signal_snapshot" not in diagnostics
    assert diagnostics["snapshot_price_fallback_symbols"] == (
        ["AAA"] if price_source == "execution" else ["BBB"] if price_source == "reconciliation" else []
    )


@pytest.mark.parametrize("brake_status", ["Submitted", "unknown", None, "no_position"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_account_brake_capture_and_unknown_counts_remain_distinct(reporting_case, brake_status, dry_run):
    case = reporting_case
    metadata = {} if brake_status is None else {"account_brake_observation": {"submission_status": brake_status}}
    case.cycle_result = replace(case.cycle_result, signal_metadata=metadata,
                               execution_summary={"execution_status": "executed", "orders_submitted": [{"symbol": "AAA"}]})

    assert case.run(override=True if dry_run else None)[1] == 200
    summary = case.report["summary"]
    observed_brake = brake_status not in (None, "no_position")
    assert ("capture_execution" in case.trace) is not observed_brake
    assert summary["orders_submitted_count"] == (None if observed_brake else 1)
    assert summary["orders_previewed_count"] == (None if observed_brake and dry_run else 1 if dry_run else 0)
    assert summary["dry_run_order_preview_available"] == (None if observed_brake and dry_run else dry_run)
    if observed_brake:
        assert summary["account_brake_submission_status"] == brake_status
        assert summary["orders_submitted_coverage"] == "strategy_only_account_brake_details_unavailable"
        assert "execution_order_records" not in case.cycle
    else:
        assert case.cycle["execution_order_records"] is None
        assert case.cycle["execution_fill_records"] is None


@pytest.mark.parametrize("events", [[], [{"sink": "telegram", "delivery_status": "sent", "transport_acknowledged": True}],
                                   [{"sink": "telegram", "delivery_status": "failed", "transport_acknowledged": False}]])
@pytest.mark.parametrize("dry_run", [False, True])
def test_notification_receipts_keep_delivery_state(reporting_case, events, dry_run):
    case = reporting_case
    case.notification_events = events
    case.cycle_result = replace(case.cycle_result, execution_summary={
        "execution_status": "dry_run" if dry_run else "executed", "orders_submitted": [{"symbol": "AAA"}],
    })

    assert case.run(override=True if dry_run else None)[1] == 200
    summary = case.report["summary"]
    assert ("notification_delivery_summary" in summary) is bool(events)
    sent = bool(events and events[0]["delivery_status"] == "sent")
    assert ("notification_delivery_log" in summary) is (dry_run and sent)
    if events:
        delivery = summary["notification_delivery_summary"]
        assert delivery["sent_count"] == int(sent)
        assert delivery["failed_count"] == int(not sent)
        assert delivery["all_acknowledged"] is sent


@pytest.mark.parametrize("stage", ["snapshot", "summary", "cycle_receipt", "log:strategy_cycle_completed"])
@pytest.mark.parametrize("exception_name,status,category", [
    ("RuntimeError", 500, None),
    ("IBKRGatewayUnavailableError", 503, "ibkr_gateway_unavailable"),
    ("IBKRTradingPermissionError", 503, "ibkr_trading_permission"),
    ("IBKRPortfolioSnapshotUnavailableError", 503, "ibkr_portfolio_snapshot"),
    ("RuntimeDeadlineExceeded", None, "strategy_deadline"),
])
def test_report_stage_failures_preserve_classification_identity_and_cleanup(
    reporting_case, stage, exception_name, status, category,
):
    case = reporting_case
    exception_type = RuntimeError if exception_name == "RuntimeError" else getattr(case.module, exception_name)
    error = exception_type("synthetic reporting failure")
    case.error = error
    case.fail_stage = stage
    if status is None:
        with pytest.raises(exception_type) as raised:
            case.run()
        assert raised.value is error
        assert not case.persisted
        assert not case.failure_notifications
    else:
        assert case.run() == ("Error", status)
        assert case.failure_notifications[0]["exc"] is error
        assert len(case.persisted) == 1
    assert case.report["status"] == "error"
    assert case.report["errors"][-1]["error_type"] == exception_name
    if category:
        assert case.report["diagnostics"]["failure_category"] == category
    assert case.trace.count("release") == 1
    assert case.trace.index("release") < case.trace.index("provenance") < case.trace.index("decision_receipt")


@pytest.mark.parametrize("field,value", [("reconciliation_record", 1), ("signal_metadata", 1),
                                         ("signal_metadata", {"signal_snapshot": 1})])
def test_malformed_reporting_input_keeps_error_response(reporting_case, field, value):
    case = reporting_case
    case.cycle_result = replace(case.cycle_result, **{field: value})
    assert case.run() == ("Error", 500)
    assert case.report["status"] == "error"
    assert case.report["errors"][-1]["error_type"] in {"TypeError", "AttributeError"}
    assert case.trace[-5:] == ["release", "provenance", "fallback_receipt", "decision_receipt", "persist"]


@pytest.mark.parametrize("gate,response", [("overlap", "Already Running"), ("market", "Market Closed"),
                                         ("unsupported", "Unsupported Strategy")])
def test_early_exit_does_not_enter_cycle_reporting(reporting_case, gate, response):
    case = reporting_case
    case.acquire = gate != "overlap"
    case.market_open = gate != "market"
    case.unsupported = "synthetic-unsupported" if gate == "unsupported" else None
    assert case.run() == (response, 200)
    assert "summary" not in case.trace
    assert "core" not in case.trace
    assert ("release" in case.trace) is (gate != "overlap")
    assert case.trace[-4:] == ["provenance", "fallback_receipt", "decision_receipt", "persist"]


def test_missing_cycle_receipt_keeps_fallback_after_provenance(reporting_case):
    case = reporting_case
    case.attach_receipt = False
    assert case.run(persist_report=False) == ("synthetic cycle result", 200)
    assert case.trace[-4:] == ["release", "provenance", "fallback_receipt", "decision_receipt"]
    assert not case.persisted


def test_plugin_error_retains_report_path_without_routine_count_observation(reporting_case):
    case = reporting_case
    case.plugin_error = "synthetic plugin failure"
    assert case.run()[1] == 200
    assert "routine_counts" not in case.trace
    assert case.cycle["plugin_error"] == case.plugin_error
    assert case.report["summary"]["execution_status"] == "executed"
