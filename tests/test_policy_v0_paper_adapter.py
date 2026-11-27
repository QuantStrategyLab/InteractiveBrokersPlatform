"""Unit tests for the offline Phase 9 PAPER policy v0 + V7 spread collar adapter."""

from __future__ import annotations

import pytest

from interactive_brokers_platform.application.policy_v0_paper_adapter import (
    PilotQuoteSnapshot,
    compute_policy_v0_paper_cycle,
    evaluate_v7_spread_collar,
)
from quant_platform_kit.risk import ExternalCashFlow, FlowAdjustedHwmState


def test_normal_balanced_cycle_with_inflow_and_valid_quotes() -> None:
    prior = FlowAdjustedHwmState(equity_usd=10_000.0, governed_hwm_usd=10_000.0)
    quotes = {
        "TQQQ": PilotQuoteSnapshot(symbol="TQQQ", bid_usd=79.98, ask_usd=80.02),
        "SOXL": PilotQuoteSnapshot(symbol="SOXL", bid_usd=29.99, ask_usd=30.01),
    }
    decision = compute_policy_v0_paper_cycle(
        prior_hwm_state=prior,
        ending_equity_usd=12_000.0,
        settled_cash_usd=12_000.0,
        flows=(ExternalCashFlow(amount_usd=2_000.0, is_end_of_session=True),),
        prices_usd={"TQQQ": 80.0, "SOXL": 30.0},
        current_shares={"TQQQ": 0, "SOXL": 0},
        quotes=quotes,
    )
    assert decision.ok
    assert decision.effective_new_risk_allowed is True
    assert decision.v7_spread_collar_tripped is False
    # Zero investment drawdown across ending-boundary deposit preserves full cushion=0.30.
    assert decision.hwm_update.cushion == pytest.approx(0.30)
    assert decision.sizing_result.target_effective_exposure == pytest.approx(0.27)
    assert decision.sizing_result.safety_invariants.live is False
    assert decision.sizing_result.safety_invariants.whole_shares_only is True
    assert decision.sizing_result.proposals["TQQQ"].delta_shares > 0
    assert decision.sizing_result.proposals["SOXL"].delta_shares > 0


def test_v7_abnormal_spread_blocks_buys_but_permits_sells() -> None:
    prior = FlowAdjustedHwmState(equity_usd=10_000.0, governed_hwm_usd=10_000.0)
    wide_quotes = {
        # Half-spread ~50 bps > 25 bps limit on TQQQ.
        "TQQQ": PilotQuoteSnapshot(symbol="TQQQ", bid_usd=49.75, ask_usd=50.25),
        "SOXL": PilotQuoteSnapshot(symbol="SOXL", bid_usd=49.99, ask_usd=50.01),
    }
    decision = compute_policy_v0_paper_cycle(
        prior_hwm_state=prior,
        ending_equity_usd=10_000.0,
        settled_cash_usd=5_000.0,
        prices_usd={"TQQQ": 50.0, "SOXL": 50.0},
        current_shares={"TQQQ": 0, "SOXL": 30},
        quotes=wide_quotes,
        max_half_spread_bps=25.0,
    )
    assert not decision.ok
    assert decision.v7_spread_collar_tripped is True
    assert "V7_ABNORMAL_SPREAD_TQQQ" in decision.v7_spread_reasons
    assert decision.effective_new_risk_allowed is False
    # Buys blocked fail-closed; overweight SOXL still trims down to target (18 shares).
    assert decision.sizing_result.proposals["TQQQ"].suppressed_by_fuse is True
    assert decision.sizing_result.proposals["TQQQ"].delta_shares == 0
    assert decision.sizing_result.proposals["SOXL"].suppressed_by_fuse is False
    assert decision.sizing_result.proposals["SOXL"].delta_shares == 18 - 30


def test_v7_rejects_crossed_and_missing_quotes() -> None:
    crossed = {
        "TQQQ": PilotQuoteSnapshot(symbol="TQQQ", bid_usd=50.10, ask_usd=50.00),
    }
    tripped, reasons, _ = evaluate_v7_spread_collar(crossed)
    assert tripped is True
    assert "CROSSED_QUOTE_TQQQ" in reasons
    assert "MISSING_QUOTE_SOXL" in reasons


def test_hwm_fail_closed_proposes_zero_orders() -> None:
    prior = FlowAdjustedHwmState(equity_usd=10_000.0, governed_hwm_usd=10_000.0)
    decision = compute_policy_v0_paper_cycle(
        prior_hwm_state=prior,
        ending_equity_usd=10_500.0,
        settled_cash_usd=5_000.0,
        flows=(ExternalCashFlow(amount_usd=500.0, pre_flow_equity_usd=None, is_end_of_session=False),),
        prices_usd={"TQQQ": 50.0, "SOXL": 50.0},
        current_shares={"TQQQ": 10, "SOXL": 10},
    )
    assert not decision.ok
    assert "O1_MISSING_PRE_FLOW_VALUATION_FAIL_CLOSED" in decision.fail_closed_reasons
    assert decision.sizing_result.proposals == {}
    assert decision.effective_new_risk_allowed is False
