import sys
import types

import pytest

from strategy_loader import (
    load_strategy_entrypoint_for_profile,
    load_strategy_runtime_adapter_for_profile,
)


def test_load_strategy_entrypoint_for_profile_resolves_global_etf_rotation(monkeypatch):
    market_calendars_module = types.ModuleType("pandas_market_calendars")
    market_calendars_module.get_calendar = lambda name: None
    monkeypatch.setitem(sys.modules, "pandas_market_calendars", market_calendars_module)

    entrypoint = load_strategy_entrypoint_for_profile("global_etf_rotation")

    assert entrypoint.manifest.profile == "global_etf_rotation"
    assert "historical_close_loader" in entrypoint.manifest.required_inputs


def test_load_strategy_entrypoint_for_profile_resolves_tech_pullback_cash_buffer(monkeypatch):
    try:
        import pandas  # noqa: F401
    except ModuleNotFoundError:
        return

    market_calendars_module = types.ModuleType("pandas_market_calendars")
    market_calendars_module.get_calendar = lambda name: None
    monkeypatch.setitem(sys.modules, "pandas_market_calendars", market_calendars_module)

    entrypoint = load_strategy_entrypoint_for_profile("tech_pullback_cash_buffer")

    assert entrypoint.manifest.profile == "tech_pullback_cash_buffer"
    assert entrypoint.manifest.default_config["safe_haven"] == "BOXX"


def test_load_strategy_entrypoint_for_profile_rejects_legacy_cash_buffer_profile(monkeypatch):
    try:
        import pandas  # noqa: F401
    except ModuleNotFoundError:
        return

    market_calendars_module = types.ModuleType("pandas_market_calendars")
    market_calendars_module.get_calendar = lambda name: None
    monkeypatch.setitem(sys.modules, "pandas_market_calendars", market_calendars_module)

    with pytest.raises(ValueError, match="Unsupported STRATEGY_PROFILE"):
        load_strategy_entrypoint_for_profile("cash_buffer_branch_default")


def test_load_strategy_runtime_adapter_for_profile_resolves_tech_pullback_cash_buffer(monkeypatch):
    try:
        import pandas  # noqa: F401
    except ModuleNotFoundError:
        return

    market_calendars_module = types.ModuleType("pandas_market_calendars")
    market_calendars_module.get_calendar = lambda name: None
    monkeypatch.setitem(sys.modules, "pandas_market_calendars", market_calendars_module)

    adapter = load_strategy_runtime_adapter_for_profile("tech_pullback_cash_buffer")

    assert adapter.status_icon == "🧲"
    assert adapter.available_inputs == frozenset({"feature_snapshot"})
    assert adapter.require_snapshot_manifest is True
    assert adapter.snapshot_contract_version == "tech_pullback_cash_buffer.feature_snapshot.v1"


def test_load_strategy_runtime_adapter_for_profile_resolves_global_etf_rotation_inputs(monkeypatch):
    market_calendars_module = types.ModuleType("pandas_market_calendars")
    market_calendars_module.get_calendar = lambda name: None
    monkeypatch.setitem(sys.modules, "pandas_market_calendars", market_calendars_module)

    adapter = load_strategy_runtime_adapter_for_profile("global_etf_rotation")

    assert adapter.available_inputs == frozenset({"historical_close_loader"})
    assert adapter.available_capabilities == frozenset({"broker_client"})
