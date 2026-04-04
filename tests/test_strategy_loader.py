import sys
import types

from strategy_loader import load_signal_logic_module


def test_load_signal_logic_module_resolves_global_etf_rotation(monkeypatch):
    market_calendars_module = types.ModuleType("pandas_market_calendars")
    market_calendars_module.get_calendar = lambda name: None
    monkeypatch.setitem(sys.modules, "pandas_market_calendars", market_calendars_module)
    sys.modules.pop("us_equity_strategies.strategies.global_etf_rotation", None)

    module = load_signal_logic_module("global_etf_rotation")

    assert module.__name__ == "us_equity_strategies.strategies.global_etf_rotation"
    assert module.TOP_N == 2


def test_load_signal_logic_module_resolves_russell_1000_multi_factor_defensive():
    try:
        import pandas  # noqa: F401
    except ModuleNotFoundError:
        return

    module = load_signal_logic_module("russell_1000_multi_factor_defensive")

    assert module.__name__ == "us_equity_strategies.strategies.russell_1000_multi_factor_defensive"
    assert module.SIGNAL_SOURCE == "feature_snapshot"


def test_load_signal_logic_module_resolves_cash_buffer_branch_default(monkeypatch):
    try:
        import pandas  # noqa: F401
    except ModuleNotFoundError:
        return

    market_calendars_module = types.ModuleType("pandas_market_calendars")
    market_calendars_module.get_calendar = lambda name: None
    monkeypatch.setitem(sys.modules, "pandas_market_calendars", market_calendars_module)
    sys.modules.pop("us_equity_strategies.strategies.cash_buffer_branch_default", None)

    module = load_signal_logic_module("cash_buffer_branch_default")

    assert module.__name__ == "us_equity_strategies.strategies.cash_buffer_branch_default"
    assert module.SIGNAL_SOURCE == "feature_snapshot"
