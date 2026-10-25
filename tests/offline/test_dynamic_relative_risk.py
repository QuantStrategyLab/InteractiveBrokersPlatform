"""Artificial fixed paths and hypothetical policies; no data or strategy runs."""
from copy import deepcopy
import json
import os
import socket

import pytest

if not os.environ.get("IBKR_OFFLINE_UES_SOURCE"):
    pytest.skip("requires isolated TQQQ research environment", allow_module_level=True)

from tests.test_relative_risk_preview import assert_unarmed, inputs as base_inputs
from application.relative_risk_preview import RelativeRiskPreviewError, preview_relative_risk
from application.tqqq_offline_risk import _research_dependencies


@pytest.fixture(autouse=True)
def isolated_research_only(monkeypatch):
    _research_dependencies()
    def denied(*args, **kwargs):
        raise AssertionError("dynamic previews prohibit network")
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'getaddrinfo', denied)


def inputs():
    packet = base_inputs()
    packet['strategy_context'].update(account_scope='ARTIFICIAL_ACCOUNT_SCOPE',
        wealth_scope='ALLOCATED_ACCOUNT_NET_WEALTH')
    packet['benchmark_context'].update(account_scope='ARTIFICIAL_ACCOUNT_SCOPE',
        wealth_scope='ACCOUNT_RISK_REFERENCE_NET_WEALTH')
    return packet


def policy(packet=None, *, level='balanced', capital=10000.):
    packet = inputs() if packet is None else packet
    return dict(synthetic=True, loss_measure='MAX_DRAWDOWN_LOSS_FRACTION',
        policy_id='artificial-drawdown-policy', source_sha256='c' * 64,
        account_scope='ARTIFICIAL_ACCOUNT_SCOPE',
        binding=vars(packet['binding']).copy(),
        comparison_context={key: value for key, value in packet['strategy_context'].items()
            if key not in ('source_sha256', 'series_id', 'account_scope', 'wealth_scope')},
        level=level, nav=capital,
        curve=dict(reference_capital=10000., exponent=1., protected_capital=capital,
            currency='USD', source='artificial-curve-source'),
        levels=dict(conservative=dict(drawdown_fraction=.3, excess_fraction=.02),
            balanced=dict(drawdown_fraction=.4, excess_fraction=.04),
            growth=dict(drawdown_fraction=.5, excess_fraction=.06)),
        account_max_drawdown_fraction=.4, previous_limits=None)


def run(packet=None, proposed=None):
    packet = inputs() if packet is None else packet
    out = preview_relative_risk(**packet,
        dynamic_policy=policy(packet) if proposed is None else proposed)
    assert_unarmed(out)
    return out, out['drawdown_rule_previews']['capital_adaptive']


@pytest.mark.parametrize('level,account,extra', [
    ('conservative', .15, .01), ('balanced', .2, .02), ('growth', .25, .03)])
def test_three_tiers_reuse_the_original_curve_with_distinct_drawdown_inputs(level, account, extra):
    out, rule = run(proposed=policy(level=level))
    assert rule['capital_factor'] == pytest.approx(.5)
    assert rule['account_limit_fraction'] == pytest.approx(account)
    assert rule['excess_fraction'] == pytest.approx(extra)
    assert rule['benchmark_relative_limit_fraction'] == pytest.approx(.060094 + extra)
    assert rule['combined_limit_fraction'] == pytest.approx(.060094 + extra)
    assert rule['status'] == 'EXCEEDS'
    assert rule['retained_state_verified'] is False
    assert 'drawdown_excess_method_and_value' not in out['remaining_policy_fields']
    assert 'dynamic_drawdown_calibration_and_source_approval' in out['remaining_policy_fields']


def test_larger_protected_capital_tightens_both_limits_and_lower_nav_does_not_relax_them():
    _, first = run(proposed=policy())
    proposed = policy(capital=40000.)
    _, larger = run(proposed=proposed)
    assert larger['capital_factor'] == pytest.approx(.2)
    assert larger['account_limit_fraction'] < first['account_limit_fraction']
    assert larger['excess_fraction'] < first['excess_fraction']
    proposed['nav'] = 20000.
    _, after_loss = run(proposed=proposed)
    assert after_loss == larger


def test_previous_limits_only_tighten_and_remain_caller_supplied_state():
    proposed = policy()
    proposed['previous_limits'] = dict(account_limit_fraction=.1, excess_fraction=.005)
    _, rule = run(proposed=proposed)
    assert rule['account_limit_fraction'] == .1
    assert rule['excess_fraction'] == .005


@pytest.mark.parametrize('explicit', [None, .07, .41])
def test_frozen_benchmark_can_supply_or_tighten_account_ceiling_without_a_personal_number(explicit):
    from tests.test_benchmark_drawdown_reference import reference_inputs

    packet, reference = reference_inputs()
    proposed = policy(packet)
    proposed['account_scope'] = packet['strategy_context']['account_scope']
    proposed['comparison_context'] = {key: value for key, value in proposed['comparison_context'].items()
        if key not in ('account_risk_benchmark', 'decision_cutoff_utc')}
    proposed['account_max_drawdown_fraction'] = explicit
    out = preview_relative_risk(**packet, dynamic_policy=proposed, account_drawdown_reference=reference)
    rule = out['drawdown_rule_previews']['capital_adaptive']
    assert rule['account_limit_fraction'] == pytest.approx(.07 if explicit == .07 else .17)
    assert rule['model_calibrated'] is False
    assert_unarmed(out)


