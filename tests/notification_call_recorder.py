"""Call-recorder fixtures captured from main 2f16cb9 with all flags unset."""
import datetime as dt
import json


from application import rebalance_service as cycle
from application.runtime_dependencies import IBKRRebalanceConfig
from notifications.telegram import build_translator
from scripts import daily_dry_run_digest as drill
from scripts import execution_report_heartbeat as heartbeat


def capture_cycle(monkeypatch, case, locale, *, metadata_overrides=None):
    calls = []
    class Broker:
        def isConnected(self):
            calls.append(["connected"])
            return True
        def disconnect(self):
            calls.append(["disconnect"])
    def connect():
        calls.append(["connect"])
        return Broker()
    def portfolio(_ib):
        calls.append(["portfolio"])
        return {}, {"equity": 1000.0, "buying_power": 500.0}
    metadata = {"strategy_profile": "synthetic_strategy", "status_icon": "", "allocation": {
        "target_mode": "weight", "targets": {"AAA": 0.5}, "strategy_symbols": ["AAA"],
    }}
    metadata.update(metadata_overrides or {})
    def signals(_ib, holdings):
        calls.append(["signals", sorted(holdings)])
        return ({"AAA": 0.5} if case != "heartbeat" else None), "", False, "", metadata
    def execute(_ib, weights, positions, values, **kwargs):
        calls.append(["execute", weights, positions, values,
                      {k: v for k, v in kwargs.items() if not callable(v)}])
        summary = {"execution_status": "completed", "orders_submitted": [], "orders_filled": []}
        if case == "filled":
            order = {"symbol": "AAA", "side": "buy", "quantity": 1, "status": "filled"}
            summary.update(orders_submitted=[order], orders_filled=[order])
        if case == "pending":
            order = {"symbol": "AAA", "side": "buy", "quantity": 1, "status": "pending"}
            summary.update(execution_status="pending_reconciliation", orders_submitted=[order], orders_pending=[order])
        if case == "error":
            summary.update(execution_status="error", errors=["synthetic_error"])
        return [], summary
    def persist(record, *, output_path=None):
        calls.append(["persist", record.get("execution_status"), record.get("orders_submitted")])
        return "synthetic-record.json"
    original_snapshot = cycle.build_signal_snapshot
    def fixed_snapshot(*args, **kwargs):
        return {**original_snapshot(*args, **kwargs), "generated_at": "2026-10-16T20:00:00+00:00"}
    monkeypatch.setattr(cycle, "build_signal_snapshot", fixed_snapshot)
    monkeypatch.setattr(cycle, "write_reconciliation_record", persist)
    monkeypatch.setattr(cycle, "_record_platform_execution_telemetry", lambda *a: calls.append(["telemetry"]))
    result = cycle.run_strategy_core(
        connect_ib=connect, get_current_portfolio=portfolio, compute_signals=signals,
        execute_rebalance=execute, send_tg_message=lambda message: calls.append(["send", message]) or True,
        config=IBKRRebalanceConfig(translator=build_translator(locale), separator="---",
                                   strategy_display_name="Example", notify_no_trade_cycles=True),
    )
    return json.loads(json.dumps({"calls": calls, "result": result.result}, default=lambda value: "<timestamp>" if isinstance(value, dt.datetime) else str(value)))


def capture_heartbeat(monkeypatch, case, locale):
    calls = []
    for key in list(__import__("os").environ):
        if key.startswith("RUNTIME_HEARTBEAT_") or key in {
            "RUNTIME_TARGET_ENABLED", "RUNTIME_TARGET_JSON", "CLOUD_RUN_SERVICE_TARGETS_JSON",
            "CLOUD_RUN_SERVICES", "CLOUD_RUN_SERVICE", "EXECUTION_REPORT_GCS_URI",
        }:
            monkeypatch.delenv(key, raising=False)
    for key in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NOTIFY_LANG", locale)
    monkeypatch.setenv("RUNTIME_HEARTBEAT_NAME", "Example")
    monkeypatch.setenv("RUNTIME_HEARTBEAT_NOTIFY_ON_SUCCESS", "true")
    monkeypatch.setattr(heartbeat, "load_runtime_targets", lambda env: [])
    monkeypatch.setattr(heartbeat, "_hydrate_runtime_target_schedules", lambda targets, **kw: targets)
    monkeypatch.setattr(heartbeat, "_runtime_target_scheduler_skip_reason", lambda *a: None)
    monkeypatch.setattr(heartbeat, "_resolve_required_services", lambda **kw: ([], None, True))
    monkeypatch.setattr(heartbeat, "_report_globs", lambda *a: ["synthetic-reports"])
    monkeypatch.setattr(heartbeat, "_list_gcs_objects", lambda *a, **kw: [] if case == "missing" else [
        {"url": "gs://synthetic/reports/a.json", "metadata": {"updated": "2026-10-16T20:00:00Z"}},
    ])
    monkeypatch.setattr(heartbeat, "_cat_gcs_json", lambda *a, **kw: {"status": "ok", "execution_backend": "gateway"})
    monkeypatch.setattr(heartbeat, "_is_quiet_report", lambda *a, **kw: True, raising=False)
    monkeypatch.setattr(heartbeat, "_send_telegram", lambda message, **kw: calls.append([message, kw]) or True)
    result = heartbeat.main(now=dt.datetime(2026, 10, 16, 22, 20, tzinfo=dt.timezone.utc))
    return {"sends": calls, "exit": result}


