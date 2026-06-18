import types

from application.cycle_result import StrategyCycleResult


def route_methods(strategy_module):
    return {
        rule.rule: sorted(rule.methods - {"HEAD", "OPTIONS"})
        for rule in strategy_module.app.url_map.iter_rules()
    }


def test_cloud_run_route_contracts_are_registered(strategy_module):
    assert route_methods(strategy_module) == {
        "/": ["GET", "POST"],
        "/precheck": ["GET", "POST"],
        "/probe": ["GET", "POST"],
        "/health": ["GET"],
        "/static/<path:filename>": ["GET"],
    }


def test_health_route_returns_ok(strategy_module):
    with strategy_module.app.test_request_context("/health", method="GET"):
        body, status = strategy_module.health()

    assert status == 200
    assert body == "OK"


def test_handle_request_get_returns_safe_message(strategy_module, monkeypatch):
    def fail_if_called():
        raise AssertionError("GET should not execute strategy")

    monkeypatch.setattr(strategy_module, "run_strategy_core", fail_if_called)

    with strategy_module.app.test_request_context("/", method="GET"):
        body, status = strategy_module.handle_request()

    assert status == 200
    assert body == "OK - use POST to execute strategy"


def test_handle_request_post_executes_on_market_day(strategy_module, monkeypatch):
    observed = {"called": False}

    def fake_run_strategy_core(**_kwargs):
        observed["called"] = True
        return "OK - executed"

    monkeypatch.setattr(strategy_module, "is_market_open_today", lambda **_kwargs: True)
    monkeypatch.setattr(strategy_module, "run_strategy_core", fake_run_strategy_core)

    with strategy_module.app.test_request_context("/", method="POST"):
        body, status = strategy_module.handle_request()

    assert status == 200
    assert body == "OK - executed"
    assert observed["called"] is True


def test_handle_request_sends_escalated_strategy_plugin_alert(strategy_module, monkeypatch):
    signal = types.SimpleNamespace(
        plugin="crisis_response_shadow",
        effective_mode="shadow",
        canonical_route="true_crisis",
        suggested_action="defend",
        would_trade_if_enabled=True,
        as_of="2026-05-24",
    )
    observed = {"alerts": []}

    monkeypatch.setattr(strategy_module, "is_market_open_today", lambda **_kwargs: True)
    monkeypatch.setattr(strategy_module, "load_strategy_plugin_signals", lambda: ((signal,), None))
    monkeypatch.setattr(strategy_module, "attach_strategy_plugin_report", lambda *args, **kwargs: None)

    def fake_dispatch(signals, **kwargs):
        observed["alerts"].append((tuple(signals), kwargs))
        return types.SimpleNamespace(attach_to_report=lambda _report: None)

    monkeypatch.setattr(strategy_module, "dispatch_strategy_plugin_alerts", fake_dispatch)
    monkeypatch.setattr(strategy_module, "run_strategy_core", lambda **_kwargs: "OK - executed")

    with strategy_module.app.test_request_context("/", method="POST"):
        body, status = strategy_module.handle_request()

    assert status == 200
    assert body == "OK - executed"
    assert len(observed["alerts"]) == 1
    assert observed["alerts"][0][0] == (signal,)
    assert "ibkr" in observed["alerts"][0][1]["context_label"]
    assert observed["alerts"][0][1]["notification_settings"] is strategy_module.RUNTIME_SETTINGS
    assert observed["alerts"][0][1]["state_settings"] is not None


