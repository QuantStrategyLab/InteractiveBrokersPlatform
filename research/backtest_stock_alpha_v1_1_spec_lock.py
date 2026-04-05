#!/usr/bin/env python3
"""
V1.1 spec-lock research for qqq_plus_stock_alpha_v1.

Goal:
- keep the strategy research-only
- converge the current offensive V1 into one frozen default spec and one aggressive alt spec
- retain the original promotion gate result from the previous round
- add a V1.1 recommendation layer: not_ready / shadow_candidate / shadow_ready
"""

from __future__ import annotations

import argparse
import json
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
import backtest_stock_alpha_v1_robustness as robust  # noqa: E402


DEFAULT_RESULTS_DIR = SCRIPT_DIR / "results"
DEFAULT_CONFIGS_DIR = SCRIPT_DIR / "configs"
MAIN_COST_BPS = 5.0
COST_LEVELS = (0.0, 5.0)
LOCAL_GRID_PERIODS = (
    ("Full Sample", None, None),
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023+", "2023-01-01", None),
)
COMPARISON_PERIODS = (
    ("Full Sample", None, None),
    ("OOS Sample", "2022-01-01", None),
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023+", "2023-01-01", None),
)
PLATEAU_AXES = (
    "universe_filter",
    "group_normalization",
    "holdings_count",
    "single_name_cap",
    "sector_cap",
    "hold_bonus",
)
SUBSPACE_AXES = ("universe_filter", "group_normalization", "holdings_count")
ORIGINAL_GATE_PLATEAU_THRESHOLD = 0.20
OOS_START = "2022-01-01"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--alias-data-run-dir",
        help="Prepared Russell data run with alias repair (defaults to newest official_monthly_v2_alias run)",
    )
    parser.add_argument(
        "--results-dir",
        default=str(DEFAULT_RESULTS_DIR),
        help="Directory where V1.1 spec-lock outputs will be written",
    )
    parser.add_argument(
        "--configs-dir",
        default=str(DEFAULT_CONFIGS_DIR),
        help="Directory where frozen/aggressive research configs will be written",
    )
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    return parser.parse_args()


def normalization_label(value: str) -> str:
    return "universe_cross_sectional" if value == "universe" else value


def normalization_from_label(value: str) -> str:
    normalized = str(value).strip().lower()
    if normalized in {"universe", "universe_cross_sectional"}:
        return "universe"
    if normalized == "sector":
        return "sector"
    raise ValueError(f"Unsupported normalization label: {value}")


def build_targeted_local_grid(base: suite.OffensiveConfig) -> list[suite.OffensiveConfig]:
    universes = (
        suite.UniverseFilterConfig("leadership_liquid", 50_000_000.0, leadership_only=True),
        suite.UniverseFilterConfig("liquid_50m", 50_000_000.0, leadership_only=False),
    )
    configs: list[suite.OffensiveConfig] = []
    for universe_filter in universes:
        for group_normalization in ("sector", "universe"):
            for holdings_count in (12, 16):
                for single_name_cap in (0.08, 0.09, 0.10):
                    for sector_cap in (0.25, 0.30, 0.35):
                        for hold_bonus in (0.05, 0.10, 0.15):
                            configs.append(
                                replace(
                                    base,
                                    name=(
                                        f"v11_{universe_filter.name}_norm_{normalization_label(group_normalization)}"
                                        f"_h{holdings_count}_cap{int(single_name_cap * 100)}"
                                        f"_sector{int(sector_cap * 100)}_hold{int(hold_bonus * 100):02d}"
                                    ),
                                    universe_filter=universe_filter,
                                    group_normalization=group_normalization,
                                    holdings_count=holdings_count,
                                    single_name_cap=single_name_cap,
                                    sector_cap=sector_cap,
                                    hold_bonus=hold_bonus,
                                )
                            )
    return configs


def spec_to_dict(config: suite.OffensiveConfig, *, role: str) -> dict[str, object]:
    return {
        "role": role,
        "strategy": suite.OFFENSIVE_NAME,
        "status": "research_only",
        "name": config.name,
        "universe_filter": config.universe_filter.name,
        "min_adv20_usd": float(config.universe_filter.min_adv20_usd),
        "leadership_only": bool(config.universe_filter.leadership_only),
        "normalization": normalization_label(config.group_normalization),
        "holdings_count": int(config.holdings_count),
        "single_name_cap": float(config.single_name_cap),
        "sector_cap": float(config.sector_cap),
        "hold_bonus": float(config.hold_bonus),
        "regime": config.regime.name,
        "benchmark_symbol": config.regime.benchmark_symbol,
        "breadth_mode": config.regime.breadth_mode,
        "breadth_symbols": list(config.regime.breadth_symbols),
        "exposures": {
            "risk_on": 1.0,
            "soft_defense": float(config.exposures.soft_defense_exposure),
            "hard_defense": float(config.exposures.hard_defense_exposure),
        },
        "breadth_thresholds": {
            "soft": robust.SOFT_BREADTH_THRESHOLD,
            "hard": robust.HARD_BREADTH_THRESHOLD,
        },
        "residual_proxy": "simple_excess_return_vs_QQQ",
        "cost_assumption_bps_one_way": MAIN_COST_BPS,
    }


def save_spec_config(path: Path, config: suite.OffensiveConfig, *, role: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(spec_to_dict(config, role=role), indent=2, ensure_ascii=False), encoding="utf-8")


def load_spec_config(path: Path) -> suite.OffensiveConfig:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return suite.OffensiveConfig(
        name=str(payload["name"]),
        universe_filter=suite.UniverseFilterConfig(
            str(payload["universe_filter"]),
            float(payload["min_adv20_usd"]),
            leadership_only=bool(payload["leadership_only"]),
        ),
        holdings_count=int(payload["holdings_count"]),
        single_name_cap=float(payload["single_name_cap"]),
        sector_cap=float(payload["sector_cap"]),
        regime=suite.RegimeConfig(
            str(payload["regime"]),
            str(payload["benchmark_symbol"]),
            str(payload["breadth_mode"]),
            tuple(payload.get("breadth_symbols", [])),
        ),
        exposures=suite.ExposureConfig(
            "100_60_0",
            float(payload["exposures"]["soft_defense"]),
            float(payload["exposures"]["hard_defense"]),
        ),
        hold_bonus=float(payload["hold_bonus"]),
        group_normalization=normalization_from_label(str(payload["normalization"])),
    )


