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
    assert "market_history" in entrypoint.manifest.required_inputs


def test_load_strategy_entrypoint_for_profile_resolves_tech_communication_pullback_enhancement(monkeypatch):
    try:
        import pandas  # noqa: F401
    except ModuleNotFoundError:
        return

    market_calendars_module = types.ModuleType("pandas_market_calendars")
    market_calendars_module.get_calendar = lambda name: None
    monkeypatch.setitem(sys.modules, "pandas_market_calendars", market_calendars_module)

    entrypoint = load_strategy_entrypoint_for_profile("tech_communication_pullback_enhancement")

    assert entrypoint.manifest.profile == "tech_communication_pullback_enhancement"
    assert entrypoint.manifest.default_config["safe_haven"] == "BOXX"


def test_load_strategy_entrypoint_for_profile_resolves_tqqq_growth_income(monkeypatch):
    try:
        import pandas  # noqa: F401
    except ModuleNotFoundError:
        return

    market_calendars_module = types.ModuleType("pandas_market_calendars")
    market_calendars_module.get_calendar = lambda name: None
    monkeypatch.setitem(sys.modules, "pandas_market_calendars", market_calendars_module)

    entrypoint = load_strategy_entrypoint_for_profile("tqqq_growth_income")

    assert entrypoint.manifest.profile == "tqqq_growth_income"
    assert entrypoint.manifest.required_inputs == frozenset({"benchmark_history", "portfolio_snapshot"})
    assert entrypoint.manifest.default_config["benchmark_symbol"] == "QQQ"
    assert entrypoint.manifest.default_config["managed_symbols"] == ("TQQQ", "QQQ", "BOXX", "SPYI", "QQQI")
    assert entrypoint.manifest.default_config["attack_allocation_mode"] == "fixed_qqq_tqqq_pullback"


def test_load_strategy_entrypoint_for_profile_rejects_archived_mega_cap_dynamic_top20(monkeypatch):
    try:
        import pandas  # noqa: F401
    except ModuleNotFoundError:
        return

    market_calendars_module = types.ModuleType("pandas_market_calendars")
    market_calendars_module.get_calendar = lambda name: None
    monkeypatch.setitem(sys.modules, "pandas_market_calendars", market_calendars_module)

    with pytest.raises(ValueError, match="Unsupported STRATEGY_PROFILE"):
        load_strategy_entrypoint_for_profile("mega_cap_leader_rotation_dynamic_top20")


def test_load_strategy_entrypoint_for_profile_rejects_legacy_cash_buffer_profile(monkeypatch):
    try:
        import pandas  # noqa: F401
    except ModuleNotFoundError:
        return

    market_calendars_module = types.ModuleType("pandas_market_calendars")
    market_calendars_module.get_calendar = lambda name: None
    monkeypatch.setitem(sys.modules, "pandas_market_calendars", market_calendars_module)

    with pytest.raises(ValueError, match="Unsupported STRATEGY_PROFILE"):
        load_strategy_entrypoint_for_profile("tech_pullback_cash_buffer")


def test_load_strategy_runtime_adapter_for_profile_resolves_tech_communication_pullback_enhancement(monkeypatch):
    try:
        import pandas  # noqa: F401
    except ModuleNotFoundError:
        return

    market_calendars_module = types.ModuleType("pandas_market_calendars")
    market_calendars_module.get_calendar = lambda name: None
    monkeypatch.setitem(sys.modules, "pandas_market_calendars", market_calendars_module)

    adapter = load_strategy_runtime_adapter_for_profile("tech_communication_pullback_enhancement")

    assert adapter.status_icon == "🧲"
    assert adapter.available_inputs == frozenset({"feature_snapshot"})
    assert adapter.require_snapshot_manifest is True
    assert adapter.snapshot_contract_version == "tech_communication_pullback_enhancement.feature_snapshot.v1"


def test_load_strategy_runtime_adapter_for_profile_rejects_archived_mega_cap_dynamic_top20(monkeypatch):
    try:
        import pandas  # noqa: F401
    except ModuleNotFoundError:
        return

    market_calendars_module = types.ModuleType("pandas_market_calendars")
    market_calendars_module.get_calendar = lambda name: None
    monkeypatch.setitem(sys.modules, "pandas_market_calendars", market_calendars_module)

    with pytest.raises(ValueError, match="Unsupported STRATEGY_PROFILE"):
        load_strategy_runtime_adapter_for_profile("mega_cap_leader_rotation_dynamic_top20")


def test_load_strategy_runtime_adapter_for_profile_resolves_global_etf_rotation_inputs(monkeypatch):
    market_calendars_module = types.ModuleType("pandas_market_calendars")
    market_calendars_module.get_calendar = lambda name: None
    monkeypatch.setitem(sys.modules, "pandas_market_calendars", market_calendars_module)

    adapter = load_strategy_runtime_adapter_for_profile("global_etf_rotation")

    assert adapter.available_inputs == frozenset({"market_history"})
    assert adapter.available_capabilities == frozenset({"broker_client"})


def test_load_strategy_runtime_adapter_for_profile_resolves_semiconductor_inputs(monkeypatch):
    try:
        import pandas  # noqa: F401
    except ModuleNotFoundError:
        return

    market_calendars_module = types.ModuleType("pandas_market_calendars")
    market_calendars_module.get_calendar = lambda name: None
    monkeypatch.setitem(sys.modules, "pandas_market_calendars", market_calendars_module)

    adapter = load_strategy_runtime_adapter_for_profile("soxl_soxx_trend_income")

    assert adapter.available_inputs == frozenset({"derived_indicators", "portfolio_snapshot"})
    assert adapter.portfolio_input_name == "portfolio_snapshot"


def test_load_strategy_runtime_adapter_for_profile_resolves_tqqq_inputs(monkeypatch):
    try:
        import pandas  # noqa: F401
    except ModuleNotFoundError:
        return

    market_calendars_module = types.ModuleType("pandas_market_calendars")
    market_calendars_module.get_calendar = lambda name: None
    monkeypatch.setitem(sys.modules, "pandas_market_calendars", market_calendars_module)

    adapter = load_strategy_runtime_adapter_for_profile("tqqq_growth_income")

    assert adapter.available_inputs == frozenset({"benchmark_history", "portfolio_snapshot"})
    assert adapter.portfolio_input_name == "portfolio_snapshot"


def test_load_strategy_runtime_adapter_for_profile_rejects_legacy_semiconductor_alias(monkeypatch):
    try:
        import pandas  # noqa: F401
    except ModuleNotFoundError:
        return

    market_calendars_module = types.ModuleType("pandas_market_calendars")
    market_calendars_module.get_calendar = lambda name: None
    monkeypatch.setitem(sys.modules, "pandas_market_calendars", market_calendars_module)

    with pytest.raises(ValueError, match="Unsupported STRATEGY_PROFILE"):
        load_strategy_runtime_adapter_for_profile("semiconductor_rotation_income")
