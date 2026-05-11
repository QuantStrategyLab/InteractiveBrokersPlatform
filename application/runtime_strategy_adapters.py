"""Builder helpers for IBKR strategy evaluation adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd


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
        value = str(raw_value or "").strip() or "unknown"
        if category == "route" and value == "taco_fake_crisis":
            value = "unknown_route"
        elif category == "action" and value == "small_taco":
            value = "unknown_action"
        key = f"strategy_plugin_{category}_{value}"
        translated = self.translator(key)
        return translated if translated != key else value

    def build_strategy_plugin_notification_lines(self, signals) -> tuple[str, ...]:
        lines = []
        for signal in signals:
            route = signal.canonical_route or "unknown_route"
            action = signal.suggested_action or "unknown_action"
            lines.append(
                self.translator(
                    "strategy_plugin_line",
                    plugin=self.translate_strategy_plugin_value("name", signal.plugin),
                    mode=self.translate_strategy_plugin_value("mode", signal.effective_mode),
                    route=self.translate_strategy_plugin_value("route", route),
                    action=self.translate_strategy_plugin_value("action", action),
                )
            )
        return tuple(lines)

    def get_historical_close(self, ib, symbol, duration="2 Y", bar_size="1 day"):
        series = self.fetch_historical_price_series_fn(
            ib,
            symbol,
            duration=duration,
            bar_size=bar_size,
        )
        if not series.points:
            return pd.Series(dtype=float)
        return pd.Series(
            data=[point.close for point in series.points],
            index=pd.to_datetime([point.as_of for point in series.points]),
        )

    def get_historical_candles(self, ib, symbol, duration="2 Y", bar_size="1 day"):
        return self.fetch_historical_price_candles_fn(
            ib,
            symbol,
            duration=duration,
            bar_size=bar_size,
        )

    def compute_signals(self, ib, current_holdings):
        evaluation = self.strategy_runtime.evaluate(
            ib=ib,
            current_holdings=current_holdings,
            historical_close_loader=self.get_historical_close,
            historical_candle_loader=self.get_historical_candles,
            run_as_of=self.resolve_run_as_of_date_fn(),
            translator=self.translator,
            pacing_sec=self.pacing_sec,
        )
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
        build_strategy_plugin_report_payload_fn=build_strategy_plugin_report_payload_fn,
        load_configured_strategy_plugin_signals_fn=load_configured_strategy_plugin_signals_fn,
        parse_strategy_plugin_mounts_fn=parse_strategy_plugin_mounts_fn,
    )
