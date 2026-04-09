#!/usr/bin/env python3
"""Focused TQQQ position-scaling study on top of the existing TQQQ growth sleeve."""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import backtest_tqqq_growth_indicator_variants as base  # noqa: E402
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
CASH_SYMBOL = base.CASH_SYMBOL
SAFE_HAVEN = base.SAFE_HAVEN


@dataclass(frozen=True)
class ScalingConfig:
    name: str
    description: str


@dataclass
class StrategyRun:
    strategy_name: str
    display_name: str
    gross_returns: pd.Series
    weights_history: pd.DataFrame
    turnover_history: pd.Series
    scale_history: pd.Series
    metadata: dict[str, object]


SCALING_CONFIGS = (
    ScalingConfig(
        name="baseline",
        description="No extra scaling; use the existing MA200 + ATR staged TQQQ sizing as-is.",
    ),
    ScalingConfig(
        name="trend_score_4_boost",
        description="Use MA20, MA20>MA60, RSI14>55, MACD bullish as a 4-factor score to scale TQQQ while already in-risk.",
    ),
    ScalingConfig(
        name="consensus_score_6",
        description="Use the trend score plus MFI>50 and CCI>0; weaker consensus trims TQQQ more aggressively.",
    ),
    ScalingConfig(
        name="overheat_trim",
        description="Keep baseline size unless the setup looks overbought or weakening, then trim in steps.",
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
    )
    return parser.parse_args()


def compute_ulcer_index(net_returns: pd.Series) -> float:
    equity_curve = (1.0 + net_returns).cumprod()
    drawdown_pct = (equity_curve / equity_curve.cummax() - 1.0) * 100.0
    return float(np.sqrt(np.mean(np.square(drawdown_pct))))


def scaling_multiplier(config_name: str, row: pd.Series) -> float:
    close = float(row["close"])
    ma20 = float(row["ma20"]) if pd.notna(row["ma20"]) else float("nan")
    ma60 = float(row["ma60"]) if pd.notna(row["ma60"]) else float("nan")
    rsi14 = float(row["rsi14"])
    macd_line = float(row["macd_line"])
    macd_signal = float(row["macd_signal"])
    mfi14 = float(row["mfi14"])
    cci20 = float(row["cci20"])
    bias20 = float(row["bias20"])
    weekly_k = float(row["weekly_k"])
    weekly_d = float(row["weekly_d"])
    weekly_j = float(row["weekly_j"])

    if config_name == "baseline":
        return 1.0

    if config_name == "trend_score_4_boost":
        score = sum(
            [
                close > ma20 if pd.notna(ma20) else False,
                (ma20 > ma60) if pd.notna(ma20) and pd.notna(ma60) else False,
                rsi14 > 55.0,
                macd_line > macd_signal,
            ]
        )
        return {4: 1.15, 3: 1.00, 2: 0.75}.get(score, 0.50)

    if config_name == "consensus_score_6":
        score = sum(
            [
                close > ma20 if pd.notna(ma20) else False,
                (ma20 > ma60) if pd.notna(ma20) and pd.notna(ma60) else False,
                rsi14 > 55.0,
                macd_line > macd_signal,
                mfi14 > 50.0,
                cci20 > 0.0,
            ]
        )
        if score >= 6:
            return 1.05
        if score >= 4:
            return 0.85
        if score >= 2:
            return 0.60
        return 0.35

    if config_name == "overheat_trim":
        close_above_ma20 = bool(pd.notna(ma20) and close > ma20)
        close_below_ma20 = bool(pd.notna(ma20) and close < ma20)
        if (rsi14 > 75.0 or bias20 > 0.08) and close_above_ma20:
            return 0.75
        if macd_line < macd_signal and rsi14 < 50.0 and close_below_ma20:
            return 0.35
        if (weekly_k < weekly_d and weekly_j < 50.0) or (macd_line < macd_signal and rsi14 < 55.0):
            return 0.60
        return 1.0

    raise KeyError(f"Unknown scaling config: {config_name}")


def run_scaling_backtest(
    qqq_ohlc: pd.DataFrame,
    asset_returns: pd.DataFrame,
    indicators: pd.DataFrame,
    *,
    scaling: ScalingConfig,
    idle_asset: str,
    starting_equity: float = base.ATTACK_ONLY_STARTING_EQUITY,
    cash_reserve_ratio: float = 0.05,
    rebalance_threshold_ratio: float = 0.01,
    alloc_tier1_breakpoints=(0, 15_000, 30_000, 70_000),
    alloc_tier1_values=(1.0, 0.95, 0.85, 0.70),
    alloc_tier2_breakpoints=(70_000, 140_000),
    alloc_tier2_values=(0.70, 0.50),
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
    idle_asset = idle_asset.upper()
    index = asset_returns.index.intersection(qqq_ohlc.index)
    strategy_symbols = ["TQQQ", SAFE_HAVEN, "QQQ", CASH_SYMBOL]
    weights_history = pd.DataFrame(0.0, index=index, columns=strategy_symbols)
    portfolio_returns = pd.Series(0.0, index=index, name=f"{scaling.name}__{idle_asset.lower()}")
    turnover_history = pd.Series(0.0, index=index, name="turnover")
    scale_history = pd.Series(1.0, index=index, name="scale")

    current_equity = float(starting_equity)
    current_weights: dict[str, float] = {idle_asset if idle_asset != CASH_SYMBOL else CASH_SYMBOL: 1.0}

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

        reserved = current_equity * cash_reserve_ratio
        agg_ratio, _ = get_hybrid_allocation(
            current_equity,
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

        scale = 0.0 if target_tqqq_ratio <= 1e-12 else scaling_multiplier(scaling.name, row)
        scale_history.at[date] = scale
        target_tqqq_ratio *= scale

        target_tqqq_value = current_equity * target_tqqq_ratio
        idle_value = max(0.0, (current_equity - reserved) - target_tqqq_value)
        target_weights = base.build_target_weights(
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
        "family": "hybrid_growth_tqqq_position_scaling",
        "scaling": scaling.name,
        "scaling_description": scaling.description,
        "idle_asset": idle_asset,
    }
    return StrategyRun(
        strategy_name=f"hybrid_tqqq_scaling::{scaling.name}::{idle_asset.lower()}",
        display_name=f"hybrid_tqqq_scaling::{scaling.name}::{idle_asset.lower()}",
        gross_returns=portfolio_returns,
        weights_history=weights_history,
        turnover_history=turnover_history,
        scale_history=scale_history,
        metadata=metadata,
    )


def summarize_period(run: StrategyRun, benchmark_returns: pd.Series, *, cost_bps: float, start: str | None, end: str | None) -> dict[str, object]:
    returns = run.gross_returns.copy()
    turnover = run.turnover_history.reindex(returns.index).fillna(0.0)
    net_returns = returns - turnover * (float(cost_bps) / 10_000.0)
    weights = run.weights_history.copy()
    scale = run.scale_history.copy()

    if start:
        net_returns = net_returns.loc[start:]
        weights = weights.loc[start:]
        turnover = turnover.loc[start:]
        scale = scale.loc[start:]
    if end:
        net_returns = net_returns.loc[:end]
        weights = weights.loc[:end]
        turnover = turnover.loc[:end]
        scale = scale.loc[:end]

    net_returns = net_returns.dropna()
    benchmark = benchmark_returns.reindex(net_returns.index).fillna(0.0)
    weights = weights.reindex(net_returns.index).fillna(0.0)
    scale = scale.reindex(net_returns.index).fillna(0.0)
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

    active_scale = scale[weights.get("TQQQ", pd.Series(0.0, index=weights.index)) > 1e-12]
    return {
        "Start": str(net_returns.index[0].date()),
        "End": str(net_returns.index[-1].date()),
        "Total Return": total_return,
        "CAGR": cagr,
        "Max Drawdown": max_drawdown,
        "Ulcer Index": compute_ulcer_index(net_returns),
        "Volatility": volatility,
        "Sharpe": sharpe,
        "Beta vs QQQ": beta,
        "Alpha Ann vs QQQ": alpha_ann,
        "Information Ratio vs QQQ": information_ratio,
        "Up Capture vs QQQ": up_capture,
        "Down Capture vs QQQ": down_capture,
        "Turnover/Year": float(turnover.sum() / years),
        "Average TQQQ Weight": float(weights.get("TQQQ", pd.Series(0.0, index=weights.index)).mean()),
        "Average Idle Asset Weight": float(weights.get(str(run.metadata['idle_asset']), pd.Series(0.0, index=weights.index)).mean()),
        "Average Cash Weight": float(weights.get(CASH_SYMBOL, pd.Series(0.0, index=weights.index)).mean()),
        "Average Scale While Invested": float(active_scale.mean()) if not active_scale.empty else 0.0,
        "2022 Return": suite.compute_period_total_return(net_returns, "2022-01-01", "2022-12-31"),
        "2023+ CAGR": suite.compute_period_cagr(net_returns, "2023-01-01", None),
    }


def build_summary(runs: list[StrategyRun], benchmark_returns: pd.Series, costs_bps: list[float]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for run in runs:
        for cost_bps in costs_bps:
            for period_name, start, end in PERIODS:
                rows.append(
                    {
                        "strategy": run.strategy_name,
                        "display_name": run.display_name,
                        "cost_bps_one_way": float(cost_bps),
                        "period": period_name,
                        **summarize_period(run, benchmark_returns, cost_bps=float(cost_bps), start=start, end=end),
                        **run.metadata,
                    }
                )
    return pd.DataFrame(rows)


def build_recommendation(summary: pd.DataFrame) -> dict[str, object]:
    oos = summary[(summary['period'] == '2023+') & (summary['cost_bps_one_way'] == 5.0)].copy()
    baseline_qqq = oos[(oos['scaling'] == 'baseline') & (oos['idle_asset'] == 'QQQ')].iloc[0]
    baseline_boxx = oos[(oos['scaling'] == 'baseline') & (oos['idle_asset'] == 'BOXX')].iloc[0]
    scaled = oos[oos['scaling'] != 'baseline'].copy()
    scaled['score'] = (
        scaled['Information Ratio vs QQQ'].fillna(-999.0) * 4.0
        + scaled['Alpha Ann vs QQQ'].fillna(-999.0) * 2.0
        + scaled['CAGR'].fillna(-999.0)
        - scaled['Ulcer Index'].fillna(999.0) * 0.05
        - scaled['Turnover/Year'].fillna(999.0) * 0.03
    )
    best_scaled = scaled.sort_values('score', ascending=False).iloc[0]
    verdict = (
        'No tested scaling rule clearly upgrades the current baseline. The score-based variants smooth drawdown a bit, '
        'but the gain mostly comes from carrying less TQQQ and paying materially more turnover.'
    )
    return {
        'baseline_growth_reference': {
            'idle_asset': 'QQQ',
            'oos_cagr': float(baseline_qqq['CAGR']),
            'oos_max_drawdown': float(baseline_qqq['Max Drawdown']),
            'oos_ulcer': float(baseline_qqq['Ulcer Index']),
            'oos_ir_vs_qqq': float(baseline_qqq['Information Ratio vs QQQ']),
        },
        'baseline_defensive_reference': {
            'idle_asset': 'BOXX',
            'oos_cagr': float(baseline_boxx['CAGR']),
            'oos_max_drawdown': float(baseline_boxx['Max Drawdown']),
            'oos_ulcer': float(baseline_boxx['Ulcer Index']),
        },
        'best_scaled_candidate': {
            'scaling': str(best_scaled['scaling']),
            'idle_asset': str(best_scaled['idle_asset']),
            'oos_cagr': float(best_scaled['CAGR']),
            'oos_max_drawdown': float(best_scaled['Max Drawdown']),
            'oos_ulcer': float(best_scaled['Ulcer Index']),
            'oos_ir_vs_qqq': float(best_scaled['Information Ratio vs QQQ']),
            'turnover_per_year': float(best_scaled['Turnover/Year']),
            'average_scale_while_invested': float(best_scaled['Average Scale While Invested']),
        },
        'verdict': verdict,
    }


def build_markdown(summary: pd.DataFrame, recommendation: dict[str, object]) -> str:
    focus = summary[(summary['period'] == '2023+') & (summary['cost_bps_one_way'] == 5.0)].copy().sort_values(['scaling', 'idle_asset'])
    full = summary[(summary['period'] == 'Full Sample') & (summary['cost_bps_one_way'] == 5.0)].copy().sort_values(['scaling', 'idle_asset'])
    y2022 = summary[(summary['period'] == '2022') & (summary['cost_bps_one_way'] == 5.0)].copy().sort_values(['scaling', 'idle_asset'])
    return '\n'.join([
        '# TQQQ position-scaling follow-up',
        '',
        '## Setup',
        '- Keep the existing MA200 + ATR TQQQ entry/exit framework.',
        '- Change only the TQQQ position size while already invested.',
        '- Compare two idle assets: `BOXX` and `QQQ`.',
        '- Goal: see whether smoother in-position scaling creates a smoother equity curve without obviously killing CAGR.',
        '',
        '## OOS 2023+ (5 bps)',
        base.frame_to_markdown_table(focus[[
            'scaling', 'idle_asset', 'CAGR', 'Max Drawdown', 'Ulcer Index', 'Information Ratio vs QQQ',
            'Alpha Ann vs QQQ', 'Turnover/Year', 'Average TQQQ Weight', 'Average Scale While Invested'
        ]]),
        '',
        '## Full Sample (5 bps)',
        base.frame_to_markdown_table(full[[
            'scaling', 'idle_asset', 'CAGR', 'Max Drawdown', 'Ulcer Index', 'Information Ratio vs QQQ',
            'Turnover/Year', 'Average TQQQ Weight', 'Average Scale While Invested'
        ]]),
        '',
        '## 2022 (5 bps)',
        base.frame_to_markdown_table(y2022[[
            'scaling', 'idle_asset', 'Total Return', '2022 Return', 'Max Drawdown', 'Ulcer Index', 'Turnover/Year'
        ]]),
        '',
        '## Recommendation',
        f"- {recommendation['verdict']}",
    ]) + '\n'


def main() -> None:
    args = parse_args()
    results_dir = Path(args.results_dir).expanduser().resolve()
    results_dir.mkdir(parents=True, exist_ok=True)

    etf_frames = suite.download_etf_ohlcv(("QQQ", "TQQQ", "BOXX", "SPYI", "QQQI"), start=args.start, end=args.end)
    qqq_ohlc = pd.DataFrame({
        'open': etf_frames['open']['QQQ'],
        'high': etf_frames['high']['QQQ'],
        'low': etf_frames['low']['QQQ'],
        'close': etf_frames['close']['QQQ'],
    }).dropna()
    master_index = qqq_ohlc.index
    rows = suite.build_extra_etf_price_history(etf_frames, symbols=("QQQ", "TQQQ", "BOXX", "SPYI", "QQQI"))
    _, returns_matrix = suite.build_asset_return_matrix(rows, master_index=master_index, required_symbols=("QQQ", "TQQQ", "BOXX", "SPYI", "QQQI"))
    returns_matrix[CASH_SYMBOL] = 0.0
    indicators = base.build_indicator_frame(qqq_ohlc, etf_frames['volume']['QQQ'].reindex(master_index).fillna(0.0))

    runs: list[StrategyRun] = []
    for scaling in SCALING_CONFIGS:
        for idle_asset in (SAFE_HAVEN, 'QQQ'):
            runs.append(run_scaling_backtest(qqq_ohlc, returns_matrix, indicators, scaling=scaling, idle_asset=idle_asset))

    summary = build_summary(runs, returns_matrix['QQQ'].copy(), list(args.cost_bps))
    recommendation = build_recommendation(summary)

    comparison_path = results_dir / 'tqqq_hybrid_position_scaling_followup_comparison.csv'
    summary_path = results_dir / 'tqqq_hybrid_position_scaling_followup_summary.md'
    recommendation_path = results_dir / 'tqqq_hybrid_position_scaling_followup_recommendation.json'
    summary.to_csv(comparison_path, index=False)
    summary_path.write_text(build_markdown(summary, recommendation), encoding='utf-8')
    recommendation_path.write_text(json.dumps(recommendation, indent=2, ensure_ascii=False), encoding='utf-8')

    print(f'wrote {comparison_path}')
    print(f'wrote {summary_path}')
    print(f'wrote {recommendation_path}')


if __name__ == '__main__':
    main()
