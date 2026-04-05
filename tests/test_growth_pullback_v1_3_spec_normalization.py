import importlib.util
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
RESEARCH_DIR = ROOT / 'research'
CONFIGS_DIR = RESEARCH_DIR / 'configs'


def load_module():
    path = RESEARCH_DIR / 'backtest_growth_pullback_v1_3_spec_normalization.py'
    spec = importlib.util.spec_from_file_location('growth_pullback_v1_3_spec_normalization_test', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_geometry_consistency_checks():
    module = load_module()
    center_path, stable_path = module.resolve_config_paths(CONFIGS_DIR)
    center = module.gp.load_spec_config(center_path)
    stable = module.gp.load_spec_config(stable_path)
    candidates = {candidate.label: candidate for candidate in module.build_candidates(center, stable)}

    cash = candidates['coherent_cash_buffer_branch']
    full = candidates['coherent_full_deployment_branch']
    full_single8 = candidates['coherent_full_deployment_branch_single8']

    assert cash.config.holdings_count == 8
    assert cash.risk_on_exposure == 0.8
    assert full.config.holdings_count == 10
    assert full.config.sector_cap == 0.50
    assert full.config.single_name_cap == 0.10
    assert full.risk_on_exposure == 1.0
    assert full_single8.config.single_name_cap == 0.08


def test_deployment_consistency_statistics():
    module = load_module()
    center_path, _ = module.resolve_config_paths(CONFIGS_DIR)
    center = module.gp.load_spec_config(center_path)
    candidate = module.v12.make_candidate(
        'coherent_cash_buffer_branch',
        center.__class__(
            name='cash_cfg',
            family=center.family,
            universe_spec=center.universe_spec,
            score_template=center.score_template,
            holdings_count=8,
            single_name_cap=0.10,
            sector_cap=0.40,
            regime=center.regime,
            exposures=center.exposures,
            hold_bonus=0.05,
        ),
        risk_on_exposure=0.8,
        note='test',
    )
    summary = pd.Series(
        {
            'risk_on_avg_names': 8.0,
            'risk_on_realized_stock_weight': 0.8,
        }
    )
    metrics = module.spec_consistency_metrics(candidate, summary)

    assert metrics['target_names'] == 8
    assert metrics['realized_names'] == 8
    assert metrics['target_stock_weight'] == 0.8
    assert metrics['realized_stock_weight'] == 0.8
    assert metrics['names_alignment_score'] == 1.0
    assert metrics['stock_alignment_score'] == 1.0
    assert metrics['deployment_honesty_score'] == 1.0


def test_recommendation_logic_prefers_cash_buffer_when_selected():
    module = load_module()
    rec = module.build_recommendation(
        center_summary=pd.Series(
            {
                'strategy': 'center_current',
                'risk_on_fill_rate': 0.8,
                'deployment_honesty_score': 0.73,
            }
        ),
        cash_summary=pd.Series(
            {
                'strategy': 'coherent_cash_buffer_branch',
                'risk_on_fill_rate': 1.0,
                'deployment_honesty_score': 1.0,
            }
        ),
        full_summary=pd.Series({'strategy': 'coherent_full_deployment_branch'}),
        selected_summary=pd.Series(
            {
                'strategy': 'coherent_cash_buffer_branch',
                'oos_cagr_minus_qqq': 0.15,
                'oos_max_drawdown': -0.18,
                'annual_turnover': 2.8,
                'risk_on_fill_rate': 1.0,
                'deployment_honesty_score': 1.0,
                'avg_names_held': 8.5,
                'underfilled_month_share': 0.2,
                'avg_top3_stock_weight': 0.28,
                'full_max_drawdown': -0.31,
                'oos_cagr': 0.30,
            }
        ),
        qqq_plus_oos=pd.Series({'CAGR': 0.334, 'Max Drawdown': -0.323}),
    )

    assert rec['research_recommendation'] == 'cash_buffer_branch'
    assert rec['checks']['center_is_cash_prototype'] is True
    assert rec['role_vs_qqq_plus_current_default'] == '并行分支'


def test_result_export_not_empty(tmp_path):
    module = load_module()
    candidate_summary = pd.DataFrame(
        [
            {
                'strategy': 'coherent_cash_buffer_branch',
                'target_names': 8,
                'realized_names': 8,
                'target_stock_weight': 0.8,
                'realized_stock_weight': 0.8,
                'risk_on_fill_rate': 1.0,
                'deployment_honesty_score': 1.0,
                'spec_normalization_score': 0.8,
            }
        ]
    )
    deployment_summary = pd.DataFrame(
        [
            {
                'strategy': 'coherent_cash_buffer_branch',
                'avg_names_held': 8.5,
                'risk_on_avg_names': 8,
                'risk_on_target_stock_weight': 0.8,
                'risk_on_realized_stock_weight': 0.8,
                'risk_on_fill_rate': 1.0,
                'underfilled_month_share': 0.2,
                'avg_top1_stock_weight': 0.08,
                'avg_top3_stock_weight': 0.24,
                'avg_top5_stock_weight': 0.40,
                'avg_safe_haven_weight': 0.36,
            }
        ]
    )
    comparison = pd.DataFrame(
        [
            {
                'strategy': 'coherent_cash_buffer_branch',
                'period': 'OOS Sample',
                'CAGR': 0.30,
                'Max Drawdown': -0.18,
                'Turnover/Year': 2.8,
                'Average Names Held': 8.5,
                'risk_on_realized_stock_weight': 0.8,
                'beta_vs_qqq': 0.55,
                'alpha_ann_vs_qqq': 0.18,
                'Information Ratio vs QQQ': 0.6,
                'Up Capture vs QQQ': 0.95,
                'Down Capture vs QQQ': 0.22,
            }
        ]
    )
    recommendation = {
        'research_recommendation': 'cash_buffer_branch',
        'selected_research_default': 'coherent_cash_buffer_branch',
        'role_vs_qqq_plus_current_default': '并行分支',
        'reason': 'test',
    }

    md_path = tmp_path / 'growth_pullback_v1_3_spec_normalization.md'
    module.write_markdown_report(
        md_path,
        candidate_summary=candidate_summary,
        deployment_summary=deployment_summary,
        comparison_5bps=comparison,
        recommendation=recommendation,
    )

    assert md_path.exists() and md_path.stat().st_size > 100
    text = md_path.read_text(encoding='utf-8')
    assert 'growth_pullback_systematic_v1.3 spec normalization' in text
    assert 'coherent_cash_buffer_branch' in text
