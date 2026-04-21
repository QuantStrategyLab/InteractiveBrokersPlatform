#!/usr/bin/env python3
"""Research-only position-scaling follow-up for the QQQ/TQQQ dual-drive idea.

This script does not change the live strategy. It tests whether changing only
the in-position TQQQ sleeve size improves the retained dual-drive reconstruction.
Signals are formed on today's close and applied to the next close-to-close
return.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import requests

from backtest_video_qqq_tqqq_dual_drive import (
    CASH_SYMBOL,
    VideoConfig,
    build_buy_hold_run,
    build_summary,
    decide_video_weights,
    frame_to_markdown_table,
    normalize_weights,
)


CURRENT_DIR = Path(__file__).resolve().parent
DEFAULT_RESULTS_DIR = CURRENT_DIR / "results"
DEFAULT_PERIOD_START = "2017-01-03"
DEFAULT_DOWNLOAD_START = "2016-01-01"
DEFAULT_PERIOD_END = None
DEFAULT_COSTS_BPS = (0.0, 5.0, 10.0)
NASDAQ_ENDPOINT = "https://api.nasdaq.com/api/quote/{symbol}/historical"


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
    metadata: dict[str, object]
    scale_history: pd.Series


SCALING_CONFIGS = (
    ScalingConfig(
        name="baseline",
        description="No in-position scaling; keep 45% QQQ + 45% TQQQ + 10% cash while risk-on.",
    ),
    ScalingConfig(
        name="ma60_half",
        description="Cut TQQQ by 50% while risk-on when QQQ closes below MA60; move freed weight to cash.",
    ),
    ScalingConfig(
        name="ma20_gap_trim_only",
        description="Scale TQQQ to 50% when QQQ is 2%+ below MA20, 75% when below MA20, otherwise baseline.",
    ),
    ScalingConfig(
        name="ma20_gap_trim_boost",
        description="Same MA20 trim, plus boost TQQQ to 115% of baseline when QQQ is 3%+ above MA20.",
    ),
    ScalingConfig(
        name="trend_score_4",
        description="Use close>MA20, MA20>MA60, positive MA20 slope, close>MA200 to scale TQQQ in steps.",
    ),
)


SIGNAL_CONFIGS = (
    VideoConfig(
        name="trend_only",
        description="Above-MA200 dual-drive state machine without below-MA200 pullback entry.",
        execution_mode="next_close",
    ),
    VideoConfig(
        name="pullback",
        description="Above-MA200 dual-drive plus the retained below-MA200 pullback state.",
        execution_mode="next_close",
        allow_below_ma200_pullback=True,
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


def _clean_price(value: object) -> float:
    text = str(value).replace("$", "").replace(",", "").strip()
    return float(text)


def download_nasdaq_ohlcv(symbol: str, *, start: str, end: str | None) -> pd.DataFrame:
    params = {
        "assetclass": "etf",
        "fromdate": start,
        "limit": "9999",
    }
    if end:
        params["todate"] = end
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
        "Origin": "https://www.nasdaq.com",
        "Referer": f"https://www.nasdaq.com/market-activity/etf/{symbol.lower()}/historical",
    }
    response = requests.get(
        NASDAQ_ENDPOINT.format(symbol=symbol.upper()),
        params=params,
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    rows = (((payload.get("data") or {}).get("tradesTable") or {}).get("rows") or [])
    if not rows:
        raise RuntimeError(f"No Nasdaq historical rows returned for {symbol}")

    frame = pd.DataFrame(rows)
    frame["date"] = pd.to_datetime(frame["date"]).dt.tz_localize(None).dt.normalize()
    frame = frame.rename(columns={"close": "close", "open": "open", "high": "high", "low": "low", "volume": "volume"})
    for column in ("open", "high", "low", "close"):
        frame[column] = frame[column].map(_clean_price)
    frame["volume"] = frame["volume"].map(lambda value: int(str(value).replace(",", "").strip()))
    frame = frame.set_index("date").sort_index()
    return frame[["open", "high", "low", "close", "volume"]]


def load_market_data(*, start: str, end: str | None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    qqq_ohlc = download_nasdaq_ohlcv("QQQ", start=start, end=end)
    tqqq_ohlc = download_nasdaq_ohlcv("TQQQ", start=start, end=end)
    index = qqq_ohlc.index.intersection(tqqq_ohlc.index)
    qqq_ohlc = qqq_ohlc.reindex(index).dropna()
    tqqq_ohlc = tqqq_ohlc.reindex(index).dropna()
    close = pd.DataFrame({"QQQ": qqq_ohlc["close"], "TQQQ": tqqq_ohlc["close"]}, index=index)
    returns_matrix = close.pct_change(fill_method=None).replace([float("inf"), float("-inf")], pd.NA).fillna(0.0)
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
    frame["qqq_ma60"] = qqq_close.rolling(60).mean()
    frame["qqq_ma200"] = qqq_close.rolling(200).mean()
    frame["qqq_ma20_slope"] = frame["qqq_ma20"].diff()
    frame["qqq_ma20_gap"] = qqq_close / frame["qqq_ma20"] - 1.0
    frame["tqqq_ma200"] = tqqq_close.rolling(200).mean()
    frame["tqqq_overheat_ratio"] = tqqq_close / frame["tqqq_ma200"]
    return frame


def compute_turnover(old_weights: dict[str, float], new_weights: dict[str, float]) -> float:
    symbols = set(old_weights) | set(new_weights)
    return 0.5 * sum(abs(float(new_weights.get(symbol, 0.0)) - float(old_weights.get(symbol, 0.0))) for symbol in symbols)


def scaling_multiplier(config_name: str, row: pd.Series) -> float:
    close = float(row["qqq_close"])
    ma20 = float(row["qqq_ma20"]) if pd.notna(row["qqq_ma20"]) else float("nan")
    ma60 = float(row["qqq_ma60"]) if pd.notna(row["qqq_ma60"]) else float("nan")
    ma200 = float(row["qqq_ma200"]) if pd.notna(row["qqq_ma200"]) else float("nan")
    ma20_slope = float(row["qqq_ma20_slope"]) if pd.notna(row["qqq_ma20_slope"]) else float("nan")
    ma20_gap = float(row["qqq_ma20_gap"]) if pd.notna(row["qqq_ma20_gap"]) else float("nan")

    if config_name == "baseline":
        return 1.0
    if config_name == "ma60_half":
        return 0.5 if pd.notna(ma60) and close < ma60 else 1.0
    if config_name == "ma20_gap_trim_only":
        if pd.notna(ma20_gap) and ma20_gap <= -0.02:
            return 0.5
        if pd.notna(ma20_gap) and ma20_gap < 0.0:
            return 0.75
        return 1.0
    if config_name == "ma20_gap_trim_boost":
        if pd.notna(ma20_gap) and ma20_gap <= -0.02:
            return 0.5
        if pd.notna(ma20_gap) and ma20_gap < 0.0:
            return 0.75
        if pd.notna(ma20_gap) and ma20_gap >= 0.03:
            return 1.15
        return 1.0
    if config_name == "trend_score_4":
        score = sum(
            [
                pd.notna(ma20) and close > ma20,
                pd.notna(ma20) and pd.notna(ma60) and ma20 > ma60,
                pd.notna(ma20_slope) and ma20_slope > 0.0,
                pd.notna(ma200) and close > ma200,
            ]
        )
        if score >= 4:
            return 1.10
        if score == 3:
            return 1.0
        if score == 2:
            return 0.75
        return 0.5
    raise KeyError(f"Unknown scaling config: {config_name}")


def apply_tqqq_scaling(weights: dict[str, float], scale: float) -> dict[str, float]:
    base_tqqq = float(weights.get("TQQQ", 0.0))
    if base_tqqq <= 1e-12:
        return normalize_weights(weights)

    target_tqqq = max(0.0, base_tqqq * float(scale))
    delta = target_tqqq - base_tqqq
    adjusted = dict(weights)
    adjusted["TQQQ"] = target_tqqq

    if delta < 0.0:
        adjusted[CASH_SYMBOL] = float(adjusted.get(CASH_SYMBOL, 0.0)) + abs(delta)
    elif delta > 0.0:
        cash = float(adjusted.get(CASH_SYMBOL, 0.0))
        cash_used = min(cash, delta)
        adjusted[CASH_SYMBOL] = cash - cash_used
        remaining = delta - cash_used
        if remaining > 1e-12:
            adjusted["QQQ"] = max(0.0, float(adjusted.get("QQQ", 0.0)) - remaining)

    return normalize_weights(adjusted)


def run_scaling_backtest(
    signal_config: VideoConfig,
    scaling_config: ScalingConfig,
    returns_matrix: pd.DataFrame,
    indicators: pd.DataFrame,
) -> StrategyRun:
    index = returns_matrix.index.intersection(indicators.index)
    asset_columns = ("QQQ", "TQQQ", CASH_SYMBOL)
    weights_history = pd.DataFrame(0.0, index=index, columns=asset_columns)
    portfolio_returns = pd.Series(0.0, index=index, name=f"{signal_config.name}_{scaling_config.name}")
    turnover_history = pd.Series(0.0, index=index, name="turnover")
    scale_history = pd.Series(0.0, index=index, name="tqqq_scale")

    current_weights = {CASH_SYMBOL: 1.0}
    risk_active = False
    for idx in range(len(index) - 1):
        date = index[idx]
        next_date = index[idx + 1]
        base_weights, risk_active = decide_video_weights(signal_config, indicators.loc[date], risk_active=risk_active)
        scale = scaling_multiplier(scaling_config.name, indicators.loc[date]) if base_weights.get("TQQQ", 0.0) > 0.0 else 0.0
        target_weights = apply_tqqq_scaling(base_weights, scale)
        scale_history.at[date] = scale

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
        strategy_name=f"{signal_config.name}_{scaling_config.name}",
        display_name=f"{signal_config.name}_{scaling_config.name}",
        gross_returns=portfolio_returns,
        weights_history=weights_history,
        turnover_history=turnover_history,
        scale_history=scale_history,
        metadata={
            "family": "video_qqq_tqqq_position_scaling",
            "execution_mode": "next_close",
            "signal_mode": signal_config.name,
            "scaling": scaling_config.name,
            "description": f"{signal_config.description} {scaling_config.description}",
            "known_limitation": "Nasdaq close data is not dividend-adjusted; exact live state machine remains approximate.",
        },
    )


def enrich_summary(summary: pd.DataFrame, runs: list[StrategyRun]) -> pd.DataFrame:
    scale_by_strategy = {
        run.strategy_name: run.scale_history.reindex(run.gross_returns.index).fillna(0.0)
        for run in runs
    }
    rows: list[dict[str, object]] = []
    for row in summary.to_dict("records"):
        strategy = str(row["strategy"])
        scale = scale_by_strategy.get(strategy)
        if scale is None:
            row["Average Scale While Invested"] = math.nan
        else:
            start = row.get("Start")
            end = row.get("End")
            sliced_scale = scale.copy()
            if start:
                sliced_scale = sliced_scale.loc[str(start):]
            if end:
                sliced_scale = sliced_scale.loc[: str(end)]
            invested = sliced_scale[sliced_scale > 0.0]
            row["Average Scale While Invested"] = float(invested.mean()) if not invested.empty else 0.0
        rows.append(row)
    return pd.DataFrame(rows)


def choose_recommendation(summary: pd.DataFrame) -> dict[str, object]:
    focus = summary.loc[summary["cost_bps_one_way"] == 5.0].copy()
    scaled = focus.loc[focus["family"] == "video_qqq_tqqq_position_scaling"].copy()
    pullback = scaled.loc[scaled["signal_mode"] == "pullback"].copy()
    baseline = pullback.loc[pullback["scaling"] == "baseline"].iloc[0]
    candidates = pullback.loc[pullback["scaling"] != "baseline"].copy()
    candidates["score"] = (
        candidates["CAGR"].fillna(-999.0) * 3.0
        + candidates["Information Ratio vs QQQ"].fillna(-999.0)
        - candidates["Turnover/Year"].fillna(999.0) * 0.02
        + (candidates["Max Drawdown"].fillna(-999.0) - baseline["Max Drawdown"]) * 0.5
    )
    best = candidates.sort_values("score", ascending=False).iloc[0]
    improves_cagr = float(best["CAGR"]) > float(baseline["CAGR"])
    improves_drawdown = float(best["Max Drawdown"]) > float(baseline["Max Drawdown"])
    if improves_cagr and improves_drawdown:
        verdict = "The best scaled pullback variant improves both CAGR and MaxDD in this run, but should still be validated with adjusted data before live use."
    elif improves_cagr:
        verdict = "The best scaled pullback variant raises CAGR, but it does not improve drawdown enough to justify a live change by itself."
    elif improves_drawdown:
        verdict = "Scaling smooths drawdown, but it lowers CAGR and adds turnover; this is not a clear upgrade."
    else:
        verdict = "No tested in-position scaling variant clearly improves the pullback baseline."

    findings = [
        (
            f"Pullback baseline: {baseline['CAGR']:.2%} CAGR / "
            f"{baseline['Max Drawdown']:.2%} MaxDD / turnover {baseline['Turnover/Year']:.2f}/yr."
        ),
        (
            f"Best scaled pullback candidate: `{best['scaling']}` at {best['CAGR']:.2%} CAGR / "
            f"{best['Max Drawdown']:.2%} MaxDD / turnover {best['Turnover/Year']:.2f}/yr."
        ),
        verdict,
    ]
    return {
        "baseline_pullback": {
            "strategy": str(baseline["strategy"]),
            "cagr": float(baseline["CAGR"]),
            "max_drawdown": float(baseline["Max Drawdown"]),
            "turnover_per_year": float(baseline["Turnover/Year"]),
            "average_tqqq_weight": float(baseline["Average TQQQ Weight"]),
        },
        "best_scaled_pullback_candidate": {
            "strategy": str(best["strategy"]),
            "scaling": str(best["scaling"]),
            "cagr": float(best["CAGR"]),
            "max_drawdown": float(best["Max Drawdown"]),
            "turnover_per_year": float(best["Turnover/Year"]),
            "average_tqqq_weight": float(best["Average TQQQ Weight"]),
            "average_scale_while_invested": float(best["Average Scale While Invested"]),
        },
        "findings": findings,
        "verdict": verdict,
    }


def build_markdown(summary: pd.DataFrame, recommendation: dict[str, object]) -> str:
    focus = summary.loc[summary["cost_bps_one_way"] == 5.0].copy()
    focus = focus.loc[focus["family"] == "video_qqq_tqqq_position_scaling"].sort_values(
        ["signal_mode", "CAGR"],
        ascending=[True, False],
    )
    compact_columns = [
        "strategy",
        "signal_mode",
        "scaling",
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
        "Average Scale While Invested",
    ]
    return "\n".join(
        [
            "# Video QQQ/TQQQ Position-Scaling Follow-up",
            "",
            "## Setup",
            "- Data: Nasdaq daily close/OHLC, not dividend-adjusted.",
            "- Signal timing: next-close implementation; no same-close lookahead.",
            "- Baseline risk-on sleeve: 45% QQQ + 45% TQQQ + 10% cash.",
            "- Scaling changes only the TQQQ sleeve while already in a risk-on or pullback-risk-on state.",
            "",
            "## 5 bps Comparison",
            frame_to_markdown_table(focus[compact_columns]),
            "",
            "## Findings",
            *[f"- {item}" for item in recommendation["findings"]],
            "",
            "## Caveats",
            "- Nasdaq close data is not adjusted for dividends, so absolute CAGR is not a replacement for the retained Yahoo adjusted reference.",
            "- This is a research-only experiment; no live allocation code was changed.",
        ]
    ) + "\n"


def write_scaling_outputs(summary: pd.DataFrame, recommendation: dict[str, object], results_dir: Path) -> None:
    comparison_path = results_dir / "video_qqq_tqqq_position_scaling_comparison.csv"
    summary_path = results_dir / "video_qqq_tqqq_position_scaling_summary.md"
    recommendation_path = results_dir / "video_qqq_tqqq_position_scaling_recommendation.json"
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
    runs: list[StrategyRun] = []
    for signal_config in SIGNAL_CONFIGS:
        for scaling_config in SCALING_CONFIGS:
            runs.append(run_scaling_backtest(signal_config, scaling_config, returns_matrix, indicators))

    reference_runs = [build_buy_hold_run("QQQ", returns_matrix), build_buy_hold_run("TQQQ", returns_matrix)]
    summary = build_summary(
        [*runs, *reference_runs],
        returns_matrix["QQQ"],
        costs_bps=args.cost_bps,
        period_start=args.period_start,
        period_end=args.period_end,
    )
    summary = enrich_summary(summary, runs)
    recommendation = choose_recommendation(summary)
    write_scaling_outputs(summary, recommendation, results_dir)


if __name__ == "__main__":
    main()
