from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd

MODULE_PATH = Path(__file__).resolve().parents[1] / 'research' / 'backtest_tqqq_growth_position_scaling_followup.py'
spec = importlib.util.spec_from_file_location('tqqq_scaling_followup', MODULE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_trend_score_boost_scaling_mapping():
    row = pd.Series({
        'close': 110.0, 'ma20': 100.0, 'ma60': 90.0,
        'rsi14': 60.0, 'macd_line': 1.0, 'macd_signal': 0.5,
        'mfi14': 55.0, 'cci20': 10.0, 'bias20': 0.02,
        'weekly_k': 60.0, 'weekly_d': 50.0, 'weekly_j': 70.0,
    })
    assert module.scaling_multiplier('trend_score_4_boost', row) == 1.15


def test_overheat_trim_reduces_size():
    row = pd.Series({
        'close': 110.0, 'ma20': 100.0, 'ma60': 95.0,
        'rsi14': 80.0, 'macd_line': 1.0, 'macd_signal': 0.5,
        'mfi14': 60.0, 'cci20': 15.0, 'bias20': 0.09,
        'weekly_k': 65.0, 'weekly_d': 55.0, 'weekly_j': 75.0,
    })
    assert module.scaling_multiplier('overheat_trim', row) == 0.75


def test_recommendation_returns_expected_keys():
    sample = pd.DataFrame([
        {'period': '2023+', 'cost_bps_one_way': 5.0, 'scaling': 'baseline', 'idle_asset': 'QQQ', 'CAGR': 0.38, 'Max Drawdown': -0.32, 'Ulcer Index': 10.0, 'Information Ratio vs QQQ': 0.75, 'Alpha Ann vs QQQ': -0.05, 'Turnover/Year': 1.1, 'Average Scale While Invested': 1.0},
        {'period': '2023+', 'cost_bps_one_way': 5.0, 'scaling': 'baseline', 'idle_asset': 'BOXX', 'CAGR': 0.24, 'Max Drawdown': -0.20, 'Ulcer Index': 7.0, 'Information Ratio vs QQQ': -0.17, 'Alpha Ann vs QQQ': -0.03, 'Turnover/Year': 1.2, 'Average Scale While Invested': 1.0},
        {'period': '2023+', 'cost_bps_one_way': 5.0, 'scaling': 'trend_score_4_boost', 'idle_asset': 'QQQ', 'CAGR': 0.34, 'Max Drawdown': -0.28, 'Ulcer Index': 8.0, 'Information Ratio vs QQQ': 0.60, 'Alpha Ann vs QQQ': -0.03, 'Turnover/Year': 2.0, 'Average Scale While Invested': 0.9},
    ])
    rec = module.build_recommendation(sample)
    assert rec['baseline_growth_reference']['idle_asset'] == 'QQQ'
    assert rec['baseline_defensive_reference']['idle_asset'] == 'BOXX'
    assert rec['best_scaled_candidate']['idle_asset'] == 'QQQ'
