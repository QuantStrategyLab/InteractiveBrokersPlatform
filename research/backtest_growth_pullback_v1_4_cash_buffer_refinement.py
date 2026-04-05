#!/usr/bin/env python3
"""Refinement and freeze review for the honest cash-buffered tech pullback branch."""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import backtest_growth_pullback_suite as gp  # noqa: E402
import backtest_growth_pullback_v1_2_geometry_repair as v12  # noqa: E402
import backtest_growth_pullback_v1_3_spec_normalization as v13  # noqa: E402
import backtest_stock_alpha_v1_robustness as robust  # noqa: E402


DEFAULT_RESULTS_DIR = SCRIPT_DIR / "results"
DEFAULT_CONFIGS_DIR = SCRIPT_DIR / "configs"
MAIN_COST_BPS = 5.0
COST_LEVELS = (0.0, MAIN_COST_BPS)
CENTER_CONFIG_FILENAME = "growth_pullback_systematic_v1_default.json"
COMPARISON_PERIODS = (
    ("Full Sample", None, None),
    ("OOS Sample", "2022-01-01", None),
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023+", "2023-01-01", None),
)


@dataclass(frozen=True)
class CashBufferCandidate:
    label: str
    config: gp.GrowthPullbackConfig
    risk_on_exposure: float
    soft_threshold: float
    hard_threshold: float
    note: str
    base_shape: str
    regime_variant: str
    adv_bucket: str
    hold_bonus_bucket: str


@dataclass(frozen=True)
class ReferenceCandidate:
    label: str
    config: gp.GrowthPullbackConfig
    risk_on_exposure: float
    soft_threshold: float
    hard_threshold: float
    note: str


@contextmanager
def temporary_breadth_thresholds(soft: float, hard: float):
    original_soft = robust.SOFT_BREADTH_THRESHOLD
    original_hard = robust.HARD_BREADTH_THRESHOLD
    robust.SOFT_BREADTH_THRESHOLD = float(soft)
    robust.HARD_BREADTH_THRESHOLD = float(hard)
    try:
        yield
    finally:
        robust.SOFT_BREADTH_THRESHOLD = original_soft
        robust.HARD_BREADTH_THRESHOLD = original_hard


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alias-data-run-dir", help="Prepared alias-fixed Russell data run dir")
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR))
    parser.add_argument("--configs-dir", default=str(DEFAULT_CONFIGS_DIR))
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    return parser.parse_args()


def resolve_center_config(configs_dir: Path) -> Path:
    return configs_dir / CENTER_CONFIG_FILENAME


def build_cash_buffer_candidates(center_cfg: gp.GrowthPullbackConfig) -> list[CashBufferCandidate]:
    candidates: list[CashBufferCandidate] = []
    structures = {
        "A": {"holdings": 8, "single_cap": 0.10, "sector_cap": 0.40},
        "B": {"holdings": 10, "single_cap": 0.08, "sector_cap": 0.40},
    }
    regime_variants = {
        "base": {"soft_threshold": 0.55, "hard_threshold": 0.35, "soft_defense": 0.60},
        "tight": {"soft_threshold": 0.60, "hard_threshold": 0.40, "soft_defense": 0.50},
    }
    for structure_key, structure in structures.items():
        for hold_bonus in (0.05, 0.10):
            for regime_key, regime in regime_variants.items():
                for adv20 in (50_000_000.0, 20_000_000.0):
                    exposures = gp.suite.ExposureConfig(
                        name=f"80_{int(round(regime['soft_defense'] * 100))}_0",
                        soft_defense_exposure=float(regime["soft_defense"]),
                        hard_defense_exposure=0.0,
                    )
                    universe_spec = replace(center_cfg.universe_spec, min_adv20_usd=float(adv20))
                    config = replace(
                        center_cfg,
                        name=(
                            "coherent_cash_buffer_branch"
                            if structure_key == "A" and hold_bonus == 0.05 and regime_key == "base" and adv20 == 50_000_000.0
                            else f"cash_buffer_{structure_key.lower()}__hb{int(round(hold_bonus * 100)):02d}__{regime_key}__adv{int(round(adv20 / 1_000_000)):02d}"
                        ),
                        universe_spec=universe_spec,
                        holdings_count=int(structure["holdings"]),
                        single_name_cap=float(structure["single_cap"]),
                        sector_cap=float(structure["sector_cap"]),
                        exposures=exposures,
                        hold_bonus=float(hold_bonus),
                    )
                    candidates.append(
                        CashBufferCandidate(
                            label=config.name,
                            config=config,
                            risk_on_exposure=0.80,
                            soft_threshold=float(regime["soft_threshold"]),
                            hard_threshold=float(regime["hard_threshold"]),
                            note=(
                                f"cash-buffer branch {structure_key}: {structure['holdings']} names / {int(round(structure['single_cap']*100))}% single cap / "
                                f"{int(round(structure['sector_cap']*100))}% sector cap / 80% risk_on / hold_bonus={hold_bonus:.2f} / "
                                f"soft-hard={regime['soft_threshold']:.2f}/{regime['hard_threshold']:.2f} / soft_defense={regime['soft_defense']:.2f} / adv20={adv20/1_000_000:.0f}M"
                            ),
                            base_shape=structure_key,
                            regime_variant=regime_key,
                            adv_bucket=f"{int(round(adv20 / 1_000_000))}M",
                            hold_bonus_bucket=f"{hold_bonus:.2f}",
                        )
                    )
    return candidates


