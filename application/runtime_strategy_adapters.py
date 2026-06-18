"""Builder helpers for IBKR strategy evaluation adapters."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
import re
from typing import Any

import pandas as pd
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


def _coerce_yfinance_candles(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    close_series = _frame_column(frame, "Adj Close")
    if close_series is None:
        close_series = _frame_column(frame, "Close")
    if close_series is None:
        return []
    open_series = _frame_column(frame, "Open")
    high_series = _frame_column(frame, "High")
    low_series = _frame_column(frame, "Low")
    volume_series = _frame_column(frame, "Volume")
    candles: list[dict[str, Any]] = []
    for as_of, close in close_series.dropna().items():
        close_value = float(close)
        candles.append(
            {
                "as_of": pd.Timestamp(as_of).to_pydatetime(),
                "open": float(open_series.get(as_of, close_value)) if open_series is not None else close_value,
                "high": float(high_series.get(as_of, close_value)) if high_series is not None else close_value,
                "low": float(low_series.get(as_of, close_value)) if low_series is not None else close_value,
                "close": close_value,
                "volume": float(volume_series.get(as_of, 0.0) or 0.0) if volume_series is not None else 0.0,
            }
        )
    return candles


def fetch_yfinance_historical_candles(symbol: str, *, duration: str = "2 Y", bar_size: str = "1 day") -> list[dict[str, Any]]:
    try:
        import yfinance as yf
    except Exception:
        return []

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
        return []
    return _coerce_yfinance_candles(frame)


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

    def _get_fallback_historical_candles(self, symbol, *, duration: str, bar_size: str) -> tuple[dict[str, Any], ...]:
        if self.fallback_historical_candles_fn is None:
            return ()
        try:
            return tuple(self.fallback_historical_candles_fn(symbol, duration=duration, bar_size=bar_size) or ())
        except Exception:
            return ()

    def get_historical_close(self, ib, symbol, duration="2 Y", bar_size="1 day"):
        fallback_candles = self._get_fallback_historical_candles(symbol, duration=duration, bar_size=bar_size)
        fallback_points = tuple(
            (candle["as_of"], candle["close"])
            for candle in fallback_candles
            if "close" in candle
        )
        if fallback_points:
            return pd.Series(
                data=[close for _, close in fallback_points],
                index=pd.to_datetime([as_of for as_of, _ in fallback_points]),
            )

        series = self.fetch_historical_price_series_fn(
            ib,
            symbol,
            duration=duration,
            bar_size=bar_size,
        )
        points = tuple(series.points or ())
        if points:
            return pd.Series(
                data=[point.close for point in points],
                index=pd.to_datetime([point.as_of for point in points]),
            )
        return pd.Series(dtype=float)

    def get_historical_candles(self, ib, symbol, duration="2 Y", bar_size="1 day"):
        fallback_candles = self._get_fallback_historical_candles(symbol, duration=duration, bar_size=bar_size)
        if fallback_candles:
            return list(fallback_candles)

        candles = self.fetch_historical_price_candles_fn(
            ib,
            symbol,
            duration=duration,
            bar_size=bar_size,
        )
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
        return self.map_strategy_decision_fn(
            evaluation.decision,
            strategy_profile=self.strategy_profile,
            runtime_metadata=evaluation.metadata,
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
) -> IBKRRuntimeStrategyAdapters:
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
    )
