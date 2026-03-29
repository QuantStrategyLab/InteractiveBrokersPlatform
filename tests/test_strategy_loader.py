from strategy_loader import load_signal_logic_module


def test_load_signal_logic_module_resolves_global_etf_rotation():
    module = load_signal_logic_module("global_etf_rotation")

    assert module.__name__ == "us_equity_strategies.strategies.global_etf_rotation"
    assert module.TOP_N == 2
