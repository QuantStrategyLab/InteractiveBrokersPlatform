import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
RESEARCH_DIR = ROOT / 'research'
CONFIGS_DIR = RESEARCH_DIR / 'configs'


def load_module():
    path = RESEARCH_DIR / 'backtest_growth_pullback_v1_5_freeze_review.py'
    spec = importlib.util.spec_from_file_location('growth_pullback_v1_5_freeze_review_test', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_canonical_config_export(tmp_path):
    module = load_module()
    center = module.gp.load_spec_config(CONFIGS_DIR / module.v14.CENTER_CONFIG_FILENAME)
    spec = module.build_canonical_spec(center, 'cash_buffer_a__hb10__base__adv50')

    out = tmp_path / 'growth_pullback_tech_pullback_cash_buffer.json'
    module.save_canonical_spec(out, spec)
    payload = json.loads(out.read_text(encoding='utf-8'))

    assert payload['name'] == 'tech_pullback_cash_buffer'
    assert payload['previous_candidate_name'] == 'cash_buffer_a__hb10__base__adv50'
    assert payload['holdings_count'] == 8
    assert payload['single_name_cap'] == 0.10
    assert payload['sector_cap'] == 0.40
    assert payload['hold_bonus'] == 0.10
    assert payload['exposures']['risk_on'] == 0.80
    assert payload['branch_role'] == 'cash-buffered parallel branch'


def test_manifest_fields_non_empty():
    module = load_module()
    center = module.gp.load_spec_config(CONFIGS_DIR / module.v14.CENTER_CONFIG_FILENAME)
    spec = module.build_canonical_spec(center, 'cash_buffer_a__hb10__base__adv50')
    manifest = module.build_manifest(
        spec=spec,
        canonical_summary={
            'risk_on_realized_stock_weight': 0.8,
            'avg_names_held': 5.9,
            'risk_on_fill_rate': 1.0,
            'beta_vs_qqq_oos': 0.50,
            'oos_cagr': 0.34068,
            'oos_cagr_minus_qqq': 0.238732,
            'oos_max_drawdown': -0.191763,
            'return_2022': -0.120121,
        },
        qqq_plus_oos=pd.Series({'CAGR': 0.33401}),
        recommendation_level='tech_pullback_cash_buffer',
    )

    assert manifest['branch_name'] == 'tech_pullback_cash_buffer'
    assert manifest['role']
    assert manifest['intended_use']
    assert manifest['why_this_is_a_cash_buffer_branch']
    assert manifest['why_this_is_not_frozen']
    assert manifest['preferred_comparison_set']


def test_geometry_and_deployment_consistency_checks():
    module = load_module()
    center = module.gp.load_spec_config(CONFIGS_DIR / module.v14.CENTER_CONFIG_FILENAME)
    spec = module.build_canonical_spec(center, 'cash_buffer_a__hb10__base__adv50')
    payload = module.canonical_spec_to_dict(spec)
    v14_summary = pd.DataFrame(
        [
            {
                'strategy': 'cash_buffer_a__hb10__base__adv50',
                'oos_cagr': 0.34068,
                'return_2022': -0.120121,
                'avg_names_held': 5.9,
            }
        ]
    )
    manifest = {'role': 'cash-buffered parallel branch'}
    canonical_summary = {
        'risk_on_realized_stock_weight': 0.8,
        'soft_defense_realized_stock_weight': 0.6,
        'hard_defense_realized_stock_weight': 0.0,
        'oos_cagr': 0.34068,
        'return_2022': -0.120121,
        'avg_names_held': 5.9,
    }

    checks = module.build_consistency_checks(
        spec=spec,
        config_payload=payload,
        v14_summary=v14_summary,
        previous_candidate_name='cash_buffer_a__hb10__base__adv50',
        canonical_summary=canonical_summary,
        recommendation_level='tech_pullback_cash_buffer',
        manifest=manifest,
    )

    assert checks['passed'].all()
    assert 'geometry_capacity_matches_target' in checks['check'].values
    assert 'risk_on_realized_matches_target' in checks['check'].values


def test_recommendation_logic_defaults_but_not_frozen():
    module = load_module()
    recommendation = module.build_freeze_recommendation(
        canonical_summary={
            'oos_cagr_minus_qqq': 0.238732,
            'oos_max_drawdown': -0.191763,
            'annual_turnover': 2.80,
            'avg_names_held': 5.90,
            'return_2022': -0.120121,
        },
        checks_df=pd.DataFrame([
            {'check': 'a', 'passed': True},
            {'check': 'b', 'passed': True},
        ]),
    )

    assert recommendation['research_recommendation'] == 'tech_pullback_cash_buffer'
    assert recommendation['keep_parallel_branch'] is True
    assert recommendation['role_vs_qqq_plus_current_default'] == '并行分支'
    assert 'average_names_held_still_low' in recommendation['frozen_blockers']


def test_result_export_not_empty(tmp_path):
    module = load_module()
    md_path = tmp_path / 'growth_pullback_v1_5_freeze_review.md'
    module.write_markdown_report(
        md_path,
        canonical_payload={
            'name': 'tech_pullback_cash_buffer',
            'previous_candidate_name': 'cash_buffer_a__hb10__base__adv50',
            'holdings_count': 8,
            'single_name_cap': 0.10,
            'sector_cap': 0.40,
        },
        comparison_df=pd.DataFrame([
            {
                'strategy': 'tech_pullback_cash_buffer',
                'role': 'cash-buffered parallel branch',
                'oos_cagr': 0.34068,
            }
        ]),
        checks_df=pd.DataFrame([
            {'check': 'geometry_capacity_matches_target', 'passed': True, 'detail': 'ok'}
        ]),
        recommendation={
            'research_recommendation': 'tech_pullback_cash_buffer',
            'previous_candidate_name': 'cash_buffer_a__hb10__base__adv50',
            'branch_name': 'tech_pullback_cash_buffer',
            'role_vs_qqq_plus_current_default': '并行分支',
            'keep_parallel_branch': True,
            'reason': 'test',
            'frozen_blockers': ['average_names_held_still_low'],
        },
        manifest={
            'branch_name': 'tech_pullback_cash_buffer',
            'role': 'cash-buffered parallel branch',
            'previous_candidate_name': 'cash_buffer_a__hb10__base__adv50',
            'benchmark': 'QQQ',
            'intended_use': 'test',
        },
    )

    assert md_path.exists() and md_path.stat().st_size > 100
    assert 'growth_pullback_systematic_v1.5 freeze review and branch packaging' in md_path.read_text(encoding='utf-8')
