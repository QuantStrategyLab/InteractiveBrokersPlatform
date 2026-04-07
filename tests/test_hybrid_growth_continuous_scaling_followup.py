from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd

MODULE_PATH = Path(__file__).resolve().parents[1] / "research" / "backtest_hybrid_growth_continuous_scaling_followup.py"
spec = importlib.util.spec_from_file_location("tqqq_continuous_followup", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_continuous_scale_boosts_above_ma20():
    row = pd.Series({"close": 104.0, "ma20": 100.0})
    assert module.continuous_scale("ma20_gap_linear", row) == 1.15


def test_continuous_scale_trims_below_ma20():
    row = pd.Series({"close": 94.0, "ma20": 100.0})
    assert round(module.continuous_scale("ma20_gap_linear", row), 4) == 0.6625


def test_trim_only_never_boosts_above_one():
    above = pd.Series({"close": 104.0, "ma20": 100.0})
    below = pd.Series({"close": 94.0, "ma20": 100.0})
    assert module.continuous_scale("ma20_gap_trim_only", above) == 1.0
    assert round(module.continuous_scale("ma20_gap_trim_only", below), 4) == 0.6625


def test_recommendation_keys_present():
    sample = pd.DataFrame([
        {"period": "2023+", "cost_bps_one_way": 5.0, "scaling": "baseline", "idle_asset": "QQQ", "CAGR": 0.38, "Max Drawdown": -0.32, "Ulcer Index": 9.6, "Information Ratio vs QQQ": 0.75, "Turnover/Year": 1.1},
        {"period": "2023+", "cost_bps_one_way": 5.0, "scaling": "baseline", "idle_asset": "BOXX", "CAGR": 0.24, "Max Drawdown": -0.20, "Ulcer Index": 7.5, "Information Ratio vs QQQ": -0.17, "Turnover/Year": 1.2},
        {"period": "2023+", "cost_bps_one_way": 5.0, "scaling": "ma20_gap_trim_only", "idle_asset": "QQQ", "CAGR": 0.35, "Max Drawdown": -0.30, "Ulcer Index": 9.0, "Information Ratio vs QQQ": 0.70, "Alpha Ann vs QQQ": -0.02, "Turnover/Year": 2.0, "Average Scale While Invested": 0.95},
    ])
    rec = module.build_recommendation(sample)
    assert rec["baseline_growth_reference"]["idle_asset"] == "QQQ"
    assert rec["baseline_defensive_reference"]["idle_asset"] == "BOXX"
    assert rec["continuous_candidate"]["idle_asset"] == "QQQ"
    assert rec["continuous_candidate"]["scaling"] == "ma20_gap_trim_only"
