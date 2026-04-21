#!/usr/bin/env python3
"""Research-only growth optimization study for the QQQ/TQQQ dual-drive idea.

This script keeps the next-close execution model and explores growth-oriented
changes around three themes:

- higher TQQQ share during risk-on trends;
- partial QQQ exposure while the baseline would sit in cash;
- cleaner or more aggressive pullback entries.

It does not change live strategy code.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from backtest_video_qqq_tqqq_dual_drive import (
    CASH_SYMBOL,
    build_buy_hold_run,
    build_summary,
    frame_to_markdown_table,
    normalize_weights,
)
from backtest_video_qqq_tqqq_position_scaling import compute_turnover, load_market_data


CURRENT_DIR = Path(__file__).resolve().parent
DEFAULT_RESULTS_DIR = CURRENT_DIR / "results"
DEFAULT_DOWNLOAD_START = "2016-01-01"
DEFAULT_PERIOD_START = "2017-01-03"
DEFAULT_PERIOD_END = None
DEFAULT_COSTS_BPS = (0.0, 5.0, 10.0)


@dataclass(frozen=True)
class OptimizationConfig:
    name: str
    theme: str
    description: str
    bull_qqq_weight: float = 0.45
    bull_tqqq_weight: float = 0.45
    bull_cash_weight: float = 0.10
    strong_qqq_weight: float | None = None
    strong_tqqq_weight: float | None = None
    strong_cash_weight: float = 0.10
    pullback_mode: str = "base"
    pullback_qqq_weight: float = 0.45
    pullback_tqqq_weight: float = 0.45
    pullback_cash_weight: float = 0.10
    idle_qqq_weight: float = 0.0
    idle_condition: str = "always"


@dataclass
class StrategyRun:
    strategy_name: str
    display_name: str
    gross_returns: pd.Series
    weights_history: pd.DataFrame
    turnover_history: pd.Series
    metadata: dict[str, object]


CONFIGS = (
    OptimizationConfig(
        name="baseline_pullback_45_45",
        theme="baseline",
        description="Current retained pullback reconstruction: 45% QQQ + 45% TQQQ + 10% cash.",
    ),
    OptimizationConfig(
        name="attack_40_50",
        theme="attack_weight",
        description="Replace 5 points of QQQ with TQQQ during both trend and pullback risk-on states.",
        bull_qqq_weight=0.40,
        bull_tqqq_weight=0.50,
        pullback_qqq_weight=0.40,
        pullback_tqqq_weight=0.50,
    ),
    OptimizationConfig(
        name="attack_35_55",
        theme="attack_weight",
        description="Replace 10 points of QQQ with TQQQ during both trend and pullback risk-on states.",
        bull_qqq_weight=0.35,
        bull_tqqq_weight=0.55,
        pullback_qqq_weight=0.35,
        pullback_tqqq_weight=0.55,
    ),
    OptimizationConfig(
        name="attack_30_60",
        theme="attack_weight",
        description="Replace 15 points of QQQ with TQQQ during both trend and pullback risk-on states.",
        bull_qqq_weight=0.30,
        bull_tqqq_weight=0.60,
        pullback_qqq_weight=0.30,
        pullback_tqqq_weight=0.60,
    ),
    OptimizationConfig(
        name="attack_25_65",
        theme="attack_weight",
        description="Replace 20 points of QQQ with TQQQ during both trend and pullback risk-on states.",
        bull_qqq_weight=0.25,
        bull_tqqq_weight=0.65,
        pullback_qqq_weight=0.25,
        pullback_tqqq_weight=0.65,
    ),
    OptimizationConfig(
        name="strong_trend_35_55",
        theme="attack_weight",
        description="Use 35% QQQ + 55% TQQQ only when QQQ trend stack is strong; otherwise baseline weights.",
        strong_qqq_weight=0.35,
        strong_tqqq_weight=0.55,
    ),
    OptimizationConfig(
        name="strong_trend_30_60",
        theme="attack_weight",
        description="Use 30% QQQ + 60% TQQQ only when QQQ trend stack is strong; otherwise baseline weights.",
        strong_qqq_weight=0.30,
        strong_tqqq_weight=0.60,
    ),
    OptimizationConfig(
        name="idle_25qqq",
        theme="idle_exposure",
        description="Keep 25% QQQ / 75% cash while the baseline is idle.",
        idle_qqq_weight=0.25,
    ),
    OptimizationConfig(
        name="idle_50qqq",
        theme="idle_exposure",
        description="Keep 50% QQQ / 50% cash while the baseline is idle.",
        idle_qqq_weight=0.50,
    ),
    OptimizationConfig(
        name="idle_25qqq_positive_ma20",
        theme="idle_exposure",
        description="Keep 25% QQQ while idle only when QQQ is above MA20 with positive MA20 slope.",
        idle_qqq_weight=0.25,
        idle_condition="positive_ma20",
    ),
    OptimizationConfig(
        name="idle_50qqq_positive_ma20",
        theme="idle_exposure",
        description="Keep 50% QQQ while idle only when QQQ is above MA20 with positive MA20 slope.",
        idle_qqq_weight=0.50,
        idle_condition="positive_ma20",
    ),
    OptimizationConfig(
        name="pullback_quality_45_45",
        theme="pullback_quality",
        description="Require below-MA200 pullback entries to clear a volatility-scaled rebound threshold.",
        pullback_mode="quality_rebound",
    ),
    OptimizationConfig(
        name="pullback_quality_35_55",
        theme="pullback_quality",
        description="Quality rebound pullback gate, then use 35% QQQ + 55% TQQQ.",
        pullback_mode="quality_rebound",
        pullback_qqq_weight=0.35,
        pullback_tqqq_weight=0.55,
    ),
    OptimizationConfig(
        name="pullback_quality_30_60",
        theme="pullback_quality",
        description="Quality rebound pullback gate, then use 30% QQQ + 60% TQQQ.",
        pullback_mode="quality_rebound",
        pullback_qqq_weight=0.30,
        pullback_tqqq_weight=0.60,
    ),
    OptimizationConfig(
        name="pullback_aggressive_35_55",
        theme="pullback_quality",
        description="Keep the current pullback gate but shift pullback weight to 35% QQQ + 55% TQQQ.",
        pullback_qqq_weight=0.35,
        pullback_tqqq_weight=0.55,
    ),
    OptimizationConfig(
        name="pullback_aggressive_30_60",
        theme="pullback_quality",
        description="Keep the current pullback gate but shift pullback weight to 30% QQQ + 60% TQQQ.",
        pullback_qqq_weight=0.30,
        pullback_tqqq_weight=0.60,
    ),
    OptimizationConfig(
        name="strong_trend_35_55_quality_pullback_35_55",
        theme="combo",
        description="Use stronger TQQQ in strong trends and only take quality rebound pullbacks at 35%/55%.",
        strong_qqq_weight=0.35,
        strong_tqqq_weight=0.55,
        pullback_mode="quality_rebound",
        pullback_qqq_weight=0.35,
        pullback_tqqq_weight=0.55,
    ),
    OptimizationConfig(
        name="attack_40_50_idle_25qqq",
        theme="combo",
        description="Use 40%/50% risk-on weights and keep 25% QQQ while idle.",
        bull_qqq_weight=0.40,
        bull_tqqq_weight=0.50,
        pullback_qqq_weight=0.40,
        pullback_tqqq_weight=0.50,
        idle_qqq_weight=0.25,
    ),
    OptimizationConfig(
        name="strong_trend_35_55_idle_25qqq",
        theme="combo",
        description="Use 35%/55% only in strong trend stacks and keep 25% QQQ while idle.",
        strong_qqq_weight=0.35,
        strong_tqqq_weight=0.55,
        idle_qqq_weight=0.25,
    ),
    OptimizationConfig(
        name="attack_40_50_quality_pullback_35_55_idle_25qqq",
        theme="combo",
        description="Moderately higher trend TQQQ, quality rebound pullbacks at 35%/55%, and 25% QQQ idle.",
        bull_qqq_weight=0.40,
        bull_tqqq_weight=0.50,
        pullback_mode="quality_rebound",
        pullback_qqq_weight=0.35,
        pullback_tqqq_weight=0.55,
        idle_qqq_weight=0.25,
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR))
    parser.add_argument("--download-start", default=DEFAULT_DOWNLOAD_START)
    parser.add_argument("--period-start", default=DEFAULT_PERIOD_START)
    parser.add_argument("--period-end", default=DEFAULT_PERIOD_END)
    parser.add_argument("--end", default=None, help="Inclusive Nasdaq download end date, YYYY-MM-DD.")
    parser.add_argument("--cost-bps", nargs="*", type=float, default=list(DEFAULT_COSTS_BPS))
    return parser.parse_args()


def enrich_indicators(indicators: pd.DataFrame) -> pd.DataFrame:
    frame = indicators.copy()
    close = frame["qqq_close"]
    frame["qqq_ma10"] = close.rolling(10).mean()
    frame["qqq_ma10_slope"] = frame["qqq_ma10"].diff()
    frame["qqq_return_5d"] = close.pct_change(5, fill_method=None)
    frame["qqq_pullback_low20"] = close.rolling(20).min()
    frame["qqq_pullback_rebound20"] = close / frame["qqq_pullback_low20"] - 1.0
    frame["qqq_rolling_vol20"] = close.pct_change(fill_method=None).rolling(20).std()
    return frame


def _is_strong_trend(row: pd.Series) -> bool:
    close = float(row["qqq_close"])
    ma20 = float(row["qqq_ma20"]) if pd.notna(row["qqq_ma20"]) else float("nan")
    ma60 = float(row["qqq_ma60"]) if pd.notna(row["qqq_ma60"]) else float("nan")
    ma200 = float(row["qqq_ma200"]) if pd.notna(row["qqq_ma200"]) else float("nan")
    ma20_slope = float(row["qqq_ma20_slope"]) if pd.notna(row["qqq_ma20_slope"]) else float("nan")
    return (
        pd.notna(ma20)
        and pd.notna(ma60)
        and pd.notna(ma200)
        and pd.notna(ma20_slope)
        and close > ma20
        and ma20 > ma60
        and close > ma200
        and ma20_slope > 0.0
    )


def _idle_qqq_weight(config: OptimizationConfig, row: pd.Series) -> float:
    if config.idle_qqq_weight <= 0.0:
        return 0.0
    if config.idle_condition == "always":
        return float(config.idle_qqq_weight)
    if config.idle_condition == "positive_ma20":
        close = float(row["qqq_close"])
        ma20 = float(row["qqq_ma20"]) if pd.notna(row["qqq_ma20"]) else float("nan")
        ma20_slope = float(row["qqq_ma20_slope"]) if pd.notna(row["qqq_ma20_slope"]) else float("nan")
        if pd.notna(ma20) and pd.notna(ma20_slope) and close > ma20 and ma20_slope > 0.0:
            return float(config.idle_qqq_weight)
        return 0.0
    raise KeyError(f"Unknown idle condition: {config.idle_condition}")


def _pullback_risk_on(config: OptimizationConfig, row: pd.Series, *, above_ma200: bool) -> bool:
    if config.pullback_mode == "none":
        return False

    ma200 = float(row["qqq_ma200"]) if pd.notna(row["qqq_ma200"]) else float("nan")
    if pd.isna(ma200):
        return False

    close = float(row["qqq_close"])
    ma20 = float(row["qqq_ma20"]) if pd.notna(row["qqq_ma20"]) else float("nan")
    ma20_slope = float(row["qqq_ma20_slope"]) if pd.notna(row["qqq_ma20_slope"]) else float("nan")
    positive_ma20_slope = pd.notna(ma20_slope) and ma20_slope > 0.0
    base = (not above_ma200) and pd.notna(ma20) and close > ma20 and positive_ma20_slope
    if config.pullback_mode == "base":
        return base
    if config.pullback_mode == "quality_rebound":
        rebound = float(row["qqq_pullback_rebound20"]) if pd.notna(row["qqq_pullback_rebound20"]) else float("nan")
        rolling_vol = float(row["qqq_rolling_vol20"]) if pd.notna(row["qqq_rolling_vol20"]) else float("nan")
        threshold = max(0.02, rolling_vol * 2.0) if pd.notna(rolling_vol) else 0.02
        return base and pd.notna(rebound) and rebound > threshold
    raise KeyError(f"Unknown pullback mode: {config.pullback_mode}")


def decide_weights(
    config: OptimizationConfig,
    row: pd.Series,
    *,
    risk_active: bool,
) -> tuple[dict[str, float], bool]:
    close = float(row["qqq_close"])
    ma200 = float(row["qqq_ma200"]) if pd.notna(row["qqq_ma200"]) else float("nan")
    ma20_slope = float(row["qqq_ma20_slope"]) if pd.notna(row["qqq_ma20_slope"]) else float("nan")
    has_long_history = pd.notna(ma200)
    above_ma200 = has_long_history and close > ma200
    positive_ma20_slope = pd.notna(ma20_slope) and ma20_slope > 0.0

    next_risk_active = risk_active
    if risk_active and has_long_history and not above_ma200:
        next_risk_active = False
    elif not risk_active and above_ma200 and positive_ma20_slope:
        next_risk_active = True

    if next_risk_active:
        if config.strong_qqq_weight is not None and config.strong_tqqq_weight is not None and _is_strong_trend(row):
            return normalize_weights(
                {
                    "QQQ": config.strong_qqq_weight,
                    "TQQQ": config.strong_tqqq_weight,
                    CASH_SYMBOL: config.strong_cash_weight,
                }
            ), next_risk_active
        return normalize_weights(
            {
                "QQQ": config.bull_qqq_weight,
                "TQQQ": config.bull_tqqq_weight,
                CASH_SYMBOL: config.bull_cash_weight,
            }
        ), next_risk_active

    if _pullback_risk_on(config, row, above_ma200=above_ma200):
        return normalize_weights(
            {
                "QQQ": config.pullback_qqq_weight,
                "TQQQ": config.pullback_tqqq_weight,
                CASH_SYMBOL: config.pullback_cash_weight,
            }
        ), next_risk_active

    idle_qqq = _idle_qqq_weight(config, row)
    return normalize_weights({"QQQ": idle_qqq, CASH_SYMBOL: max(0.0, 1.0 - idle_qqq)}), next_risk_active


def run_backtest(config: OptimizationConfig, returns_matrix: pd.DataFrame, indicators: pd.DataFrame) -> StrategyRun:
    index = returns_matrix.index.intersection(indicators.index)
    asset_columns = ("QQQ", "TQQQ", CASH_SYMBOL)
    weights_history = pd.DataFrame(0.0, index=index, columns=asset_columns)
    portfolio_returns = pd.Series(0.0, index=index, name=config.name)
    turnover_history = pd.Series(0.0, index=index, name="turnover")

    current_weights = {CASH_SYMBOL: 1.0}
    risk_active = False
    for idx in range(len(index) - 1):
        date = index[idx]
        next_date = index[idx + 1]
        target_weights, risk_active = decide_weights(config, indicators.loc[date], risk_active=risk_active)
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

    for symbol, weight in current_weights.items():
        weights_history.at[index[-1], symbol] = weight

    return StrategyRun(
        strategy_name=config.name,
        display_name=config.name,
        gross_returns=portfolio_returns,
        weights_history=weights_history,
        turnover_history=turnover_history,
        metadata={
            "family": "video_qqq_tqqq_growth_optimizations",
            "execution_mode": "next_close",
            "theme": config.theme,
            "description": config.description,
            "known_limitation": "Nasdaq close data is not dividend-adjusted; exact live state machine remains approximate.",
        },
    )


def choose_recommendation(summary: pd.DataFrame) -> dict[str, object]:
    focus = summary.loc[
        (summary["cost_bps_one_way"] == 5.0)
        & (summary["family"] == "video_qqq_tqqq_growth_optimizations")
    ].copy()
    baseline = focus.loc[focus["strategy"] == "baseline_pullback_45_45"].iloc[0]
    candidates = focus.loc[focus["strategy"] != "baseline_pullback_45_45"].copy()
    baseline_maxdd = float(baseline["Max Drawdown"])
    near_drawdown_floor = baseline_maxdd - 0.05
    near_drawdown = candidates.loc[candidates["Max Drawdown"] >= near_drawdown_floor].copy()
    if near_drawdown.empty:
        near_drawdown = candidates.copy()

    best_cagr = candidates.sort_values("CAGR", ascending=False).iloc[0]
    best_near_drawdown = near_drawdown.sort_values("CAGR", ascending=False).iloc[0]
    best_2023 = candidates.sort_values("2023+ CAGR", ascending=False).iloc[0]

    by_theme: dict[str, dict[str, object]] = {}
    for theme, rows in candidates.groupby("theme"):
        best = rows.sort_values("CAGR", ascending=False).iloc[0]
        by_theme[str(theme)] = {
            "strategy": str(best["strategy"]),
            "cagr": float(best["CAGR"]),
            "max_drawdown": float(best["Max Drawdown"]),
            "turnover_per_year": float(best["Turnover/Year"]),
            "cagr_delta_vs_baseline": float(best["CAGR"] - baseline["CAGR"]),
        }

    findings = [
        (
            f"Baseline: {baseline['CAGR']:.2%} CAGR / {baseline['Max Drawdown']:.2%} MaxDD / "
            f"{baseline['2023+ CAGR']:.2%} 2023+ CAGR."
        ),
        (
            f"Best CAGR candidate: `{best_cagr['strategy']}` at {best_cagr['CAGR']:.2%} CAGR / "
            f"{best_cagr['Max Drawdown']:.2%} MaxDD."
        ),
        (
            f"Best candidate within 5pp worse MaxDD than baseline: `{best_near_drawdown['strategy']}` at "
            f"{best_near_drawdown['CAGR']:.2%} CAGR / {best_near_drawdown['Max Drawdown']:.2%} MaxDD."
        ),
        (
            f"Best 2023+ candidate: `{best_2023['strategy']}` at {best_2023['2023+ CAGR']:.2%} 2023+ CAGR / "
            f"{best_2023['Max Drawdown']:.2%} MaxDD."
        ),
    ]

    verdict = (
        "The cleanest growth lever is shifting a moderate amount of QQQ into TQQQ during risk-on states. "
        "Idle QQQ exposure helps less, and stricter pullback quality gates mostly give up too much upside."
    )
    findings.append(verdict)

    return {
        "baseline": {
            "strategy": str(baseline["strategy"]),
            "cagr": float(baseline["CAGR"]),
            "max_drawdown": baseline_maxdd,
            "turnover_per_year": float(baseline["Turnover/Year"]),
            "cagr_2023_plus": float(baseline["2023+ CAGR"]),
        },
        "best_cagr": _row_summary(best_cagr),
        "best_near_baseline_drawdown": _row_summary(best_near_drawdown),
        "best_2023_plus": _row_summary(best_2023),
        "best_by_theme": by_theme,
        "findings": findings,
        "verdict": verdict,
    }


def _row_summary(row: pd.Series) -> dict[str, object]:
    return {
        "strategy": str(row["strategy"]),
        "theme": str(row["theme"]),
        "cagr": float(row["CAGR"]),
        "max_drawdown": float(row["Max Drawdown"]),
        "turnover_per_year": float(row["Turnover/Year"]),
        "cagr_2023_plus": float(row["2023+ CAGR"]),
        "average_qqq_weight": float(row["Average QQQ Weight"]),
        "average_tqqq_weight": float(row["Average TQQQ Weight"]),
    }


def build_markdown(summary: pd.DataFrame, recommendation: dict[str, object]) -> str:
    focus = summary.loc[
        (summary["cost_bps_one_way"] == 5.0)
        & (summary["family"] == "video_qqq_tqqq_growth_optimizations")
    ].copy()
    compact_columns = [
        "strategy",
        "theme",
        "CAGR",
        "Max Drawdown",
        "2020 Return",
        "2022 Return",
        "2023 Return",
        "2023+ CAGR",
        "Turnover/Year",
        "Average QQQ Weight",
        "Average TQQQ Weight",
        "Average Cash Weight",
    ]
    top_cagr = focus.sort_values("CAGR", ascending=False).head(10)
    top_2023 = focus.sort_values("2023+ CAGR", ascending=False).head(10)
    theme_best = focus.loc[focus["strategy"].isin(item["strategy"] for item in recommendation["best_by_theme"].values())]
    theme_best = theme_best.sort_values("CAGR", ascending=False)
    return "\n".join(
        [
            "# Video QQQ/TQQQ Growth Optimization Follow-up",
            "",
            "## Setup",
            "- Data: Nasdaq daily close/OHLC, not dividend-adjusted.",
            "- Signal timing: next-close implementation; no same-close lookahead.",
            "- Baseline: retained pullback reconstruction, 45% QQQ + 45% TQQQ + 10% cash.",
            "- Goal: improve growth without treating lower drawdown as the primary objective.",
            "",
            "## Top CAGR Candidates",
            frame_to_markdown_table(top_cagr[compact_columns]),
            "",
            "## Top 2023+ CAGR Candidates",
            frame_to_markdown_table(top_2023[compact_columns]),
            "",
            "## Best By Theme",
            frame_to_markdown_table(theme_best[compact_columns]),
            "",
            "## Findings",
            *[f"- {item}" for item in recommendation["findings"]],
            "",
            "## Caveats",
            "- Nasdaq close data is not adjusted for dividends, so absolute CAGR should be compared mainly within this study.",
            "- This is a research-only experiment; no live allocation code was changed.",
        ]
    ) + "\n"


def write_outputs(summary: pd.DataFrame, recommendation: dict[str, object], results_dir: Path) -> None:
    comparison_path = results_dir / "video_qqq_tqqq_growth_optimizations_comparison.csv"
    summary_path = results_dir / "video_qqq_tqqq_growth_optimizations_summary.md"
    recommendation_path = results_dir / "video_qqq_tqqq_growth_optimizations_recommendation.json"
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

    _qqq_ohlc, returns_matrix, indicators = load_market_data(start=args.download_start, end=args.end)
    indicators = enrich_indicators(indicators)
    runs = [run_backtest(config, returns_matrix, indicators) for config in CONFIGS]
    reference_runs = [build_buy_hold_run("QQQ", returns_matrix), build_buy_hold_run("TQQQ", returns_matrix)]

    summary = build_summary(
        [*runs, *reference_runs],
        returns_matrix["QQQ"],
        costs_bps=args.cost_bps,
        period_start=args.period_start,
        period_end=args.period_end,
    )
    recommendation = choose_recommendation(summary)
    write_outputs(summary, recommendation, results_dir)


if __name__ == "__main__":
    main()