def build_reference_candidates(center_cfg: gp.GrowthPullbackConfig) -> list[ReferenceCandidate]:
    full_cfg = replace(
        center_cfg,
        name="coherent_full_deployment_branch_cfg",
        holdings_count=10,
        single_name_cap=0.10,
        sector_cap=0.50,
        exposures=gp.suite.ExposureConfig("100_60_0", 0.60, 0.00),
    )
    center_current_cfg = replace(
        center_cfg,
        name="center_current_cfg",
        holdings_count=12,
        single_name_cap=0.10,
        sector_cap=0.40,
        exposures=gp.suite.ExposureConfig("100_60_0", 0.60, 0.00),
        hold_bonus=0.05,
    )
    return [
        ReferenceCandidate(
            label="center_current",
            config=center_current_cfg,
            risk_on_exposure=1.0,
            soft_threshold=0.55,
            hard_threshold=0.35,
            note="Historical prototype reference",
        ),
        ReferenceCandidate(
            label="coherent_full_deployment_branch",
            config=full_cfg,
            risk_on_exposure=1.0,
            soft_threshold=0.55,
            hard_threshold=0.35,
            note="Honest full-deployment reference from v1.3",
        ),
    ]


def candidate_config_fields(candidate: CashBufferCandidate | ReferenceCandidate) -> dict[str, object]:
    fields = gp.config_fields(candidate.config)
    fields.update(
        {
            "candidate_label": candidate.label,
            "config_name": candidate.config.name,
            "risk_on_target_exposure": float(candidate.risk_on_exposure),
            "soft_threshold": float(candidate.soft_threshold),
            "hard_threshold": float(candidate.hard_threshold),
            "geometry_note": candidate.note,
        }
    )
    if isinstance(candidate, CashBufferCandidate):
        fields.update(
            {
                "base_shape": candidate.base_shape,
                "regime_variant": candidate.regime_variant,
                "adv_bucket": candidate.adv_bucket,
                "hold_bonus_bucket": candidate.hold_bonus_bucket,
            }
        )
    return fields


def evaluate_geometry_candidate(
    label: str,
    config: gp.GrowthPullbackConfig,
    *,
    risk_on_exposure: float,
    soft_threshold: float,
    hard_threshold: float,
    note: str,
    context: dict[str, object],
    benchmark_returns: pd.Series,
    cost_bps_values: Iterable[float],
) -> tuple[list[dict[str, object]], dict[float, gp.StrategyArtifacts], pd.DataFrame]:
    geometry_candidate = v12.make_candidate(label, config, risk_on_exposure=risk_on_exposure, note=note)
    with temporary_breadth_thresholds(soft_threshold, hard_threshold):
        rows, artifacts_by_cost = v12.evaluate_candidate_rows(
            geometry_candidate,
            context,
            benchmark_returns,
            cost_bps_values=cost_bps_values,
        )
        monthly = v12.build_deployment_monthly(geometry_candidate, artifacts_by_cost[MAIN_COST_BPS], context)
    enriched_rows = []
    for row in rows:
        enriched_rows.append({**row, **candidate_config_fields(CashBufferCandidate(label, config, risk_on_exposure, soft_threshold, hard_threshold, note, "", "", "", "") )})
    return enriched_rows, artifacts_by_cost, monthly


