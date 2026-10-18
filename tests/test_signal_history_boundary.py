"""Signal-only history boundary regressions; no broker, assets or execution."""
from types import SimpleNamespace

import pandas as pd
import pytest

from strategy_runtime import LoadedStrategyRuntime


def runtime(profile, required):
    return LoadedStrategyRuntime(
        entrypoint=SimpleNamespace(manifest=SimpleNamespace(profile=profile, required_inputs=(required,))),
        runtime_settings=SimpleNamespace(market_signal_source="broker"),
        runtime_adapter=None,
        merged_runtime_config={"trend_ma_window": 140, "benchmark_symbol": "QQQ"},
        logger=lambda _message: None,
    )


def history():
    return pd.Series([100. + i / 10 for i in range(300)],
                     index=pd.date_range(end="2026-03-31", periods=300, freq="B"))


def semiconductor_inputs(values):
    return runtime("soxl_soxx_trend_income", "derived_indicators")._build_value_target_market_inputs(
        ib=None, historical_close_loader=lambda *_args, **_kwargs: values,
        historical_candle_loader=None, as_of=pd.Timestamp("2026-03-31"))


def test_soxl_signal_excludes_later_session_without_changing_same_day_bar():
    known = history()
    with_future = pd.concat([known, pd.Series([9999.], index=pd.to_datetime(["2026-04-01"]))])
    assert semiconductor_inputs(with_future) == semiconductor_inputs(known)


def test_soxl_signal_sorts_dated_closes_before_rolling_features():
    known = history()
    assert semiconductor_inputs(known.iloc[::-1]) == semiconductor_inputs(known)


def test_soxl_signal_refuses_undated_history_instead_of_assuming_session_order():
    with pytest.raises(ValueError, match="dated"):
        semiconductor_inputs(pd.Series([100.] * 300))


def test_tqqq_completed_history_is_sorted_before_entering_plugin():
    bars = [{"as_of": d, "close": i} for i, d in enumerate(
        ["2026-03-27", "2026-03-30", "2026-03-31"], 100)]
    mixed = [bars[1], bars[0], bars[2], {"as_of": "2026-04-01", "close": 9999.}]
    got = runtime("tqqq_growth_income", "benchmark_history")._build_value_target_market_inputs(
        ib=None, historical_close_loader=None, historical_candle_loader=lambda *_a, **_k: mixed,
        as_of=pd.Timestamp("2026-03-31"))
    assert got["benchmark_history"] == bars


def test_tqqq_existing_future_clip_and_exact_anchor_rejection_remain():
    strategy = runtime("tqqq_growth_income", "benchmark_history")
    known = {"as_of": "2026-03-31", "close": 100.}
    future = {"as_of": "2026-04-01", "close": 9999.}
    kwargs = dict(ib=None, historical_close_loader=None, as_of=pd.Timestamp("2026-03-31"))
    assert strategy._build_value_target_market_inputs(
        **kwargs, historical_candle_loader=lambda *_a, **_k: [known, future]
    )["benchmark_history"] == [known]
    with pytest.raises(ValueError, match="completed benchmark history is unavailable"):
        strategy._build_value_target_market_inputs(**kwargs, historical_candle_loader=lambda *_a, **_k: [future])