def config_fields(config: suite.OffensiveConfig) -> dict[str, object]:
    return {
        "scenario": config.name,
        "universe_filter": config.universe_filter.name,
        "leadership_only": bool(config.universe_filter.leadership_only),
        "group_normalization": config.group_normalization,
        "group_normalization_label": normalization_label(config.group_normalization),
        "holdings_count": int(config.holdings_count),
        "single_name_cap": float(config.single_name_cap),
        "sector_cap": float(config.sector_cap),
        "hold_bonus": float(config.hold_bonus),
        "regime_name": config.regime.name,
        "benchmark_symbol": config.regime.benchmark_symbol,
        "breadth_mode": config.regime.breadth_mode,
        "soft_defense_exposure": float(config.exposures.soft_defense_exposure),
        "hard_defense_exposure": float(config.exposures.hard_defense_exposure),
    }


def percentile_rank(series: pd.Series, *, higher_is_better: bool) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    if higher_is_better:
        ranked = numeric.rank(method="average", pct=True)
    else:
        ranked = (-numeric).rank(method="average", pct=True)
    return ranked.fillna(0.0)


def build_qqq_period_metrics(benchmark_returns: pd.Series) -> dict[str, dict[str, float]]:
    weights = pd.DataFrame({"QQQ": 1.0}, index=benchmark_returns.index)
    turnover = pd.Series(0.0, index=benchmark_returns.index)
    metrics = {}
    for period_name, start, end in COMPARISON_PERIODS:
        metrics[period_name] = robust.evaluate_period_metrics(
            benchmark_returns,
            weights,
            turnover,
            benchmark_returns,
            start=start,
            end=end,
        )
    oos_rolling = robust.compute_rolling_capm_alpha_fast(
        robust.slice_series_or_frame(benchmark_returns, OOS_START, None),
        robust.slice_series_or_frame(benchmark_returns, OOS_START, None),
    )
    metrics["OOS rolling"] = robust.compute_rolling_alpha_summary(oos_rolling)
    return metrics


def summarize_candidate(
    config: suite.OffensiveConfig,
    context: dict[str, object],
    benchmark_returns: pd.Series,
) -> tuple[dict[str, object], dict[str, object], list[dict[str, object]]]:
    rows, net_returns, weights_history, turnover_history, selection_history, rolling_alpha = robust.evaluate_scenario(
        config.name,
        config,
        context,
        experiment_group="v1_1_local_grid",
        cost_bps=MAIN_COST_BPS,
    )
    rows_df = pd.DataFrame(rows)
    full_row = rows_df.loc[rows_df["period"] == "Full Sample"].iloc[0].to_dict()
    row_2022 = rows_df.loc[rows_df["period"] == "2022"].iloc[0].to_dict()
    row_2023 = rows_df.loc[rows_df["period"] == "2023+"].iloc[0].to_dict()
    oos_metrics = robust.evaluate_period_metrics(
        net_returns,
        weights_history,
        turnover_history,
        benchmark_returns,
        start=OOS_START,
        end=None,
    )
    oos_rolling = robust.compute_rolling_capm_alpha_fast(
        robust.slice_series_or_frame(net_returns, OOS_START, None),
        robust.slice_series_or_frame(benchmark_returns, OOS_START, None),
    )
    oos_rolling_summary = robust.compute_rolling_alpha_summary(oos_rolling)
    turnover_profile = robust.compute_turnover_profile(selection_history, turnover_history)
    sector_weights = robust.compute_average_sector_weights(weights_history, selection_history, context["universe_history"])

    summary_row = {
        **config_fields(config),
        "full_total_return": float(full_row["Total Return"]),
        "full_cagr": float(full_row["CAGR"]),
        "full_max_drawdown": float(full_row["Max Drawdown"]),
        "full_volatility": float(full_row["Volatility"]),
        "full_sharpe": float(full_row["Sharpe"]),
        "full_sortino": float(full_row["Sortino"]),
        "full_calmar": float(full_row["Calmar"]),
        "full_information_ratio_vs_qqq": float(full_row["information_ratio_vs_qqq"]),
        "full_beta_vs_qqq": float(full_row["beta_vs_qqq"]),
        "full_tracking_error_vs_qqq": float(full_row["tracking_error_vs_qqq"]),
        "full_up_capture_vs_qqq": float(full_row["up_capture_vs_qqq"]),
        "full_down_capture_vs_qqq": float(full_row["down_capture_vs_qqq"]),
        "full_rolling_36m_alpha_positive_ratio": float(full_row["rolling_36m_alpha_positive_ratio"]),
        "full_average_names_held": float(full_row["Average Names Held"]),
        "return_2022": float(row_2022["Total Return"]),
        "cagr_2023_plus": float(row_2023["CAGR"]),
        "oos_total_return": float(oos_metrics["Total Return"]),
        "oos_cagr": float(oos_metrics["CAGR"]),
        "oos_max_drawdown": float(oos_metrics["Max Drawdown"]),
        "oos_volatility": float(oos_metrics["Volatility"]),
        "oos_sharpe": float(oos_metrics["Sharpe"]),
        "oos_sortino": float(oos_metrics["Sortino"]),
        "oos_calmar": float(oos_metrics["Calmar"]),
        "oos_information_ratio_vs_qqq": float(oos_metrics["information_ratio_vs_qqq"]),
        "oos_beta_vs_qqq": float(oos_metrics["beta_vs_qqq"]),
        "oos_tracking_error_vs_qqq": float(oos_metrics["tracking_error_vs_qqq"]),
        "oos_up_capture_vs_qqq": float(oos_metrics["up_capture_vs_qqq"]),
        "oos_down_capture_vs_qqq": float(oos_metrics["down_capture_vs_qqq"]),
        "oos_average_names_held": float(oos_metrics["Average Names Held"]),
        "oos_rolling_36m_alpha_mean": float(oos_rolling_summary["rolling_36m_alpha_mean"]),
        "oos_rolling_36m_alpha_median": float(oos_rolling_summary["rolling_36m_alpha_median"]),
        "oos_rolling_36m_alpha_last": float(oos_rolling_summary["rolling_36m_alpha_last"]),
        "oos_rolling_36m_alpha_positive_ratio": float(oos_rolling_summary["rolling_36m_alpha_positive_ratio"]),
        "annual_turnover": float(turnover_profile["annual_turnover"]),
        "average_monthly_turnover": float(turnover_profile["average_monthly_turnover"]),
        "average_names_replaced_per_rebalance": float(turnover_profile["average_names_replaced_per_rebalance"]),
        "median_holding_duration_days": float(turnover_profile["median_holding_duration_days"]),
        "top5_continuity": float(turnover_profile["top5_continuity"]),
        "avg_sector_weights_json": json.dumps({k: float(v) for k, v in sector_weights.items()}, ensure_ascii=False),
    }
    artifact = {
        "config": config,
        "rows_df": rows_df,
        "net_returns": net_returns,
        "weights_history": weights_history,
        "turnover_history": turnover_history,
        "selection_history": selection_history,
        "rolling_alpha_full": rolling_alpha,
        "rolling_alpha_oos": oos_rolling,
        "summary_row": summary_row,
    }
    export_rows: list[dict[str, object]] = []
    for period_name, start, end in LOCAL_GRID_PERIODS:
        metrics = robust.evaluate_period_metrics(
            net_returns,
            weights_history,
            turnover_history,
            benchmark_returns,
            start=start,
            end=end,
        )
        export_rows.append(
            {
                "experiment_group": "v1_1_local_grid",
                "cost_bps_one_way": MAIN_COST_BPS,
                "period": period_name,
                **config_fields(config),
                **metrics,
                "rolling_36m_alpha_positive_ratio": float(summary_row["full_rolling_36m_alpha_positive_ratio"]),
            }
        )
    export_rows.append(
        {
            "experiment_group": "v1_1_local_grid",
            "cost_bps_one_way": MAIN_COST_BPS,
            "period": "OOS Sample",
            **config_fields(config),
            **oos_metrics,
            "rolling_36m_alpha_positive_ratio": float(summary_row["oos_rolling_36m_alpha_positive_ratio"]),
        }
    )
    return summary_row, artifact, export_rows