def evaluate_candidate(candidate: CashBufferCandidate, context: dict[str, object], benchmark_returns: pd.Series) -> tuple[list[dict[str, object]], dict[float, gp.StrategyArtifacts], pd.DataFrame]:
    return evaluate_geometry_candidate(
        candidate.label,
        candidate.config,
        risk_on_exposure=candidate.risk_on_exposure,
        soft_threshold=candidate.soft_threshold,
        hard_threshold=candidate.hard_threshold,
        note=candidate.note,
        context=context,
        benchmark_returns=benchmark_returns,
        cost_bps_values=COST_LEVELS,
    )


def evaluate_reference(reference: ReferenceCandidate, context: dict[str, object], benchmark_returns: pd.Series) -> tuple[list[dict[str, object]], dict[float, gp.StrategyArtifacts], pd.DataFrame]:
    return evaluate_geometry_candidate(
        reference.label,
        reference.config,
        risk_on_exposure=reference.risk_on_exposure,
        soft_threshold=reference.soft_threshold,
        hard_threshold=reference.hard_threshold,
        note=reference.note,
        context=context,
        benchmark_returns=benchmark_returns,
        cost_bps_values=COST_LEVELS,
    )


def summarize_2022_deployment(monthly: pd.DataFrame) -> dict[str, float]:
    if monthly.empty:
        return {
            "share_2022_risk_on": np.nan,
            "share_2022_soft_defense": np.nan,
            "share_2022_hard_defense": np.nan,
            "avg_2022_stock_weight": np.nan,
            "avg_2022_safe_haven_weight": np.nan,
        }
    frame = monthly.copy()
    frame["rebalance_date"] = pd.to_datetime(frame["rebalance_date"]).dt.normalize()
    frame = frame.loc[(frame["rebalance_date"] >= pd.Timestamp("2022-01-01")) & (frame["rebalance_date"] <= pd.Timestamp("2022-12-31"))]
    if frame.empty:
        return {
            "share_2022_risk_on": np.nan,
            "share_2022_soft_defense": np.nan,
            "share_2022_hard_defense": np.nan,
            "avg_2022_stock_weight": np.nan,
            "avg_2022_safe_haven_weight": np.nan,
        }
    return {
        "share_2022_risk_on": float((frame["regime"] == "risk_on").mean()),
        "share_2022_soft_defense": float((frame["regime"] == "soft_defense").mean()),
        "share_2022_hard_defense": float((frame["regime"] == "hard_defense").mean()),
        "avg_2022_stock_weight": float(frame["realized_stock_weight"].mean()),
        "avg_2022_safe_haven_weight": float(frame["safe_haven_weight"].mean()),
    }


def percentile_rank(series: pd.Series, *, higher_is_better: bool) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    ranked = numeric.rank(method="average", pct=True) if higher_is_better else (-numeric).rank(method="average", pct=True)
    return ranked.fillna(0.0)


def add_refinement_scores(summary_df: pd.DataFrame) -> pd.DataFrame:
    scored = summary_df.copy()
    scored["score_honesty"] = percentile_rank(scored["deployment_honesty_score"], higher_is_better=True)
    scored["score_fill_rate"] = percentile_rank(scored["risk_on_fill_rate"], higher_is_better=True)
    scored["score_oos_rel_qqq"] = percentile_rank(scored["oos_cagr_minus_qqq"], higher_is_better=True)
    scored["score_oos_maxdd"] = percentile_rank(scored["oos_max_drawdown"], higher_is_better=True)
    scored["score_2022"] = percentile_rank(scored["return_2022"], higher_is_better=True)
    scored["score_turnover"] = percentile_rank(scored["annual_turnover"], higher_is_better=False)
    scored["score_avg_names"] = percentile_rank(scored["avg_names_held"], higher_is_better=True)
    scored["score_top3_concentration"] = percentile_rank(scored["avg_top3_stock_weight"], higher_is_better=False)
    scored["cash_buffer_refinement_score"] = (
        scored["score_oos_rel_qqq"] * 0.28
        + scored["score_2022"] * 0.08
        + scored["score_oos_maxdd"] * 0.16
        + scored["score_honesty"] * 0.12
        + scored["score_fill_rate"] * 0.10
        + scored["score_turnover"] * 0.10
        + scored["score_avg_names"] * 0.08
        + scored["score_top3_concentration"] * 0.08
    )
    return scored.sort_values(
        by=["cash_buffer_refinement_score", "oos_cagr_minus_qqq", "return_2022", "oos_max_drawdown"],
        ascending=[False, False, False, False],
    ).reset_index(drop=True)


