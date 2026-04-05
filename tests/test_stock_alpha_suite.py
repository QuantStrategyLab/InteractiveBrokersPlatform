import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest



def load_research_module():
    path = Path(__file__).resolve().parents[1] / "research" / "backtest_stock_alpha_suite.py"
    spec = importlib.util.spec_from_file_location("backtest_stock_alpha_suite_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_build_offensive_ablation_configs_covers_requested_grid_axes():
    module = load_research_module()

    configs = module.build_offensive_ablation_configs()
    names = {config.name for config in configs}

    assert "base_candidate" in names
    assert "universe_full_eligible" in names
    assert "universe_liquid_50m" in names
    assert "universe_leadership_liquid" in names
    assert "struct_h12_cap6_sector20" in names
    assert "struct_h16_cap8_sector30" in names
    assert "struct_h24_cap10_sector40" in names
    assert "regime_spy_breadth" in names
    assert "regime_qqq_breadth" in names
    assert "regime_qqq_xlk_smh_breadth" in names
    assert "exposure_100_50_10" in names
    assert "exposure_100_60_0" in names
    assert "exposure_100_70_20" in names



def test_normalize_universe_history_parses_dates_and_symbols():
    module = load_research_module()

    raw = pd.DataFrame(
        {
            "symbol": [" aapl ", "msft"],
            "sector": ["Tech", ""],
            "start_date": ["2020-01-31", "2020-02-29"],
            "end_date": ["2020-12-31", None],
        }
    )

    normalized = module.normalize_universe_history(raw)

    assert normalized.loc[0, "symbol"] == "AAPL"
    assert normalized.loc[1, "sector"] == "unknown"
    assert normalized.loc[0, "start_date"] == pd.Timestamp("2020-01-31")
    assert pd.isna(normalized.loc[1, "end_date"])



def test_build_offensive_target_weights_respects_caps_and_safe_haven():
    module = load_research_module()

    rows = [
        {"symbol": "QQQ", "sector": "benchmark", "sma200_gap": 0.15, "mom_6_1": 0.20, "mom_12_1": 0.30, "breakout_252": 0.08, "vol_63": 0.20, "maxdd_126": -0.10, "adv20_usd": 1e9, "history_days": 500, "base_eligible": False},
        {"symbol": "SPY", "sector": "benchmark", "sma200_gap": 0.10, "mom_6_1": 0.12, "mom_12_1": 0.18, "breakout_252": 0.04, "vol_63": 0.16, "maxdd_126": -0.08, "adv20_usd": 1e9, "history_days": 500, "base_eligible": False},
        {"symbol": "XLK", "sector": "benchmark", "sma200_gap": 0.12, "mom_6_1": 0.18, "mom_12_1": 0.26, "breakout_252": 0.07, "vol_63": 0.19, "maxdd_126": -0.09, "adv20_usd": 1e9, "history_days": 500, "base_eligible": False},
        {"symbol": "SMH", "sector": "benchmark", "sma200_gap": 0.14, "mom_6_1": 0.22, "mom_12_1": 0.34, "breakout_252": 0.09, "vol_63": 0.24, "maxdd_126": -0.11, "adv20_usd": 1e9, "history_days": 500, "base_eligible": False},
        {"symbol": "AAA", "sector": "tech", "sma200_gap": 0.20, "mom_6_1": 0.32, "mom_12_1": 0.44, "breakout_252": 0.12, "vol_63": 0.18, "maxdd_126": -0.08, "adv20_usd": 2e8, "history_days": 500, "base_eligible": True},
        {"symbol": "AAB", "sector": "tech", "sma200_gap": 0.18, "mom_6_1": 0.28, "mom_12_1": 0.40, "breakout_252": 0.10, "vol_63": 0.19, "maxdd_126": -0.09, "adv20_usd": 2e8, "history_days": 500, "base_eligible": True},
        {"symbol": "AAC", "sector": "tech", "sma200_gap": 0.16, "mom_6_1": 0.24, "mom_12_1": 0.36, "breakout_252": 0.08, "vol_63": 0.20, "maxdd_126": -0.10, "adv20_usd": 2e8, "history_days": 500, "base_eligible": True},
        {"symbol": "BAA", "sector": "health", "sma200_gap": 0.15, "mom_6_1": 0.25, "mom_12_1": 0.35, "breakout_252": 0.09, "vol_63": 0.17, "maxdd_126": -0.07, "adv20_usd": 2e8, "history_days": 500, "base_eligible": True},
        {"symbol": "BAB", "sector": "health", "sma200_gap": 0.13, "mom_6_1": 0.21, "mom_12_1": 0.31, "breakout_252": 0.07, "vol_63": 0.18, "maxdd_126": -0.08, "adv20_usd": 2e8, "history_days": 500, "base_eligible": True},
        {"symbol": "BAC", "sector": "health", "sma200_gap": 0.11, "mom_6_1": 0.19, "mom_12_1": 0.29, "breakout_252": 0.06, "vol_63": 0.19, "maxdd_126": -0.09, "adv20_usd": 2e8, "history_days": 500, "base_eligible": True},
    ]
    snapshot = pd.DataFrame(rows)
    config = module.OffensiveConfig(
        name="toy",
        universe_filter=module.UniverseFilterConfig("full_eligible", 20_000_000.0),
        holdings_count=4,
        single_name_cap=0.20,
        sector_cap=0.30,
        regime=module.RegimeConfig("qqq_breadth", "QQQ", "broad"),
        exposures=module.ExposureConfig("100_60_0", 0.60, 0.0),
    )

    weights, metadata = module.build_offensive_target_weights(snapshot, {"AAA"}, config)

    stock_weights = {symbol: weight for symbol, weight in weights.items() if symbol != module.SAFE_HAVEN}
    assert metadata["regime"] == "risk_on"
    assert len(stock_weights) == 2
    assert all(weight <= 0.20 + 1e-12 for weight in stock_weights.values())
    assert weights[module.SAFE_HAVEN] == pytest.approx(0.60)
    selected = set(metadata["selected_symbols"])
    assert "AAA" in selected
    tech_count = sum(1 for symbol in selected if symbol.startswith("AA"))
    assert tech_count <= 1



def test_build_offensive_target_weights_can_go_full_safe_haven_in_hard_defense():
    module = load_research_module()

    rows = [
        {"symbol": "QQQ", "sector": "benchmark", "sma200_gap": -0.10, "mom_6_1": -0.05, "mom_12_1": 0.02, "breakout_252": -0.08, "vol_63": 0.22, "maxdd_126": -0.25, "adv20_usd": 1e9, "history_days": 500, "base_eligible": False},
        {"symbol": "SPY", "sector": "benchmark", "sma200_gap": -0.08, "mom_6_1": -0.04, "mom_12_1": 0.01, "breakout_252": -0.07, "vol_63": 0.18, "maxdd_126": -0.20, "adv20_usd": 1e9, "history_days": 500, "base_eligible": False},
        {"symbol": "XLK", "sector": "benchmark", "sma200_gap": -0.09, "mom_6_1": -0.06, "mom_12_1": 0.00, "breakout_252": -0.09, "vol_63": 0.24, "maxdd_126": -0.22, "adv20_usd": 1e9, "history_days": 500, "base_eligible": False},
        {"symbol": "SMH", "sector": "benchmark", "sma200_gap": -0.11, "mom_6_1": -0.08, "mom_12_1": -0.01, "breakout_252": -0.10, "vol_63": 0.26, "maxdd_126": -0.24, "adv20_usd": 1e9, "history_days": 500, "base_eligible": False},
        {"symbol": "AAA", "sector": "tech", "sma200_gap": -0.05, "mom_6_1": -0.03, "mom_12_1": 0.00, "breakout_252": -0.05, "vol_63": 0.19, "maxdd_126": -0.18, "adv20_usd": 2e8, "history_days": 500, "base_eligible": True},
        {"symbol": "BAA", "sector": "health", "sma200_gap": -0.02, "mom_6_1": -0.01, "mom_12_1": 0.01, "breakout_252": -0.04, "vol_63": 0.17, "maxdd_126": -0.16, "adv20_usd": 2e8, "history_days": 500, "base_eligible": True},
    ]
    snapshot = pd.DataFrame(rows)
    config = module.OffensiveConfig(
        name="hard_defense",
        universe_filter=module.UniverseFilterConfig("full_eligible", 20_000_000.0),
        holdings_count=4,
        single_name_cap=0.10,
        sector_cap=0.30,
        regime=module.RegimeConfig("qqq_breadth", "QQQ", "broad"),
        exposures=module.ExposureConfig("100_60_0", 0.60, 0.0),
    )

    weights, metadata = module.build_offensive_target_weights(snapshot, set(), config)

    assert metadata["regime"] == "hard_defense"
    assert metadata["stock_exposure"] == pytest.approx(0.0)
    assert weights == {module.SAFE_HAVEN: 1.0}
