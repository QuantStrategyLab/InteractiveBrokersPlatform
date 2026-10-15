import json
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from application import signal_provenance
from application.price_basis import (
    HistoricalCandles, PriceBasis, PriceBasisError, validate_candles, validate_price_basis,
)
from application.runtime_strategy_adapters import (
    BasisHistoricalIB, _coerce_yfinance_candles, build_runtime_strategy_adapters,
    fetch_yfinance_historical_candles,
)


def frame():
    # Synthetic dividend: split-only drops from 100 to 99, while adjusted
    # history is continuous at 99. No real market data is used.
    return pd.DataFrame({
        "Open": [100.0, 99.0], "High": [102.0, 101.0], "Low": [98.0, 97.0],
        "Close": [100.0, 99.0], "Adj Close": [99.0, 99.0], "Volume": [1000.0, 1500.0],
    }, index=pd.to_datetime(["2024-06-03", "2024-06-04"]))


def adapters(*, basis="split_adjusted", fallback=None, **kwargs):
    from quant_platform_kit.ibkr.market_data import fetch_historical_price_candles, fetch_historical_price_series
    def contract(*_a, **_k):
        return SimpleNamespace()
    return build_runtime_strategy_adapters(
        strategy_runtime=SimpleNamespace(merged_runtime_config={"price_basis": basis}),
        strategy_profile="synthetic", translator=lambda key: key, pacing_sec=0,
        resolve_run_as_of_date_fn=lambda: None,
        fetch_historical_price_series_fn=lambda ib, symbol, **kw: fetch_historical_price_series(
            ib, symbol, stock_factory=contract, **kw),
        fetch_historical_price_candles_fn=lambda ib, symbol, **kw: fetch_historical_price_candles(
            ib, symbol, stock_factory=contract, **kw),
        map_strategy_decision_fn=lambda *_a, **_kw: None,
        fallback_historical_candles_fn=fallback, **kwargs,
    )


class FakeIB:
    def __init__(self, *, empty=False, error=False):
        self.calls = []
        self.empty = empty
        self.error = error

    def qualifyContracts(self, contract):  # noqa: N802
        return [contract]

    def reqHistoricalData(self, _contract, **kwargs):  # noqa: N802
        self.calls.append(kwargs["whatToShow"])
        if self.error:
            raise RuntimeError("SENSITIVE_PROVIDER_EXCEPTION_MUST_NOT_APPEAR")
        if self.empty:
            return []
        return [SimpleNamespace(date=pd.Timestamp("2024-06-03"), open=100.0, high=102.0,
                                low=98.0, close=100.0, volume=1000.0)]


def test_missing_close_never_uses_adjusted_close():
    frame = pd.DataFrame({"Open": [100.0], "Adj Close": [90.0]},
                         index=pd.to_datetime(["2024-06-03"]))
    with pytest.raises(PriceBasisError, match="missing_column:Close"):
        _coerce_yfinance_candles(frame)


@pytest.mark.parametrize("basis", list(PriceBasis))
def test_each_enum_is_valid_configuration(basis):
    assert validate_price_basis(basis.value) == basis.value
    assert adapters(basis=basis.value).price_basis == basis.value


@pytest.mark.parametrize("basis", ["", "adjusted", "split_only", "unknown_legacy", "SPLIT_ADJUSTED", None, 1, True])
def test_invalid_configuration(basis):
    with pytest.raises(ValueError, match="price_basis must be one of"):
        validate_price_basis(basis)


@pytest.mark.parametrize("basis,reason", [("raw_unadjusted", "raw_prices_not_provided"),
                                        ("total_return_index", "total_return_index_is_not_ohlc")])
def test_unsupported_ohlc_fails_before_provider_access(basis, reason, monkeypatch):
    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(download=lambda *_a, **_k: pytest.fail("provider called")))
    with pytest.raises(PriceBasisError, match=reason):
        fetch_yfinance_historical_candles("SYN", price_basis=basis)
    with pytest.raises(PriceBasisError, match=reason):
        BasisHistoricalIB(None, basis)
    with pytest.raises(PriceBasisError, match=reason):
        adapters(basis=basis).get_historical_candles(None, "SYN")


def test_dividend_basis_requires_adj_close():
    with pytest.raises(PriceBasisError, match="missing_column:Adj Close"):
        _coerce_yfinance_candles(frame().drop(columns="Adj Close"), price_basis="split_dividend_adjusted")


@pytest.mark.parametrize("column", ["Open", "High", "Low", "Volume"])
def test_ohlcv_columns_must_not_be_fabricated(column):
    with pytest.raises(PriceBasisError, match="missing_column:" + column):
        _coerce_yfinance_candles(frame().drop(columns=column))