def test_handle_precheck_post_uses_dry_run_override(strategy_module, monkeypatch):
    observed = {"called": False, "dry_run_only_override": None, "events": []}

    monkeypatch.setattr(strategy_module, "build_request_log_context", lambda: types.SimpleNamespace(run_id="run-001"))
    monkeypatch.setattr(strategy_module, "build_execution_report", lambda log_context, **_kwargs: {"status": "pending"})
    monkeypatch.setattr(strategy_module, "persist_execution_report", lambda report, **_kwargs: observed.setdefault("report", dict(report)) or "/tmp/runtime-report.json")
    monkeypatch.setattr(strategy_module, "emit_runtime_log", lambda context, event, **fields: observed["events"].append((event, fields)))
    monkeypatch.setattr(strategy_module, "is_market_open_today", lambda **_kwargs: True)
    monkeypatch.setattr(strategy_module, "load_strategy_plugin_signals", lambda: ((), None))
    monkeypatch.setattr(strategy_module, "attach_strategy_plugin_report", lambda *args, **kwargs: None)

    def fake_run_strategy_core(**kwargs):
        observed["called"] = True
        observed["dry_run_only_override"] = kwargs.get("dry_run_only_override")
        return "OK - precheck"

    monkeypatch.setattr(strategy_module, "run_strategy_core", fake_run_strategy_core)

    with strategy_module.app.test_request_context("/precheck", method="POST"):
        body, status = strategy_module.handle_precheck()

    assert status == 200
    assert body == "Precheck OK"
    assert observed["called"] is True
    assert observed["dry_run_only_override"] is True
    assert observed["events"][0][0] == "strategy_cycle_received"
    assert observed["events"][0][1]["execution_window"] == "precheck"


def test_precheck_composer_wires_dry_run_override_to_order_execution(strategy_module, monkeypatch):
    observed = {}

    class FakeBrokerAdapters:
        def execute_rebalance(self, *_args, **_kwargs):
            observed["called"] = True
            return (), {"mode": "dry_run"}

    def fake_build_broker_adapters(*, dry_run_only_override=None):
        observed["dry_run_only_override"] = dry_run_only_override
        return FakeBrokerAdapters()

    monkeypatch.setattr(strategy_module, "build_broker_adapters", fake_build_broker_adapters)

    runtime = strategy_module.build_composer(dry_run_only_override=True).build_rebalance_runtime()
    runtime.execute_rebalance("ib", {}, {}, {}, strategy_symbols=(), signal_metadata={})

    assert observed["called"] is True
    assert observed["dry_run_only_override"] is True


