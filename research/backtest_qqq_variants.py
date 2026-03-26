#!/usr/bin/env python3
"""
Compare the legacy non-tech rotation strategy, the current QQQ-enabled default,
and a proposed VOO/XLK/SMH structure. Optional QQQ core-satellite variants are
kept as additional reference benchmarks.
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from typing import Iterable

import numpy as np
import pandas as pd
import yfinance as yf


BASE_RANKING_POOL = [
    "EWY", "EWT", "INDA", "FXI", "EWJ", "VGK",
    "GLD", "SLV", "USO", "DBA",
    "XLE", "XLF", "ITA",
    "XLP", "XLU", "XLV", "IHI",
    "VNQ", "KRE",
]
CURRENT_DEFAULT_POOL = BASE_RANKING_POOL + ["QQQ"]
REPLACE_QQQ_WITH_VOO_POOL = BASE_RANKING_POOL + ["VOO"]
VOO_PLUS_XLK_POOL = REPLACE_QQQ_WITH_VOO_POOL + ["XLK"]
VOO_PLUS_XLK_PLUS_SMH_POOL = VOO_PLUS_XLK_POOL + ["SMH"]
CANARY_ASSETS = ["SPY", "EFA", "EEM", "AGG"]
SAFE_HAVEN = "BIL"

TOP_N = 2
SMA_PERIOD = 200
HOLD_BONUS = 0.02
CANARY_BAD_THRESHOLD = 4
REBALANCE_MONTHS = {3, 6, 9, 12}
DEFAULT_START = "2006-01-01"
DEFAULT_CORE_WEIGHTS = [0.20, 0.30, 0.40]
KEY_PERIODS = [
    ("Full Sample", None, None),
    ("Tech Bull 2009-2021", "2009-01-01", "2021-11-30"),
    ("Bear 2022", "2022-01-01", "2022-12-31"),
    ("AI Bull 2023+", "2023-01-01", None),
]
MAIN_COMPARISON_NAMES = [
    "baseline_non_tech",
    "current_default_qqq",
    "proposed_voo_xlk_smh",
]
BUILD_UP_COMPARISON_NAMES = [
    "current_default_qqq",
    "replace_qqq_with_voo",
    "voo_plus_xlk",
    "voo_plus_xlk_plus_smh",
]


@dataclass(frozen=True)
class StrategyConfig:
    name: str
    ranking_pool: tuple[str, ...]
    fixed_qqq_weight: float = 0.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default=DEFAULT_START, help="Price download start date (YYYY-MM-DD)")
    parser.add_argument("--end", default=None, help="Price download end date (YYYY-MM-DD)")
    parser.add_argument(
        "--qqq-core-weights",
        nargs="*",
        type=float,
        default=DEFAULT_CORE_WEIGHTS,
        help="Optional fixed QQQ core weights for reference core-satellite variants, e.g. --qqq-core-weights 0.2 0.3 0.4",
    )
    return parser.parse_args()


def build_configs(core_weights: Iterable[float]) -> list[StrategyConfig]:
    configs = [
        StrategyConfig("baseline_non_tech", tuple(BASE_RANKING_POOL)),
        StrategyConfig("current_default_qqq", tuple(CURRENT_DEFAULT_POOL)),
        StrategyConfig("proposed_voo_xlk_smh", tuple(VOO_PLUS_XLK_PLUS_SMH_POOL)),
        StrategyConfig("replace_qqq_with_voo", tuple(REPLACE_QQQ_WITH_VOO_POOL)),
        StrategyConfig("voo_plus_xlk", tuple(VOO_PLUS_XLK_POOL)),
        StrategyConfig("voo_plus_xlk_plus_smh", tuple(VOO_PLUS_XLK_PLUS_SMH_POOL)),
    ]
    for weight in core_weights:
        if not 0 < weight < 1:
            raise ValueError(f"QQQ core weight must be between 0 and 1, got {weight}")
        label = int(round(weight * 100))
        configs.append(
            StrategyConfig(
                f"qqq_core_{label}",
                tuple(BASE_RANKING_POOL),
                fixed_qqq_weight=weight,
            )
        )
    return configs


def download_prices(symbols: list[str], start: str, end: str | None) -> pd.DataFrame:
    data = yf.download(
        symbols,
        start=start,
        end=end,
        auto_adjust=True,
        progress=False,
        threads=False,
    )
    if data.empty:
        raise RuntimeError("No price data downloaded from Yahoo Finance")

    if isinstance(data.columns, pd.MultiIndex):
        closes = data["Close"].copy()
    else:
        closes = data[["Close"]].copy()
        closes.columns = symbols[:1]

    closes.index = pd.to_datetime(closes.index).tz_localize(None)
    closes = closes.sort_index()
    closes = closes.loc[:, ~closes.columns.duplicated()].copy()

    missing_symbols = [symbol for symbol in symbols if symbol not in closes.columns or closes[symbol].dropna().empty]
    for symbol in missing_symbols:
        single = yf.download(
            symbol,
            start=start,
            end=end,
            auto_adjust=True,
            progress=False,
            threads=False,
        )
        if single.empty:
            continue
        closes[symbol] = single["Close"]

    closes = closes.reindex(columns=symbols)
    unresolved = [symbol for symbol in symbols if closes[symbol].dropna().empty]
    if unresolved:
        raise RuntimeError(f"Failed to download usable price history for: {', '.join(unresolved)}")

    return closes


def normalize_price_matrix(prices: pd.DataFrame) -> pd.DataFrame:
    first_valid_dates = []
    for column in prices.columns:
        first_valid = prices[column].first_valid_index()
        if first_valid is None:
            raise RuntimeError(f"{column} has no valid price history")
        first_valid_dates.append(first_valid)

    common_start = max(first_valid_dates)
    normalized = prices.loc[common_start:].ffill().dropna(how="any")
    if normalized.empty:
        raise RuntimeError("No common history remains after aligning all symbols")
    return normalized


def compute_13612w_daily(closes: pd.Series) -> pd.Series:
    monthly_last = closes.groupby(closes.index.to_period("M")).last()
    month_periods = closes.index.to_period("M")
    current = closes.to_numpy(dtype=float)

    weighted_sum = np.zeros(len(closes), dtype=float)
    valid = np.ones(len(closes), dtype=bool)

    for months, weight in ((1, 12), (3, 4), (6, 2), (12, 1)):
        prior = monthly_last.reindex(month_periods - months).to_numpy(dtype=float)
        current_valid = ~np.isnan(current) & ~np.isnan(prior) & (prior != 0)
        valid &= current_valid
        weighted_sum[current_valid] += weight * (current[current_valid] / prior[current_valid] - 1.0)

    result = np.full(len(closes), np.nan)
    result[valid] = weighted_sum[valid] / 19.0
    return pd.Series(result, index=closes.index)


def build_rebalance_dates(index: pd.DatetimeIndex) -> set[pd.Timestamp]:
    quarter_end_dates = index[index.month.isin(REBALANCE_MONTHS)]
    grouped = pd.Series(quarter_end_dates, index=quarter_end_dates).groupby(quarter_end_dates.to_period("M")).max()
    return set(pd.to_datetime(grouped.values))


def compute_indicators(prices: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
    momentum = pd.DataFrame({symbol: compute_13612w_daily(prices[symbol]) for symbol in prices.columns})
    sma = prices.rolling(SMA_PERIOD, min_periods=SMA_PERIOD).mean()
    sma_ok = prices.gt(sma)
    canary_bad = momentum[CANARY_ASSETS].isna() | momentum[CANARY_ASSETS].lt(0)
    emergency = canary_bad.sum(axis=1) >= CANARY_BAD_THRESHOLD
    return momentum, sma_ok, emergency


def compute_rotation_weights(
    date: pd.Timestamp,
    config: StrategyConfig,
    momentum: pd.DataFrame,
    sma_ok: pd.DataFrame,
    current_weights: dict[str, float],
) -> dict[str, float]:
    scores: dict[str, float] = {}
    current_holdings = {symbol for symbol, weight in current_weights.items() if weight > 0 and symbol != SAFE_HAVEN}

    for symbol in config.ranking_pool:
        mom = momentum.at[date, symbol]
        if pd.isna(mom):
            continue
        if not bool(sma_ok.at[date, symbol]):
            continue
        bonus = HOLD_BONUS if symbol in current_holdings else 0.0
        scores[symbol] = float(mom + bonus)

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:TOP_N]
    if not ranked:
        return {SAFE_HAVEN: 1.0}

    per_weight = 1.0 / TOP_N
    weights = {symbol: per_weight for symbol, _ in ranked}
    if len(ranked) < TOP_N:
        weights[SAFE_HAVEN] = per_weight * (TOP_N - len(ranked))
    return weights


def combine_with_fixed_qqq(rotation_weights: dict[str, float], fixed_qqq_weight: float) -> dict[str, float]:
    if fixed_qqq_weight <= 0:
        return rotation_weights

    satellite_weight = 1.0 - fixed_qqq_weight
    combined = {symbol: weight * satellite_weight for symbol, weight in rotation_weights.items()}
    combined["QQQ"] = combined.get("QQQ", 0.0) + fixed_qqq_weight
    total = sum(combined.values())
    if not math.isclose(total, 1.0, rel_tol=1e-9, abs_tol=1e-9):
        combined = {symbol: weight / total for symbol, weight in combined.items()}
    return combined


def run_backtest(prices: pd.DataFrame, config: StrategyConfig) -> tuple[pd.Series, pd.DataFrame]:
    momentum, sma_ok, emergency = compute_indicators(prices)
    daily_returns = prices.pct_change().fillna(0.0)
    rebalance_dates = build_rebalance_dates(prices.index)

    portfolio_returns = pd.Series(0.0, index=prices.index, name=config.name)
    weights_history = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)

    current_weights: dict[str, float] = {SAFE_HAVEN: 1.0}

    for idx in range(len(prices.index) - 1):
        date = prices.index[idx]
        next_date = prices.index[idx + 1]

        if bool(emergency.loc[date]):
            emergency_weights = {SAFE_HAVEN: 1.0}
            current_weights = combine_with_fixed_qqq(emergency_weights, config.fixed_qqq_weight)
        elif date in rebalance_dates:
            rotation_weights = compute_rotation_weights(date, config, momentum, sma_ok, current_weights)
            current_weights = combine_with_fixed_qqq(rotation_weights, config.fixed_qqq_weight)

        for symbol, weight in current_weights.items():
            weights_history.at[date, symbol] = weight

        next_day_returns = daily_returns.loc[next_date]
        portfolio_returns.at[next_date] = sum(
            weight * float(next_day_returns.get(symbol, 0.0))
            for symbol, weight in current_weights.items()
        )

    if current_weights:
        for symbol, weight in current_weights.items():
            weights_history.at[prices.index[-1], symbol] = weight

    return portfolio_returns, weights_history


def summarize_returns(portfolio_returns: pd.Series, benchmark_returns: pd.DataFrame) -> dict[str, float]:
    returns = portfolio_returns.dropna()
    if returns.empty:
        raise RuntimeError("No portfolio returns to summarize")

    equity_curve = (1.0 + returns).cumprod()
    total_return = equity_curve.iloc[-1] - 1.0
    years = max((returns.index[-1] - returns.index[0]).days / 365.25, 1 / 365.25)
    cagr = equity_curve.iloc[-1] ** (1.0 / years) - 1.0
    drawdown = equity_curve / equity_curve.cummax() - 1.0
    volatility = returns.std(ddof=0) * np.sqrt(252)
    sharpe = returns.mean() / returns.std(ddof=0) * np.sqrt(252) if returns.std(ddof=0) else np.nan

    summary = {
        "Total Return": total_return,
        "CAGR": cagr,
        "Max Drawdown": drawdown.min(),
        "Volatility": volatility,
        "Sharpe": sharpe,
        "SPY Corr": returns.corr(benchmark_returns["SPY"]),
        "QQQ Corr": returns.corr(benchmark_returns["QQQ"]),
    }
    return summary


def summarize_period(
    strategy_returns: dict[str, pd.Series],
    benchmark_returns: pd.DataFrame,
    start: str | None,
    end: str | None,
) -> pd.DataFrame:
    rows = {}
    for name, returns in strategy_returns.items():
        sliced_returns = returns.copy()
        sliced_benchmarks = benchmark_returns.copy()
        if start:
            sliced_returns = sliced_returns.loc[start:]
            sliced_benchmarks = sliced_benchmarks.loc[start:]
        if end:
            sliced_returns = sliced_returns.loc[:end]
            sliced_benchmarks = sliced_benchmarks.loc[:end]
        rows[name] = summarize_returns(sliced_returns, sliced_benchmarks)
    frame = pd.DataFrame(rows).T
    return frame


def compute_annual_returns(strategy_returns: dict[str, pd.Series]) -> pd.DataFrame:
    annual = {}
    for name, returns in strategy_returns.items():
        annual[name] = (1.0 + returns).groupby(returns.index.year).prod() - 1.0
    frame = pd.DataFrame(annual).sort_index()
    frame.index.name = "Year"
    return frame


def format_percent_frame(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.map(lambda value: f"{value * 100:.1f}%" if pd.notna(value) else "nan")


def subset_strategy_returns(strategy_returns: dict[str, pd.Series], names: list[str]) -> dict[str, pd.Series]:
    return {name: strategy_returns[name] for name in names if name in strategy_returns}


def print_group_summary(
    title: str,
    strategy_returns: dict[str, pd.Series],
    benchmark_returns: pd.DataFrame,
    start: str | None,
    end: str | None,
) -> None:
    frame = summarize_period(strategy_returns, benchmark_returns, start, end)
    print(title)
    print(format_percent_frame(frame[["Total Return", "CAGR", "Max Drawdown", "Volatility"]]).to_string())
    print(frame[["Sharpe", "SPY Corr", "QQQ Corr"]].round(2).to_string())
    print()


def print_report(prices: pd.DataFrame, strategy_returns: dict[str, pd.Series]) -> None:
    benchmark_returns = prices[["SPY", "QQQ"]].pct_change().fillna(0.0)
    main_returns = subset_strategy_returns(strategy_returns, MAIN_COMPARISON_NAMES)
    build_up_returns = subset_strategy_returns(strategy_returns, BUILD_UP_COMPARISON_NAMES)
    qqq_core_returns = {
        name: returns for name, returns in strategy_returns.items() if name.startswith("qqq_core_")
    }

    print(f"Common backtest window: {prices.index[0].date()} -> {prices.index[-1].date()}")
    print(f"Tickers aligned: {', '.join(prices.columns)}")
    print()

    print("=== Full Sample Summary ===")
    print_group_summary("Main Comparison", main_returns, benchmark_returns, None, None)
    print_group_summary("Build-Up Comparison", build_up_returns, benchmark_returns, None, None)
    if qqq_core_returns:
        print_group_summary("QQQ Core Benchmarks", qqq_core_returns, benchmark_returns, None, None)

    print("=== Key Periods ===")
    for label, start, end in KEY_PERIODS[1:]:
        print(label)
        main_frame = summarize_period(main_returns, benchmark_returns, start, end)
        print("Main Comparison")
        print(format_percent_frame(main_frame[["Total Return", "CAGR", "Max Drawdown"]]).to_string())
        print(main_frame[["Sharpe", "SPY Corr", "QQQ Corr"]].round(2).to_string())
        print()

        build_up_frame = summarize_period(build_up_returns, benchmark_returns, start, end)
        print("Build-Up Comparison")
        print(format_percent_frame(build_up_frame[["Total Return", "CAGR", "Max Drawdown"]]).to_string())
        print(build_up_frame[["Sharpe", "SPY Corr", "QQQ Corr"]].round(2).to_string())
        print()

        if qqq_core_returns:
            qqq_core_frame = summarize_period(qqq_core_returns, benchmark_returns, start, end)
            print("QQQ Core Benchmarks")
            print(format_percent_frame(qqq_core_frame[["Total Return", "CAGR", "Max Drawdown"]]).to_string())
            print(qqq_core_frame[["Sharpe", "SPY Corr", "QQQ Corr"]].round(2).to_string())
            print()

    annual = compute_annual_returns(strategy_returns)
    print("=== Annual Returns ===")
    print(format_percent_frame(annual).to_string())


def main() -> None:
    args = parse_args()
    configs = build_configs(args.qqq_core_weights)

    all_symbols = sorted(
        {
            SAFE_HAVEN,
            "QQQ",
            "SPY",
            *CANARY_ASSETS,
            *(symbol for config in configs for symbol in config.ranking_pool),
        }
    )

    raw_prices = download_prices(all_symbols, start=args.start, end=args.end)
    prices = normalize_price_matrix(raw_prices)

    strategy_returns = {}
    for config in configs:
        returns, _weights = run_backtest(prices, config)
        strategy_returns[config.name] = returns

    print_report(prices, strategy_returns)


if __name__ == "__main__":
    main()
