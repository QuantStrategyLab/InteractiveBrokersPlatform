"""Frozen synthetic helper crosswalk; not broker or full-executor equivalence."""
from types import SimpleNamespace

import pytest

from application.execution_service import (
    _planned_buy_order_quantity, _projected_sell_release_value_for_report,
    _sell_order_quantity,
)
from quant_platform_kit.common.execution_translation import translate_weight_decision_to_value_targets
from quant_platform_kit.common.strategy_contracts import PositionTarget, StrategyDecision
from quant_platform_kit.risk.capital_risk_envelope import apply_combined_scale_to_targets


@pytest.mark.parametrize("symbol,weight,price,expected", [
    ("TQQQ", .24, 35, 6), ("SOXL", .12, 27, 4),
])
def test_post_risk_weight_is_converted_once_to_native_buy_quantity(symbol, weight, price, expected):
    post = apply_combined_scale_to_targets({symbol: weight}, .5)
    decision = StrategyDecision(positions=(PositionTarget(symbol, target_weight=post[symbol]),))
    translated = translate_weight_decision_to_value_targets(decision, total_equity=2000)
    target = translated.positions[0].target_value
    assert target == pytest.approx(2000 * weight * .5)
    quantity, enlarged = _planned_buy_order_quantity(symbol, buy_value=target,
        limit_price=price, quantity_step=1, investable_buying_power=2000)
    assert quantity == expected
    assert enlarged is False
    assert quantity * price <= target
    # No extra factor of three is applied to shares of a leveraged ETF.
    assert quantity * price * 3 > target


@pytest.mark.parametrize("step,budget,price,expected", [
    (1, 99, 10, 9), (.3, 10, 3, 3.3), (1, 9.99, 10, 0),
])
def test_native_floor_never_enlarges_approved_notional(step, budget, price, expected):
    quantity, _ = _planned_buy_order_quantity("TQQQ", buy_value=budget,
        limit_price=price, quantity_step=step, investable_buying_power=1000)
    assert quantity == pytest.approx(expected)
    assert quantity * price <= budget + 1e-12


@pytest.mark.parametrize("price", [None, 0, -1])
def test_absent_or_nonpositive_buy_price_cannot_create_quantity(price):
    quantity, enlarged = _planned_buy_order_quantity("SOXL", buy_value=100,
        limit_price=price, quantity_step=1, investable_buying_power=1000)
    assert quantity == 0
    assert enlarged is False


@pytest.mark.parametrize("price,expected", [(100, 10), (65, 10), (0, 10)])
def test_zero_target_sell_is_bounded_by_held_quantity_not_trigger_price(price, expected):
    quantity = _sell_order_quantity(current_value=10 * price, target_value=0,
        price=price, position_quantity=10, quantity_step=1)
    assert quantity == expected
    # This helper does not admit an executable/fresh quote. Zero-price full-exit
    # sizing is evidence only of held-share mapping, never a valid submission.


@pytest.mark.parametrize("status,filled,price,expected", [
    ("Submitted", 0, None, 0), ("Rejected", 0, None, 0),
    ("Unknown", 0, None, 0), ("PartiallyFilled", 4, 65, 260),
    ("Cancelled", 4, 65, 0), ("Filled", 10, 65, 650),
])
def test_reported_sell_release_is_distinct_from_settled_spendable_cash(status, filled, price, expected):
    report = SimpleNamespace(status=status, filled_quantity=filled,
                             average_fill_price=price, quantity=10)
    projected = _projected_sell_release_value_for_report(report, fallback_price=100)
    assert projected == expected
    assert 10 - filled >= 0
    # The output is an execution-report estimate; there is no settlement clock
    # or account-cash proof in this helper's interface.


def test_native_buy_helper_does_not_establish_cash_admission_by_itself():
    quantity, _ = _planned_buy_order_quantity("TQQQ", buy_value=100,
        limit_price=10, quantity_step=1, investable_buying_power=1)
    assert quantity == 10
    # The caller must cap buy_value first. This freezes the helper's actual
    # contract and prevents a helper pass being called account-cash acceptance.