def test_no_scalar_and_no_reference_never_default_to_an_account_risk_number():
    proposed = policy()
    proposed['account_max_drawdown_fraction'] = None
    with pytest.raises(RelativeRiskPreviewError, match='DYNAMIC_ACCOUNT_CEILING_SOURCE_REQUIRED'):
        preview_relative_risk(**inputs(), dynamic_policy=proposed)


def test_candidate_cannot_set_its_own_limit_and_a_smaller_path_can_pass():
    packet = inputs()
    _, large = run(packet)
    packet['strategy_returns'] *= .5
    _, small = run(packet)
    for key in ('account_limit_fraction', 'excess_fraction', 'combined_limit_fraction'):
        assert large[key] == small[key]
    assert large['status'] == 'EXCEEDS' and small['status'] == 'WITHIN'


def test_benchmark_cannot_relax_the_separate_account_ceiling():
    packet = inputs()
    packet['benchmark_returns'].iloc[:] = [-.5, 0., 0., 0.]
    proposed = policy(packet)
    proposed['account_max_drawdown_fraction'] = .08
    _, rule = run(packet, proposed)
    assert rule['within_relative_limit'] is True
    assert rule['within_account_limit'] is False
    assert rule['combined_limit_fraction'] == .08
    assert rule['status'] == 'EXCEEDS'


def test_zero_benchmark_drawdown_uses_additive_allowance_without_division():
    packet = inputs()
    packet['benchmark_returns'].iloc[:] = .01
    _, rule = run(packet)
    assert rule['combined_limit_fraction'] == pytest.approx(.02)
    assert rule['status'] == 'EXCEEDS'


@pytest.mark.parametrize('kind', ['strategy_only', 'benchmark_role_missing',
    'other_account', 'policy_scope_missing'])
def test_account_ceiling_requires_allocated_account_and_reference_path_declarations(kind):
    packet = inputs(); proposed = policy(packet)
    if kind == 'strategy_only': packet['strategy_context']['wealth_scope'] = 'FULL_STRATEGY_PATH'
    elif kind == 'benchmark_role_missing': packet['benchmark_context'].pop('wealth_scope')
    elif kind == 'other_account': packet['benchmark_context']['account_scope'] = 'OTHER'
    elif kind == 'policy_scope_missing': proposed.pop('account_scope')
    with pytest.raises(RelativeRiskPreviewError):
        run(packet, proposed)


@pytest.mark.parametrize('kind', ['tail_metric', 'stop_metric', 'real', 'currency', 'window',
    'binding', 'unordered', 'missing_curve', 'missing_source', 'boolean', 'negative', 'over_one',
    'nav_above_protected', 'missing_previous_field', 'nonfinite'])
def test_wrong_basis_or_missing_policy_is_not_filled_from_tail_budgets_or_candidate(kind):
    proposed = policy()
    if kind == 'tail_metric': proposed['loss_measure'] = 'CVAR95_TWO_SESSION_DECISION_NAV_FRACTION'
    elif kind == 'stop_metric': proposed['loss_measure'] = 'STOP_LOSS_RATIO'
    elif kind == 'real': proposed['synthetic'] = False
    elif kind == 'currency': proposed['curve']['currency'] = 'HKD'
    elif kind == 'window': proposed['comparison_context']['window_id'] = 'OTHER'
    elif kind == 'binding': proposed['binding']['strategy_profile'] = 'OTHER'
    elif kind == 'unordered': proposed['levels']['conservative']['excess_fraction'] = .5
    elif kind == 'missing_curve': proposed.pop('curve')
    elif kind == 'missing_source': proposed.pop('source_sha256')
    elif kind == 'boolean': proposed['levels']['balanced']['drawdown_fraction'] = True
    elif kind == 'negative': proposed['levels']['balanced']['excess_fraction'] = -.01
    elif kind == 'over_one': proposed['account_max_drawdown_fraction'] = 1.1
    elif kind == 'nav_above_protected': proposed['nav'] = 10001.
    elif kind == 'missing_previous_field': proposed['previous_limits'] = dict(account_limit_fraction=.1)
    elif kind == 'nonfinite': proposed['nav'] = float('nan')
    with pytest.raises(RelativeRiskPreviewError):
        run(proposed=proposed)


def test_deterministic_snapshot_hash_binds_all_policy_inputs_without_mutating_them():
    packet = inputs(); proposed = policy(packet); before = deepcopy(proposed)
    first, _ = run(packet, proposed)
    second, _ = run(packet, proposed)
    assert first == second and before == proposed
    proposed['source_sha256'] = 'd' * 64
    changed, _ = run(packet, proposed)
    assert changed['input_sha256'] != first['input_sha256']
    json.dumps(changed, allow_nan=False)


def test_no_dynamic_source_retains_an_undetermined_rule():
    out = preview_relative_risk(**inputs())
    assert out['drawdown_rule_previews']['capital_adaptive']['status'] == 'UNDETERMINED'
    assert out['drawdown_rule_previews']['capital_adaptive']['limit_fraction'] is None
    assert_unarmed(out)