def build_plateau_table(selection_df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, float], pd.DataFrame]:
    frame = selection_df.copy()
    best_cagr = float(frame["full_cagr"].max())
    top_decile_cut = float(frame["full_cagr"].quantile(0.90))
    masks = {
        "top_decile_full_cagr": frame["full_cagr"] >= top_decile_cut,
        "within_100bps_best_cagr_ir_positive": (frame["full_cagr"] >= best_cagr - 0.01) & (frame["full_information_ratio_vs_qqq"] > 0),
        "within_200bps_best_cagr_ir_positive": (frame["full_cagr"] >= best_cagr - 0.02) & (frame["full_information_ratio_vs_qqq"] > 0),
    }

    rows: list[dict[str, object]] = []
    overall_stats: dict[str, float] = {"best_full_cagr": best_cagr, "top_decile_cut": top_decile_cut}
    for neighborhood_name, mask in masks.items():
        overall_stats[f"{neighborhood_name}_count"] = int(mask.sum())
        overall_stats[f"{neighborhood_name}_share"] = float(mask.mean())
        rows.append(
            {
                "scope": "overall",
                "dimension": "all",
                "value": "all",
                "neighborhood": neighborhood_name,
                "count": int(mask.sum()),
                "total": int(len(frame)),
                "share": float(mask.mean()),
            }
        )
        for axis in PLATEAU_AXES:
            for axis_value, group in frame.groupby(axis, dropna=False):
                group_mask = mask.loc[group.index]
                rows.append(
                    {
                        "scope": "axis",
                        "dimension": axis,
                        "value": axis_value,
                        "neighborhood": neighborhood_name,
                        "count": int(group_mask.sum()),
                        "total": int(len(group)),
                        "share": float(group_mask.mean()),
                    }
                )
        for key, group in frame.groupby(list(SUBSPACE_AXES), dropna=False):
            if not isinstance(key, tuple):
                key = (key,)
            label = " | ".join(str(part) for part in key)
            group_mask = mask.loc[group.index]
            rows.append(
                {
                    "scope": "subspace",
                    "dimension": "+".join(SUBSPACE_AXES),
                    "value": label,
                    "neighborhood": neighborhood_name,
                    "count": int(group_mask.sum()),
                    "total": int(len(group)),
                    "share": float(group_mask.mean()),
                }
            )

    candidate_rows = []
    grouped = frame.groupby(list(SUBSPACE_AXES), dropna=False)
    for key, group in grouped:
        top_share = float(masks["top_decile_full_cagr"].loc[group.index].mean())
        share_100 = float(masks["within_100bps_best_cagr_ir_positive"].loc[group.index].mean())
        share_200 = float(masks["within_200bps_best_cagr_ir_positive"].loc[group.index].mean())
        frame.loc[group.index, "top_decile_neighborhood_share"] = top_share
        frame.loc[group.index, "plateau_100bps_share"] = share_100
        frame.loc[group.index, "plateau_200bps_share"] = share_200
        frame.loc[group.index, "plateau_subspace_label"] = " | ".join(str(part) for part in key)
        candidate_rows.append(
            {
                "scope": "candidate_subspace",
                "dimension": "+".join(SUBSPACE_AXES),
                "value": " | ".join(str(part) for part in key),
                "neighborhood": "top_decile_full_cagr",
                "count": int(round(top_share * len(group))),
                "total": int(len(group)),
                "share": top_share,
            }
        )
        candidate_rows.append(
            {
                "scope": "candidate_subspace",
                "dimension": "+".join(SUBSPACE_AXES),
                "value": " | ".join(str(part) for part in key),
                "neighborhood": "within_100bps_best_cagr_ir_positive",
                "count": int(round(share_100 * len(group))),
                "total": int(len(group)),
                "share": share_100,
            }
        )
        candidate_rows.append(
            {
                "scope": "candidate_subspace",
                "dimension": "+".join(SUBSPACE_AXES),
                "value": " | ".join(str(part) for part in key),
                "neighborhood": "within_200bps_best_cagr_ir_positive",
                "count": int(round(share_200 * len(group))),
                "total": int(len(group)),
                "share": share_200,
            }
        )

    plateau_df = pd.DataFrame(rows + candidate_rows)
    return plateau_df, overall_stats, frame


