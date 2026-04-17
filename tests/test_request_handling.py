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

    def fake_run_strategy_core():
        observed["called"] = True
        return "OK - executed"

    monkeypatch.setattr(strategy_module, "is_market_open_today", lambda: True)
    monkeypatch.setattr(strategy_module, "run_strategy_core", fake_run_strategy_core)

    with strategy_module.app.test_request_context("/", method="POST"):
        body, status = strategy_module.handle_request()

    assert status == 200
    assert body == "OK - executed"
    assert observed["called"] is True


def test_handle_request_emits_structured_runtime_events(strategy_module, monkeypatch):
    observed = []

    monkeypatch.setattr(strategy_module, "build_run_id", lambda: "run-001")
    monkeypatch.setattr(
        strategy_module,
        "emit_runtime_log",
        lambda context, event, **fields: observed.append((context.run_id, event, fields)),
    )
    monkeypatch.setattr(strategy_module, "is_market_open_today", lambda: True)
    monkeypatch.setattr(strategy_module, "run_strategy_core", lambda: "OK - executed")

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
    monkeypatch.setattr(strategy_module, "is_market_open_today", lambda: True)
    monkeypatch.setattr(strategy_module, "run_strategy_core", lambda: "OK - executed")
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


def test_execution_report_prefers_configured_managed_symbols_without_ranking_pool(strategy_module_factory):
    module = strategy_module_factory(STRATEGY_PROFILE="soxl_soxx_trend_income")
    report = module.build_execution_report(module.RUNTIME_LOG_CONTEXT.with_run("run-001"))

    assert report["summary"]["managed_symbols"] == ["SOXL", "SOXX", "BOXX", "QQQI", "SPYI"]
    assert report["summary"]["safe_haven"] == "BIL"


def test_handle_request_enriches_runtime_report_with_cycle_details(strategy_module, monkeypatch):
    observed = {}

    monkeypatch.setattr(strategy_module, "build_run_id", lambda: "run-001")
    monkeypatch.setattr(strategy_module, "is_market_open_today", lambda: True)

    def fake_run_strategy_core():
        strategy_module.LAST_CYCLE_DETAILS = {
            "execution_summary": {
                "execution_status": "executed",
                "orders_submitted": [{"symbol": "AAA"}],
                "orders_skipped": [],
                "price_source_mode": "mixed_market_quote_snapshot_close",
                "snapshot_price_fallback_used": True,
                "snapshot_price_fallback_count": 1,
                "snapshot_price_fallback_symbols": ["AAA"],
            },
            "reconciliation_record_path": "/tmp/reconciliation.json",
        }
        return "OK - executed"

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
    assert observed["report"]["summary"]["snapshot_price_fallback_used"] is True
    assert observed["report"]["summary"]["snapshot_price_fallback_count"] == 1
    assert observed["report"]["diagnostics"]["price_source_mode"] == "mixed_market_quote_snapshot_close"
    assert observed["report"]["diagnostics"]["snapshot_price_fallback_symbols"] == ["AAA"]
    assert observed["report"]["artifacts"]["reconciliation_record_path"] == "/tmp/reconciliation.json"


def test_handle_request_post_returns_market_closed_when_schedule_empty(strategy_module, monkeypatch):
    observed = {}

    def fail_if_called():
        raise AssertionError("Closed market should not execute strategy")

    monkeypatch.setattr(strategy_module, "is_market_open_today", lambda: False)
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
    monkeypatch.setattr(strategy_module, "is_market_open_today", lambda: True)
    monkeypatch.setattr(
        strategy_module,
        "run_strategy_core",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
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

    assert first == "OK - heartbeat"
    assert second == "OK - heartbeat"
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
