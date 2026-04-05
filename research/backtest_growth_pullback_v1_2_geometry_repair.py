#!/usr/bin/env python3
"""Geometry-repair review for growth_pullback_systematic_v1 tech-heavy branch."""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Mapping

import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import backtest_growth_pullback_suite as gp  # noqa: E402
import backtest_growth_pullback_v1_1_spec_lock as v11g  # noqa: E402
import backtest_stock_alpha_v1_robustness as robust  # noqa: E402
import backtest_stock_alpha_suite as suite  # noqa: E402


DEFAULT_RESULTS_DIR = SCRIPT_DIR / "results"
DEFAULT_CONFIGS_DIR = SCRIPT_DIR / "configs"
MAIN_COST_BPS = 5.0
COST_LEVELS = (0.0, MAIN_COST_BPS)
CENTER_CONFIG_PATH = DEFAULT_CONFIGS_DIR / "growth_pullback_systematic_v1_default.json"
STABLE_NEIGHBOR_CONFIG_PATH = DEFAULT_CONFIGS_DIR / "growth_pullback_systematic_v1_1_default.json"
COMPARISON_PERIODS = (
    ("Full Sample", None, None),
    ("OOS Sample", "2022-01-01", None),
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023+", "2023-01-01", None),
)


@dataclass(frozen=True)
class GeometryCandidate:
    label: str
    config: gp.GrowthPullbackConfig
    risk_on_exposure: float
    note: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alias-data-run-dir", help="Prepared alias-fixed Russell data run dir")
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR))
    parser.add_argument("--configs-dir", default=str(DEFAULT_CONFIGS_DIR))
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    return parser.parse_args()


def extract_period_row(frame: pd.DataFrame, strategy: str, cost_bps: float, period: str) -> pd.Series:
    return frame.loc[
        (frame["strategy"] == strategy)
        & (frame["cost_bps_one_way"] == float(cost_bps))
        & (frame["period"] == period)
    ].iloc[0]


def candidate_config_fields(candidate: GeometryCandidate) -> dict[str, object]:
    fields = gp.config_fields(candidate.config)
    fields.update(
        {
            "candidate_label": candidate.label,
            "config_name": candidate.config.name,
            "risk_on_target_exposure": float(candidate.risk_on_exposure),
            "geometry_note": candidate.note,
        }
    )
    return fields


def make_candidate(label: str, config: gp.GrowthPullbackConfig, *, risk_on_exposure: float, note: str) -> GeometryCandidate:
    return GeometryCandidate(label=label, config=config, risk_on_exposure=float(risk_on_exposure), note=note)


def build_candidates(center: gp.GrowthPullbackConfig, stable_neighbor: gp.GrowthPullbackConfig) -> list[GeometryCandidate]:
    return [
        make_candidate(
            "center_current",
            center,
            risk_on_exposure=1.0,
            note="Current center spec; risk_on target=100% but two-sector + 40% sector cap implies theoretical 80% stock max",
        ),
        make_candidate(
            "local_stable_neighbor",
            stable_neighbor,
            risk_on_exposure=1.0,
            note="Current local stable neighbor from v1.1; single cap lowered from 10% to 8%",
        ),
        make_candidate(
            "explicit_cash_buffer_control",
            replace(center, name=f"{center.name}__explicit_cash_buffer_control"),
            risk_on_exposure=0.8,
            note="Make the accidental 80% risk_on stock cap explicit while keeping the same two-sector geometry",
        ),
        make_candidate(
            "feasible_two_sector_50cap",
            replace(center, name=f"{center.name}__sector_cap_0p5", sector_cap=0.50),
            risk_on_exposure=1.0,
            note="Keep two-sector theme but raise sector cap to 50% so 12 names can reach 100% risk_on",
        ),
        make_candidate(
            "feasible_two_sector_50cap_single8",
            replace(center, name=f"{center.name}__sector_cap_0p5__single_name_cap_0p08", sector_cap=0.50, single_name_cap=0.08),
            risk_on_exposure=1.0,
            note="Raise sector cap to 50% but keep single cap at 8%; geometry becomes near-feasible but still maxes at 96% stock",
        ),
    ]