def build_summary_rows(
    candidate_rows_df: pd.DataFrame,
    deployment_summary_df: pd.DataFrame,
    qqq_oos_cagr: float,
) -> pd.DataFrame:
    rows = []
    for strategy, group in candidate_rows_df.groupby("strategy", sort=False):
        full_row = v12.extract_period_row(candidate_rows_df, strategy, MAIN_COST_BPS, "Full Sample")
        oos_row = v12.extract_period_row(candidate_rows_df, strategy, MAIN_COST_BPS, "OOS Sample")
        row_2022 = v12.extract_period_row(candidate_rows_df, strategy, MAIN_COST_BPS, "2022")
        row_2023 = v12.extract_period_row(candidate_rows_df, strategy, MAIN_COST_BPS, "2023+")
        dep = deployment_summary_df.loc[deployment_summary_df["strategy"] == strategy].iloc[0]
        first_row = group.iloc[0]
        rows.append(
            {
                "strategy": strategy,
                "config_name": str(first_row["config_name"]),
                "family": str(first_row["family"]),
                "base_shape": first_row.get("base_shape", "reference"),
                "regime_variant": first_row.get("regime_variant", "reference"),
                "adv_bucket": first_row.get("adv_bucket", "reference"),
                "hold_bonus_bucket": first_row.get("hold_bonus_bucket", "reference"),
                "holdings_count": int(first_row["holdings_count"]),
                "single_name_cap": float(first_row["single_name_cap"]),
                "sector_cap": float(first_row["sector_cap"]),
                "hold_bonus": float(first_row["hold_bonus"]),
                "min_adv20_usd": float(first_row["min_adv20_usd"]),
                "soft_threshold": float(first_row["soft_threshold"]),
                "hard_threshold": float(first_row["hard_threshold"]),
                "soft_defense_exposure": float(first_row["soft_defense_exposure"]),
                "risk_on_target_exposure": float(first_row["risk_on_target_exposure"]),
                "full_cagr": float(full_row["CAGR"]),
                "oos_cagr": float(oos_row["CAGR"]),
                "oos_cagr_minus_qqq": float(oos_row["CAGR"] - qqq_oos_cagr),
                "full_max_drawdown": float(full_row["Max Drawdown"]),
                "oos_max_drawdown": float(oos_row["Max Drawdown"]),
                "return_2022": float(row_2022["Total Return"]),
                "cagr_2023_plus": float(row_2023["CAGR"]),
                "annual_turnover": float(full_row["Turnover/Year"]),
                **dep.to_dict(),
            }
        )
    return pd.DataFrame(rows)


