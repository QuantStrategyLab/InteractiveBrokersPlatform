import importlib.util
import sys
from pathlib import Path

import pandas as pd


def load_module():
    path = Path(__file__).resolve().parents[1] / 'research' / 'backtest_growth_pullback_v1_1_spec_lock.py'
    spec = importlib.util.spec_from_file_location('growth_pullback_v1_1_spec_lock_test', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_config_parse_and_first_order_neighbors():
    module = load_module()
    cfg = module.gp.load_spec_config(
        Path(__file__).resolve().parents[1] / 'research' / 'configs' / 'growth_pullback_systematic_v1_default.json'
    )
    neighbors = module.build_first_order_neighbors(cfg)
    names = [config.name for _meta, config in neighbors]

    assert cfg.name == 'tech_heavy_pullback_balanced_focused_qqq_breadth'
    assert len(neighbors) == 6
    assert any('holdings_count_16' in name for name in names)
    assert any('single_name_cap_0p08' in name for name in names)
    assert any('sector_cap_0p5' in name for name in names)
    assert any(name.endswith('__adv20m') for name in names)


def test_occupancy_summary_detects_sector_cap_binding():
    module = load_module()
    monthly = pd.DataFrame(
        [
            {
                'strategy': 'center', 'rebalance_date': '2024-01-31', 'regime': 'risk_on', 'selected_count': 8,
                'stock_weight': 1.0, 'safe_haven_weight': 0.0,
                'top1_stock_weight': 0.10, 'top3_stock_weight': 0.30, 'top5_stock_weight': 0.50,
                'underfilled_lt_nominal': True, 'underfilled_lt_10': True, 'underfilled_lt_8': False, 'underfilled_lt_6': False,
                'primary_underfill_reason': 'sector_cap_binding',
            },
            {
                'strategy': 'center', 'rebalance_date': '2024-02-29', 'regime': 'soft_defense', 'selected_count': 6,
                'stock_weight': 0.60, 'safe_haven_weight': 0.40,
                'top1_stock_weight': 0.10, 'top3_stock_weight': 0.30, 'top5_stock_weight': 0.50,
                'underfilled_lt_nominal': True, 'underfilled_lt_10': True, 'underfilled_lt_8': True, 'underfilled_lt_6': False,
                'primary_underfill_reason': 'sector_cap_binding',
            },
            {
                'strategy': 'center', 'rebalance_date': '2024-03-31', 'regime': 'risk_on', 'selected_count': 8,
                'stock_weight': 1.0, 'safe_haven_weight': 0.0,
                'top1_stock_weight': 0.10, 'top3_stock_weight': 0.30, 'top5_stock_weight': 0.50,
                'underfilled_lt_nominal': True, 'underfilled_lt_10': True, 'underfilled_lt_8': False, 'underfilled_lt_6': False,
                'primary_underfill_reason': 'sector_cap_binding',
            },
        ]
    )
    summary = module.summarize_occupancy(monthly, strategy_label='center')

    assert summary['avg_selected_count'] == 22 / 3
    assert summary['risk_on_avg_names'] == 8
    assert summary['soft_defense_avg_names'] == 6
    assert summary['share_lt_8'] == 1 / 3
    assert summary['dominant_underfill_reason'] == 'sector_cap_binding'
    assert summary['dominant_underfill_reason_share'] == 1.0


def test_recommendation_logic_requires_more_than_performance():
    module = load_module()
    center = pd.Series({'strategy': 'center', 'avg_selected_count': 6.4})
    stable = pd.Series(
        {
            'strategy': 'center',
            'avg_selected_count': 6.4,
            'share_lt_8': 0.55,
            'avg_top3_stock_weight': 0.74,
            'oos_cagr_minus_qqq': 0.21,
            'oos_max_drawdown': -0.19,
            'annual_turnover': 2.9,
            'full_max_drawdown': -0.33,
            'oos_cagr': 0.315,
        }
    )
    rec = module.build_recommendation(
        center_summary=center,
        stable_summary=stable,
        local_plateau_summary={
            'local_plateau_100bps_share': 0.33,
            'local_plateau_200bps_share': 0.33,
        },
        qqq_plus_default_oos=pd.Series({'CAGR': 0.334, 'Max Drawdown': -0.323}),
    )

    assert rec['research_recommendation'] == 'research_default_candidate'
    assert rec['role_vs_qqq_plus_current_default'] in {'并行分支', '次级实验'}


def test_markdown_export_not_empty(tmp_path):
    module = load_module()
    cfg = module.gp.load_spec_config(
        Path(__file__).resolve().parents[1] / 'research' / 'configs' / 'growth_pullback_systematic_v1_default.json'
    )
    md_path = tmp_path / 'spec_lock.md'
    module.write_markdown_report(
        md_path,
        center_config=cfg,
        center_result_5bps=pd.Series({'strategy': cfg.name}),
        stable_result_5bps=pd.Series({'strategy': cfg.name}),
        occupancy_summary_df=pd.DataFrame(
            [
                {
                    'strategy': cfg.name,
                    'avg_selected_count': 6.4,
                    'risk_on_avg_names': 7.8,
                    'soft_defense_avg_names': 5.0,
                    'share_lt_nominal': 1.0,
                    'share_lt_8': 0.5,
                    'avg_top3_stock_weight': 0.72,
                    'avg_safe_haven_weight': 0.18,
                    'dominant_underfill_reason': 'sector_cap_binding',
                    'dominant_underfill_reason_share': 0.8,
                }
            ]
        ),
        local_plateau_df=pd.DataFrame(
            [
                {
                    'strategy': cfg.name,
                    'variant_scope': 'center',
                    'change_summary': 'center',
                    'CAGR': 0.31,
                    'Max Drawdown': -0.18,
                    'Turnover/Year': 2.9,
                    'delta_oos_cagr_vs_center': 0.0,
                    'delta_oos_maxdd_vs_center': 0.0,
                    'delta_oos_turnover_vs_center': 0.0,
                }
            ]
        ),
        local_plateau_summary={
            'local_plateau_50bps_share': 0.17,
            'local_plateau_100bps_share': 0.33,
            'local_plateau_200bps_share': 0.33,
        },
        comparison_df_5bps=pd.DataFrame(
            [
                {
                    'strategy': cfg.name,
                    'period': 'OOS Sample',
                    'family': cfg.family,
                    'CAGR': 0.31,
                    'Total Return': 1.2,
                    'Max Drawdown': -0.18,
                    'Sharpe': 1.0,
                    'Alpha_ann_vs_QQQ': 0.23,
                    'Information Ratio vs QQQ': 0.18,
                    'Turnover/Year': 2.9,
                    'Average Names Held': 6.4,
                }
            ]
        ),
        recommendation={
            'research_recommendation': 'research_default_candidate',
            'role_vs_qqq_plus_current_default': '并行分支',
            'selected_local_default': cfg.name,
            'reason': 'edge is good, but occupancy/concentration or local plateau still needs more tightening',
        },
    )
    assert md_path.exists() and md_path.stat().st_size > 100
    assert 'growth_pullback_systematic_v1.1 spec lock' in md_path.read_text(encoding='utf-8')
