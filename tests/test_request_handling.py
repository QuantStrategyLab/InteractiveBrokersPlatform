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


def test_try_acquire_execution_lock_uses_only_in_memory_guard(strategy_module, monkeypatch):
    monkeypatch.setenv("EXECUTION_LOCK_BUCKET", "lock-bucket")

    def fail_if_persistent_lock_used(*args, **kwargs):
        raise AssertionError("persistent execution lock should not be used")

    monkeypatch.setattr(
        strategy_module,
        "try_acquire_persistent_execution_lock",
        fail_if_persistent_lock_used,
        raising=False,
    )

    assert strategy_module.try_acquire_execution_lock() is True
    assert strategy_module.try_acquire_execution_lock() is False


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
