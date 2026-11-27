"""Offline Phase 9 PAPER adapter composing QPK HWM, policy v0 sizing, and V7 spread collar.

This module is pure calculation with zero broker, network, or Cloud Run side
effects. It enforces Option A ``policy v0`` whole-share IBKR sizing together
with ``flow_adjusted_hwm`` (#816), external ``FUSE`` gating, and the ``V7``
abnormal-spread collar guard prior to any future ``PAPER`` enablement.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import math

from quant_platform_kit.risk import (
    CorporateActionSplit,
    DEFAULT_SLEEVE_BETA_SHARES,
    ExternalCashFlow,
    FlowAdjustedHwmState,
    FlowAdjustedHwmUpdate,
    PILOT_SYMBOLS,
    POLICY_V0_TIERS,
    PolicyV0SizingResult,
    advance_flow_adjusted_hwm,
    compute_policy_v0_rebalance,
)


@dataclass(frozen=True)
class PilotQuoteSnapshot:
    """Bid/ask quote observation used by the ``V7`` abnormal-spread collar."""

    symbol: str
    bid_usd: float
    ask_usd: float


@dataclass(frozen=True)
class PolicyV0PaperCycleDecision:
    """Offline Phase 9 PAPER cycle decision binding HWM, sizing, and V7 quote health."""

    hwm_update: FlowAdjustedHwmUpdate | None
    sizing_result: PolicyV0SizingResult | None
    v7_spread_collar_tripped: bool
    v7_spread_reasons: tuple[str, ...]
    half_spreads_bps: dict[str, float]
    effective_new_risk_allowed: bool
    fail_closed_reasons: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.fail_closed_reasons and not self.v7_spread_collar_tripped


def _is_finite_pos(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 0.0
    )


def evaluate_v7_spread_collar(
    quotes: Mapping[str, PilotQuoteSnapshot] | None,
    *,
    max_half_spread_bps: float = 25.0,
) -> tuple[bool, tuple[str, ...], dict[str, float]]:
    """Evaluate the ``V7`` fail-closed abnormal-spread collar on ``PILOT_SYMBOLS``.

    Returns ``(tripped, reasons, half_spreads_bps)``. Absent (``None``),
    invalid, crossed, or wide quotes, as well as invalid threshold inputs,
    trip the collar fail-closed so new risk-increasing buys are blocked while
    risk-reducing sells remain permitted when sizing inputs are valid.
    """
    reasons: list[str] = []
    half_spreads_bps: dict[str, float] = {}

    valid_limit = _is_finite_pos(max_half_spread_bps)
    if not valid_limit:
        reasons.append("INVALID_V7_SPREAD_COLLAR_BPS")

    if quotes is None:
        reasons.append("MISSING_V7_QUOTES")
        return True, tuple(reasons), half_spreads_bps
    if not isinstance(quotes, Mapping):
        reasons.append("INVALID_V7_QUOTES_MAPPING")
        return True, tuple(reasons), half_spreads_bps

    limit_bps = float(max_half_spread_bps) if valid_limit else 0.0
    for sym in PILOT_SYMBOLS:
        snap = quotes.get(sym)
        if not isinstance(snap, PilotQuoteSnapshot) or snap.symbol != sym:
            reasons.append(f"MISSING_QUOTE_{sym}")
            continue
        if not (_is_finite_pos(snap.bid_usd) and _is_finite_pos(snap.ask_usd)):
            reasons.append(f"INVALID_QUOTE_{sym}")
            continue
        bid = float(snap.bid_usd)
        ask = float(snap.ask_usd)
        if ask < bid:
            reasons.append(f"CROSSED_QUOTE_{sym}")
            continue
        mid = 0.5 * (bid + ask)
        half_bps = ((ask - bid) / (2.0 * mid)) * 10_000.0
        half_spreads_bps[sym] = half_bps
        if valid_limit and half_bps > limit_bps:
            reasons.append(f"V7_ABNORMAL_SPREAD_{sym}")

    return bool(reasons), tuple(reasons), half_spreads_bps


def compute_policy_v0_paper_cycle(
    *,
    prior_hwm_state: FlowAdjustedHwmState,
    ending_equity_usd: float,
    settled_cash_usd: float,
    prices_usd: Mapping[str, float],
    current_shares: Mapping[str, int],
    quotes: Mapping[str, PilotQuoteSnapshot] | None,
    tier: str = "BALANCED_COMPOUNDING",
    flows: Iterable[ExternalCashFlow] = (),
    splits: Iterable[CorporateActionSplit] = (),
    max_half_spread_bps: float = 25.0,
    fuse_new_risk_allowed: bool = True,
    sleeve_beta_shares: tuple[float, float] = DEFAULT_SLEEVE_BETA_SHARES,
    pricing_plan: str = "PRO_TIERED",
    whole_shares_only: bool = True,
) -> PolicyV0PaperCycleDecision:
    """Compute one deterministic offline Phase 9 PAPER rebalance decision."""
    collar_tripped, collar_reasons, half_spreads_bps = evaluate_v7_spread_collar(
        quotes,
        max_half_spread_bps=max_half_spread_bps,
    )

    tier_spec = POLICY_V0_TIERS.get(tier) if isinstance(tier, str) else None
    if tier_spec is None:
        reasons = tuple(dict.fromkeys(("UNKNOWN_POLICY_V0_TIER", *collar_reasons)))
        return PolicyV0PaperCycleDecision(
            hwm_update=None,
            sizing_result=None,
            v7_spread_collar_tripped=collar_tripped,
            v7_spread_reasons=collar_reasons,
            half_spreads_bps=half_spreads_bps,
            effective_new_risk_allowed=False,
            fail_closed_reasons=reasons,
        )

    hwm_update = advance_flow_adjusted_hwm(
        prior_hwm_state,
        ending_equity_usd=ending_equity_usd,
        flows=flows,
        splits=splits,
        tier_loss_budget_d=tier_spec.drawdown_budget_d,
    )
    if not hwm_update.ok:
        reasons = tuple(dict.fromkeys((*hwm_update.fail_closed_reasons, *collar_reasons)))
        return PolicyV0PaperCycleDecision(
            hwm_update=hwm_update,
            sizing_result=None,
            v7_spread_collar_tripped=collar_tripped,
            v7_spread_reasons=collar_reasons,
            half_spreads_bps=half_spreads_bps,
            effective_new_risk_allowed=False,
            fail_closed_reasons=reasons,
        )

    effective_new_risk = bool(
        isinstance(fuse_new_risk_allowed, bool)
        and fuse_new_risk_allowed
        and not collar_tripped
    )
    sizing_result = compute_policy_v0_rebalance(
        tier=tier,
        equity_usd=hwm_update.next_state.equity_usd,
        settled_cash_usd=settled_cash_usd,
        cushion=hwm_update.cushion,
        prices_usd=prices_usd,
        current_shares=current_shares,
        fuse_new_risk_allowed=effective_new_risk,
        sleeve_beta_shares=sleeve_beta_shares,
        pricing_plan=pricing_plan,
        whole_shares_only=whole_shares_only,
    )
    return PolicyV0PaperCycleDecision(
        hwm_update=hwm_update,
        sizing_result=sizing_result,
        v7_spread_collar_tripped=collar_tripped,
        v7_spread_reasons=collar_reasons,
        half_spreads_bps=half_spreads_bps,
        effective_new_risk_allowed=effective_new_risk,
        fail_closed_reasons=tuple(sizing_result.fail_closed_reasons),
    )


__all__ = (
    "PilotQuoteSnapshot",
    "PolicyV0PaperCycleDecision",
    "compute_policy_v0_paper_cycle",
    "evaluate_v7_spread_collar",
)