def add_selection_scores(selection_df: pd.DataFrame, previous_default: suite.OffensiveConfig, qqq_oos_cagr: float) -> pd.DataFrame:
    frame = selection_df.copy()
    changed_dimensions = (
        (frame["universe_filter"] != previous_default.universe_filter.name).astype(int)
        + (frame["group_normalization"] != previous_default.group_normalization).astype(int)
        + (frame["holdings_count"] != previous_default.holdings_count).astype(int)
        + (frame["single_name_cap"] != previous_default.single_name_cap).astype(int)
        + (frame["sector_cap"] != previous_default.sector_cap).astype(int)
        + (frame["hold_bonus"] != previous_default.hold_bonus).astype(int)
    )
    frame["complexity_score"] = 1.0 - (changed_dimensions / 6.0)
    frame["oos_cagr_minus_qqq"] = frame["oos_cagr"] - float(qqq_oos_cagr)

    frame["score_oos_cagr_minus_qqq"] = percentile_rank(frame["oos_cagr_minus_qqq"], higher_is_better=True)
    frame["score_oos_positive_alpha_ratio"] = percentile_rank(frame["oos_rolling_36m_alpha_positive_ratio"], higher_is_better=True)
    frame["score_plateau_200bps_share"] = percentile_rank(frame["plateau_200bps_share"], higher_is_better=True)
    frame["score_oos_drawdown"] = percentile_rank(frame["oos_max_drawdown"], higher_is_better=True)
    frame["score_2022"] = percentile_rank(frame["return_2022"], higher_is_better=True)
    frame["score_2023_plus"] = percentile_rank(frame["cagr_2023_plus"], higher_is_better=True)
    frame["score_turnover"] = percentile_rank(frame["annual_turnover"], higher_is_better=False)
    frame["score_complexity"] = frame["complexity_score"]

    frame["robustness_score"] = (
        0.30 * frame["score_oos_cagr_minus_qqq"]
        + 0.20 * frame["score_oos_positive_alpha_ratio"]
        + 0.15 * frame["score_plateau_200bps_share"]
        + 0.10 * frame["score_oos_drawdown"]
        + 0.10 * frame["score_2022"]
        + 0.07 * frame["score_2023_plus"]
        + 0.05 * frame["score_turnover"]
        + 0.03 * frame["score_complexity"]
    )
    frame["robustness_rank"] = frame["robustness_score"].rank(method="min", ascending=False).astype(int)
    frame["full_cagr_rank"] = frame["full_cagr"].rank(method="min", ascending=False).astype(int)
    frame["full_total_return_rank"] = frame["full_total_return"].rank(method="min", ascending=False).astype(int)
    frame["core_viable"] = (
        (frame["oos_cagr_minus_qqq"] > 0)
        & (frame["oos_rolling_36m_alpha_positive_ratio"] >= 0.60)
        & (frame["oos_max_drawdown"] >= -0.40)
        & (frame["annual_turnover"] <= 5.0)
        & (frame["full_information_ratio_vs_qqq"] > 0)
    )
    return frame.sort_values(
        by=["robustness_score", "oos_cagr_minus_qqq", "full_cagr"],
        ascending=[False, False, False],
    ).reset_index(drop=True)


def evaluate_spy_sanity(
    configs: Iterable[suite.OffensiveConfig],
    context: dict[str, object],
    benchmark_returns: pd.Series,
    qqq_oos_cagr: float,
) -> pd.DataFrame:
    rows = []
    for config in configs:
        spy_config = replace(
            config,
            name=f"{config.name}__spy_sanity",
            regime=suite.RegimeConfig("spy_breadth", "SPY", "broad"),
        )
        _rows, net_returns, weights_history, turnover_history, _selection_history, _rolling_alpha = robust.evaluate_scenario(
            spy_config.name,
            spy_config,
            context,
            experiment_group="spy_sanity_check",
            cost_bps=MAIN_COST_BPS,
        )
        rows_df = pd.DataFrame(_rows)
        full_row = rows_df.loc[rows_df["period"] == "Full Sample"].iloc[0]
        oos_metrics = robust.evaluate_period_metrics(
            net_returns,
            weights_history,
            turnover_history,
            benchmark_returns,
            start=OOS_START,
            end=None,
        )
        rows.append(
            {
                **config_fields(config),
                "scenario": config.name,
                "spy_sanity_full_cagr": float(full_row["CAGR"]),
                "spy_sanity_full_ir_vs_qqq": float(full_row["information_ratio_vs_qqq"]),
                "spy_sanity_full_maxdd": float(full_row["Max Drawdown"]),
                "spy_sanity_oos_cagr": float(oos_metrics["CAGR"]),
                "spy_sanity_oos_cagr_minus_qqq": float(oos_metrics["CAGR"] - qqq_oos_cagr),
                "spy_sanity_oos_maxdd": float(oos_metrics["Max Drawdown"]),
                "spy_sanity_pass": bool(
                    float(full_row["information_ratio_vs_qqq"]) > 0
                    and float(oos_metrics["CAGR"] - qqq_oos_cagr) > -0.02
                ),
            }
        )
    return pd.DataFrame(rows)


def pick_specs(selection_df: pd.DataFrame, spy_sanity_df: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.DataFrame]:
    ranked = selection_df.copy()
    if not spy_sanity_df.empty:
        ranked = ranked.merge(
            spy_sanity_df[["scenario", "spy_sanity_full_cagr", "spy_sanity_full_ir_vs_qqq", "spy_sanity_oos_cagr", "spy_sanity_oos_cagr_minus_qqq", "spy_sanity_pass"]],
            on="scenario",
            how="left",
        )
    ranked["spy_sanity_pass"] = ranked["spy_sanity_pass"].fillna(True)

    default_pool = ranked.loc[ranked["core_viable"] & ranked["spy_sanity_pass"]].copy()
    if default_pool.empty:
        default_pool = ranked.loc[ranked["core_viable"]].copy()
    if default_pool.empty:
        default_pool = ranked.copy()
    default_row = default_pool.sort_values(
        by=["robustness_score", "plateau_200bps_share", "oos_cagr_minus_qqq", "full_cagr"],
        ascending=[False, False, False, False],
    ).iloc[0]

    aggressive_pool = ranked.loc[ranked["core_viable"]].copy()
    if aggressive_pool.empty:
        aggressive_pool = ranked.copy()
    aggressive_pool = aggressive_pool.loc[aggressive_pool["scenario"] != default_row["scenario"]].copy()
    aggressive_row = aggressive_pool.sort_values(
        by=["full_cagr", "oos_cagr_minus_qqq", "cagr_2023_plus", "robustness_score"],
        ascending=[False, False, False, False],
    ).iloc[0]
    return default_row, aggressive_row, ranked