@pytest.mark.parametrize("column,value", [("Close", 0), ("Close", float("nan")), ("Close", float("inf")),
                                        ("Adj Close", float("nan")), ("Volume", float("nan")), ("Volume", -1)])
def test_invalid_row_never_switches_basis_or_disappears(column, value):
    data = frame()
    data.loc[data.index[0], column] = value
    with pytest.raises(PriceBasisError):
        _coerce_yfinance_candles(data, price_basis="split_dividend_adjusted")


def test_dividend_factor_applies_to_all_prices_not_volume():
    data = frame()
    split = _coerce_yfinance_candles(data)
    adjusted = _coerce_yfinance_candles(data, price_basis="split_dividend_adjusted")
    assert [row["close"] for row in split] == [100.0, 99.0]
    assert [row["close"] for row in adjusted] == [99.0, 99.0]
    for key in ("open", "high", "low"):
        assert adjusted[0][key] == split[0][key] * 0.99
        assert adjusted[1][key] == split[1][key]
    assert [row["volume"] for row in adjusted] == [1000.0, 1500.0]
    assert adjusted.diagnostic["volume_basis"] == "provider_native_unscaled"


def test_dividend_ohlc_boundaries_allow_only_float_roundoff():
    data = frame()
    for key in ("Open", "High", "Low", "Close"):
        data[key] = [0.14, 0.14]
    data["Adj Close"] = [0.11, 0.11]
    result = _coerce_yfinance_candles(data, price_basis="split_dividend_adjusted")
    assert result[0]["close"] == 0.11
    assert result[0]["low"] == 0.11000000000000001
    data["Low"] = [0.14001, 0.14]
    with pytest.raises(PriceBasisError, match="inconsistent_ohlc"):
        _coerce_yfinance_candles(data, price_basis="split_dividend_adjusted")


def test_four_for_one_split_is_continuous_and_is_not_raw():
    raw = [400.0, 100.0]  # Contemporaneous units on either side of a 4:1 split.
    data = frame()
    for key in ("Open", "High", "Low", "Close", "Adj Close"):
        data[key] = [raw[0] / 4, raw[1]]
    split = _coerce_yfinance_candles(data)
    assert [row["close"] for row in split] == [100.0, 100.0]
    assert raw[0] / raw[1] == 4
    with pytest.raises(PriceBasisError, match="raw_prices_not_provided"):
        _coerce_yfinance_candles(data, price_basis="raw_unadjusted")


def test_mixed_source_field_metadata_is_rejected():
    data = frame()
    data.attrs["field_price_basis"] = {"Close": "split_dividend_adjusted", "Open": "split_adjusted"}
    with pytest.raises(PriceBasisError, match="mixed_or_mismatched_basis"):
        _coerce_yfinance_candles(data)


def test_mixed_custom_provider_ohlc_metadata_is_rejected():
    candles = _coerce_yfinance_candles(frame())
    candles.field_basis["close"] = "split_dividend_adjusted"
    with pytest.raises(PriceBasisError, match="mixed_or_mismatched_basis"):
        validate_candles(candles, "split_adjusted")


def test_inconsistent_ohlc_geometry_is_rejected():
    data = frame()
    data["Close"] = [200.0, 99.0]
    with pytest.raises(PriceBasisError, match="inconsistent_ohlc"):
        _coerce_yfinance_candles(data)


@pytest.mark.parametrize("multiindex", [None, "field_first", "ticker_first"])
def test_default_matches_frozen_current_main_float_bytes(multiindex):
    fixture = json.loads((Path(__file__).parent / "fixtures/price_basis_main_golden.json").read_text())
    data = pd.DataFrame(fixture["synthetic_input"], index=pd.to_datetime(fixture["dates"]))
    if multiindex:
        data.columns = pd.MultiIndex.from_tuples([
            (key, "SYN") if multiindex == "field_first" else ("SYN", key) for key in data.columns])
    result = _coerce_yfinance_candles(data)
    actual = [{"as_of": row["as_of"].isoformat(), **{
        key: struct.pack("!d", row[key]).hex() for key in ("open", "high", "low", "close", "volume")}}
        for row in result]
    assert fixture["source_sha"] == "8e173b2bd1cb903f00ffbfe77e7f5f709aa00b7a"
    assert actual == fixture["main_output"]


def test_multiple_symbols_never_selects_first_silently():
    data = pd.concat({"SYN": frame(), "OTHER": frame()}, axis=1)
    with pytest.raises(PriceBasisError, match="ambiguous_columns"):
        _coerce_yfinance_candles(data)


@pytest.mark.parametrize("basis,endpoint", [("split_adjusted", "TRADES"),
                                          ("split_dividend_adjusted", "ADJUSTED_LAST")])