def capture_drill(monkeypatch, case, locale):
    calls = []
    monkeypatch.delenv("DRILL_DIGEST_PREVIEW", raising=False)
    monkeypatch.setenv("NOTIFY_LANG", locale)
    target = drill.DrillTarget("Example", "synthetic_strategy", "synthetic_scope", "synthetic-service", "0 1 * * *", "UTC")
    monkeypatch.setattr(drill, "_targets", lambda: [target])
    monkeypatch.setattr(drill, "_target_status", lambda *a: (dt.date(2026, 10, 16), "synthetic status"))
    monkeypatch.setattr(drill, "_send_telegram", lambda message, **kw: calls.append([message, kw]) or True)
    result = drill.main(now=dt.datetime(2026, 10, 16, 22, 20, tzinfo=dt.timezone.utc))
    return {"sends": calls, "exit": result}


def capture_attention(monkeypatch, case, locale):
    from application.account_new_risk_gate_support import maybe_publish_attention_for_admission, reset_attention_sent_keys_for_tests
    from quant_platform_kit.risk.account_new_risk_gate import NewRiskAdmissionResult, NewRiskDisposition
    monkeypatch.setenv("NOTIFY_LANG", locale)
    monkeypatch.setenv("QSL_NOTIFY_LANG", locale)
    reset_attention_sent_keys_for_tests()
    admission = NewRiskAdmissionResult(
        NewRiskDisposition.NEW_RISK_PROHIBITED if case == "blocked" else NewRiskDisposition.ALLOW_NEW_RISK,
        ("EQUITY_UNKNOWN_FAIL_CLOSED",) if case == "blocked" else (),
    )
    sends = []
    def send(**kwargs):
        sends.append(kwargs)
        return True
    outcomes = [maybe_publish_attention_for_admission(admission,
        portfolio={"strategy_profile": "soxl_soxx_trend_income", "account_id": "synthetic-account"},
        telegram_sender=send, log_message=lambda *a, **kw: None) for _ in range(2)]
    return {"sends": sends, "outcomes": outcomes}


def capture_runtime_failure(monkeypatch, case, locale):
    from tests.conftest import strategy_module_factory
    module = strategy_module_factory.__wrapped__(monkeypatch)(NOTIFY_LANG=locale)
    monkeypatch.setattr(module, "TG_TOKEN", "synthetic-token")
    monkeypatch.setattr(module, "TG_CHAT_ID", "synthetic-chat")
    sends = []
    def send(message, **kwargs):
        sends.append([message, kwargs["chat_id"], kwargs["token"] == "synthetic-token", kwargs["requests_module"] is module.requests])
        return True
    monkeypatch.setattr(module, "send_telegram_message", send)
    return {"sends": sends, "result": module._notify_runtime_error(RuntimeError("synthetic_failure"))}


SCENARIOS = [("cycle", c) for c in ("heartbeat", "no_order", "filled", "pending", "error")] + [
    ("heartbeat", "healthy"), ("heartbeat", "missing"), ("drill", "healthy"), ("drill", "alert"),
    ("attention", "blocked"), ("attention", "allowed"), ("runtime", "error"),
]
CAPTURE = {"cycle": capture_cycle, "heartbeat": capture_heartbeat, "drill": capture_drill,
           "attention": capture_attention, "runtime": capture_runtime_failure}
