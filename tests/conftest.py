import importlib
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def strategy_module_factory(monkeypatch):
    def load_strategy_module(**env_overrides):
        defaults = {
            "IB_GATEWAY_INSTANCE_NAME": "127.0.0.1",
            "IB_GATEWAY_ZONE": None,
            "IB_GATEWAY_MODE": "live",
            "STRATEGY_PROFILE": "global_etf_rotation",
            "ACCOUNT_GROUP": "default",
            "IB_CLIENT_ID": "1",
            "IBKR_FEATURE_SNAPSHOT_MANIFEST_PATH": None,
            "IBKR_RECONCILIATION_OUTPUT_PATH": None,
            "IB_ACCOUNT_GROUP_CONFIG_JSON": (
                '{"groups":{"default":{"ib_gateway_instance_name":"127.0.0.1",'
                '"ib_gateway_mode":"live","ib_client_id":1}}}'
            ),
            "IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME": None,
            "GLOBAL_TELEGRAM_CHAT_ID": None,
        }
        defaults.update(env_overrides)

        for key, value in defaults.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)

        google_module = types.ModuleType("google")
        google_module.__path__ = []

        google_auth_module = types.ModuleType("google.auth")
        google_auth_module.__path__ = []
        google_auth_module.default = lambda *args, **kwargs: (None, None)

        google_auth_transport_module = types.ModuleType("google.auth.transport")
        google_auth_transport_module.__path__ = []
        google_auth_transport_requests_module = types.ModuleType("google.auth.transport.requests")
        google_auth_transport_requests_module.AuthorizedSession = type("AuthorizedSession", (), {})

        google_cloud_module = types.ModuleType("google.cloud")
        google_cloud_module.__path__ = []
        compute_v1_module = types.ModuleType("google.cloud.compute_v1")

        google_module.auth = google_auth_module
        google_auth_module.transport = google_auth_transport_module
        google_auth_transport_module.requests = google_auth_transport_requests_module
        google_cloud_module.compute_v1 = compute_v1_module

        monkeypatch.setitem(sys.modules, "google", google_module)
        monkeypatch.setitem(sys.modules, "google.auth", google_auth_module)
        monkeypatch.setitem(sys.modules, "google.auth.transport", google_auth_transport_module)
        monkeypatch.setitem(sys.modules, "google.auth.transport.requests", google_auth_transport_requests_module)
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

    return load_strategy_module


@pytest.fixture
def strategy_module(strategy_module_factory):
    return strategy_module_factory()
