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
    )
