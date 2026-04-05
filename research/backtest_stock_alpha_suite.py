#!/usr/bin/env python3
"""
Cross-strategy comparison for:
- russell_1000_multi_factor_defensive
- hybrid_growth_income
- semiconductor_rotation_income
- qqq_plus_stock_alpha_v1 (research-only)

The script keeps the defensive baseline frozen, compares it fairly against the
two existing high-elasticity ETF strategies, and researches a separate
offensive stock-selection candidate that aims to outperform QQQ on a
benchmark-relative basis.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

import numpy as np
import pandas as pd
import yfinance as yf


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
US_EQUITY_STRATEGIES_ROOT = WORKSPACE_ROOT / "UsEquityStrategies"
QUANT_PLATFORM_KIT_ROOT = WORKSPACE_ROOT / "QuantPlatformKit"
LOCAL_RUNS_ROOT = WORKSPACE_ROOT / "_local_runs"

for candidate in (
    US_EQUITY_STRATEGIES_ROOT,
    US_EQUITY_STRATEGIES_ROOT / "src",
    QUANT_PLATFORM_KIT_ROOT,
    QUANT_PLATFORM_KIT_ROOT / "src",
):
    if candidate.exists():
        candidate_text = str(candidate)
        if candidate_text not in sys.path:
            sys.path.insert(0, candidate_text)

from us_equity_strategies.backtests.russell_1000_multi_factor_defensive import (  # noqa: E402
    build_monthly_rebalance_dates,
    resolve_active_universe,
    run_backtest as run_defensive_backtest,
)
from us_equity_strategies.snapshots.russell_1000_multi_factor_defensive import (  # noqa: E402
    read_table,
)
from us_equity_strategies.strategies.hybrid_growth_income import (  # noqa: E402
    get_hybrid_allocation,
    get_income_ratio as get_hybrid_income_ratio,
)
from us_equity_strategies.strategies.semiconductor_rotation_income import (  # noqa: E402
    get_dynamic_allocation,
    get_income_layer_ratio as get_semiconductor_income_layer_ratio,
)


SAFE_HAVEN = "BOXX"
HYBRID_SAFE_CASH = "CASH"
OFFENSIVE_NAME = "qqq_plus_stock_alpha_v1"

DEFENSIVE_NAME = "russell_1000_multi_factor_defensive"
HYBRID_FULL_NAME = "hybrid_growth_income"
HYBRID_NORMALIZED_NAME = "hybrid_growth_income_no_income"
SEMICONDUCTOR_FULL_NAME = "semiconductor_rotation_income"
SEMICONDUCTOR_NORMALIZED_NAME = "semiconductor_rotation_income_no_income"

FULL_COMPARISON_LAYER = "full_strategy"
NORMALIZED_COMPARISON_LAYER = "normalized"
RESEARCH_LAYER = "research_only"

DEFAULT_RESULTS_DIR = Path(__file__).resolve().parent / "results"
DEFAULT_ETF_START = "2018-01-01"
DEFAULT_ACCOUNT_EQUITY_FULL = 200_000.0
DEFAULT_ACCOUNT_EQUITY_NORMALIZED = 50_000.0
DEFAULT_COSTS_BPS = (0.0, 5.0)

FULL_PERIODS = (
    ("Full Sample", None, None),
    ("2018-2021", "2018-01-01", "2021-12-31"),
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023+", "2023-01-01", None),
)
ABLATION_PERIODS = (
    ("Full Sample", None, None),
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023+", "2023-01-01", None),
)

COMPARISON_ETFS = (
    "QQQ",
    "SPY",
    "TQQQ",
    "SOXL",
    "SOXX",
    "SPYI",
    "QQQI",
    "XLK",
    "SMH",
)


@dataclass(frozen=True)
class UniverseFilterConfig:
    name: str
    min_adv20_usd: float
    leadership_only: bool = False


@dataclass(frozen=True)
class RegimeConfig:
    name: str
    benchmark_symbol: str
    breadth_mode: str
    breadth_symbols: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExposureConfig:
    name: str
    soft_defense_exposure: float
    hard_defense_exposure: float


@dataclass(frozen=True)
class OffensiveConfig:
    name: str
    universe_filter: UniverseFilterConfig
    holdings_count: int
    single_name_cap: float
    sector_cap: float
    regime: RegimeConfig
    exposures: ExposureConfig
    hold_bonus: float = 0.10
    group_normalization: str = "sector"


@dataclass
class StrategyRun:
    strategy_name: str
    display_name: str
    comparison_layer: str
    gross_returns: pd.Series
    weights_history: pd.DataFrame
    turnover_history: pd.Series
    metadata: dict[str, object]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-run-dir",
        help="Prepared Russell 1000 data run directory (defaults to the newest official_monthly_v2_alias run under _local_runs)",
    )
    parser.add_argument(
        "--results-dir",
        default=str(DEFAULT_RESULTS_DIR),
        help="Directory where comparison tables and research outputs will be written",
    )
    parser.add_argument(
        "--start",
        default=None,
        help="Optional backtest start date override (defaults to the prepared Russell data start)",
    )
    parser.add_argument(
        "--end",
        default=None,
        help="Optional backtest end date override (defaults to the prepared Russell data latest price date)",
    )
    parser.add_argument(
        "--full-account-equity",
        type=float,
        default=DEFAULT_ACCOUNT_EQUITY_FULL,
        help="Starting equity for full-strategy comparisons",
    )
    parser.add_argument(
        "--normalized-account-equity",
        type=float,
        default=DEFAULT_ACCOUNT_EQUITY_NORMALIZED,
        help="Starting equity for normalized no-income comparisons",
    )
    parser.add_argument(
        "--cost-bps",
        nargs="*",
        type=float,
        default=list(DEFAULT_COSTS_BPS),
        help="One-way turnover cost assumptions in bps, e.g. --cost-bps 0 5",
    )
    return parser.parse_args()


def discover_prepared_r1000_run(explicit_path: str | None) -> Path:
    if explicit_path:
        path = Path(explicit_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"data-run-dir not found: {path}")
        return path

    candidates = sorted(
        LOCAL_RUNS_ROOT.glob("r1000_multifactor_defensive_*_official_monthly_v2_alias"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(
            "No prepared official_monthly_v2_alias Russell 1000 run found under "
            f"{LOCAL_RUNS_ROOT}"
        )
    return candidates[0]


def normalize_long_price_history(price_history) -> pd.DataFrame:
    frame = pd.DataFrame(price_history).copy()
    required = {"symbol", "as_of", "close", "volume"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"price_history missing required columns: {', '.join(sorted(missing))}")
    frame["symbol"] = frame["symbol"].astype(str).str.upper().str.strip()
    frame["as_of"] = pd.to_datetime(frame["as_of"]).dt.tz_localize(None).dt.normalize()
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce")
    frame = frame.dropna(subset=["symbol", "as_of", "close"])
    return frame.sort_values(["as_of", "symbol"]).reset_index(drop=True)


def normalize_universe_history(universe_history) -> pd.DataFrame:
    frame = pd.DataFrame(universe_history).copy()
    required = {"symbol", "sector"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"universe_history missing required columns: {', '.join(sorted(missing))}")

    frame["symbol"] = frame["symbol"].astype(str).str.upper().str.strip()
    frame["sector"] = frame["sector"].fillna("unknown").astype(str).str.strip().replace("", "unknown")
    for column in ("start_date", "end_date"):
        if column in frame.columns:
            frame[column] = pd.to_datetime(frame[column], errors="coerce").dt.tz_localize(None).dt.normalize()
    return frame.sort_values(["symbol"] + [column for column in ("start_date", "end_date") if column in frame.columns]).reset_index(drop=True)


def discover_prepared_data(data_run_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp, pd.Timestamp]:
    universe_path = data_run_dir / "r1000_universe_history.csv"
    prices_path = data_run_dir / "r1000_price_history.csv"
    if not universe_path.exists() or not prices_path.exists():
        raise FileNotFoundError(
            f"Missing prepared Russell files under {data_run_dir}; "
            "expected r1000_universe_history.csv and r1000_price_history.csv"
        )

    universe_history = normalize_universe_history(read_table(universe_path))
    price_history = normalize_long_price_history(read_table(prices_path))
    start_date = pd.Timestamp(price_history["as_of"].min()).normalize()
    end_date = pd.Timestamp(price_history["as_of"].max()).normalize()
    return universe_history, price_history, start_date, end_date


def download_etf_ohlcv(symbols: Iterable[str], start: str, end: str | None) -> dict[str, pd.DataFrame]:
    symbols = [str(symbol).strip().upper() for symbol in symbols]
    data = yf.download(
        symbols,
        start=start,
        end=end,
        auto_adjust=True,
        progress=False,
        threads=False,
    )
    if data.empty:
        raise RuntimeError("No ETF price data downloaded from Yahoo Finance")

    fields = ("Open", "High", "Low", "Close", "Volume")
    frames: dict[str, pd.DataFrame] = {}
    if isinstance(data.columns, pd.MultiIndex):
        for field in fields:
            field_frame = data[field].copy() if field in data.columns.get_level_values(0) else pd.DataFrame(index=data.index)
            field_frame.index = pd.to_datetime(field_frame.index).tz_localize(None).normalize()
            field_frame.columns = field_frame.columns.map(str).str.upper()
            frames[field.lower()] = field_frame.reindex(columns=symbols)
    else:
        for field in fields:
            if field not in data.columns:
                frames[field.lower()] = pd.DataFrame(index=pd.to_datetime(data.index).tz_localize(None).normalize())
                continue
            field_frame = data[[field]].copy()
            field_frame.index = pd.to_datetime(field_frame.index).tz_localize(None).normalize()
            field_frame.columns = symbols[:1]
            frames[field.lower()] = field_frame

    for symbol in symbols:
        if symbol in frames["close"].columns and frames["close"][symbol].dropna().any():
            continue
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
        single.index = pd.to_datetime(single.index).tz_localize(None).normalize()
        for field in fields:
            field_key = field.lower()
            if symbol not in frames[field_key].columns:
                frames[field_key][symbol] = np.nan
            if field in single.columns:
                frames[field_key][symbol] = single[field]

    frames["close"] = frames["close"].reindex(columns=symbols)
    unresolved = [symbol for symbol in symbols if symbol not in frames["close"].columns or frames["close"][symbol].dropna().empty]
    if unresolved:
        raise RuntimeError(f"Failed to download usable ETF price history for: {', '.join(unresolved)}")

    return frames


def build_extra_etf_price_history(
    etf_frames: Mapping[str, pd.DataFrame],
    *,
    symbols: Iterable[str],
) -> pd.DataFrame:
    closes = etf_frames["close"].copy()
    volumes = etf_frames["volume"].copy()
    rows: list[dict[str, object]] = []
    for symbol in symbols:
        symbol_text = str(symbol).strip().upper()
        if symbol_text not in closes.columns:
            continue
        close_series = pd.to_numeric(closes[symbol_text], errors="coerce")
        volume_series = (
            pd.to_numeric(volumes[symbol_text], errors="coerce")
            if symbol_text in volumes.columns
            else pd.Series(index=close_series.index, dtype=float)
        )
        for as_of, close in close_series.dropna().items():
            volume = volume_series.get(as_of)
            rows.append(
                {
                    "symbol": symbol_text,
                    "as_of": pd.Timestamp(as_of).normalize(),
                    "close": float(close),
                    "volume": float(volume) if pd.notna(volume) else float("nan"),
                }
            )
    return pd.DataFrame(rows, columns=["symbol", "as_of", "close", "volume"]).sort_values(["as_of", "symbol"]).reset_index(drop=True)


def build_master_index(stock_prices: pd.DataFrame, etf_close_frame: pd.DataFrame) -> pd.DatetimeIndex:
    stock_index = pd.DatetimeIndex(sorted(pd.to_datetime(stock_prices["as_of"]).unique()))
    qqq_index = pd.DatetimeIndex(etf_close_frame["QQQ"].dropna().index)
    common = stock_index.intersection(qqq_index)
    if common.empty:
        raise RuntimeError("No common dates between Russell stock data and ETF benchmark data")
    return common


def build_asset_return_matrix(
    price_history: pd.DataFrame,
    *,
    master_index: pd.DatetimeIndex,
    required_symbols: Iterable[str] = (),
) -> tuple[pd.DataFrame, pd.DataFrame]:
    close_matrix = (
        price_history.pivot_table(index="as_of", columns="symbol", values="close", aggfunc="last")
        .sort_index()
        .reindex(master_index)
    )
    close_matrix = close_matrix.ffill()
    for symbol in required_symbols:
        symbol_text = str(symbol).strip().upper()
        if symbol_text not in close_matrix.columns:
            close_matrix[symbol_text] = np.nan
    close_matrix = close_matrix.reindex(columns=sorted(close_matrix.columns))
    returns_matrix = close_matrix.pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    return close_matrix, returns_matrix


def compute_turnover(previous_weights: Mapping[str, float], new_weights: Mapping[str, float]) -> float:
    symbols = set(previous_weights) | set(new_weights)
    return 0.5 * sum(abs(float(new_weights.get(symbol, 0.0)) - float(previous_weights.get(symbol, 0.0))) for symbol in symbols)


def compute_negative_sortino(returns: pd.Series) -> float:
    downside = returns.loc[returns < 0]
    if downside.empty:
        return float("nan")
    downside_std = float(downside.std(ddof=0))
    if downside_std == 0:
        return float("nan")
    return float(returns.mean() / downside_std * math.sqrt(252))


def compute_capture_ratios(strategy_returns: pd.Series, benchmark_returns: pd.Series) -> tuple[float, float]:
    strategy_monthly = (1.0 + strategy_returns).groupby(strategy_returns.index.to_period("M")).prod() - 1.0
    benchmark_monthly = (1.0 + benchmark_returns).groupby(benchmark_returns.index.to_period("M")).prod() - 1.0
    aligned = pd.concat(
        [
            strategy_monthly.rename("strategy"),
            benchmark_monthly.rename("benchmark"),
        ],
        axis=1,
    ).dropna()
    if aligned.empty:
        return float("nan"), float("nan")

    up = aligned.loc[aligned["benchmark"] > 0]
    down = aligned.loc[aligned["benchmark"] < 0]

    up_capture = float(up["strategy"].mean() / up["benchmark"].mean()) if not up.empty and up["benchmark"].mean() != 0 else float("nan")
    down_capture = float(down["strategy"].mean() / down["benchmark"].mean()) if not down.empty and down["benchmark"].mean() != 0 else float("nan")
    return up_capture, down_capture


def compute_beta(strategy_returns: pd.Series, benchmark_returns: pd.Series) -> float:
    aligned = pd.concat(
        [strategy_returns.rename("strategy"), benchmark_returns.rename("benchmark")],
        axis=1,
    ).dropna()
    if len(aligned) < 2:
        return float("nan")
    benchmark_var = float(aligned["benchmark"].var(ddof=0))
    if benchmark_var == 0:
        return float("nan")
    return float(aligned["strategy"].cov(aligned["benchmark"], ddof=0) / benchmark_var)


def compute_information_ratio(strategy_returns: pd.Series, benchmark_returns: pd.Series) -> float:
    aligned = pd.concat(
        [strategy_returns.rename("strategy"), benchmark_returns.rename("benchmark")],
        axis=1,
    ).dropna()
    if aligned.empty:
        return float("nan")
    active = aligned["strategy"] - aligned["benchmark"]
    active_std = float(active.std(ddof=0))
    if active_std == 0:
        return float("nan")
    return float(active.mean() / active_std * math.sqrt(252))


def compute_period_total_return(returns: pd.Series, start: str | None, end: str | None) -> float:
    sliced = returns.copy()
    if start:
        sliced = sliced.loc[start:]
    if end:
        sliced = sliced.loc[:end]
    if sliced.empty:
        return float("nan")
    return float((1.0 + sliced).prod() - 1.0)


def compute_period_cagr(returns: pd.Series, start: str | None, end: str | None) -> float:
    sliced = returns.copy()
    if start:
        sliced = sliced.loc[start:]
    if end:
        sliced = sliced.loc[:end]
    sliced = sliced.dropna()
    if sliced.empty:
        return float("nan")
    equity = float((1.0 + sliced).prod())
    years = max((sliced.index[-1] - sliced.index[0]).days / 365.25, 1 / 365.25)
    return float(equity ** (1.0 / years) - 1.0)


def summarize_strategy_period(
    returns: pd.Series,
    weights_history: pd.DataFrame,
    turnover_history: pd.Series,
    benchmark_returns: pd.Series,
    *,
    start: str | None,
    end: str | None,
    safe_haven_symbols: Iterable[str],
    full_returns_reference: pd.Series,
) -> dict[str, float | str]:
    sliced_returns = returns.copy()
    sliced_weights = weights_history.copy()
    sliced_turnover = turnover_history.copy()
    sliced_benchmark = benchmark_returns.copy()
    if start:
        sliced_returns = sliced_returns.loc[start:]
        sliced_weights = sliced_weights.loc[start:]
        sliced_turnover = sliced_turnover.loc[start:]
        sliced_benchmark = sliced_benchmark.loc[start:]
    if end:
        sliced_returns = sliced_returns.loc[:end]
        sliced_weights = sliced_weights.loc[:end]
        sliced_turnover = sliced_turnover.loc[:end]
        sliced_benchmark = sliced_benchmark.loc[:end]

    sliced_returns = sliced_returns.dropna()
    sliced_benchmark = sliced_benchmark.reindex(sliced_returns.index).fillna(0.0)
    if sliced_returns.empty:
        raise RuntimeError("No strategy returns remain inside the selected period")

    equity_curve = (1.0 + sliced_returns).cumprod()
    total_return = float(equity_curve.iloc[-1] - 1.0)
    years = max((sliced_returns.index[-1] - sliced_returns.index[0]).days / 365.25, 1 / 365.25)
    cagr = float(equity_curve.iloc[-1] ** (1.0 / years) - 1.0)
    drawdown = equity_curve / equity_curve.cummax() - 1.0
    max_drawdown = float(drawdown.min())
    volatility = float(sliced_returns.std(ddof=0) * math.sqrt(252))
    std = float(sliced_returns.std(ddof=0))
    sharpe = float(sliced_returns.mean() / std * math.sqrt(252)) if std else float("nan")
    sortino = compute_negative_sortino(sliced_returns)
    calmar = float(cagr / abs(max_drawdown)) if max_drawdown < 0 else float("nan")
    beta_vs_qqq = compute_beta(sliced_returns, sliced_benchmark)
    information_ratio = compute_information_ratio(sliced_returns, sliced_benchmark)
    up_capture, down_capture = compute_capture_ratios(sliced_returns, sliced_benchmark)

    changes = sliced_weights.fillna(0.0).diff().fillna(0.0)
    if not changes.empty:
        changes.iloc[0] = 0.0
    daily_turnover = sliced_turnover.reindex(sliced_returns.index).fillna(0.0)
    rebalances_per_year = float((daily_turnover > 1e-12).sum() / years)
    turnover_per_year = float(daily_turnover.sum() / years)

    safe_haven_set = {str(symbol).strip().upper() for symbol in safe_haven_symbols}
    name_columns = [column for column in sliced_weights.columns if str(column).strip().upper() not in safe_haven_set]
    if name_columns:
        avg_names_held = float((sliced_weights[name_columns].fillna(0.0) > 1e-12).sum(axis=1).mean())
    else:
        avg_names_held = 0.0

    year_2022_return = compute_period_total_return(full_returns_reference, "2022-01-01", "2022-12-31")
    cagr_2023_plus = compute_period_cagr(full_returns_reference, "2023-01-01", None)

    return {
        "Start": str(sliced_returns.index[0].date()),
        "End": str(sliced_returns.index[-1].date()),
        "Total Return": total_return,
        "CAGR": cagr,
        "Max Drawdown": max_drawdown,
        "Volatility": volatility,
        "Sharpe": sharpe,
        "Sortino": sortino,
        "Calmar": calmar,
        "Turnover/Year": turnover_per_year,
        "Rebalances/Year": rebalances_per_year,
        "Average Names Held": avg_names_held,
        "Beta vs QQQ": beta_vs_qqq,
        "Information Ratio vs QQQ": information_ratio,
        "Up Capture vs QQQ": up_capture,
        "Down Capture vs QQQ": down_capture,
        "2022 Return": year_2022_return,
        "2023+ CAGR": cagr_2023_plus,
    }


def build_period_summary_rows(
    strategy_runs: list[StrategyRun],
    benchmark_returns: pd.Series,
    *,
    costs_bps: Iterable[float],
    periods: Iterable[tuple[str, str | None, str | None]],
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for strategy_run in strategy_runs:
        for cost_bps in costs_bps:
            net_returns = strategy_run.gross_returns - strategy_run.turnover_history.reindex(strategy_run.gross_returns.index).fillna(0.0) * (float(cost_bps) / 10_000.0)
            for period_name, start, end in periods:
                metrics = summarize_strategy_period(
                    net_returns,
                    strategy_run.weights_history,
                    strategy_run.turnover_history,
                    benchmark_returns,
                    start=start,
                    end=end,
                    safe_haven_symbols=(SAFE_HAVEN, HYBRID_SAFE_CASH),
                    full_returns_reference=net_returns,
                )
                rows.append(
                    {
                        "comparison_layer": strategy_run.comparison_layer,
                        "strategy": strategy_run.strategy_name,
                        "display_name": strategy_run.display_name,
                        "cost_bps_one_way": float(cost_bps),
                        "period": period_name,
                        **metrics,
                        **strategy_run.metadata,
                    }
                )
    return pd.DataFrame(rows)


def compute_rolling_36m_capm_alpha(strategy_returns: pd.Series, benchmark_returns: pd.Series) -> pd.Series:
    aligned = pd.concat(
        [
            strategy_returns.rename("strategy"),
            benchmark_returns.rename("benchmark"),
        ],
        axis=1,
    ).dropna()
    if aligned.empty:
        return pd.Series(dtype=float, name=strategy_returns.name)

    window = 756
    results: list[tuple[pd.Timestamp, float]] = []
    for end_index in range(window, len(aligned) + 1):
        sample = aligned.iloc[end_index - window : end_index]
        benchmark_var = float(sample["benchmark"].var(ddof=0))
        if benchmark_var == 0:
            alpha_ann = float("nan")
        else:
            beta = float(sample["strategy"].cov(sample["benchmark"], ddof=0) / benchmark_var)
            alpha_daily = float((sample["strategy"] - beta * sample["benchmark"]).mean())
            alpha_ann = alpha_daily * 252.0
        results.append((sample.index[-1], alpha_ann))
    return pd.Series(
        [value for _date, value in results],
        index=[date for date, _value in results],
        name=strategy_returns.name,
    )


def run_hybrid_growth_income_backtest(
    qqq_ohlc: pd.DataFrame,
    asset_returns: pd.DataFrame,
    *,
    starting_equity: float,
    income_threshold_usd: float,
    qqqi_income_ratio: float,
    cash_reserve_ratio: float,
    rebalance_threshold_ratio: float,
    alloc_tier1_breakpoints: Iterable[float],
    alloc_tier1_values: Iterable[float],
    alloc_tier2_breakpoints: Iterable[float],
    alloc_tier2_values: Iterable[float],
    risk_leverage_factor: float,
    risk_agg_cap: float,
    risk_numerator: float,
    atr_exit_scale: float,
    atr_entry_scale: float,
    exit_line_floor: float,
    exit_line_cap: float,
    entry_line_floor: float,
    entry_line_cap: float,
) -> tuple[pd.Series, pd.DataFrame, pd.Series]:
    strategy_symbols = ["TQQQ", SAFE_HAVEN, "SPYI", "QQQI", HYBRID_SAFE_CASH]
    index = asset_returns.index.intersection(qqq_ohlc.index)
    qqq_history = qqq_ohlc.loc[index].copy()
    returns = asset_returns.reindex(index).fillna(0.0)
    weights_history = pd.DataFrame(0.0, index=index, columns=strategy_symbols)
    portfolio_returns = pd.Series(0.0, index=index, name=HYBRID_FULL_NAME)
    turnover_history = pd.Series(0.0, index=index, name="turnover")

    current_weights: dict[str, float] = {SAFE_HAVEN: 1.0}
    current_equity = float(starting_equity)

    for idx in range(len(index) - 1):
        date = index[idx]
        next_date = index[idx + 1]

        history = qqq_history.loc[:date]
        qqq_p = float(history["close"].iloc[-1])
        ma200 = float(history["close"].rolling(200).mean().iloc[-1])
        true_range = pd.concat(
            [
                history["high"] - history["low"],
                (history["high"] - history["close"].shift(1)).abs(),
                (history["low"] - history["close"].shift(1)).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr_pct = float(true_range.rolling(14).mean().iloc[-1] / qqq_p) if len(history) >= 14 else 0.0

        exit_line = ma200 * max(exit_line_floor, min(exit_line_cap, 1.0 - (atr_pct * atr_exit_scale)))
        entry_line = ma200 * max(entry_line_floor, min(entry_line_cap, 1.0 + (atr_pct * atr_entry_scale)))

        income_ratio = get_hybrid_income_ratio(current_equity, income_threshold_usd=income_threshold_usd)
        target_income_value = current_equity * income_ratio
        target_spyi_value = target_income_value * (1.0 - qqqi_income_ratio)
        target_qqqi_value = target_income_value * qqqi_income_ratio

        strategy_equity = max(0.0, current_equity - target_income_value)
        reserved = strategy_equity * cash_reserve_ratio
        agg_ratio, _target_yield = get_hybrid_allocation(
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
        target_boxx_value = max(0.0, (strategy_equity - reserved) - target_tqqq_value)
        target_values = {
            "TQQQ": target_tqqq_value,
            SAFE_HAVEN: target_boxx_value,
            "SPYI": target_spyi_value,
            "QQQI": target_qqqi_value,
            HYBRID_SAFE_CASH: reserved,
        }
        target_weights = {
            symbol: value / current_equity
            for symbol, value in target_values.items()
            if value > 1e-12 and current_equity > 0
        }
        if not target_weights:
            target_weights = {HYBRID_SAFE_CASH: 1.0}

        threshold_weight = rebalance_threshold_ratio
        rebalance_needed = any(
            abs(target_weights.get(symbol, 0.0) - current_weights.get(symbol, 0.0)) > threshold_weight
            for symbol in set(target_weights) | set(current_weights)
        )
        if rebalance_needed:
            turnover_history.at[next_date] = compute_turnover(current_weights, target_weights)
            current_weights = target_weights

        for symbol, weight in current_weights.items():
            weights_history.at[date, symbol] = weight

        next_returns = returns.loc[next_date]
        portfolio_returns.at[next_date] = sum(
            weight * float(next_returns.get(symbol, 0.0))
            for symbol, weight in current_weights.items()
            if symbol != HYBRID_SAFE_CASH
        )
        current_equity *= 1.0 + float(portfolio_returns.at[next_date])

    for symbol, weight in current_weights.items():
        weights_history.at[index[-1], symbol] = weight

    return portfolio_returns, weights_history, turnover_history


def run_semiconductor_rotation_income_backtest(
    soxl_prices: pd.Series,
    asset_returns: pd.DataFrame,
    *,
    starting_equity: float,
    trend_ma_window: int,
    cash_reserve_ratio: float,
    min_trade_ratio: float,
    min_trade_floor: float,
    rebalance_threshold_ratio: float,
    small_account_deploy_ratio: float,
    mid_account_deploy_ratio: float,
    large_account_deploy_ratio: float,
    trade_layer_decay_coeff: float,
    income_layer_start_usd: float,
    income_layer_max_ratio: float,
    income_layer_qqqi_weight: float,
    income_layer_spyi_weight: float,
) -> tuple[pd.Series, pd.DataFrame, pd.Series]:
    strategy_symbols = ["SOXL", "SOXX", SAFE_HAVEN, "QQQI", "SPYI"]
    index = asset_returns.index.intersection(soxl_prices.index)
    soxl_series = soxl_prices.loc[index].copy()
    returns = asset_returns.reindex(index).fillna(0.0)
    weights_history = pd.DataFrame(0.0, index=index, columns=strategy_symbols)
    portfolio_returns = pd.Series(0.0, index=index, name=SEMICONDUCTOR_FULL_NAME)
    turnover_history = pd.Series(0.0, index=index, name="turnover")

    current_weights: dict[str, float] = {SAFE_HAVEN: 1.0}
    current_equity = float(starting_equity)

    for idx in range(len(index) - 1):
        date = index[idx]
        next_date = index[idx + 1]

        soxl_price = float(soxl_series.loc[date])
        ma_trend = float(soxl_series.loc[:date].rolling(trend_ma_window).mean().iloc[-1])
        active_risk_asset = "SOXL" if soxl_price > ma_trend else "SOXX"

        income_layer_ratio = get_semiconductor_income_layer_ratio(
            current_equity,
            income_layer_start_usd=income_layer_start_usd,
            income_layer_max_ratio=income_layer_max_ratio,
        )
        current_income_layer_value = current_equity * (current_weights.get("QQQI", 0.0) + current_weights.get("SPYI", 0.0))
        desired_income_layer_value = current_equity * income_layer_ratio
        locked_income_layer_value = max(current_income_layer_value, desired_income_layer_value)
        income_layer_add_value = max(0.0, locked_income_layer_value - current_income_layer_value)

        core_equity = max(0.0, current_equity - locked_income_layer_value)
        deploy_ratio = get_dynamic_allocation(
            core_equity,
            small_account_deploy_ratio=small_account_deploy_ratio,
            mid_account_deploy_ratio=mid_account_deploy_ratio,
            large_account_deploy_ratio=large_account_deploy_ratio,
            trade_layer_decay_coeff=trade_layer_decay_coeff,
        )
        deployed_capital = core_equity * deploy_ratio
        current_values = {symbol: current_equity * current_weights.get(symbol, 0.0) for symbol in strategy_symbols}
        target_values = {
            "SOXL": deployed_capital if active_risk_asset == "SOXL" else 0.0,
            "SOXX": deployed_capital if active_risk_asset == "SOXX" else 0.0,
            "QQQI": current_values.get("QQQI", 0.0) + (income_layer_add_value * income_layer_qqqi_weight),
            "SPYI": current_values.get("SPYI", 0.0) + (income_layer_add_value * income_layer_spyi_weight),
            SAFE_HAVEN: max(0.0, core_equity - deployed_capital),
        }
        target_weights = {
            symbol: value / current_equity
            for symbol, value in target_values.items()
            if value > 1e-12 and current_equity > 0
        }
        if not target_weights:
            target_weights = {SAFE_HAVEN: 1.0}

        min_trade_weight = max(rebalance_threshold_ratio, min_trade_ratio, min_trade_floor / current_equity if current_equity > 0 else 0.0)
        rebalance_needed = any(
            abs(target_weights.get(symbol, 0.0) - current_weights.get(symbol, 0.0)) > min_trade_weight
            for symbol in set(target_weights) | set(current_weights)
        )
        if rebalance_needed:
            turnover_history.at[next_date] = compute_turnover(current_weights, target_weights)
            current_weights = target_weights

        for symbol, weight in current_weights.items():
            weights_history.at[date, symbol] = weight

        next_returns = returns.loc[next_date]
        portfolio_returns.at[next_date] = sum(
            weight * float(next_returns.get(symbol, 0.0))
            for symbol, weight in current_weights.items()
        )
        current_equity *= 1.0 + float(portfolio_returns.at[next_date])

    for symbol, weight in current_weights.items():
        weights_history.at[index[-1], symbol] = weight

    return portfolio_returns, weights_history, turnover_history


def zscore(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    std = float(numeric.std(ddof=0))
    if pd.isna(std) or std == 0:
        return pd.Series(0.0, index=values.index, dtype=float)
    return ((numeric - numeric.mean()) / std).fillna(0.0)


def compute_window_drawdown(closes: pd.Series) -> float:
    if closes.empty:
        return float("nan")
    running_peak = closes.cummax()
    drawdown = closes / running_peak - 1.0
    return float(drawdown.min())


def precompute_stock_feature_history(price_history: pd.DataFrame) -> dict[str, pd.DataFrame]:
    feature_history: dict[str, pd.DataFrame] = {}
    for symbol, group in price_history.groupby("symbol", sort=False):
        history = group.sort_values("as_of").reset_index(drop=True).copy()
        closes = pd.to_numeric(history["close"], errors="coerce")
        volumes = pd.to_numeric(history["volume"], errors="coerce")
        returns = closes.pct_change()
        dollar_volume = closes * volumes
        feature_history[str(symbol)] = pd.DataFrame(
            {
                "as_of": history["as_of"],
                "close": closes,
                "volume": volumes,
                "adv20_usd": dollar_volume.rolling(20).mean(),
                "history_days": np.arange(1, len(history) + 1, dtype=int),
                "mom_6_1": closes.shift(21) / closes.shift(147) - 1.0,
                "mom_12_1": closes.shift(21) / closes.shift(273) - 1.0,
                "sma200_gap": closes / closes.rolling(200).mean() - 1.0,
                "vol_63": returns.rolling(63).std(ddof=0) * math.sqrt(252),
                "breakout_252": closes / closes.rolling(252).max() - 1.0,
            }
        )
    return feature_history


def lookup_symbol_features(
    symbol: str,
    as_of: pd.Timestamp,
    feature_history_by_symbol: Mapping[str, pd.DataFrame],
    *,
    sector: str,
    drawdown_window: int = 126,
) -> dict[str, object]:
    history = feature_history_by_symbol.get(symbol)
    if history is None or history.empty:
        return {
            "as_of": as_of,
            "symbol": symbol,
            "sector": sector,
            "close": float("nan"),
            "volume": float("nan"),
            "adv20_usd": float("nan"),
            "history_days": 0,
            "mom_6_1": float("nan"),
            "mom_12_1": float("nan"),
            "sma200_gap": float("nan"),
            "vol_63": float("nan"),
            "maxdd_126": float("nan"),
            "breakout_252": float("nan"),
        }

    cutoff = int(history["as_of"].searchsorted(as_of, side="right"))
    if cutoff <= 0:
        return {
            "as_of": as_of,
            "symbol": symbol,
            "sector": sector,
            "close": float("nan"),
            "volume": float("nan"),
            "adv20_usd": float("nan"),
            "history_days": 0,
            "mom_6_1": float("nan"),
            "mom_12_1": float("nan"),
            "sma200_gap": float("nan"),
            "vol_63": float("nan"),
            "maxdd_126": float("nan"),
            "breakout_252": float("nan"),
        }

    current = history.iloc[cutoff - 1]
    closes_window = history["close"].iloc[max(0, cutoff - drawdown_window) : cutoff]
    maxdd_126 = compute_window_drawdown(closes_window) if len(closes_window) >= drawdown_window else float("nan")
    return {
        "as_of": as_of,
        "symbol": symbol,
        "sector": sector,
        "close": float(current["close"]) if pd.notna(current["close"]) else float("nan"),
        "volume": float(current["volume"]) if pd.notna(current["volume"]) else float("nan"),
        "adv20_usd": float(current["adv20_usd"]) if pd.notna(current["adv20_usd"]) else float("nan"),
        "history_days": int(current["history_days"]) if pd.notna(current["history_days"]) else 0,
        "mom_6_1": float(current["mom_6_1"]) if pd.notna(current["mom_6_1"]) else float("nan"),
        "mom_12_1": float(current["mom_12_1"]) if pd.notna(current["mom_12_1"]) else float("nan"),
        "sma200_gap": float(current["sma200_gap"]) if pd.notna(current["sma200_gap"]) else float("nan"),
        "vol_63": float(current["vol_63"]) if pd.notna(current["vol_63"]) else float("nan"),
        "maxdd_126": maxdd_126,
        "breakout_252": float(current["breakout_252"]) if pd.notna(current["breakout_252"]) else float("nan"),
    }


def build_offensive_raw_snapshots(
    universe_history: pd.DataFrame,
    feature_history_by_symbol: Mapping[str, pd.DataFrame],
    rebalance_dates: Iterable[pd.Timestamp],
) -> dict[pd.Timestamp, pd.DataFrame]:
    snapshots: dict[pd.Timestamp, pd.DataFrame] = {}
    required_benchmark_symbols = ("SPY", "QQQ", "XLK", "SMH")
    for rebalance_date in sorted(pd.Timestamp(date).normalize() for date in rebalance_dates):
        active_universe = resolve_active_universe(universe_history, rebalance_date)
        sector_map = dict(zip(active_universe["symbol"], active_universe["sector"]))
        symbols = active_universe["symbol"].astype(str).str.upper().tolist()
        for extra in required_benchmark_symbols:
            if extra not in symbols:
                symbols.append(extra)
        rows = [
            lookup_symbol_features(
                symbol,
                rebalance_date,
                feature_history_by_symbol,
                sector=sector_map.get(symbol, "benchmark" if symbol in required_benchmark_symbols else "unknown"),
            )
            for symbol in symbols
        ]
        frame = pd.DataFrame(rows).sort_values("symbol").reset_index(drop=True)
        frame["base_eligible"] = (
            ~frame["symbol"].isin(required_benchmark_symbols + (SAFE_HAVEN,))
            & frame["history_days"].ge(252)
            & frame["close"].gt(10.0)
            & frame["adv20_usd"].ge(20_000_000.0)
            & frame[["mom_6_1", "mom_12_1", "sma200_gap", "vol_63", "maxdd_126", "breakout_252"]].notna().all(axis=1)
        )
        snapshots[rebalance_date] = frame
    return snapshots


def select_offensive_universe(frame: pd.DataFrame, config: OffensiveConfig) -> pd.DataFrame:
    eligible = frame.loc[frame["base_eligible"]].copy()
    eligible = eligible.loc[eligible["adv20_usd"] >= float(config.universe_filter.min_adv20_usd)].copy()
    if eligible.empty:
        return eligible

    if config.universe_filter.leadership_only:
        momentum_cut = float(eligible["mom_12_1"].median())
        eligible = eligible.loc[
            (eligible["mom_12_1"] >= momentum_cut)
            & (eligible["sma200_gap"] > 0)
        ].copy()
    return eligible


def select_breadth_ratio(frame: pd.DataFrame, eligible: pd.DataFrame, config: OffensiveConfig) -> float:
    if config.regime.breadth_mode == "broad":
        return float((eligible["sma200_gap"] > 0).mean()) if not eligible.empty else 0.0
    if config.regime.breadth_mode == "sector_etf":
        etf_rows = frame.loc[frame["symbol"].isin(config.regime.breadth_symbols)].copy()
        if etf_rows.empty:
            return 0.0
        return float((etf_rows["sma200_gap"] > 0).mean())
    raise ValueError(f"Unsupported breadth_mode: {config.regime.breadth_mode}")


def build_offensive_target_weights(
    raw_snapshot: pd.DataFrame,
    current_holdings: set[str],
    config: OffensiveConfig,
) -> tuple[dict[str, float], dict[str, object]]:
    frame = raw_snapshot.copy()
    benchmark_symbol = str(config.regime.benchmark_symbol).strip().upper()
    benchmark_rows = frame.loc[frame["symbol"] == benchmark_symbol]
    benchmark_trend_positive = bool(
        (not benchmark_rows.empty)
        and pd.notna(benchmark_rows.iloc[-1]["sma200_gap"])
        and float(benchmark_rows.iloc[-1]["sma200_gap"]) > 0
    )

    eligible = select_offensive_universe(frame, config)
    breadth_ratio = select_breadth_ratio(frame, eligible, config)

    if (not benchmark_trend_positive) and breadth_ratio < 0.35:
        regime = "hard_defense"
        stock_exposure = float(config.exposures.hard_defense_exposure)
    elif (not benchmark_trend_positive) or breadth_ratio < 0.55:
        regime = "soft_defense"
        stock_exposure = float(config.exposures.soft_defense_exposure)
    else:
        regime = "risk_on"
        stock_exposure = 1.0

    if eligible.empty or stock_exposure <= 0:
        return (
            {SAFE_HAVEN: 1.0},
            {
                "benchmark_symbol": benchmark_symbol,
                "benchmark_trend_positive": benchmark_trend_positive,
                "breadth_ratio": breadth_ratio,
                "regime": regime,
                "stock_exposure": 0.0,
                "selected_symbols": (),
                "candidate_count": int(len(eligible)),
                "universe_filter": config.universe_filter.name,
                "group_normalization": config.group_normalization,
                "residual_proxy": "simple_excess_return_vs_QQQ",
            },
        )

    qqq_row = frame.loc[frame["symbol"] == "QQQ"]
    if qqq_row.empty:
        raise RuntimeError("QQQ row missing from offensive research snapshot")
    qqq_mom_6_1 = float(qqq_row.iloc[-1]["mom_6_1"])
    qqq_mom_12_1 = float(qqq_row.iloc[-1]["mom_12_1"])

    scored = eligible.copy()
    scored["resid_mom_6_1"] = scored["mom_6_1"] - qqq_mom_6_1
    scored["resid_mom_12_1"] = scored["mom_12_1"] - qqq_mom_12_1
    scored["drawdown_abs"] = scored["maxdd_126"].abs()
    scored["rel_strength_vs_group"] = scored["mom_12_1"] - scored.groupby("sector")["mom_12_1"].transform("median")

    group_column = "sector" if config.group_normalization == "sector" else None
    if group_column is None:
        raise ValueError(f"Unsupported group normalization: {config.group_normalization}")

    for column in (
        "resid_mom_6_1",
        "resid_mom_12_1",
        "sma200_gap",
        "breakout_252",
        "rel_strength_vs_group",
        "vol_63",
        "drawdown_abs",
    ):
        scored[f"z_{column}"] = scored.groupby(group_column)[column].transform(zscore)

    scored["score"] = (
        (scored["z_resid_mom_6_1"] * 0.30)
        + (scored["z_resid_mom_12_1"] * 0.25)
        + (scored["z_sma200_gap"] * 0.15)
        + (scored["z_breakout_252"] * 0.10)
        + (scored["z_rel_strength_vs_group"] * 0.10)
        - (scored["z_vol_63"] * 0.05)
        - (scored["z_drawdown_abs"] * 0.05)
    )
    scored.loc[scored["symbol"].isin(current_holdings), "score"] += float(config.hold_bonus)

    ranked = scored.sort_values(
        by=["score", "resid_mom_12_1", "resid_mom_6_1", "symbol"],
        ascending=[False, False, False, True],
    )

    per_name_target = stock_exposure / config.holdings_count
    if per_name_target <= 0:
        sector_slot_cap = config.holdings_count
    else:
        sector_slot_cap = max(1, int(math.floor(config.sector_cap / per_name_target)))

    selected_rows = []
    sector_counts: dict[str, int] = {}
    for row in ranked.itertuples(index=False):
        sector = str(row.sector)
        if sector_counts.get(sector, 0) >= sector_slot_cap:
            continue
        selected_rows.append(row._asdict())
        sector_counts[sector] = sector_counts.get(sector, 0) + 1
        if len(selected_rows) >= config.holdings_count:
            break

    selected = pd.DataFrame(selected_rows)
    if selected.empty:
        return (
            {SAFE_HAVEN: 1.0},
            {
                "benchmark_symbol": benchmark_symbol,
                "benchmark_trend_positive": benchmark_trend_positive,
                "breadth_ratio": breadth_ratio,
                "regime": regime,
                "stock_exposure": 0.0,
                "selected_symbols": (),
                "candidate_count": int(len(scored)),
                "universe_filter": config.universe_filter.name,
                "group_normalization": config.group_normalization,
                "residual_proxy": "simple_excess_return_vs_QQQ",
            },
        )

    per_name_weight = min(config.single_name_cap, stock_exposure / len(selected))
    invested_weight = per_name_weight * len(selected)
    weights = {row.symbol: per_name_weight for row in selected.itertuples(index=False)}
    if invested_weight < 1.0:
        weights[SAFE_HAVEN] = 1.0 - invested_weight

    metadata = {
        "benchmark_symbol": benchmark_symbol,
        "benchmark_trend_positive": benchmark_trend_positive,
        "breadth_ratio": breadth_ratio,
        "regime": regime,
        "stock_exposure": stock_exposure,
        "selected_symbols": tuple(selected["symbol"].tolist()),
        "candidate_count": int(len(scored)),
        "sector_slot_cap": sector_slot_cap,
        "universe_filter": config.universe_filter.name,
        "group_normalization": config.group_normalization,
        "residual_proxy": "simple_excess_return_vs_QQQ",
    }
    return weights, metadata


def run_offensive_backtest(
    raw_snapshots: Mapping[pd.Timestamp, pd.DataFrame],
    returns_matrix: pd.DataFrame,
    config: OffensiveConfig,
) -> tuple[pd.Series, pd.DataFrame, pd.Series, dict[str, object]]:
    index = returns_matrix.index
    rebalance_dates = set(raw_snapshots)
    weights_history = pd.DataFrame(0.0, index=index, columns=sorted(set(returns_matrix.columns) | {SAFE_HAVEN}))
    portfolio_returns = pd.Series(0.0, index=index, name=config.name)
    turnover_history = pd.Series(0.0, index=index, name="turnover")
    current_weights: dict[str, float] = {SAFE_HAVEN: 1.0}
    current_holdings: set[str] = set()
    last_metadata: dict[str, object] = {}

    for idx in range(len(index) - 1):
        date = index[idx]
        next_date = index[idx + 1]

        if date in rebalance_dates:
            target_weights, metadata = build_offensive_target_weights(raw_snapshots[date], current_holdings, config)
            turnover_history.at[next_date] = compute_turnover(current_weights, target_weights)
            current_weights = target_weights
            current_holdings = {symbol for symbol, weight in current_weights.items() if weight > 1e-12 and symbol != SAFE_HAVEN}
            last_metadata = metadata

        for symbol, weight in current_weights.items():
            if symbol not in weights_history.columns:
                weights_history[symbol] = 0.0
            weights_history.at[date, symbol] = weight

        next_returns = returns_matrix.loc[next_date].fillna(0.0)
        portfolio_returns.at[next_date] = sum(weight * float(next_returns.get(symbol, 0.0)) for symbol, weight in current_weights.items())

    for symbol, weight in current_weights.items():
        if symbol not in weights_history.columns:
            weights_history[symbol] = 0.0
        weights_history.at[index[-1], symbol] = weight

    return portfolio_returns, weights_history.loc[:, (weights_history != 0.0).any(axis=0)], turnover_history, last_metadata


def build_offensive_ablation_configs() -> list[OffensiveConfig]:
    universe_filters = {
        "full_eligible": UniverseFilterConfig("full_eligible", 20_000_000.0, leadership_only=False),
        "liquid_50m": UniverseFilterConfig("liquid_50m", 50_000_000.0, leadership_only=False),
        "leadership_liquid": UniverseFilterConfig("leadership_liquid", 50_000_000.0, leadership_only=True),
    }
    regimes = {
        "spy_breadth": RegimeConfig("spy_breadth", "SPY", "broad"),
        "qqq_breadth": RegimeConfig("qqq_breadth", "QQQ", "broad"),
        "qqq_xlk_smh_breadth": RegimeConfig("qqq_xlk_smh_breadth", "QQQ", "sector_etf", ("XLK", "SMH")),
    }
    exposures = {
        "100_50_10": ExposureConfig("100_50_10", 0.50, 0.10),
        "100_60_0": ExposureConfig("100_60_0", 0.60, 0.00),
        "100_70_20": ExposureConfig("100_70_20", 0.70, 0.20),
    }

    configs: list[OffensiveConfig] = []
    base = OffensiveConfig(
        name="base_candidate",
        universe_filter=universe_filters["leadership_liquid"],
        holdings_count=16,
        single_name_cap=0.08,
        sector_cap=0.30,
        regime=regimes["qqq_xlk_smh_breadth"],
        exposures=exposures["100_60_0"],
    )
    configs.append(base)

    for universe_filter in universe_filters.values():
        configs.append(
            OffensiveConfig(
                name=f"universe_{universe_filter.name}",
                universe_filter=universe_filter,
                holdings_count=base.holdings_count,
                single_name_cap=base.single_name_cap,
                sector_cap=base.sector_cap,
                regime=base.regime,
                exposures=base.exposures,
            )
        )

    for holdings_count in (12, 16, 24):
        for single_name_cap in (0.06, 0.08, 0.10):
            for sector_cap in (0.20, 0.30, 0.40):
                configs.append(
                    OffensiveConfig(
                        name=f"struct_h{holdings_count}_cap{int(single_name_cap * 100)}_sector{int(sector_cap * 100)}",
                        universe_filter=base.universe_filter,
                        holdings_count=holdings_count,
                        single_name_cap=single_name_cap,
                        sector_cap=sector_cap,
                        regime=base.regime,
                        exposures=base.exposures,
                    )
                )

    for regime in regimes.values():
        configs.append(
            OffensiveConfig(
                name=f"regime_{regime.name}",
                universe_filter=base.universe_filter,
                holdings_count=base.holdings_count,
                single_name_cap=base.single_name_cap,
                sector_cap=base.sector_cap,
                regime=regime,
                exposures=base.exposures,
            )
        )

    for exposure in exposures.values():
        configs.append(
            OffensiveConfig(
                name=f"exposure_{exposure.name}",
                universe_filter=base.universe_filter,
                holdings_count=base.holdings_count,
                single_name_cap=base.single_name_cap,
                sector_cap=base.sector_cap,
                regime=base.regime,
                exposures=exposure,
            )
        )

    deduped: dict[str, OffensiveConfig] = {}
    for config in configs:
        deduped[config.name] = config
    return list(deduped.values())


def select_best_offensive_candidate(ablation_summary: pd.DataFrame) -> OffensiveConfig:
    full_rows = ablation_summary.loc[
        (ablation_summary["period"] == "Full Sample")
        & (ablation_summary["cost_bps_one_way"] == 5.0)
    ].copy()
    if full_rows.empty:
        raise RuntimeError("No offensive ablation rows available to select a best candidate")

    full_rows = full_rows.sort_values(
        by=[
            "Information Ratio vs QQQ",
            "CAGR",
            "2023+ CAGR",
            "2022 Return",
        ],
        ascending=[False, False, False, False],
    )
    best_name = str(full_rows.iloc[0]["strategy"])
    config_map = {config.name: config for config in build_offensive_ablation_configs()}
    if best_name not in config_map:
        raise RuntimeError(f"Best offensive candidate {best_name} not found in config map")
    return config_map[best_name]


def build_strategy_runs(
    universe_history: pd.DataFrame,
    stock_price_history: pd.DataFrame,
    etf_frames: Mapping[str, pd.DataFrame],
    *,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    full_account_equity: float,
    normalized_account_equity: float,
) -> tuple[list[StrategyRun], pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    master_index = build_master_index(stock_price_history, etf_frames["close"])
    master_index = master_index[(master_index >= start_date) & (master_index <= end_date)]
    if master_index.empty:
        raise RuntimeError("No common comparison dates remain inside the selected window")

    extra_stock_prices = build_extra_etf_price_history(etf_frames, symbols=("QQQ", "XLK", "SMH"))
    merged_stock_prices = normalize_long_price_history(pd.concat([stock_price_history, extra_stock_prices], ignore_index=True))
    stock_returns_matrix = build_asset_return_matrix(merged_stock_prices, master_index=master_index, required_symbols=(SAFE_HAVEN, "SPY", "QQQ", "XLK", "SMH"))[1]

    etf_rows = build_extra_etf_price_history(etf_frames, symbols=COMPARISON_ETFS)
    etf_close_matrix, etf_returns_matrix = build_asset_return_matrix(etf_rows, master_index=master_index, required_symbols=(*COMPARISON_ETFS, SAFE_HAVEN))
    qqq_benchmark_returns = etf_returns_matrix["QQQ"].copy()

    defensive_result_0 = run_defensive_backtest(
        stock_price_history,
        universe_history,
        start_date=str(start_date.date()),
        end_date=str(end_date.date()),
        turnover_cost_bps=0.0,
    )
    defensive_weights = defensive_result_0["weights_history"].reindex(master_index).fillna(0.0)
    defensive_turnover = defensive_result_0["turnover_history"].reindex(master_index).fillna(0.0)
    defensive_returns = defensive_result_0["portfolio_returns"].reindex(master_index).fillna(0.0)
    defensive_run = StrategyRun(
        strategy_name=DEFENSIVE_NAME,
        display_name="defensive_baseline",
        comparison_layer=FULL_COMPARISON_LAYER,
        gross_returns=defensive_returns,
        weights_history=defensive_weights,
        turnover_history=defensive_turnover,
        metadata={
            "research_group": "baseline",
            "group_normalization": "sector",
            "data_assumption": "official_monthly_v2_alias",
        },
    )

    qqq_ohlc = pd.DataFrame(
        {
            "open": etf_frames["open"]["QQQ"],
            "high": etf_frames["high"]["QQQ"],
            "low": etf_frames["low"]["QQQ"],
            "close": etf_frames["close"]["QQQ"],
        }
    ).reindex(master_index)
    soxl_close = etf_frames["close"]["SOXL"].reindex(master_index)

    hybrid_full_returns, hybrid_full_weights, hybrid_full_turnover = run_hybrid_growth_income_backtest(
        qqq_ohlc,
        etf_returns_matrix,
        starting_equity=full_account_equity,
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
    hybrid_normalized_returns, hybrid_normalized_weights, hybrid_normalized_turnover = run_hybrid_growth_income_backtest(
        qqq_ohlc,
        etf_returns_matrix,
        starting_equity=normalized_account_equity,
        income_threshold_usd=1_000_000_000.0,
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

    semiconductor_full_returns, semiconductor_full_weights, semiconductor_full_turnover = run_semiconductor_rotation_income_backtest(
        soxl_close,
        etf_returns_matrix,
        starting_equity=full_account_equity,
        trend_ma_window=150,
        cash_reserve_ratio=0.03,
        min_trade_ratio=0.01,
        min_trade_floor=100.0,
        rebalance_threshold_ratio=0.01,
        small_account_deploy_ratio=0.60,
        mid_account_deploy_ratio=0.57,
        large_account_deploy_ratio=0.50,
        trade_layer_decay_coeff=0.04,
        income_layer_start_usd=150_000.0,
        income_layer_max_ratio=0.15,
        income_layer_qqqi_weight=0.70,
        income_layer_spyi_weight=0.30,
    )
    semiconductor_normalized_returns, semiconductor_normalized_weights, semiconductor_normalized_turnover = run_semiconductor_rotation_income_backtest(
        soxl_close,
        etf_returns_matrix,
        starting_equity=normalized_account_equity,
        trend_ma_window=150,
        cash_reserve_ratio=0.03,
        min_trade_ratio=0.01,
        min_trade_floor=100.0,
        rebalance_threshold_ratio=0.01,
        small_account_deploy_ratio=0.60,
        mid_account_deploy_ratio=0.57,
        large_account_deploy_ratio=0.50,
        trade_layer_decay_coeff=0.04,
        income_layer_start_usd=1_000_000_000.0,
        income_layer_max_ratio=0.15,
        income_layer_qqqi_weight=0.70,
        income_layer_spyi_weight=0.30,
    )

    feature_history = precompute_stock_feature_history(merged_stock_prices)
    rebalance_dates = build_monthly_rebalance_dates(master_index)
    raw_snapshots = build_offensive_raw_snapshots(universe_history, feature_history, rebalance_dates)

    offensive_runs: list[StrategyRun] = []
    for config in build_offensive_ablation_configs():
        offensive_returns, offensive_weights, offensive_turnover, offensive_metadata = run_offensive_backtest(
            raw_snapshots,
            stock_returns_matrix,
            config,
        )
        offensive_runs.append(
            StrategyRun(
                strategy_name=config.name,
                display_name=config.name,
                comparison_layer=RESEARCH_LAYER,
                gross_returns=offensive_returns,
                weights_history=offensive_weights.reindex(master_index).fillna(0.0),
                turnover_history=offensive_turnover.reindex(master_index).fillna(0.0),
                metadata={
                    "research_group": "offensive_ablation",
                    "universe_filter": config.universe_filter.name,
                    "holdings_count": config.holdings_count,
                    "single_name_cap": config.single_name_cap,
                    "sector_cap": config.sector_cap,
                    "regime_name": config.regime.name,
                    "benchmark_symbol": config.regime.benchmark_symbol,
                    "breadth_mode": config.regime.breadth_mode,
                    "soft_defense_exposure": config.exposures.soft_defense_exposure,
                    "hard_defense_exposure": config.exposures.hard_defense_exposure,
                    "hold_bonus": config.hold_bonus,
                    "group_normalization": config.group_normalization,
                    "residual_proxy": offensive_metadata.get("residual_proxy", "simple_excess_return_vs_QQQ"),
                },
            )
        )

    ablation_summary = build_period_summary_rows(
        offensive_runs,
        qqq_benchmark_returns,
        costs_bps=DEFAULT_COSTS_BPS,
        periods=ABLATION_PERIODS,
    )
    best_offensive_config = select_best_offensive_candidate(ablation_summary)
    best_offensive_run = next(run for run in offensive_runs if run.strategy_name == best_offensive_config.name)
    best_offensive_run = StrategyRun(
        strategy_name=OFFENSIVE_NAME,
        display_name=f"{OFFENSIVE_NAME}::{best_offensive_config.name}",
        comparison_layer=FULL_COMPARISON_LAYER,
        gross_returns=best_offensive_run.gross_returns,
        weights_history=best_offensive_run.weights_history,
        turnover_history=best_offensive_run.turnover_history,
        metadata={
            **best_offensive_run.metadata,
            "research_group": "best_offensive_candidate",
            "selected_candidate_name": best_offensive_config.name,
        },
    )
    best_offensive_normalized_run = StrategyRun(
        strategy_name=OFFENSIVE_NAME,
        display_name=f"{OFFENSIVE_NAME}::{best_offensive_config.name}",
        comparison_layer=NORMALIZED_COMPARISON_LAYER,
        gross_returns=best_offensive_run.gross_returns,
        weights_history=best_offensive_run.weights_history,
        turnover_history=best_offensive_run.turnover_history,
        metadata={
            **best_offensive_run.metadata,
            "research_group": "best_offensive_candidate",
            "selected_candidate_name": best_offensive_config.name,
        },
    )

    strategy_runs = [
        defensive_run,
        StrategyRun(
            strategy_name=HYBRID_FULL_NAME,
            display_name="hybrid_growth_income::full",
            comparison_layer=FULL_COMPARISON_LAYER,
            gross_returns=hybrid_full_returns,
            weights_history=hybrid_full_weights,
            turnover_history=hybrid_full_turnover,
            metadata={"research_group": "etf_full", "starting_equity": full_account_equity, "income_layer": "on"},
        ),
        StrategyRun(
            strategy_name=SEMICONDUCTOR_FULL_NAME,
            display_name="semiconductor_rotation_income::full",
            comparison_layer=FULL_COMPARISON_LAYER,
            gross_returns=semiconductor_full_returns,
            weights_history=semiconductor_full_weights,
            turnover_history=semiconductor_full_turnover,
            metadata={"research_group": "etf_full", "starting_equity": full_account_equity, "income_layer": "on"},
        ),
        best_offensive_run,
        StrategyRun(
            strategy_name=DEFENSIVE_NAME,
            display_name="defensive_baseline",
            comparison_layer=NORMALIZED_COMPARISON_LAYER,
            gross_returns=defensive_returns,
            weights_history=defensive_weights,
            turnover_history=defensive_turnover,
            metadata={"research_group": "baseline", "group_normalization": "sector", "data_assumption": "official_monthly_v2_alias"},
        ),
        StrategyRun(
            strategy_name=HYBRID_NORMALIZED_NAME,
            display_name="hybrid_growth_income::no_income",
            comparison_layer=NORMALIZED_COMPARISON_LAYER,
            gross_returns=hybrid_normalized_returns,
            weights_history=hybrid_normalized_weights,
            turnover_history=hybrid_normalized_turnover,
            metadata={"research_group": "etf_normalized", "starting_equity": normalized_account_equity, "income_layer": "off"},
        ),
        StrategyRun(
            strategy_name=SEMICONDUCTOR_NORMALIZED_NAME,
            display_name="semiconductor_rotation_income::no_income",
            comparison_layer=NORMALIZED_COMPARISON_LAYER,
            gross_returns=semiconductor_normalized_returns,
            weights_history=semiconductor_normalized_weights,
            turnover_history=semiconductor_normalized_turnover,
            metadata={"research_group": "etf_normalized", "starting_equity": normalized_account_equity, "income_layer": "off"},
        ),
        best_offensive_normalized_run,
    ]
    return strategy_runs, ablation_summary, etf_returns_matrix, etf_close_matrix


def write_results(
    results_dir: Path,
    strategy_runs: list[StrategyRun],
    comparison_summary: pd.DataFrame,
    ablation_summary: pd.DataFrame,
    benchmark_returns: pd.Series,
) -> None:
    results_dir.mkdir(parents=True, exist_ok=True)
    comparison_path = results_dir / "stock_strategy_comparison.csv"
    ablation_path = results_dir / "stock_strategy_ablations.csv"
    equity_curve_path = results_dir / "stock_strategy_equity_curves.csv"
    rolling_alpha_path = results_dir / "stock_strategy_rolling_36m_alpha_vs_qqq.csv"
    markdown_path = results_dir / "stock_strategy_comparison.md"

    comparison_summary.to_csv(comparison_path, index=False)
    ablation_summary.to_csv(ablation_path, index=False)

    equity_rows = []
    rolling_alpha_frames = []
    for strategy_run in strategy_runs:
        if strategy_run.comparison_layer not in {FULL_COMPARISON_LAYER, NORMALIZED_COMPARISON_LAYER}:
            continue
        net_returns = strategy_run.gross_returns - strategy_run.turnover_history.reindex(strategy_run.gross_returns.index).fillna(0.0) * (5.0 / 10_000.0)
        equity = (1.0 + net_returns).cumprod()
        for as_of, value in equity.items():
            equity_rows.append(
                {
                    "comparison_layer": strategy_run.comparison_layer,
                    "strategy": strategy_run.strategy_name,
                    "display_name": strategy_run.display_name,
                    "as_of": as_of,
                    "equity_cost_5bps": float(value),
                }
            )
        rolling_alpha = compute_rolling_36m_capm_alpha(net_returns, benchmark_returns)
        rolling_alpha_frames.append(
            pd.DataFrame(
                {
                    "comparison_layer": strategy_run.comparison_layer,
                    "strategy": strategy_run.strategy_name,
                    "display_name": strategy_run.display_name,
                    "as_of": rolling_alpha.index,
                    "rolling_36m_capm_alpha_vs_qqq": rolling_alpha.values,
                }
            )
        )

    pd.DataFrame(equity_rows).to_csv(equity_curve_path, index=False)
    pd.concat(rolling_alpha_frames, ignore_index=True).to_csv(rolling_alpha_path, index=False)

    full_cost5 = comparison_summary.loc[
        (comparison_summary["cost_bps_one_way"] == 5.0)
        & (comparison_summary["period"] == "Full Sample")
    ].copy()
    full_table = full_cost5.loc[full_cost5["comparison_layer"] == FULL_COMPARISON_LAYER].copy()
    normalized_table = full_cost5.loc[full_cost5["comparison_layer"] == NORMALIZED_COMPARISON_LAYER].copy()
    offensive_rows = ablation_summary.loc[
        (ablation_summary["cost_bps_one_way"] == 5.0)
        & (ablation_summary["period"] == "Full Sample")
    ].sort_values(
        by=["Information Ratio vs QQQ", "CAGR", "2023+ CAGR"],
        ascending=[False, False, False],
    )

    def dataframe_to_markdown(frame: pd.DataFrame) -> str:
        if frame.empty:
            return "_empty_"

        def format_value(value) -> str:
            if pd.isna(value):
                return ""
            if isinstance(value, (float, np.floating)):
                return f"{float(value):.6f}"
            return str(value)

        columns = [str(column) for column in frame.columns]
        lines = [
            "| " + " | ".join(columns) + " |",
            "| " + " | ".join(["---"] * len(columns)) + " |",
        ]
        for row in frame.itertuples(index=False, name=None):
            lines.append("| " + " | ".join(format_value(value) for value in row) + " |")
        return "\n".join(lines)

    markdown = [
        "# Stock strategy comparison research",
        "",
        "## Full-strategy comparison (5 bps one-way)",
        dataframe_to_markdown(
            full_table[
                [
                    "display_name",
                    "CAGR",
                    "Total Return",
                    "Max Drawdown",
                    "Sharpe",
                    "Sortino",
                    "Calmar",
                    "Turnover/Year",
                    "Average Names Held",
                    "Beta vs QQQ",
                    "Information Ratio vs QQQ",
                    "2022 Return",
                    "2023+ CAGR",
                ]
            ]
        ),
        "",
        "## Normalized comparison (5 bps one-way)",
        dataframe_to_markdown(
            normalized_table[
                [
                    "display_name",
                    "CAGR",
                    "Total Return",
                    "Max Drawdown",
                    "Sharpe",
                    "Sortino",
                    "Calmar",
                    "Turnover/Year",
                    "Average Names Held",
                    "Beta vs QQQ",
                    "Information Ratio vs QQQ",
                    "2022 Return",
                    "2023+ CAGR",
                ]
            ]
        ),
        "",
        "## Top offensive ablations (5 bps one-way, full sample)",
        dataframe_to_markdown(
            offensive_rows.head(10)[
                [
                    "strategy",
                    "universe_filter",
                    "holdings_count",
                    "single_name_cap",
                    "sector_cap",
                    "regime_name",
                    "soft_defense_exposure",
                    "hard_defense_exposure",
                    "CAGR",
                    "Sharpe",
                    "Information Ratio vs QQQ",
                    "2022 Return",
                    "2023+ CAGR",
                ]
            ]
        ),
    ]
    markdown_path.write_text("\n".join(markdown), encoding="utf-8")


def build_workspace_mapping(results_dir: Path, data_run_dir: Path) -> dict[str, object]:
    return {
        "workspace_root": str(WORKSPACE_ROOT),
        "defensive_code_entry": str(US_EQUITY_STRATEGIES_ROOT / "src/us_equity_strategies/strategies/russell_1000_multi_factor_defensive.py"),
        "defensive_snapshot_entry": str(US_EQUITY_STRATEGIES_ROOT / "src/us_equity_strategies/snapshots/russell_1000_multi_factor_defensive.py"),
        "defensive_backtest_entry": str(US_EQUITY_STRATEGIES_ROOT / "src/us_equity_strategies/backtests/russell_1000_multi_factor_defensive.py"),
        "hybrid_code_entry": str(US_EQUITY_STRATEGIES_ROOT / "src/us_equity_strategies/strategies/hybrid_growth_income.py"),
        "hybrid_runtime_entry": str(WORKSPACE_ROOT / "CharlesSchwabPlatform/main.py"),
        "semiconductor_code_entry": str(US_EQUITY_STRATEGIES_ROOT / "src/us_equity_strategies/strategies/semiconductor_rotation_income.py"),
        "semiconductor_runtime_entry": str(WORKSPACE_ROOT / "LongBridgePlatform/main.py"),
        "existing_research_entry": str(Path(__file__).resolve().parent / "backtest_qqq_variants.py"),
        "new_research_entry": str(Path(__file__).resolve()),
        "results_dir": str(results_dir),
        "prepared_data_run_dir": str(data_run_dir),
    }


def main() -> None:
    args = parse_args()
    results_dir = Path(args.results_dir).expanduser().resolve()
    data_run_dir = discover_prepared_r1000_run(args.data_run_dir)
    universe_history, stock_price_history, prepared_start, prepared_end = discover_prepared_data(data_run_dir)

    start_date = pd.Timestamp(args.start or prepared_start).normalize()
    end_date = pd.Timestamp(args.end or prepared_end).normalize()

    etf_end = str((end_date + pd.Timedelta(days=1)).date())
    etf_frames = download_etf_ohlcv(COMPARISON_ETFS, start=DEFAULT_ETF_START, end=etf_end)

    strategy_runs, ablation_summary, etf_returns_matrix, _etf_close_matrix = build_strategy_runs(
        universe_history,
        stock_price_history,
        etf_frames,
        start_date=start_date,
        end_date=end_date,
        full_account_equity=float(args.full_account_equity),
        normalized_account_equity=float(args.normalized_account_equity),
    )

    comparison_summary = build_period_summary_rows(
        strategy_runs,
        etf_returns_matrix["QQQ"],
        costs_bps=args.cost_bps,
        periods=FULL_PERIODS,
    )
    write_results(
        results_dir,
        strategy_runs,
        comparison_summary,
        ablation_summary,
        etf_returns_matrix["QQQ"],
    )

    mapping_path = results_dir / "stock_strategy_workspace_mapping.json"
    mapping_path.write_text(
        json.dumps(build_workspace_mapping(results_dir, data_run_dir), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"prepared Russell data: {data_run_dir}")
    print(f"results written to: {results_dir}")
    print(f"comparison csv: {results_dir / 'stock_strategy_comparison.csv'}")
    print(f"ablation csv: {results_dir / 'stock_strategy_ablations.csv'}")


if __name__ == "__main__":
    main()
