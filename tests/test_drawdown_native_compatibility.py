"""Keep native account drawdown distinct from cached strategy projections."""
import math
import struct

import pytest

from application import account_new_risk_gate_support as gate
from tests.test_drawdown_cache_postmerge import facts, projected


@pytest.mark.parametrize("drawdown", [
    0.0, .074999, math.nextafter(.075, 0), .075,
    math.nextafter(.075, 1), .075001, .10, .15, .20,
])
@pytest.mark.parametrize("cached", [0.0, .6])
def test_native_explicit_drawdown_keeps_main_bits(drawdown, cached):
    values, metadata = facts("native-snapshot", cached=cached)
    values.update(drawdown_from_peak=drawdown,
                  peak_equity_usd=40_000 / (1 - drawdown))
    snapshot = projected(values, metadata)
    # Main passes through the explicit native float, without an inverse round-trip.
    assert struct.pack("!d", snapshot.drawdown_from_peak) == struct.pack("!d", drawdown)


@pytest.mark.parametrize("variant", ["native-snapshot", "top-level-copy"])
def test_rebuilt_cached_projection_uses_current_nav(variant):
    values, metadata = facts(variant, cached=0.0)
    portfolio = gate.build_portfolio_from_account_values(values, signal_metadata=metadata)
    portfolio["total_equity"] = 50_000
    assert gate.build_snapshot_from_portfolio(portfolio).drawdown_from_peak == 0.0
