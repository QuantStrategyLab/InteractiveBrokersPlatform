"""Tests for IBKR account_new_risk_gate W1 wiring (fail-closed, no broker I/O)."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest import mock

import pytest
from quant_platform_kit.common.models import OrderIntent
from quant_platform_kit.risk.account_new_risk_gate import (
    InjectedReconciliationSnapshot,
    NewRiskDisposition,
)

from interactive_brokers_platform.application.account_new_risk_gate_support import (
    ACCOUNT_NEW_RISK_GATE_ENV,
    apply_combined_scale_to_target_weights,
    build_account_new_risk_snapshot,
    build_portfolio_from_account_values,
    build_snapshot_from_portfolio,
    evaluate_account_values_new_risk_admission,
    evaluate_cycle_new_risk_admission,
    evaluate_portfolio_new_risk_admission,
    is_account_new_risk_gate_enabled,
    maybe_publish_attention_for_admission,
    new_risk_buy_prohibited,
    reset_attention_sent_keys_for_tests,
    set_cycle_snapshot,
)
from interactive_brokers_platform.application.ibkr_order_execution import submit_order_intent
from interactive_brokers_platform.application.rebalance_service import _snapshot_to_portfolio_view, _strategy_portfolio_view


@pytest.fixture(autouse=True)
def _clear_cycle_snapshot():
    set_cycle_snapshot(None)
    reset_attention_sent_keys_for_tests()
    for key in (
        "IBKR_MAX_DAILY_LOSS_USD",
        "MAX_DAILY_LOSS_USD",
        "RUNTIME_TARGET_JSON",
    ):
        os.environ.pop(key, None)
    yield
    set_cycle_snapshot(None)
    for key in (
        "IBKR_MAX_DAILY_LOSS_USD",
        "MAX_DAILY_LOSS_USD",
        "RUNTIME_TARGET_JSON",
    ):
        os.environ.pop(key, None)


def test_gate_enabled_by_default():
    with mock.patch.dict(os.environ, {}, clear=True):
        assert is_account_new_risk_gate_enabled() is True


def test_gate_disabled_when_env_zero():
    with mock.patch.dict(os.environ, {ACCOUNT_NEW_RISK_GATE_ENV: "0"}, clear=False):
        assert is_account_new_risk_gate_enabled() is False


def test_missing_equity_prohibits_new_risk():
    # Rule A represents unmeasured drawdown as None and retains the existing unavailable-equity prohibition.
    # Missing equity keeps observation_ok=False, which still fails closed via
    # EQUITY_UNKNOWN_FAIL_CLOSED in the gate below -- the circuit-breaker axis
    # is not durably OPEN here since there is no explicit unknown-pending /
    # durable-breaker evidence this cycle.
    assert build_account_new_risk_snapshot({}) == {
        "observation_status": "UNAVAILABLE",
        "reconciliation_status": "UNVERIFIED",
        "circuit_breaker_state": "CLOSED",
        "equity_usd": None,
        "drawdown_from_peak": None,
    }
    result = evaluate_account_values_new_risk_admission({"equity": 0.0})
    assert result.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
    assert "EQUITY_UNKNOWN_FAIL_CLOSED" in result.reason_codes
    assert result.live_authority_granted is False


def test_drawdown_brake_prohibits_new_risk():
    portfolio = {
        "total_equity": 80_000.0,
        "peak_equity_usd": 100_000.0,
    }
    result = evaluate_portfolio_new_risk_admission(portfolio)
    assert result.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
    assert result.live_authority_granted is False


def test_healthy_equity_without_explicit_snapshot_allows_new_risk():
    # A plain healthy-equity portfolio (no explicit snapshot, no drawdown)
    # now derives COMPLETE/VERIFIED/CLOSED via cycle-health instead of
    # failing closed on the old blanket UNAVAILABLE/UNVERIFIED/OPEN defaults.
    portfolio = {
        "total_equity": 100_000.0,
        "peak_equity_usd": 100_000.0,
    }
    with mock.patch(
        "interactive_brokers_platform.application.account_new_risk_gate_support.resolve_production_drift_status_from_store",
        return_value=None,
    ):
        result = evaluate_portfolio_new_risk_admission(portfolio)
    assert result.disposition == NewRiskDisposition.ALLOW_NEW_RISK


def test_explicit_critical_production_drift_prohibits_new_risk():
    portfolio = {
        "total_equity": 100_000.0,
        "peak_equity_usd": 100_000.0,
        "production_drift_status": "critical",
    }
    result = evaluate_portfolio_new_risk_admission(portfolio)
    assert result.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
    assert "PRODUCTION_DRIFT_CRITICAL" in result.reason_codes


def test_store_critical_production_drift_prohibits_new_risk():
    portfolio = {
        "total_equity": 100_000.0,
        "peak_equity_usd": 100_000.0,
    }
    with mock.patch(
        "interactive_brokers_platform.application.account_new_risk_gate_support.resolve_production_drift_status_from_store",
        return_value="critical",
    ) as store_mock:
        result = evaluate_portfolio_new_risk_admission(portfolio)
    store_mock.assert_called_once()
    assert result.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
    assert "PRODUCTION_DRIFT_CRITICAL" in result.reason_codes


def test_store_probe_failure_is_fail_soft_not_invented_ban():
    portfolio = {
        "total_equity": 100_000.0,
        "peak_equity_usd": 100_000.0,
    }
    with mock.patch(
        "interactive_brokers_platform.application.account_new_risk_gate_support.resolve_production_drift_status_from_store",
        return_value=None,
    ):
        result = evaluate_portfolio_new_risk_admission(portfolio)
    assert result.disposition == NewRiskDisposition.ALLOW_NEW_RISK


def test_explicit_healthy_snapshot_allows_new_risk():
    portfolio = {
        "total_equity": 100_000.0,
        "peak_equity_usd": 100_000.0,
        "account_new_risk_snapshot": {
            "observation_status": "COMPLETE",
            "reconciliation_status": "VERIFIED",
            "circuit_breaker_state": "CLOSED",
        },
    }
    result = evaluate_portfolio_new_risk_admission(portfolio)
    assert result.disposition == NewRiskDisposition.ALLOW_NEW_RISK
    assert result.live_authority_granted is False


def test_explicit_daily_loss_at_limit_prohibits_buy():
    portfolio = {
        "total_equity": 50_000.0,
        "peak_equity_usd": 50_000.0,
        "account_new_risk_snapshot": {
            "daily_loss_usd": 100.0,
            "max_daily_loss_usd": 100.0,
        },
    }
    with mock.patch(
        "interactive_brokers_platform.application.account_new_risk_gate_support.resolve_production_drift_status_from_store",
        return_value=None,
    ):
        snapshot = build_snapshot_from_portfolio(portfolio)
        result = evaluate_portfolio_new_risk_admission(portfolio)
    assert snapshot.daily_loss_usd == 100.0
    assert result.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
    assert "DAILY_LOSS_LIMIT_EXCEEDED" in result.reason_codes
    assert new_risk_buy_prohibited(result)


def test_unconfigured_daily_loss_limit_omits_axis():
    portfolio = {
        "total_equity": 50_000.0,
        "peak_equity_usd": 50_000.0,
        # daily_loss fact absent / invalid must not invent a prohibit when
        # no max_daily_loss_usd is configured.
        "account_new_risk_snapshot": {"daily_loss_usd": float("nan")},
    }
    with mock.patch(
        "interactive_brokers_platform.application.account_new_risk_gate_support.resolve_production_drift_status_from_store",
        return_value=None,
    ):
        result = evaluate_portfolio_new_risk_admission(portfolio)
    assert result.disposition == NewRiskDisposition.ALLOW_NEW_RISK
    assert "DAILY_LOSS_UNKNOWN_FAIL_CLOSED" not in result.reason_codes
    assert "DAILY_LOSS_LIMIT_EXCEEDED" not in result.reason_codes


def test_configured_limit_without_daily_loss_fact_fails_closed():
    portfolio = {
        "total_equity": 50_000.0,
        "peak_equity_usd": 50_000.0,
        "account_new_risk_snapshot": {"max_daily_loss_usd": 100.0},
    }
    with mock.patch(
        "interactive_brokers_platform.application.account_new_risk_gate_support.resolve_production_drift_status_from_store",
        return_value=None,
    ):
        result = evaluate_portfolio_new_risk_admission(portfolio)
    assert result.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
    assert "DAILY_LOSS_UNKNOWN_FAIL_CLOSED" in result.reason_codes


def test_unknown_pending_orders_prohibits_and_opens_breaker():
    # Rule A requires peak evidence; equal NAV and peak isolate the pending-order OPEN invariant.
    portfolio = {
        "total_equity": 50_000.0,
        "peak_equity_usd": 50_000.0,
        "unknown_pending_orders": True,
    }
    snapshot = build_account_new_risk_snapshot(portfolio)
    assert snapshot["observation_status"] == "COMPLETE"
    assert snapshot["reconciliation_status"] == "UNVERIFIED"
    assert snapshot["circuit_breaker_state"] == "OPEN"
    result = evaluate_portfolio_new_risk_admission(portfolio)
    assert result.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
    assert "CIRCUIT_BREAKER_OPEN" in result.reason_codes
    assert "RECONCILIATION_NOT_VERIFIED" in result.reason_codes


def test_durable_circuit_breaker_open_prohibits_new_risk():
    portfolio = {
        "total_equity": 50_000.0,
        "durable_circuit_breaker_state": "OPEN",
    }
    snapshot = build_account_new_risk_snapshot(portfolio)
    assert snapshot["circuit_breaker_state"] == "OPEN"
    result = evaluate_portfolio_new_risk_admission(portfolio)
    assert result.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
    assert "CIRCUIT_BREAKER_OPEN" in result.reason_codes


@pytest.mark.parametrize(
    ("metadata", "reason"),
    [
        ({"durable_circuit_breaker_state": "OPEN"}, "CIRCUIT_BREAKER_OPEN"),
        ({"unknown_pending_orders": True}, "RECONCILIATION_NOT_VERIFIED"),
    ],
)
def test_rebalance_snapshot_risk_state_reaches_new_risk_gate(monkeypatch, metadata, reason):
    monkeypatch.setattr(
        "interactive_brokers_platform.application.account_new_risk_gate_support.resolve_production_drift_status_from_store",
        lambda **kwargs: None,
    )
    snapshot = SimpleNamespace(positions=(), total_equity=5000.0, buying_power=5000.0,
        metadata={"currency": "USD", **metadata})
    positions, account_values = _snapshot_to_portfolio_view(snapshot)
    _, account_values = _strategy_portfolio_view(positions, account_values, ("TQQQ",))
    result = evaluate_account_values_new_risk_admission(account_values,
        signal_metadata={"durable_circuit_breaker_state": "CLOSED", "unknown_pending_orders": False})
    assert result.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
    assert reason in result.reason_codes
    assert result.live_authority_granted is False
    assert snapshot.metadata == {"currency": "USD", **metadata}


@pytest.mark.parametrize("metadata", [
    {},
    {"durable_circuit_breaker_state": "CLOSED", "unknown_pending_orders": False},
])
def test_rebalance_snapshot_preserves_absent_and_explicit_risk_fields(metadata):
    snapshot = SimpleNamespace(positions=(), total_equity=5000.0, buying_power=5000.0,
        metadata={"currency": "USD", **metadata})
    _, account_values = _snapshot_to_portfolio_view(snapshot)
    for key in ("durable_circuit_breaker_state", "unknown_pending_orders"):
        assert (key in account_values) == (key in metadata)
        if key in metadata:
            assert account_values[key] == metadata[key]


def test_build_portfolio_from_account_values_maps_equity():
    # Rule A copies peak before projection; (55,000 - 50,000) / 55,000 = 1/11 is independently derived drawdown.
    portfolio = build_portfolio_from_account_values(
        {"equity": 50_000.0},
        signal_metadata={"peak_equity_usd": 55_000.0},
    )
    assert portfolio["total_equity"] == 50_000.0
    assert portfolio["peak_equity_usd"] == 55_000.0
    assert portfolio["account_new_risk_snapshot"] == {
        "observation_status": "COMPLETE",
        "reconciliation_status": "VERIFIED",
        "circuit_breaker_state": "CLOSED",
        "equity_usd": 50_000.0,
        "peak_equity_usd": 55_000.0,
        "drawdown_from_peak": 1 / 11,
    }


def test_combined_scale_halves_target_weights():
    assert apply_combined_scale_to_target_weights({"TQQQ": 0.8, "QQQ": 0.2}, 0.5) == {
        "TQQQ": 0.4,
        "QQQ": 0.1,
    }


def test_missing_combined_scale_leaves_target_weights():
    assert apply_combined_scale_to_target_weights({"TQQQ": 0.8}, None) == {"TQQQ": 0.8}


def test_submit_order_intent_rejects_buy_when_gate_prohibits():
    set_cycle_snapshot(
        InjectedReconciliationSnapshot(
            observation_status="COMPLETE",
            reconciliation_status="VERIFIED",
            circuit_breaker_state="CLOSED",
            equity_usd=None,
        )
    )
    ib = SimpleNamespace()
    with mock.patch(
        "interactive_brokers_platform.application.ibkr_order_execution._submit_order_intent",
    ) as submit_mock:
        report = submit_order_intent(
            ib,
            OrderIntent(symbol="SPY", side="buy", quantity=1.0),
        )
    submit_mock.assert_not_called()
    assert report.status == "rejected"
    assert report.raw_payload.get("detail") == "account_new_risk_gate"
    assert "EQUITY_UNKNOWN_FAIL_CLOSED" in report.raw_payload.get("reason_codes", [])


def test_submit_order_intent_does_not_scale_buy_quantity():
    """Envelope scale applies to target weights, not submit-time quantity."""
    set_cycle_snapshot(
        InjectedReconciliationSnapshot(
            observation_status="COMPLETE",
            reconciliation_status="VERIFIED",
            circuit_breaker_state="CLOSED",
            equity_usd=40_000.0,
            drawdown_from_peak=0.075,
        )
    )
    expected = SimpleNamespace(status="Submitted")
    with mock.patch(
        "interactive_brokers_platform.application.ibkr_order_execution._submit_order_intent",
        return_value=expected,
    ) as submit_mock:
        submit_order_intent(
            SimpleNamespace(),
            OrderIntent(symbol="SPY", side="buy", quantity=4.0),
        )
    assert submit_mock.call_args.args[1].quantity == 4.0


def test_submit_order_intent_allows_sell_when_gate_prohibits():
    set_cycle_snapshot(
        InjectedReconciliationSnapshot(
            observation_status="COMPLETE",
            reconciliation_status="VERIFIED",
            circuit_breaker_state="CLOSED",
            equity_usd=None,
        )
    )
    ib = SimpleNamespace()
    expected = SimpleNamespace(status="Submitted")
    with mock.patch(
        "interactive_brokers_platform.application.ibkr_order_execution._submit_order_intent",
        return_value=expected,
    ) as submit_mock:
        submit_order_intent(
            ib,
            OrderIntent(symbol="SPY", side="sell", quantity=1.0),
            reduction_position=2.0, reduction_pending_quantity=0.0,
        )
    submit_mock.assert_called_once()
    assert submit_mock.call_args.args[1].quantity == 1.0


def test_submit_order_intent_skips_gate_when_disabled():
    set_cycle_snapshot(
        InjectedReconciliationSnapshot(
            observation_status="COMPLETE",
            reconciliation_status="VERIFIED",
            circuit_breaker_state="CLOSED",
            equity_usd=None,
        )
    )
    ib = SimpleNamespace()
    expected = SimpleNamespace(status="Submitted")
    with mock.patch.dict(os.environ, {ACCOUNT_NEW_RISK_GATE_ENV: "0"}, clear=False):
        with mock.patch(
            "interactive_brokers_platform.application.ibkr_order_execution._submit_order_intent",
            return_value=expected,
        ) as submit_mock:
            submit_order_intent(
                ib,
                OrderIntent(symbol="SPY", side="buy", quantity=1.0),
            )
    submit_mock.assert_called_once()


def test_cycle_gate_without_snapshot_is_fail_closed():
    result = evaluate_cycle_new_risk_admission()
    assert result.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
    assert "EQUITY_UNKNOWN_FAIL_CLOSED" in result.reason_codes

def test_attention_notify_on_new_risk_prohibit_dedupes(monkeypatch):
    import sys
    from pathlib import Path

    qpk = Path(__file__).resolve().parents[2] / "QuantPlatformKit" / "src"
    if qpk.exists() and str(qpk) not in sys.path:
        sys.path.insert(0, str(qpk))

    reset_attention_sent_keys_for_tests()
    portfolio = {
        "total_equity": 50_000.0,
        "strategy_profile": "soxl_soxx_trend_income",
        "account_id": "U00000005",
        "account_new_risk_snapshot": {"production_drift_status": "critical"},
    }
    admission = evaluate_portfolio_new_risk_admission(portfolio)
    assert new_risk_buy_prohibited(admission)
    snapshot = build_snapshot_from_portfolio(portfolio)
    payloads: list[str] = []

    def _sender(*, text: str, alert_key: str | None = None, **_kwargs) -> bool:
        payloads.append(text)
        return True

    counts = maybe_publish_attention_for_admission(
        admission,
        portfolio=portfolio,
        snapshot=snapshot,
        telegram_sender=_sender,
        log_message=lambda *_a, **_k: None,
    )
    assert counts.get("sent") == 1
    counts2 = maybe_publish_attention_for_admission(
        admission,
        portfolio=portfolio,
        snapshot=snapshot,
        telegram_sender=_sender,
        log_message=lambda *_a, **_k: None,
    )
    assert counts2.get("sent") == 0
    assert counts2.get("skipped") == 1
    assert len(payloads) == 1



def test_invalid_risk_projection_is_json_safe_and_preserves_independent_brake():
    # Rule A ignores invalid cached drawdown: measured 0.2 trips the brake while invalid volatility still fails closed.
    import json
    from interactive_brokers_platform.application.account_new_risk_gate_support import build_account_new_risk_snapshot, evaluate_portfolio_new_risk_admission
    portfolio = {"total_equity": 40000, "peak_equity_usd": 50000, "drawdown_from_peak": float("nan"), "realized_vol": float("nan")}
    projection = build_account_new_risk_snapshot(portfolio)
    json.dumps(projection, allow_nan=False)
    assert build_account_new_risk_snapshot({**portfolio, "account_new_risk_snapshot": projection}) == projection
    result = evaluate_portfolio_new_risk_admission(portfolio)
    assert result.risk_input_invalid and result.hard_brake_triggered
    assert result.combined_scale == 0.0
    assert "INVALID_DRAWDOWN_FAIL_CLOSED" not in result.reason_codes
    assert "INVALID_REALIZED_VOL_FAIL_CLOSED" in result.reason_codes
    assert "DRAWDOWN_BRAKE_TRIPPED" in result.reason_codes


def test_invalid_vol_prohibits_without_zero_scale():
    from interactive_brokers_platform.application.account_new_risk_gate_support import evaluate_portfolio_new_risk_admission
    result = evaluate_portfolio_new_risk_admission({"total_equity": 40000, "realized_vol": "bad"})
    assert result.risk_input_invalid and not result.hard_brake_triggered
    assert result.combined_scale is None


def test_submit_rejects_unproven_reduction_and_allows_short_cover():
    set_cycle_snapshot(InjectedReconciliationSnapshot(observation_status="COMPLETE", reconciliation_status="VERIFIED", circuit_breaker_state="CLOSED", equity_usd=None))
    with mock.patch("interactive_brokers_platform.application.ibkr_order_execution._submit_order_intent", return_value=SimpleNamespace(status="Submitted")) as submit:
        rejected = submit_order_intent(SimpleNamespace(), OrderIntent(symbol="SPY", side="sell", quantity=2), reduction_position=1, reduction_pending_quantity=0)
        assert rejected.status == "rejected"
        rejected = submit_order_intent(SimpleNamespace(), OrderIntent(symbol="SPY", side="buy", quantity=1), reduction_position=-2, reduction_pending_quantity=None)
        assert rejected.status == "rejected"
        submit_order_intent(SimpleNamespace(), OrderIntent(symbol="SPY", side="buy", quantity=1), reduction_position=-2, reduction_pending_quantity=0)
        submit.assert_called_once()


def _clean_fuse_record(trade_date: str = "2026-11-27", **overrides):
    record = {
        "trade_date": trade_date,
        "session_allowed": True,
        "session_state": "regular",
        "fuse_latched": False,
        "fuse_level": "L0",
        "blocks_orders": False,
        "blocks_eligibility": False,
        "calendar_version": "test-cal-v1",
        "spec_digest": "0" * 64,
    }
    record.update(overrides)
    return record


def test_fuse2_same_day_clean_readback_allows_and_missing_or_stale_blocks(monkeypatch):
    from interactive_brokers_platform.application import account_new_risk_gate_support as gate
    from interactive_brokers_platform.application.account_new_risk_gate_support import (
        evaluate_account_values_new_risk_admission,
        is_effective_new_risk_gate_enabled,
    )

    monkeypatch.setenv("FUSE_READBACK_MODE", "enforce")
    monkeypatch.setattr(gate, "_current_fuse_trade_date", lambda *a, **kw: "2026-11-27")
    monkeypatch.delenv("FUSE_TRADE_DATE", raising=False)
    monkeypatch.delenv("ACCOUNT_NEW_RISK_GATE", raising=False)

    with mock.patch(
        "interactive_brokers_platform.application.account_new_risk_gate_support.resolve_production_drift_status_from_store",
        return_value=None,
    ):
        allowed = evaluate_account_values_new_risk_admission(
            {"account_equity_usd": 50_000.0, "peak_equity_usd": 50_000.0, "fuse_readback": _clean_fuse_record()},
        )
        assert allowed.disposition == NewRiskDisposition.ALLOW_NEW_RISK
        assert allowed.fuse_decision is not None
        assert not allowed.fuse_decision.blocks_new_risk

        stale = evaluate_account_values_new_risk_admission(
            {"account_equity_usd": 50_000.0, "peak_equity_usd": 50_000.0, "fuse_readback": _clean_fuse_record("2026-11-26")},
        )
        assert stale.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
        assert "FUSE_READBACK_STALE_FAIL_CLOSED" in stale.reason_codes

        null_payload = evaluate_account_values_new_risk_admission(
            {"account_equity_usd": 50_000.0, "peak_equity_usd": 50_000.0, "fuse_readback": None},
        )
        assert null_payload.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
        assert "FUSE_READBACK_MISSING_FAIL_CLOSED" in null_payload.reason_codes

        latched = evaluate_account_values_new_risk_admission(
            {"account_equity_usd": 50_000.0, "peak_equity_usd": 50_000.0, "fuse_readback": _clean_fuse_record(fuse_latched=True)},
        )
        assert latched.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
        assert "FUSE_LATCHED_BLOCKED" in latched.reason_codes
        assert is_effective_new_risk_gate_enabled()


def test_fuse2_legacy_switch_off_cannot_disable_fuse_and_observe_window_transitions(monkeypatch):
    from interactive_brokers_platform.application import account_new_risk_gate_support as gate
    from interactive_brokers_platform.application.account_new_risk_gate_support import (
        account_new_risk_gate_cycle,
        evaluate_account_values_new_risk_admission,
        is_effective_new_risk_gate_enabled,
    )

    monkeypatch.setenv("ACCOUNT_NEW_RISK_GATE", "0")
    monkeypatch.setenv("FUSE_READBACK_MODE", "observe")
    monkeypatch.setenv("FUSE_OBSERVE_UNTIL", "2026-12-11")
    monkeypatch.delenv("FUSE_TRADE_DATE", raising=False)
    monkeypatch.setattr(gate, "_current_fuse_trade_date", lambda *a, **kw: "2026-12-01")

    with mock.patch(
        "interactive_brokers_platform.application.account_new_risk_gate_support.resolve_production_drift_status_from_store",
        return_value=None,
    ):
        with account_new_risk_gate_cycle(
            {"account_equity_usd": 50_000.0, "peak_equity_usd": 50_000.0, "fuse_readback": _clean_fuse_record("2026-12-01")}
        ):
            assert is_effective_new_risk_gate_enabled() is True
            with mock.patch("interactive_brokers_platform.application.ibkr_order_execution._submit_order_intent") as submit_mock:
                rejected = submit_order_intent(SimpleNamespace(), OrderIntent(symbol="SPY", side="buy", quantity=1.0))
                assert rejected.status == "rejected"
                assert "LEGACY_NEW_RISK_SWITCH_DISABLED" in rejected.raw_payload["reason_codes"]
                submit_mock.assert_not_called()

        monkeypatch.setenv("ACCOUNT_NEW_RISK_GATE", "1")
        observed_stale = evaluate_account_values_new_risk_admission(
            {"account_equity_usd": 50_000.0, "peak_equity_usd": 50_000.0, "fuse_readback": _clean_fuse_record("2026-11-30")},
        )
        assert observed_stale.disposition == NewRiskDisposition.ALLOW_NEW_RISK
        assert observed_stale.fuse_decision is not None
        assert observed_stale.fuse_decision.effective_mode == "observe"
        assert observed_stale.fuse_decision.would_block is True

        monkeypatch.setattr(gate, "_current_fuse_trade_date", lambda *a, **kw: "2026-12-12")
        expired_latched = evaluate_account_values_new_risk_admission(
            {"account_equity_usd": 50_000.0, "peak_equity_usd": 50_000.0, "fuse_readback": _clean_fuse_record("2026-12-12", fuse_latched=True)},
        )
        assert expired_latched.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
        assert expired_latched.fuse_decision is not None
        assert expired_latched.fuse_decision.effective_mode == "enforce"
        assert expired_latched.fuse_decision.observe_window_expired is True


def test_fuse2_native_readback_precedence_and_stale_env_isolation(monkeypatch):
    from interactive_brokers_platform.application import account_new_risk_gate_support as gate

    monkeypatch.setenv("FUSE_READBACK_MODE", "enforce")
    monkeypatch.setenv("FUSE_TRADE_DATE", "2026-11-26")
    monkeypatch.setattr(gate, "_current_fuse_trade_date", lambda *a, **kw: "2026-11-27")
    monkeypatch.delenv("ACCOUNT_NEW_RISK_GATE", raising=False)

    clean_cached = {"account_new_risk_snapshot": {"fuse_readback": _clean_fuse_record("2026-11-27")}}
    with mock.patch.object(gate, "resolve_production_drift_status_from_store", return_value=None):
        for bad_native, expected_code in (
            (None, "FUSE_READBACK_MISSING_FAIL_CLOSED"),
            ({"trade_date": "2026-11-27", "fuse_latched": "yes"}, "FUSE_READBACK_INVALID_FAIL_CLOSED"),
            (_clean_fuse_record("2026-11-27", fuse_latched=True), "FUSE_LATCHED_BLOCKED"),
        ):
            blocked = gate.evaluate_account_values_new_risk_admission(
                {"account_equity_usd": 50_000.0, "peak_equity_usd": 50_000.0, "fuse_readback": bad_native},
                signal_metadata=clean_cached,
            )
            assert blocked.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
            assert expected_code in blocked.reason_codes

        stale_day = gate.evaluate_account_values_new_risk_admission(
            {"account_equity_usd": 50_000.0, "peak_equity_usd": 50_000.0, "fuse_readback": _clean_fuse_record("2026-11-26")},
        )
        assert stale_day.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
        assert "FUSE_READBACK_STALE_FAIL_CLOSED" in stale_day.reason_codes

        today_ok = gate.evaluate_account_values_new_risk_admission(
            {"account_equity_usd": 50_000.0, "peak_equity_usd": 50_000.0, "fuse_readback": _clean_fuse_record("2026-11-27")},
        )
        assert today_ok.disposition == NewRiskDisposition.ALLOW_NEW_RISK


def test_fuse2_rebalance_snapshot_transport_preserves_explicit_null_and_gate(monkeypatch):
    from interactive_brokers_platform.application import account_new_risk_gate_support as gate

    for key in ("FUSE_READBACK_MODE", "FUSE_TRADE_DATE", "FUSE_OBSERVE_UNTIL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ACCOUNT_NEW_RISK_GATE", "0")
    monkeypatch.setattr(gate, "_current_fuse_trade_date", lambda *a, **kw: "2026-11-27")

    snapshot = SimpleNamespace(
        total_equity=50_000.0,
        buying_power=50_000.0,
        as_of=None,
        positions={},
        metadata={
            "currency": "USD",
            "broker_net_liquidation": 50_000.0,
            "total_equity_source": "broker_net_liquidation",
            "peak_equity_usd": 50_000.0,
            "fuse_readback": None,
        },
    )
    _positions, account_values = _snapshot_to_portfolio_view(snapshot)
    assert "fuse_readback" in account_values and account_values["fuse_readback"] is None
    portfolio = build_portfolio_from_account_values(account_values)
    assert gate.is_effective_new_risk_gate_enabled(portfolio) is True
    with mock.patch.object(gate, "resolve_production_drift_status_from_store", return_value=None):
        result = evaluate_account_values_new_risk_admission(account_values)
    assert result.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
    assert "FUSE_READBACK_MISSING_FAIL_CLOSED" in result.reason_codes


def test_fuse2_cycle_snapshot_and_observe_summary_telemetry(monkeypatch):
    from interactive_brokers_platform.application import _execution_allocation as alloc
    from interactive_brokers_platform.application import account_new_risk_gate_support as gate

    monkeypatch.setenv("ACCOUNT_NEW_RISK_GATE", "0")
    monkeypatch.setenv("FUSE_READBACK_MODE", "enforce")
    monkeypatch.setattr(gate, "_current_fuse_trade_date", lambda *a, **kw: "2026-11-27")

    bound_snapshot = InjectedReconciliationSnapshot(
        observation_status="COMPLETE",
        reconciliation_status="VERIFIED",
        circuit_breaker_state="CLOSED",
        equity_usd=50_000.0,
        peak_equity_usd=50_000.0,
        fuse_enabled=False,
        fuse_readback={"trade_date": "2026-11-27", "fuse_latched": "yes"},
    )
    set_cycle_snapshot(bound_snapshot)
    with mock.patch.object(gate, "resolve_production_drift_status_from_store", return_value=None):
        with mock.patch("interactive_brokers_platform.application.ibkr_order_execution._submit_order_intent") as submit_mock:
            rejected = submit_order_intent(SimpleNamespace(), OrderIntent(symbol="SPY", side="buy", quantity=1.0))
            assert rejected.status == "rejected"
            assert "FUSE_READBACK_INVALID_FAIL_CLOSED" in rejected.raw_payload["reason_codes"]
            submit_mock.assert_not_called()

    set_cycle_snapshot(None)
    monkeypatch.setenv("ACCOUNT_NEW_RISK_GATE", "1")
    monkeypatch.setenv("FUSE_READBACK_MODE", "observe")
    monkeypatch.setenv("FUSE_OBSERVE_UNTIL", "2026-12-11")
    monkeypatch.setattr(gate, "_current_fuse_trade_date", lambda *a, **kw: "2026-12-01")
    summary: dict[str, object] = {}
    with mock.patch.object(gate, "resolve_production_drift_status_from_store", return_value=None):
        _weights, blocked, _codes = alloc._apply_account_new_risk_gate(
            {"SPY": 0.5},
            account_values={
                "account_equity_usd": 50_000.0,
                "peak_equity_usd": 50_000.0,
                "fuse_readback": _clean_fuse_record("2026-11-30"),
            },
            signal_metadata={},
            account_execution_session=None,
            execution_summary=summary,
            dry_run_only=True,
        )
    assert blocked is False
    assert summary["fuse_readback_effective_mode"] == "observe"
    assert summary["fuse_readback_would_block"] is True
    assert "FUSE_READBACK_STALE_FAIL_CLOSED" in summary["fuse_readback_reason_codes"]  # type: ignore[operator]


def test_fuse2_hk_market_timezone_removed_observe_and_legacy_transport(monkeypatch):
    from interactive_brokers_platform.application import account_new_risk_gate_support as gate
    from interactive_brokers_platform.application import rebalance_service

    monkeypatch.setenv("MARKET", "HK")
    monkeypatch.delenv("IBKR_MARKET_TIMEZONE", raising=False)
    monkeypatch.delenv("RUNTIME_TARGET_JSON", raising=False)
    assert gate._resolve_fuse_market_timezone() == "Asia/Hong_Kong"
    # IBKR_MARKET must take precedence over RUNTIME_TARGET_JSON["market"], and QSL_RUNTIME_TARGET_JSON must be honored.
    monkeypatch.setenv("QSL_RUNTIME_TARGET_JSON", '{"market": "HK"}')
    assert gate._resolve_fuse_market_timezone() == "Asia/Hong_Kong"
    monkeypatch.delenv("QSL_RUNTIME_TARGET_JSON", raising=False)
    monkeypatch.setenv("RUNTIME_TARGET_JSON", '{"market": "US"}')
    monkeypatch.setenv("IBKR_MARKET", "HK")
    assert gate._resolve_fuse_market_timezone() == "Asia/Hong_Kong"
    monkeypatch.delenv("IBKR_MARKET", raising=False)
    monkeypatch.delenv("RUNTIME_TARGET_JSON", raising=False)
    # Cycle snapshot preserves portfolio-level market_timezone across submit-time checks.
    monkeypatch.delenv("MARKET", raising=False)
    observed_tz: list[str | None] = []
    orig_trade_date = gate._current_fuse_trade_date
    monkeypatch.setattr(
        gate,
        "_current_fuse_trade_date",
        lambda *a, **kw: observed_tz.append(kw.get("market_timezone")) or "2026-12-09",
    )
    with mock.patch.object(gate, "resolve_production_drift_status_from_store", return_value=None):
        with gate.account_new_risk_gate_cycle(
            {
                "account_equity_usd": 50_000.0,
                "peak_equity_usd": 50_000.0,
                "market_timezone": "Asia/Hong_Kong",
                "fuse_readback": _clean_fuse_record("2026-12-09"),
            }
        ):
            gate.evaluate_cycle_new_risk_admission()
    assert observed_tz[-1] == "Asia/Hong_Kong"
    monkeypatch.setattr(gate, "_current_fuse_trade_date", orig_trade_date)

    monkeypatch.setenv("ACCOUNT_NEW_RISK_GATE", "0")
    monkeypatch.setenv("FUSE_READBACK_MODE", "observe")
    monkeypatch.setenv("FUSE_OBSERVE_UNTIL", "2026-12-11")
    monkeypatch.setattr(gate, "_current_fuse_trade_date", lambda *a, **kw: "2026-12-09")

    with mock.patch.object(gate, "resolve_production_drift_status_from_store", return_value=None):
        with gate.account_new_risk_gate_cycle(
            {
                "account_equity_usd": 50_000.0,
                "peak_equity_usd": 50_000.0,
                "fuse_readback": _clean_fuse_record("2026-12-09", fuse_latched=True),
            }
        ):
            # Removing observe env mid-cycle must promote submit_order_intent to enforce immediately.
            monkeypatch.delenv("FUSE_OBSERVE_UNTIL", raising=False)
            monkeypatch.delenv("FUSE_READBACK_MODE", raising=False)
            with mock.patch("interactive_brokers_platform.application.ibkr_order_execution._submit_order_intent") as submit_mock:
                rejected = submit_order_intent(SimpleNamespace(), OrderIntent(symbol="SPY", side="buy", quantity=1.0))
                assert rejected.status == "rejected"
                assert "FUSE_LATCHED_BLOCKED" in rejected.raw_payload["reason_codes"]
                submit_mock.assert_not_called()

    # Legacy portfolio -> PortfolioSnapshot -> _snapshot_to_portfolio_view preserves fuse_readback and fuse_enabled.
    for key in ("FUSE_READBACK_MODE", "FUSE_TRADE_DATE", "FUSE_OBSERVE_UNTIL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ACCOUNT_NEW_RISK_GATE", "0")
    for raw_values, expected_code in (
        ({"equity": 50_000.0, "buying_power": 10_000.0, "fuse_readback": _clean_fuse_record("2026-12-09", fuse_latched=True)}, "FUSE_LATCHED_BLOCKED"),
        ({"equity": 50_000.0, "buying_power": 10_000.0, "fuse_enabled": True}, "FUSE_READBACK_MISSING_FAIL_CLOSED"),
    ):
        snap = rebalance_service._legacy_portfolio_snapshot(
            SimpleNamespace(), get_current_portfolio=lambda _ib, v=raw_values: ({}, v)
        )
        _pos, transported = _snapshot_to_portfolio_view(snap)
        portfolio = build_portfolio_from_account_values(transported)
        assert gate.is_effective_new_risk_gate_enabled(portfolio) is True
        with mock.patch.object(gate, "resolve_production_drift_status_from_store", return_value=None):
            res = evaluate_account_values_new_risk_admission(transported)
        assert res.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
        assert expected_code in res.reason_codes

    # Platform telemetry helper preserves observe findings.
    exec_summary = {
        "execution_status": "completed",
        "orders_submitted": [],
        "orders_skipped": [],
        "fuse_readback_effective_mode": "observe",
        "fuse_readback_would_block": True,
        "fuse_readback_reason_codes": ["FUSE_READBACK_STALE_FAIL_CLOSED"],
        "fuse_readback_observe_window_expired": False,
    }
    recorded: list[dict[str, object]] = []
    with mock.patch.object(rebalance_service, "try_record_platform_execution", side_effect=lambda _p, payload, **_kw: recorded.append(payload)):
        rebalance_service._record_platform_execution_telemetry({"strategy_profile": "synthetic"}, exec_summary)
        rebalance_service._record_platform_execution_telemetry(
            {"strategy_profile": "synthetic", "account_brake_observation": {
                "fuse_readback_effective_mode": "observe",
                "fuse_readback_would_block": True,
                "fuse_readback_reason_codes": ["FUSE_READBACK_MISSING_FAIL_CLOSED"],
                "fuse_readback_observe_window_expired": False,
            }},
            {},
        )
    assert len(recorded) == 2
    assert recorded[0]["fuse_readback_effective_mode"] == "observe"
    assert recorded[0]["fuse_readback_would_block"] is True
    assert recorded[0]["fuse_readback_reason_codes"] == ["FUSE_READBACK_STALE_FAIL_CLOSED"]
    assert recorded[1]["fuse_readback_reason_codes"] == ["FUSE_READBACK_MISSING_FAIL_CLOSED"]


def test_fuse3_state_path_readback_allow_and_fail_closed(tmp_path, monkeypatch):
    import json
    from interactive_brokers_platform.application import account_new_risk_gate_support as gate

    state_file = tmp_path / "fuse_readback.json"
    state_file.write_text(json.dumps(_clean_fuse_record("2026-12-09")), encoding="utf-8")
    monkeypatch.setenv("FUSE_READBACK_STATE_PATH", str(state_file))
    monkeypatch.delenv("FUSE_READBACK_MODE", raising=False)
    monkeypatch.delenv("FUSE_TRADE_DATE", raising=False)
    monkeypatch.setattr(gate, "_current_fuse_trade_date", lambda *a, **kw: "2026-12-09")

    with mock.patch.object(gate, "resolve_production_drift_status_from_store", return_value=None):
        allowed = evaluate_account_values_new_risk_admission(
            {"account_equity_usd": 50_000.0, "peak_equity_usd": 50_000.0},
        )
        assert allowed.disposition == NewRiskDisposition.ALLOW_NEW_RISK

        state_file.unlink()
        monkeypatch.setenv("ACCOUNT_NEW_RISK_GATE", "0")
        assert gate.is_effective_new_risk_gate_enabled(snapshot=None) is True
        with mock.patch("interactive_brokers_platform.application.ibkr_order_execution._submit_order_intent") as submit_mock:
            rejected = submit_order_intent(SimpleNamespace(), OrderIntent(symbol="SPY", side="buy", quantity=1.0))
            assert rejected.status == "rejected"
            submit_mock.assert_not_called()

        blocked = evaluate_account_values_new_risk_admission(
            {"account_equity_usd": 50_000.0, "peak_equity_usd": 50_000.0},
        )
        assert blocked.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
        assert "FUSE_READBACK_MISSING_FAIL_CLOSED" in blocked.reason_codes

        # Mid-cycle external state update / deletion must refresh at evaluate_cycle_new_risk_admission().
        monkeypatch.delenv("ACCOUNT_NEW_RISK_GATE", raising=False)
        state_file.write_text(json.dumps(_clean_fuse_record("2026-12-09")), encoding="utf-8")
        with gate.account_new_risk_gate_cycle({"account_equity_usd": 50_000.0, "peak_equity_usd": 50_000.0}):
            assert gate.evaluate_cycle_new_risk_admission().disposition == NewRiskDisposition.ALLOW_NEW_RISK
            state_file.write_text(json.dumps(_clean_fuse_record("2026-12-09", fuse_latched=True)), encoding="utf-8")
            latched_res = gate.evaluate_cycle_new_risk_admission()
            assert latched_res.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
            assert "FUSE_LATCHED_BLOCKED" in latched_res.reason_codes
            state_file.unlink()
            missing_res = gate.evaluate_cycle_new_risk_admission()
            assert missing_res.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED
            assert "FUSE_READBACK_MISSING_FAIL_CLOSED" in missing_res.reason_codes

        # Explicit payload takes precedence over FUSE_READBACK_STATE_PATH throughout the cycle.
        with gate.account_new_risk_gate_cycle(
            {
                "account_equity_usd": 50_000.0,
                "peak_equity_usd": 50_000.0,
                "fuse_readback": _clean_fuse_record("2026-12-09"),
            }
        ):
            assert gate.evaluate_cycle_new_risk_admission().disposition == NewRiskDisposition.ALLOW_NEW_RISK