def build_strategy_label(name: str) -> str:
    if name == suite.OFFENSIVE_NAME:
        return "previous_offensive_default"
    return name


def evaluate_final_strategy_rows(
    label: str,
    config: suite.OffensiveConfig,
    context: dict[str, object],
    benchmark_returns: pd.Series,
    *,
    cost_bps_values: Iterable[float],
) -> tuple[list[dict[str, object]], dict[float, dict[str, object]]]:
    rows: list[dict[str, object]] = []
    artifacts_by_cost: dict[float, dict[str, object]] = {}
    for cost_bps in cost_bps_values:
        _rows, net_returns, weights_history, turnover_history, selection_history, rolling_alpha = robust.evaluate_scenario(
            label,
            config,
            context,
            experiment_group="final_comparison",
            cost_bps=cost_bps,
        )
        turnover_profile = robust.compute_turnover_profile(selection_history, turnover_history)
        sector_weights = robust.compute_average_sector_weights(weights_history, selection_history, context["universe_history"])
        artifacts_by_cost[float(cost_bps)] = {
            "net_returns": net_returns,
            "weights_history": weights_history,
            "turnover_history": turnover_history,
            "selection_history": selection_history,
            "rolling_alpha": rolling_alpha,
            "turnover_profile": turnover_profile,
            "sector_weights": sector_weights,
        }
        for period_name, start, end in COMPARISON_PERIODS:
            metrics = robust.evaluate_period_metrics(
                net_returns,
                weights_history,
                turnover_history,
                benchmark_returns,
                start=start,
                end=end,
            )
            rolling_series = rolling_alpha if period_name == "Full Sample" else robust.compute_rolling_capm_alpha_fast(
                robust.slice_series_or_frame(net_returns, start, end),
                robust.slice_series_or_frame(benchmark_returns, start, end),
            )
            rolling_summary = robust.compute_rolling_alpha_summary(rolling_series)
            rows.append(
                {
                    "experiment_group": "final_comparison",
                    "strategy": label,
                    "cost_bps_one_way": float(cost_bps),
                    "period": period_name,
                    **config_fields(config),
                    **metrics,
                    **rolling_summary,
                    **turnover_profile,
                    "avg_sector_weights_json": json.dumps({k: float(v) for k, v in sector_weights.items()}, ensure_ascii=False),
                }
            )
    return rows, artifacts_by_cost


def evaluate_defensive_rows(
    context: dict[str, object],
    *,
    cost_bps_values: Iterable[float],
) -> tuple[list[dict[str, object]], dict[float, dict[str, object]]]:
    rows: list[dict[str, object]] = []
    artifacts_by_cost: dict[float, dict[str, object]] = {}
    benchmark_returns = context["stock_returns_matrix"]["QQQ"].copy()
    for cost_bps in cost_bps_values:
        result = robust.run_defensive_backtest(
            context["stock_price_history"],
            context["universe_history"],
            start_date=str(context["master_index"][0].date()),
            end_date=str(context["master_index"][-1].date()),
            turnover_cost_bps=float(cost_bps),
        )
        portfolio_returns = result["portfolio_returns"].reindex(context["master_index"]).fillna(0.0)
        weights_history = result["weights_history"].reindex(context["master_index"]).fillna(0.0)
        turnover_history = result["turnover_history"].reindex(context["master_index"]).fillna(0.0)
        rolling_alpha = robust.compute_rolling_capm_alpha_fast(portfolio_returns, benchmark_returns)
        turnover_profile = {
            "annual_turnover": float(result.get("summary", {}).get("Turnover/Year", np.nan)),
            "average_monthly_turnover": float(turnover_history.groupby(turnover_history.index.to_period("M")).sum().mean()),
            "average_names_replaced_per_rebalance": float("nan"),
            "median_holding_duration_days": float("nan"),
            "top5_continuity": float("nan"),
        }
        artifacts_by_cost[float(cost_bps)] = {
            "net_returns": portfolio_returns,
            "weights_history": weights_history,
            "turnover_history": turnover_history,
            "rolling_alpha": rolling_alpha,
            "turnover_profile": turnover_profile,
        }
        for period_name, start, end in COMPARISON_PERIODS:
            metrics = robust.evaluate_period_metrics(
                portfolio_returns,
                weights_history,
                turnover_history,
                benchmark_returns,
                start=start,
                end=end,
            )
            rolling_series = rolling_alpha if period_name == "Full Sample" else robust.compute_rolling_capm_alpha_fast(
                robust.slice_series_or_frame(portfolio_returns, start, end),
                robust.slice_series_or_frame(benchmark_returns, start, end),
            )
            rolling_summary = robust.compute_rolling_alpha_summary(rolling_series)
            rows.append(
                {
                    "experiment_group": "final_comparison",
                    "strategy": "defensive_baseline",
                    "cost_bps_one_way": float(cost_bps),
                    "period": period_name,
                    "universe_filter": "IWB_proxy_russell_1000",
                    "leadership_only": False,
                    "group_normalization": "industry_like_sector_zscore",
                    "group_normalization_label": "industry_like_sector_zscore",
                    "holdings_count": 24,
                    "single_name_cap": 0.06,
                    "sector_cap": 0.20,
                    "hold_bonus": 0.15,
                    "regime_name": "spy_breadth_defensive",
                    "benchmark_symbol": "SPY",
                    "breadth_mode": "broad",
                    "soft_defense_exposure": 0.50,
                    "hard_defense_exposure": 0.10,
                    **metrics,
                    **rolling_summary,
                    **turnover_profile,
                    "avg_sector_weights_json": json.dumps({}, ensure_ascii=False),
                }
            )
    return rows, artifacts_by_cost


