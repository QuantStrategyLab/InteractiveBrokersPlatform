"""Post-merge cache regressions; artificial account reads and local journals only."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from fractions import Fraction
import struct

import pytest

from application import account_new_risk_gate_support as gate
from quant_platform_kit.common.execution_state import AccountSubmissionSession, ExecutionMarkerStore

NOW = datetime(2026, 11, 4, 0, 0, tzinfo=timezone.utc)
VARIANTS = ["native-snapshot", "top-level-copy"]


@pytest.fixture(autouse=True)
def offline_context(monkeypatch):
    for key in ("IBKR_MAX_DAILY_LOSS_USD", "MAX_DAILY_LOSS_USD", "RUNTIME_TARGET_JSON"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(gate, "resolve_production_drift_status_from_store", lambda **_kw: None)
    monkeypatch.setattr(gate, "is_portfolio_loss_stoploss_enabled", lambda: False)


def session_at(path):
    return AccountSubmissionSession(
        ExecutionMarkerStore(local_dir=path), broker="fixture", account_id="<PROJECT_ID>",
        owner_id="<PROJECT_ID>", profile="<PROJECT_ID>", intent_id="1" * 64, release_id="2" * 64,
    )


def facts(variant, equity=40_000, peak=50_000, cached=0.0, timestamp=None):
    values = {"account_equity_usd": equity}
    projection = {"observation_status": "COMPLETE", "reconciliation_status": "VERIFIED",
                  "circuit_breaker_state": "CLOSED"}
    metadata = {"account_new_risk_snapshot": projection}
    source = projection if variant == "native-snapshot" else metadata
    source.update(peak_equity_usd=peak, drawdown_from_peak=cached)
    if timestamp is not None:
        source["drawdown_from_peak_observed_at"] = timestamp
    return values, metadata


def projected(values, metadata):
    return gate.build_snapshot_from_portfolio(
        gate.build_portfolio_from_account_values(values, signal_metadata=metadata))


def bits(number):
    return struct.pack("!d", number)


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("equity,cached", [(40_000, 0.0), (50_000, .6), (50_000, 0.0), (48_000, .6)])
def test_current_read_wins_and_journal_survives_restart(variant, equity, cached, tmp_path):
    values, metadata = facts(variant, equity=equity, cached=cached)
    original = deepcopy((values, metadata))
    snapshot = projected(values, metadata)
    # Exact rational arithmetic determines the expected fixture independently.
    expected = float(max(Fraction(0), Fraction(50_000 - equity, 50_000)))
    assert bits(snapshot.drawdown_from_peak) == bits(expected)
    session = session_at(tmp_path)
    result = gate.evaluate_persistent_account_new_risk_admission(values, signal_metadata=metadata, session=session)
    breached = equity == 40_000
    assert gate.new_risk_buy_prohibited(result) is breached
    assert result.hard_brake_triggered is breached
    assert session.read_state()["circuit_breaker_open"] is breached
    # The existing capital envelope retains 85% of targets at equity >= 50,000.
    expected_target = .425 if equity >= 50_000 else .5
    assert gate.scale_targets_for_admission({"SPY": .5}, result, snapshot=snapshot) == {"SPY": expected_target}
    assert (values, metadata) == original
    if breached:
        assert "DRAWDOWN_BRAKE_TRIPPED" in result.reason_codes
        recovered = {**values, "account_equity_usd": 50_000}
        later = gate.evaluate_persistent_account_new_risk_admission(
            recovered, signal_metadata=metadata, session=session_at(tmp_path))
        assert "CIRCUIT_BREAKER_OPEN" in later.reason_codes
        assert gate.new_risk_buy_prohibited(later)


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("missing", [("nav",), ("peak",), ("nav", "peak")])
def test_missing_measurement_is_unknown_without_cache_fallback(variant, missing, tmp_path):
    values, metadata = facts(variant, cached=0.0)
    if "nav" in missing:
        values.pop("account_equity_usd")
    if "peak" in missing:
        metadata.get("account_new_risk_snapshot", {}).pop("peak_equity_usd", None)
        metadata.pop("peak_equity_usd", None)
    snapshot = projected(values, metadata)
    assert snapshot.drawdown_from_peak is None
    assert snapshot.observation_status == "UNAVAILABLE"
    session = session_at(tmp_path)
    result = gate.evaluate_persistent_account_new_risk_admission(values, signal_metadata=metadata, session=session)
    assert gate.new_risk_buy_prohibited(result)
    assert "OBSERVATION_NOT_COMPLETE" in result.reason_codes
    assert not result.hard_brake_triggered
    assert session.read_state()["circuit_breaker_open"] is False
    # Unknown measurements block buys without fabricating an exit or a measured breach.
    assert gate.scale_targets_for_admission({"SPY": .5}, result, snapshot=snapshot) == {"SPY": .5}


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("axis", ["nav", "peak"])
@pytest.mark.parametrize("invalid", [None, True, 0, -1, float("nan"), float("inf"), "invalid"])
def test_invalid_measurement_is_unknown(variant, axis, invalid):
    values, metadata = facts(variant, cached=0.0)
    if axis == "nav":
        values["account_equity_usd"] = invalid
    else:
        source = metadata["account_new_risk_snapshot"] if variant == "native-snapshot" else metadata
        source["peak_equity_usd"] = invalid
    snapshot = projected(values, metadata)
    assert snapshot.drawdown_from_peak is None
    assert snapshot.observation_status == "UNAVAILABLE"
    result = gate.evaluate_account_values_new_risk_admission(values, signal_metadata=metadata)
    assert gate.new_risk_buy_prohibited(result)
    assert "OBSERVATION_NOT_COMPLETE" in result.reason_codes
    assert not result.hard_brake_triggered


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("stamp", [None, "invalid", "2020-01-01T00:00:00Z", "2999-01-01T00:00:00Z", "2026-11-04T00:00:00"])
def test_untrusted_broker_timestamp_cannot_authorize_entries(variant, stamp):
    values, metadata = facts(variant, peak=None, cached=0.0, timestamp=stamp)
    assert projected(values, metadata).drawdown_from_peak is None
    assert gate.new_risk_buy_prohibited(gate.evaluate_account_values_new_risk_admission(values, signal_metadata=metadata))


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("missing", ["nav", "peak", "both"])
def test_fresh_broker_value_only_when_recompute_impossible(variant, missing):
    stamp = datetime.now(timezone.utc).isoformat()
    values, metadata = facts(variant, cached=.2, timestamp=stamp)
    source = metadata["account_new_risk_snapshot"] if variant == "native-snapshot" else metadata
    if missing in {"nav", "both"}:
        values.pop("account_equity_usd")
    if missing in {"peak", "both"}:
        source.pop("peak_equity_usd")
    assert bits(projected(values, metadata).drawdown_from_peak) == bits(.2)
    values, metadata = facts(variant, equity=50_000, cached=.2, timestamp=stamp)
    assert bits(projected(values, metadata).drawdown_from_peak) == bits(0.0)
    assert not gate.new_risk_buy_prohibited(gate.evaluate_account_values_new_risk_admission(values, signal_metadata=metadata))


@pytest.mark.parametrize("variant", VARIANTS)
def test_timestamp_expires_on_rebuild(variant, monkeypatch):
    class Clock(datetime):
        instant = NOW

        @classmethod
        def now(cls, tz=None):
            return cls.instant.astimezone(tz)

    monkeypatch.setattr(gate, "datetime", Clock, raising=False)
    values, metadata = facts(variant, peak=None, cached=0.0, timestamp=NOW.isoformat())
    portfolio = gate.build_portfolio_from_account_values(values, signal_metadata=metadata)
    assert gate.build_snapshot_from_portfolio(portfolio).drawdown_from_peak == 0.0
    Clock.instant = NOW + timedelta(seconds=gate.BROKER_DRAWDOWN_MAX_AGE_SECONDS + 1)
    assert gate.build_snapshot_from_portfolio(portfolio).drawdown_from_peak is None
    assert gate.new_risk_buy_prohibited(gate.evaluate_portfolio_new_risk_admission(portfolio))


@pytest.mark.parametrize("variant", VARIANTS)
def test_native_point_two_is_bit_equal(variant):
    values, metadata = facts(variant, cached=.2)
    assert bits(projected(values, metadata).drawdown_from_peak) == bits(.2)


def test_integer_nav_peak_property_sweep():
    # Exact integer differences avoid cancellation; the Fraction oracle is independent.
    for peak in range(1, 201):
        for nav in range(1, peak + 2):
            values, metadata = facts("native-snapshot", equity=nav, peak=peak, cached=.6)
            expected = float(max(Fraction(0), Fraction(peak - nav, peak)))
            assert bits(projected(values, metadata).drawdown_from_peak) == bits(expected)


@pytest.mark.parametrize("variant", VARIANTS)
def test_recomputation_never_coerces_cached_value(variant):
    class UnreadableCache:
        def __float__(self):
            raise AssertionError("cached value must not be read")

    values, metadata = facts(variant, cached=UnreadableCache())
    assert bits(projected(values, metadata).drawdown_from_peak) == bits(.2)


@pytest.mark.parametrize("variant", VARIANTS)
def test_fresh_zero_broker_value_allows_without_peak(variant):
    values, metadata = facts(variant, peak=None, cached=0.0,
                             timestamp=datetime.now(timezone.utc).isoformat())
    source = metadata["account_new_risk_snapshot"] if variant == "native-snapshot" else metadata
    source.pop("peak_equity_usd")
    result = gate.evaluate_account_values_new_risk_admission(values, signal_metadata=metadata)
    assert not gate.new_risk_buy_prohibited(result)
