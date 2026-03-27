from datetime import date
import types

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


def test_try_acquire_execution_lock_uses_persistent_backend_once(strategy_module, monkeypatch):
    calls = []

    def fake_persistent_lock(execution_date):
        calls.append(execution_date)
        return True

    monkeypatch.setenv("EXECUTION_LOCK_BUCKET", "lock-bucket")
    monkeypatch.setattr(strategy_module, "try_acquire_persistent_execution_lock", fake_persistent_lock)

    assert strategy_module.try_acquire_execution_lock() is True
    assert strategy_module.try_acquire_execution_lock() is False
    assert len(calls) == 1
    assert calls[0] == strategy_module.current_execution_date()


def test_try_acquire_execution_lock_skips_when_persistent_lock_exists(strategy_module, monkeypatch):
    monkeypatch.setenv("EXECUTION_LOCK_BUCKET", "lock-bucket")
    monkeypatch.setattr(strategy_module, "try_acquire_persistent_execution_lock", lambda execution_date: False)

    assert strategy_module.try_acquire_execution_lock() is False


def test_try_acquire_persistent_execution_lock_creates_gcs_marker(strategy_module, monkeypatch):
    observed = {}

    class FakeSession:
        def post(self, url, data, headers, timeout):
            observed["url"] = url
            observed["data"] = data.decode("utf-8")
            observed["headers"] = headers
            observed["timeout"] = timeout
            return types.SimpleNamespace(status_code=200, text="ok")

    monkeypatch.setenv("EXECUTION_LOCK_BUCKET", "lock-bucket")
    monkeypatch.setenv("EXECUTION_LOCK_PREFIX", "prod/ibkr")
    monkeypatch.setattr(strategy_module, "build_authorized_session", lambda: FakeSession())

    assert strategy_module.try_acquire_persistent_execution_lock(date(2026, 3, 27)) is True
    assert "b/lock-bucket/o" in observed["url"]
    assert "prod%2Fibkr%2Fexecutions%2F2026-03-27.lock" in observed["url"]
    assert "date=2026-03-27" in observed["data"]
    assert observed["headers"]["Content-Type"] == "text/plain; charset=utf-8"
    assert observed["timeout"] == 10


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