def build_target_weights_with_override(
    raw_snapshot: pd.DataFrame,
    current_holdings: set[str],
    candidate: GeometryCandidate,
) -> tuple[dict[str, float], dict[str, object]]:
    config = candidate.config
    frame = raw_snapshot.copy()
    benchmark_symbol = str(config.regime.benchmark_symbol).upper()
    benchmark_rows = frame.loc[frame["symbol"] == benchmark_symbol]
    benchmark_trend_positive = bool(
        (not benchmark_rows.empty)
        and pd.notna(benchmark_rows.iloc[-1]["sma200_gap"])
        and float(benchmark_rows.iloc[-1]["sma200_gap"]) > 0
    )

    eligible_for_breadth = gp.apply_universe_filter(frame, config)
    breadth_ratio = gp.select_breadth_ratio(frame, eligible_for_breadth, config)
    if (not benchmark_trend_positive) and breadth_ratio < robust.HARD_BREADTH_THRESHOLD:
        regime = "hard_defense"
        stock_exposure = float(config.exposures.hard_defense_exposure)
    elif (not benchmark_trend_positive) or breadth_ratio < robust.SOFT_BREADTH_THRESHOLD:
        regime = "soft_defense"
        stock_exposure = float(config.exposures.soft_defense_exposure)
    else:
        regime = "risk_on"
        stock_exposure = float(candidate.risk_on_exposure)

    if eligible_for_breadth.empty or stock_exposure <= 0:
        return (
            {suite.SAFE_HAVEN: 1.0},
            {
                "regime": regime,
                "stock_exposure": 0.0,
                "breadth_ratio": breadth_ratio,
                "benchmark_symbol": benchmark_symbol,
                "benchmark_trend_positive": benchmark_trend_positive,
                "candidate_count": int(len(eligible_for_breadth)),
                "selected_symbols": (),
                "selected_sectors": (),
                "sector_slot_cap": 0,
            },
        )

    scored = gp.score_candidates(frame, current_holdings, config)
    if scored.empty:
        return (
            {suite.SAFE_HAVEN: 1.0},
            {
                "regime": regime,
                "stock_exposure": 0.0,
                "breadth_ratio": breadth_ratio,
                "benchmark_symbol": benchmark_symbol,
                "benchmark_trend_positive": benchmark_trend_positive,
                "candidate_count": 0,
                "selected_symbols": (),
                "selected_sectors": (),
                "sector_slot_cap": 0,
            },
        )

    ranked = scored.sort_values(
        by=["score", "excess_mom_12_1", "trend_strength", "symbol"],
        ascending=[False, False, False, True],
    )
    per_name_target = stock_exposure / max(config.holdings_count, 1)
    sector_slot_cap = config.holdings_count if per_name_target <= 0 else max(1, int(math.floor(config.sector_cap / per_name_target)))
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
                "regime": regime,
                "stock_exposure": 0.0,
                "breadth_ratio": breadth_ratio,
                "benchmark_symbol": benchmark_symbol,
                "benchmark_trend_positive": benchmark_trend_positive,
                "candidate_count": int(len(scored)),
                "selected_symbols": (),
                "selected_sectors": (),
                "sector_slot_cap": sector_slot_cap,
            },
        )

    per_name_weight = min(float(config.single_name_cap), stock_exposure / len(selected))
    invested_weight = per_name_weight * len(selected)
    weights = {row.symbol: per_name_weight for row in selected.itertuples(index=False)}
    if invested_weight < 1.0:
        weights[suite.SAFE_HAVEN] = 1.0 - invested_weight
    return (
        weights,
        {
            "regime": regime,
            "stock_exposure": stock_exposure,
            "breadth_ratio": breadth_ratio,
            "benchmark_symbol": benchmark_symbol,
            "benchmark_trend_positive": benchmark_trend_positive,
            "candidate_count": int(len(scored)),
            "selected_symbols": tuple(selected["symbol"].tolist()),
            "selected_sectors": tuple(selected["sector"].tolist()),
            "sector_slot_cap": sector_slot_cap,
        },
    )


def run_candidate_backtest(
    raw_snapshots: Mapping[pd.Timestamp, pd.DataFrame],
    returns_matrix: pd.DataFrame,
    candidate: GeometryCandidate,
) -> dict[str, object]:
    index = returns_matrix.index
    rebalance_dates = set(raw_snapshots)
    weights_history = pd.DataFrame(0.0, index=index, columns=sorted(set(returns_matrix.columns) | {suite.SAFE_HAVEN}))
    portfolio_returns = pd.Series(0.0, index=index, name=candidate.label)
    turnover_history = pd.Series(0.0, index=index, name="turnover")
    selection_rows: list[dict[str, object]] = []
    current_weights: dict[str, float] = {suite.SAFE_HAVEN: 1.0}
    current_holdings: set[str] = set()

    for idx in range(len(index) - 1):
        date = index[idx]
        next_date = index[idx + 1]
        if date in rebalance_dates:
            target_weights, metadata = build_target_weights_with_override(raw_snapshots[date], current_holdings, candidate)
            turnover_history.at[next_date] = suite.compute_turnover(current_weights, target_weights)
            current_weights = target_weights
            current_holdings = {symbol for symbol, weight in current_weights.items() if weight > 1e-12 and symbol != suite.SAFE_HAVEN}
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
                    "family": candidate.config.family,
                }
            )
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

    return {
        "gross_returns": portfolio_returns,
        "weights_history": weights_history.loc[:, (weights_history != 0.0).any(axis=0)],
        "turnover_history": turnover_history,
        "selection_history": pd.DataFrame(selection_rows),
    }