def test_handle_precheck_ignores_paper_liquidate_only(strategy_module, monkeypatch):
    observed = {"called": False}

    monkeypatch.setattr(strategy_module, "build_request_log_context", lambda: types.SimpleNamespace(run_id="run-001"))
    monkeypatch.setattr(strategy_module, "build_execution_report", lambda log_context, **_kwargs: {"status": "pending"})
    monkeypatch.setattr(strategy_module, "persist_execution_report", lambda report, **_kwargs: "/tmp/runtime-report.json")
    monkeypatch.setattr(strategy_module, "emit_runtime_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(strategy_module, "is_market_open_today", lambda **_kwargs: True)
    monkeypatch.setattr(strategy_module, "load_strategy_plugin_signals", lambda: ((), None))
    monkeypatch.setattr(strategy_module, "attach_strategy_plugin_report", lambda *args, **kwargs: None)
    monkeypatch.setattr(strategy_module, "PAPER_LIQUIDATE_ONLY", True)

    def fake_run_strategy_core(**kwargs):
        observed["called"] = True
        assert kwargs.get("dry_run_only_override") is True
        return "OK - precheck"

    monkeypatch.setattr(strategy_module, "run_strategy_core", fake_run_strategy_core)

    with strategy_module.app.test_request_context("/precheck", method="POST"):
        body, status = strategy_module.handle_precheck()

    assert status == 200
    assert body == "Precheck OK"
    assert observed["called"] is True


def test_handle_precheck_get_does_not_execute(strategy_module, monkeypatch):
    observed = {"called": False}

    monkeypatch.setattr(strategy_module, "run_strategy_core", lambda **_kwargs: observed.__setitem__("called", True))

    with strategy_module.app.test_request_context("/precheck", method="GET"):
        body, status = strategy_module.handle_precheck()

    assert status == 200
    assert body == "Precheck OK - use POST to run precheck"
    assert observed["called"] is False


def test_handle_probe_checks_account_snapshot_without_success_notification(strategy_module, monkeypatch):
    observed = {"events": [], "disconnects": 0, "notifications": []}

    class FakeIB:
        def disconnect(self):
            observed["disconnects"] += 1

    snapshot = types.SimpleNamespace(
        buying_power=123.0,
        total_equity=456.0,
        positions=(types.SimpleNamespace(symbol="SOXL"),),
    )

    monkeypatch.setattr(strategy_module, "build_request_log_context", lambda: types.SimpleNamespace(run_id="run-001"))
    monkeypatch.setattr(strategy_module, "build_execution_report", lambda log_context, **_kwargs: {"status": "pending"})
    monkeypatch.setattr(
        strategy_module,
        "persist_execution_report",
        lambda report, **_kwargs: observed.setdefault("report", dict(report)) or "/tmp/runtime-report.json",
    )
    monkeypatch.setattr(
        strategy_module,
        "log_runtime_event",
        lambda context, event, **fields: observed["events"].append((event, fields)),
    )
    monkeypatch.setattr(strategy_module, "load_strategy_plugin_signals", lambda: ((), None))
    monkeypatch.setattr(strategy_module, "attach_strategy_plugin_report", lambda *args, **kwargs: None)
    monkeypatch.setattr(strategy_module, "connect_ib", lambda: FakeIB())
    monkeypatch.setattr(strategy_module, "build_portfolio_snapshot", lambda ib: snapshot)
    monkeypatch.setattr(
        strategy_module,
        "publish_notification",
        lambda **_kwargs: observed["notifications"].append(_kwargs),
    )

    with strategy_module.app.test_request_context("/probe", method="POST"):
        body, status = strategy_module.handle_probe()

    assert status == 200
    assert body == "Probe OK"
    assert [event for event, _fields in observed["events"]] == [
        "health_probe_received",
        "health_probe_completed",
    ]
    assert observed["report"]["status"] == "ok"
    assert observed["report"]["summary"]["buying_power"] == 123.0
    assert observed["report"]["summary"]["total_equity"] == 456.0
    assert observed["report"]["summary"]["positions_count"] == 1
    assert observed["disconnects"] == 1
    assert observed["notifications"] == []


def test_handle_probe_connect_timeout_sends_concise_connection_notification(strategy_module, monkeypatch):
    observed = {"events": [], "notifications": []}
    timeout_message = (
        "IBKR API handshake timed out after TCP preflight succeeded "
        "for 172.16.159.2:4001 clientId=231"
    )

    monkeypatch.setattr(strategy_module, "build_request_log_context", lambda: types.SimpleNamespace(run_id="run-001"))
    monkeypatch.setattr(strategy_module, "build_execution_report", lambda log_context, **_kwargs: {"status": "pending"})
    monkeypatch.setattr(
        strategy_module,
        "persist_execution_report",
        lambda report, **_kwargs: observed.setdefault("report", dict(report)) or "/tmp/runtime-report.json",
    )
    monkeypatch.setattr(
        strategy_module,
        "log_runtime_event",
        lambda context, event, **fields: observed["events"].append((event, fields)),
    )
    monkeypatch.setattr(strategy_module, "load_strategy_plugin_signals", lambda: ((), None))
    monkeypatch.setattr(strategy_module, "attach_strategy_plugin_report", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        strategy_module,
        "connect_ib",
        lambda: (_ for _ in ()).throw(TimeoutError(timeout_message)),
    )
    monkeypatch.setattr(
        strategy_module,
        "publish_notification",
        lambda **kwargs: observed["notifications"].append(kwargs),
    )

    with strategy_module.app.test_request_context("/probe", method="POST"):
        body, status = strategy_module.handle_probe()

    assert status == 500
    assert body == "Error"
    assert observed["report"]["status"] == "error"
    assert observed["report"]["errors"][0]["stage"] == "health_probe"
    assert observed["report"]["errors"][0]["failure_category"] == "ibkr_connection"
    assert [event for event, _fields in observed["events"]] == [
        "health_probe_received",
        "health_probe_failed",
    ]
    assert observed["events"][1][1]["failure_category"] == "ibkr_connection"
    assert len(observed["notifications"]) == 1
    detailed_text = observed["notifications"][0]["detailed_text"]
    assert "IBKR" in detailed_text
    assert timeout_message in detailed_text
    assert "Traceback" not in detailed_text


def test_handle_probe_failure_sends_notification(strategy_module, monkeypatch):
    observed = {"events": [], "notifications": []}

    monkeypatch.setattr(strategy_module, "build_request_log_context", lambda: types.SimpleNamespace(run_id="run-001"))
    monkeypatch.setattr(strategy_module, "build_execution_report", lambda log_context, **_kwargs: {"status": "pending"})
    monkeypatch.setattr(strategy_module, "persist_execution_report", lambda report, **_kwargs: observed.setdefault("report", dict(report)) or "/tmp/runtime-report.json")
    monkeypatch.setattr(
        strategy_module,
        "log_runtime_event",
        lambda context, event, **fields: observed["events"].append((event, fields)),
    )
    monkeypatch.setattr(strategy_module, "load_strategy_plugin_signals", lambda: ((), None))
    monkeypatch.setattr(strategy_module, "attach_strategy_plugin_report", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        strategy_module,
        "connect_ib",
        lambda: (_ for _ in ()).throw(RuntimeError("probe failed")),
    )
    monkeypatch.setattr(
        strategy_module,
        "publish_notification",
        lambda **kwargs: observed["notifications"].append(kwargs),
    )

    with strategy_module.app.test_request_context("/probe", method="POST"):
        body, status = strategy_module.handle_probe()

    assert status == 500
    assert body == "Error"
    assert observed["report"]["status"] == "error"
    assert observed["report"]["errors"][0]["stage"] == "health_probe"
    assert [event for event, _fields in observed["events"]] == [
        "health_probe_received",
        "health_probe_failed",
    ]
    assert len(observed["notifications"]) == 1
    assert "probe failed" in observed["notifications"][0]["detailed_text"]


def test_build_extra_notification_lines_includes_account_id(strategy_module):
    lines = strategy_module.build_extra_notification_lines(("plugin-line",))
    assert any("U1234567" in line for line in lines)
    assert all("plugin-line" not in line for line in lines)


def test_build_extra_notification_lines_includes_plugin_error(strategy_module_factory):
    strategy_module = strategy_module_factory(NOTIFY_LANG="zh")
    lines = strategy_module.build_extra_notification_lines(
        (),
        strategy_plugin_error="ValueError: bad config",
    )
    assert any("插件信号未加载" in line for line in lines)
    assert any("插件配置校验失败" in line for line in lines)


def test_handle_request_skips_overlapping_post(strategy_module, monkeypatch):
    observed = {}

    def fail_if_called():
        raise AssertionError("overlapping request should not execute strategy")

    monkeypatch.setattr(strategy_module, "run_strategy_core", fail_if_called)
    monkeypatch.setattr(
        strategy_module,
        "persist_execution_report",
        lambda report: observed.setdefault("report", dict(report)) or "/tmp/runtime-report.json",
    )

    strategy_module.STRATEGY_RUN_LOCK.acquire()
    try:
        with strategy_module.app.test_request_context("/", method="POST"):
            body, status = strategy_module.handle_request()
    finally:
        strategy_module.STRATEGY_RUN_LOCK.release()

    assert status == 200
    assert body == "Already Running"
    assert observed["report"]["status"] == "skipped"
    assert observed["report"]["diagnostics"]["skip_reason"] == "already_running"


def test_handle_request_emits_structured_runtime_events(strategy_module, monkeypatch):
    observed = []

    monkeypatch.setattr(strategy_module, "build_run_id", lambda: "run-001")
    monkeypatch.setattr(
        strategy_module,
        "emit_runtime_log",
        lambda context, event, **fields: observed.append((context.run_id, event, fields)),
    )
    monkeypatch.setattr(strategy_module, "is_market_open_today", lambda **_kwargs: True)
    monkeypatch.setattr(strategy_module, "run_strategy_core", lambda **_kwargs: "OK - executed")

    with strategy_module.app.test_request_context(
        "/",
        method="POST",
        headers={"X-Cloud-Trace-Context": "trace-123/1;o=1"},
    ):
        body, status = strategy_module.handle_request()

    assert status == 200
    assert body == "OK - executed"
    assert [event for _run_id, event, _fields in observed] == [
        "strategy_cycle_received",
        "strategy_cycle_started",
        "strategy_cycle_completed",
    ]
    assert all(run_id == "run-001" for run_id, _event, _fields in observed)
    assert observed[0][2]["http_method"] == "POST"


def test_handle_request_persists_machine_readable_report(strategy_module, monkeypatch):
    observed = {}

    monkeypatch.setattr(strategy_module, "build_run_id", lambda: "run-001")
    monkeypatch.setattr(strategy_module, "is_market_open_today", lambda **_kwargs: True)
    monkeypatch.setattr(strategy_module, "run_strategy_core", lambda **_kwargs: "OK - executed")
    monkeypatch.setattr(
        strategy_module,
        "persist_execution_report",
        lambda report: observed.setdefault("report", dict(report)) or "/tmp/runtime-report.json",
    )

    with strategy_module.app.test_request_context("/", method="POST"):
        body, status = strategy_module.handle_request()

    assert status == 200
    assert body == "OK - executed"
    assert observed["report"]["status"] == "ok"
    assert observed["report"]["strategy_profile"] == strategy_module.STRATEGY_PROFILE
    assert observed["report"]["summary"]["strategy_display_name"] == strategy_module.STRATEGY_DISPLAY_NAME
    assert observed["report"]["summary"]["strategy_display_name_localized"] == strategy_module.strategy_display_name
    assert observed["report"]["run_source"] == "cloud_run"
    assert observed["report"]["account_scope"] == strategy_module.ACCOUNT_GROUP
    assert observed["report"]["summary"]["signal_source"] == strategy_module.STRATEGY_SIGNAL_SOURCE
    assert observed["report"]["summary"]["execution_timing_contract"] == "next_trading_day"
    assert observed["report"]["summary"]["signal_date"]
    assert observed["report"]["summary"]["effective_date"]


def test_execution_report_prefers_configured_managed_symbols_without_ranking_pool(strategy_module_factory):
    module = strategy_module_factory(STRATEGY_PROFILE="soxl_soxx_trend_income")
    report = module.build_execution_report(module.RUNTIME_LOG_CONTEXT.with_run("run-001"))

    assert report["summary"]["managed_symbols"] == [
        "SOXL",
        "SOXX",
        "BOXX",
        "SCHD",
        "DGRO",
        "SGOV",
        "SPYI",
        "QQQI",
    ]
    assert report["summary"]["safe_haven"] == "BIL"
    assert report["summary"]["execution_timing_contract"] == "next_trading_day"


def test_handle_request_enriches_runtime_report_with_cycle_details(strategy_module, monkeypatch):
    observed = {}

    monkeypatch.setattr(strategy_module, "build_run_id", lambda: "run-001")
    monkeypatch.setattr(strategy_module, "is_market_open_today", lambda **_kwargs: True)

    def fake_run_strategy_core(**_kwargs):
        return StrategyCycleResult(
            result="OK - executed",
            execution_summary={
                "execution_status": "executed",
                "orders_submitted": [{"symbol": "AAA"}],
                "orders_skipped": [],
                "price_source_mode": "mixed_market_quote_snapshot_close",
                "snapshot_price_fallback_used": True,
                "snapshot_price_fallback_count": 1,
                "snapshot_price_fallback_symbols": ["AAA"],
            },
            reconciliation_record_path="/tmp/reconciliation.json",
        )

    monkeypatch.setattr(strategy_module, "run_strategy_core", fake_run_strategy_core)
    monkeypatch.setattr(
        strategy_module,
        "persist_execution_report",
        lambda report: observed.setdefault("report", dict(report)) or "/tmp/runtime-report.json",
    )

    with strategy_module.app.test_request_context("/", method="POST"):
        body, status = strategy_module.handle_request()

    assert status == 200
    assert body == "OK - executed"
    assert observed["report"]["summary"]["execution_status"] == "executed"
    assert observed["report"]["summary"]["orders_submitted_count"] == 1
    assert observed["report"]["summary"]["orders_previewed_count"] == 0
    assert observed["report"]["summary"]["dry_run_order_preview_available"] is False
    assert observed["report"]["summary"]["snapshot_price_fallback_used"] is True
    assert observed["report"]["summary"]["snapshot_price_fallback_count"] == 1
    assert observed["report"]["diagnostics"]["price_source_mode"] == "mixed_market_quote_snapshot_close"
    assert observed["report"]["diagnostics"]["snapshot_price_fallback_symbols"] == ["AAA"]
    assert observed["report"]["artifacts"]["reconciliation_record_path"] == "/tmp/reconciliation.json"


def test_cycle_report_summary_counts_dry_run_order_previews(strategy_module):
    cycle_result = StrategyCycleResult(result="Precheck OK")
    execution_summary = {
        "execution_status": "dry_run",
        "orders_submitted": [{"symbol": "AAA"}, {"symbol": "BBB"}],
        "orders_skipped": [{"symbol": "CCC", "reason": "min_notional"}],
        "quote_snapshot": {"quotes": [{"symbol": "AAA", "last_price": 100.0}]},
    }

    summary = strategy_module._build_cycle_report_summary(
        cycle_result,
        execution_summary,
        {},
        dry_run=True,
    )

    assert summary["orders_submitted_count"] == 2
    assert summary["orders_previewed_count"] == 2
    assert summary["orders_skipped_count"] == 1
    assert summary["dry_run_order_preview_available"] is True
    assert summary["quote_snapshot"]["quotes"][0]["symbol"] == "AAA"


def test_notification_delivery_log_summary_records_sent_dry_run_without_raw_text(strategy_module):
    payload = strategy_module._build_notification_delivery_log_for_report(
        platform="interactive_brokers",
        strategy_profile="hk_low_vol_dividend_quality_snapshot",
        run_id="run-001",
        dry_run=True,
        orders_previewed_count=2,
        delivery_events=[
            {
                "sink": "telegram",
                "delivery_status": "sent",
                "compact_text_sha256": "a" * 64,
                "compact_text_length": 42,
            }
        ],
    )

    assert payload["notification_schema_version"] == "hk_live_enablement_notification.v1"
    assert payload["notification_event_type"] == "hk_snapshot_live_enablement_dry_run"
    assert payload["notification_correlation_id"] == "run-001"
    assert payload["locales"] == ["en", "zh-Hans"]
    assert payload["profile"] == "hk_low_vol_dividend_quality_snapshot"
    assert payload["platform"] == "interactive_brokers"
    assert payload["orders_previewed"] == 2
    assert payload["notification_redacts_sensitive_fields"] is True
    assert "compact_text" not in payload["delivery_events"][0]


def test_notification_delivery_log_summary_stays_empty_without_sent_event(strategy_module):
    payload = strategy_module._build_notification_delivery_log_for_report(
        platform="interactive_brokers",
        strategy_profile="hk_low_vol_dividend_quality_snapshot",
        run_id="run-001",
        dry_run=True,
        orders_previewed_count=2,
        delivery_events=[],
    )

    assert payload == {}


def test_handle_request_post_returns_market_closed_when_schedule_empty(strategy_module, monkeypatch):
    observed = {}

    def fail_if_called():
        raise AssertionError("Closed market should not execute strategy")

    monkeypatch.setattr(strategy_module, "is_market_open_today", lambda **_kwargs: False)
    monkeypatch.setattr(strategy_module, "run_strategy_core", fail_if_called)
    monkeypatch.setattr(
        strategy_module,
        "persist_execution_report",
        lambda report: observed.setdefault("report", dict(report)) or "/tmp/runtime-report.json",
    )

    with strategy_module.app.test_request_context("/", method="POST"):
        body, status = strategy_module.handle_request()

    assert status == 200
    assert body == "Market Closed"
    assert observed["report"]["status"] == "skipped"
    assert observed["report"]["diagnostics"]["skip_reason"] == "market_closed"


def test_handle_request_error_persists_machine_readable_report(strategy_module, monkeypatch):
    observed = {"messages": []}

    monkeypatch.setattr(strategy_module, "build_run_id", lambda: "run-001")
    monkeypatch.setattr(strategy_module, "is_market_open_today", lambda **_kwargs: True)
    monkeypatch.setattr(
        strategy_module,
        "run_strategy_core",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(
        strategy_module,
        "persist_execution_report",
        lambda report: observed.setdefault("report", dict(report)) or "/tmp/runtime-report.json",
    )
    monkeypatch.setattr(strategy_module, "send_tg_message", lambda message: observed["messages"].append(message))

    with strategy_module.app.test_request_context("/", method="POST"):
        body, status = strategy_module.handle_request()

    assert status == 500
    assert body == "Error"
    assert observed["report"]["status"] == "error"
    assert observed["report"]["errors"][0]["stage"] == "strategy_cycle"
    assert observed["report"]["errors"][0]["error_type"] == "RuntimeError"
    assert len(observed["messages"]) == 1


def test_run_strategy_core_allows_multiple_runs_in_same_process(strategy_module, monkeypatch):
    observed = {"connect_calls": 0, "disconnect_calls": 0, "messages": []}

    class FakeIB:
        def isConnected(self):
            return True

        def disconnect(self):
            observed["disconnect_calls"] += 1

    def fake_connect_ib():
        observed["connect_calls"] += 1
        return FakeIB()

    monkeypatch.setattr(strategy_module, "connect_ib", fake_connect_ib)
    monkeypatch.setattr(strategy_module, "get_current_portfolio", lambda ib: ({}, {"equity": 1000.0, "buying_power": 500.0}))
    monkeypatch.setattr(strategy_module, "compute_signals", lambda ib, holdings: (None, "daily-check", False, "SPY:✅"))
    monkeypatch.setattr(strategy_module, "send_tg_message", lambda message: observed["messages"].append(message))

    first = strategy_module.run_strategy_core()
    second = strategy_module.run_strategy_core()

    assert first.result == "OK - heartbeat"
    assert second.result == "OK - heartbeat"
    assert observed["connect_calls"] == 2
    assert observed["disconnect_calls"] == 2


def test_send_tg_message_logs_non_200_response(strategy_module, monkeypatch, capsys):
    class FakeResponse:
        status_code = 401
        text = "unauthorized"

    monkeypatch.setattr(strategy_module, "TG_TOKEN", "token")
    monkeypatch.setattr(strategy_module, "TG_CHAT_ID", "chat-id")
    monkeypatch.setattr(strategy_module.requests, "post", lambda *args, **kwargs: FakeResponse())

    strategy_module.send_tg_message("hello")

    captured = capsys.readouterr()
    assert "Telegram send failed with status 401: unauthorized" in captured.out


def test_global_telegram_chat_id_is_used(strategy_module_factory):
    module = strategy_module_factory(
        GLOBAL_TELEGRAM_CHAT_ID="shared-chat-id",
    )

    assert module.TG_CHAT_ID == "shared-chat-id"


def test_handle_request_runtime_error_fallback_sends_telegram(strategy_module, monkeypatch):
    sent_payloads = []

    class FakeResponse:
        status_code = 200
        text = "ok"

    def fake_post(_url, *, json, timeout):
        sent_payloads.append((json, timeout))
        return FakeResponse()

    monkeypatch.setattr(strategy_module, "_handle_request", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(strategy_module, "TG_TOKEN", "token-1")
    monkeypatch.setattr(strategy_module, "TG_CHAT_ID", "chat-1")
    monkeypatch.setenv("STRATEGY_PLUGIN_ALERT_TELEGRAM_BOT_TOKEN", "plugin-token")
    monkeypatch.setenv("STRATEGY_PLUGIN_ALERT_TELEGRAM_CHAT_IDS", "plugin-chat")
    monkeypatch.setattr(strategy_module.requests, "post", fake_post)

    with strategy_module.app.test_request_context("/", method="POST"):
        body, status = strategy_module.handle_request()

    assert status == 500
    assert body == "Error"
    assert len(sent_payloads) == 1
    assert sent_payloads[0][0]["chat_id"] == "chat-1"
    assert "IBKR strategy run failed" in sent_payloads[0][0]["text"]
    assert "RuntimeError: boom" in sent_payloads[0][0]["text"]


def test_handle_request_runtime_error_fallback_uses_chinese_copy(strategy_module_factory, monkeypatch):
    strategy_module = strategy_module_factory(NOTIFY_LANG="zh")
    sent_payloads = []

    class FakeResponse:
        status_code = 200
        text = "ok"

    def fake_post(_url, *, json, timeout):
        sent_payloads.append((json, timeout))
        return FakeResponse()

    monkeypatch.setattr(strategy_module, "_handle_request", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(strategy_module, "TG_TOKEN", "token-1")
    monkeypatch.setattr(strategy_module, "TG_CHAT_ID", "chat-1")
    monkeypatch.setattr(strategy_module.requests, "post", fake_post)

    with strategy_module.app.test_request_context("/", method="POST"):
        body, status = strategy_module.handle_request()

    assert status == 500
    assert body == "Error"
    text = sent_payloads[0][0]["text"]
    assert "IBKR 策略运行失败" in text
    assert "账户组:" in text
    assert "错误: RuntimeError: boom" in text
