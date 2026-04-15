#!/usr/bin/env python3
"""Research-only reconstruction of the video QQQ/TQQQ dual-drive idea.

The YouTube video describes a QQQ/TQQQ strategy but does not publish exact code.
This script keeps the reconstruction explicit and parameterized, then compares
several execution assumptions against the existing tqqq_growth_income research
benchmarks on the same Yahoo Finance adjusted data.

The important distinction is execution timing:
- next_close is implementable: signal from today's close affects tomorrow's return.
- same_close_lookahead is intentionally biased: today's close signal is applied to
  today's close-to-close return.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import backtest_stock_alpha_suite as suite  # noqa: E402
import backtest_tqqq_growth_indicator_variants as tqqq_base  # noqa: E402

DEFAULT_RESULTS_DIR = CURRENT_DIR / "results"
DEFAULT_DOWNLOAD_START = "2016-01-01"
DEFAULT_PERIOD_START = "2017-01-03"
DEFAULT_PERIOD_END = None
DEFAULT_COSTS_BPS = (0.0, 5.0, 10.0)
CASH_SYMBOL = "CASH"
VIDEO_REPORTED = {
    "CAGR": 0.494,
    "Max Drawdown": -0.361,
    "2022 Return": -0.158,
}


@dataclass(frozen=True)
class VideoConfig:
    name: str
    description: str
    execution_mode: str
    bull_qqq_weight: float = 0.45
    bull_tqqq_weight: float = 0.45
    cash_weight: float = 0.10
    require_ma20_slope: bool = True
    allow_below_ma200_pullback: bool = False
    pullback_qqq_weight: float = 0.45
    pullback_tqqq_weight: float = 0.45
    pullback_cash_weight: float = 0.10
    use_tqqq_overheat_exit: bool = False
    overheat_multiple: float = 2.2
    overheat_exit_qqq_weight: float = 0.45
    overheat_exit_cash_weight: float = 0.55


@dataclass
class StrategyRun:
    strategy_name: str
    display_name: str
    gross_returns: pd.Series
    weights_history: pd.DataFrame
    turnover_history: pd.Series
    metadata: dict[str, object]


@dataclass(frozen=True)
class RuntimeDualDriveConfig:
    name: str
    description: str
    qqq_idle_mode: str  # off | tqqq_active | above_ma200 | above_ma200_ma20_slope
    qqq_idle_fraction: float


VIDEO_CONFIGS = (
    VideoConfig(
        name="video_like_next_close",
        description=(
            "Transcript reconstruction: hold 45% QQQ + 45% TQQQ + 10% cash "
            "when QQQ is above MA200 and MA20 slope is positive; otherwise cash. "
            "Trades take effect on the next close-to-close return."
        ),
        execution_mode="next_close",
    ),
    VideoConfig(
        name="video_like_no_slope_next_close",
        description=(
            "Same as video_like_next_close but does not wait for positive MA20 "
            "slope after reclaiming MA200."
        ),
        execution_mode="next_close",
        require_ma20_slope=False,
    ),
    VideoConfig(
        name="video_like_pullback_next_close",
        description=(
            "Adds a speculative below-MA200 pullback state: if QQQ is below MA200 "
            "but above MA20 with positive MA20 slope, use the same 45/45/10 risk-on "
            "weights. This approximates the video's 'low buy/high sell below MA200' "
            "comment, but the exact rule is not disclosed."
        ),
        execution_mode="next_close",
        allow_below_ma200_pullback=True,
    ),
    VideoConfig(
        name="video_like_overheat_next_close",
        description=(
            "Adds a speculative TQQQ overheat exit: when TQQQ closes above "
            "2.2x its MA200, drop TQQQ and keep 45% QQQ / 55% cash until the "
            "normal trend condition resumes without overheat."
        ),
        execution_mode="next_close",
        use_tqqq_overheat_exit=True,
    ),
    VideoConfig(
        name="video_like_same_close_lookahead",
        description=(
            "Intentionally biased variant: uses today's close to choose today's "
            "weights for today's close-to-close return. Included only to measure "
            "how much lookahead can inflate the video-like result."
        ),
        execution_mode="same_close_lookahead",
    ),
    VideoConfig(
        name="buy_hold_45_45_10",
        description="Daily-rebalanced 45% QQQ + 45% TQQQ + 10% cash reference.",
        execution_mode="buy_hold",
        require_ma20_slope=False,
    ),
)


RUNTIME_DUAL_DRIVE_CONFIGS = (
    RuntimeDualDriveConfig(
        name="tqqq_growth_income_runtime_dual_drive_tqqq_active_50qqq",
        description=(
            "Current full tqqq_growth_income, but when the TQQQ attack sleeve is active, "
            "move 50% of the non-TQQQ/non-income idle sleeve from BOXX to QQQ."
        ),
        qqq_idle_mode="tqqq_active",
        qqq_idle_fraction=0.50,
    ),
    RuntimeDualDriveConfig(
        name="tqqq_growth_income_runtime_dual_drive_tqqq_active_100qqq",
        description=(
            "Current full tqqq_growth_income, but when the TQQQ attack sleeve is active, "
            "move all non-TQQQ/non-income idle capital from BOXX to QQQ."
        ),
        qqq_idle_mode="tqqq_active",
        qqq_idle_fraction=1.00,
    ),
    RuntimeDualDriveConfig(
        name="tqqq_growth_income_runtime_dual_drive_ma200_50qqq",
        description=(
            "Current full tqqq_growth_income, but when QQQ is above MA200, move 50% "
            "of the non-TQQQ/non-income idle sleeve from BOXX to QQQ."
        ),
        qqq_idle_mode="above_ma200",
        qqq_idle_fraction=0.50,
    ),
    RuntimeDualDriveConfig(
        name="tqqq_growth_income_runtime_dual_drive_ma200_slope_50qqq",
        description=(
            "Current full tqqq_growth_income, but only when QQQ is above MA200 and "
            "MA20 slope is positive, move 50% of the idle sleeve from BOXX to QQQ."
        ),
        qqq_idle_mode="above_ma200_ma20_slope",
        qqq_idle_fraction=0.50,
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR))
    parser.add_argument("--download-start", default=DEFAULT_DOWNLOAD_START)
    parser.add_argument("--period-start", default=DEFAULT_PERIOD_START)
    parser.add_argument("--period-end", default=DEFAULT_PERIOD_END)
    parser.add_argument("--end", default=None, help="Data download end date, exclusive in yfinance.")
    parser.add_argument("--cost-bps", nargs="*", type=float, default=list(DEFAULT_COSTS_BPS))
    return parser.parse_args()


def load_market_data(*, start: str, end: str | None):
    etf_frames = suite.download_etf_ohlcv(("QQQ", "TQQQ", "BOXX", "SPYI", "QQQI"), start=start, end=end)
    qqq_ohlc = pd.DataFrame(
        {
            "open": etf_frames["open"]["QQQ"],
            "high": etf_frames["high"]["QQQ"],
            "low": etf_frames["low"]["QQQ"],
            "close": etf_frames["close"]["QQQ"],
        }
    ).dropna()
    tqqq_ohlc = pd.DataFrame(
        {
            "open": etf_frames["open"]["TQQQ"],
            "high": etf_frames["high"]["TQQQ"],
            "low": etf_frames["low"]["TQQQ"],
            "close": etf_frames["close"]["TQQQ"],
        }
    ).dropna()
    master_index = qqq_ohlc.index.intersection(tqqq_ohlc.index)
    qqq_ohlc = qqq_ohlc.reindex(master_index).dropna()
    tqqq_ohlc = tqqq_ohlc.reindex(master_index).dropna()
    rows = suite.build_extra_etf_price_history(etf_frames, symbols=("QQQ", "TQQQ", "BOXX", "SPYI", "QQQI"))
    _close_matrix, returns_matrix = suite.build_asset_return_matrix(
        rows,
        master_index=master_index,
        required_symbols=("QQQ", "TQQQ", "BOXX", "SPYI", "QQQI"),
    )
    returns_matrix[CASH_SYMBOL] = 0.0
    indicators = build_indicator_frame(qqq_ohlc, tqqq_ohlc)
    return qqq_ohlc, returns_matrix, indicators


def build_indicator_frame(qqq_ohlc: pd.DataFrame, tqqq_ohlc: pd.DataFrame) -> pd.DataFrame:
    index = qqq_ohlc.index.intersection(tqqq_ohlc.index)
    qqq_close = qqq_ohlc["close"].reindex(index)
    tqqq_close = tqqq_ohlc["close"].reindex(index)
    frame = pd.DataFrame(index=index)
    frame["qqq_close"] = qqq_close
    frame["tqqq_close"] = tqqq_close
    frame["qqq_ma20"] = qqq_close.rolling(20).mean()
    frame["qqq_ma200"] = qqq_close.rolling(200).mean()
    frame["qqq_ma20_slope"] = frame["qqq_ma20"].diff()
    frame["tqqq_ma20"] = tqqq_close.rolling(20).mean()
    frame["tqqq_ma200"] = tqqq_close.rolling(200).mean()
    frame["tqqq_overheat_ratio"] = tqqq_close / frame["tqqq_ma200"]
    return frame


def normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    cleaned = {symbol: max(0.0, float(weight)) for symbol, weight in weights.items()}
    total = sum(cleaned.values())
    if total <= 0:
        return {CASH_SYMBOL: 1.0}
    if abs(total - 1.0) > 1e-9:
        cleaned = {symbol: weight / total for symbol, weight in cleaned.items()}
    return {symbol: weight for symbol, weight in cleaned.items() if weight > 1e-12}


def decide_video_weights(
    config: VideoConfig,
    row: pd.Series,
    *,
    risk_active: bool,
) -> tuple[dict[str, float], bool]:
    qqq_close = float(row["qqq_close"])
    qqq_ma20 = float(row["qqq_ma20"]) if pd.notna(row["qqq_ma20"]) else float("nan")
    qqq_ma200 = float(row["qqq_ma200"]) if pd.notna(row["qqq_ma200"]) else float("nan")
    qqq_ma20_slope = float(row["qqq_ma20_slope"]) if pd.notna(row["qqq_ma20_slope"]) else float("nan")
    tqqq_overheat_ratio = (
        float(row["tqqq_overheat_ratio"]) if pd.notna(row["tqqq_overheat_ratio"]) else float("nan")
    )

    has_long_history = pd.notna(qqq_ma200)
    above_ma200 = has_long_history and qqq_close > qqq_ma200
    positive_ma20_slope = pd.notna(qqq_ma20_slope) and qqq_ma20_slope > 0.0
    slope_ok = positive_ma20_slope if config.require_ma20_slope else True
    entry_confirmed = above_ma200 and slope_ok

    overheat = (
        config.use_tqqq_overheat_exit
        and pd.notna(tqqq_overheat_ratio)
        and tqqq_overheat_ratio >= config.overheat_multiple
    )

    next_risk_active = risk_active
    if risk_active and has_long_history and not above_ma200:
        next_risk_active = False
    elif not risk_active and entry_confirmed:
        next_risk_active = True

    if next_risk_active:
        if overheat:
            return (
                normalize_weights(
                    {
                        "QQQ": config.overheat_exit_qqq_weight,
                        "TQQQ": 0.0,
                        CASH_SYMBOL: config.overheat_exit_cash_weight,
                    }
                ),
                next_risk_active,
            )
        return (
            normalize_weights(
                {
                    "QQQ": config.bull_qqq_weight,
                    "TQQQ": config.bull_tqqq_weight,
                    CASH_SYMBOL: config.cash_weight,
                }
            ),
            next_risk_active,
        )

    pullback_risk_on = (
        config.allow_below_ma200_pullback
        and has_long_history
        and not above_ma200
        and pd.notna(qqq_ma20)
        and qqq_close > qqq_ma20
        and positive_ma20_slope
    )
    if pullback_risk_on:
        return (
            normalize_weights(
                {
                    "QQQ": config.pullback_qqq_weight,
                    "TQQQ": config.pullback_tqqq_weight,
                    CASH_SYMBOL: config.pullback_cash_weight,
                }
            ),
            next_risk_active,
        )

    return {CASH_SYMBOL: 1.0}, next_risk_active


def compute_turnover(old_weights: dict[str, float], new_weights: dict[str, float]) -> float:
    symbols = set(old_weights) | set(new_weights)
    return 0.5 * sum(abs(float(new_weights.get(symbol, 0.0)) - float(old_weights.get(symbol, 0.0))) for symbol in symbols)


def run_video_backtest(
    config: VideoConfig,
    returns_matrix: pd.DataFrame,
    indicators: pd.DataFrame,
) -> StrategyRun:
    index = returns_matrix.index.intersection(indicators.index)
    asset_columns = ("QQQ", "TQQQ", CASH_SYMBOL)
    weights_history = pd.DataFrame(0.0, index=index, columns=asset_columns)
    portfolio_returns = pd.Series(0.0, index=index, name=config.name)
    turnover_history = pd.Series(0.0, index=index, name="turnover")

    if config.execution_mode == "buy_hold":
        current_weights = normalize_weights(
            {"QQQ": config.bull_qqq_weight, "TQQQ": config.bull_tqqq_weight, CASH_SYMBOL: config.cash_weight}
        )
        for idx in range(1, len(index)):
            date = index[idx]
            for symbol, weight in current_weights.items():
                weights_history.at[date, symbol] = weight
            row_returns = returns_matrix.loc[date]
            portfolio_returns.at[date] = sum(weight * float(row_returns.get(symbol, 0.0)) for symbol, weight in current_weights.items())
        return StrategyRun(
            strategy_name=config.name,
            display_name=config.name,
            gross_returns=portfolio_returns,
            weights_history=weights_history,
            turnover_history=turnover_history,
            metadata={
                "family": "video_qqq_tqqq_dual_drive",
                "execution_mode": config.execution_mode,
                "description": config.description,
                "known_limitation": "Daily rebalanced reference, not a disclosed video state machine.",
            },
        )

    current_weights: dict[str, float] = {CASH_SYMBOL: 1.0}
    risk_active = False
    if config.execution_mode == "next_close":
        iterable = range(len(index) - 1)
        for idx in iterable:
            date = index[idx]
            next_date = index[idx + 1]
            target_weights, risk_active = decide_video_weights(
                config,
                indicators.loc[date],
                risk_active=risk_active,
            )
            if target_weights != current_weights:
                turnover_history.at[next_date] = compute_turnover(current_weights, target_weights)
                current_weights = target_weights
            for symbol, weight in current_weights.items():
                weights_history.at[date, symbol] = weight
            row_returns = returns_matrix.loc[next_date]
            portfolio_returns.at[next_date] = sum(
                weight * float(row_returns.get(symbol, 0.0))
                for symbol, weight in current_weights.items()
                if symbol != CASH_SYMBOL
            )
    elif config.execution_mode == "same_close_lookahead":
        for idx in range(1, len(index)):
            date = index[idx]
            target_weights, risk_active = decide_video_weights(
                config,
                indicators.loc[date],
                risk_active=risk_active,
            )
            if target_weights != current_weights:
                turnover_history.at[date] = compute_turnover(current_weights, target_weights)
                current_weights = target_weights
            for symbol, weight in current_weights.items():
                weights_history.at[date, symbol] = weight
            row_returns = returns_matrix.loc[date]
            portfolio_returns.at[date] = sum(
                weight * float(row_returns.get(symbol, 0.0))
                for symbol, weight in current_weights.items()
                if symbol != CASH_SYMBOL
            )
    else:
        raise KeyError(f"Unknown execution mode: {config.execution_mode}")

    for symbol, weight in current_weights.items():
        weights_history.at[index[-1], symbol] = weight

    limitation = "Approximate reconstruction; exact video state machine and high-exit logic are not public."
    if config.execution_mode == "same_close_lookahead":
        limitation = "Biased lookahead control; not implementable as stated."
    return StrategyRun(
        strategy_name=config.name,
        display_name=config.name,
        gross_returns=portfolio_returns,
        weights_history=weights_history,
        turnover_history=turnover_history,
        metadata={
            "family": "video_qqq_tqqq_dual_drive",
            "execution_mode": config.execution_mode,
            "description": config.description,
            "known_limitation": limitation,
        },
    )


def build_current_strategy_references(qqq_ohlc: pd.DataFrame, returns_matrix: pd.DataFrame) -> list[tqqq_base.StrategyRun]:
    indicator_frame = tqqq_base.build_indicator_frame(
        qqq_ohlc,
        pd.Series(0.0, index=qqq_ohlc.index, name="volume"),
    )
    baseline_overlay = tqqq_base.OverlayConfig(
        name="baseline",
        description="Current MA200 + ATR baseline with no extra daily gate.",
    )
    return [
        tqqq_base.build_runtime_full_reference(qqq_ohlc, returns_matrix),
        tqqq_base.run_attack_only_variant_backtest(
            qqq_ohlc,
            returns_matrix,
            indicator_frame,
            config=tqqq_base.BacktestConfig(
                overlay=baseline_overlay,
                idle_asset=tqqq_base.SAFE_HAVEN,
                income_mode="attack_only",
            ),
        ),
        tqqq_base.run_attack_only_variant_backtest(
            qqq_ohlc,
            returns_matrix,
            indicator_frame,
            config=tqqq_base.BacktestConfig(
                overlay=baseline_overlay,
                idle_asset="QQQ",
                income_mode="attack_only",
            ),
        ),
    ]


def convert_base_run(run: tqqq_base.StrategyRun) -> StrategyRun:
    return StrategyRun(
        strategy_name=run.strategy_name,
        display_name=run.display_name,
        gross_returns=run.gross_returns,
        weights_history=run.weights_history,
        turnover_history=run.turnover_history,
        metadata=dict(run.metadata),
    )


def should_use_runtime_qqq_idle(
    config: RuntimeDualDriveConfig,
    *,
    target_tqqq_ratio: float,
    qqq_p: float,
    ma200: float,
    ma20_slope: float,
) -> bool:
    if config.qqq_idle_fraction <= 0.0 or config.qqq_idle_mode == "off":
        return False
    if config.qqq_idle_mode == "tqqq_active":
        return target_tqqq_ratio > 1e-12
    if config.qqq_idle_mode == "above_ma200":
        return pd.notna(ma200) and qqq_p > ma200
    if config.qqq_idle_mode == "above_ma200_ma20_slope":
        return pd.notna(ma200) and qqq_p > ma200 and pd.notna(ma20_slope) and ma20_slope > 0.0
    raise KeyError(f"Unknown runtime dual-drive idle mode: {config.qqq_idle_mode}")


def run_runtime_dual_drive_variant(
    config: RuntimeDualDriveConfig,
    qqq_ohlc: pd.DataFrame,
    asset_returns: pd.DataFrame,
    *,
    starting_equity: float = tqqq_base.RUNTIME_FULL_STARTING_EQUITY,
    income_threshold_usd: float = 100_000.0,
    qqqi_income_ratio: float = 0.50,
    cash_reserve_ratio: float = 0.05,
    rebalance_threshold_ratio: float = 0.01,
    alloc_tier1_breakpoints: Iterable[float] = (0, 15_000, 30_000, 70_000),
    alloc_tier1_values: Iterable[float] = (1.0, 0.95, 0.85, 0.70),
    alloc_tier2_breakpoints: Iterable[float] = (70_000, 140_000),
    alloc_tier2_values: Iterable[float] = (0.70, 0.50),
    risk_leverage_factor: float = 3.0,
    risk_agg_cap: float = 0.50,
    risk_numerator: float = 0.30,
    atr_exit_scale: float = 2.0,
    atr_entry_scale: float = 2.5,
    exit_line_floor: float = 0.92,
    exit_line_cap: float = 0.98,
    entry_line_floor: float = 1.02,
    entry_line_cap: float = 1.08,
) -> StrategyRun:
    strategy_symbols = ["TQQQ", "QQQ", "BOXX", "SPYI", "QQQI", CASH_SYMBOL]
    index = asset_returns.index.intersection(qqq_ohlc.index)
    qqq_history = qqq_ohlc.loc[index].copy()
    returns = asset_returns.reindex(index).fillna(0.0)
    close = qqq_history["close"]
    ma20_series = close.rolling(20).mean()
    ma200_series = close.rolling(200).mean()
    ma20_slope_series = ma20_series.diff()
    true_range = pd.concat(
        [
            qqq_history["high"] - qqq_history["low"],
            (qqq_history["high"] - close.shift(1)).abs(),
            (qqq_history["low"] - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr_pct_series = true_range.rolling(14).mean() / close
    weights_history = pd.DataFrame(0.0, index=index, columns=strategy_symbols)
    portfolio_returns = pd.Series(0.0, index=index, name=config.name)
    turnover_history = pd.Series(0.0, index=index, name="turnover")

    current_weights: dict[str, float] = {"BOXX": 1.0}
    current_equity = float(starting_equity)

    for idx in range(len(index) - 1):
        date = index[idx]
        next_date = index[idx + 1]
        qqq_p = float(close.loc[date])
        ma200 = float(ma200_series.loc[date])
        ma20_slope = float(ma20_slope_series.loc[date])
        atr_pct = float(atr_pct_series.loc[date]) if pd.notna(atr_pct_series.loc[date]) else 0.0
        exit_line = ma200 * max(exit_line_floor, min(exit_line_cap, 1.0 - (atr_pct * atr_exit_scale)))
        entry_line = ma200 * max(entry_line_floor, min(entry_line_cap, 1.0 + (atr_pct * atr_entry_scale)))

        income_ratio = suite.get_hybrid_income_ratio(current_equity, income_threshold_usd=income_threshold_usd)
        target_income_value = current_equity * income_ratio
        target_spyi_value = target_income_value * (1.0 - qqqi_income_ratio)
        target_qqqi_value = target_income_value * qqqi_income_ratio

        strategy_equity = max(0.0, current_equity - target_income_value)
        reserved = strategy_equity * cash_reserve_ratio
        agg_ratio, _target_yield = suite.get_hybrid_allocation(
            strategy_equity,
            qqq_p,
            exit_line,
            alloc_tier1_breakpoints=alloc_tier1_breakpoints,
            alloc_tier1_values=alloc_tier1_values,
            alloc_tier2_breakpoints=alloc_tier2_breakpoints,
            alloc_tier2_values=alloc_tier2_values,
            risk_leverage_factor=risk_leverage_factor,
            risk_agg_cap=risk_agg_cap,
            risk_numerator=risk_numerator,
        )

        target_tqqq_ratio = 0.0
        if current_weights.get("TQQQ", 0.0) > 1e-12:
            if qqq_p < exit_line:
                target_tqqq_ratio = 0.0
            elif qqq_p < ma200:
                target_tqqq_ratio = agg_ratio * 0.33
            else:
                target_tqqq_ratio = agg_ratio
        elif qqq_p > entry_line:
            target_tqqq_ratio = agg_ratio

        target_tqqq_value = strategy_equity * target_tqqq_ratio
        idle_value = max(0.0, (strategy_equity - reserved) - target_tqqq_value)
        use_qqq_idle = should_use_runtime_qqq_idle(
            config,
            target_tqqq_ratio=target_tqqq_ratio,
            qqq_p=qqq_p,
            ma200=ma200,
            ma20_slope=ma20_slope,
        )
        qqq_idle_value = idle_value * min(1.0, max(0.0, config.qqq_idle_fraction)) if use_qqq_idle else 0.0
        boxx_value = max(0.0, idle_value - qqq_idle_value)
        target_values = {
            "TQQQ": target_tqqq_value,
            "QQQ": qqq_idle_value,
            "BOXX": boxx_value,
            "SPYI": target_spyi_value,
            "QQQI": target_qqqi_value,
            CASH_SYMBOL: reserved,
        }
        target_weights = {
            symbol: value / current_equity
            for symbol, value in target_values.items()
            if value > 1e-12 and current_equity > 0.0
        }
        if not target_weights:
            target_weights = {CASH_SYMBOL: 1.0}

        rebalance_needed = any(
            abs(target_weights.get(symbol, 0.0) - current_weights.get(symbol, 0.0)) > rebalance_threshold_ratio
            for symbol in set(target_weights) | set(current_weights)
        )
        if rebalance_needed:
            turnover_history.at[next_date] = suite.compute_turnover(current_weights, target_weights)
            current_weights = target_weights

        for symbol, weight in current_weights.items():
            if symbol in weights_history.columns:
                weights_history.at[date, symbol] = weight

        next_returns = returns.loc[next_date]
        portfolio_returns.at[next_date] = sum(
            weight * float(next_returns.get(symbol, 0.0))
            for symbol, weight in current_weights.items()
            if symbol != CASH_SYMBOL
        )
        current_equity *= 1.0 + float(portfolio_returns.at[next_date])

    for symbol, weight in current_weights.items():
        if symbol in weights_history.columns:
            weights_history.at[index[-1], symbol] = weight

    return StrategyRun(
        strategy_name=config.name,
        display_name=config.name,
        gross_returns=portfolio_returns,
        weights_history=weights_history,
        turnover_history=turnover_history,
        metadata={
            "family": "hybrid_growth_runtime_dual_drive_candidate",
            "execution_mode": "next_close",
            "description": config.description,
            "qqq_idle_mode": config.qqq_idle_mode,
            "qqq_idle_fraction": config.qqq_idle_fraction,
            "known_limitation": "Research-only candidate; not enabled in live defaults.",
        },
    )


def summarize_run(
    run: StrategyRun,
    benchmark_returns: pd.Series,
    *,
    cost_bps: float,
    start: str | None,
    end: str | None,
) -> dict[str, object]:
    returns = run.gross_returns.copy()
    turnover = run.turnover_history.reindex(returns.index).fillna(0.0)
    net_returns = returns - turnover * (float(cost_bps) / 10_000.0)
    weights = run.weights_history.reindex(returns.index).fillna(0.0)
    if start:
        net_returns = net_returns.loc[start:]
        turnover = turnover.loc[start:]
        weights = weights.loc[start:]
    if end:
        net_returns = net_returns.loc[:end]
        turnover = turnover.loc[:end]
        weights = weights.loc[:end]
    net_returns = net_returns.dropna()
    if net_returns.empty:
        raise RuntimeError(f"No returns remain for {run.strategy_name}")

    benchmark = benchmark_returns.reindex(net_returns.index).fillna(0.0)
    equity_curve = (1.0 + net_returns).cumprod()
    total_return = float(equity_curve.iloc[-1] - 1.0)
    years = max((net_returns.index[-1] - net_returns.index[0]).days / 365.25, 1 / 365.25)
    cagr = float(equity_curve.iloc[-1] ** (1.0 / years) - 1.0)
    drawdown = equity_curve / equity_curve.cummax() - 1.0
    max_drawdown = float(drawdown.min())
    std = float(net_returns.std(ddof=0))
    volatility = std * math.sqrt(252) if std else float("nan")
    sharpe = float(net_returns.mean() / std * math.sqrt(252)) if std else float("nan")
    beta = suite.compute_beta(net_returns, benchmark)
    information_ratio = suite.compute_information_ratio(net_returns, benchmark)
    turnover_per_year = float(turnover.reindex(net_returns.index).fillna(0.0).sum() / years)
    tqqq_weight = weights.get("TQQQ", pd.Series(0.0, index=weights.index))
    qqq_weight = weights.get("QQQ", pd.Series(0.0, index=weights.index))
    cash_weight = weights.get(CASH_SYMBOL, pd.Series(0.0, index=weights.index))
    boxx_weight = weights.get("BOXX", pd.Series(0.0, index=weights.index))
    return {
        "Start": str(net_returns.index[0].date()),
        "End": str(net_returns.index[-1].date()),
        "Total Return": total_return,
        "CAGR": cagr,
        "Max Drawdown": max_drawdown,
        "Volatility": volatility,
        "Sharpe": sharpe,
        "Beta vs QQQ": beta,
        "Information Ratio vs QQQ": information_ratio,
        "Turnover/Year": turnover_per_year,
        "2020 Return": suite.compute_period_total_return(net_returns, "2020-01-01", "2020-12-31"),
        "2022 Return": suite.compute_period_total_return(net_returns, "2022-01-01", "2022-12-31"),
        "2023 Return": suite.compute_period_total_return(net_returns, "2023-01-01", "2023-12-31"),
        "2023+ CAGR": suite.compute_period_cagr(net_returns, "2023-01-01", None),
        "Average QQQ Weight": float(qqq_weight.reindex(net_returns.index).fillna(0.0).mean()),
        "Average TQQQ Weight": float(tqqq_weight.reindex(net_returns.index).fillna(0.0).mean()),
        "Average BOXX Weight": float(boxx_weight.reindex(net_returns.index).fillna(0.0).mean()),
        "Average Cash Weight": float(cash_weight.reindex(net_returns.index).fillna(0.0).mean()),
        "TQQQ Days Share": float((tqqq_weight.reindex(net_returns.index).fillna(0.0) > 1e-12).mean()),
    }


def build_summary(
    runs: Iterable[StrategyRun],
    benchmark_returns: pd.Series,
    *,
    costs_bps: Iterable[float],
    period_start: str | None,
    period_end: str | None,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for run in runs:
        for cost_bps in costs_bps:
            metrics = summarize_run(
                run,
                benchmark_returns,
                cost_bps=float(cost_bps),
                start=period_start,
                end=period_end,
            )
            rows.append(
                {
                    "strategy": run.strategy_name,
                    "display_name": run.display_name,
                    "cost_bps_one_way": float(cost_bps),
                    **metrics,
                    **run.metadata,
                }
            )
    return pd.DataFrame(rows)


def frame_to_markdown_table(frame: pd.DataFrame) -> str:
    display = frame.copy()
    for column in display.columns:
        if pd.api.types.is_numeric_dtype(display[column]):
            def _format(value: object) -> str:
                if pd.isna(value):
                    return ""
                return f"{float(value):.6f}"

            display[column] = display[column].map(_format)
        else:
            display[column] = display[column].fillna("").astype(str)
    headers = [str(column) for column in display.columns]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in display.itertuples(index=False, name=None):
        lines.append("| " + " | ".join(str(value) for value in row) + " |")
    return "\n".join(lines)


def build_markdown(summary: pd.DataFrame, recommendation: dict[str, object]) -> str:
    focus = summary.loc[summary["cost_bps_one_way"] == 5.0].copy()
    focus = focus.sort_values("CAGR", ascending=False)
    compact_columns = [
        "strategy",
        "execution_mode",
        "CAGR",
        "Max Drawdown",
        "2020 Return",
        "2022 Return",
        "2023 Return",
        "Turnover/Year",
        "Average QQQ Weight",
        "Average TQQQ Weight",
        "known_limitation",
    ]
    lines = [
        "# Video QQQ/TQQQ Dual-Drive Reconstruction",
        "",
        "## Setup",
        "- Data: Yahoo Finance adjusted daily OHLCV via the existing research loader.",
        "- Main comparison window follows the video window as closely as trading days allow.",
        "- Cost focus: 5 bps one-way turnover cost.",
        "- The exact video code is not public, so variants are explicit approximations.",
        "",
        "## 5 bps Comparison",
        frame_to_markdown_table(focus[compact_columns]),
        "",
        "## Video Reported Reference",
        f"- Reported CAGR: {VIDEO_REPORTED['CAGR']:.2%}",
        f"- Reported MaxDD: {VIDEO_REPORTED['Max Drawdown']:.2%}",
        f"- Reported 2022 return: {VIDEO_REPORTED['2022 Return']:.2%}",
        "",
        "## Findings",
    ]
    lines.extend(f"- {item}" for item in recommendation["findings"])
    lines.extend(
        [
            "",
            "## Caveats",
            "- The video mentions six internal states, high-level top escape, and below-MA200 low-buy/high-sell behavior, but does not disclose exact conditions.",
            "- The same-close variant is intentionally non-tradable; it is included only as a bias diagnostic.",
            "- BOXX, SPYI, and QQQI histories are shorter than QQQ/TQQQ, matching the existing local research limitation.",
        ]
    )
    return "\n".join(lines) + "\n"


def choose_recommendation(summary: pd.DataFrame) -> dict[str, object]:
    focus = summary.loc[summary["cost_bps_one_way"] == 5.0].copy()
    video_like = focus.loc[focus["family"] == "video_qqq_tqqq_dual_drive"].copy()
    implementable = video_like.loc[video_like["execution_mode"] != "same_close_lookahead"].copy()
    lookahead = video_like.loc[video_like["execution_mode"] == "same_close_lookahead"].copy()
    findings: list[str] = []

    rule_based = implementable.loc[implementable["strategy"] != "buy_hold_45_45_10"].copy()
    best_impl = rule_based.sort_values("CAGR", ascending=False).iloc[0]
    findings.append(
        f"Best implementable reconstruction is `{best_impl['strategy']}` at "
        f"{best_impl['CAGR']:.2%} CAGR / {best_impl['Max Drawdown']:.2%} MaxDD, "
        f"well below the video's reported 49.40% CAGR."
    )

    buy_hold_45 = implementable.loc[implementable["strategy"] == "buy_hold_45_45_10"]
    if not buy_hold_45.empty:
        row = buy_hold_45.iloc[0]
        findings.append(
            f"The simple 45/45/10 daily-rebalanced reference is {row['CAGR']:.2%} CAGR "
            f"with {row['Max Drawdown']:.2%} MaxDD, so the video headline is not explained "
            "by static QQQ/TQQQ exposure alone."
        )

    if not lookahead.empty:
        lookahead_row = lookahead.iloc[0]
        findings.append(
            f"The intentionally biased same-close version reaches {lookahead_row['CAGR']:.2%} CAGR "
            f"with {lookahead_row['Max Drawdown']:.2%} MaxDD; if a backtest applies close-generated "
            "signals to the same close-to-close return, the headline can be inflated."
        )

    closest_to_video = focus.assign(
        cagr_gap=(focus["CAGR"] - VIDEO_REPORTED["CAGR"]).abs(),
        drawdown_gap=(focus["Max Drawdown"] - VIDEO_REPORTED["Max Drawdown"]).abs(),
    ).sort_values(["cagr_gap", "drawdown_gap"]).iloc[0]
    findings.append(
        f"Closest CAGR to the video in this local run is `{closest_to_video['strategy']}` at "
        f"{closest_to_video['CAGR']:.2%}; it still misses the reported CAGR by "
        f"{closest_to_video['cagr_gap']:.2%}."
    )

    tqqq_buy_hold = focus.loc[focus["strategy"] == "TQQQ_buy_hold"]
    if not tqqq_buy_hold.empty:
        row = tqqq_buy_hold.iloc[0]
        findings.append(
            f"Raw TQQQ buy-and-hold produces {row['CAGR']:.2%} CAGR but {row['Max Drawdown']:.2%} MaxDD, "
            "so the video's combination of near-TQQQ-level CAGR and much lower drawdown needs exact "
            "state-machine disclosure before trusting it."
        )

    runtime_reference = focus.loc[focus["strategy"] == "tqqq_growth_income_runtime_full_reference"]
    runtime_candidates = focus.loc[focus["family"] == "hybrid_growth_runtime_dual_drive_candidate"].copy()
    if not runtime_reference.empty and not runtime_candidates.empty:
        baseline = runtime_reference.iloc[0]
        best_runtime = runtime_candidates.sort_values("CAGR", ascending=False).iloc[0]
        findings.append(
            "Highest-CAGR video-inspired upgrade for the current full strategy is "
            f"`{best_runtime['strategy']}`: {best_runtime['CAGR']:.2%} CAGR / "
            f"{best_runtime['Max Drawdown']:.2%} MaxDD versus the original "
            f"{baseline['CAGR']:.2%} CAGR / {baseline['Max Drawdown']:.2%} MaxDD."
        )
        risk_balanced = runtime_candidates.loc[
            (runtime_candidates["Max Drawdown"] >= baseline["Max Drawdown"] - 0.005)
            & (runtime_candidates["2022 Return"] >= baseline["2022 Return"] - 0.010)
        ].copy()
        if not risk_balanced.empty:
            best_balanced = risk_balanced.sort_values("CAGR", ascending=False).iloc[0]
            findings.append(
                "Most conservative upgrade that keeps drawdown and 2022 close to the original is "
                f"`{best_balanced['strategy']}`: {best_balanced['CAGR']:.2%} CAGR / "
                f"{best_balanced['Max Drawdown']:.2%} MaxDD / 2022 {best_balanced['2022 Return']:.2%}."
            )

    return {"findings": findings}


def build_buy_hold_run(symbol: str, returns_matrix: pd.DataFrame) -> StrategyRun:
    returns = returns_matrix[symbol].copy().rename(f"{symbol}_buy_hold")
    weights = pd.DataFrame(0.0, index=returns.index, columns=(symbol,))
    weights[symbol] = 1.0
    turnover = pd.Series(0.0, index=returns.index, name="turnover")
    return StrategyRun(
        strategy_name=f"{symbol}_buy_hold",
        display_name=f"{symbol}_buy_hold",
        gross_returns=returns,
        weights_history=weights,
        turnover_history=turnover,
        metadata={
            "family": "reference",
            "execution_mode": "buy_hold",
            "description": f"Buy-and-hold {symbol}.",
            "known_limitation": "Reference only.",
        },
    )


def write_outputs(summary: pd.DataFrame, recommendation: dict[str, object], results_dir: Path) -> None:
    comparison_path = results_dir / "video_qqq_tqqq_dual_drive_comparison.csv"
    summary_path = results_dir / "video_qqq_tqqq_dual_drive_summary.md"
    recommendation_path = results_dir / "video_qqq_tqqq_dual_drive_recommendation.json"
    summary.to_csv(comparison_path, index=False)
    summary_path.write_text(build_markdown(summary, recommendation), encoding="utf-8")
    recommendation_path.write_text(json.dumps(recommendation, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {comparison_path}")
    print(f"wrote {summary_path}")
    print(f"wrote {recommendation_path}")


def main() -> None:
    args = parse_args()
    results_dir = Path(args.results_dir).expanduser().resolve()
    results_dir.mkdir(parents=True, exist_ok=True)

    qqq_ohlc, returns_matrix, indicators = load_market_data(start=args.download_start, end=args.end)
    runs: list[StrategyRun] = [
        run_video_backtest(config, returns_matrix, indicators)
        for config in VIDEO_CONFIGS
    ]
    runs.extend(convert_base_run(run) for run in build_current_strategy_references(qqq_ohlc, returns_matrix))
    runs.extend(
        run_runtime_dual_drive_variant(config, qqq_ohlc, returns_matrix)
        for config in RUNTIME_DUAL_DRIVE_CONFIGS
    )
    runs.extend([build_buy_hold_run("QQQ", returns_matrix), build_buy_hold_run("TQQQ", returns_matrix)])

    summary = build_summary(
        runs,
        returns_matrix["QQQ"],
        costs_bps=args.cost_bps,
        period_start=args.period_start,
        period_end=args.period_end,
    )
    recommendation = choose_recommendation(summary)
    write_outputs(summary, recommendation, results_dir)


if __name__ == "__main__":
    main()