def evaluate_candidate_rows(
    candidate: GeometryCandidate,
    context: dict[str, object],
    benchmark_returns: pd.Series,
    *,
    cost_bps_values: Iterable[float],
) -> tuple[list[dict[str, object]], dict[float, gp.StrategyArtifacts]]:
    result = run_candidate_backtest(context["raw_snapshots"], context["stock_returns_matrix"], candidate)
    rows: list[dict[str, object]] = []
    artifacts_by_cost: dict[float, gp.StrategyArtifacts] = {}
    for cost_bps in cost_bps_values:
        net_returns = result["gross_returns"] - result["turnover_history"].reindex(result["gross_returns"].index).fillna(0.0) * (float(cost_bps) / 10_000.0)
        rolling_alpha = robust.compute_rolling_capm_alpha_fast(net_returns, benchmark_returns)
        turnover_profile = robust.compute_turnover_profile(result["selection_history"], result["turnover_history"])
        sector_weights = robust.compute_average_sector_weights(result["weights_history"], result["selection_history"], context["universe_history"])
        artifacts_by_cost[float(cost_bps)] = gp.StrategyArtifacts(
            gross_returns=net_returns,
            weights_history=result["weights_history"],
            turnover_history=result["turnover_history"],
            selection_history=result["selection_history"],
            rolling_alpha=rolling_alpha,
            turnover_profile=turnover_profile,
            sector_weights=sector_weights,
        )
        for period_name, start, end in COMPARISON_PERIODS:
            metrics = robust.evaluate_period_metrics(
                net_returns,
                result["weights_history"],
                result["turnover_history"],
                benchmark_returns,
                start=start,
                end=end,
            )
            rolling_series = rolling_alpha if period_name == "Full Sample" else robust.compute_rolling_capm_alpha_fast(
                robust.slice_series_or_frame(net_returns, start, end),
                robust.slice_series_or_frame(benchmark_returns, start, end),
            )
            rows.append(
                {
                    "strategy": candidate.label,
                    "cost_bps_one_way": float(cost_bps),
                    "period": period_name,
                    **candidate_config_fields(candidate),
                    **metrics,
                    **robust.compute_rolling_alpha_summary(rolling_series),
                    **turnover_profile,
                    "avg_sector_weights_json": json.dumps({k: float(v) for k, v in sector_weights.items()}, ensure_ascii=False),
                }
            )
    return rows, artifacts_by_cost


def theoretical_stock_capacity(stock_exposure: float, config: gp.GrowthPullbackConfig, active_sector_count: int) -> float:
    return float(min(stock_exposure, config.holdings_count * config.single_name_cap, active_sector_count * config.sector_cap))


