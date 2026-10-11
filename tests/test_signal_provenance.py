from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from application import signal_provenance
from application import runtime_strategy_adapters as rsa


@pytest.fixture(autouse=True)
def _reset_provenance():
    signal_provenance.reset()
    yield
    signal_provenance.reset()


def _adapters(*, fallback, series_points=(), candles=()):
    return rsa.build_runtime_strategy_adapters(
        strategy_runtime=SimpleNamespace(),
        strategy_profile="soxl_soxx_trend_income",
        translator=lambda key, **_kw: key,
        pacing_sec=0.0,
        resolve_run_as_of_date_fn=lambda: None,
        fetch_historical_price_series_fn=lambda *_a, **_k: SimpleNamespace(points=tuple(series_points)),
        fetch_historical_price_candles_fn=lambda *_a, **_k: list(candles),
        map_strategy_decision_fn=lambda *_a, **_k: (),
        fallback_historical_candles_fn=fallback,
    )


def _frame(columns):
    index = pd.to_datetime(["2026-10-07", "2026-10-08", "2026-10-09"])
    data = {name: [10.0, 11.0, 12.0] for name in columns}
    return pd.DataFrame(data, index=index)


def test_recorder_keeps_only_non_sensitive_fields():
    signal_provenance.record("soxx", source="yfinance", price_basis="dividend_adjusted",
                             dates=[datetime(2024, 10, 10), datetime(2026, 10, 9)])
    signal_provenance.record("SOXX", source="yfinance", price_basis="dividend_adjusted",
                             dates=[datetime(2024, 10, 10), datetime(2026, 10, 9)])
    snap = signal_provenance.snapshot()
    assert snap["schema_version"] == "qsl.signal_data_provenance.v1"
    entry = snap["symbols"]["SOXX"]
    assert entry == {"source": "yfinance", "price_basis": "dividend_adjusted", "rows": 2,
                     "first_date": "2024-10-10", "last_date": "2026-10-09", "fetch_count": 2}


def test_recorder_normalizes_unknown_basis_and_never_raises():
    signal_provenance.record("QQQ", source="x", price_basis="weird", dates=[1_728_518_400_000])
    signal_provenance.record(None, source="x", price_basis="split_only", dates=[])
    signal_provenance.record("BAD", source="x", price_basis="split_only", dates=object())  # not iterable
    snap = signal_provenance.snapshot()
    assert snap["symbols"]["QQQ"]["price_basis"] == "unknown"
    assert snap["symbols"]["QQQ"]["first_date"] == "2024-10-10"
    assert "BAD" not in snap["symbols"]
    signal_provenance.reset()
    assert signal_provenance.snapshot() == {}


def test_yfinance_adj_close_is_labelled_dividend_adjusted_and_output_unchanged():
    frame = _frame(["Open", "High", "Low", "Close", "Adj Close", "Volume"])
    frame["Adj Close"] = [9.0, 10.0, 11.0]
    candles = rsa._coerce_yfinance_candles(frame, symbol="SOXL")
    assert [c["close"] for c in candles] == [9.0, 10.0, 11.0]  # same column choice as before
    assert rsa.yfinance_price_basis("soxl") == "dividend_adjusted"


def test_yfinance_close_only_is_labelled_split_only():
    rsa._coerce_yfinance_candles(_frame(["Open", "Close"]), symbol="SOXX")
    assert rsa.yfinance_price_basis("SOXX") == "split_only"


def test_adapter_records_yfinance_fallback_history(monkeypatch):
    frame = _frame(["Close", "Adj Close"])

    def fake_fetch(symbol, *, duration, bar_size):
        return rsa._coerce_yfinance_candles(frame, symbol=symbol)

    monkeypatch.setattr(rsa, "fetch_yfinance_historical_candles", fake_fetch)
    adapters = _adapters(fallback=fake_fetch)
    # identity check uses the module attribute, so the fake counts as the yfinance provider
    closes = adapters.get_historical_close(None, "SOXL", duration="420 D")
    candles = adapters.get_historical_candles(None, "QQQ")
    assert list(closes.values) == [10.0, 11.0, 12.0]
    assert len(candles) == 3
    symbols = signal_provenance.snapshot()["symbols"]
    assert symbols["SOXL"] == {"source": "yfinance", "price_basis": "dividend_adjusted", "rows": 3,
                               "first_date": "2026-10-07", "last_date": "2026-10-09", "fetch_count": 1}
    assert symbols["QQQ"]["source"] == "yfinance"


def test_adapter_records_ibkr_history_when_fallback_empty():
    points = [SimpleNamespace(as_of=datetime(2026, 10, 8), close=1.0),
              SimpleNamespace(as_of=datetime(2026, 10, 9), close=2.0)]
    candles = [{"as_of": datetime(2026, 10, 9), "close": 3.0}]
    adapters = _adapters(fallback=lambda *_a, **_k: [], series_points=points, candles=candles)
    closes = adapters.get_historical_close(None, "SOXX")
    got = adapters.get_historical_candles(None, "QQQ")
    assert list(closes.values) == [1.0, 2.0]
    assert got == candles
    symbols = signal_provenance.snapshot()["symbols"]
    assert symbols["SOXX"]["source"] == "ibkr_historical"
    assert symbols["SOXX"]["price_basis"] == "unknown"
    assert symbols["SOXX"]["rows"] == 2
    assert "ADJUSTED_LAST" in symbols["SOXX"]["note"]
    assert symbols["QQQ"]["rows"] == 1


def test_generator_candles_are_not_consumed():
    def gen():
        yield {"as_of": datetime(2026, 10, 9), "close": 3.0}

    adapters = rsa.build_runtime_strategy_adapters(
        strategy_runtime=SimpleNamespace(), strategy_profile="tqqq_growth_income",
        translator=lambda key, **_kw: key, pacing_sec=0.0, resolve_run_as_of_date_fn=lambda: None,
        fetch_historical_price_series_fn=lambda *_a, **_k: SimpleNamespace(points=()),
        fetch_historical_price_candles_fn=lambda *_a, **_k: gen(),
        map_strategy_decision_fn=lambda *_a, **_k: (),
        fallback_historical_candles_fn=lambda *_a, **_k: [],
    )
    result = adapters.get_historical_candles(None, "QQQ")
    assert len(list(result)) == 1


def test_main_attaches_provenance_to_report(strategy_module):
    signal_provenance.record("SOXL", source="yfinance", price_basis="dividend_adjusted",
                             dates=[datetime(2026, 10, 9)])
    report = {"diagnostics": {"result": "ok"}}
    strategy_module.attach_signal_data_provenance(report)
    assert report["diagnostics"]["result"] == "ok"
    assert report["diagnostics"]["signal_data_provenance"]["symbols"]["SOXL"]["rows"] == 1
    empty = {}
    signal_provenance.reset()
    strategy_module.attach_signal_data_provenance(empty)
    assert empty == {}