def build_recommendation(
    *,
    current_default: pd.Series,
    best_refined: pd.Series,
    qqq_plus_oos: pd.Series,
    full_deployment_reference: pd.Series,
    all_candidates: pd.DataFrame,
) -> dict[str, object]:
    best_is_current_default = str(best_refined["strategy"]) == "coherent_cash_buffer_branch"
    regime_2022_helped = bool(float(best_refined["return_2022"]) - float(current_default["return_2022"]) >= 0.02)
    honest_ok = bool(float(best_refined["deployment_honesty_score"]) >= 0.99 and float(best_refined["risk_on_fill_rate"]) >= 0.99)
    concentration_ok = bool(float(best_refined["avg_top3_stock_weight"]) <= 0.22)
    occupancy_ok = bool(float(best_refined["avg_names_held"]) >= 6.0 and float(best_refined["underfilled_month_share"]) <= 0.35)
    performance_ok = bool(float(best_refined["oos_cagr_minus_qqq"]) > 0.15 and float(best_refined["oos_max_drawdown"]) >= -0.25)
    stable_margin = bool(
        len(all_candidates) <= 1
        or float(best_refined.get("cash_buffer_refinement_score", 0.0)) - float(all_candidates.iloc[1].get("cash_buffer_refinement_score", 0.0)) >= 0.03
    )
    no_material_gain_over_current = bool(
        abs(float(best_refined["oos_cagr"]) - float(current_default["oos_cagr"])) <= 0.015
        and abs(float(best_refined["return_2022"]) - float(current_default["return_2022"])) <= 0.02
        and abs(float(best_refined["avg_names_held"]) - float(current_default["avg_names_held"])) <= 1.0
    )

    if not performance_ok:
        level = "cash_buffer_branch_candidate"
        reason = "cash-buffer branch direction still exists, but the refined candidates do not keep enough relative-to-QQQ edge"
    elif honest_ok and occupancy_ok and concentration_ok and best_is_current_default and (stable_margin or no_material_gain_over_current):
        level = "cash_buffer_branch_default_frozen"
        reason = "current honest cash-buffer branch is already good enough; small refinements do not justify further spec churn"
    elif honest_ok and performance_ok:
        level = "cash_buffer_branch_default"
        reason = "a refined honest cash-buffer candidate is strong enough to act as the branch default, but the spec is not fully frozen yet"
    else:
        level = "cash_buffer_branch_candidate"
        reason = "the branch is real, but concentration / occupancy / 2022 behavior still need one more tightening pass"

    keep_parallel = bool(float(best_refined["oos_cagr_minus_qqq"]) > 0 and float(best_refined["oos_max_drawdown"]) >= -0.25)
    role = "并行分支" if keep_parallel else "次级实验"
    return {
        "current_default": str(current_default["strategy"]),
        "selected_refined_default": str(best_refined["strategy"]),
        "full_deployment_reference": str(full_deployment_reference["strategy"]),
        "research_recommendation": level,
        "keep_parallel_branch": keep_parallel,
        "role_vs_qqq_plus_current_default": role,
        "reason": reason,
        "checks": {
            "best_is_current_default": best_is_current_default,
            "performance_ok": performance_ok,
            "honest_ok": honest_ok,
            "occupancy_ok": occupancy_ok,
            "concentration_ok": concentration_ok,
            "stable_margin": stable_margin,
            "no_material_gain_over_current": no_material_gain_over_current,
            "regime_micro_tuning_helped_2022": regime_2022_helped,
            "selected_oos_cagr_minus_qqq": float(best_refined["oos_cagr_minus_qqq"]),
            "selected_2022": float(best_refined["return_2022"]),
            "selected_oos_max_drawdown": float(best_refined["oos_max_drawdown"]),
            "selected_annual_turnover": float(best_refined["annual_turnover"]),
            "selected_avg_names_held": float(best_refined["avg_names_held"]),
            "selected_avg_top3_stock_weight": float(best_refined["avg_top3_stock_weight"]),
            "selected_risk_on_fill_rate": float(best_refined["risk_on_fill_rate"]),
            "selected_deployment_honesty_score": float(best_refined["deployment_honesty_score"]),
            "qqq_plus_oos_cagr": float(qqq_plus_oos["CAGR"]),
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
    branch_default: dict[str, object],
    reference_params: list[dict[str, object]],
    candidate_summary: pd.DataFrame,
    deployment_summary: pd.DataFrame,
    comparison_5bps: pd.DataFrame,
    recommendation: dict[str, object],
) -> None:
    lines = [
        "# growth_pullback_systematic_v1.4 cash buffer branch refinement",
        "",
        "## Current branch default parameters",
        format_table(pd.DataFrame([branch_default])),
        "",
        "## Reference objects",
        format_table(pd.DataFrame(reference_params)),
        "",
        "## Candidate ranking",
        format_table(candidate_summary[[
            "strategy", "base_shape", "regime_variant", "adv_bucket", "hold_bonus_bucket",
            "holdings_count", "single_name_cap", "sector_cap", "soft_defense_exposure",
            "full_cagr", "oos_cagr", "oos_cagr_minus_qqq", "return_2022", "oos_max_drawdown",
            "annual_turnover", "avg_names_held", "avg_top3_stock_weight", "cash_buffer_refinement_score"
        ]]),
        "",
        "## Deployment consistency",
        format_table(deployment_summary[[
            "strategy", "avg_names_held", "risk_on_avg_names", "risk_on_realized_stock_weight", "risk_on_fill_rate",
            "underfilled_month_share", "avg_top1_stock_weight", "avg_top3_stock_weight", "avg_top5_stock_weight",
            "avg_safe_haven_weight", "share_2022_risk_on", "share_2022_soft_defense", "share_2022_hard_defense", "avg_2022_stock_weight"
        ]]),
        "",
        "## Main comparison (5 bps)",
        format_table(comparison_5bps[[
            "strategy", "period", "CAGR", "Max Drawdown", "Turnover/Year", "Average Names Held",
            "risk_on_realized_stock_weight", "beta_vs_qqq", "alpha_ann_vs_qqq", "Information Ratio vs QQQ",
            "Up Capture vs QQQ", "Down Capture vs QQQ"
        ]]),
        "",
        "## Recommendation",
        f"- research_recommendation={recommendation['research_recommendation']}",
        f"- selected_refined_default={recommendation['selected_refined_default']}",
        f"- keep_parallel_branch={recommendation['keep_parallel_branch']}",
        f"- role_vs_qqq_plus_current_default={recommendation['role_vs_qqq_plus_current_default']}",
        f"- reason={recommendation['reason']}",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = parse_args()
    results_dir = Path(args.results_dir).expanduser().resolve()
    configs_dir = Path(args.configs_dir).expanduser().resolve()
    results_dir.mkdir(parents=True, exist_ok=True)
    configs_dir.mkdir(parents=True, exist_ok=True)

    center_cfg = gp.load_spec_config(resolve_center_config(configs_dir))
    cash_candidates = build_cash_buffer_candidates(center_cfg)
    reference_candidates = build_reference_candidates(center_cfg)

    alias_dir = robust.discover_run_dir(args.alias_data_run_dir, "r1000_multifactor_defensive_*_official_monthly_v2_alias")
    context = gp.build_context(alias_dir, start=args.start, end=args.end)
    benchmark_returns = context["stock_returns_matrix"]["QQQ"].copy()

    candidate_rows: list[dict[str, object]] = []
    deployment_rows: list[dict[str, object]] = []
    for candidate in cash_candidates:
        rows, artifacts_by_cost, monthly = evaluate_candidate(candidate, context, benchmark_returns)
        candidate_rows.extend(rows)
        deployment_summary = v12.summarize_deployment(monthly)
        deployment_rows.append({
            **deployment_summary,
            **summarize_2022_deployment(monthly),
            **v13.spec_consistency_metrics(candidate, pd.Series(deployment_summary)),
            **candidate_config_fields(candidate),
        })

    reference_rows: list[dict[str, object]] = []
    reference_deploy_rows: list[dict[str, object]] = []
    for reference in reference_candidates:
        rows, artifacts_by_cost, monthly = evaluate_reference(reference, context, benchmark_returns)
        reference_rows.extend(rows)
        deployment_summary = v12.summarize_deployment(monthly)
        reference_deploy_rows.append({
            **deployment_summary,
            **summarize_2022_deployment(monthly),
            **v13.spec_consistency_metrics(reference, pd.Series(deployment_summary)),
            **candidate_config_fields(reference),
        })

    candidate_rows_df = pd.DataFrame(candidate_rows)
    deployment_summary_df = pd.DataFrame(deployment_rows)
    reference_rows_df = pd.DataFrame(reference_rows)
    reference_deployment_df = pd.DataFrame(reference_deploy_rows)

    qqq_reference_rows, reference_artifacts = gp.build_reference_rows(context, COST_LEVELS)
    qqq_oos_cagr = float(v12.extract_period_row(qqq_reference_rows, "QQQ", MAIN_COST_BPS, "OOS Sample")["CAGR"])

    candidate_summary_df = add_refinement_scores(build_summary_rows(candidate_rows_df, deployment_summary_df, qqq_oos_cagr))
    reference_summary_df = build_summary_rows(reference_rows_df, reference_deployment_df, qqq_oos_cagr)

    current_default = candidate_summary_df.loc[candidate_summary_df["strategy"] == "coherent_cash_buffer_branch"].iloc[0]
    best_refined = candidate_summary_df.iloc[0]
    full_deployment_reference = reference_summary_df.loc[reference_summary_df["strategy"] == "coherent_full_deployment_branch"].iloc[0]
    qqq_plus_oos = v12.extract_period_row(qqq_reference_rows, "qqq_plus_current_default", MAIN_COST_BPS, "OOS Sample")

    recommendation = build_recommendation(
        current_default=current_default,
        best_refined=best_refined,
        qqq_plus_oos=qqq_plus_oos,
        full_deployment_reference=full_deployment_reference,
        all_candidates=candidate_summary_df,
    )

    comparison_df = pd.concat(
        [
            candidate_rows_df.loc[candidate_rows_df["cost_bps_one_way"] == MAIN_COST_BPS].copy(),
            reference_rows_df.loc[reference_rows_df["cost_bps_one_way"] == MAIN_COST_BPS].copy(),
            qqq_reference_rows.loc[qqq_reference_rows["cost_bps_one_way"] == MAIN_COST_BPS].copy(),
        ],
        ignore_index=True,
    )
    comparison_df = comparison_df.merge(
        pd.concat(
            [
                deployment_summary_df[["strategy", "risk_on_realized_stock_weight", "avg_names_held"]],
                reference_deployment_df[["strategy", "risk_on_realized_stock_weight", "avg_names_held"]],
                pd.DataFrame([
                    v12.summarize_reference_deployment("qqq_plus_current_default", reference_artifacts["qqq_plus_current_default"][MAIN_COST_BPS]),
                    v12.summarize_reference_deployment("aggressive_alt_spec", reference_artifacts["aggressive_alt_spec"][MAIN_COST_BPS]),
                    v12.summarize_reference_deployment("defensive_baseline", reference_artifacts["defensive_baseline"][MAIN_COST_BPS]),
                    {"strategy": "QQQ", "risk_on_realized_stock_weight": 1.0, "avg_names_held": 1.0},
                ]),
            ],
            ignore_index=True,
        ),
        on="strategy",
        how="left",
    )

    relevant_strategies = [
        "coherent_cash_buffer_branch",
        str(best_refined["strategy"]),
        "coherent_full_deployment_branch",
        "qqq_plus_current_default",
        "aggressive_alt_spec",
        "defensive_baseline",
        "QQQ",
    ]
    comparison_df = comparison_df.loc[comparison_df["strategy"].isin(relevant_strategies)].copy()

    candidate_summary_df.to_csv(results_dir / "growth_pullback_v1_4_cash_buffer_refinement.csv", index=False)
    deployment_summary_df.to_csv(results_dir / "growth_pullback_v1_4_deployment_consistency.csv", index=False)
    (results_dir / "growth_pullback_v1_4_recommendation.json").write_text(
        json.dumps(recommendation, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    write_markdown_report(
        results_dir / "growth_pullback_v1_4_cash_buffer_refinement.md",
        branch_default={
            "strategy": "coherent_cash_buffer_branch",
            "holdings_count": 8,
            "single_name_cap": 0.10,
            "sector_cap": 0.40,
            "risk_on": 0.80,
            "soft_defense": 0.60,
            "hard_defense": 0.00,
            "hold_bonus": 0.05,
            "min_adv20_usd": 50_000_000.0,
            "soft_threshold": 0.55,
            "hard_threshold": 0.35,
        },
        reference_params=[
            {
                "strategy": "center_current",
                "holdings_count": 12,
                "single_name_cap": 0.10,
                "sector_cap": 0.40,
                "risk_on": 1.00,
                "soft_defense": 0.60,
                "hard_defense": 0.00,
                "hold_bonus": 0.05,
                "min_adv20_usd": 50_000_000.0,
                "soft_threshold": 0.55,
                "hard_threshold": 0.35,
            },
            {
                "strategy": "qqq_plus_current_default",
                "reference": "frozen offensive research default",
            },
            {
                "strategy": "coherent_full_deployment_branch",
                "holdings_count": 10,
                "single_name_cap": 0.10,
                "sector_cap": 0.50,
                "risk_on": 1.00,
                "soft_defense": 0.60,
                "hard_defense": 0.00,
                "hold_bonus": 0.05,
                "min_adv20_usd": 50_000_000.0,
                "soft_threshold": 0.55,
                "hard_threshold": 0.35,
            },
        ],
        candidate_summary=candidate_summary_df,
        deployment_summary=deployment_summary_df,
        comparison_5bps=comparison_df,
        recommendation=recommendation,
    )

    print(f"alias data: {alias_dir}")
    print(f"selected refined default: {recommendation['selected_refined_default']}")
    print(f"research recommendation: {recommendation['research_recommendation']}")
    print(f"results written to: {results_dir}")


if __name__ == "__main__":
    main()
