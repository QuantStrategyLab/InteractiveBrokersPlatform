import pandas as pd


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

    class FakeCalendar:
        def schedule(self, start_date, end_date):
            return pd.DataFrame({"market_open": [pd.Timestamp("2026-03-27 09:30:00")]}, index=[pd.Timestamp("2026-03-27")])

    def fake_run_strategy_core():
        observed["called"] = True
        return "OK - executed"

    monkeypatch.setattr(strategy_module.mcal, "get_calendar", lambda name: FakeCalendar())
    monkeypatch.setattr(strategy_module, "run_strategy_core", fake_run_strategy_core)

    with strategy_module.app.test_request_context("/", method="POST"):
        body, status = strategy_module.handle_request()

    assert status == 200
    assert body == "OK - executed"
    assert observed["called"] is True


def test_handle_request_post_returns_market_closed_when_schedule_empty(strategy_module, monkeypatch):
    class ClosedCalendar:
        def schedule(self, start_date, end_date):
            return pd.DataFrame()

    def fail_if_called():
        raise AssertionError("Closed market should not execute strategy")

    monkeypatch.setattr(strategy_module.mcal, "get_calendar", lambda name: ClosedCalendar())
    monkeypatch.setattr(strategy_module, "run_strategy_core", fail_if_called)

    with strategy_module.app.test_request_context("/", method="POST"):
        body, status = strategy_module.handle_request()

    assert status == 200
    assert body == "Market Closed"


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


def test_global_telegram_chat_id_is_used_as_fallback(strategy_module_factory):
    module = strategy_module_factory(
        TELEGRAM_CHAT_ID=None,
        GLOBAL_TELEGRAM_CHAT_ID="shared-chat-id",
    )

    assert module.TG_CHAT_ID == "shared-chat-id"
