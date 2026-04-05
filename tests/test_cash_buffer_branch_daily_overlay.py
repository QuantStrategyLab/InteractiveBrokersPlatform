import importlib.util
import sys
from pathlib import Path

import pandas as pd


def load_module():
    path = Path(__file__).resolve().parents[1] / 'research' / 'backtest_cash_buffer_branch_daily_overlay.py'
    spec = importlib.util.spec_from_file_location('backtest_cash_buffer_branch_daily_overlay_test', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_overlay_state_transition_requires_recovery_confirmation():
    module = load_module()
    current, recovery, changed = module.update_portfolio_state(0.4, 0.6, 0, 2)
    assert current == 0.4
    assert recovery == 1
    assert changed is False

    current, recovery, changed = module.update_portfolio_state(current, 0.6, recovery, 2)
    assert current == 0.6
    assert recovery == 0
    assert changed is True

    current, recovery, changed = module.update_portfolio_state(current, 0.2, recovery, 2)
    assert current == 0.2
    assert recovery == 0
    assert changed is True


def test_no_new_names_added_mid_month_and_boxx_reallocation_correct():
    module = load_module()
    monthly_target_weights = {'AAA': 0.10, 'BBB': 0.10, 'CCC': 0.10, module.SAFE_HAVEN: 0.70}
    monthly_names = ['AAA', 'BBB', 'CCC']
    effective = module.build_effective_weights(
        monthly_target_weights,
        monthly_names,
        monthly_base_stock_weight=0.30,
        portfolio_target=0.20,
        name_multipliers={'AAA': 1.0, 'BBB': 0.5, 'CCC': 1.0},
    )

    assert set(effective).issubset({'AAA', 'BBB', 'CCC', module.SAFE_HAVEN})
    assert abs(sum(weight for symbol, weight in effective.items() if symbol != module.SAFE_HAVEN) - 0.20) < 1e-12
    assert abs(effective[module.SAFE_HAVEN] - 0.80) < 1e-12


def test_name_trim_overlay_reallocates_to_boxx_without_replacing_names():
    module = load_module()
    monthly_target_weights = {'AAA': 0.10, 'BBB': 0.10, module.SAFE_HAVEN: 0.80}
    monthly_names = ['AAA', 'BBB']
    effective = module.build_effective_weights(
        monthly_target_weights,
        monthly_names,
        monthly_base_stock_weight=0.20,
        portfolio_target=None,
        name_multipliers={'AAA': 0.0, 'BBB': 0.5},
    )

    assert set(effective).issubset({'AAA', 'BBB', module.SAFE_HAVEN})
    assert 'AAA' not in effective or effective['AAA'] == 0.0
    assert abs(effective['BBB'] - 0.05) < 1e-12
    assert abs(effective[module.SAFE_HAVEN] - 0.95) < 1e-12


def test_result_export_not_empty(tmp_path):
    module = load_module()
    summary_path = tmp_path / 'summary.md'
    baseline_payload = {
        'name': 'cash_buffer_branch_default',
        'family': 'tech_heavy_pullback',
        'exposures': {'risk_on': 0.8, 'soft_defense': 0.6, 'hard_defense': 0.0},
        'holdings_count': 8,
        'single_name_cap': 0.1,
        'sector_cap': 0.4,
        'hold_bonus': 0.1,
        'benchmark_symbol': 'QQQ',
    }
    baseline_row = pd.Series({
        'strategy': 'cash_buffer_branch_default_monthly_baseline',
        'CAGR': 0.25,
        'Max Drawdown': -0.2,
        'Information Ratio vs QQQ': 0.8,
        'alpha_ann_vs_qqq': 0.2,
        'return_2022': -0.12,
        'cagr_2023_plus': 0.4,
        'Turnover/Year': 2.5,
        'Average Stock Weight': 0.6,
        'Average BOXX Weight': 0.4,
    })
    family_scores = pd.DataFrame([
        {
            'strategy': 'baseline', 'family': 'monthly_baseline', 'overlay_selection_score': 0.4,
            'CAGR': 0.25, 'Max Drawdown': -0.2, 'Information Ratio vs QQQ': 0.8,
            'alpha_ann_vs_qqq': 0.2, 'return_2022': -0.12, 'cagr_2023_plus': 0.4,
            'Turnover/Year': 2.5, 'Average Stock Weight': 0.6, 'Average BOXX Weight': 0.4,
            'Overlay Trigger Frequency': 0.0, 'Average Days In Throttle': 0.0, 'Average Days Trimmed': 0.0,
        }
    ])
    comparison_rows = pd.DataFrame([
        {
            'strategy': 'baseline', 'family': 'monthly_baseline', 'CAGR': 0.25, 'cagr_minus_qqq': 0.1,
            'Max Drawdown': -0.2, 'return_2022': -0.12, 'cagr_2023_plus': 0.4, 'Turnover/Year': 2.5,
            'Average Names Held': 6.0, 'Average Stock Weight': 0.6, 'Average BOXX Weight': 0.4,
            'beta_vs_qqq': 0.5, 'alpha_ann_vs_qqq': 0.2, 'Information Ratio vs QQQ': 0.8,
            'Up Capture vs QQQ': 0.9, 'Down Capture vs QQQ': 0.4,
            'Overlay Trigger Frequency': 0.0, 'Average Days In Throttle': 0.0, 'Average Days Trimmed': 0.0,
        }
    ])
    recommendation = {
        'overlay_has_incremental_value': False,
        'best_overlay_family': 'monthly_baseline',
        'best_overlay_strategy': 'baseline',
        'recommended_upgrade_direction': 'do_not_upgrade_overlay',
        'next_step': '保持月频 baseline，不加 overlay',
    }

    module.write_summary_markdown(
        summary_path,
        baseline_payload=baseline_payload,
        baseline_row=baseline_row,
        family_scores=family_scores,
        comparison_rows=comparison_rows,
        recommendation=recommendation,
    )

    assert summary_path.exists() and summary_path.stat().st_size > 0
    assert 'cash_buffer_branch daily overlay research' in summary_path.read_text(encoding='utf-8')