@pytest.mark.parametrize("method", ["get_historical_close", "get_historical_candles"])
def test_native_qpk_integration_uses_exact_endpoint(basis, endpoint, method):
    ib = FakeIB()
    result = getattr(adapters(basis=basis), method)(ib, "SYN")
    assert len(result) == 1
    assert ib.calls == [endpoint]
    details = signal_provenance.snapshot()["symbols"]["SYN"]["basis_details"]
    assert details["requested_basis"] == details["resolved_basis"] == basis
    assert details["provider"] == "ibkr"
    assert endpoint in details["source_columns"]


@pytest.mark.parametrize("method", ["get_historical_close", "get_historical_candles"])
def test_adjusted_last_empty_never_retries_trades(method):
    ib = FakeIB(empty=True)
    with pytest.raises(PriceBasisError, match="empty_ADJUSTED_LAST") as error:
        getattr(adapters(basis="split_dividend_adjusted"), method)(ib, "SYN")
    assert ib.calls == ["ADJUSTED_LAST"]
    assert error.value.diagnostic["resolved_basis"] is None


def test_native_empty_trades_is_diagnosable():
    with pytest.raises(PriceBasisError, match="empty_TRADES"):
        adapters().get_historical_close(FakeIB(empty=True), "SYN")


def test_provider_errors_do_not_disclose_exception_text(caplog):
    with pytest.raises(PriceBasisError, match="provider_request_failed") as error:
        adapters(basis="split_dividend_adjusted").get_historical_close(FakeIB(error=True), "SYN")
    assert "SENSITIVE_PROVIDER" not in str(error.value) + caplog.text
    assert set(error.value.diagnostic) == {
        "requested_basis", "resolved_basis", "provider", "source_columns", "volume_basis", "reason"}


@pytest.mark.parametrize("result", [[], [{"close": 1.0}]])
def test_unlabelled_fallback_is_not_relabelled(result):
    with pytest.raises(PriceBasisError, match="missing_basis_metadata"):
        adapters(fallback=lambda *_a, **_kw: result).get_historical_close(None, "SYN")


def test_explicit_fallback_mismatch_is_rejected():
    candles = _coerce_yfinance_candles(frame(), price_basis="split_dividend_adjusted")
    with pytest.raises(PriceBasisError, match="mixed_or_mismatched_basis"):
        adapters(fallback=lambda *_a, **_kw: candles).get_historical_close(None, "SYN")


def test_requested_basis_reaches_wrapped_provider(monkeypatch):
    seen = {}
    def download(*_a, **kw):
        seen.update(kw)
        return frame()
    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(download=download))
    loaded = adapters(basis="split_dividend_adjusted", fallback=fetch_yfinance_historical_candles)
    assert list(loaded.get_historical_close(None, "SYN")) == [99.0, 99.0]
    assert seen["auto_adjust"] is False
    assert signal_provenance.snapshot()["symbols"]["SYN"]["basis_details"]["provider"] == "yfinance"


def test_fail_closed_is_not_swallowed_by_runtime_price_map():
    from strategy_runtime import LoadedStrategyRuntime
    runtime = LoadedStrategyRuntime(entrypoint=SimpleNamespace(manifest=SimpleNamespace(profile="synthetic")),
                                    runtime_adapter=SimpleNamespace(), runtime_settings=SimpleNamespace(),
                                    logger=lambda _message: None)
    def fail(*_a, **_kw):
        raise PriceBasisError("split_adjusted", provider="yfinance", reason="missing_column:Close")
    with pytest.raises(PriceBasisError, match="missing_column:Close"):
        runtime._build_historical_close_map(None, fail, ("SYN",))


def test_native_ohlc_is_checked_before_qpk_can_fabricate_fields():
    class CloseOnlyIB(FakeIB):
        def reqHistoricalData(self, _contract, **kwargs):  # noqa: N802
            self.calls.append(kwargs["whatToShow"])
            return [SimpleNamespace(date=pd.Timestamp("2024-06-03"), close=100.0)]
    with pytest.raises(PriceBasisError, match="missing_native_ohlcv"):
        adapters().get_historical_candles(CloseOnlyIB(), "SYN")


@pytest.mark.parametrize("basis", ["split_adjusted", "split_dividend_adjusted"])
def test_yfinance_empty_does_not_switch_provider(basis, monkeypatch):
    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(download=lambda *_a, **_kw: pd.DataFrame()))
    ib = FakeIB()
    with pytest.raises(PriceBasisError, match="empty_history"):
        adapters(basis=basis, fallback=fetch_yfinance_historical_candles).get_historical_close(ib, "SYN")
    assert ib.calls == []
