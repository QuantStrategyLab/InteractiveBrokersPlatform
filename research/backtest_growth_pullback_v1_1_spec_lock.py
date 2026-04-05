#!/usr/bin/env python3
"""Local spec-lock for growth_pullback_systematic_v1 around the current tech-heavy center."""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import backtest_growth_pullback_suite as gp  # noqa: E402
import backtest_stock_alpha_v1_robustness as robust  # noqa: E402
import backtest_stock_alpha_suite as suite  # noqa: E402


DEFAULT_RESULTS_DIR = SCRIPT_DIR / "results"
DEFAULT_CONFIGS_DIR = SCRIPT_DIR / "configs"
MAIN_COST_BPS = 5.0
SUPPLEMENTAL_COST_BPS = 0.0
COST_LEVELS = (SUPPLEMENTAL_COST_BPS, MAIN_COST_BPS)
CENTER_CONFIG_PATH = DEFAULT_CONFIGS_DIR / "growth_pullback_systematic_v1_default.json"
COMPARISON_PERIODS = (
    ("Full Sample", None, None),
    ("OOS Sample", "2022-01-01", None),
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023+", "2023-01-01", None),
)
LOCAL_VALUES = {
    "holdings_count": (12, 16),
    "single_name_cap": (0.08, 0.10),
    "sector_cap": (0.30, 0.40, 0.50),
    "hold_bonus": (0.05, 0.10),
    "min_adv20_usd": (20_000_000.0, 50_000_000.0),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alias-data-run-dir", help="Prepared alias-fixed Russell run dir")
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


def _find_neighbor_index(values: tuple[float, ...], current_value: float) -> int:
    for idx, value in enumerate(values):
        if math.isclose(float(value), float(current_value), rel_tol=0.0, abs_tol=1e-12):
            return idx
    raise ValueError(f"Current value {current_value} not found in {values}")


def _variant_name(center_name: str, changes: list[tuple[str, object]]) -> str:
    suffixes = []
    for field_name, value in changes:
        if isinstance(value, float):
            if field_name == "min_adv20_usd":
                suffixes.append(f"adv{int(round(value / 1_000_000.0))}m")
            else:
                suffixes.append(f"{field_name}_{str(value).replace('.', 'p')}")
        else:
            suffixes.append(f"{field_name}_{value}")
    return center_name + "__" + "__".join(suffixes)


def apply_change(config: gp.GrowthPullbackConfig, field_name: str, value) -> gp.GrowthPullbackConfig:
    if field_name == "min_adv20_usd":
        universe_spec = replace(config.universe_spec, min_adv20_usd=float(value))
        return replace(config, name=_variant_name(config.name, [(field_name, value)]), universe_spec=universe_spec)
    return replace(config, name=_variant_name(config.name, [(field_name, value)]), **{field_name: value})


def apply_changes(config: gp.GrowthPullbackConfig, changes: list[tuple[str, object]]) -> gp.GrowthPullbackConfig:
    updated = config
    for field_name, value in changes:
        if field_name == "min_adv20_usd":
            updated = replace(updated, universe_spec=replace(updated.universe_spec, min_adv20_usd=float(value)))
        else:
            updated = replace(updated, **{field_name: value})
    return replace(updated, name=_variant_name(config.name, changes))


def build_first_order_neighbors(center: gp.GrowthPullbackConfig) -> list[tuple[dict[str, object], gp.GrowthPullbackConfig]]:
    neighbors: list[tuple[dict[str, object], gp.GrowthPullbackConfig]] = []
    current_map = {
        "holdings_count": center.holdings_count,
        "single_name_cap": center.single_name_cap,
        "sector_cap": center.sector_cap,
        "hold_bonus": center.hold_bonus,
        "min_adv20_usd": center.universe_spec.min_adv20_usd,
    }
    for field_name, values in LOCAL_VALUES.items():
        idx = _find_neighbor_index(values, current_map[field_name])
        for direction, offset in (("down", -1), ("up", 1)):
            new_idx = idx + offset
            if new_idx < 0 or new_idx >= len(values):
                continue
            value = values[new_idx]
            neighbor = apply_change(center, field_name, value)
            neighbors.append((
                {
                    "variant_scope": "first_order",
                    "change_count": 1,
                    "change_1_field": field_name,
                    "change_1_value": value,
                    "change_2_field": None,
                    "change_2_value": None,
                    "change_summary": f"{field_name}:{current_map[field_name]}->{value}",
                },
                neighbor,
            ))
    return neighbors


def build_second_order_candidates(
    center: gp.GrowthPullbackConfig,
    *,
    first_order_oos: pd.DataFrame,
    occupancy_summary: pd.DataFrame,
) -> list[tuple[dict[str, object], gp.GrowthPullbackConfig]]:
    center_avg = float(occupancy_summary.loc[occupancy_summary["strategy"] == center.name, "avg_selected_count"].iloc[0])
    center_oos = float(first_order_oos.loc[first_order_oos["strategy"] == center.name, "CAGR"].iloc[0])

    candidate_rows = first_order_oos.merge(
        occupancy_summary[["strategy", "avg_selected_count"]],
        on="strategy",
        how="left",
    )
    sector50_ok = candidate_rows.loc[
        candidate_rows["change_summary"].astype(str).str.contains("sector_cap:0.4->0.5", regex=False)
        & candidate_rows["avg_selected_count"].ge(center_avg + 1.0)
        & candidate_rows["CAGR"].ge(center_oos - 0.02)
    ]
    if sector50_ok.empty:
        return []

    changes = [
        [("sector_cap", 0.50), ("holdings_count", 16)],
        [("sector_cap", 0.50), ("single_name_cap", 0.08)],
        [("sector_cap", 0.50), ("hold_bonus", 0.10)],
        [("sector_cap", 0.50), ("min_adv20_usd", 20_000_000.0)],
    ]
    variants: list[tuple[dict[str, object], gp.GrowthPullbackConfig]] = []
    seen: set[str] = set()
    for pair in changes:
        config = apply_changes(center, pair)
        if config.name in seen:
            continue
        seen.add(config.name)
        variants.append((
            {
                "variant_scope": "second_order",
                "change_count": 2,
                "change_1_field": pair[0][0],
                "change_1_value": pair[0][1],
                "change_2_field": pair[1][0],
                "change_2_value": pair[1][1],
                "change_summary": ", ".join(f"{field}={value}" for field, value in pair),
            },
            config,
        ))
    return variants


def stage_universe_counts(frame: pd.DataFrame, config: gp.GrowthPullbackConfig) -> dict[str, object]:
    base = frame.loc[frame["base_eligible"]].copy()
    adv = base.loc[base["adv20_usd"] >= float(config.universe_spec.min_adv20_usd)].copy()
    stage = adv
    sector_count_before_theme = int(stage["sector"].nunique()) if not stage.empty else 0
    if config.universe_spec.sector_whitelist:
        stage = stage.loc[stage["sector"].isin(config.universe_spec.sector_whitelist)].copy()
    after_sector = stage.copy()
    if config.universe_spec.symbol_whitelist:
        allowed = {str(symbol).upper() for symbol in config.universe_spec.symbol_whitelist}
        stage = stage.loc[stage["symbol"].isin(allowed)].copy()
    after_symbols = stage.copy()
    if config.universe_spec.leadership_filter and not stage.empty:
        leadership_proxy = (
            stage["mom_12_1"] * 0.45
            + stage["mom_6_1"] * 0.20
            + stage["breakout_252"] * 0.20
            + stage["sma200_gap"] * 0.15
        )
        cutoff = float(leadership_proxy.median())
        stage = stage.loc[(leadership_proxy >= cutoff) & (stage["sma200_gap"] > 0)].copy()
    final_stage = stage
    return {
        "base_eligible_count": int(len(base)),
        "adv_filtered_count": int(len(adv)),
        "after_sector_count": int(len(after_sector)),
        "after_symbol_count": int(len(after_symbols)),
        "final_candidate_count": int(len(final_stage)),
        "active_sector_count": int(final_stage["sector"].nunique()) if not final_stage.empty else 0,
        "sector_count_before_theme": sector_count_before_theme,
    }


def classify_underfill_reason(
    *,
    stock_exposure: float,
    selected_count: int,
    holdings_count: int,
    counts: dict[str, object],
    sector_capacity_limit: int,
) -> str:
    if stock_exposure <= 1e-12:
        return "hard_defense_zero_stock"
    if selected_count >= holdings_count:
        return "filled"
    base_count = int(counts["base_eligible_count"])
    adv_count = int(counts["adv_filtered_count"])
    sector_count = int(counts["after_sector_count"])
    final_count = int(counts["final_candidate_count"])
    if adv_count < holdings_count and base_count >= holdings_count:
        return "liquidity_gate"
    if sector_count < holdings_count and adv_count >= holdings_count:
        return "theme_universe_narrow"
    if final_count >= holdings_count and sector_capacity_limit < holdings_count:
        return "sector_cap_binding"
    if final_count < holdings_count:
        return "insufficient_candidates"
    return "other"


def build_monthly_occupancy(
    config: gp.GrowthPullbackConfig,
    artifacts: gp.StrategyArtifacts,
    context: dict[str, object],
    *,
    strategy_label: str,
) -> pd.DataFrame:
    selection = artifacts.selection_history.copy()
    if selection.empty:
        return pd.DataFrame()
    selection["rebalance_date"] = pd.to_datetime(selection["rebalance_date"]).dt.tz_localize(None).dt.normalize()
    rows = []
    for record in selection.itertuples(index=False):
        rebalance_date = pd.Timestamp(record.rebalance_date).normalize()
        frame = context["raw_snapshots"][rebalance_date]
        counts = stage_universe_counts(frame, config)
        stock_exposure = float(record.stock_exposure) if pd.notna(record.stock_exposure) else 0.0
        per_name_target = stock_exposure / max(int(config.holdings_count), 1)
        sector_slot_cap = config.holdings_count if per_name_target <= 0 else max(1, int(math.floor(config.sector_cap / per_name_target)))
        sector_capacity_limit = min(config.holdings_count, int(counts["active_sector_count"]) * int(sector_slot_cap))

        weight_row = artifacts.weights_history.loc[rebalance_date].fillna(0.0)
        stock_weights = weight_row.drop(labels=[suite.SAFE_HAVEN], errors="ignore")
        stock_weights = stock_weights[stock_weights > 1e-12]
        stock_weight = float(stock_weights.sum())
        safe_haven_weight = float(weight_row.get(suite.SAFE_HAVEN, 0.0))
        rows.append(
            {
                "strategy": strategy_label,
                "rebalance_date": str(rebalance_date.date()),
                "regime": str(record.regime),
                "stock_exposure": stock_exposure,
                "selected_count": int(record.selected_count),
                "stock_weight": stock_weight,
                "safe_haven_weight": safe_haven_weight,
                "top1_stock_weight": float(stock_weights.nlargest(1).sum()) if not stock_weights.empty else 0.0,
                "top3_stock_weight": float(stock_weights.nlargest(min(3, len(stock_weights))).sum()) if not stock_weights.empty else 0.0,
                "top5_stock_weight": float(stock_weights.nlargest(min(5, len(stock_weights))).sum()) if not stock_weights.empty else 0.0,
                "base_eligible_count": int(counts["base_eligible_count"]),
                "adv_filtered_count": int(counts["adv_filtered_count"]),
                "after_sector_count": int(counts["after_sector_count"]),
                "after_symbol_count": int(counts["after_symbol_count"]),
                "final_candidate_count": int(counts["final_candidate_count"]),
                "active_sector_count": int(counts["active_sector_count"]),
                "sector_slot_cap": int(sector_slot_cap),
                "sector_capacity_limit": int(sector_capacity_limit),
                "underfilled_lt_nominal": bool(int(record.selected_count) < int(config.holdings_count)),
                "underfilled_lt_10": bool(int(record.selected_count) < 10),
                "underfilled_lt_8": bool(int(record.selected_count) < 8),
                "underfilled_lt_6": bool(int(record.selected_count) < 6),
                "primary_underfill_reason": classify_underfill_reason(
                    stock_exposure=stock_exposure,
                    selected_count=int(record.selected_count),
                    holdings_count=int(config.holdings_count),
                    counts=counts,
                    sector_capacity_limit=int(sector_capacity_limit),
                ),
            }
        )
    return pd.DataFrame(rows)


def summarize_occupancy(monthly_occupancy: pd.DataFrame, *, strategy_label: str) -> dict[str, float | str]:
    if monthly_occupancy.empty:
        return {
            "strategy": strategy_label,
            "avg_selected_count": float("nan"),
            "risk_on_avg_names": float("nan"),
            "soft_defense_avg_names": float("nan"),
            "hard_defense_avg_names": float("nan"),
            "share_lt_nominal": float("nan"),
            "share_lt_10": float("nan"),
            "share_lt_8": float("nan"),
            "share_lt_6": float("nan"),
            "avg_top1_stock_weight": float("nan"),
            "avg_top3_stock_weight": float("nan"),
            "avg_top5_stock_weight": float("nan"),
            "avg_stock_weight": float("nan"),
            "avg_safe_haven_weight": float("nan"),
            "dominant_underfill_reason": "none",
            "dominant_underfill_reason_share": float("nan"),
        }
    regime_means = monthly_occupancy.groupby("regime")["selected_count"].mean()
    underfilled = monthly_occupancy.loc[monthly_occupancy["underfilled_lt_nominal"]].copy()
    if underfilled.empty:
        dominant_reason = "filled"
        dominant_reason_share = 0.0
    else:
        reason_share = underfilled["primary_underfill_reason"].value_counts(normalize=True)
        dominant_reason = str(reason_share.index[0])
        dominant_reason_share = float(reason_share.iloc[0])
    return {
        "strategy": strategy_label,
        "avg_selected_count": float(monthly_occupancy["selected_count"].mean()),
        "risk_on_avg_names": float(regime_means.get("risk_on", np.nan)),
        "soft_defense_avg_names": float(regime_means.get("soft_defense", np.nan)),
        "hard_defense_avg_names": float(regime_means.get("hard_defense", np.nan)),
        "share_lt_nominal": float(monthly_occupancy["underfilled_lt_nominal"].mean()),
        "share_lt_10": float(monthly_occupancy["underfilled_lt_10"].mean()),
        "share_lt_8": float(monthly_occupancy["underfilled_lt_8"].mean()),
        "share_lt_6": float(monthly_occupancy["underfilled_lt_6"].mean()),
        "avg_top1_stock_weight": float(monthly_occupancy["top1_stock_weight"].mean()),
        "avg_top3_stock_weight": float(monthly_occupancy["top3_stock_weight"].mean()),
        "avg_top5_stock_weight": float(monthly_occupancy["top5_stock_weight"].mean()),
        "avg_stock_weight": float(monthly_occupancy["stock_weight"].mean()),
        "avg_safe_haven_weight": float(monthly_occupancy["safe_haven_weight"].mean()),
        "dominant_underfill_reason": dominant_reason,
        "dominant_underfill_reason_share": dominant_reason_share,
    }


def add_local_selection_scores(summary_df: pd.DataFrame) -> pd.DataFrame:
    scored = summary_df.copy()
    def pr(series: pd.Series, higher: bool) -> pd.Series:
        numeric = pd.to_numeric(series, errors="coerce")
        ranked = numeric.rank(method="average", pct=True) if higher else (-numeric).rank(method="average", pct=True)
        return ranked.fillna(0.0)

    scored["score_oos_rel_qqq"] = pr(scored["oos_cagr_minus_qqq"], True)
    scored["score_oos_maxdd"] = pr(scored["oos_max_drawdown"], True)
    scored["score_avg_names"] = pr(scored["avg_selected_count"], True)
    scored["score_underfill_lt8"] = pr(scored["share_lt_8"], False)
    scored["score_top3_conc"] = pr(scored["avg_top3_stock_weight"], False)
    scored["score_turnover"] = pr(scored["annual_turnover"], False)
    scored["score_2022"] = pr(scored["return_2022"], True)
    scored["local_stability_score"] = (
        scored["score_oos_rel_qqq"] * 0.25
        + scored["score_oos_maxdd"] * 0.20
        + scored["score_avg_names"] * 0.15
        + scored["score_underfill_lt8"] * 0.15
        + scored["score_top3_conc"] * 0.10
        + scored["score_turnover"] * 0.10
        + scored["score_2022"] * 0.05
    )
    scored["core_viable"] = (
        (scored["oos_cagr_minus_qqq"] > 0.10)
        & (scored["full_max_drawdown"] >= -0.40)
        & (scored["oos_max_drawdown"] >= -0.30)
        & (scored["annual_turnover"] <= 4.5)
    )
    return scored.sort_values(
        by=["local_stability_score", "oos_cagr_minus_qqq", "avg_selected_count", "oos_max_drawdown"],
        ascending=[False, False, False, False],
    ).reset_index(drop=True)


def build_local_plateau(local_oos_rows: pd.DataFrame, center_row: pd.Series) -> tuple[pd.DataFrame, dict[str, float]]:
    frame = local_oos_rows.copy()
    frame["delta_oos_cagr_vs_center"] = frame["CAGR"] - float(center_row["CAGR"])
    frame["delta_oos_maxdd_vs_center"] = frame["Max Drawdown"] - float(center_row["Max Drawdown"])
    frame["delta_oos_turnover_vs_center"] = frame["Turnover/Year"] - float(center_row["Turnover/Year"])
    frame["abs_delta_oos_cagr_vs_center"] = frame["delta_oos_cagr_vs_center"].abs()
    frame["within_50bps"] = frame["abs_delta_oos_cagr_vs_center"] <= 0.005
    frame["within_100bps"] = frame["abs_delta_oos_cagr_vs_center"] <= 0.010
    frame["within_200bps"] = frame["abs_delta_oos_cagr_vs_center"] <= 0.020
    local_only = frame.loc[frame["strategy"] != str(center_row["strategy"])].copy()
    summary = {
        "variant_count_ex_center": int(len(local_only)),
        "local_plateau_50bps_share": float(local_only["within_50bps"].mean()) if not local_only.empty else float("nan"),
        "local_plateau_100bps_share": float(local_only["within_100bps"].mean()) if not local_only.empty else float("nan"),
        "local_plateau_200bps_share": float(local_only["within_200bps"].mean()) if not local_only.empty else float("nan"),
    }
    return frame, summary


def build_recommendation(
    *,
    center_summary: pd.Series,
    stable_summary: pd.Series,
    local_plateau_summary: dict[str, float],
    qqq_plus_default_oos: pd.Series,
) -> dict[str, object]:
    same_as_center = str(stable_summary["strategy"]) == str(center_summary["strategy"])
    stable_enough = bool(
        float(local_plateau_summary["local_plateau_100bps_share"]) >= 0.40
        and float(local_plateau_summary["local_plateau_200bps_share"]) >= 0.60
    )
    occupancy_ok = bool(
        float(stable_summary["avg_selected_count"]) >= 8.0
        and float(stable_summary["share_lt_8"]) <= 0.35
        and float(stable_summary["avg_top3_stock_weight"]) <= 0.65
    )
    performance_ok = bool(
        float(stable_summary["oos_cagr_minus_qqq"]) > 0.10
        and float(stable_summary["oos_max_drawdown"]) >= -0.30
        and float(stable_summary["annual_turnover"]) <= 4.5
    )

    if stable_enough and occupancy_ok and performance_ok:
        level = "research_default"
        reason = "local neighborhood is stable enough and occupancy/concentration are acceptable"
    elif performance_ok:
        level = "research_default_candidate"
        reason = "edge is good, but occupancy/concentration or local plateau still needs more tightening"
    else:
        level = "discard"
        reason = "local robustness is not good enough even for research default candidate"

    if float(stable_summary["oos_cagr"]) >= float(qqq_plus_default_oos["CAGR"]) and float(stable_summary["oos_max_drawdown"]) >= float(qqq_plus_default_oos["Max Drawdown"]):
        role = "替代者"
    elif float(stable_summary["oos_cagr_minus_qqq"]) > 0 and float(stable_summary["full_max_drawdown"]) >= -0.40:
        role = "并行分支"
    else:
        role = "次级实验"

    return {
        "center_strategy": str(center_summary["strategy"]),
        "selected_local_default": str(stable_summary["strategy"]),
        "selected_is_center": same_as_center,
        "research_recommendation": level,
        "role_vs_qqq_plus_current_default": role,
        "reason": reason,
        "checks": {
            "center_avg_selected_count": float(center_summary["avg_selected_count"]),
            "selected_avg_selected_count": float(stable_summary["avg_selected_count"]),
            "selected_share_lt_8": float(stable_summary["share_lt_8"]),
            "selected_avg_top3_stock_weight": float(stable_summary["avg_top3_stock_weight"]),
            "local_plateau_100bps_share": float(local_plateau_summary["local_plateau_100bps_share"]),
            "local_plateau_200bps_share": float(local_plateau_summary["local_plateau_200bps_share"]),
            "selected_oos_cagr_minus_qqq": float(stable_summary["oos_cagr_minus_qqq"]),
            "selected_oos_max_drawdown": float(stable_summary["oos_max_drawdown"]),
            "selected_annual_turnover": float(stable_summary["annual_turnover"]),
        },
        "thresholds": {
            "avg_selected_count_min_for_default": 8.0,
            "share_lt_8_max_for_default": 0.35,
            "avg_top3_weight_max_for_default": 0.65,
            "local_plateau_100bps_min_for_default": 0.40,
            "local_plateau_200bps_min_for_default": 0.60,
            "oos_cagr_minus_qqq_min": 0.10,
            "oos_max_drawdown_floor": -0.30,
            "annual_turnover_cap": 4.5,
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
    center_config: gp.GrowthPullbackConfig,
    center_result_5bps: pd.Series,
    stable_result_5bps: pd.Series,
    occupancy_summary_df: pd.DataFrame,
    local_plateau_df: pd.DataFrame,
    local_plateau_summary: dict[str, float],
    comparison_df_5bps: pd.DataFrame,
    recommendation: dict[str, object],
) -> None:
    lines = [
        "# growth_pullback_systematic_v1.1 spec lock",
        "",
        "## Center spec",
        f"- strategy={center_config.name}",
        f"- family={center_config.family}",
        f"- universe={center_config.universe_spec.name}",
        f"- normalization={gp.normalization_label(center_config.universe_spec.normalization)}",
        f"- holdings={center_config.holdings_count}",
        f"- single_cap={center_config.single_name_cap:.0%}",
        f"- sector_cap={center_config.sector_cap:.0%}",
        f"- hold_bonus={center_config.hold_bonus:.2f}",
        f"- min_adv20={center_config.universe_spec.min_adv20_usd/1_000_000:.0f}M",
        f"- regime={center_config.regime.name}",
        f"- exposures=100/{int(center_config.exposures.soft_defense_exposure*100)}/{int(center_config.exposures.hard_defense_exposure*100)}",
        "",
        "## Occupancy summary",
        format_table(occupancy_summary_df[[
            "strategy", "avg_selected_count", "risk_on_avg_names", "soft_defense_avg_names", "share_lt_nominal", "share_lt_8", "avg_top3_stock_weight", "avg_safe_haven_weight", "dominant_underfill_reason", "dominant_underfill_reason_share"
        ]]),
        "",
        "## Local plateau (5 bps, OOS)",
        f"- local_plateau_50bps_share={local_plateau_summary['local_plateau_50bps_share']:.1%}",
        f"- local_plateau_100bps_share={local_plateau_summary['local_plateau_100bps_share']:.1%}",
        f"- local_plateau_200bps_share={local_plateau_summary['local_plateau_200bps_share']:.1%}",
        format_table(local_plateau_df[[
            "strategy", "variant_scope", "change_summary", "CAGR", "Max Drawdown", "Turnover/Year", "delta_oos_cagr_vs_center", "delta_oos_maxdd_vs_center", "delta_oos_turnover_vs_center"
        ]]),
        "",
        "## Main comparison (5 bps)",
        format_table(comparison_df_5bps[[
            "strategy", "period", "family", "CAGR", "Total Return", "Max Drawdown", "Sharpe", "Alpha_ann_vs_QQQ", "Information Ratio vs QQQ", "Turnover/Year", "Average Names Held"
        ]]),
        "",
        "## Recommendation",
        f"- research_recommendation={recommendation['research_recommendation']}",
        f"- role_vs_qqq_plus_current_default={recommendation['role_vs_qqq_plus_current_default']}",
        f"- selected_local_default={recommendation['selected_local_default']}",
        f"- reason={recommendation['reason']}",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = parse_args()
    results_dir = Path(args.results_dir).expanduser().resolve()
    configs_dir = Path(args.configs_dir).expanduser().resolve()
    results_dir.mkdir(parents=True, exist_ok=True)
    configs_dir.mkdir(parents=True, exist_ok=True)

    center_config = gp.load_spec_config(configs_dir / "growth_pullback_systematic_v1_default.json")
    alias_dir = robust.discover_run_dir(args.alias_data_run_dir, "r1000_multifactor_defensive_*_official_monthly_v2_alias")
    context = gp.build_context(alias_dir, start=args.start, end=args.end)
    benchmark_returns = context["stock_returns_matrix"]["QQQ"].copy()

    evaluated: list[tuple[dict[str, object], gp.GrowthPullbackConfig]] = [(
        {
            "variant_scope": "center",
            "change_count": 0,
            "change_1_field": None,
            "change_1_value": None,
            "change_2_field": None,
            "change_2_value": None,
            "change_summary": "center",
        },
        center_config,
    )]
    evaluated.extend(build_first_order_neighbors(center_config))

    local_rows: list[dict[str, object]] = []
    artifacts_by_strategy: dict[str, dict[float, gp.StrategyArtifacts]] = {}
    occupancy_frames: list[pd.DataFrame] = []
    occupancy_summaries: list[dict[str, float | str]] = []

    for meta, config in evaluated:
        rows, artifacts = gp.evaluate_candidate_rows(config, context, benchmark_returns, cost_bps_values=COST_LEVELS)
        frame = pd.DataFrame(rows)
        frame = frame.assign(**meta)
        local_rows.extend(frame.to_dict(orient="records"))
        artifacts_by_strategy[config.name] = artifacts
        occ = build_monthly_occupancy(config, artifacts[MAIN_COST_BPS], context, strategy_label=config.name)
        if not occ.empty:
            occ["variant_scope"] = meta["variant_scope"]
            occ["change_summary"] = meta["change_summary"]
            occupancy_frames.append(occ)
            occupancy_summaries.append(summarize_occupancy(occ, strategy_label=config.name) | {
                "variant_scope": meta["variant_scope"],
                "change_summary": meta["change_summary"],
            })

    local_df = pd.DataFrame(local_rows)
    occupancy_monthly_df = pd.concat(occupancy_frames, ignore_index=True)
    occupancy_summary_df = pd.DataFrame(occupancy_summaries)

    first_order_oos = local_df.loc[
        (local_df["cost_bps_one_way"] == MAIN_COST_BPS)
        & (local_df["period"] == "OOS Sample")
    ].copy()

    second_order_variants = build_second_order_candidates(center_config, first_order_oos=first_order_oos, occupancy_summary=occupancy_summary_df)
    for meta, config in second_order_variants:
        rows, artifacts = gp.evaluate_candidate_rows(config, context, benchmark_returns, cost_bps_values=COST_LEVELS)
        frame = pd.DataFrame(rows).assign(**meta)
        local_rows.extend(frame.to_dict(orient="records"))
        artifacts_by_strategy[config.name] = artifacts
        occ = build_monthly_occupancy(config, artifacts[MAIN_COST_BPS], context, strategy_label=config.name)
        if not occ.empty:
            occ["variant_scope"] = meta["variant_scope"]
            occ["change_summary"] = meta["change_summary"]
            occupancy_frames.append(occ)
            occupancy_summaries.append(summarize_occupancy(occ, strategy_label=config.name) | {
                "variant_scope": meta["variant_scope"],
                "change_summary": meta["change_summary"],
            })

    local_df = pd.DataFrame(local_rows)
    occupancy_monthly_df = pd.concat(occupancy_frames, ignore_index=True)
    occupancy_summary_df = pd.DataFrame(occupancy_summaries)

    reference_rows_df, _ = gp.build_reference_rows(context, COST_LEVELS)
    reference_rows_df["variant_scope"] = "reference"
    reference_rows_df["change_summary"] = "reference"

    center_oos = extract_period_row(local_df, center_config.name, MAIN_COST_BPS, "OOS Sample")
    local_oos_main = local_df.loc[(local_df["cost_bps_one_way"] == MAIN_COST_BPS) & (local_df["period"] == "OOS Sample")].copy()
    local_plateau_df, local_plateau_summary = build_local_plateau(local_oos_main, center_oos)

    local_summary_rows = []
    for strategy_name in sorted(local_df["strategy"].unique()):
        full_row = extract_period_row(local_df, strategy_name, MAIN_COST_BPS, "Full Sample")
        oos_row = extract_period_row(local_df, strategy_name, MAIN_COST_BPS, "OOS Sample")
        row_2022 = extract_period_row(local_df, strategy_name, MAIN_COST_BPS, "2022")
        row_2023 = extract_period_row(local_df, strategy_name, MAIN_COST_BPS, "2023+")
        occ = occupancy_summary_df.loc[occupancy_summary_df["strategy"] == strategy_name].iloc[0]
        local_summary_rows.append(
            {
                "strategy": strategy_name,
                "variant_scope": str(full_row["variant_scope"]),
                "change_summary": str(full_row["change_summary"]),
                "full_cagr": float(full_row["CAGR"]),
                "full_max_drawdown": float(full_row["Max Drawdown"]),
                "oos_cagr": float(oos_row["CAGR"]),
                "oos_cagr_minus_qqq": float(oos_row["CAGR"] - extract_period_row(reference_rows_df, "QQQ", MAIN_COST_BPS, "OOS Sample")["CAGR"]),
                "oos_max_drawdown": float(oos_row["Max Drawdown"]),
                "oos_alpha_ann_vs_qqq": float(oos_row["Alpha_ann_vs_QQQ"] if "Alpha_ann_vs_QQQ" in oos_row else oos_row["alpha_ann_vs_qqq"]),
                "annual_turnover": float(full_row["Turnover/Year"]),
                "return_2022": float(row_2022["Total Return"]),
                "cagr_2023_plus": float(row_2023["CAGR"]),
                **occ.to_dict(),
            }
        )
    local_selection_df = add_local_selection_scores(pd.DataFrame(local_summary_rows))
    stable_local_row = local_selection_df.iloc[0]
    stable_strategy_name = str(stable_local_row["strategy"])

    qqq_plus_default_oos = extract_period_row(reference_rows_df, "qqq_plus_current_default", MAIN_COST_BPS, "OOS Sample")
    recommendation = build_recommendation(
        center_summary=local_selection_df.loc[local_selection_df["strategy"] == center_config.name].iloc[0],
        stable_summary=stable_local_row,
        local_plateau_summary=local_plateau_summary,
        qqq_plus_default_oos=qqq_plus_default_oos,
    )

    comparison_df = pd.concat(
        [
            local_df.loc[local_df["strategy"].isin([center_config.name, stable_strategy_name])].copy(),
            reference_rows_df.loc[reference_rows_df["strategy"].isin(["qqq_plus_current_default", "aggressive_alt_spec", "defensive_baseline", "QQQ"])].copy(),
        ],
        ignore_index=True,
    )
    comparison_df["family"] = comparison_df.get("family", pd.Series(index=comparison_df.index, dtype=object)).fillna("reference")
    comparison_df["Alpha_ann_vs_QQQ"] = comparison_df.get("Alpha_ann_vs_QQQ", comparison_df.get("alpha_ann_vs_qqq", pd.Series(index=comparison_df.index, dtype=float)))

    gp.save_spec_config(configs_dir / "growth_pullback_systematic_v1_1_default.json", gp.load_spec_config(configs_dir / "growth_pullback_systematic_v1_default.json") if stable_strategy_name == center_config.name else next(config for _meta, config in second_order_variants + build_first_order_neighbors(center_config) + [(None, center_config)] if config.name == stable_strategy_name), role="research_default_v1_1")

    local_df.to_csv(results_dir / "growth_pullback_v1_1_spec_lock.csv", index=False)
    occupancy_monthly_df.to_csv(results_dir / "growth_pullback_v1_1_occupancy.csv", index=False)
    local_plateau_df.to_csv(results_dir / "growth_pullback_v1_1_local_plateau.csv", index=False)
    (results_dir / "growth_pullback_v1_1_recommendation.json").write_text(json.dumps(recommendation, indent=2, ensure_ascii=False), encoding="utf-8")
    write_markdown_report(
        results_dir / "growth_pullback_v1_1_spec_lock.md",
        center_config=center_config,
        center_result_5bps=extract_period_row(local_df, center_config.name, MAIN_COST_BPS, "OOS Sample"),
        stable_result_5bps=extract_period_row(local_df, stable_strategy_name, MAIN_COST_BPS, "OOS Sample"),
        occupancy_summary_df=occupancy_summary_df,
        local_plateau_df=local_plateau_df,
        local_plateau_summary=local_plateau_summary,
        comparison_df_5bps=comparison_df.loc[comparison_df["cost_bps_one_way"] == MAIN_COST_BPS].copy(),
        recommendation=recommendation,
    )

    print(f"alias data: {alias_dir}")
    print(f"center: {center_config.name}")
    print(f"stable local default: {stable_strategy_name}")
    print(f"recommendation: {recommendation['research_recommendation']}")
    print(f"results written to: {results_dir}")


if __name__ == "__main__":
    main()
