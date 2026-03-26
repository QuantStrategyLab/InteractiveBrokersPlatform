#!/usr/bin/env python3
"""
Compare the legacy non-tech rotation strategy, the current QQQ-enabled default,
and a proposed VOO/XLK/SMH structure. Also includes lightweight rule tweaks,
rebalance-frequency experiments, and optional QQQ core-satellite reference
benchmarks.
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
EXPERIMENT_COMPARISON_NAMES = [
    "proposed_voo_xlk_smh",
    "voo_bonus_0_5",
    "voo_bonus_1_0",
    "switch_threshold_1_0",
    "hold_bonus_1_0",
    "hold_bonus_3_0",
]
REBALANCE_WEIGHTING_COMPARISON_NAMES = [
    "proposed_voo_xlk_smh",
    "monthly_top2_equal",
    "semiannual_top2_equal",
    "quarterly_top1_equal",
    "quarterly_top3_equal",
    "quarterly_top2_momentum_weighted",
    "monthly_top2_momentum_weighted",
]


@dataclass(frozen=True)
class StrategyConfig:
    name: str
    ranking_pool: tuple[str, ...]
    fixed_qqq_weight: float = 0.0
    hold_bonus_override: float | None = None
    voo_bonus: float = 0.0
    switch_threshold: float = 0.0
    rebalance_months_override: tuple[int, ...] | None = None
    top_n_override: int | None = None
    weighting_mode: str = "equal"


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
        StrategyConfig("voo_bonus_0_5", tuple(VOO_PLUS_XLK_PLUS_SMH_POOL), voo_bonus=0.005),
        StrategyConfig("voo_bonus_1_0", tuple(VOO_PLUS_XLK_PLUS_SMH_POOL), voo_bonus=0.01),
        StrategyConfig("switch_threshold_1_0", tuple(VOO_PLUS_XLK_PLUS_SMH_POOL), switch_threshold=0.01),
        StrategyConfig("hold_bonus_1_0", tuple(VOO_PLUS_XLK_PLUS_SMH_POOL), hold_bonus_override=0.01),
        StrategyConfig("hold_bonus_3_0", tuple(VOO_PLUS_XLK_PLUS_SMH_POOL), hold_bonus_override=0.03),
        StrategyConfig(
            "monthly_top2_equal",
            tuple(VOO_PLUS_XLK_PLUS_SMH_POOL),
            rebalance_months_override=tuple(range(1, 13)),
        ),
        StrategyConfig(
            "semiannual_top2_equal",
            tuple(VOO_PLUS_XLK_PLUS_SMH_POOL),
            rebalance_months_override=(6, 12),
        ),
        StrategyConfig(
            "quarterly_top1_equal",
            tuple(VOO_PLUS_XLK_PLUS_SMH_POOL),
            top_n_override=1,
        ),
        StrategyConfig(
            "quarterly_top3_equal",
            tuple(VOO_PLUS_XLK_PLUS_SMH_POOL),
            top_n_override=3,
        ),
        StrategyConfig(
            "quarterly_top2_momentum_weighted",
            tuple(VOO_PLUS_XLK_PLUS_SMH_POOL),
            weighting_mode="momentum",
        ),
        StrategyConfig(
            "monthly_top2_momentum_weighted",
            tuple(VOO_PLUS_XLK_PLUS_SMH_POOL),
            rebalance_months_override=tuple(range(1, 13)),
            weighting_mode="momentum",
        ),
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


def build_rebalance_dates(
    index: pd.DatetimeIndex,
    rebalance_months: Iterable[int] | None = None,
) -> set[pd.Timestamp]:
    months = set(REBALANCE_MONTHS if rebalance_months is None else rebalance_months)
    candidate_dates = index[index.month.isin(months)]
    grouped = pd.Series(candidate_dates, index=candidate_dates).groupby(candidate_dates.to_period("M")).max()
    return set(pd.to_datetime(grouped.values))


def resolve_top_n(config: StrategyConfig) -> int:
    top_n = TOP_N if config.top_n_override is None else config.top_n_override
    if top_n < 1:
        raise ValueError(f"top_n must be >= 1, got {top_n}")
    return top_n


def resolve_rebalance_months(config: StrategyConfig) -> tuple[int, ...]:
    return tuple(REBALANCE_MONTHS) if config.rebalance_months_override is None else config.rebalance_months_override


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
    current_holdings = {symbol for symbol, weight in current_weights.items() if weight > 0 and symbol != SAFE_HAVEN}
    scores = build_rotation_scores(date, config, momentum, sma_ok, current_holdings)
    top_n = resolve_top_n(config)
    ranked_symbols = choose_ranked_symbols(scores, current_holdings, config.switch_threshold, top_n)

    if not ranked_symbols:
        return {SAFE_HAVEN: 1.0}

    weights = allocate_selected_weights(ranked_symbols, scores, config.weighting_mode, top_n)
    if len(ranked_symbols) < top_n:
        weights[SAFE_HAVEN] = (top_n - len(ranked_symbols)) / top_n
    return weights


def build_rotation_scores(
    date: pd.Timestamp,
    config: StrategyConfig,
    momentum: pd.DataFrame,
    sma_ok: pd.DataFrame,
    current_holdings: set[str],
) -> dict[str, float]:
    scores: dict[str, float] = {}
    hold_bonus = HOLD_BONUS if config.hold_bonus_override is None else config.hold_bonus_override

    for symbol in config.ranking_pool:
        mom = momentum.at[date, symbol]
        if pd.isna(mom):
            continue
        if not bool(sma_ok.at[date, symbol]):
            continue
        bonus = hold_bonus if symbol in current_holdings else 0.0
        if symbol == "VOO":
            bonus += config.voo_bonus
        scores[symbol] = float(mom + bonus)

    return scores


def choose_ranked_symbols(
    scores: dict[str, float],
    current_holdings: set[str],
    switch_threshold: float,
    top_n: int,
) -> list[str]:
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    if not ranked:
        return []

    if switch_threshold <= 0 or not current_holdings:
        return [symbol for symbol, _score in ranked[:top_n]]

    current_ranked = sorted(
        [(symbol, scores[symbol]) for symbol in current_holdings if symbol in scores],
        key=lambda item: item[1],
        reverse=True,
    )
    selected: list[str] = []

    for held_symbol, held_score in current_ranked:
        challenger = next(
            (
                item for item in ranked
                if item[0] not in current_holdings and item[0] not in selected
            ),
            None,
        )
        if challenger and challenger[1] > held_score + switch_threshold:
            continue
        selected.append(held_symbol)
        if len(selected) == top_n:
            return selected

    for symbol, _score in ranked:
        if symbol in selected:
            continue
        selected.append(symbol)
        if len(selected) == top_n:
            break

    return selected


def allocate_selected_weights(
    ranked_symbols: list[str],
    scores: dict[str, float],
    weighting_mode: str,
    top_n: int,
) -> dict[str, float]:
    allocation_scale = len(ranked_symbols) / top_n
    if weighting_mode == "equal":
        per_weight = allocation_scale / len(ranked_symbols)
        return {symbol: per_weight for symbol in ranked_symbols}

    if weighting_mode != "momentum":
        raise ValueError(f"Unsupported weighting mode: {weighting_mode}")

    clipped_scores = {symbol: max(scores[symbol], 0.0) for symbol in ranked_symbols}
    score_sum = sum(clipped_scores.values())
    if score_sum <= 0:
        per_weight = allocation_scale / len(ranked_symbols)
        return {symbol: per_weight for symbol in ranked_symbols}

    return {
        symbol: allocation_scale * clipped_scores[symbol] / score_sum
        for symbol in ranked_symbols
    }


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
    rebalance_dates = build_rebalance_dates(prices.index, resolve_rebalance_months(config))

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


def summarize_execution(
    weights_history: pd.DataFrame,
    start: str | None,
    end: str | None,
) -> dict[str, float]:
    sliced_weights = weights_history.copy()
    if start:
        sliced_weights = sliced_weights.loc[start:]
    if end:
        sliced_weights = sliced_weights.loc[:end]
    if sliced_weights.empty:
        return {"Rebalances/Year": np.nan, "Turnover/Year": np.nan}

    changes = sliced_weights.fillna(0.0).diff().fillna(0.0)
    if not changes.empty:
        changes.iloc[0] = 0.0
    daily_turnover = 0.5 * changes.abs().sum(axis=1)
    years = max((sliced_weights.index[-1] - sliced_weights.index[0]).days / 365.25, 1 / 365.25)
    rebalances = float((daily_turnover > 1e-12).sum()) / years
    turnover = float(daily_turnover.sum()) / years
    return {
        "Rebalances/Year": rebalances,
        "Turnover/Year": turnover,
    }


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
    strategy_weights: dict[str, pd.DataFrame],
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
        row = summarize_returns(sliced_returns, sliced_benchmarks)
        row.update(summarize_execution(strategy_weights[name], start, end))
        rows[name] = row
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


def subset_named_series(items: dict[str, pd.Series], names: list[str]) -> dict[str, pd.Series]:
    return {name: items[name] for name in names if name in items}


def subset_named_frames(items: dict[str, pd.DataFrame], names: list[str]) -> dict[str, pd.DataFrame]:
    return {name: items[name] for name in names if name in items}


def print_group_summary(
    title: str,
    strategy_returns: dict[str, pd.Series],
    strategy_weights: dict[str, pd.DataFrame],
    benchmark_returns: pd.DataFrame,
    start: str | None,
    end: str | None,
) -> None:
    frame = summarize_period(strategy_returns, strategy_weights, benchmark_returns, start, end)
    print(title)
    print(format_percent_frame(frame[["Total Return", "CAGR", "Max Drawdown", "Volatility"]]).to_string())
    print(frame[["Sharpe", "SPY Corr", "QQQ Corr", "Rebalances/Year", "Turnover/Year"]].round(2).to_string())
    print()


def print_report(
    prices: pd.DataFrame,
    strategy_returns: dict[str, pd.Series],
    strategy_weights: dict[str, pd.DataFrame],
) -> None:
    benchmark_returns = prices[["SPY", "QQQ"]].pct_change().fillna(0.0)
    main_returns = subset_named_series(strategy_returns, MAIN_COMPARISON_NAMES)
    main_weights = subset_named_frames(strategy_weights, MAIN_COMPARISON_NAMES)
    build_up_returns = subset_named_series(strategy_returns, BUILD_UP_COMPARISON_NAMES)
    build_up_weights = subset_named_frames(strategy_weights, BUILD_UP_COMPARISON_NAMES)
    experiment_returns = subset_named_series(strategy_returns, EXPERIMENT_COMPARISON_NAMES)
    experiment_weights = subset_named_frames(strategy_weights, EXPERIMENT_COMPARISON_NAMES)
    rebalance_weighting_returns = subset_named_series(strategy_returns, REBALANCE_WEIGHTING_COMPARISON_NAMES)
    rebalance_weighting_weights = subset_named_frames(strategy_weights, REBALANCE_WEIGHTING_COMPARISON_NAMES)
    qqq_core_returns = {
        name: returns for name, returns in strategy_returns.items() if name.startswith("qqq_core_")
    }
    qqq_core_weights = {
        name: weights for name, weights in strategy_weights.items() if name.startswith("qqq_core_")
    }

    print(f"Common backtest window: {prices.index[0].date()} -> {prices.index[-1].date()}")
    print(f"Tickers aligned: {', '.join(prices.columns)}")
    print()

    print("=== Full Sample Summary ===")
    print_group_summary("Main Comparison", main_returns, main_weights, benchmark_returns, None, None)
    print_group_summary("Build-Up Comparison", build_up_returns, build_up_weights, benchmark_returns, None, None)
    print_group_summary("Lightweight Optimization Experiments", experiment_returns, experiment_weights, benchmark_returns, None, None)
    print_group_summary(
        "Rebalance and Weighting Experiments",
        rebalance_weighting_returns,
        rebalance_weighting_weights,
        benchmark_returns,
        None,
        None,
    )
    if qqq_core_returns:
        print_group_summary("QQQ Core Benchmarks", qqq_core_returns, qqq_core_weights, benchmark_returns, None, None)

    print("=== Key Periods ===")
    for label, start, end in KEY_PERIODS[1:]:
        print(label)
        main_frame = summarize_period(main_returns, main_weights, benchmark_returns, start, end)
        print("Main Comparison")
        print(format_percent_frame(main_frame[["Total Return", "CAGR", "Max Drawdown"]]).to_string())
        print(main_frame[["Sharpe", "SPY Corr", "QQQ Corr", "Rebalances/Year", "Turnover/Year"]].round(2).to_string())
        print()

        build_up_frame = summarize_period(build_up_returns, build_up_weights, benchmark_returns, start, end)
        print("Build-Up Comparison")
        print(format_percent_frame(build_up_frame[["Total Return", "CAGR", "Max Drawdown"]]).to_string())
        print(build_up_frame[["Sharpe", "SPY Corr", "QQQ Corr", "Rebalances/Year", "Turnover/Year"]].round(2).to_string())
        print()

        experiment_frame = summarize_period(experiment_returns, experiment_weights, benchmark_returns, start, end)
        print("Lightweight Optimization Experiments")
        print(format_percent_frame(experiment_frame[["Total Return", "CAGR", "Max Drawdown"]]).to_string())
        print(experiment_frame[["Sharpe", "SPY Corr", "QQQ Corr", "Rebalances/Year", "Turnover/Year"]].round(2).to_string())
        print()

        rebalance_weighting_frame = summarize_period(
            rebalance_weighting_returns,
            rebalance_weighting_weights,
            benchmark_returns,
            start,
            end,
        )
        print("Rebalance and Weighting Experiments")
        print(format_percent_frame(rebalance_weighting_frame[["Total Return", "CAGR", "Max Drawdown"]]).to_string())
        print(
            rebalance_weighting_frame[["Sharpe", "SPY Corr", "QQQ Corr", "Rebalances/Year", "Turnover/Year"]]
            .round(2)
            .to_string()
        )
        print()

        if qqq_core_returns:
            qqq_core_frame = summarize_period(qqq_core_returns, qqq_core_weights, benchmark_returns, start, end)
            print("QQQ Core Benchmarks")
            print(format_percent_frame(qqq_core_frame[["Total Return", "CAGR", "Max Drawdown"]]).to_string())
            print(qqq_core_frame[["Sharpe", "SPY Corr", "QQQ Corr", "Rebalances/Year", "Turnover/Year"]].round(2).to_string())
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
    strategy_weights = {}
    for config in configs:
        returns, weights = run_backtest(prices, config)
        strategy_returns[config.name] = returns
        strategy_weights[config.name] = weights

    print_report(prices, strategy_returns, strategy_weights)


if __name__ == "__main__":
    main()
