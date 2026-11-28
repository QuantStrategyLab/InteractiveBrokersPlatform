"""Unit tests for isolated Phase 9 IBKR paper config & offline dry-run guard."""

from __future__ import annotations

from datetime import date
import math

import pytest

from interactive_brokers_platform.application.isolated_paper_service_config import (
    IsolatedPaperServiceConfig,
    run_isolated_paper_dry_run,
    validate_isolated_paper_config,
)
from interactive_brokers_platform.application.policy_v0_paper_adapter import PilotQuoteSnapshot
from quant_platform_kit.risk import (
    ExecutionCostDriftConfig,
    FillCostObservation,
    FlowAdjustedHwmState,
    NavCashFlowObservation,
)


def _valid_config(**overrides: object) -> IsolatedPaperServiceConfig:
    base: dict[str, object] = {
        "service_name": "ibkr-policy-v0-paper-service",
        "account_id": "DU0000001",
        "state_prefix": "gs://example-private-paper/ibkr/policy-v0",
    }
    base.update(overrides)
    return IsolatedPaperServiceConfig(**base)  # type: ignore[arg-type]


def _healthy_quotes() -> dict[str, PilotQuoteSnapshot]:
    return {
        "TQQQ": PilotQuoteSnapshot(symbol="TQQQ", bid_usd=79.98, ask_usd=80.02),
        "SOXL": PilotQuoteSnapshot(symbol="SOXL", bid_usd=29.99, ask_usd=30.01),
    }


def test_valid_isolated_paper_config_and_full_dry_run() -> None:
    cfg = _valid_config()
    assert validate_isolated_paper_config(cfg) == ()

    report = run_isolated_paper_dry_run(
        config=cfg,
        prior_hwm_state=FlowAdjustedHwmState(equity_usd=1_500.0, governed_hwm_usd=1_500.0),
        ending_equity_usd=1_500.0,
        settled_cash_usd=1_500.0,
        prices_usd={"TQQQ": 80.0, "SOXL": 30.0},
        current_shares={"TQQQ": 0, "SOXL": 0},
        quotes=_healthy_quotes(),
        fill_observations=[
            FillCostObservation(date(2026, 11, i), "TQQQ", "BUY", 2.0, 80.0, 80.02, 0.35, 0.35)
            for i in range(1, 6)
        ],
        nav_observations=[
            NavCashFlowObservation(date(2026, 11, 2), 1_000.0, 1_000.0),
            NavCashFlowObservation(date(2026, 11, 3), 1_500.0, 500.0),
        ],
    )
    assert report.config_valid is True
    assert report.config_errors == ()
    assert report.cycle_decision is not None and report.cycle_decision.ok is True
    assert report.cost_drift is not None and report.cost_drift.pause_promotion is False
    assert report.twr_ladder is not None
    assert math.isclose(report.twr_ladder.twr_index, 1.0, rel_tol=1e-12)
    assert report.twr_ladder.current_tier.name == "PILOT"
    assert report.promotion_paused is False


@pytest.mark.parametrize(
    ("overrides", "expected_error"),
    [
        ({"service_name": "ibkr-live-service"}, "INVALID_ISOLATED_PAPER_SERVICE_NAME"),
        ({"account_id": "U0000001"}, "NON_PAPER_OR_INVALID_ACCOUNT_ID"),
        ({"account_id": "BAD"}, "NON_PAPER_OR_INVALID_ACCOUNT_ID"),
        ({"execution_mode": "live"}, "EXECUTION_MODE_MUST_BE_PAPER"),
        ({"tier": "UNKNOWN"}, "UNKNOWN_POLICY_V0_TIER"),
        ({"commission_schedule": "PRO_FIXED"}, "UNSUPPORTED_COMMISSION_SCHEDULE"),
        ({"whole_share_only": False}, "WHOLE_SHARE_API_REQUIRED"),
        ({"live_enabled": True}, "LIVE_MUST_REMAIN_OFF"),
        ({"kelly_enabled": True}, "KELLY_MUST_REMAIN_OFF"),
        ({"stop_loss_return_enabled": True}, "STOP_LOSS_RETURN_MUST_REMAIN_OFF"),
        ({"external_reserve_h_usd": 500.0}, "EXTERNAL_RESERVE_H_MUST_BE_ZERO"),
        ({"margin_enabled": True}, "MARGIN_MUST_BE_DISABLED"),
        ({"options_enabled": True}, "OPTIONS_MUST_BE_DISABLED"),
        ({"futures_enabled": True}, "FUTURES_MUST_BE_DISABLED"),
        ({"state_prefix": "https://example.invalid/bad"}, "INVALID_STATE_PREFIX_URI"),
    ],
)
def test_unsafe_or_live_paper_config_fails_closed(
    overrides: dict[str, object],
    expected_error: str,
) -> None:
    cfg = _valid_config(**overrides)
    errors = validate_isolated_paper_config(cfg)
    assert expected_error in errors

    report = run_isolated_paper_dry_run(
        config=cfg,
        prior_hwm_state=FlowAdjustedHwmState(equity_usd=1_500.0, governed_hwm_usd=1_500.0),
        ending_equity_usd=1_500.0,
        settled_cash_usd=1_500.0,
        prices_usd={"TQQQ": 80.0, "SOXL": 30.0},
        current_shares={"TQQQ": 0, "SOXL": 0},
        quotes=_healthy_quotes(),
    )
    assert report.config_valid is False
    assert expected_error in report.config_errors
    assert report.cycle_decision is None
    assert report.promotion_paused is True


def test_cost_drift_breach_pauses_isolated_paper_promotion() -> None:
    report = run_isolated_paper_dry_run(
        config=_valid_config(),
        prior_hwm_state=FlowAdjustedHwmState(equity_usd=1_500.0, governed_hwm_usd=1_500.0),
        ending_equity_usd=1_500.0,
        settled_cash_usd=1_500.0,
        prices_usd={"TQQQ": 80.0, "SOXL": 30.0},
        current_shares={"TQQQ": 0, "SOXL": 0},
        quotes=_healthy_quotes(),
        fill_observations=[
            FillCostObservation(date(2026, 11, i), "TQQQ", "BUY", 2.0, 80.0, 80.30, 0.35, 0.70)
            for i in range(1, 6)
        ],
        cost_drift_config=ExecutionCostDriftConfig(min_sample_count=5),
    )
    assert report.config_valid is True
    assert report.cost_drift is not None and report.cost_drift.pause_promotion is True
    assert report.promotion_paused is True
