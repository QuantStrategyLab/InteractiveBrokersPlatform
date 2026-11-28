"""Isolated Phase 9 PAPER configuration and offline dry-run guard (Option B).

Keeps the four legacy personal live IBKR Cloud Run services completely untouched
by enforcing a physically separated ``paper`` service name, ``DU*`` simulated
account selector, ``PRO_TIERED`` whole-share sizing, and all ``live=OFF`` /
``kelly=OFF`` / ``stop_loss_return=OFF`` / ``H=0`` safety invariants.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import math
import re
from urllib.parse import urlsplit

from interactive_brokers_platform.application.policy_v0_paper_adapter import (
    PilotQuoteSnapshot,
    PolicyV0PaperCycleDecision,
    compute_policy_v0_paper_cycle,
)
from quant_platform_kit.risk import (
    CorporateActionSplit,
    DEFAULT_SLEEVE_BETA_SHARES,
    ExecutionCostDriftConfig,
    ExecutionCostDriftReport,
    ExternalCashFlow,
    FillCostObservation,
    FlowAdjustedHwmState,
    NavCashFlowObservation,
    POLICY_V0_TIERS,
    TwrCapitalLadderReport,
    evaluate_execution_cost_drift,
    evaluate_twr_and_capital_ladder,
)

_SERVICE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_PAPER_ACCOUNT_RE = re.compile(r"^DU[0-9]+$")


@dataclass(frozen=True)
class IsolatedPaperServiceConfig:
    """Configuration for a strictly isolated IBKR Phase 9 PAPER service."""

    service_name: str
    account_id: str
    execution_mode: str = "paper"
    tier: str = "BALANCED_COMPOUNDING"
    commission_schedule: str = "PRO_TIERED"
    whole_share_only: bool = True
    live_enabled: bool = False
    paper_enabled: bool = False
    kelly_enabled: bool = False
    stop_loss_return_enabled: bool = False
    external_reserve_h_usd: float = 0.0
    margin_enabled: bool = False
    options_enabled: bool = False
    futures_enabled: bool = False
    state_prefix: str | None = None


@dataclass(frozen=True)
class IsolatedPaperDryRunReport:
    """Offline Phase 9 isolated paper dry-run combining sizing, cost drift, and TWR."""

    config_valid: bool
    config_errors: tuple[str, ...]
    cycle_decision: PolicyV0PaperCycleDecision | None
    cost_drift: ExecutionCostDriftReport | None
    twr_ladder: TwrCapitalLadderReport | None
    promotion_paused: bool


def _valid_gcs_prefix(uri: str) -> bool:
    if not isinstance(uri, str) or not uri or uri != uri.strip():
        return False
    if any(ch in uri for ch in ("\n", "\r")):
        return False
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return False
    return (
        parsed.scheme == "gs"
        and bool(parsed.netloc)
        and bool(parsed.path.strip("/"))
        and not parsed.query
        and not parsed.fragment
    )


_REQUIRED_FALSE_FLAGS: tuple[tuple[str, str], ...] = (
    ("live_enabled", "LIVE_MUST_REMAIN_OFF"),
    ("kelly_enabled", "KELLY_MUST_REMAIN_OFF"),
    ("stop_loss_return_enabled", "STOP_LOSS_RETURN_MUST_REMAIN_OFF"),
    ("margin_enabled", "MARGIN_MUST_BE_DISABLED"),
    ("options_enabled", "OPTIONS_MUST_BE_DISABLED"),
    ("futures_enabled", "FUTURES_MUST_BE_DISABLED"),
)


def _is_zero_reserve(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) == 0.0
    )


def validate_isolated_paper_config(config: IsolatedPaperServiceConfig) -> tuple[str, ...]:
    """Return fail-closed violation codes for an isolated IBKR paper service config."""
    checks: tuple[tuple[bool, str], ...] = (
        (
            isinstance(config.service_name, str)
            and bool(_SERVICE_NAME_RE.fullmatch(config.service_name))
            and "paper" in config.service_name,
            "INVALID_ISOLATED_PAPER_SERVICE_NAME",
        ),
        (
            isinstance(config.account_id, str)
            and bool(_PAPER_ACCOUNT_RE.fullmatch(config.account_id)),
            "NON_PAPER_OR_INVALID_ACCOUNT_ID",
        ),
        (config.execution_mode == "paper", "EXECUTION_MODE_MUST_BE_PAPER"),
        (isinstance(config.tier, str) and config.tier in POLICY_V0_TIERS, "UNKNOWN_POLICY_V0_TIER"),
        (config.commission_schedule == "PRO_TIERED", "UNSUPPORTED_COMMISSION_SCHEDULE"),
        (config.whole_share_only is True, "WHOLE_SHARE_API_REQUIRED"),
        (isinstance(config.paper_enabled, bool), "INVALID_PAPER_ENABLED_FLAG"),
        (_is_zero_reserve(config.external_reserve_h_usd), "EXTERNAL_RESERVE_H_MUST_BE_ZERO"),
        (
            config.state_prefix is None or _valid_gcs_prefix(config.state_prefix),
            "INVALID_STATE_PREFIX_URI",
        ),
    )
    errors = [code for ok, code in checks if not ok]
    for attr, code in _REQUIRED_FALSE_FLAGS:
        if getattr(config, attr) is not False:
            errors.append(code)
    return tuple(errors)


def run_isolated_paper_dry_run(
    *,
    config: IsolatedPaperServiceConfig,
    prior_hwm_state: FlowAdjustedHwmState,
    ending_equity_usd: float,
    settled_cash_usd: float,
    prices_usd: Mapping[str, float],
    current_shares: Mapping[str, int],
    quotes: Mapping[str, PilotQuoteSnapshot] | None,
    flows: Iterable[ExternalCashFlow] = (),
    splits: Iterable[CorporateActionSplit] = (),
    max_half_spread_bps: float = 25.0,
    fuse_new_risk_allowed: bool = True,
    sleeve_beta_shares: tuple[float, float] = DEFAULT_SLEEVE_BETA_SHARES,
    fill_observations: Iterable[FillCostObservation] = (),
    cost_drift_config: ExecutionCostDriftConfig | None = None,
    nav_observations: Iterable[NavCashFlowObservation] = (),
) -> IsolatedPaperDryRunReport:
    """Validate isolated paper guardrails and run one full offline P0/P1 dry-run."""
    config_errors = validate_isolated_paper_config(config)
    if config_errors:
        return IsolatedPaperDryRunReport(
            config_valid=False,
            config_errors=config_errors,
            cycle_decision=None,
            cost_drift=None,
            twr_ladder=None,
            promotion_paused=True,
        )

    cycle = compute_policy_v0_paper_cycle(
        prior_hwm_state=prior_hwm_state,
        ending_equity_usd=ending_equity_usd,
        settled_cash_usd=settled_cash_usd,
        prices_usd=prices_usd,
        current_shares=current_shares,
        quotes=quotes,
        tier=config.tier,
        flows=flows,
        splits=splits,
        max_half_spread_bps=max_half_spread_bps,
        fuse_new_risk_allowed=fuse_new_risk_allowed,
        sleeve_beta_shares=sleeve_beta_shares,
        pricing_plan=config.commission_schedule,
        whole_shares_only=config.whole_share_only,
    )

    fill_list = list(fill_observations)
    cost_drift = (
        evaluate_execution_cost_drift(fill_list, config=cost_drift_config)
        if fill_list
        else None
    )

    nav_list = list(nav_observations)
    twr_ladder = evaluate_twr_and_capital_ladder(nav_list) if nav_list else None

    paused = bool(
        (not cycle.ok)
        or (cost_drift is not None and cost_drift.pause_promotion)
    )
    return IsolatedPaperDryRunReport(
        config_valid=True,
        config_errors=(),
        cycle_decision=cycle,
        cost_drift=cost_drift,
        twr_ladder=twr_ladder,
        promotion_paused=paused,
    )


__all__ = [
    "IsolatedPaperDryRunReport",
    "IsolatedPaperServiceConfig",
    "run_isolated_paper_dry_run",
    "validate_isolated_paper_config",
]
