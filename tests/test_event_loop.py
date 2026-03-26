import asyncio
import importlib
import sys
import types
from concurrent.futures import ThreadPoolExecutor

import pytest


@pytest.fixture
def strategy_module(monkeypatch):
    monkeypatch.setenv("IB_GATEWAY_HOST", "127.0.0.1")
    monkeypatch.delenv("IB_GATEWAY_ZONE", raising=False)
    monkeypatch.setenv("IB_GATEWAY_PORT", "4001")
    monkeypatch.setenv("IB_CLIENT_ID", "1")

    flask_module = types.ModuleType("flask")

    class FakeFlask:
        def __init__(self, *args, **kwargs):
            pass

        def route(self, *args, **kwargs):
            def decorator(func):
                return func

            return decorator

    flask_module.Flask = FakeFlask
    monkeypatch.setitem(sys.modules, "flask", flask_module)

    google_module = types.ModuleType("google")
    google_auth_module = types.ModuleType("google.auth")
    google_auth_module.default = lambda: (None, None)
    google_cloud_module = types.ModuleType("google.cloud")
    compute_v1_module = types.ModuleType("google.cloud.compute_v1")
    google_module.auth = google_auth_module
    google_cloud_module.compute_v1 = compute_v1_module
    monkeypatch.setitem(sys.modules, "google", google_module)
    monkeypatch.setitem(sys.modules, "google.auth", google_auth_module)
    monkeypatch.setitem(sys.modules, "google.cloud", google_cloud_module)
    monkeypatch.setitem(sys.modules, "google.cloud.compute_v1", compute_v1_module)

    market_calendars_module = types.ModuleType("pandas_market_calendars")
    market_calendars_module.get_calendar = lambda name: None
    monkeypatch.setitem(sys.modules, "pandas_market_calendars", market_calendars_module)

    ib_insync_module = types.ModuleType("ib_insync")

    class PlaceholderIB:
        def connect(self, *args, **kwargs):
            raise AssertionError("test should patch IB before use")

    ib_insync_module.IB = PlaceholderIB
    ib_insync_module.Stock = type("Stock", (), {})
    ib_insync_module.MarketOrder = type("MarketOrder", (), {})
    ib_insync_module.LimitOrder = type("LimitOrder", (), {})
    monkeypatch.setitem(sys.modules, "ib_insync", ib_insync_module)

    sys.modules.pop("main", None)
    module = importlib.import_module("main")
    return importlib.reload(module)


def test_ensure_event_loop_creates_loop_in_worker_thread(strategy_module):
    def worker():
        with pytest.raises(RuntimeError):
            asyncio.get_event_loop_policy().get_event_loop()

        loop = strategy_module.ensure_event_loop()
        current = asyncio.get_event_loop_policy().get_event_loop()
        return loop, current

    with ThreadPoolExecutor(max_workers=1) as executor:
        loop, current = executor.submit(worker).result()

    assert loop is current
    assert not loop.is_closed()


def test_connect_ib_prepares_event_loop_before_connect(strategy_module, monkeypatch):
    observed = {}

    class FakeIB:
        def connect(self, host, port, clientId, timeout):
            observed["loop"] = asyncio.get_event_loop_policy().get_event_loop()
            observed["args"] = (host, port, clientId, timeout)

    monkeypatch.setattr(strategy_module, "IB", FakeIB)

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(strategy_module.connect_ib).result()

    assert observed["args"] == ("127.0.0.1", 4001, 1, 20)
    assert observed["loop"] is not None