def build_deployment_monthly(
    candidate: GeometryCandidate,
    artifacts: gp.StrategyArtifacts,
    context: dict[str, object],
) -> pd.DataFrame:
    selection = artifacts.selection_history.copy()
    if selection.empty:
        return pd.DataFrame()
    selection["rebalance_date"] = pd.to_datetime(selection["rebalance_date"]).dt.tz_localize(None).dt.normalize()
    rows: list[dict[str, object]] = []
    for record in selection.itertuples(index=False):
        rebalance_date = pd.Timestamp(record.rebalance_date).normalize()
        frame = context["raw_snapshots"][rebalance_date]
        counts = v11g.stage_universe_counts(frame, candidate.config)
        weight_row = artifacts.weights_history.loc[rebalance_date].fillna(0.0)
        stock_weights = weight_row.drop(labels=[suite.SAFE_HAVEN], errors="ignore")
        stock_weights = stock_weights[stock_weights > 1e-12]
        stock_weight = float(stock_weights.sum())
        safe_haven_weight = float(weight_row.get(suite.SAFE_HAVEN, 0.0))
        target_stock_weight = float(record.stock_exposure) if pd.notna(record.stock_exposure) else 0.0
        active_sector_count = int(counts["active_sector_count"])
        theoretical_max = theoretical_stock_capacity(target_stock_weight, candidate.config, active_sector_count)
        rows.append(
            {
                "strategy": candidate.label,
                "config_name": candidate.config.name,
                "rebalance_date": str(rebalance_date.date()),
                "regime": str(record.regime),
                "selected_count": int(record.selected_count),
                "target_stock_weight": target_stock_weight,
                "realized_stock_weight": stock_weight,
                "safe_haven_weight": safe_haven_weight,
                "risk_on_target_exposure": float(candidate.risk_on_exposure),
                "theoretical_stock_max": theoretical_max,
                "fill_rate": float(stock_weight / target_stock_weight) if target_stock_weight > 1e-12 else np.nan,
                "theoretical_fill_rate": float(theoretical_max / target_stock_weight) if target_stock_weight > 1e-12 else np.nan,
                "geometry_feasible": bool(theoretical_max >= target_stock_weight - 1e-12),
                "top1_stock_weight": float(stock_weights.nlargest(1).sum()) if not stock_weights.empty else 0.0,
                "top3_stock_weight": float(stock_weights.nlargest(min(3, len(stock_weights))).sum()) if not stock_weights.empty else 0.0,
                "top5_stock_weight": float(stock_weights.nlargest(min(5, len(stock_weights))).sum()) if not stock_weights.empty else 0.0,
                "base_eligible_count": int(counts["base_eligible_count"]),
                "adv_filtered_count": int(counts["adv_filtered_count"]),
                "after_sector_count": int(counts["after_sector_count"]),
                "after_symbol_count": int(counts["after_symbol_count"]),
                "final_candidate_count": int(counts["final_candidate_count"]),
                "active_sector_count": active_sector_count,
                "underfilled_month": bool(int(record.selected_count) < int(candidate.config.holdings_count)),
                "primary_underfill_reason": v11g.classify_underfill_reason(
                    stock_exposure=target_stock_weight,
                    selected_count=int(record.selected_count),
                    holdings_count=int(candidate.config.holdings_count),
                    counts=counts,
                    sector_capacity_limit=int(round(theoretical_max / max(candidate.config.single_name_cap, 1e-12))) if candidate.config.single_name_cap > 0 else int(candidate.config.holdings_count),
                ),
            }
        )
    return pd.DataFrame(rows)


def summarize_deployment(monthly: pd.DataFrame) -> dict[str, object]:
    if monthly.empty:
        return {
            "strategy": "",
            "avg_names_held": float("nan"),
            "risk_on_avg_names": float("nan"),
            "risk_on_target_stock_weight": float("nan"),
            "risk_on_realized_stock_weight": float("nan"),
            "risk_on_fill_rate": float("nan"),
            "soft_defense_realized_stock_weight": float("nan"),
            "hard_defense_realized_stock_weight": float("nan"),
            "underfilled_month_share": float("nan"),
            "avg_top1_stock_weight": float("nan"),
            "avg_top3_stock_weight": float("nan"),
            "avg_top5_stock_weight": float("nan"),
            "avg_safe_haven_weight": float("nan"),
            "avg_theoretical_stock_max": float("nan"),
            "risk_on_geometry_feasible_share": float("nan"),
            "selected_count_distribution_json": "{}",
            "dominant_underfill_reason": "none",
        }
    risk_on = monthly.loc[monthly["regime"] == "risk_on"]
    soft = monthly.loc[monthly["regime"] == "soft_defense"]
    hard = monthly.loc[monthly["regime"] == "hard_defense"]
    reason_share = monthly.loc[monthly["underfilled_month"], "primary_underfill_reason"].value_counts(normalize=True)
    selected_dist = monthly["selected_count"].value_counts(normalize=True).sort_index()
    return {
        "strategy": str(monthly["strategy"].iloc[0]),
        "avg_names_held": float(monthly["selected_count"].mean()),
        "risk_on_avg_names": float(risk_on["selected_count"].mean()) if not risk_on.empty else np.nan,
        "risk_on_target_stock_weight": float(risk_on["target_stock_weight"].mean()) if not risk_on.empty else np.nan,
        "risk_on_realized_stock_weight": float(risk_on["realized_stock_weight"].mean()) if not risk_on.empty else np.nan,
        "risk_on_fill_rate": float(risk_on["fill_rate"].mean()) if not risk_on.empty else np.nan,
        "soft_defense_realized_stock_weight": float(soft["realized_stock_weight"].mean()) if not soft.empty else np.nan,
        "hard_defense_realized_stock_weight": float(hard["realized_stock_weight"].mean()) if not hard.empty else np.nan,
        "underfilled_month_share": float(monthly["underfilled_month"].mean()),
        "avg_top1_stock_weight": float(monthly["top1_stock_weight"].mean()),
        "avg_top3_stock_weight": float(monthly["top3_stock_weight"].mean()),
        "avg_top5_stock_weight": float(monthly["top5_stock_weight"].mean()),
        "avg_safe_haven_weight": float(monthly["safe_haven_weight"].mean()),
        "avg_theoretical_stock_max": float(monthly["theoretical_stock_max"].mean()),
        "risk_on_geometry_feasible_share": float(risk_on["geometry_feasible"].mean()) if not risk_on.empty else np.nan,
        "selected_count_distribution_json": json.dumps({str(int(k)): float(v) for k, v in selected_dist.items()}, ensure_ascii=False),
        "dominant_underfill_reason": str(reason_share.index[0]) if not reason_share.empty else "filled",
    }


