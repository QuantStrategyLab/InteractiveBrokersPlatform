import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest



def load_module():
    path = Path(__file__).resolve().parents[1] / 'research' / 'backtest_stock_alpha_v1_robustness.py'
    spec = importlib.util.spec_from_file_location('backtest_stock_alpha_v1_robustness_test', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module



def build_toy_snapshot(module):
    rows = [
        {"symbol": "QQQ", "sector": "benchmark", "sma200_gap": 0.10, "mom_6_1": 0.20, "mom_12_1": 0.30, "breakout_252": 0.08, "vol_63": 0.21, "maxdd_126": -0.12, "adv20_usd": 1e9, "history_days": 500, "base_eligible": False, "universe_reference_date": pd.Timestamp("2024-01-31")},
        {"symbol": "SPY", "sector": "benchmark", "sma200_gap": 0.08, "mom_6_1": 0.16, "mom_12_1": 0.24, "breakout_252": 0.06, "vol_63": 0.18, "maxdd_126": -0.10, "adv20_usd": 1e9, "history_days": 500, "base_eligible": False, "universe_reference_date": pd.Timestamp("2024-01-31")},
        {"symbol": "XLK", "sector": "benchmark", "sma200_gap": 0.09, "mom_6_1": 0.18, "mom_12_1": 0.28, "breakout_252": 0.07, "vol_63": 0.22, "maxdd_126": -0.11, "adv20_usd": 1e9, "history_days": 500, "base_eligible": False, "universe_reference_date": pd.Timestamp("2024-01-31")},
        {"symbol": "SMH", "sector": "benchmark", "sma200_gap": 0.11, "mom_6_1": 0.22, "mom_12_1": 0.34, "breakout_252": 0.09, "vol_63": 0.24, "maxdd_126": -0.13, "adv20_usd": 1e9, "history_days": 500, "base_eligible": False, "universe_reference_date": pd.Timestamp("2024-01-31")},
        {"symbol": "AAA", "sector": "tech", "sma200_gap": 0.20, "mom_6_1": 0.35, "mom_12_1": 0.45, "breakout_252": 0.13, "vol_63": 0.19, "maxdd_126": -0.08, "adv20_usd": 2e8, "history_days": 500, "base_eligible": True, "universe_reference_date": pd.Timestamp("2024-01-31")},
        {"symbol": "AAB", "sector": "tech", "sma200_gap": 0.16, "mom_6_1": 0.27, "mom_12_1": 0.39, "breakout_252": 0.09, "vol_63": 0.20, "maxdd_126": -0.09, "adv20_usd": 2e8, "history_days": 500, "base_eligible": True, "universe_reference_date": pd.Timestamp("2024-01-31")},
        {"symbol": "BAA", "sector": "health", "sma200_gap": 0.18, "mom_6_1": 0.30, "mom_12_1": 0.41, "breakout_252": 0.10, "vol_63": 0.18, "maxdd_126": -0.07, "adv20_usd": 2e8, "history_days": 500, "base_eligible": True, "universe_reference_date": pd.Timestamp("2024-01-31")},
        {"symbol": "BAB", "sector": "health", "sma200_gap": 0.14, "mom_6_1": 0.24, "mom_12_1": 0.33, "breakout_252": 0.07, "vol_63": 0.17, "maxdd_126": -0.06, "adv20_usd": 2e8, "history_days": 500, "base_eligible": True, "universe_reference_date": pd.Timestamp("2024-01-31")},
    ]
    return pd.DataFrame(rows)



def test_build_local_parameter_grid_has_expected_size_and_contains_base_neighborhood():
    module = load_module()
    base = module.build_base_candidate()
    grid = module.build_local_parameter_grid(base)

    assert len(grid) == 81
    names = {config.name for config in grid}
    assert 'grid_h12_cap7_sector25_hold05' in names
    assert 'grid_h16_cap8_sector30_hold10' in names
    assert 'grid_h20_cap9_sector35_hold15' in names



def test_pressure_variants_explicitly_include_required_degradations():
    module = load_module()
    base = module.build_base_candidate()
    variants = module.build_pressure_variants(base)
    names = {name for name, _config, _context, _lag in variants}

    assert 'alias_on_baseline' in names
    assert 'alias_off_no_identifier_repair' in names
    assert 'universe_lag_1_rebalance' in names
    assert 'leadership_ultra_liquid_100m' in names
    assert 'normalization_universe' in names



def test_universe_normalization_path_runs_and_keeps_safe_haven_weight():
    module = load_module()
    snapshot = build_toy_snapshot(module)
    base = module.build_base_candidate()
    config = module.replace(base, group_normalization='universe', holdings_count=4, single_name_cap=0.20, sector_cap=0.30)

    weights, metadata = module.build_offensive_target_weights_robust(snapshot, {'AAA'}, config)

    assert metadata['group_normalization'] == 'universe'
    assert metadata['regime'] == 'risk_on'
    assert module.suite.SAFE_HAVEN in weights
    assert weights[module.suite.SAFE_HAVEN] == pytest.approx(0.60)



def test_turnover_profile_reports_replacements_and_holding_duration():
    module = load_module()
    selection_history = pd.DataFrame(
        {
            'rebalance_date': pd.to_datetime(['2024-01-31', '2024-02-29', '2024-03-31']),
            'selected_symbols': [('AAA', 'BAA', 'CAA'), ('AAA', 'BAB', 'CAA'), ('AAA', 'BAB', 'CAD')],
        }
    )
    turnover_history = pd.Series(
        [0.0, 0.15, 0.10],
        index=pd.to_datetime(['2024-01-31', '2024-02-29', '2024-03-31']),
    )

    profile = module.compute_turnover_profile(selection_history, turnover_history)

    assert profile['annual_turnover'] > 0
    assert profile['average_names_replaced_per_rebalance'] == pytest.approx(1.0)
    assert profile['median_holding_duration_days'] > 0
    assert 0 <= profile['top5_continuity'] <= 1