def evaluate_qqq_rows(benchmark_returns: pd.Series, *, cost_bps_values: Iterable[float]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    weights = pd.DataFrame({"QQQ": 1.0}, index=benchmark_returns.index)
    turnover = pd.Series(0.0, index=benchmark_returns.index)
    for cost_bps in cost_bps_values:
        for period_name, start, end in COMPARISON_PERIODS:
            metrics = robust.evaluate_period_metrics(
                benchmark_returns,
                weights,
                turnover,
                benchmark_returns,
                start=start,
                end=end,
            )
            rolling_series = robust.compute_rolling_capm_alpha_fast(
                robust.slice_series_or_frame(benchmark_returns, start, end),
                robust.slice_series_or_frame(benchmark_returns, start, end),
            )
            rolling_summary = robust.compute_rolling_alpha_summary(rolling_series)
            rows.append(
                {
                    "experiment_group": "final_comparison",
                    "strategy": "QQQ",
                    "cost_bps_one_way": float(cost_bps),
                    "period": period_name,
                    "universe_filter": "QQQ",
                    "leadership_only": False,
                    "group_normalization": "n/a",
                    "group_normalization_label": "n/a",
                    "holdings_count": 1,
                    "single_name_cap": 1.0,
                    "sector_cap": 1.0,
                    "hold_bonus": 0.0,
                    "regime_name": "buy_and_hold",
                    "benchmark_symbol": "QQQ",
                    "breadth_mode": "n/a",
                    "soft_defense_exposure": 1.0,
                    "hard_defense_exposure": 1.0,
                    **metrics,
                    **rolling_summary,
                    "annual_turnover": 0.0,
                    "average_monthly_turnover": 0.0,
                    "average_names_replaced_per_rebalance": 0.0,
                    "median_holding_duration_days": float("nan"),
                    "top5_continuity": float("nan"),
                    "avg_sector_weights_json": json.dumps({}, ensure_ascii=False),
                }
            )
    return rows


def build_attribution_rows(
    *,
    context: dict[str, object],
    benchmark_returns: pd.Series,
    final_artifacts: Mapping[str, dict[str, object]],
    defensive_artifact: Mapping[str, object],
) -> pd.DataFrame:
    rows = []
    for strategy_name, artifact in (
        ("defensive_baseline", defensive_artifact),
        *[(name, artifact) for name, artifact in final_artifacts.items()],
    ):
        relative = robust.compute_relative_stats(artifact["net_returns"], benchmark_returns)
        avg_names = float((artifact["weights_history"].drop(columns=[suite.SAFE_HAVEN], errors="ignore").fillna(0.0) > 1e-12).sum(axis=1).mean())
        rows.append(
            {
                "strategy": strategy_name,
                **relative,
                "turnover": float(artifact.get("turnover_profile", {}).get("annual_turnover", np.nan)),
                "average_names_held": avg_names,
                "avg_sector_weights_json": json.dumps({k: float(v) for k, v in artifact.get("sector_weights", pd.Series(dtype=float)).items()}, ensure_ascii=False),
                "active_share_vs_qqq": float("nan"),
            }
        )
    return pd.DataFrame(rows)


def build_recommendation(
    *,
    previous_gate: Mapping[str, object],
    local_plateau_share: float,
    default_row: pd.Series,
    aggressive_row: pd.Series,
    qqq_oos_cagr: float,
) -> dict[str, object]:
    original_gate_v11_pass = bool(
        float(default_row["oos_rolling_36m_alpha_positive_ratio"]) >= 0.60
        and float(default_row["oos_cagr"]) >= float(qqq_oos_cagr)
        and float(default_row["oos_max_drawdown"]) >= -0.40
        and float(default_row["annual_turnover"]) <= 5.0
        and float(local_plateau_share) >= ORIGINAL_GATE_PLATEAU_THRESHOLD
    )
    original_gate_v11 = {
        "recommendation": "yes_shadow_tracking" if original_gate_v11_pass else "no_shadow_tracking",
        "oos_positive_rolling_alpha_ratio": float(default_row["oos_rolling_36m_alpha_positive_ratio"]),
        "oos_cagr_minus_qqq_5bps": float(default_row["oos_cagr"] - qqq_oos_cagr),
        "oos_max_drawdown": float(default_row["oos_max_drawdown"]),
        "annual_turnover": float(default_row["annual_turnover"]),
        "plateau_200bps_share": float(local_plateau_share),
        "thresholds": {
            "oos_positive_alpha_ratio_min": 0.60,
            "oos_cagr_not_below_qqq": True,
            "oos_max_drawdown_floor": -0.40,
            "annual_turnover_cap": 5.0,
            "plateau_200bps_share_min": ORIGINAL_GATE_PLATEAU_THRESHOLD,
        },
    }

    if original_gate_v11_pass and float(default_row["plateau_200bps_share"]) >= 0.30 and bool(default_row.get("spy_sanity_pass", True)):
        v11_level = "shadow_ready"
        reason = "default frozen spec passes original gate and sits on a wider local platform"
    elif (
        float(default_row["oos_rolling_36m_alpha_positive_ratio"]) >= 0.80
        and float(default_row["oos_cagr"] - qqq_oos_cagr) > 0.05
        and float(default_row["annual_turnover"]) <= 5.0
        and float(local_plateau_share) >= 0.18
        and bool(default_row.get("spy_sanity_pass", True))
    ):
        v11_level = "shadow_candidate"
        reason = "spec is close to locked and OOS is strong, but original gate is not fully cleared"
    else:
        v11_level = "not_ready"
        reason = "edge exists but spec-lock / platform width is still insufficient"

    return {
        "original_gate_previous_round": dict(previous_gate),
        "original_gate_v1_1_local_grid": original_gate_v11,
        "v1_1_recommendation": v11_level,
        "reason": reason,
        "default_frozen_spec": str(default_row["scenario"]),
        "aggressive_alt_spec": str(aggressive_row["scenario"]),
    }


def format_markdown_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "_empty_"

    def fmt(value) -> str:
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
        lines.append("| " + " | ".join(fmt(value) for value in row) + " |")
    return "\n".join(lines)


def write_markdown_report(
    path: Path,
    *,
    previous_default_row: pd.Series,
    default_row: pd.Series,
    aggressive_row: pd.Series,
    selection_df: pd.DataFrame,
    plateau_df: pd.DataFrame,
    plateau_stats: Mapping[str, float],
    oos_df: pd.DataFrame,
    attribution_df: pd.DataFrame,
    spy_sanity_df: pd.DataFrame,
    recommendation: Mapping[str, object],
) -> None:
    selection_top = selection_df.head(12)[[
        "scenario",
        "robustness_score",
        "robustness_rank",
        "full_cagr_rank",
        "universe_filter",
        "group_normalization_label",
        "holdings_count",
        "single_name_cap",
        "sector_cap",
        "hold_bonus",
        "oos_cagr_minus_qqq",
        "plateau_200bps_share",
        "oos_max_drawdown",
        "annual_turnover",
        "return_2022",
        "cagr_2023_plus",
    ]]
    overall_plateau = plateau_df.loc[plateau_df["scope"] == "overall", ["neighborhood", "count", "total", "share"]]
    plateau_axes = plateau_df.loc[
        (plateau_df["scope"] == "axis")
        & (plateau_df["dimension"].isin(["universe_filter", "group_normalization", "holdings_count", "hold_bonus"]))
        & (plateau_df["neighborhood"] == "within_200bps_best_cagr_ir_positive"),
        ["dimension", "value", "share", "count", "total"],
    ].sort_values(["dimension", "share"], ascending=[True, False])

    comparison_view = oos_df.loc[
        (oos_df["cost_bps_one_way"] == MAIN_COST_BPS)
        & (oos_df["period"].isin(["Full Sample", "OOS Sample", "2022", "2023+"]))
        & (oos_df["strategy"].isin(["previous_offensive_default", "default_frozen_spec", "aggressive_alt_spec", "defensive_baseline", "QQQ"])),
        ["strategy", "period", "CAGR", "Total Return", "Max Drawdown", "Sharpe", "Information Ratio vs QQQ", "rolling_36m_alpha_positive_ratio", "annual_turnover"],
    ]

    lines = [
        "# qqq_plus_stock_alpha_v1.1 spec lock",
        "",
        "## 结论先看",
        f"- previous_default={previous_default_row['scenario']}",
        f"- default_frozen_spec={default_row['scenario']}",
        f"- aggressive_alt_spec={aggressive_row['scenario']}",
        f"- 原 promotion gate（上一轮）={recommendation['original_gate_previous_round']['recommendation']}",
        f"- 原 promotion gate（V1.1 局部收敛空间）={recommendation['original_gate_v1_1_local_grid']['recommendation']}",
        f"- V1.1 recommendation layer={recommendation['v1_1_recommendation']}",
        f"- reason={recommendation['reason']}",
        "",
        "## 选型规则（robustness_score）",
        "- 0.30 * pct_rank(OOS CAGR - QQQ)",
        "- 0.20 * pct_rank(OOS rolling 36m alpha > 0 ratio)",
        "- 0.15 * plateau_200bps_share（同 universe+normalization+holdings 子空间）",
        "- 0.10 * pct_rank(OOS MaxDD，越浅越好)",
        "- 0.10 * pct_rank(2022 return)",
        "- 0.07 * pct_rank(2023+ CAGR)",
        "- 0.05 * pct_rank(annual turnover，越低越好)",
        "- 0.03 * complexity_score（相对上一轮默认规格的改动越少越高）",
        "",
        "## Top candidates by robustness_score",
        format_markdown_table(selection_top),
        "",
        "## Plateau 复核",
        format_markdown_table(overall_plateau),
        "",
        "### Plateau by key axis (within 200bps)",
        format_markdown_table(plateau_axes),
        "",
        "## spy_breadth sanity check（仅前 3 名）",
        format_markdown_table(spy_sanity_df[[
            "scenario",
            "spy_sanity_full_cagr",
            "spy_sanity_full_ir_vs_qqq",
            "spy_sanity_oos_cagr",
            "spy_sanity_oos_cagr_minus_qqq",
            "spy_sanity_pass",
        ]]),
        "",
        "## Full / OOS / 2022 / 2023+（5 bps）",
        format_markdown_table(comparison_view),
        "",
        "## Relative-to-QQQ attribution",
        format_markdown_table(attribution_df[[
            "strategy",
            "beta_vs_qqq",
            "alpha_ann_vs_qqq",
            "tracking_error_vs_qqq",
            "information_ratio_vs_qqq",
            "up_capture_vs_qqq",
            "down_capture_vs_qqq",
            "turnover",
            "average_names_held",
            "active_share_vs_qqq",
        ]]),
        "",
        "## Plateau key stats",
        f"- old_round_plateau_200bps_share={recommendation['original_gate_previous_round'].get('plateau_200bps_share', float('nan')):.1%}",
        f"- v1_1_local_grid_plateau_200bps_share={plateau_stats['within_200bps_best_cagr_ir_positive_share']:.1%}",
        f"- within_100bps_share={plateau_stats['within_100bps_best_cagr_ir_positive_share']:.1%}",
        f"- top_decile_share={plateau_stats['top_decile_full_cagr_share']:.1%}",
        "",
        "## Active share note",
        "- active_share_vs_QQQ 继续显式记为 NaN，因为当前公开数据链路没有 QQQ 历史 constituent weights。",
        "- 如果后续拿到 QQQ 历史 constituent weights，可在同一回测日期轴上对比组合权重与 QQQ 成分权重，补做真正 active share。",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = parse_args()
    results_dir = Path(args.results_dir).expanduser().resolve()
    configs_dir = Path(args.configs_dir).expanduser().resolve()
    results_dir.mkdir(parents=True, exist_ok=True)
    configs_dir.mkdir(parents=True, exist_ok=True)

    alias_dir = robust.discover_run_dir(args.alias_data_run_dir, "r1000_multifactor_defensive_*_official_monthly_v2_alias")
    _universe, _prices, prepared_start, prepared_end = suite.discover_prepared_data(alias_dir)
    common_start = pd.Timestamp(args.start or prepared_start).normalize()
    common_end = pd.Timestamp(args.end or prepared_end).normalize()
    etf_frames = suite.download_etf_ohlcv(
        ("QQQ", "SPY", "XLK", "SMH"),
        start=str(common_start.date()),
        end=str((common_end + pd.Timedelta(days=1)).date()),
    )
    context = robust.prepare_context(alias_dir, etf_frames=etf_frames, start_date=common_start, end_date=common_end)
    benchmark_returns = context["stock_returns_matrix"]["QQQ"].copy()
    qqq_period_metrics = build_qqq_period_metrics(benchmark_returns)
    qqq_oos_cagr = float(qqq_period_metrics["OOS Sample"]["CAGR"])

    previous_gate_path = results_dir / "stock_alpha_v1_promotion_gate.json"
    if previous_gate_path.exists():
        previous_gate = json.loads(previous_gate_path.read_text(encoding="utf-8"))
    else:
        previous_gate = {
            "recommendation": "unknown",
            "plateau_200bps_share": float("nan"),
        }

    previous_default_config = robust.build_base_candidate()
    selection_rows = []
    artifacts: dict[str, dict[str, object]] = {}
    spec_lock_rows = []
    for config in build_targeted_local_grid(previous_default_config):
        summary_row, artifact, export_rows = summarize_candidate(config, context, benchmark_returns)
        selection_rows.append(summary_row)
        artifacts[config.name] = artifact
        spec_lock_rows.extend(export_rows)

    selection_df = pd.DataFrame(selection_rows)
    plateau_df, plateau_stats, selection_with_plateau = build_plateau_table(selection_df)
    selection_scored = add_selection_scores(selection_with_plateau, previous_default_config, qqq_oos_cagr)

    top3_configs = [artifacts[name]["config"] for name in selection_scored.head(3)["scenario"].tolist()]
    spy_sanity_df = evaluate_spy_sanity(top3_configs, context, benchmark_returns, qqq_oos_cagr)
    default_row, aggressive_row, selection_ranked = pick_specs(selection_scored, spy_sanity_df)

    previous_default_label = build_strategy_label(suite.OFFENSIVE_NAME)
    previous_rows, previous_artifacts = evaluate_final_strategy_rows(
        previous_default_label,
        previous_default_config,
        context,
        benchmark_returns,
        cost_bps_values=COST_LEVELS,
    )
    default_rows, default_artifacts = evaluate_final_strategy_rows(
        "default_frozen_spec",
        artifacts[str(default_row["scenario"])]["config"],
        context,
        benchmark_returns,
        cost_bps_values=COST_LEVELS,
    )
    aggressive_rows, aggressive_artifacts = evaluate_final_strategy_rows(
        "aggressive_alt_spec",
        artifacts[str(aggressive_row["scenario"])]["config"],
        context,
        benchmark_returns,
        cost_bps_values=COST_LEVELS,
    )
    defensive_rows, defensive_artifacts = evaluate_defensive_rows(context, cost_bps_values=COST_LEVELS)
    qqq_rows = evaluate_qqq_rows(benchmark_returns, cost_bps_values=COST_LEVELS)

    final_comparison_df = pd.DataFrame(previous_rows + default_rows + aggressive_rows + defensive_rows + qqq_rows)
    spec_lock_df = pd.concat([pd.DataFrame(spec_lock_rows), pd.DataFrame(previous_rows + default_rows + aggressive_rows)], ignore_index=True)
    spec_lock_df = pd.concat([spec_lock_df, spy_sanity_df.assign(experiment_group="spy_sanity_check")], ignore_index=True, sort=False)

    oos_df = final_comparison_df.loc[
        final_comparison_df["period"].isin(["Full Sample", "OOS Sample", "2022", "2023+"])
    ].copy()

    attribution_df = build_attribution_rows(
        context=context,
        benchmark_returns=benchmark_returns,
        final_artifacts={
            previous_default_label: previous_artifacts[MAIN_COST_BPS],
            "default_frozen_spec": default_artifacts[MAIN_COST_BPS],
            "aggressive_alt_spec": aggressive_artifacts[MAIN_COST_BPS],
        },
        defensive_artifact=defensive_artifacts[MAIN_COST_BPS],
    )

    recommendation = build_recommendation(
        previous_gate=previous_gate,
        local_plateau_share=float(plateau_stats["within_200bps_best_cagr_ir_positive_share"]),
        default_row=default_row,
        aggressive_row=aggressive_row,
        qqq_oos_cagr=qqq_oos_cagr,
    )

    selection_ranked.to_csv(results_dir / "stock_alpha_v1_1_selection_score.csv", index=False)
    plateau_df.to_csv(results_dir / "stock_alpha_v1_1_plateau.csv", index=False)
    oos_df.to_csv(results_dir / "stock_alpha_v1_1_oos.csv", index=False)
    spec_lock_df.to_csv(results_dir / "stock_alpha_v1_1_spec_lock.csv", index=False)
    attribution_df.to_csv(results_dir / "stock_alpha_v1_1_attribution.csv", index=False)
    (results_dir / "stock_alpha_v1_1_recommendation.json").write_text(
        json.dumps(recommendation, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    save_spec_config(configs_dir / "qqq_plus_stock_alpha_v1_1_default.json", artifacts[str(default_row["scenario"])]["config"], role="default_frozen_spec")
    save_spec_config(configs_dir / "qqq_plus_stock_alpha_v1_1_aggressive.json", artifacts[str(aggressive_row["scenario"])]["config"], role="aggressive_alt_spec")

    write_markdown_report(
        results_dir / "stock_alpha_v1_1_spec_lock.md",
        previous_default_row=selection_ranked.loc[selection_ranked["scenario"] == previous_default_config.name].iloc[0] if (selection_ranked["scenario"] == previous_default_config.name).any() else pd.Series({"scenario": previous_default_config.name}),
        default_row=default_row,
        aggressive_row=aggressive_row,
        selection_df=selection_ranked,
        plateau_df=plateau_df,
        plateau_stats=plateau_stats,
        oos_df=oos_df,
        attribution_df=attribution_df,
        spy_sanity_df=spy_sanity_df,
        recommendation=recommendation,
    )

    print(f"alias data: {alias_dir}")
    print(f"results written to: {results_dir}")
    print(f"configs written to: {configs_dir}")
    print(f"default frozen: {default_row['scenario']}")
    print(f"aggressive alt: {aggressive_row['scenario']}")
    print(f"recommendation: {recommendation['v1_1_recommendation']}")


if __name__ == "__main__":
    main()