def summarize_reference_deployment(label: str, artifact: dict[str, object]) -> dict[str, object]:
    weights = artifact["weights_history"].copy()
    weights = weights.fillna(0.0)
    stock_only = weights.drop(columns=[suite.SAFE_HAVEN], errors="ignore")
    stock_weight = stock_only.sum(axis=1)
    if "selection_history" in artifact:
        selection = artifact["selection_history"].copy()
        selection["rebalance_date"] = pd.to_datetime(selection["rebalance_date"]).dt.tz_localize(None).dt.normalize()
        aligned = selection.set_index("rebalance_date")
        aligned = aligned.reindex(weights.index).ffill().dropna(subset=["regime"]) if not aligned.empty else aligned
        stock_weight = stock_weight.reindex(aligned.index)
        risk_on = stock_weight.loc[aligned["regime"] == "risk_on"]
    else:
        risk_on = stock_weight.loc[stock_weight >= 0.95]
    return {
        "strategy": label,
        "risk_on_realized_stock_weight": float(risk_on.mean()) if not risk_on.empty else np.nan,
        "avg_names_held": float((stock_only > 1e-12).sum(axis=1).mean()),
    }


def add_geometry_scores(summary_df: pd.DataFrame) -> pd.DataFrame:
    scored = summary_df.copy()

    def pr(series: pd.Series, higher: bool) -> pd.Series:
        numeric = pd.to_numeric(series, errors="coerce")
        ranked = numeric.rank(method="average", pct=True) if higher else (-numeric).rank(method="average", pct=True)
        return ranked.fillna(0.0)

    scored["score_oos_rel_qqq"] = pr(scored["oos_cagr_minus_qqq"], True)
    scored["score_oos_maxdd"] = pr(scored["oos_max_drawdown"], True)
    scored["score_turnover"] = pr(scored["annual_turnover"], False)
    scored["score_fill_rate"] = pr(scored["risk_on_fill_rate"], True)
    scored["score_avg_names"] = pr(scored["avg_names_held"], True)
    scored["score_top3"] = pr(scored["avg_top3_stock_weight"], False)
    scored["score_2022"] = pr(scored["return_2022"], True)
    scored["geometry_repair_score"] = (
        scored["score_oos_rel_qqq"] * 0.25
        + scored["score_oos_maxdd"] * 0.20
        + scored["score_fill_rate"] * 0.20
        + scored["score_avg_names"] * 0.10
        + scored["score_top3"] * 0.10
        + scored["score_turnover"] * 0.10
        + scored["score_2022"] * 0.05
    )
    return scored.sort_values(
        by=["geometry_repair_score", "oos_cagr_minus_qqq", "risk_on_fill_rate", "oos_max_drawdown"],
        ascending=[False, False, False, False],
    ).reset_index(drop=True)


