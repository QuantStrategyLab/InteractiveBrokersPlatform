#!/usr/bin/env python3
"""
Robustness / promotion-readiness research for qqq_plus_stock_alpha_v1.

Keeps the strategy research-only and focuses on:
- local parameter stability around the current best candidate
- regime robustness
- data-assumption pressure tests
- cost / turnover / holding-profile analysis
- strict out-of-sample parameter selection and evaluation
- benchmark-relative attribution vs QQQ
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import replace
from pathlib import Path
from typing import Iterable, Mapping

import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import backtest_stock_alpha_suite as suite  # noqa: E402

from us_equity_strategies.backtests.russell_1000_multi_factor_defensive import (  # noqa: E402
    build_monthly_rebalance_dates,
    resolve_active_universe,
    run_backtest as run_defensive_backtest,
)


DEFAULT_RESULTS_DIR = SCRIPT_DIR / "results"
DEFAULT_COSTS = (0.0, 5.0, 10.0)
FULL_PERIOD = ("Full Sample", None, None)
REPORT_PERIODS = (
    ("Full Sample", None, None),
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023+", "2023-01-01", None),
)
IS_PERIOD = ("2018-2021", "2018-01-01", "2021-12-31")
OOS_PERIODS = (
    ("OOS 2022-2026", "2022-01-01", None),
    ("OOS 2022", "2022-01-01", "2022-12-31"),
    ("OOS 2023+", "2023-01-01", None),
)

SOFT_BREADTH_THRESHOLD = 0.55
HARD_BREADTH_THRESHOLD = 0.35
ROLLING_ALPHA_WINDOW = 756


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--alias-data-run-dir",
        help="Prepared Russell data run with alias repair (defaults to newest official_monthly_v2_alias run)",
    )
    parser.add_argument(
        "--no-alias-data-run-dir",
        help="Prepared Russell data run without alias repair (defaults to newest official_monthly run)",
    )
    parser.add_argument(
        "--results-dir",
        default=str(DEFAULT_RESULTS_DIR),
        help="Directory where robustness outputs will be written",
    )
    parser.add_argument(
        "--start",
        default=None,
        help="Optional backtest start date override",
    )
    parser.add_argument(
        "--end",
        default=None,
        help="Optional backtest end date override",
    )
    return parser.parse_args()


def discover_run_dir(explicit_path: str | None, pattern: str) -> Path:
    if explicit_path:
        path = Path(explicit_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"data run dir not found: {path}")
        return path

    candidates = sorted(
        suite.LOCAL_RUNS_ROOT.glob(pattern),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"No local run matches pattern: {pattern}")
    return candidates[0]


def build_base_candidate() -> suite.OffensiveConfig:
    return suite.OffensiveConfig(
        name=suite.OFFENSIVE_NAME,
        universe_filter=suite.UniverseFilterConfig("leadership_liquid", 50_000_000.0, leadership_only=True),
        holdings_count=16,
        single_name_cap=0.08,
        sector_cap=0.30,
        regime=suite.RegimeConfig("qqq_breadth", "QQQ", "broad"),
        exposures=suite.ExposureConfig("100_60_0", 0.60, 0.00),
        hold_bonus=0.10,
        group_normalization="sector",
    )


def build_local_parameter_grid(base: suite.OffensiveConfig) -> list[suite.OffensiveConfig]:
    configs: list[suite.OffensiveConfig] = []
    for holdings_count in (12, 16, 20):
        for single_name_cap in (0.07, 0.08, 0.09):
            for sector_cap in (0.25, 0.30, 0.35):
                for hold_bonus in (0.05, 0.10, 0.15):
                    configs.append(
                        replace(
                            base,
                            name=(
                                f"grid_h{holdings_count}_cap{int(single_name_cap * 100)}"
                                f"_sector{int(sector_cap * 100)}_hold{int(hold_bonus * 100):02d}"
                            ),
                            holdings_count=holdings_count,
                            single_name_cap=single_name_cap,
                            sector_cap=sector_cap,
                            hold_bonus=hold_bonus,
                        )
                    )
    return configs


def build_regime_variants(base: suite.OffensiveConfig) -> list[suite.OffensiveConfig]:
    return [
        replace(base, name="regime_spy_breadth", regime=suite.RegimeConfig("spy_breadth", "SPY", "broad")),
        replace(base, name="regime_qqq_breadth", regime=suite.RegimeConfig("qqq_breadth", "QQQ", "broad")),
        replace(
            base,
            name="regime_qqq_xlk_smh_breadth",
            regime=suite.RegimeConfig("qqq_xlk_smh_breadth", "QQQ", "sector_etf", ("XLK", "SMH")),
        ),
    ]


def build_pressure_variants(base: suite.OffensiveConfig) -> list[tuple[str, suite.OffensiveConfig, str, int]]:
    return [
        ("alias_on_baseline", base, "alias_on", 0),
        ("alias_off_no_identifier_repair", replace(base, name="alias_off_no_identifier_repair"), "alias_off", 0),
        ("universe_lag_1_rebalance", replace(base, name="universe_lag_1_rebalance"), "alias_on", 1),
        (
            "leadership_ultra_liquid_100m",
            replace(
                base,
                name="leadership_ultra_liquid_100m",
                universe_filter=suite.UniverseFilterConfig("leadership_ultra_liquid_100m", 100_000_000.0, leadership_only=True),
            ),
            "alias_on",
            0,
        ),
        (
            "normalization_universe",
            replace(base, name="normalization_universe", group_normalization="universe"),
            "alias_on",
            0,
        ),
    ]


def prepare_context(
    data_run_dir: Path,
    *,
    etf_frames: Mapping[str, pd.DataFrame],
    start_date: pd.Timestamp | None,
    end_date: pd.Timestamp | None,
) -> dict[str, object]:
    universe_history, stock_price_history, prepared_start, prepared_end = suite.discover_prepared_data(data_run_dir)
    effective_start = pd.Timestamp(start_date or prepared_start).normalize()
    effective_end = pd.Timestamp(end_date or prepared_end).normalize()

    master_index = suite.build_master_index(stock_price_history, etf_frames["close"])
    master_index = master_index[(master_index >= effective_start) & (master_index <= effective_end)]
    if master_index.empty:
        raise RuntimeError(f"No common dates remain inside {effective_start.date()} -> {effective_end.date()}")

    extra_stock_prices = suite.build_extra_etf_price_history(etf_frames, symbols=("QQQ", "XLK", "SMH"))
    merged_stock_prices = suite.normalize_long_price_history(
        pd.concat([stock_price_history, extra_stock_prices], ignore_index=True)
    )
    _close_matrix, stock_returns_matrix = suite.build_asset_return_matrix(
        merged_stock_prices,
        master_index=master_index,
        required_symbols=(suite.SAFE_HAVEN, "SPY", "QQQ", "XLK", "SMH"),
    )
    feature_history = suite.precompute_stock_feature_history(merged_stock_prices)
    rebalance_dates = sorted(build_monthly_rebalance_dates(master_index))

    return {
        "data_run_dir": data_run_dir,
        "universe_history": universe_history,
        "stock_price_history": stock_price_history,
        "master_index": master_index,
        "stock_returns_matrix": stock_returns_matrix,
        "feature_history": feature_history,
        "rebalance_dates": rebalance_dates,
        "prepared_start": prepared_start,
        "prepared_end": prepared_end,
        "raw_snapshots_cache": {},
    }


def build_raw_snapshots_with_options(
    universe_history: pd.DataFrame,
    feature_history_by_symbol: Mapping[str, pd.DataFrame],
    rebalance_dates: Iterable[pd.Timestamp],
    *,
    universe_lag_rebalances: int = 0,
) -> dict[pd.Timestamp, pd.DataFrame]:
    dates = [pd.Timestamp(date).normalize() for date in rebalance_dates]
    required_benchmark_symbols = ("SPY", "QQQ", "XLK", "SMH")
    snapshots: dict[pd.Timestamp, pd.DataFrame] = {}

    for idx, rebalance_date in enumerate(dates):
        reference_idx = max(0, idx - int(universe_lag_rebalances))
        universe_reference_date = dates[reference_idx]
        active_universe = resolve_active_universe(universe_history, universe_reference_date)
        sector_map = dict(zip(active_universe["symbol"], active_universe["sector"]))
        symbols = active_universe["symbol"].astype(str).str.upper().tolist()
        for extra in required_benchmark_symbols:
            if extra not in symbols:
                symbols.append(extra)
        rows = [
            suite.lookup_symbol_features(
                symbol,
                rebalance_date,
                feature_history_by_symbol,
                sector=sector_map.get(symbol, "benchmark" if symbol in required_benchmark_symbols else "unknown"),
            )
            for symbol in symbols
        ]
        frame = pd.DataFrame(rows).sort_values("symbol").reset_index(drop=True)
        frame["base_eligible"] = (
            ~frame["symbol"].isin(required_benchmark_symbols + (suite.SAFE_HAVEN,))
            & frame["history_days"].ge(252)
            & frame["close"].gt(10.0)
            & frame["adv20_usd"].ge(20_000_000.0)
            & frame[["mom_6_1", "mom_12_1", "sma200_gap", "vol_63", "maxdd_126", "breakout_252"]].notna().all(axis=1)
        )
        frame["universe_reference_date"] = universe_reference_date
        snapshots[rebalance_date] = frame

    return snapshots


def get_raw_snapshots(context: dict[str, object], *, universe_lag_rebalances: int) -> dict[pd.Timestamp, pd.DataFrame]:
    cache: dict[int, dict[pd.Timestamp, pd.DataFrame]] = context["raw_snapshots_cache"]
    if universe_lag_rebalances not in cache:
        cache[universe_lag_rebalances] = build_raw_snapshots_with_options(
            context["universe_history"],
            context["feature_history"],
            context["rebalance_dates"],
            universe_lag_rebalances=universe_lag_rebalances,
        )
    return cache[universe_lag_rebalances]


def _group_zscore(values: pd.Series, group_keys: pd.Series | None) -> pd.Series:
    if group_keys is None:
        return suite.zscore(values)
    return pd.to_numeric(values, errors="coerce").groupby(group_keys).transform(suite.zscore)


def build_offensive_target_weights_robust(
    raw_snapshot: pd.DataFrame,
    current_holdings: set[str],
    config: suite.OffensiveConfig,
) -> tuple[dict[str, float], dict[str, object]]:
    frame = raw_snapshot.copy()
    benchmark_symbol = str(config.regime.benchmark_symbol).strip().upper()
    benchmark_rows = frame.loc[frame["symbol"] == benchmark_symbol]
    benchmark_trend_positive = bool(
        (not benchmark_rows.empty)
        and pd.notna(benchmark_rows.iloc[-1]["sma200_gap"])
        and float(benchmark_rows.iloc[-1]["sma200_gap"]) > 0
    )

    eligible = suite.select_offensive_universe(frame, config)
    breadth_ratio = suite.select_breadth_ratio(frame, eligible, config)
    if (not benchmark_trend_positive) and breadth_ratio < HARD_BREADTH_THRESHOLD:
        regime = "hard_defense"
        stock_exposure = float(config.exposures.hard_defense_exposure)
    elif (not benchmark_trend_positive) or breadth_ratio < SOFT_BREADTH_THRESHOLD:
        regime = "soft_defense"
        stock_exposure = float(config.exposures.soft_defense_exposure)
    else:
        regime = "risk_on"
        stock_exposure = 1.0

    if eligible.empty or stock_exposure <= 0:
        return (
            {suite.SAFE_HAVEN: 1.0},
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
                "universe_reference_date": str(pd.Timestamp(frame["universe_reference_date"].iloc[0]).date()) if "universe_reference_date" in frame.columns else None,
            },
        )

    qqq_row = frame.loc[frame["symbol"] == "QQQ"]
    if qqq_row.empty:
        raise RuntimeError("QQQ row missing from offensive snapshot")
    qqq_mom_6_1 = float(qqq_row.iloc[-1]["mom_6_1"])
    qqq_mom_12_1 = float(qqq_row.iloc[-1]["mom_12_1"])

    scored = eligible.copy()
    scored["resid_mom_6_1"] = scored["mom_6_1"] - qqq_mom_6_1
    scored["resid_mom_12_1"] = scored["mom_12_1"] - qqq_mom_12_1
    scored["drawdown_abs"] = scored["maxdd_126"].abs()
    scored["rel_strength_vs_group"] = scored["mom_12_1"] - scored.groupby("sector")["mom_12_1"].transform("median")

    if config.group_normalization == "sector":
        group_keys = scored["sector"]
    elif config.group_normalization == "universe":
        group_keys = None
    else:
        raise ValueError(f"Unsupported group_normalization: {config.group_normalization}")

    for column in (
        "resid_mom_6_1",
        "resid_mom_12_1",
        "sma200_gap",
        "breakout_252",
        "rel_strength_vs_group",
        "vol_63",
        "drawdown_abs",
    ):
        scored[f"z_{column}"] = _group_zscore(scored[column], group_keys)

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
    sector_slot_cap = (
        config.holdings_count
        if per_name_target <= 0
        else max(1, int(math.floor(config.sector_cap / per_name_target)))
    )

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
            {suite.SAFE_HAVEN: 1.0},
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
                "universe_reference_date": str(pd.Timestamp(frame["universe_reference_date"].iloc[0]).date()) if "universe_reference_date" in frame.columns else None,
            },
        )

    per_name_weight = min(config.single_name_cap, stock_exposure / len(selected))
    invested_weight = per_name_weight * len(selected)
    weights = {row.symbol: per_name_weight for row in selected.itertuples(index=False)}
    if invested_weight < 1.0:
        weights[suite.SAFE_HAVEN] = 1.0 - invested_weight

    metadata = {
        "benchmark_symbol": benchmark_symbol,
        "benchmark_trend_positive": benchmark_trend_positive,
        "breadth_ratio": breadth_ratio,
        "regime": regime,
        "stock_exposure": stock_exposure,
        "selected_symbols": tuple(selected["symbol"].tolist()),
        "selected_sectors": tuple(selected["sector"].tolist()),
        "candidate_count": int(len(scored)),
        "sector_slot_cap": sector_slot_cap,
        "universe_filter": config.universe_filter.name,
        "group_normalization": config.group_normalization,
        "residual_proxy": "simple_excess_return_vs_QQQ",
        "universe_reference_date": str(pd.Timestamp(frame["universe_reference_date"].iloc[0]).date()) if "universe_reference_date" in frame.columns else None,
    }
    return weights, metadata


def run_offensive_backtest_with_history(
    raw_snapshots: Mapping[pd.Timestamp, pd.DataFrame],
    returns_matrix: pd.DataFrame,
    config: suite.OffensiveConfig,
) -> dict[str, object]:
    index = returns_matrix.index
    rebalance_dates = set(raw_snapshots)
    weights_history = pd.DataFrame(0.0, index=index, columns=sorted(set(returns_matrix.columns) | {suite.SAFE_HAVEN}))
    portfolio_returns = pd.Series(0.0, index=index, name=config.name)
    turnover_history = pd.Series(0.0, index=index, name="turnover")
    selection_rows: list[dict[str, object]] = []
    current_weights: dict[str, float] = {suite.SAFE_HAVEN: 1.0}
    current_holdings: set[str] = set()

    for idx in range(len(index) - 1):
        date = index[idx]
        next_date = index[idx + 1]

        if date in rebalance_dates:
            target_weights, metadata = build_offensive_target_weights_robust(raw_snapshots[date], current_holdings, config)
            turnover_history.at[next_date] = suite.compute_turnover(current_weights, target_weights)
            current_weights = target_weights
            current_holdings = {
                symbol for symbol, weight in current_weights.items() if weight > 1e-12 and symbol != suite.SAFE_HAVEN
            }
            selection_rows.append(
                {
                    "rebalance_date": date,
                    "selected_symbols": tuple(metadata.get("selected_symbols", ())),
                    "selected_count": len(metadata.get("selected_symbols", ())),
                    "regime": metadata.get("regime"),
                    "breadth_ratio": metadata.get("breadth_ratio"),
                    "benchmark_symbol": metadata.get("benchmark_symbol"),
                    "benchmark_trend_positive": metadata.get("benchmark_trend_positive"),
                    "candidate_count": metadata.get("candidate_count"),
                    "stock_exposure": metadata.get("stock_exposure"),
                    "universe_reference_date": metadata.get("universe_reference_date"),
                }
            )

        for symbol, weight in current_weights.items():
            if symbol not in weights_history.columns:
                weights_history[symbol] = 0.0
            weights_history.at[date, symbol] = weight

        next_returns = returns_matrix.loc[next_date].fillna(0.0)
        portfolio_returns.at[next_date] = sum(
            weight * float(next_returns.get(symbol, 0.0))
            for symbol, weight in current_weights.items()
        )

    for symbol, weight in current_weights.items():
        if symbol not in weights_history.columns:
            weights_history[symbol] = 0.0
        weights_history.at[index[-1], symbol] = weight

    selection_history = pd.DataFrame(selection_rows)
    return {
        "gross_returns": portfolio_returns,
        "weights_history": weights_history.loc[:, (weights_history != 0.0).any(axis=0)],
        "turnover_history": turnover_history,
        "selection_history": selection_history,
    }


def slice_series_or_frame(obj, start: str | None, end: str | None):
    sliced = obj
    if start:
        sliced = sliced.loc[start:]
    if end:
        sliced = sliced.loc[:end]
    return sliced


def compute_relative_stats(strategy_returns: pd.Series, benchmark_returns: pd.Series) -> dict[str, float]:
    aligned = pd.concat(
        [strategy_returns.rename("strategy"), benchmark_returns.rename("benchmark")],
        axis=1,
    ).dropna()
    if aligned.empty:
        return {
            "beta_vs_qqq": float("nan"),
            "alpha_ann_vs_qqq": float("nan"),
            "tracking_error_vs_qqq": float("nan"),
            "information_ratio_vs_qqq": float("nan"),
            "up_capture_vs_qqq": float("nan"),
            "down_capture_vs_qqq": float("nan"),
        }

    beta = suite.compute_beta(aligned["strategy"], aligned["benchmark"])
    active = aligned["strategy"] - aligned["benchmark"]
    tracking_error = float(active.std(ddof=0) * math.sqrt(252)) if len(active) >= 2 else float("nan")
    alpha_ann = float((aligned["strategy"] - beta * aligned["benchmark"]).mean() * 252) if pd.notna(beta) else float("nan")
    up_capture, down_capture = suite.compute_capture_ratios(aligned["strategy"], aligned["benchmark"])
    return {
        "beta_vs_qqq": beta,
        "alpha_ann_vs_qqq": alpha_ann,
        "tracking_error_vs_qqq": tracking_error,
        "information_ratio_vs_qqq": suite.compute_information_ratio(aligned["strategy"], aligned["benchmark"]),
        "up_capture_vs_qqq": up_capture,
        "down_capture_vs_qqq": down_capture,
    }


def compute_rolling_capm_alpha_fast(strategy_returns: pd.Series, benchmark_returns: pd.Series) -> pd.Series:
    aligned = pd.concat(
        [strategy_returns.rename("strategy"), benchmark_returns.rename("benchmark")],
        axis=1,
    ).dropna()
    if aligned.empty:
        return pd.Series(dtype=float, name=strategy_returns.name)

    rolling_mean_strategy = aligned["strategy"].rolling(ROLLING_ALPHA_WINDOW).mean()
    rolling_mean_benchmark = aligned["benchmark"].rolling(ROLLING_ALPHA_WINDOW).mean()
    rolling_cov = aligned["strategy"].rolling(ROLLING_ALPHA_WINDOW).cov(aligned["benchmark"], ddof=0)
    rolling_var = aligned["benchmark"].rolling(ROLLING_ALPHA_WINDOW).var(ddof=0)
    beta = rolling_cov / rolling_var.replace(0.0, np.nan)
    alpha_daily = rolling_mean_strategy - (beta * rolling_mean_benchmark)
    alpha_ann = (alpha_daily * 252.0).dropna()
    alpha_ann.name = strategy_returns.name
    return alpha_ann


def compute_rolling_alpha_summary(rolling_alpha: pd.Series) -> dict[str, float]:
    if rolling_alpha.empty:
        return {
            "rolling_36m_alpha_mean": float("nan"),
            "rolling_36m_alpha_median": float("nan"),
            "rolling_36m_alpha_last": float("nan"),
            "rolling_36m_alpha_positive_ratio": float("nan"),
        }
    return {
        "rolling_36m_alpha_mean": float(rolling_alpha.mean()),
        "rolling_36m_alpha_median": float(rolling_alpha.median()),
        "rolling_36m_alpha_last": float(rolling_alpha.iloc[-1]),
        "rolling_36m_alpha_positive_ratio": float((rolling_alpha > 0).mean()),
    }


def evaluate_period_metrics(
    net_returns: pd.Series,
    weights_history: pd.DataFrame,
    turnover_history: pd.Series,
    benchmark_returns: pd.Series,
    *,
    start: str | None,
    end: str | None,
) -> dict[str, float | str]:
    metrics = suite.summarize_strategy_period(
        net_returns,
        weights_history,
        turnover_history,
        benchmark_returns,
        start=start,
        end=end,
        safe_haven_symbols=(suite.SAFE_HAVEN, suite.HYBRID_SAFE_CASH),
        full_returns_reference=net_returns,
    )
    period_returns = slice_series_or_frame(net_returns, start, end).dropna()
    period_benchmark = slice_series_or_frame(benchmark_returns, start, end).reindex(period_returns.index).fillna(0.0)
    metrics.update(compute_relative_stats(period_returns, period_benchmark))
    return metrics


def compute_turnover_profile(
    selection_history: pd.DataFrame,
    turnover_history: pd.Series,
) -> dict[str, float]:
    turnover = turnover_history.fillna(0.0)
    if turnover.empty:
        return {
            "annual_turnover": float("nan"),
            "average_monthly_turnover": float("nan"),
            "average_names_replaced_per_rebalance": float("nan"),
            "median_holding_duration_days": float("nan"),
            "top5_continuity": float("nan"),
        }

    years = max((turnover.index[-1] - turnover.index[0]).days / 365.25, 1 / 365.25)
    annual_turnover = float(turnover.sum() / years)
    monthly_turnover = turnover.groupby(turnover.index.to_period("M")).sum()
    average_monthly_turnover = float(monthly_turnover.mean()) if not monthly_turnover.empty else float("nan")

    if selection_history.empty or len(selection_history) < 2:
        return {
            "annual_turnover": annual_turnover,
            "average_monthly_turnover": average_monthly_turnover,
            "average_names_replaced_per_rebalance": float("nan"),
            "median_holding_duration_days": float("nan"),
            "top5_continuity": float("nan"),
        }

    replaced_counts: list[float] = []
    top5_overlap_ratios: list[float] = []
    entry_dates: dict[str, pd.Timestamp] = {}
    holding_durations: list[int] = []

    history = selection_history.sort_values("rebalance_date").reset_index(drop=True)
    previous_symbols: set[str] | None = None
    previous_top5: tuple[str, ...] | None = None

    for row in history.itertuples(index=False):
        date = pd.Timestamp(row.rebalance_date).normalize()
        symbols = set(row.selected_symbols or ())
        top5 = tuple((row.selected_symbols or ())[:5])
        if previous_symbols is not None:
            replaced_counts.append(len(previous_symbols ^ symbols) / 2.0)
        if previous_top5 is not None and previous_top5:
            top5_overlap_ratios.append(len(set(previous_top5) & set(top5)) / max(len(previous_top5), 1))

        for symbol in symbols - set(entry_dates):
            entry_dates[symbol] = date
        for symbol in list(entry_dates):
            if symbol not in symbols:
                holding_durations.append((date - entry_dates.pop(symbol)).days)

        previous_symbols = symbols
        previous_top5 = top5

    final_date = pd.Timestamp(history.iloc[-1]["rebalance_date"]).normalize()
    for symbol, entry_date in entry_dates.items():
        holding_durations.append((final_date - entry_date).days)

    return {
        "annual_turnover": annual_turnover,
        "average_monthly_turnover": average_monthly_turnover,
        "average_names_replaced_per_rebalance": float(np.mean(replaced_counts)) if replaced_counts else float("nan"),
        "median_holding_duration_days": float(np.median(holding_durations)) if holding_durations else float("nan"),
        "top5_continuity": float(np.mean(top5_overlap_ratios)) if top5_overlap_ratios else float("nan"),
    }


def evaluate_scenario(
    scenario_label: str,
    config: suite.OffensiveConfig,
    context: dict[str, object],
    *,
    experiment_group: str,
    cost_bps: float,
    universe_lag_rebalances: int = 0,
    data_variant: str = "alias_on",
) -> tuple[list[dict[str, object]], pd.Series, pd.DataFrame, pd.Series, pd.DataFrame, pd.Series]:
    raw_snapshots = get_raw_snapshots(context, universe_lag_rebalances=universe_lag_rebalances)
    result = run_offensive_backtest_with_history(raw_snapshots, context["stock_returns_matrix"], config)
    gross_returns = result["gross_returns"]
    turnover_history = result["turnover_history"].reindex(gross_returns.index).fillna(0.0)
    net_returns = gross_returns - turnover_history * (float(cost_bps) / 10_000.0)
    benchmark_returns = context["stock_returns_matrix"]["QQQ"].reindex(net_returns.index).fillna(0.0)
    rolling_alpha = compute_rolling_capm_alpha_fast(net_returns, benchmark_returns)
    rolling_summary = compute_rolling_alpha_summary(rolling_alpha)

    rows: list[dict[str, object]] = []
    for period_name, start, end in REPORT_PERIODS:
        metrics = evaluate_period_metrics(
            net_returns,
            result["weights_history"],
            result["turnover_history"],
            benchmark_returns,
            start=start,
            end=end,
        )
        rows.append(
            {
                "experiment_group": experiment_group,
                "scenario": scenario_label,
                "cost_bps_one_way": float(cost_bps),
                "period": period_name,
                "data_variant": data_variant,
                "universe_lag_rebalances": universe_lag_rebalances,
                "universe_filter": config.universe_filter.name,
                "holdings_count": config.holdings_count,
                "single_name_cap": config.single_name_cap,
                "sector_cap": config.sector_cap,
                "hold_bonus": config.hold_bonus,
                "regime_name": config.regime.name,
                "benchmark_symbol": config.regime.benchmark_symbol,
                "breadth_mode": config.regime.breadth_mode,
                "soft_defense_exposure": config.exposures.soft_defense_exposure,
                "hard_defense_exposure": config.exposures.hard_defense_exposure,
                "group_normalization": config.group_normalization,
                "residual_proxy": "simple_excess_return_vs_QQQ",
                **metrics,
                **rolling_summary,
            }
        )

    return (
        rows,
        net_returns,
        result["weights_history"],
        result["turnover_history"],
        result["selection_history"],
        rolling_alpha,
    )


def evaluate_qqq_reference(
    benchmark_returns: pd.Series,
    *,
    cost_bps: float,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    weights_history = pd.DataFrame({"QQQ": 1.0}, index=benchmark_returns.index)
    turnover_history = pd.Series(0.0, index=benchmark_returns.index)
    for period_name, start, end in REPORT_PERIODS:
        metrics = suite.summarize_strategy_period(
            benchmark_returns,
            weights_history,
            turnover_history,
            benchmark_returns,
            start=start,
            end=end,
            safe_haven_symbols=(suite.SAFE_HAVEN,),
            full_returns_reference=benchmark_returns,
        )
        relative = compute_relative_stats(
            slice_series_or_frame(benchmark_returns, start, end),
            slice_series_or_frame(benchmark_returns, start, end),
        )
        rows.append(
            {
                "experiment_group": "qqq_reference",
                "scenario": "QQQ",
                "cost_bps_one_way": float(cost_bps),
                "period": period_name,
                "data_variant": "public_yfinance",
                "universe_lag_rebalances": 0,
                "universe_filter": "n/a",
                "holdings_count": 1,
                "single_name_cap": 1.0,
                "sector_cap": 1.0,
                "hold_bonus": 0.0,
                "regime_name": "buy_and_hold",
                "benchmark_symbol": "QQQ",
                "breadth_mode": "n/a",
                "soft_defense_exposure": 1.0,
                "hard_defense_exposure": 1.0,
                "group_normalization": "n/a",
                "residual_proxy": "n/a",
                **metrics,
                **relative,
                "rolling_36m_alpha_mean": 0.0,
                "rolling_36m_alpha_median": 0.0,
                "rolling_36m_alpha_last": 0.0,
                "rolling_36m_alpha_positive_ratio": 0.0,
            }
        )
    return rows


def build_parameter_stability_summary(param_rows: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, float]]:
    full_rows = param_rows.loc[param_rows["period"] == "Full Sample"].copy()
    best_cagr = float(full_rows["CAGR"].max())
    best_ir = float(full_rows["information_ratio_vs_qqq"].max())
    plateau_100bps = full_rows.loc[
        (full_rows["CAGR"] >= best_cagr - 0.01) & (full_rows["information_ratio_vs_qqq"] > 0)
    ]
    plateau_200bps = full_rows.loc[
        (full_rows["CAGR"] >= best_cagr - 0.02) & (full_rows["information_ratio_vs_qqq"] > 0)
    ]

    axis_frames = []
    for axis in ("holdings_count", "single_name_cap", "sector_cap", "hold_bonus"):
        grouped = full_rows.groupby(axis).agg(
            mean_cagr=("CAGR", "mean"),
            median_cagr=("CAGR", "median"),
            std_cagr=("CAGR", "std"),
            mean_ir=("information_ratio_vs_qqq", "mean"),
            mean_2022=("2022 Return", "mean"),
            mean_2023_plus=("2023+ CAGR", "mean"),
        )
        grouped = grouped.reset_index().rename(columns={axis: "axis_value"})
        grouped.insert(0, "axis", axis)
        axis_frames.append(grouped)

    summary = pd.concat(axis_frames, ignore_index=True)
    stability_stats = {
        "best_cagr": best_cagr,
        "best_information_ratio": best_ir,
        "plateau_100bps_count": float(len(plateau_100bps)),
        "plateau_100bps_share": float(len(plateau_100bps) / len(full_rows)) if len(full_rows) else float("nan"),
        "plateau_200bps_count": float(len(plateau_200bps)),
        "plateau_200bps_share": float(len(plateau_200bps) / len(full_rows)) if len(full_rows) else float("nan"),
    }
    return summary, stability_stats


def compute_average_sector_weights(
    weights_history: pd.DataFrame,
    selection_history: pd.DataFrame,
    universe_history: pd.DataFrame,
) -> pd.Series:
    if selection_history.empty:
        return pd.Series(dtype=float)

    rows = []
    for row in selection_history.itertuples(index=False):
        date = pd.Timestamp(row.rebalance_date).normalize()
        active_universe = resolve_active_universe(universe_history, date)
        sector_map = dict(zip(active_universe["symbol"], active_universe["sector"]))
        weight_row = weights_history.loc[date].fillna(0.0)
        sector_weights: dict[str, float] = {}
        for symbol, weight in weight_row.items():
            symbol_text = str(symbol).upper()
            if weight <= 1e-12 or symbol_text == suite.SAFE_HAVEN:
                continue
            sector = sector_map.get(symbol_text)
            if not sector:
                continue
            sector_weights[sector] = sector_weights.get(sector, 0.0) + float(weight)
        rows.append(pd.Series(sector_weights, name=date))

    if not rows:
        return pd.Series(dtype=float)
    return pd.DataFrame(rows).fillna(0.0).mean().sort_values(ascending=False)


def build_walkforward_rows(
    grid_artifacts: dict[str, dict[str, object]],
    base_config: suite.OffensiveConfig,
    base_artifact: dict[str, object],
    defensive_returns: pd.Series,
    defensive_weights: pd.DataFrame,
    defensive_turnover: pd.Series,
    benchmark_returns: pd.Series,
) -> tuple[pd.DataFrame, suite.OffensiveConfig]:
    ranking_rows: list[dict[str, object]] = []
    for name, artifact in grid_artifacts.items():
        is_metrics = evaluate_period_metrics(
            artifact["net_returns"],
            artifact["weights_history"],
            artifact["turnover_history"],
            benchmark_returns,
            start=IS_PERIOD[1],
            end=IS_PERIOD[2],
        )
        ranking_rows.append(
            {
                "stage": "in_sample_ranking",
                "scenario": name,
                "selection_objective": "IR_vs_QQQ_then_CAGR_then_Sharpe",
                **artifact["config_fields"],
                **is_metrics,
            }
        )
    ranking = pd.DataFrame(ranking_rows).sort_values(
        by=["information_ratio_vs_qqq", "CAGR", "Sharpe"],
        ascending=[False, False, False],
    )
    selected_name = str(ranking.iloc[0]["scenario"])
    selected_artifact = grid_artifacts[selected_name]
    selected_config = selected_artifact["config"]

    oos_rows: list[dict[str, object]] = []
    comparators = [
        ("offensive_default_candidate", base_artifact["net_returns"], base_artifact["weights_history"], base_artifact["turnover_history"]),
        ("offensive_is_selected", selected_artifact["net_returns"], selected_artifact["weights_history"], selected_artifact["turnover_history"]),
        ("defensive_baseline", defensive_returns, defensive_weights, defensive_turnover),
        ("QQQ", benchmark_returns, pd.DataFrame({"QQQ": 1.0}, index=benchmark_returns.index), pd.Series(0.0, index=benchmark_returns.index)),
    ]
    for comparator_name, returns, weights, turnover in comparators:
        rolling_alpha = compute_rolling_capm_alpha_fast(
            slice_series_or_frame(returns, OOS_PERIODS[0][1], OOS_PERIODS[0][2]),
            slice_series_or_frame(benchmark_returns, OOS_PERIODS[0][1], OOS_PERIODS[0][2]),
        )
        rolling_summary = compute_rolling_alpha_summary(rolling_alpha)
        for period_name, start, end in OOS_PERIODS:
            metrics = evaluate_period_metrics(
                returns,
                weights,
                turnover,
                benchmark_returns,
                start=start,
                end=end,
            )
            oos_rows.append(
                {
                    "stage": "oos_eval",
                    "scenario": comparator_name,
                    "period": period_name,
                    "selected_from_in_sample": selected_name,
                    "selection_objective": "IR_vs_QQQ_then_CAGR_then_Sharpe",
                    **rolling_summary,
                    **metrics,
                }
            )

    return pd.concat([ranking, pd.DataFrame(oos_rows)], ignore_index=True), selected_config


def write_markdown_report(
    results_dir: Path,
    *,
    base_config: suite.OffensiveConfig,
    parameter_rows: pd.DataFrame,
    stability_axis_summary: pd.DataFrame,
    stability_stats: dict[str, float],
    regime_rows: pd.DataFrame,
    pressure_rows: pd.DataFrame,
    costs_rows: pd.DataFrame,
    walkforward_rows: pd.DataFrame,
    attribution_rows: pd.DataFrame,
    gate: dict[str, object],
) -> None:
    def format_frame(frame: pd.DataFrame) -> str:
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

    parameter_full = parameter_rows.loc[parameter_rows["period"] == "Full Sample"].sort_values(
        by=["information_ratio_vs_qqq", "CAGR", "rolling_36m_alpha_positive_ratio"],
        ascending=[False, False, False],
    )
    regime_full = regime_rows.loc[regime_rows["period"] == "Full Sample"].sort_values(
        by=["information_ratio_vs_qqq", "CAGR"],
        ascending=[False, False],
    )
    pressure_full = pressure_rows.loc[pressure_rows["period"] == "Full Sample"].sort_values(
        by=["information_ratio_vs_qqq", "CAGR"],
        ascending=[False, False],
    )
    costs_full = costs_rows.loc[costs_rows["period"] == "Full Sample"].sort_values("cost_bps_one_way")
    walkforward_oos = walkforward_rows.loc[walkforward_rows["stage"] == "oos_eval"].copy()

    lines = [
        "# qqq_plus_stock_alpha_v1 robustness",
        "",
        "## Base candidate",
        f"- universe={base_config.universe_filter.name}",
        f"- holdings={base_config.holdings_count}",
        f"- single_cap={base_config.single_name_cap:.0%}",
        f"- sector_cap={base_config.sector_cap:.0%}",
        f"- hold_bonus={base_config.hold_bonus:.2f}",
        f"- regime={base_config.regime.name}",
        f"- breadth thresholds: soft={SOFT_BREADTH_THRESHOLD:.0%}, hard={HARD_BREADTH_THRESHOLD:.0%}",
        f"- normalization={base_config.group_normalization}",
        "- residual proxy=simple excess return vs QQQ",
        "",
        "## Parameter stability (5 bps, full sample top 12)",
        format_frame(parameter_full.head(12)[[
            "scenario",
            "holdings_count",
            "single_name_cap",
            "sector_cap",
            "hold_bonus",
            "CAGR",
            "Sharpe",
            "information_ratio_vs_qqq",
            "rolling_36m_alpha_positive_ratio",
            "2022 Return",
            "2023+ CAGR",
        ]]),
        "",
        "## Stability axis summary",
        format_frame(stability_axis_summary),
        "",
        "### Stability platform stats",
        f"- best_full_sample_CAGR={stability_stats['best_cagr']:.4f}",
        f"- best_full_sample_IR_vs_QQQ={stability_stats['best_information_ratio']:.4f}",
        f"- plateau_within_100bps_and_positive_IR={int(stability_stats['plateau_100bps_count'])} ({stability_stats['plateau_100bps_share']:.1%})",
        f"- plateau_within_200bps_and_positive_IR={int(stability_stats['plateau_200bps_count'])} ({stability_stats['plateau_200bps_share']:.1%})",
        "",
        "## Regime robustness (5 bps, full sample)",
        "- spy_breadth: benchmark=SPY, breadth=eligible universe 中 sma200_gap > 0 的比例",
        "- qqq_breadth: benchmark=QQQ, breadth=eligible universe 中 sma200_gap > 0 的比例",
        "- qqq_xlk_smh_breadth: benchmark=QQQ, breadth=(XLK 与 SMH 在 200 日线上方的 ETF 比例)",
        "- ETF data assumption: QQQ / XLK / SMH 均来自 yfinance/Yahoo，2018-01-01 之后可直接取到",
        format_frame(regime_full[[
            "scenario",
            "CAGR",
            "Sharpe",
            "information_ratio_vs_qqq",
            "rolling_36m_alpha_positive_ratio",
            "2022 Return",
            "2023+ CAGR",
        ]]),
        "",
        "## Data pressure tests (5 bps, full sample)",
        format_frame(pressure_full[[
            "scenario",
            "data_variant",
            "universe_lag_rebalances",
            "universe_filter",
            "group_normalization",
            "CAGR",
            "Sharpe",
            "information_ratio_vs_qqq",
            "rolling_36m_alpha_positive_ratio",
            "2022 Return",
            "2023+ CAGR",
        ]]),
        "",
        "## Cost / turnover profile",
        format_frame(costs_full[[
            "cost_bps_one_way",
            "CAGR",
            "Total Return",
            "Max Drawdown",
            "Sharpe",
            "annual_turnover",
            "average_monthly_turnover",
            "average_names_replaced_per_rebalance",
            "median_holding_duration_days",
            "top5_continuity",
        ]]),
        "",
        "## Walk-forward / OOS",
        format_frame(walkforward_oos[[
            "scenario",
            "period",
            "CAGR",
            "Total Return",
            "Max Drawdown",
            "Sharpe",
            "information_ratio_vs_qqq",
            "rolling_36m_alpha_positive_ratio",
        ]]),
        "",
        "## Relative attribution vs QQQ",
        format_frame(attribution_rows[[
            "strategy",
            "beta_vs_qqq",
            "alpha_ann_vs_qqq",
            "tracking_error_vs_qqq",
            "information_ratio_vs_qqq",
            "up_capture_vs_qqq",
            "down_capture_vs_qqq",
            "active_share_vs_qqq",
        ]]),
        "",
        "## Shadow tracking gate",
        f"- recommendation={gate['recommendation']}",
        f"- reason={gate['reason']}",
        f"- oos_positive_rolling_alpha_ratio={gate['oos_positive_rolling_alpha_ratio']:.1%}",
        f"- oos_cagr_minus_qqq_5bps={gate['oos_cagr_minus_qqq_5bps']:.4f}",
        f"- oos_max_drawdown={gate['oos_max_drawdown']:.4f}",
        f"- annual_turnover={gate['annual_turnover']:.4f}",
        f"- plateau_200bps_share={gate['plateau_200bps_share']:.1%}",
        "",
        "### Gate thresholds used",
        "- OOS rolling 36m alpha > 0 的窗口占比 >= 60%",
        "- 5 bps 成本后 OOS CAGR 不低于 QQQ",
        "- OOS MaxDD <= 40%",
        "- annual turnover <= 5.0x",
        "- 参数局部网格里，至少 20% 组合落在 best-200bps 且 IR>0 的平台内",
        "",
        "## Active share vs QQQ note",
        "- 这里无法准确给出真正的 active share vs QQQ，因为当前公开数据链路没有 QQQ 历史 constituent weights。输出里显式记为 NaN。",
    ]
    (results_dir / "stock_alpha_v1_robustness.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = parse_args()
    results_dir = Path(args.results_dir).expanduser().resolve()
    results_dir.mkdir(parents=True, exist_ok=True)

    alias_dir = discover_run_dir(args.alias_data_run_dir, "r1000_multifactor_defensive_*_official_monthly_v2_alias")
    no_alias_dir = discover_run_dir(args.no_alias_data_run_dir, "r1000_multifactor_defensive_*_official_monthly")

    alias_universe, alias_prices, alias_start, alias_end = suite.discover_prepared_data(alias_dir)
    no_alias_universe, no_alias_prices, no_alias_start, no_alias_end = suite.discover_prepared_data(no_alias_dir)
    common_start = pd.Timestamp(args.start or max(alias_start, no_alias_start)).normalize()
    common_end = pd.Timestamp(args.end or min(alias_end, no_alias_end)).normalize()
    etf_frames = suite.download_etf_ohlcv(("QQQ", "SPY", "XLK", "SMH"), start=str(common_start.date()), end=str((common_end + pd.Timedelta(days=1)).date()))

    alias_context = prepare_context(alias_dir, etf_frames=etf_frames, start_date=common_start, end_date=common_end)
    no_alias_context = prepare_context(no_alias_dir, etf_frames=etf_frames, start_date=common_start, end_date=common_end)
    contexts = {"alias_on": alias_context, "alias_off": no_alias_context}
    benchmark_returns = alias_context["stock_returns_matrix"]["QQQ"].copy()

    base_config = build_base_candidate()
    base_rows, base_returns, base_weights, base_turnover, base_selection, base_rolling_alpha = evaluate_scenario(
        base_config.name,
        base_config,
        alias_context,
        experiment_group="base_candidate",
        cost_bps=5.0,
    )
    base_artifact = {
        "config": base_config,
        "net_returns": base_returns,
        "weights_history": base_weights,
        "turnover_history": base_turnover,
        "selection_history": base_selection,
        "rolling_alpha": base_rolling_alpha,
        "config_fields": {
            "universe_filter": base_config.universe_filter.name,
            "holdings_count": base_config.holdings_count,
            "single_name_cap": base_config.single_name_cap,
            "sector_cap": base_config.sector_cap,
            "hold_bonus": base_config.hold_bonus,
            "regime_name": base_config.regime.name,
            "group_normalization": base_config.group_normalization,
        },
    }

    parameter_rows: list[dict[str, object]] = []
    rolling_alpha_rows: list[dict[str, object]] = []
    grid_artifacts: dict[str, dict[str, object]] = {}
    for config in build_local_parameter_grid(base_config):
        rows, net_returns, weights_history, turnover_history, selection_history, rolling_alpha = evaluate_scenario(
            config.name,
            config,
            alias_context,
            experiment_group="parameter_stability",
            cost_bps=5.0,
        )
        parameter_rows.extend(rows)
        grid_artifacts[config.name] = {
            "config": config,
            "net_returns": net_returns,
            "weights_history": weights_history,
            "turnover_history": turnover_history,
            "selection_history": selection_history,
            "rolling_alpha": rolling_alpha,
            "config_fields": {
                "universe_filter": config.universe_filter.name,
                "holdings_count": config.holdings_count,
                "single_name_cap": config.single_name_cap,
                "sector_cap": config.sector_cap,
                "hold_bonus": config.hold_bonus,
                "regime_name": config.regime.name,
                "group_normalization": config.group_normalization,
            },
        }
        for as_of, alpha_value in rolling_alpha.items():
            rolling_alpha_rows.append(
                {
                    "experiment_group": "parameter_stability",
                    "scenario": config.name,
                    "as_of": as_of,
                    "rolling_36m_alpha_vs_qqq": float(alpha_value),
                }
            )

    parameter_rows_df = pd.DataFrame(parameter_rows)
    stability_axis_summary, stability_stats = build_parameter_stability_summary(parameter_rows_df)

    regime_rows: list[dict[str, object]] = []
    for config in build_regime_variants(base_config):
        rows, _ret, _weights, _turnover, _selection, rolling_alpha = evaluate_scenario(
            config.name,
            config,
            alias_context,
            experiment_group="regime_robustness",
            cost_bps=5.0,
        )
        regime_rows.extend(rows)
        for as_of, alpha_value in rolling_alpha.items():
            rolling_alpha_rows.append(
                {
                    "experiment_group": "regime_robustness",
                    "scenario": config.name,
                    "as_of": as_of,
                    "rolling_36m_alpha_vs_qqq": float(alpha_value),
                }
            )
    regime_rows_df = pd.DataFrame(regime_rows)

    pressure_rows: list[dict[str, object]] = []
    for scenario_label, config, context_key, universe_lag in build_pressure_variants(base_config):
        rows, _ret, _weights, _turnover, _selection, rolling_alpha = evaluate_scenario(
            scenario_label,
            config,
            contexts[context_key],
            experiment_group="pressure_test",
            cost_bps=5.0,
            universe_lag_rebalances=universe_lag,
            data_variant=context_key,
        )
        pressure_rows.extend(rows)
        for as_of, alpha_value in rolling_alpha.items():
            rolling_alpha_rows.append(
                {
                    "experiment_group": "pressure_test",
                    "scenario": scenario_label,
                    "as_of": as_of,
                    "rolling_36m_alpha_vs_qqq": float(alpha_value),
                }
            )
    pressure_rows_df = pd.DataFrame(pressure_rows)

    costs_rows: list[dict[str, object]] = []
    turnover_profile = compute_turnover_profile(base_selection, base_turnover)
    for cost_bps in DEFAULT_COSTS:
        rows, _ret, _weights, _turnover, _selection, _rolling_alpha = evaluate_scenario(
            f"{base_config.name}_cost_{int(cost_bps)}bps",
            base_config,
            alias_context,
            experiment_group="cost_sensitivity",
            cost_bps=cost_bps,
        )
        for row in rows:
            row.update(turnover_profile)
            costs_rows.append(row)
    costs_rows_df = pd.DataFrame(costs_rows)

    defensive_result = run_defensive_backtest(
        alias_context["stock_price_history"],
        alias_context["universe_history"],
        start_date=str(common_start.date()),
        end_date=str(common_end.date()),
        turnover_cost_bps=5.0,
    )
    defensive_returns = defensive_result["portfolio_returns"].reindex(alias_context["master_index"]).fillna(0.0)
    defensive_weights = defensive_result["weights_history"].reindex(alias_context["master_index"]).fillna(0.0)
    defensive_turnover = defensive_result["turnover_history"].reindex(alias_context["master_index"]).fillna(0.0)

    walkforward_rows_df, selected_config = build_walkforward_rows(
        grid_artifacts,
        base_config,
        base_artifact,
        defensive_returns,
        defensive_weights,
        defensive_turnover,
        benchmark_returns,
    )

    attribution_rows = []
    defensive_relative = compute_relative_stats(defensive_returns, benchmark_returns)
    defensive_selection_proxy = pd.DataFrame(
        {"rebalance_date": alias_context["rebalance_dates"], "selected_symbols": [tuple()] * len(alias_context["rebalance_dates"])}
    )
    offensive_relative = compute_relative_stats(base_returns, benchmark_returns)
    defensive_sector_weights = compute_average_sector_weights(defensive_weights, defensive_selection_proxy, alias_context["universe_history"])
    offensive_sector_weights = compute_average_sector_weights(base_weights, base_selection, alias_context["universe_history"])

    attribution_rows.append(
        {
            "strategy": "defensive_baseline",
            **defensive_relative,
            "active_share_vs_qqq": float("nan"),
            "avg_sector_weights_json": json.dumps({k: float(v) for k, v in defensive_sector_weights.items()}, ensure_ascii=False),
        }
    )
    attribution_rows.append(
        {
            "strategy": suite.OFFENSIVE_NAME,
            **offensive_relative,
            "active_share_vs_qqq": float("nan"),
            "avg_sector_weights_json": json.dumps({k: float(v) for k, v in offensive_sector_weights.items()}, ensure_ascii=False),
        }
    )
    attribution_rows_df = pd.DataFrame(attribution_rows)

    sector_weights_rows = []
    for strategy_name, sector_weights in (
        ("defensive_baseline", defensive_sector_weights),
        (suite.OFFENSIVE_NAME, offensive_sector_weights),
    ):
        for sector, weight in sector_weights.items():
            sector_weights_rows.append({"strategy": strategy_name, "sector": sector, "avg_weight": float(weight)})
    sector_weights_df = pd.DataFrame(sector_weights_rows)

    qqq_rows_df = pd.DataFrame(evaluate_qqq_reference(benchmark_returns, cost_bps=5.0))

    robustness_rows_df = pd.concat(
        [parameter_rows_df, regime_rows_df, pressure_rows_df, qqq_rows_df],
        ignore_index=True,
    )
    robustness_rows_df.to_csv(results_dir / "stock_alpha_v1_robustness.csv", index=False)
    stability_axis_summary.to_csv(results_dir / "stock_alpha_v1_parameter_stability_summary.csv", index=False)
    costs_rows_df.to_csv(results_dir / "stock_alpha_v1_costs.csv", index=False)
    walkforward_rows_df.to_csv(results_dir / "stock_alpha_v1_walkforward.csv", index=False)
    pd.DataFrame(rolling_alpha_rows).to_csv(results_dir / "stock_alpha_v1_rolling_alpha.csv", index=False)
    attribution_rows_df.to_csv(results_dir / "stock_alpha_v1_attribution.csv", index=False)
    sector_weights_df.to_csv(results_dir / "stock_alpha_v1_sector_weights.csv", index=False)

    oos_rows = walkforward_rows_df.loc[
        (walkforward_rows_df["stage"] == "oos_eval") & (walkforward_rows_df["period"] == "OOS 2022-2026")
    ].copy()
    offensive_oos = oos_rows.loc[oos_rows["scenario"] == "offensive_default_candidate"].iloc[0]
    qqq_oos = oos_rows.loc[oos_rows["scenario"] == "QQQ"].iloc[0]

    gate = {
        "recommendation": "yes_shadow_tracking"
        if (
            float(offensive_oos["rolling_36m_alpha_positive_ratio"]) >= 0.60
            and float(offensive_oos["CAGR"]) >= float(qqq_oos["CAGR"])
            and float(offensive_oos["Max Drawdown"]) >= -0.40
            and float(turnover_profile["annual_turnover"]) <= 5.0
            and float(stability_stats["plateau_200bps_share"]) >= 0.20
        )
        else "no_shadow_tracking",
        "reason": "passes OOS alpha / cost / drawdown / turnover / neighborhood gates"
        if (
            float(offensive_oos["rolling_36m_alpha_positive_ratio"]) >= 0.60
            and float(offensive_oos["CAGR"]) >= float(qqq_oos["CAGR"])
            and float(offensive_oos["Max Drawdown"]) >= -0.40
            and float(turnover_profile["annual_turnover"]) <= 5.0
            and float(stability_stats["plateau_200bps_share"]) >= 0.20
        )
        else "fails at least one promotion gate",
        "oos_positive_rolling_alpha_ratio": float(offensive_oos["rolling_36m_alpha_positive_ratio"]),
        "oos_cagr_minus_qqq_5bps": float(offensive_oos["CAGR"] - qqq_oos["CAGR"]),
        "oos_max_drawdown": float(offensive_oos["Max Drawdown"]),
        "annual_turnover": float(turnover_profile["annual_turnover"]),
        "plateau_200bps_share": float(stability_stats["plateau_200bps_share"]),
        "selected_oos_config": selected_config.name,
    }

    write_markdown_report(
        results_dir,
        base_config=base_config,
        parameter_rows=parameter_rows_df,
        stability_axis_summary=stability_axis_summary,
        stability_stats=stability_stats,
        regime_rows=regime_rows_df,
        pressure_rows=pressure_rows_df,
        costs_rows=costs_rows_df,
        walkforward_rows=walkforward_rows_df,
        attribution_rows=attribution_rows_df,
        gate=gate,
    )

    gate_path = results_dir / "stock_alpha_v1_promotion_gate.json"
    gate_path.write_text(json.dumps(gate, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"alias data: {alias_dir}")
    print(f"no-alias data: {no_alias_dir}")
    print(f"results written to: {results_dir}")
    print(f"promotion gate: {gate_path}")


if __name__ == "__main__":
    main()
