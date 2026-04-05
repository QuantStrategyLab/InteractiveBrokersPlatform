import importlib.util
import sys
from pathlib import Path

import pandas as pd


def load_module():
    path = Path(__file__).resolve().parents[1] / 'research' / 'backtest_growth_pullback_v1_2_geometry_repair.py'
    spec = importlib.util.spec_from_file_location('growth_pullback_v1_2_geometry_repair_test', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_geometry_feasibility_checks():
    module = load_module()
    center = module.gp.load_spec_config(
        Path(__file__).resolve().parents[1] / 'research' / 'configs' / 'growth_pullback_systematic_v1_default.json'
    )
    assert module.theoretical_stock_capacity(1.0, center, 2) == 0.8

    feasible = center.__class__(
        name='feasible',
        family=center.family,
        universe_spec=center.universe_spec,
        score_template=center.score_template,
        holdings_count=center.holdings_count,
        single_name_cap=0.10,
        sector_cap=0.50,
        regime=center.regime,
        exposures=center.exposures,
        hold_bonus=center.hold_bonus,
    )
    assert module.theoretical_stock_capacity(1.0, feasible, 2) == 1.0

    single8 = center.__class__(
        name='single8',
        family=center.family,
        universe_spec=center.universe_spec,
        score_template=center.score_template,
        holdings_count=center.holdings_count,
        single_name_cap=0.08,
        sector_cap=0.50,
        regime=center.regime,
        exposures=center.exposures,
        hold_bonus=center.hold_bonus,
    )
    assert module.theoretical_stock_capacity(1.0, single8, 2) == 0.96


def test_deployment_fill_rate_statistics():
    module = load_module()
    monthly = pd.DataFrame(
        [
            {
                'strategy': 'center_current', 'regime': 'risk_on', 'selected_count': 8,
                'target_stock_weight': 1.0, 'realized_stock_weight': 0.8, 'safe_haven_weight': 0.2,
                'fill_rate': 0.8, 'theoretical_stock_max': 0.8, 'geometry_feasible': False,
                'top1_stock_weight': 0.10, 'top3_stock_weight': 0.30, 'top5_stock_weight': 0.50,
                'underfilled_month': True, 'primary_underfill_reason': 'sector_cap_binding',
            },
            {
                'strategy': 'center_current', 'regime': 'soft_defense', 'selected_count': 12,
                'target_stock_weight': 0.6, 'realized_stock_weight': 0.6, 'safe_haven_weight': 0.4,
                'fill_rate': 1.0, 'theoretical_stock_max': 0.6, 'geometry_feasible': True,
                'top1_stock_weight': 0.05, 'top3_stock_weight': 0.15, 'top5_stock_weight': 0.25,
                'underfilled_month': False, 'primary_underfill_reason': 'filled',
            },
        ]
    )
    summary = module.summarize_deployment(monthly)
    assert summary['avg_names_held'] == 10
    assert summary['risk_on_realized_stock_weight'] == 0.8
    assert summary['risk_on_fill_rate'] == 0.8
    assert summary['soft_defense_realized_stock_weight'] == 0.6
    assert summary['underfilled_month_share'] == 0.5
    assert summary['dominant_underfill_reason'] == 'sector_cap_binding'


def test_recommendation_logic_can_return_cash_buffer_branch():
    module = load_module()
    center = pd.Series({'strategy': 'center_current', 'oos_cagr': 0.31, 'oos_max_drawdown': -0.18})
    stable = pd.Series({'strategy': 'local_stable_neighbor'})
    explicit_cash = pd.Series({'strategy': 'explicit_cash_buffer_control', 'oos_cagr': 0.30, 'oos_max_drawdown': -0.17})
    best = pd.Series(
        {
            'strategy': 'center_current',
            'oos_cagr': 0.31,
            'oos_cagr_minus_qqq': 0.18,
            'oos_max_drawdown': -0.18,
            'annual_turnover': 3.0,
            'risk_on_fill_rate': 0.80,
            'avg_names_held': 6.5,
            'underfilled_month_share': 0.5,
            'avg_top3_stock_weight': 0.30,
            'full_max_drawdown': -0.33,
        }
    )
    rec = module.build_recommendation(
        center_summary=center,
        stable_summary=stable,
        explicit_cash_summary=explicit_cash,
        best_overall_summary=best,
        selected_repair_summary=best,
        qqq_plus_oos=pd.Series({'CAGR': 0.334, 'Max Drawdown': -0.323}),
    )
    assert rec['research_recommendation'] == 'cash_buffer_branch'
    assert rec['role_vs_qqq_plus_current_default'] == '并行分支'


def test_markdown_export_not_empty(tmp_path):
    module = load_module()
    center = module.gp.load_spec_config(
        Path(__file__).resolve().parents[1] / 'research' / 'configs' / 'growth_pullback_systematic_v1_default.json'
    )
    stable = module.gp.load_spec_config(
        Path(__file__).resolve().parents[1] / 'research' / 'configs' / 'growth_pullback_systematic_v1_1_default.json'
    )
    md_path = tmp_path / 'geometry.md'
    module.write_markdown_report(
        md_path,
        center_cfg=center,
        stable_cfg=stable,
        repair_summary=pd.DataFrame(
            [
                {
                    'strategy': 'center_current', 'full_cagr': 0.23, 'oos_cagr': 0.31, 'oos_cagr_minus_qqq': 0.21,
                    'full_max_drawdown': -0.33, 'oos_max_drawdown': -0.18, 'return_2022': -0.11,
                    'cagr_2023_plus': 0.48, 'annual_turnover': 2.96, 'avg_names_held': 6.4,
                    'risk_on_fill_rate': 0.8, 'geometry_repair_score': 0.7,
                }
            ]
        ),
        deployment_summary=pd.DataFrame(
            [
                {
                    'strategy': 'center_current', 'avg_names_held': 6.4, 'risk_on_avg_names': 8.0,
                    'risk_on_target_stock_weight': 1.0, 'risk_on_realized_stock_weight': 0.8,
                    'risk_on_fill_rate': 0.8, 'avg_top3_stock_weight': 0.3, 'avg_safe_haven_weight': 0.44,
                    'underfilled_month_share': 0.84, 'dominant_underfill_reason': 'sector_cap_binding',
                }
            ]
        ),
        comparison_5bps=pd.DataFrame(
            [
                {
                    'strategy': 'center_current', 'period': 'OOS Sample', 'CAGR': 0.31, 'Max Drawdown': -0.18,
                    'Turnover/Year': 3.0, 'Average Names Held': 6.4, 'risk_on_realized_stock_weight': 0.8,
                    'beta_vs_qqq': 0.5, 'alpha_ann_vs_qqq': 0.23, 'Information Ratio vs QQQ': 0.79,
                    'Up Capture vs QQQ': 0.98, 'Down Capture vs QQQ': 0.18,
                }
            ]
        ),
        recommendation={
            'research_recommendation': 'cash_buffer_branch',
            'role_vs_qqq_plus_current_default': '并行分支',
            'selected_geometry_repair_default': 'center_current',
            'reason': 'test',
        },
    )
    assert md_path.exists() and md_path.stat().st_size > 100
    assert 'growth_pullback_systematic_v1.2 geometry repair' in md_path.read_text(encoding='utf-8')
