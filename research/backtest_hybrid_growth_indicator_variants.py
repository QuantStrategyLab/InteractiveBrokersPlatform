#!/usr/bin/env python3
"""Research-only TQQQ / hybrid_growth_income indicator overlay study.

Goal:
- keep the current hybrid growth attack logic as the baseline;
- test a few simple daily indicator gates on top of the existing QQQ MA200 + ATR logic;
- compare idle capital parked in CASH / BOXX / QQQ;
- answer whether extra indicators add value without obviously overfitting.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import backtest_stock_alpha_suite as suite  # noqa: E402
from us_equity_strategies.strategies.tqqq_growth_income import get_hybrid_allocation  # noqa: E402

DEFAULT_RESULTS_DIR = Path(__file__).resolve().parent / "results"
DEFAULT_START = "2018-01-01"
DEFAULT_COSTS_BPS = (0.0, 5.0, 10.0)
PERIODS = (
    ("Full Sample", None, None),
    ("2018-2021", "2018-01-01", "2021-12-31"),
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023+", "2023-01-01", None),
)
ATTACK_ONLY_STARTING_EQUITY = 50_000.0
RUNTIME_FULL_STARTING_EQUITY = 200_000.0
CASH_SYMBOL = "CASH"
SAFE_HAVEN = "BOXX"


@dataclass(frozen=True)
class OverlayConfig:
    name: str
    description: str
    entry_confirm_days: int = 2
    exit_confirm_days: int = 2


@dataclass(frozen=True)
class BacktestConfig:
    overlay: OverlayConfig
    idle_asset: str
    income_mode: str  # attack_only | runtime_full_reference


@dataclass
class StrategyRun:
    strategy_name: str
    display_name: str
    gross_returns: pd.Series
    weights_history: pd.DataFrame
    turnover_history: pd.Series
    metadata: dict[str, object]
    raw_gate: pd.Series
    active_gate: pd.Series


OVERLAY_CONFIGS: tuple[OverlayConfig, ...] = (
    OverlayConfig(
        name="baseline",
        description="Current MA200 + ATR baseline with no extra daily gate.",
    ),
    OverlayConfig(
        name="ma20_ma60_stack",
        description="Require close > MA20 and MA20 > MA60 before allowing the existing TQQQ signal.",
    ),
    OverlayConfig(
        name="ma_stack_rsi_macd",
        description="MA20/MA60 stack plus RSI14 > 55 and MACD line above signal.",
    ),
    OverlayConfig(
        name="consensus_5",
        description="MA stack + RSI + MACD + MFI > 50 + CCI > 0.",
    ),
    OverlayConfig(
        name="consensus_7_weekly_kdj",
        description="Consensus_5 plus BIAS20 > 0 and weekly K>D with J > 50.",
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR))
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=None)
    parser.add_argument(
        "--cost-bps",
        nargs="*",
        type=float,
        default=list(DEFAULT_COSTS_BPS),
        help="One-way turnover cost assumptions in bps.",
    )
    return parser.parse_args()


def compute_rsi(close: pd.Series, window: int = 14) -> pd.Series:
    delta = close.diff()
    gains = delta.clip(lower=0.0)
    losses = (-delta).clip(lower=0.0)
    avg_gain = gains.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    avg_loss = losses.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)


def compute_macd(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def compute_mfi(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, window: int = 14) -> pd.Series:
    typical_price = (high + low + close) / 3.0
    money_flow = typical_price * volume.fillna(0.0)
    direction = typical_price.diff()
    positive_flow = money_flow.where(direction > 0.0, 0.0)
    negative_flow = money_flow.where(direction < 0.0, 0.0)
    pos_sum = positive_flow.rolling(window).sum()
    neg_sum = negative_flow.abs().rolling(window).sum()
    ratio = pos_sum / neg_sum.replace(0.0, np.nan)
    mfi = 100.0 - (100.0 / (1.0 + ratio))
    return mfi.fillna(50.0)


def compute_cci(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 20) -> pd.Series:
    typical_price = (high + low + close) / 3.0
    sma = typical_price.rolling(window).mean()
    mad = typical_price.rolling(window).apply(lambda values: np.mean(np.abs(values - values.mean())), raw=False)
    cci = (typical_price - sma) / (0.015 * mad.replace(0.0, np.nan))
    return cci.fillna(0.0)


def compute_weekly_kdj(ohlc: pd.DataFrame, window: int = 9) -> pd.DataFrame:
    weekly = ohlc.resample("W-FRI").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    lowest = weekly["low"].rolling(window).min()
    highest = weekly["high"].rolling(window).max()
    rsv = ((weekly["close"] - lowest) / (highest - lowest).replace(0.0, np.nan) * 100.0).fillna(50.0)
    k_values: list[float] = []
    d_values: list[float] = []
    k_prev = 50.0
    d_prev = 50.0
    for value in rsv:
        k_prev = (2.0 / 3.0) * k_prev + (1.0 / 3.0) * float(value)
        d_prev = (2.0 / 3.0) * d_prev + (1.0 / 3.0) * k_prev
        k_values.append(k_prev)
        d_values.append(d_prev)
    weekly["weekly_k"] = k_values
    weekly["weekly_d"] = d_values
    weekly["weekly_j"] = 3.0 * weekly["weekly_k"] - 2.0 * weekly["weekly_d"]
    daily = weekly[["weekly_k", "weekly_d", "weekly_j"]].reindex(ohlc.index, method="ffill")
    return daily.fillna(50.0)


def build_indicator_frame(qqq_ohlc: pd.DataFrame, qqq_volume: pd.Series) -> pd.DataFrame:
    close = qqq_ohlc["close"].copy()
    high = qqq_ohlc["high"].copy()
    low = qqq_ohlc["low"].copy()
    indicators = pd.DataFrame(index=close.index)
    indicators["close"] = close
    indicators["ma20"] = close.rolling(20).mean()
    indicators["ma60"] = close.rolling(60).mean()
    indicators["ma200"] = close.rolling(200).mean()
    indicators["rsi14"] = compute_rsi(close, 14)
    macd_line, macd_signal, macd_hist = compute_macd(close)
    indicators["macd_line"] = macd_line
    indicators["macd_signal"] = macd_signal
    indicators["macd_hist"] = macd_hist
    indicators["mfi14"] = compute_mfi(high, low, close, qqq_volume, 14)
    indicators["cci20"] = compute_cci(high, low, close, 20)
    indicators["bias20"] = (close / indicators["ma20"] - 1.0).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    indicators = indicators.join(compute_weekly_kdj(qqq_ohlc))
    return indicators


def build_raw_gate(indicators: pd.DataFrame, overlay_name: str) -> pd.Series:
    ma_stack = (indicators["close"] > indicators["ma20"]) & (indicators["ma20"] > indicators["ma60"])
    rsi_macd = ma_stack & (indicators["rsi14"] > 55.0) & (indicators["macd_line"] > indicators["macd_signal"])
    consensus_5 = rsi_macd & (indicators["mfi14"] > 50.0) & (indicators["cci20"] > 0.0)
    consensus_7 = (
        consensus_5
        & (indicators["bias20"] > 0.0)
        & (indicators["weekly_k"] > indicators["weekly_d"])
        & (indicators["weekly_j"] > 50.0)
    )
    mapping = {
        "baseline": pd.Series(True, index=indicators.index),
        "ma20_ma60_stack": ma_stack,
        "ma_stack_rsi_macd": rsi_macd,
        "consensus_5": consensus_5,
        "consensus_7_weekly_kdj": consensus_7,
    }
    return mapping[overlay_name].fillna(False)


def apply_confirmation_gate(raw_gate: pd.Series, *, entry_confirm_days: int, exit_confirm_days: int) -> pd.Series:
    active = False
    true_streak = 0
    false_streak = 0
    values: list[bool] = []
    for value in raw_gate.fillna(False).astype(bool):
        if value:
            true_streak += 1
            false_streak = 0
            if not active and true_streak >= entry_confirm_days:
                active = True
        else:
            false_streak += 1
            true_streak = 0
            if active and false_streak >= exit_confirm_days:
                active = False
        values.append(active)
    return pd.Series(values, index=raw_gate.index, dtype=bool)


def select_symbols(idle_asset: str) -> list[str]:
    symbols = ["QQQ", "TQQQ", SAFE_HAVEN]
    idle = str(idle_asset).strip().upper()
    if idle not in symbols and idle != CASH_SYMBOL:
        symbols.append(idle)
    return symbols


def build_target_weights(*, current_equity: float, target_tqqq_value: float, idle_value: float, reserved_value: float, idle_asset: str) -> dict[str, float]:
    idle_symbol = str(idle_asset).strip().upper()
    target_values: dict[str, float] = {"TQQQ": max(0.0, target_tqqq_value)}
    if idle_symbol == CASH_SYMBOL:
        target_values[CASH_SYMBOL] = max(0.0, idle_value + reserved_value)
    else:
        target_values[idle_symbol] = max(0.0, idle_value)
        target_values[CASH_SYMBOL] = max(0.0, reserved_value)
    target_weights = {
        symbol: value / current_equity
        for symbol, value in target_values.items()
        if current_equity > 0.0 and value > 1e-12
    }
    if not target_weights:
        target_weights = {CASH_SYMBOL: 1.0}
    return target_weights


def run_attack_only_variant_backtest(
    qqq_ohlc: pd.DataFrame,
    asset_returns: pd.DataFrame,
    indicators: pd.DataFrame,
    *,
    config: BacktestConfig,
    starting_equity: float = ATTACK_ONLY_STARTING_EQUITY,
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
    idle_asset = str(config.idle_asset).strip().upper()
    raw_gate = build_raw_gate(indicators, config.overlay.name)
    active_gate = apply_confirmation_gate(
        raw_gate,
        entry_confirm_days=config.overlay.entry_confirm_days,
        exit_confirm_days=config.overlay.exit_confirm_days,
    )

    strategy_symbols = ["TQQQ", SAFE_HAVEN, "QQQ", CASH_SYMBOL]
    strategy_symbols = [symbol for symbol in strategy_symbols if symbol in set(select_symbols(idle_asset)) | {CASH_SYMBOL, "TQQQ"}]
    index = asset_returns.index.intersection(qqq_ohlc.index)
    weights_history = pd.DataFrame(0.0, index=index, columns=strategy_symbols)
    portfolio_returns = pd.Series(0.0, index=index, name=f"{config.overlay.name}__{idle_asset.lower()}")
    turnover_history = pd.Series(0.0, index=index, name="turnover")

    initial_idle_symbol = idle_asset if idle_asset != CASH_SYMBOL else CASH_SYMBOL
    current_weights: dict[str, float] = {initial_idle_symbol: 1.0}
    current_equity = float(starting_equity)

    for idx in range(len(index) - 1):
        date = index[idx]
        next_date = index[idx + 1]
        row = indicators.loc[date]
        qqq_p = float(row["close"])
        ma200 = float(row["ma200"]) if pd.notna(row["ma200"]) else float("nan")

        history = qqq_ohlc.loc[:date]
        true_range = pd.concat(
            [
                history["high"] - history["low"],
                (history["high"] - history["close"].shift(1)).abs(),
                (history["low"] - history["close"].shift(1)).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr_pct = float(true_range.rolling(14).mean().iloc[-1] / qqq_p) if len(history) >= 14 else 0.0
        exit_line = ma200 * max(exit_line_floor, min(exit_line_cap, 1.0 - (atr_pct * atr_exit_scale))) if pd.notna(ma200) else float("nan")
        entry_line = ma200 * max(entry_line_floor, min(entry_line_cap, 1.0 + (atr_pct * atr_entry_scale))) if pd.notna(ma200) else float("nan")

        strategy_equity = current_equity
        reserved = strategy_equity * cash_reserve_ratio
        agg_ratio, _ = get_hybrid_allocation(
            strategy_equity,
            qqq_p,
            exit_line if pd.notna(exit_line) else qqq_p,
            alloc_tier1_breakpoints=alloc_tier1_breakpoints,
            alloc_tier1_values=alloc_tier1_values,
            alloc_tier2_breakpoints=alloc_tier2_breakpoints,
            alloc_tier2_values=alloc_tier2_values,
            risk_leverage_factor=risk_leverage_factor,
            risk_agg_cap=risk_agg_cap,
            risk_numerator=risk_numerator,
        )

        current_tqqq_weight = current_weights.get("TQQQ", 0.0)
        target_tqqq_ratio = 0.0
        if current_tqqq_weight > 1e-12:
            if qqq_p < exit_line:
                target_tqqq_ratio = 0.0
            elif qqq_p < ma200:
                target_tqqq_ratio = agg_ratio * 0.33
            else:
                target_tqqq_ratio = agg_ratio
        elif qqq_p > entry_line:
            target_tqqq_ratio = agg_ratio

        if not bool(active_gate.loc[date]):
            target_tqqq_ratio = 0.0

        target_tqqq_value = strategy_equity * target_tqqq_ratio
        idle_value = max(0.0, (strategy_equity - reserved) - target_tqqq_value)
        target_weights = build_target_weights(
            current_equity=current_equity,
            target_tqqq_value=target_tqqq_value,
            idle_value=idle_value,
            reserved_value=reserved,
            idle_asset=idle_asset,
        )

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

        next_returns = asset_returns.loc[next_date]
        portfolio_returns.at[next_date] = sum(
            weight * float(next_returns.get(symbol, 0.0))
            for symbol, weight in current_weights.items()
            if symbol != CASH_SYMBOL
        )
        current_equity *= 1.0 + float(portfolio_returns.at[next_date])

    for symbol, weight in current_weights.items():
        if symbol in weights_history.columns:
            weights_history.at[index[-1], symbol] = weight

    metadata = {
        "family": "hybrid_growth_tqqq_attack_only",
        "overlay": config.overlay.name,
        "overlay_description": config.overlay.description,
        "idle_asset": idle_asset,
        "entry_confirm_days": config.overlay.entry_confirm_days,
        "exit_confirm_days": config.overlay.exit_confirm_days,
        "income_mode": config.income_mode,
        "safe_haven_symbols": tuple(symbol for symbol in (SAFE_HAVEN, CASH_SYMBOL, idle_asset) if symbol != "TQQQ"),
    }
    return StrategyRun(
        strategy_name=f"hybrid_tqqq::{config.overlay.name}::{idle_asset.lower()}",
        display_name=f"hybrid_tqqq::{config.overlay.name}::{idle_asset.lower()}",
        gross_returns=portfolio_returns,
        weights_history=weights_history,
        turnover_history=turnover_history,
        metadata=metadata,
        raw_gate=raw_gate.reindex(index).fillna(False),
        active_gate=active_gate.reindex(index).fillna(False),
    )


def summarize_strategy_period(
    strategy_run: StrategyRun,
    benchmark_returns: pd.Series,
    *,
    cost_bps: float,
    start: str | None,
    end: str | None,
) -> dict[str, object]:
    returns = strategy_run.gross_returns.copy()
    turnover = strategy_run.turnover_history.reindex(returns.index).fillna(0.0)
    net_returns = returns - turnover * (float(cost_bps) / 10_000.0)
    weights = strategy_run.weights_history.copy()
    raw_gate = strategy_run.raw_gate.copy()
    active_gate = strategy_run.active_gate.copy()

    if start:
        net_returns = net_returns.loc[start:]
        weights = weights.loc[start:]
        turnover = turnover.loc[start:]
        raw_gate = raw_gate.loc[start:]
        active_gate = active_gate.loc[start:]
    if end:
        net_returns = net_returns.loc[:end]
        weights = weights.loc[:end]
        turnover = turnover.loc[:end]
        raw_gate = raw_gate.loc[:end]
        active_gate = active_gate.loc[:end]

    net_returns = net_returns.dropna()
    benchmark = benchmark_returns.reindex(net_returns.index).fillna(0.0)
    weights = weights.reindex(net_returns.index).fillna(0.0)
    raw_gate = raw_gate.reindex(net_returns.index).fillna(False)
    active_gate = active_gate.reindex(net_returns.index).fillna(False)
    if net_returns.empty:
        raise RuntimeError("No returns remain inside selected period")

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
    up_capture, down_capture = suite.compute_capture_ratios(net_returns, benchmark)

    aligned = pd.concat([net_returns.rename("strategy"), benchmark.rename("benchmark")], axis=1).dropna()
    if aligned.empty or math.isnan(beta):
        alpha_ann = float("nan")
    else:
        alpha_daily = float((aligned["strategy"] - beta * aligned["benchmark"]).mean())
        alpha_ann = alpha_daily * 252.0

    avg_tqqq_weight = float(weights.get("TQQQ", pd.Series(0.0, index=weights.index)).mean())
    idle_asset = str(strategy_run.metadata["idle_asset"])
    avg_idle_weight = float(weights.get(idle_asset, pd.Series(0.0, index=weights.index)).mean())
    avg_cash_weight = float(weights.get(CASH_SYMBOL, pd.Series(0.0, index=weights.index)).mean())
    tqqq_days_share = float((weights.get("TQQQ", pd.Series(0.0, index=weights.index)) > 1e-12).mean())
    gate_active_share = float(active_gate.astype(float).mean())
    raw_gate_true_share = float(raw_gate.astype(float).mean())
    gate_flips = active_gate.astype(int).diff().abs().fillna(0.0)
    gate_flips_per_year = float(gate_flips.sum() / years)
    turnover_per_year = float(turnover.sum() / years)
    total_2022 = suite.compute_period_total_return(net_returns, "2022-01-01", "2022-12-31")
    cagr_2023_plus = suite.compute_period_cagr(net_returns, "2023-01-01", None)

    return {
        "Start": str(net_returns.index[0].date()),
        "End": str(net_returns.index[-1].date()),
        "Total Return": total_return,
        "CAGR": cagr,
        "Max Drawdown": max_drawdown,
        "Volatility": volatility,
        "Sharpe": sharpe,
        "Beta vs QQQ": beta,
        "Alpha Ann vs QQQ": alpha_ann,
        "Information Ratio vs QQQ": information_ratio,
        "Up Capture vs QQQ": up_capture,
        "Down Capture vs QQQ": down_capture,
        "Turnover/Year": turnover_per_year,
        "2022 Return": total_2022,
        "2023+ CAGR": cagr_2023_plus,
        "Average TQQQ Weight": avg_tqqq_weight,
        "Average Idle Asset Weight": avg_idle_weight,
        "Average Cash Weight": avg_cash_weight,
        "TQQQ Days Share": tqqq_days_share,
        "Raw Gate True Share": raw_gate_true_share,
        "Gate Active Share": gate_active_share,
        "Gate Flips/Year": gate_flips_per_year,
    }


def build_summary_rows(strategy_runs: list[StrategyRun], benchmark_returns: pd.Series, costs_bps: Iterable[float]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for run in strategy_runs:
        for cost_bps in costs_bps:
            for period_name, start, end in PERIODS:
                metrics = summarize_strategy_period(run, benchmark_returns, cost_bps=float(cost_bps), start=start, end=end)
                rows.append(
                    {
                        "strategy": run.strategy_name,
                        "display_name": run.display_name,
                        "cost_bps_one_way": float(cost_bps),
                        "period": period_name,
                        **metrics,
                        **run.metadata,
                    }
                )
    return pd.DataFrame(rows)


def build_runtime_full_reference(qqq_ohlc: pd.DataFrame, asset_returns: pd.DataFrame) -> StrategyRun:
    full_returns, full_weights, full_turnover = suite.run_hybrid_growth_income_backtest(
        qqq_ohlc,
        asset_returns,
        starting_equity=RUNTIME_FULL_STARTING_EQUITY,
        income_threshold_usd=100_000.0,
        qqqi_income_ratio=0.50,
        cash_reserve_ratio=0.05,
        rebalance_threshold_ratio=0.01,
        alloc_tier1_breakpoints=(0, 15_000, 30_000, 70_000),
        alloc_tier1_values=(1.0, 0.95, 0.85, 0.70),
        alloc_tier2_breakpoints=(70_000, 140_000),
        alloc_tier2_values=(0.70, 0.50),
        risk_leverage_factor=3.0,
        risk_agg_cap=0.50,
        risk_numerator=0.30,
        atr_exit_scale=2.0,
        atr_entry_scale=2.5,
        exit_line_floor=0.92,
        exit_line_cap=0.98,
        entry_line_floor=1.02,
        entry_line_cap=1.08,
    )
    index = full_returns.index
    return StrategyRun(
        strategy_name="hybrid_growth_income_runtime_full_reference",
        display_name="hybrid_growth_income_runtime_full_reference",
        gross_returns=full_returns,
        weights_history=full_weights.reindex(index).fillna(0.0),
        turnover_history=full_turnover.reindex(index).fillna(0.0),
        metadata={
            "family": "hybrid_growth_runtime_full_reference",
            "overlay": "baseline",
            "overlay_description": "Current runtime full strategy with income layer and BOXX idle asset.",
            "idle_asset": SAFE_HAVEN,
            "entry_confirm_days": 0,
            "exit_confirm_days": 0,
            "income_mode": "runtime_full_reference",
        },
        raw_gate=pd.Series(True, index=index),
        active_gate=pd.Series(True, index=index),
    )


def choose_recommendation(summary: pd.DataFrame) -> dict[str, object]:
    oos = summary.loc[(summary["period"] == "2023+") & (summary["cost_bps_one_way"] == 5.0)].copy()
    baseline_boxx = oos.loc[
        (oos["overlay"] == "baseline")
        & (oos["idle_asset"] == "BOXX")
        & (oos["income_mode"] == "attack_only")
    ].iloc[0]

    attack_only = oos.loc[oos["income_mode"] == "attack_only"].copy()
    attack_only["score"] = (
        attack_only["Information Ratio vs QQQ"].fillna(-999.0) * 4.0
        + attack_only["Alpha Ann vs QQQ"].fillna(-999.0) * 2.0
        + attack_only["CAGR"].fillna(-999.0)
        - attack_only["Max Drawdown"].abs().fillna(999.0)
        - attack_only["Turnover/Year"].fillna(999.0) * 0.03
    )
    best = attack_only.sort_values("score", ascending=False).iloc[0]

    idle_slice = attack_only.loc[attack_only["overlay"] == "baseline", ["idle_asset", "CAGR", "Max Drawdown", "Information Ratio vs QQQ", "Alpha Ann vs QQQ", "2022 Return", "Turnover/Year"]]
    recommendation = {
        "baseline_attack_only_boxx": {
            "oos_cagr": float(baseline_boxx["CAGR"]),
            "oos_max_drawdown": float(baseline_boxx["Max Drawdown"]),
            "oos_ir_vs_qqq": float(baseline_boxx["Information Ratio vs QQQ"]),
            "oos_alpha_ann_vs_qqq": float(baseline_boxx["Alpha Ann vs QQQ"]),
            "return_2022": float(baseline_boxx["2022 Return"]),
        },
        "best_attack_only_candidate": {
            "strategy": str(best["strategy"]),
            "overlay": str(best["overlay"]),
            "idle_asset": str(best["idle_asset"]),
            "oos_cagr": float(best["CAGR"]),
            "oos_max_drawdown": float(best["Max Drawdown"]),
            "oos_ir_vs_qqq": float(best["Information Ratio vs QQQ"]),
            "oos_alpha_ann_vs_qqq": float(best["Alpha Ann vs QQQ"]),
            "return_2022": float(best["2022 Return"]),
            "turnover_per_year": float(best["Turnover/Year"]),
        },
        "idle_asset_baseline_comparison": idle_slice.to_dict(orient="records"),
    }

    if best["overlay"] == "baseline" and best["idle_asset"] == "BOXX":
        verdict = "No tested daily overlay clearly beats the current attack-only baseline; keep the logic simple."
    else:
        cagr_delta = float(best["CAGR"] - baseline_boxx["CAGR"])
        mdd_delta = float(best["Max Drawdown"] - baseline_boxx["Max Drawdown"])
        if cagr_delta > -0.01 and mdd_delta > 0.02:
            verdict = "A small daily overlay looks promising enough for another focused round."
        else:
            verdict = "The tested overlays mainly trade more often; they do not justify upgrading the default logic yet."
    recommendation["verdict"] = verdict
    return recommendation


def frame_to_markdown_table(frame: pd.DataFrame) -> str:
    display = frame.copy()
    for column in display.columns:
        if pd.api.types.is_bool_dtype(display[column]):
            display[column] = display[column].map(lambda value: "true" if bool(value) else "false")
        elif pd.api.types.is_numeric_dtype(display[column]):
            def _fmt(value: object) -> str:
                if pd.isna(value):
                    return ""
                if isinstance(value, (float, np.floating)):
                    return f"{float(value):.6f}"
                return str(value)

            display[column] = display[column].map(_fmt)
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
    focus = summary.loc[(summary["period"] == "2023+") & (summary["cost_bps_one_way"] == 5.0)].copy()
    attack_only = focus.loc[focus["income_mode"] == "attack_only"].copy()
    attack_only = attack_only.sort_values(["overlay", "idle_asset"])
    runtime_reference = focus.loc[focus["income_mode"] == "runtime_full_reference"].copy()

    lines = [
        "# TQQQ / hybrid_growth_income indicator overlay review",
        "",
        "## Setup",
        "- Baseline signal: current QQQ MA200 + ATR staged TQQQ logic.",
        "- Main research set: attack-only normalization (`income_threshold_usd = 1e9`) so the idle-asset choice is visible.",
        "- Runtime reference: current full `hybrid_growth_income` with income layer on.",
        "- Idle asset candidates: `CASH`, `BOXX`, `QQQ`.",
        "- Extra indicator gates are nested from simple to complex to avoid blind indicator stuffing.",
        "",
        "## OOS 2023+ (5 bps)",
        frame_to_markdown_table(
            attack_only[[
                "overlay",
                "idle_asset",
                "CAGR",
                "Max Drawdown",
                "Information Ratio vs QQQ",
                "Alpha Ann vs QQQ",
                "2022 Return",
                "Turnover/Year",
                "Average TQQQ Weight",
                "Average Idle Asset Weight",
                "Gate Active Share",
            ]]
        ),
        "",
        "## Runtime full reference (2023+, 5 bps)",
        frame_to_markdown_table(
            runtime_reference[[
                "strategy",
                "idle_asset",
                "CAGR",
                "Max Drawdown",
                "Information Ratio vs QQQ",
                "Alpha Ann vs QQQ",
                "2022 Return",
                "Turnover/Year",
            ]]
        ),
        "",
        "## Recommendation",
        f"- {recommendation['verdict']}",
    ]
    best = recommendation["best_attack_only_candidate"]
    lines.extend(
        [
            f"- Best attack-only candidate in this run: `{best['overlay']}` + `{best['idle_asset']}`",
            f"- OOS CAGR: {best['oos_cagr']:.2%}",
            f"- OOS MaxDD: {best['oos_max_drawdown']:.2%}",
            f"- OOS IR vs QQQ: {best['oos_ir_vs_qqq']:.3f}",
            f"- 2022: {best['return_2022']:.2%}",
            "",
            "## Caveats",
            "- BOXX pre-launch history is naturally short; before listed data exists it behaves like 0% carry in this backtest, so early BOXX results are closer to cash than to realized BOXX carry.",
            "- These tests add daily gates on top of the existing daily TQQQ logic; they are not monthly overlays and should not be read as direct runtime recommendations.",
            "- Weekly KDJ is mapped back to daily with forward-fill from Friday weekly bars; useful for a sanity check, but not a reason to trust the heaviest gate by default.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    args = parse_args()
    results_dir = Path(args.results_dir).expanduser().resolve()
    results_dir.mkdir(parents=True, exist_ok=True)

    end = args.end
    etf_frames = suite.download_etf_ohlcv(("QQQ", "TQQQ", "BOXX", "SPYI", "QQQI"), start=args.start, end=end)
    qqq_ohlc = pd.DataFrame(
        {
            "open": etf_frames["open"]["QQQ"],
            "high": etf_frames["high"]["QQQ"],
            "low": etf_frames["low"]["QQQ"],
            "close": etf_frames["close"]["QQQ"],
        }
    ).dropna()
    master_index = qqq_ohlc.index
    rows = suite.build_extra_etf_price_history(etf_frames, symbols=("QQQ", "TQQQ", "BOXX", "SPYI", "QQQI"))
    close_matrix, returns_matrix = suite.build_asset_return_matrix(rows, master_index=master_index, required_symbols=("QQQ", "TQQQ", "BOXX", "SPYI", "QQQI"))
    returns_matrix[CASH_SYMBOL] = 0.0
    indicators = build_indicator_frame(qqq_ohlc, etf_frames["volume"]["QQQ"].reindex(master_index).fillna(0.0))

    strategy_runs: list[StrategyRun] = [build_runtime_full_reference(qqq_ohlc, returns_matrix)]
    for overlay in OVERLAY_CONFIGS:
        for idle_asset in (CASH_SYMBOL, SAFE_HAVEN, "QQQ"):
            strategy_runs.append(
                run_attack_only_variant_backtest(
                    qqq_ohlc,
                    returns_matrix,
                    indicators,
                    config=BacktestConfig(
                        overlay=overlay,
                        idle_asset=idle_asset,
                        income_mode="attack_only",
                    ),
                )
            )

    benchmark_returns = returns_matrix["QQQ"].copy()
    summary = build_summary_rows(strategy_runs, benchmark_returns, args.cost_bps)
    recommendation = choose_recommendation(summary)

    comparison_path = results_dir / "tqqq_hybrid_indicator_variants_comparison.csv"
    summary_path = results_dir / "tqqq_hybrid_indicator_variants_summary.md"
    recommendation_path = results_dir / "tqqq_hybrid_indicator_variants_recommendation.json"
    summary.to_csv(comparison_path, index=False)
    summary_path.write_text(build_markdown(summary, recommendation), encoding="utf-8")
    recommendation_path.write_text(json.dumps(recommendation, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"wrote {comparison_path}")
    print(f"wrote {summary_path}")
    print(f"wrote {recommendation_path}")


if __name__ == "__main__":
    main()
