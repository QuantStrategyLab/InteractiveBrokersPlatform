import importlib.util
import sys
from pathlib import Path

import pandas as pd



def load_module():
    path = Path(__file__).resolve().parents[1] / 'research' / 'backtest_growth_pullback_suite.py'
    spec = importlib.util.spec_from_file_location('backtest_growth_pullback_suite_test', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module



def test_config_round_trip(tmp_path):
    module = load_module()
    config = module.GrowthPullbackConfig(
        name='growth_round_trip',
        family='broad_growth_leadership_pullback',
        universe_spec=module.UniverseSpec(
            name='leadership_growth',
            normalization='sector',
            min_adv20_usd=50_000_000.0,
            leadership_filter=True,
            notes='round trip',
        ),
        score_template='balanced_pullback',
        holdings_count=16,
        single_name_cap=0.08,
        sector_cap=0.30,
        regime=module.suite.RegimeConfig('qqq_breadth', 'QQQ', 'broad'),
        exposures=module.suite.ExposureConfig('100_60_0', 0.60, 0.00),
        hold_bonus=0.10,
    )
    path = tmp_path / 'default.json'
    module.save_spec_config(path, config, role='default_research_spec')
    loaded = module.load_spec_config(path)

    assert path.exists() and path.stat().st_size > 0
    assert loaded.name == config.name
    assert loaded.family == config.family
    assert loaded.universe_spec.name == 'leadership_growth'
    assert loaded.score_template == 'balanced_pullback'
    assert loaded.regime.name == 'qqq_breadth'



def test_balanced_pullback_score_prefers_controlled_pullback_over_falling_knife():
    module = load_module()
    config = module.GrowthPullbackConfig(
        name='score_test',
        family='broad_growth_leadership_pullback',
        universe_spec=module.UniverseSpec(
            name='large_cap_eligible',
            normalization='sector',
            min_adv20_usd=20_000_000.0,
        ),
        score_template='balanced_pullback',
        holdings_count=12,
        single_name_cap=0.10,
        sector_cap=0.40,
        regime=module.suite.RegimeConfig('qqq_breadth', 'QQQ', 'broad'),
        exposures=module.suite.ExposureConfig('100_60_0', 0.60, 0.00),
        hold_bonus=0.0,
    )
    frame = pd.DataFrame(
        [
            {
                'symbol': 'QQQ', 'sector': 'benchmark', 'base_eligible': False,
                'mom_6_1': 0.12, 'mom_12_1': 0.22, 'sma20_gap': 0.03, 'sma50_gap': 0.05, 'sma200_gap': 0.10,
                'ma50_over_ma200': 0.06, 'vol_63': 0.25, 'maxdd_126': -0.18, 'breakout_252': -0.03,
                'dist_63_high': -0.04, 'dist_126_high': -0.06, 'rebound_20': 0.05, 'adv20_usd': 1e9,
                'history_days': 1000, 'close': 100.0,
            },
            {
                'symbol': 'CTRL', 'sector': 'Information Technology', 'base_eligible': True,
                'mom_6_1': 0.20, 'mom_12_1': 0.35, 'sma20_gap': 0.02, 'sma50_gap': 0.04, 'sma200_gap': 0.12,
                'ma50_over_ma200': 0.08, 'vol_63': 0.22, 'maxdd_126': -0.16, 'breakout_252': -0.05,
                'dist_63_high': -0.08, 'dist_126_high': -0.12, 'rebound_20': 0.06, 'adv20_usd': 2e8,
                'history_days': 900, 'close': 80.0,
            },
            {
                'symbol': 'KNIFE', 'sector': 'Information Technology', 'base_eligible': True,
                'mom_6_1': -0.05, 'mom_12_1': 0.02, 'sma20_gap': -0.10, 'sma50_gap': -0.18, 'sma200_gap': -0.25,
                'ma50_over_ma200': -0.12, 'vol_63': 0.40, 'maxdd_126': -0.55, 'breakout_252': -0.42,
                'dist_63_high': -0.32, 'dist_126_high': -0.45, 'rebound_20': -0.12, 'adv20_usd': 2e8,
                'history_days': 900, 'close': 30.0,
            },
        ]
    )
    scored = module.score_candidates(frame, current_holdings=set(), config=config)
    ranked = scored.sort_values('score', ascending=False)['symbol'].tolist()
    assert ranked[0] == 'CTRL'
    assert ranked[-1] == 'KNIFE'



def test_selection_score_and_recommendation_prefer_broad_over_naive_or_theme():
    module = load_module()
    summary = pd.DataFrame(
        [
            {
                'strategy': 'broad_best', 'family': 'broad_growth_leadership_pullback', 'universe': 'leadership_growth', 'normalization': 'sector',
                'normalization_label': 'sector', 'min_adv20_usd': 5e7, 'leadership_filter': True, 'sector_whitelist': '[]', 'symbol_whitelist': '[]', 'notes': '',
                'score_template': 'balanced_pullback', 'holdings_count': 16, 'single_name_cap': 0.08, 'sector_cap': 0.30, 'hold_bonus': 0.10,
                'regime_name': 'qqq_breadth', 'benchmark_symbol': 'QQQ', 'breadth_mode': 'broad', 'breadth_symbols': '[]',
                'soft_defense_exposure': 0.60, 'hard_defense_exposure': 0.00,
                'full_cagr': 0.24, 'full_information_ratio_vs_qqq': 0.22, 'full_alpha_ann_vs_qqq': 0.11,
                'oos_cagr': 0.29, 'oos_cagr_minus_qqq': 0.18, 'oos_alpha_ann_vs_qqq': 0.14, 'oos_max_drawdown': -0.28,
                'oos_rolling_alpha_positive_ratio': 1.0, 'return_2022': -0.05, 'cagr_2023_plus': 0.38,
                'annual_turnover': 3.2, 'average_names_held': 14,
            },
            {
                'strategy': 'crypto_theme', 'family': 'crypto_equity_theme_pullback', 'universe': 'crypto_linked_equity_theme', 'normalization': 'universe',
                'normalization_label': 'universe_cross_sectional', 'min_adv20_usd': 2e7, 'leadership_filter': False, 'sector_whitelist': '[]', 'symbol_whitelist': '["COIN"]', 'notes': '',
                'score_template': 'balanced_pullback', 'holdings_count': 12, 'single_name_cap': 0.10, 'sector_cap': 0.40, 'hold_bonus': 0.05,
                'regime_name': 'qqq_breadth', 'benchmark_symbol': 'QQQ', 'breadth_mode': 'broad', 'breadth_symbols': '[]',
                'soft_defense_exposure': 0.60, 'hard_defense_exposure': 0.00,
                'full_cagr': 0.27, 'full_information_ratio_vs_qqq': 0.08, 'full_alpha_ann_vs_qqq': 0.08,
                'oos_cagr': 0.26, 'oos_cagr_minus_qqq': 0.15, 'oos_alpha_ann_vs_qqq': 0.12, 'oos_max_drawdown': -0.46,
                'oos_rolling_alpha_positive_ratio': 0.8, 'return_2022': -0.16, 'cagr_2023_plus': 0.42,
                'annual_turnover': 5.0, 'average_names_held': 4,
            },
            {
                'strategy': 'naive', 'family': 'naive_dip_buy_control', 'universe': 'large_cap_eligible', 'normalization': 'sector',
                'normalization_label': 'sector', 'min_adv20_usd': 2e7, 'leadership_filter': False, 'sector_whitelist': '[]', 'symbol_whitelist': '[]', 'notes': '',
                'score_template': 'naive_dip_buy', 'holdings_count': 16, 'single_name_cap': 0.08, 'sector_cap': 0.30, 'hold_bonus': 0.10,
                'regime_name': 'qqq_breadth', 'benchmark_symbol': 'QQQ', 'breadth_mode': 'broad', 'breadth_symbols': '[]',
                'soft_defense_exposure': 0.60, 'hard_defense_exposure': 0.00,
                'full_cagr': 0.18, 'full_information_ratio_vs_qqq': -0.02, 'full_alpha_ann_vs_qqq': 0.01,
                'oos_cagr': 0.16, 'oos_cagr_minus_qqq': 0.05, 'oos_alpha_ann_vs_qqq': 0.03, 'oos_max_drawdown': -0.41,
                'oos_rolling_alpha_positive_ratio': 0.6, 'return_2022': -0.22, 'cagr_2023_plus': 0.25,
                'annual_turnover': 5.8, 'average_names_held': 16,
            },
        ]
    )
    scored = module.add_selection_scores(summary)
    default_row, aggressive_row, ranked = module.pick_specs(scored)
    rec = module.build_recommendation(
        family_best_df=module.build_family_best_table(ranked),
        default_row=default_row,
        aggressive_row=aggressive_row,
    )

    assert default_row['strategy'] == 'broad_best'
    assert rec['answers']['C_stock_line_to_continue'] == 'broad_growth_leadership_pullback'
    assert rec['answers']['D_crypto_linked_equity_theme'] in {'no', 'only_as_secondary_thematic_bucket'}



def test_result_export_not_empty(tmp_path):
    module = load_module()
    md_path = tmp_path / 'hypotheses.md'
    module.write_hypotheses_note(md_path)
    assert md_path.exists() and md_path.stat().st_size > 100
    assert 'growth_pullback_systematic_v1 hypotheses' in md_path.read_text(encoding='utf-8')
