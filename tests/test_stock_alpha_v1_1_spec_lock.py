import importlib.util
import sys
from pathlib import Path

import pandas as pd



def load_module():
    path = Path(__file__).resolve().parents[1] / 'research' / 'backtest_stock_alpha_v1_1_spec_lock.py'
    spec = importlib.util.spec_from_file_location('backtest_stock_alpha_v1_1_spec_lock_test', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module



def test_config_round_trip(tmp_path):
    module = load_module()
    base = module.robust.build_base_candidate()
    config = module.replace(
        base,
        name='spec_lock_round_trip',
        universe_filter=module.suite.UniverseFilterConfig('liquid_50m', 50_000_000.0, leadership_only=False),
        group_normalization='universe',
        holdings_count=12,
        single_name_cap=0.09,
        sector_cap=0.25,
        hold_bonus=0.05,
    )

    path = tmp_path / 'default.json'
    module.save_spec_config(path, config, role='default_frozen_spec')
    loaded = module.load_spec_config(path)

    assert path.exists() and path.stat().st_size > 0
    assert loaded.name == config.name
    assert loaded.universe_filter.name == 'liquid_50m'
    assert loaded.group_normalization == 'universe'
    assert loaded.holdings_count == 12
    assert loaded.single_name_cap == 0.09



def test_selection_score_prefers_more_robust_candidate_and_recommendation_layers():
    module = load_module()
    base = module.robust.build_base_candidate()
    frame = pd.DataFrame(
        [
            {
                'scenario': 'candidate_a',
                'universe_filter': 'leadership_liquid',
                'group_normalization': 'sector',
                'group_normalization_label': 'sector',
                'holdings_count': 16,
                'single_name_cap': 0.08,
                'sector_cap': 0.30,
                'hold_bonus': 0.10,
                'full_total_return': 3.0,
                'full_cagr': 0.20,
                'full_information_ratio_vs_qqq': 0.10,
                'return_2022': -0.12,
                'cagr_2023_plus': 0.32,
                'oos_cagr': 0.21,
                'oos_max_drawdown': -0.28,
                'oos_rolling_36m_alpha_positive_ratio': 1.0,
                'annual_turnover': 3.2,
                'plateau_200bps_share': 0.25,
            },
            {
                'scenario': 'candidate_b',
                'universe_filter': 'liquid_50m',
                'group_normalization': 'universe',
                'group_normalization_label': 'universe_cross_sectional',
                'holdings_count': 12,
                'single_name_cap': 0.10,
                'sector_cap': 0.25,
                'hold_bonus': 0.05,
                'full_total_return': 3.4,
                'full_cagr': 0.22,
                'full_information_ratio_vs_qqq': 0.14,
                'return_2022': -0.08,
                'cagr_2023_plus': 0.36,
                'oos_cagr': 0.24,
                'oos_max_drawdown': -0.31,
                'oos_rolling_36m_alpha_positive_ratio': 1.0,
                'annual_turnover': 3.7,
                'plateau_200bps_share': 0.32,
            },
        ]
    )
    scored = module.add_selection_scores(frame, base, qqq_oos_cagr=0.10)
    top = scored.iloc[0]

    assert top['scenario'] == 'candidate_b'
    assert top['robustness_score'] > scored.iloc[1]['robustness_score']

    recommendation = module.build_recommendation(
        previous_gate={'recommendation': 'no_shadow_tracking', 'plateau_200bps_share': 0.185},
        local_plateau_share=0.22,
        default_row=top,
        aggressive_row=top,
        qqq_oos_cagr=0.10,
    )
    assert recommendation['original_gate_v1_1_local_grid']['recommendation'] == 'yes_shadow_tracking'
    assert recommendation['v1_1_recommendation'] in {'shadow_candidate', 'shadow_ready'}



def test_markdown_export_is_not_empty(tmp_path):
    module = load_module()
    md_path = tmp_path / 'report.md'
    selection_df = pd.DataFrame(
        [{
            'scenario': 'candidate_a',
            'robustness_score': 0.75,
            'robustness_rank': 1,
            'full_cagr_rank': 1,
            'universe_filter': 'leadership_liquid',
            'group_normalization_label': 'sector',
            'holdings_count': 16,
            'single_name_cap': 0.08,
            'sector_cap': 0.30,
            'hold_bonus': 0.10,
            'oos_cagr_minus_qqq': 0.08,
            'plateau_200bps_share': 0.25,
            'oos_max_drawdown': -0.28,
            'annual_turnover': 3.2,
            'return_2022': -0.12,
            'cagr_2023_plus': 0.32,
        }]
    )
    plateau_df = pd.DataFrame(
        [
            {'scope': 'overall', 'dimension': 'all', 'value': 'all', 'neighborhood': 'top_decile_full_cagr', 'count': 2, 'total': 10, 'share': 0.2},
            {'scope': 'overall', 'dimension': 'all', 'value': 'all', 'neighborhood': 'within_100bps_best_cagr_ir_positive', 'count': 3, 'total': 10, 'share': 0.3},
            {'scope': 'overall', 'dimension': 'all', 'value': 'all', 'neighborhood': 'within_200bps_best_cagr_ir_positive', 'count': 4, 'total': 10, 'share': 0.4},
            {'scope': 'axis', 'dimension': 'universe_filter', 'value': 'leadership_liquid', 'neighborhood': 'within_200bps_best_cagr_ir_positive', 'count': 4, 'total': 5, 'share': 0.8},
        ]
    )
    oos_df = pd.DataFrame(
        [{
            'strategy': 'default_frozen_spec',
            'cost_bps_one_way': 5.0,
            'period': 'OOS Sample',
            'CAGR': 0.22,
            'Total Return': 1.0,
            'Max Drawdown': -0.3,
            'Sharpe': 0.9,
            'Information Ratio vs QQQ': 0.5,
            'rolling_36m_alpha_positive_ratio': 1.0,
            'annual_turnover': 3.5,
        }]
    )
    attribution_df = pd.DataFrame(
        [{
            'strategy': 'default_frozen_spec',
            'beta_vs_qqq': 0.7,
            'alpha_ann_vs_qqq': 0.08,
            'tracking_error_vs_qqq': 0.2,
            'information_ratio_vs_qqq': 0.1,
            'up_capture_vs_qqq': 0.8,
            'down_capture_vs_qqq': 0.45,
            'turnover': 3.5,
            'average_names_held': 14,
            'active_share_vs_qqq': float('nan'),
        }]
    )
    spy_sanity_df = pd.DataFrame(
        [{
            'scenario': 'candidate_a',
            'spy_sanity_full_cagr': 0.18,
            'spy_sanity_full_ir_vs_qqq': 0.05,
            'spy_sanity_oos_cagr': 0.19,
            'spy_sanity_oos_cagr_minus_qqq': 0.03,
            'spy_sanity_pass': True,
        }]
    )
    recommendation = {
        'original_gate_previous_round': {'recommendation': 'no_shadow_tracking', 'plateau_200bps_share': 0.185},
        'original_gate_v1_1_local_grid': {'recommendation': 'yes_shadow_tracking'},
        'v1_1_recommendation': 'shadow_candidate',
        'reason': 'test',
    }

    module.write_markdown_report(
        md_path,
        previous_default_row=pd.Series({'scenario': 'previous_offensive_default'}),
        default_row=pd.Series({'scenario': 'candidate_a'}),
        aggressive_row=pd.Series({'scenario': 'candidate_b'}),
        selection_df=selection_df,
        plateau_df=plateau_df,
        plateau_stats={
            'within_200bps_best_cagr_ir_positive_share': 0.4,
            'within_100bps_best_cagr_ir_positive_share': 0.3,
            'top_decile_full_cagr_share': 0.2,
        },
        oos_df=oos_df,
        attribution_df=attribution_df,
        spy_sanity_df=spy_sanity_df,
        recommendation=recommendation,
    )

    text = md_path.read_text(encoding='utf-8')
    assert 'qqq_plus_stock_alpha_v1.1 spec lock' in text
    assert 'shadow_candidate' in text
    assert len(text) > 100
