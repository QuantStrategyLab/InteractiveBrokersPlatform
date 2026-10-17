"""Builder helpers for IBKR strategy evaluation adapters."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, isfinite
import json
import logging
import re
from typing import Any

import pandas as pd

from application import signal_provenance
from application.price_basis import (
    DEFAULT_PRICE_BASIS, HistoricalCandles, PriceBasis, PriceBasisError,
    basis_diagnostic, require_supported_ohlc_basis, validate_candles, validate_price_basis,
)
from quant_platform_kit.common.strategy_plugins import (
    build_strategy_plugin_alert_messages,
    build_strategy_plugin_error_notification_lines,
    build_strategy_plugin_notification_lines,
    should_alert_strategy_plugin_signal,
    translate_strategy_plugin_value,
)


def _duration_to_yfinance_period(duration: str) -> str:
    text = str(duration or "").strip()
    match = re.fullmatch(r"(\d+)\s*([A-Za-z]+)", text)
    if not match:
        return "2y"

    quantity = int(match.group(1))
    unit = match.group(2).upper()
    if unit == "Y":
        return f"{max(quantity, 1)}y"
    if unit == "M":
        return f"{max(quantity, 1)}mo"
    if unit == "W":
        return f"{max(quantity, 1)}wk"
    if unit == "D":
        if quantity > 365:
            return f"{ceil(quantity / 365)}y"
        return f"{max(quantity, 1)}d"
    return "2y"


def _bar_size_to_yfinance_interval(bar_size: str) -> str:
    text = str(bar_size or "").strip().lower()
    if text in {"1 day", "1d", "day"}:
        return "1d"
    if text in {"1 week", "1wk", "week"}:
        return "1wk"
    if text in {"1 month", "1mo", "month"}:
        return "1mo"
    return "1d"


def _coerce_frame_selection_to_series(selection: Any) -> pd.Series | None:
    if isinstance(selection, pd.Series):
        return selection
    if isinstance(selection, pd.DataFrame) and len(selection.columns) > 0:
        return selection.iloc[:, 0]
    return None


def _frame_column(frame: pd.DataFrame, name: str) -> pd.Series | None:
    if name in frame.columns:
        series = _coerce_frame_selection_to_series(frame[name])
        if series is not None:
            return series
    if isinstance(frame.columns, pd.MultiIndex):
        expected = name.lower()
        for column in frame.columns:
            if any(str(part).lower() == expected for part in column):
                series = _coerce_frame_selection_to_series(frame[column])
                if series is not None:
                    return series
    return None


_YFINANCE_PRICE_BASIS: dict[str, str] = {}


class BasisHistoricalIB:
    """Constrain the pinned QPK loader to exactly one IBKR price basis.

    For split-only requests, skip QPK's first ADJUSTED_LAST probe. For adjusted
    requests, forbid its TRADES retry even if QPK catches the initial error.
    This is endpoint selection, never a price-basis fallback.
    """

    def __init__(self, ib: Any, price_basis=DEFAULT_PRICE_BASIS, *, ohlc=False) -> None:
        self._ib = ib
        self.price_basis = require_supported_ohlc_basis(price_basis, provider="ibkr")
        self.endpoint = "TRADES" if self.price_basis == PriceBasis.SPLIT_ADJUSTED else "ADJUSTED_LAST"
        self.failure = None
        self.resolved = False
        self.ohlc = ohlc

    def reqHistoricalData(self, *args: Any, **kwargs: Any) -> Any:  # noqa: N802 - ib_insync API name
        endpoint = str(kwargs.get("whatToShow") or "").upper()
        if self.failure is not None:
            raise self.failure
        if endpoint != self.endpoint:
            if endpoint == "ADJUSTED_LAST" and self.endpoint == "TRADES":
                return []
            raise PriceBasisError(self.price_basis, provider="ibkr", reason="basis_fallback_forbidden",
                                  source_columns=(self.endpoint,))
        try:
            bars = self._ib.reqHistoricalData(*args, **kwargs)
        except Exception:
            self.failure = PriceBasisError(self.price_basis, provider="ibkr", reason="provider_request_failed",
                                           source_columns=(self.endpoint,))
            raise self.failure from None
        if not bars:
            self.failure = PriceBasisError(self.price_basis, provider="ibkr", reason=f"empty_{self.endpoint}",
                                           source_columns=(self.endpoint,))
            raise self.failure
        if self.ohlc:
            # Check native fields BEFORE QPK's legacy candle conversion, which
            # otherwise synthesizes missing O/H/L from C and missing volume as 0.
            try:
                rows = [{field: getattr(bar, field) for field in ("open", "high", "low", "close", "volume")}
                        for bar in bars]
                validate_candles(HistoricalCandles(
                    rows, price_basis=self.price_basis, provider="ibkr",
                    source_columns=(self.endpoint, "open", "high", "low", "close", "volume"),
                ), self.price_basis)
            except AttributeError:
                self.failure = PriceBasisError(self.price_basis, provider="ibkr", reason="missing_native_ohlcv",
                                               source_columns=(self.endpoint,))
                raise self.failure from None
            except PriceBasisError as exc:
                self.failure = exc
                raise
        self.resolved = True
        return bars

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ib, name)


def yfinance_price_basis(symbol: str) -> str:
    """Price-basis label of the last yfinance fetch for ``symbol`` (provenance only)."""
    return _YFINANCE_PRICE_BASIS.get(str(symbol or "").strip().upper(), signal_provenance.UNKNOWN)


def _coerce_yfinance_candles(frame: pd.DataFrame, *, symbol: str | None = None,
                           price_basis: str = DEFAULT_PRICE_BASIS) -> list[dict[str, Any]]:
    basis = require_supported_ohlc_basis(price_basis, provider="yfinance")
    if symbol:
        _YFINANCE_PRICE_BASIS.pop(str(symbol).strip().upper(), None)
    if frame is None or frame.empty:
        raise PriceBasisError(basis, provider="yfinance", reason="empty_history")
    columns = ["Close", "Open", "High", "Low", "Volume"]
    if basis == PriceBasis.SPLIT_DIVIDEND_ADJUSTED:
        columns.append("Adj Close")
    selected = {}
    for column in columns:
        series = _frame_column(frame, column)
        if series is None:
            raise PriceBasisError(basis, provider="yfinance", reason=f"missing_column:{column}",
                                  source_columns=tuple(selected))
        # Multiple instruments/duplicate fields cannot be silently collapsed.
        matches = [key for key in frame.columns if key == column or (
            isinstance(key, tuple) and column in key
        )]
        if len(matches) != 1:
            raise PriceBasisError(basis, provider="yfinance", reason="ambiguous_columns", source_columns=columns)
        selected[column] = series
    if not frame.index.is_unique:
        raise PriceBasisError(basis, provider="yfinance", reason="duplicate_rows", source_columns=columns)
    # Native yfinance auto_adjust=False OHLC is split adjusted. An explicit
    # per-column metadata declaration must agree with that source contract.
    declared = frame.attrs.get("field_price_basis", {})
    expected = {name: PriceBasis.SPLIT_ADJUSTED.value for name in ("Open", "High", "Low", "Close")}
    expected["Adj Close"] = PriceBasis.SPLIT_DIVIDEND_ADJUSTED.value
    if any(name in declared and declared[name] != expected[name] for name in expected):
        raise PriceBasisError(basis, provider="yfinance", reason="mixed_or_mismatched_basis", source_columns=columns)
    close_series = selected["Close"]
    candles: list[dict[str, Any]] = []
    try:
        for as_of, close in close_series.items():
            # Split+dividend OHLC: apply Adj Close / Close on EACH row to O/H/L;
            # use Adj Close itself for C. Volume is NEVER scaled by this factor.
            close_value = float(close)
            factor = float(selected["Adj Close"].loc[as_of]) / close_value if "Adj Close" in selected else 1.0
            if not isfinite(close_value) or close_value <= 0 or not isfinite(factor) or factor <= 0:
                raise ValueError("invalid factor")
            row = {
                "as_of": pd.Timestamp(as_of).to_pydatetime(),
                "open": float(selected["Open"].loc[as_of]) * factor,
                "high": float(selected["High"].loc[as_of]) * factor,
                "low": float(selected["Low"].loc[as_of]) * factor,
                "close": float(selected["Adj Close"].loc[as_of]) if "Adj Close" in selected else close_value,
                "volume": float(selected["Volume"].loc[as_of]),
            }
            candles.append(row)
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        raise PriceBasisError(basis, provider="yfinance", reason="invalid_ohlcv_or_factor", source_columns=columns) from None
    result = validate_candles(HistoricalCandles(candles, price_basis=basis, provider="yfinance", source_columns=columns), basis)
    if symbol:
        _YFINANCE_PRICE_BASIS[str(symbol).strip().upper()] = basis
    logging.getLogger(__name__).info("price_basis_resolved %s", json.dumps(result.diagnostic, sort_keys=True))
    return result


def fetch_yfinance_historical_candles(symbol: str, *, duration: str = "2 Y", bar_size: str = "1 day",
                                    price_basis: str = DEFAULT_PRICE_BASIS) -> list[dict[str, Any]]:
    basis = require_supported_ohlc_basis(price_basis, provider="yfinance")
    _YFINANCE_PRICE_BASIS.pop(str(symbol).strip().upper(), None)
    try:
        import yfinance as yf
    except Exception:
        raise PriceBasisError(basis, provider="yfinance", reason="provider_unavailable") from None

    try:
        frame = yf.download(
            str(symbol).strip().upper(),
            period=_duration_to_yfinance_period(duration),
            interval=_bar_size_to_yfinance_interval(bar_size),
            auto_adjust=False,
            progress=False,
            threads=False,
        )
    except Exception:
        raise PriceBasisError(basis, provider="yfinance", reason="provider_request_failed") from None
    return _coerce_yfinance_candles(frame, symbol=str(symbol).strip().upper(), price_basis=basis)


@dataclass(frozen=True)
class IBKRRuntimeStrategyAdapters:
    strategy_runtime: Any
    strategy_profile: str
    translator: Any
    pacing_sec: float
    resolve_run_as_of_date_fn: Any
    fetch_historical_price_series_fn: Any
    fetch_historical_price_candles_fn: Any
    map_strategy_decision_fn: Any
    fallback_historical_candles_fn: Any = fetch_yfinance_historical_candles
    build_strategy_plugin_report_payload_fn: Any = None
    load_configured_strategy_plugin_signals_fn: Any = None
    parse_strategy_plugin_mounts_fn: Any = None
    price_basis: str = DEFAULT_PRICE_BASIS

    def __post_init__(self):
        validate_price_basis(self.price_basis)

    def load_strategy_plugin_signals(self, raw_mounts):
        if not raw_mounts or self.parse_strategy_plugin_mounts_fn is None or self.load_configured_strategy_plugin_signals_fn is None:
            return (), None
        try:
            mounts = self.parse_strategy_plugin_mounts_fn(raw_mounts)
            if not mounts:
                return (), None
            return (
                self.load_configured_strategy_plugin_signals_fn(
                    mounts,
                    strategy_profile=self.strategy_profile,
                ),
                None,
            )
        except Exception as exc:
            return (), f"{type(exc).__name__}: {exc}"

    def attach_strategy_plugin_report(self, report, *, signals, error: str | None = None):
        if signals and self.build_strategy_plugin_report_payload_fn is not None:
            report.setdefault("summary", {}).update(self.build_strategy_plugin_report_payload_fn(signals))
        if error:
            report.setdefault("diagnostics", {})["strategy_plugin_error"] = error

    def translate_strategy_plugin_value(self, category: str, raw_value: str | None) -> str:
        return translate_strategy_plugin_value(category, raw_value, translator=self.translator)

    def build_strategy_plugin_notification_lines(self, signals) -> tuple[str, ...]:
        return build_strategy_plugin_notification_lines(signals, translator=self.translator)

    def build_strategy_plugin_error_notification_lines(self, error) -> tuple[str, ...]:
        return build_strategy_plugin_error_notification_lines(error, translator=self.translator)

    def should_alert_strategy_plugin_signal(self, signal) -> bool:
        return should_alert_strategy_plugin_signal(signal)

    def build_strategy_plugin_alert_messages(self, signals):
        return build_strategy_plugin_alert_messages(
            signals,
            translator=self.translator,
            strategy_label=self.strategy_profile,
        )

    def _get_fallback_historical_candles(self, symbol, *, duration: str, bar_size: str):
        require_supported_ohlc_basis(self.price_basis, provider="history_loader")
        if self.fallback_historical_candles_fn is None:
            return None
        try:
            candles = self.fallback_historical_candles_fn(
                symbol, duration=duration, bar_size=bar_size, price_basis=self.price_basis,
            )
        except PriceBasisError:
            raise
        except Exception:
            raise PriceBasisError(self.price_basis, provider="history_loader", reason="provider_request_failed") from None
        return validate_candles(candles, self.price_basis)

    def _record_history(self, symbol, dates, diagnostic):
        signal_provenance.record(
            symbol, source=diagnostic["provider"], price_basis=self.price_basis,
            dates=dates, basis_details=diagnostic,
        )
        logging.getLogger(__name__).info("price_basis_resolved %s", json.dumps(diagnostic, sort_keys=True))

    def _fetch_native(self, fn, ib, symbol, *, duration, bar_size, ohlc=False):
        proxy = BasisHistoricalIB(ib, self.price_basis, ohlc=ohlc)
        try:
            result = fn(proxy, symbol, duration=duration, bar_size=bar_size)
        except PriceBasisError:
            raise
        except Exception:
            raise PriceBasisError(self.price_basis, provider="ibkr", reason="provider_request_failed",
                                  source_columns=(proxy.endpoint,)) from None
        if proxy.failure is not None:
            raise proxy.failure
        if not proxy.resolved:
            raise PriceBasisError(self.price_basis, provider="ibkr", reason="unverified_provider_response",
                                  source_columns=(proxy.endpoint,))
        return result, basis_diagnostic(self.price_basis, provider="ibkr", resolved=self.price_basis,
                                        source_columns=(proxy.endpoint, "Open", "High", "Low", "Close", "Volume"))

    def get_historical_close(self, ib, symbol, duration="2 Y", bar_size="1 day"):
        fallback_candles = self._get_fallback_historical_candles(symbol, duration=duration, bar_size=bar_size)
        fallback_points = tuple(
            (candle["as_of"], candle["close"])
            for candle in (fallback_candles or ())
        )
        if fallback_points:
            self._record_history(symbol, [as_of for as_of, _ in fallback_points], fallback_candles.diagnostic)
            return pd.Series(
                data=[close for _, close in fallback_points],
                index=pd.to_datetime([as_of for as_of, _ in fallback_points]),
            )

        series, diagnostic = self._fetch_native(
            self.fetch_historical_price_series_fn, ib, symbol,
            duration=duration,
            bar_size=bar_size,
        )
        points = tuple(series.points or ())
        if not points or any(not isfinite(float(point.close)) or float(point.close) <= 0 for point in points):
            raise PriceBasisError(self.price_basis, provider="ibkr", reason="empty_or_invalid_close",
                                  source_columns=diagnostic["source_columns"])
        diagnostic["source_columns"] = [diagnostic["source_columns"][0], "Close"]
        self._record_history(symbol, [point.as_of for point in points], diagnostic)
        return pd.Series(
            data=[point.close for point in points],
            index=pd.to_datetime([point.as_of for point in points]),
        )

    def get_historical_candles(self, ib, symbol, duration="2 Y", bar_size="1 day"):
        fallback_candles = self._get_fallback_historical_candles(symbol, duration=duration, bar_size=bar_size)
        if fallback_candles:
            self._record_history(symbol, [candle["as_of"] for candle in fallback_candles], fallback_candles.diagnostic)
            return fallback_candles

        candles, diagnostic = self._fetch_native(
            self.fetch_historical_price_candles_fn, ib, symbol,
            duration=duration,
            bar_size=bar_size,
            ohlc=True,
        )
        candles = validate_candles(HistoricalCandles(
            candles, price_basis=self.price_basis, provider="ibkr", source_columns=diagnostic["source_columns"],
        ), self.price_basis)
        self._record_history(symbol, [candle["as_of"] for candle in candles], diagnostic)
        return candles

    def compute_signals(self, ib, current_holdings, *, strategy_plugin_signals=()):
        evaluate_kwargs = {
            "ib": ib,
            "current_holdings": current_holdings,
            "historical_close_loader": self.get_historical_close,
            "historical_candle_loader": self.get_historical_candles,
            "run_as_of": self.resolve_run_as_of_date_fn(),
            "translator": self.translator,
            "pacing_sec": self.pacing_sec,
        }
        if strategy_plugin_signals:
            evaluate_kwargs["strategy_plugin_signals"] = tuple(strategy_plugin_signals or ())
        evaluation = self.strategy_runtime.evaluate(**evaluate_kwargs)
        runtime_settings = getattr(self.strategy_runtime, "runtime_settings", None)
        metadata = dict(evaluation.metadata)
        if runtime_settings is not None:
            metadata["account_ids"] = tuple(getattr(runtime_settings, "account_ids", ()) or ())
        return self.map_strategy_decision_fn(
            evaluation.decision,
            strategy_profile=self.strategy_profile,
            runtime_metadata=metadata,
        )


def build_runtime_strategy_adapters(
    *,
    strategy_runtime: Any,
    strategy_profile: str,
    translator,
    pacing_sec: float,
    resolve_run_as_of_date_fn,
    fetch_historical_price_series_fn,
    fetch_historical_price_candles_fn,
    map_strategy_decision_fn,
    fallback_historical_candles_fn=fetch_yfinance_historical_candles,
    build_strategy_plugin_report_payload_fn=None,
    load_configured_strategy_plugin_signals_fn=None,
    parse_strategy_plugin_mounts_fn=None,
    price_basis=None,
) -> IBKRRuntimeStrategyAdapters:
    config = getattr(strategy_runtime, "merged_runtime_config", {}) or {}
    basis = validate_price_basis(config.get("price_basis", DEFAULT_PRICE_BASIS) if price_basis is None else price_basis)
    return IBKRRuntimeStrategyAdapters(
        strategy_runtime=strategy_runtime,
        strategy_profile=str(strategy_profile),
        translator=translator,
        pacing_sec=float(pacing_sec),
        resolve_run_as_of_date_fn=resolve_run_as_of_date_fn,
        fetch_historical_price_series_fn=fetch_historical_price_series_fn,
        fetch_historical_price_candles_fn=fetch_historical_price_candles_fn,
        map_strategy_decision_fn=map_strategy_decision_fn,
        fallback_historical_candles_fn=fallback_historical_candles_fn,
        build_strategy_plugin_report_payload_fn=build_strategy_plugin_report_payload_fn,
        load_configured_strategy_plugin_signals_fn=load_configured_strategy_plugin_signals_fn,
        parse_strategy_plugin_mounts_fn=parse_strategy_plugin_mounts_fn,
        price_basis=basis,
    )
