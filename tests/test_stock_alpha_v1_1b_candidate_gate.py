import importlib.util
import sys
from pathlib import Path

import pandas as pd



def load_module():
    path = Path(__file__).resolve().parents[1] / 'research' / 'backtest_stock_alpha_v1_1b_candidate_gate.py'
    spec = importlib.util.spec_from_file_location('backtest_stock_alpha_v1_1b_candidate_gate_test', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module



def test_build_first_order_neighbors_changes_one_dimension_at_a_time():
    module = load_module()
    center = module.v11.load_spec_config(
        Path(__file__).resolve().parents[1] / 'research' / 'configs' / 'qqq_plus_stock_alpha_v1_1_default.json'
    )
    neighbors = module.build_first_order_neighbors(center)

    assert len(neighbors) == 6
    dims = [meta['change_dimension'] for meta, _config in neighbors]
    assert dims.count('holdings_count') == 1
    assert dims.count('single_name_cap') == 1
    assert dims.count('sector_cap') == 2
    assert dims.count('hold_bonus') == 2



def test_candidate_plateau_summary_counts_first_order_neighbors():
    module = load_module()
    center = pd.Series({'CAGR': 0.33, 'alpha_ann_vs_qqq': 0.12, 'Max Drawdown': -0.32})
    neighbors = pd.DataFrame(
        [
            {'strategy': 'n1', 'CAGR': 0.329, 'alpha_ann_vs_qqq': 0.11, 'Max Drawdown': -0.31},
            {'strategy': 'n2', 'CAGR': 0.321, 'alpha_ann_vs_qqq': 0.10, 'Max Drawdown': -0.34},
            {'strategy': 'n3', 'CAGR': 0.300, 'alpha_ann_vs_qqq': 0.05, 'Max Drawdown': -0.36},
        ]
    )
    _frame, summary = module.build_candidate_plateau(neighbors, center)

    assert summary['neighbor_count'] == 3
    assert summary['neighbor_plateau_50bps_count'] == 1
    assert summary['neighbor_plateau_100bps_count'] == 2
    assert summary['neighbor_plateau_200bps_count'] == 2



def test_holdout_builders_and_recommendation_layer():
    module = load_module()
    dates = pd.bdate_range('2022-01-03', '2023-12-29')
    strategy = pd.Series(0.0008, index=dates)
    benchmark = pd.Series(0.0004, index=dates)

    monthly, monthly_summary = module.build_monthly_jackknife(
        strategy_returns=strategy,
        benchmark_returns=benchmark,
        cost_bps=5.0,
    )
    blocks, block6_summary = module.build_block_holdout(
        strategy_returns=strategy,
        benchmark_returns=benchmark,
        cost_bps=5.0,
        window_months=6,
    )
    _blocks12, block12_summary = module.build_block_holdout(
        strategy_returns=strategy,
        benchmark_returns=benchmark,
        cost_bps=5.0,
        window_months=12,
    )

    assert not monthly.empty
    assert not blocks.empty
    assert 0.0 <= monthly_summary['positive_alpha_share'] <= 1.0
    assert 0.0 <= block6_summary['positive_alpha_share'] <= 1.0

    center_row = pd.Series({'alpha_ann_vs_qqq': 0.10, 'Max Drawdown': -0.20})
    recommendation = module.build_candidate_recommendation(
        original_global_gate={'recommendation': 'no_shadow_tracking', 'plateau_200bps_share': 0.162},
        center_row=center_row,
        local_plateau_summary={'neighbor_plateau_100bps_share': 0.6, 'neighbor_plateau_200bps_share': 0.8},
        monthly_summary_5bps={'positive_alpha_share': 0.95},
        block6_summary_5bps={'positive_alpha_share': 0.85},
        block12_summary_5bps={'positive_alpha_share': 0.75},
    )
    assert recommendation['candidate_centric_recommendation'] == 'shadow_candidate'