def build_recommendation(
    *,
    center_summary: pd.Series,
    stable_summary: pd.Series,
    explicit_cash_summary: pd.Series,
    best_overall_summary: pd.Series,
    selected_repair_summary: pd.Series,
    qqq_plus_oos: pd.Series,
) -> dict[str, object]:
    center_vs_explicit_close = bool(
        abs(float(center_summary["oos_cagr"]) - float(explicit_cash_summary["oos_cagr"])) <= 0.02
        and abs(float(center_summary["oos_max_drawdown"]) - float(explicit_cash_summary["oos_max_drawdown"])) <= 0.03
    )
    best_is_geometry_feasible = bool(float(selected_repair_summary["risk_on_fill_rate"]) >= 0.95)
    occupancy_ok = bool(
        float(selected_repair_summary["avg_names_held"]) >= 9.0
        and float(selected_repair_summary["underfilled_month_share"]) <= 0.35
    )
    concentration_ok = bool(float(selected_repair_summary["avg_top3_stock_weight"]) <= 0.55)
    performance_ok = bool(
        float(selected_repair_summary["oos_cagr_minus_qqq"]) > 0.10
        and float(selected_repair_summary["oos_max_drawdown"]) >= -0.30
        and float(selected_repair_summary["annual_turnover"]) <= 4.5
    )

    if not performance_ok:
        level = "discard"
        reason = "after geometry repair, the edge is not strong enough"
    elif center_vs_explicit_close and (not best_is_geometry_feasible or float(selected_repair_summary["oos_cagr"]) < float(qqq_plus_oos["CAGR"]) - 0.03):
        level = "cash_buffer_branch"
        reason = "current edge still looks heavily tied to explicit/implicit cash buffering rather than a clean deployable offensive geometry"
    elif best_is_geometry_feasible and occupancy_ok and concentration_ok:
        level = "research_default"
        reason = "geometry is deployable, occupancy is acceptable, and the repaired branch keeps strong relative-to-QQQ edge"
    else:
        level = "research_default_candidate"
        reason = "geometry-repaired version still has edge, but occupancy or concentration still needs more tightening"

    if float(selected_repair_summary["oos_cagr"]) >= float(qqq_plus_oos["CAGR"]) and float(selected_repair_summary["oos_max_drawdown"]) >= float(qqq_plus_oos["Max Drawdown"]):
        role = "替代者"
    elif float(selected_repair_summary["oos_cagr_minus_qqq"]) > 0 and float(selected_repair_summary["full_max_drawdown"]) >= -0.40:
        role = "并行分支"
    else:
        role = "次级实验"

    return {
        "center_strategy": str(center_summary["strategy"]),
        "stable_neighbor_strategy": str(stable_summary["strategy"]),
        "explicit_cash_buffer_control": str(explicit_cash_summary["strategy"]),
        "best_overall_score_strategy": str(best_overall_summary["strategy"]),
        "selected_geometry_repair_default": str(selected_repair_summary["strategy"]),
        "research_recommendation": level,
        "role_vs_qqq_plus_current_default": role,
        "reason": reason,
        "checks": {
            "center_oos_cagr": float(center_summary["oos_cagr"]),
            "explicit_cash_oos_cagr": float(explicit_cash_summary["oos_cagr"]),
            "center_oos_maxdd": float(center_summary["oos_max_drawdown"]),
            "explicit_cash_oos_maxdd": float(explicit_cash_summary["oos_max_drawdown"]),
            "center_vs_explicit_close": center_vs_explicit_close,
            "best_overall_score_strategy": str(best_overall_summary["strategy"]),
            "selected_oos_cagr_minus_qqq": float(selected_repair_summary["oos_cagr_minus_qqq"]),
            "selected_oos_max_drawdown": float(selected_repair_summary["oos_max_drawdown"]),
            "selected_risk_on_fill_rate": float(selected_repair_summary["risk_on_fill_rate"]),
            "selected_avg_names_held": float(selected_repair_summary["avg_names_held"]),
            "selected_underfilled_month_share": float(selected_repair_summary["underfilled_month_share"]),
            "selected_avg_top3_stock_weight": float(selected_repair_summary["avg_top3_stock_weight"]),
            "selected_annual_turnover": float(selected_repair_summary["annual_turnover"]),
        },
    }


def format_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "_empty_"
    def fmt(value):
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
        lines.append("| " + " | ".join(fmt(v) for v in row) + " |")
    return "\n".join(lines)


