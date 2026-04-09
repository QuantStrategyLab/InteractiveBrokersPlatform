from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd

MODULE_PATH = Path(__file__).resolve().parents[1] / 'research' / 'backtest_tqqq_growth_idle_throttle_followup.py'
spec = importlib.util.spec_from_file_location('tqqq_followup', MODULE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_throttle_multiplier_halves_only_below_ma60():
    row = pd.Series({'close': 100.0, 'ma60': 105.0})
    assert module.throttle_multiplier('ma60_half', row) == 0.5
    row2 = pd.Series({'close': 106.0, 'ma60': 105.0})
    assert module.throttle_multiplier('ma60_half', row2) == 1.0


def test_markdown_table_has_headers():
    frame = pd.DataFrame([{'a': 1, 'b': 2}])
    text = module.base.frame_to_markdown_table(frame)
    assert '| a | b |' in text
    assert '| --- | --- |' in text


def test_recommendation_shape():
    sample = pd.DataFrame([
        {'period': '2023+', 'cost_bps_one_way': 5.0, 'throttle': 'baseline', 'idle_asset': 'QQQ', 'CAGR': 0.3, 'Max Drawdown': -0.3, 'Information Ratio vs QQQ': 0.7, 'Alpha Ann vs QQQ': 0.0, '2022 Return': -0.4},
        {'period': '2023+', 'cost_bps_one_way': 5.0, 'throttle': 'baseline', 'idle_asset': 'BOXX', 'CAGR': 0.2, 'Max Drawdown': -0.2, 'Information Ratio vs QQQ': -0.1, 'Alpha Ann vs QQQ': 0.0, '2022 Return': -0.16},
        {'period': '2023+', 'cost_bps_one_way': 5.0, 'throttle': 'ma60_half', 'idle_asset': 'BOXX', 'CAGR': 0.1, 'Max Drawdown': -0.15, 'Information Ratio vs QQQ': -0.3, 'Alpha Ann vs QQQ': 0.0, '2022 Return': -0.06},
    ])
    rec = module.build_recommendation(sample)
    assert rec['best_baseline_by_cagr']['idle_asset'] == 'QQQ'
    assert rec['safest_baseline_idle_asset']['idle_asset'] == 'BOXX'
    assert rec['best_throttle_candidate']['idle_asset'] == 'BOXX'
