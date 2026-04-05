import importlib.util
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
RESEARCH_DIR = ROOT / 'research'
CONFIGS_DIR = RESEARCH_DIR / 'configs'


def load_module():
    path = RESEARCH_DIR / 'backtest_growth_pullback_v1_4_cash_buffer_refinement.py'
    spec = importlib.util.spec_from_file_location('growth_pullback_v1_4_cash_buffer_refinement_test', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_honest_cash_buffer_geometry_checks():
    module = load_module()
    center = module.gp.load_spec_config(module.resolve_center_config(CONFIGS_DIR))
    candidates = {candidate.label: candidate for candidate in module.build_cash_buffer_candidates(center)}

    default = candidates['coherent_cash_buffer_branch']
    diversified = candidates['cash_buffer_b__hb05__base__adv50']

    assert default.config.holdings_count == 8
    assert default.config.single_name_cap == 0.10
    assert default.config.sector_cap == 0.40
    assert default.risk_on_exposure == 0.80

    assert diversified.config.holdings_count == 10
    assert diversified.config.single_name_cap == 0.08
    assert diversified.config.sector_cap == 0.40
    assert diversified.risk_on_exposure == 0.80


def test_deployment_consistency_statistics_include_2022_breakdown():
    module = load_module()
    monthly = pd.DataFrame(
        [
            {
                'strategy': 'coherent_cash_buffer_branch', 'rebalance_date': '2022-01-31', 'regime': 'risk_on', 'selected_count': 8,
                'target_stock_weight': 0.8, 'realized_stock_weight': 0.8, 'safe_haven_weight': 0.2,
                'fill_rate': 1.0, 'theoretical_stock_max': 0.8, 'geometry_feasible': True,
                'top1_stock_weight': 0.10, 'top3_stock_weight': 0.30, 'top5_stock_weight': 0.50,
                'underfilled_month': False, 'primary_underfill_reason': 'filled',
            },
            {
                'strategy': 'coherent_cash_buffer_branch', 'rebalance_date': '2022-02-28', 'regime': 'soft_defense', 'selected_count': 8,
                'target_stock_weight': 0.6, 'realized_stock_weight': 0.6, 'safe_haven_weight': 0.4,
                'fill_rate': 1.0, 'theoretical_stock_max': 0.6, 'geometry_feasible': True,
                'top1_stock_weight': 0.08, 'top3_stock_weight': 0.24, 'top5_stock_weight': 0.40,
                'underfilled_month': False, 'primary_underfill_reason': 'filled',
            },
            {
                'strategy': 'coherent_cash_buffer_branch', 'rebalance_date': '2022-03-31', 'regime': 'hard_defense', 'selected_count': 0,
                'target_stock_weight': 0.0, 'realized_stock_weight': 0.0, 'safe_haven_weight': 1.0,
                'fill_rate': None, 'theoretical_stock_max': 0.0, 'geometry_feasible': True,
                'top1_stock_weight': 0.0, 'top3_stock_weight': 0.0, 'top5_stock_weight': 0.0,
                'underfilled_month': True, 'primary_underfill_reason': 'hard_defense_zero_stock',
            },
        ]
    )

    summary = module.v12.summarize_deployment(monthly)
    year_2022 = module.summarize_2022_deployment(monthly)

    assert summary['avg_names_held'] == 16 / 3
    assert summary['risk_on_realized_stock_weight'] == 0.8
    assert summary['risk_on_fill_rate'] == 1.0
    assert year_2022['share_2022_risk_on'] == 1 / 3
    assert year_2022['share_2022_soft_defense'] == 1 / 3
    assert year_2022['share_2022_hard_defense'] == 1 / 3
    assert year_2022['avg_2022_stock_weight'] == (0.8 + 0.6 + 0.0) / 3


def test_freeze_decision_logic_can_freeze_current_default():
    module = load_module()
    candidate_table = pd.DataFrame(
        [
            {'strategy': 'coherent_cash_buffer_branch', 'cash_buffer_refinement_score': 0.80},
            {'strategy': 'cash_buffer_b__hb05__base__adv50', 'cash_buffer_refinement_score': 0.75},
        ]
    )
    rec = module.build_recommendation(
        current_default=pd.Series(
            {
                'strategy': 'coherent_cash_buffer_branch',
                'oos_cagr': 0.33,
                'return_2022': -0.12,
                'avg_names_held': 6.0,
            }
        ),
        best_refined=pd.Series(
            {
                'strategy': 'coherent_cash_buffer_branch',
                'oos_cagr': 0.33,
                'oos_cagr_minus_qqq': 0.20,
                'oos_max_drawdown': -0.19,
                'annual_turnover': 2.8,
                'avg_names_held': 6.2,
                'underfilled_month_share': 0.25,
                'avg_top3_stock_weight': 0.21,
                'deployment_honesty_score': 1.0,
                'risk_on_fill_rate': 1.0,
                'return_2022': -0.11,
            }
        ),
        qqq_plus_oos=pd.Series({'CAGR': 0.334, 'Max Drawdown': -0.323}),
        full_deployment_reference=pd.Series({'strategy': 'coherent_full_deployment_branch'}),
        all_candidates=candidate_table,
    )

    assert rec['research_recommendation'] == 'tech_pullback_cash_buffer_frozen'
    assert rec['keep_parallel_branch'] is True
    assert rec['role_vs_qqq_plus_current_default'] == '并行分支'


def test_result_export_not_empty(tmp_path):
    module = load_module()
    md_path = tmp_path / 'growth_pullback_v1_4_cash_buffer_refinement.md'
    module.write_markdown_report(
        md_path,
        branch_default={'strategy': 'coherent_cash_buffer_branch'},
        reference_params=[{'strategy': 'center_current'}],
        candidate_summary=pd.DataFrame([
            {
                'strategy': 'coherent_cash_buffer_branch', 'base_shape': 'A', 'regime_variant': 'base', 'adv_bucket': '50M',
                'hold_bonus_bucket': '0.05', 'holdings_count': 8, 'single_name_cap': 0.10, 'sector_cap': 0.40,
                'soft_defense_exposure': 0.60, 'full_cagr': 0.24, 'oos_cagr': 0.33, 'oos_cagr_minus_qqq': 0.23,
                'return_2022': -0.12, 'oos_max_drawdown': -0.19, 'annual_turnover': 2.8, 'avg_names_held': 5.9,
                'avg_top3_stock_weight': 0.21, 'cash_buffer_refinement_score': 0.69,
            }
        ]),
        deployment_summary=pd.DataFrame([
            {
                'strategy': 'coherent_cash_buffer_branch', 'avg_names_held': 5.9, 'risk_on_avg_names': 8.0,
                'risk_on_realized_stock_weight': 0.8, 'risk_on_fill_rate': 1.0, 'underfilled_month_share': 0.26,
                'avg_top1_stock_weight': 0.07, 'avg_top3_stock_weight': 0.21, 'avg_top5_stock_weight': 0.35,
                'avg_safe_haven_weight': 0.44, 'share_2022_risk_on': 0.25, 'share_2022_soft_defense': 0.50,
                'share_2022_hard_defense': 0.25, 'avg_2022_stock_weight': 0.35,
            }
        ]),
        comparison_5bps=pd.DataFrame([
            {
                'strategy': 'coherent_cash_buffer_branch', 'period': 'OOS Sample', 'CAGR': 0.33, 'Max Drawdown': -0.19,
                'Turnover/Year': 2.8, 'Average Names Held': 5.9, 'risk_on_realized_stock_weight': 0.8,
                'beta_vs_qqq': 0.50, 'alpha_ann_vs_qqq': 0.25, 'Information Ratio vs QQQ': 0.84,
                'Up Capture vs QQQ': 1.0, 'Down Capture vs QQQ': 0.15,
            }
        ]),
        recommendation={
            'research_recommendation': 'tech_pullback_cash_buffer',
            'selected_refined_default': 'coherent_cash_buffer_branch',
            'keep_parallel_branch': True,
            'role_vs_qqq_plus_current_default': '并行分支',
            'reason': 'test',
        },
    )
    assert md_path.exists() and md_path.stat().st_size > 100
    assert 'growth_pullback_systematic_v1.4 cash buffer branch refinement' in md_path.read_text(encoding='utf-8')