def write_markdown_report(
    path: Path,
    *,
    center_cfg: gp.GrowthPullbackConfig,
    stable_cfg: gp.GrowthPullbackConfig,
    repair_summary: pd.DataFrame,
    deployment_summary: pd.DataFrame,
    comparison_5bps: pd.DataFrame,
    recommendation: dict[str, object],
) -> None:
    lines = [
        "# growth_pullback_systematic_v1.2 geometry repair",
        "",
        "## Current center spec",
        f"- name={center_cfg.name}",
        f"- family={center_cfg.family}",
        f"- universe={center_cfg.universe_spec.name}",
        f"- normalization={gp.normalization_label(center_cfg.universe_spec.normalization)}",
        f"- holdings={center_cfg.holdings_count}",
        f"- single_cap={center_cfg.single_name_cap:.0%}",
        f"- sector_cap={center_cfg.sector_cap:.0%}",
        f"- hold_bonus={center_cfg.hold_bonus:.2f}",
        f"- min_adv20={center_cfg.universe_spec.min_adv20_usd/1_000_000:.0f}M",
        "",
        "## Current stable neighbor",
        f"- name={stable_cfg.name}",
        f"- single_cap={stable_cfg.single_name_cap:.0%}",
        f"- sector_cap={stable_cfg.sector_cap:.0%}",
        f"- hold_bonus={stable_cfg.hold_bonus:.2f}",
        "",
        "## Geometry repair summary (5 bps)",
        format_table(repair_summary[[
            "strategy", "full_cagr", "oos_cagr", "oos_cagr_minus_qqq", "full_max_drawdown", "oos_max_drawdown", "return_2022", "cagr_2023_plus", "annual_turnover", "avg_names_held", "risk_on_fill_rate", "geometry_repair_score"
        ]]),
        "",
        "## Deployment diagnostics",
        format_table(deployment_summary[[
            "strategy", "avg_names_held", "risk_on_avg_names", "risk_on_target_stock_weight", "risk_on_realized_stock_weight", "risk_on_fill_rate", "avg_top3_stock_weight", "avg_safe_haven_weight", "underfilled_month_share", "dominant_underfill_reason"
        ]]),
        "",
        "## Main comparison (5 bps)",
        format_table(comparison_5bps[[
            "strategy", "period", "CAGR", "Max Drawdown", "Turnover/Year", "Average Names Held", "risk_on_realized_stock_weight", "beta_vs_qqq", "alpha_ann_vs_qqq", "Information Ratio vs QQQ", "Up Capture vs QQQ", "Down Capture vs QQQ"
        ]]),
        "",
        "## Recommendation",
        f"- research_recommendation={recommendation['research_recommendation']}",
        f"- role_vs_qqq_plus_current_default={recommendation['role_vs_qqq_plus_current_default']}",
        f"- selected_geometry_repair_default={recommendation['selected_geometry_repair_default']}",
        f"- reason={recommendation['reason']}",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = parse_args()
    results_dir = Path(args.results_dir).expanduser().resolve()
    configs_dir = Path(args.configs_dir).expanduser().resolve()
    results_dir.mkdir(parents=True, exist_ok=True)
    configs_dir.mkdir(parents=True, exist_ok=True)

    center_cfg = gp.load_spec_config(CENTER_CONFIG_PATH)
    stable_cfg = gp.load_spec_config(STABLE_NEIGHBOR_CONFIG_PATH)
    candidates = build_candidates(center_cfg, stable_cfg)

    alias_dir = robust.discover_run_dir(args.alias_data_run_dir, "r1000_multifactor_defensive_*_official_monthly_v2_alias")
    context = gp.build_context(alias_dir, start=args.start, end=args.end)
    benchmark_returns = context["stock_returns_matrix"]["QQQ"].copy()

    candidate_rows: list[dict[str, object]] = []
    deployment_monthly_frames: list[pd.DataFrame] = []
    deployment_summary_rows: list[dict[str, object]] = []

    for candidate in candidates:
        rows, artifacts_by_cost = evaluate_candidate_rows(candidate, context, benchmark_returns, cost_bps_values=COST_LEVELS)
        candidate_rows.extend(rows)
        monthly = build_deployment_monthly(candidate, artifacts_by_cost[MAIN_COST_BPS], context)
        deployment_monthly_frames.append(monthly)
        deployment_summary_rows.append(summarize_deployment(monthly))

    candidate_df = pd.DataFrame(candidate_rows)
    deployment_summary_df = pd.DataFrame(deployment_summary_rows)

    reference_rows_df, reference_artifacts = gp.build_reference_rows(context, COST_LEVELS)
    reference_deployment_rows = [
        summarize_reference_deployment("qqq_plus_current_default", reference_artifacts["qqq_plus_current_default"][MAIN_COST_BPS]),
        summarize_reference_deployment("aggressive_alt_spec", reference_artifacts["aggressive_alt_spec"][MAIN_COST_BPS]),
        summarize_reference_deployment("defensive_baseline", reference_artifacts["defensive_baseline"][MAIN_COST_BPS]),
        {
            "strategy": "QQQ",
            "risk_on_realized_stock_weight": 1.0,
            "avg_names_held": 1.0,
        },
    ]
    reference_deployment_df = pd.DataFrame(reference_deployment_rows)

    qqq_oos_cagr = float(reference_rows_df.loc[(reference_rows_df["strategy"] == "QQQ") & (reference_rows_df["cost_bps_one_way"] == MAIN_COST_BPS) & (reference_rows_df["period"] == "OOS Sample"), "CAGR"].iloc[0])
    summary_rows = []
    for candidate in candidates:
        full_row = extract_period_row(candidate_df, candidate.label, MAIN_COST_BPS, "Full Sample")
        oos_row = extract_period_row(candidate_df, candidate.label, MAIN_COST_BPS, "OOS Sample")
        row_2022 = extract_period_row(candidate_df, candidate.label, MAIN_COST_BPS, "2022")
        row_2023 = extract_period_row(candidate_df, candidate.label, MAIN_COST_BPS, "2023+")
        dep = deployment_summary_df.loc[deployment_summary_df["strategy"] == candidate.label].iloc[0]
        summary_rows.append(
            {
                "strategy": candidate.label,
                "config_name": candidate.config.name,
                "risk_on_target_exposure": float(candidate.risk_on_exposure),
                "full_cagr": float(full_row["CAGR"]),
                "full_max_drawdown": float(full_row["Max Drawdown"]),
                "oos_cagr": float(oos_row["CAGR"]),
                "oos_cagr_minus_qqq": float(oos_row["CAGR"] - qqq_oos_cagr),
                "oos_max_drawdown": float(oos_row["Max Drawdown"]),
                "oos_alpha_ann_vs_qqq": float(oos_row["alpha_ann_vs_qqq"]),
                "annual_turnover": float(full_row["Turnover/Year"]),
                "return_2022": float(row_2022["Total Return"]),
                "cagr_2023_plus": float(row_2023["CAGR"]),
                **dep.to_dict(),
            }
        )
    repair_summary_df = add_geometry_scores(pd.DataFrame(summary_rows))

    center_summary = repair_summary_df.loc[repair_summary_df["strategy"] == "center_current"].iloc[0]
    stable_summary = repair_summary_df.loc[repair_summary_df["strategy"] == "local_stable_neighbor"].iloc[0]
    explicit_cash_summary = repair_summary_df.loc[repair_summary_df["strategy"] == "explicit_cash_buffer_control"].iloc[0]
    best_overall_summary = repair_summary_df.iloc[0]
    repair_pool = repair_summary_df.loc[
        repair_summary_df["strategy"].isin(["feasible_two_sector_50cap", "feasible_two_sector_50cap_single8"])
    ].copy()
    selected_repair_summary = repair_pool.iloc[0]
    qqq_plus_oos = extract_period_row(reference_rows_df, "qqq_plus_current_default", MAIN_COST_BPS, "OOS Sample")
    recommendation = build_recommendation(
        center_summary=center_summary,
        stable_summary=stable_summary,
        explicit_cash_summary=explicit_cash_summary,
        best_overall_summary=best_overall_summary,
        selected_repair_summary=selected_repair_summary,
        qqq_plus_oos=qqq_plus_oos,
    )

    comparison_df = pd.concat(
        [
            candidate_df.loc[candidate_df["cost_bps_one_way"] == MAIN_COST_BPS].copy(),
            reference_rows_df.loc[reference_rows_df["cost_bps_one_way"] == MAIN_COST_BPS].copy(),
        ],
        ignore_index=True,
    )
    comparison_df = comparison_df.merge(
        pd.concat([deployment_summary_df[["strategy", "risk_on_realized_stock_weight", "avg_names_held"]], reference_deployment_df], ignore_index=True),
        on="strategy",
        how="left",
    )

    candidate_df.to_csv(results_dir / "growth_pullback_v1_2_geometry_repair.csv", index=False)
    deployment_summary_df.to_csv(results_dir / "growth_pullback_v1_2_deployment_diagnostics.csv", index=False)
    (results_dir / "growth_pullback_v1_2_recommendation.json").write_text(json.dumps(recommendation, indent=2, ensure_ascii=False), encoding="utf-8")
    write_markdown_report(
        results_dir / "growth_pullback_v1_2_geometry_repair.md",
        center_cfg=center_cfg,
        stable_cfg=stable_cfg,
        repair_summary=repair_summary_df,
        deployment_summary=deployment_summary_df,
        comparison_5bps=comparison_df,
        recommendation=recommendation,
    )

    print(f"alias data: {alias_dir}")
    print(f"best overall score candidate: {best_overall_summary['strategy']}")
    print(f"selected geometry-repair default: {selected_repair_summary['strategy']}")
    print(f"recommendation: {recommendation['research_recommendation']}")
    print(f"results written to: {results_dir}")


if __name__ == "__main__":
    main()
