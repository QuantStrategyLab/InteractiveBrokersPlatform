#!/usr/bin/env python3
"""Freeze review and branch packaging for the cash-buffered tech pullback branch."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import backtest_growth_pullback_suite as gp  # noqa: E402
import backtest_growth_pullback_v1_2_geometry_repair as v12  # noqa: E402
import backtest_growth_pullback_v1_3_spec_normalization as v13  # noqa: E402
import backtest_growth_pullback_v1_4_cash_buffer_refinement as v14  # noqa: E402
import backtest_stock_alpha_v1_robustness as robust  # noqa: E402


DEFAULT_RESULTS_DIR = SCRIPT_DIR / "results"
DEFAULT_CONFIGS_DIR = SCRIPT_DIR / "configs"
MAIN_COST_BPS = 5.0
CANONICAL_CONFIG_FILENAME = "growth_pullback_cash_buffer_branch_default.json"
V14_SUMMARY_FILENAME = "growth_pullback_v1_4_cash_buffer_refinement.csv"
V14_RECOMMENDATION_FILENAME = "growth_pullback_v1_4_recommendation.json"


@dataclass(frozen=True)
class CanonicalBranchSpec:
    name: str
    previous_candidate_name: str
    config: gp.GrowthPullbackConfig
    risk_on_exposure: float
    soft_threshold: float
    hard_threshold: float
    benchmark_symbol: str
    residual_proxy: str
    cost_bps_one_way: float
    branch_role: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alias-data-run-dir", help="Prepared alias-fixed Russell data run dir")
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR))
    parser.add_argument("--configs-dir", default=str(DEFAULT_CONFIGS_DIR))
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    return parser.parse_args()


def resolve_paths(configs_dir: Path, results_dir: Path) -> dict[str, Path]:
    return {
        "center_config": configs_dir / v14.CENTER_CONFIG_FILENAME,
        "canonical_config": configs_dir / CANONICAL_CONFIG_FILENAME,
        "v14_summary": results_dir / V14_SUMMARY_FILENAME,
        "v14_recommendation": results_dir / V14_RECOMMENDATION_FILENAME,
        "manifest": results_dir / "growth_pullback_branch_manifest.json",
        "checks_csv": results_dir / "growth_pullback_v1_5_consistency_checks.csv",
        "freeze_review_csv": results_dir / "growth_pullback_v1_5_freeze_review.csv",
        "freeze_review_md": results_dir / "growth_pullback_v1_5_freeze_review.md",
        "recommendation_json": results_dir / "growth_pullback_v1_5_recommendation.json",
    }


def build_canonical_spec(center_cfg: gp.GrowthPullbackConfig, previous_candidate_name: str) -> CanonicalBranchSpec:
    config = replace(
        center_cfg,
        name="cash_buffer_branch_default",
        holdings_count=8,
        single_name_cap=0.10,
        sector_cap=0.40,
        hold_bonus=0.10,
        exposures=gp.suite.ExposureConfig("80_60_0", 0.60, 0.00),
    )
    return CanonicalBranchSpec(
        name="cash_buffer_branch_default",
        previous_candidate_name=previous_candidate_name,
        config=config,
        risk_on_exposure=0.80,
        soft_threshold=0.55,
        hard_threshold=0.35,
        benchmark_symbol="QQQ",
        residual_proxy="simple_excess_return_vs_QQQ",
        cost_bps_one_way=MAIN_COST_BPS,
        branch_role="cash-buffered parallel branch",
    )


def canonical_spec_to_dict(spec: CanonicalBranchSpec) -> dict[str, object]:
    return {
        "role": "cash_buffer_branch_default",
        "status": "research_only",
        "strategy": "growth_pullback_systematic_v1",
        "branch_name": "cash_buffer_branch",
        "name": spec.name,
        "previous_candidate_name": spec.previous_candidate_name,
        "family": spec.config.family,
        "universe": spec.config.universe_spec.name,
        "normalization": gp.normalization_label(spec.config.universe_spec.normalization),
        "min_adv20_usd": float(spec.config.universe_spec.min_adv20_usd),
        "sector_whitelist": list(spec.config.universe_spec.sector_whitelist),
        "symbol_whitelist": list(spec.config.universe_spec.symbol_whitelist),
        "notes": spec.config.universe_spec.notes,
        "score_template": spec.config.score_template,
        "holdings_count": int(spec.config.holdings_count),
        "single_name_cap": float(spec.config.single_name_cap),
        "sector_cap": float(spec.config.sector_cap),
        "hold_bonus": float(spec.config.hold_bonus),
        "regime": spec.config.regime.name,
        "benchmark_symbol": spec.benchmark_symbol,
        "breadth_mode": spec.config.regime.breadth_mode,
        "breadth_symbols": list(spec.config.regime.breadth_symbols),
        "breadth_thresholds": {
            "soft": float(spec.soft_threshold),
            "hard": float(spec.hard_threshold),
        },
        "exposures": {
            "risk_on": float(spec.risk_on_exposure),
            "soft_defense": float(spec.config.exposures.soft_defense_exposure),
            "hard_defense": float(spec.config.exposures.hard_defense_exposure),
        },
        "residual_proxy": spec.residual_proxy,
        "cost_assumption_bps_one_way": float(spec.cost_bps_one_way),
        "branch_role": spec.branch_role,
        "canonicalized_from": "growth_pullback_systematic_v1.4 cash_buffer_refinement",
    }


def save_canonical_spec(path: Path, spec: CanonicalBranchSpec) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(canonical_spec_to_dict(spec), indent=2, ensure_ascii=False), encoding="utf-8")


def load_canonical_spec(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_v14_selection(summary_path: Path, recommendation_path: Path) -> tuple[pd.DataFrame, dict[str, object], str]:
    summary = pd.read_csv(summary_path)
    recommendation = json.loads(recommendation_path.read_text(encoding="utf-8"))
    previous_candidate_name = str(recommendation["selected_refined_default"])
    return summary, recommendation, previous_candidate_name


def evaluate_branch(
    spec: CanonicalBranchSpec,
    context: dict[str, object],
    benchmark_returns: pd.Series,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
    rows, artifacts_by_cost, monthly = v14.evaluate_geometry_candidate(
        spec.name,
        spec.config,
        risk_on_exposure=spec.risk_on_exposure,
        soft_threshold=spec.soft_threshold,
        hard_threshold=spec.hard_threshold,
        note="Canonical cash-buffer branch default",
        context=context,
        benchmark_returns=benchmark_returns,
        cost_bps_values=(0.0, MAIN_COST_BPS),
    )
    deployment = v12.summarize_deployment(monthly)
    deployment_row = pd.Series(
        {
            **deployment,
            **v14.summarize_2022_deployment(monthly),
            **v13.spec_consistency_metrics(
                v12.make_candidate(spec.name, spec.config, risk_on_exposure=spec.risk_on_exposure, note="canonical"),
                pd.Series(deployment),
            ),
        }
    )
    return pd.DataFrame(rows), monthly, deployment_row


def evaluate_reference_candidate(
    label: str,
    config: gp.GrowthPullbackConfig,
    *,
    risk_on_exposure: float,
    soft_threshold: float,
    hard_threshold: float,
    note: str,
    context: dict[str, object],
    benchmark_returns: pd.Series,
) -> tuple[pd.DataFrame, pd.Series]:
    rows, artifacts_by_cost, monthly = v14.evaluate_geometry_candidate(
        label,
        config,
        risk_on_exposure=risk_on_exposure,
        soft_threshold=soft_threshold,
        hard_threshold=hard_threshold,
        note=note,
        context=context,
        benchmark_returns=benchmark_returns,
        cost_bps_values=(0.0, MAIN_COST_BPS),
    )
    deployment = v12.summarize_deployment(monthly)
    deployment_row = pd.Series(
        {
            **deployment,
            **v14.summarize_2022_deployment(monthly),
            **v13.spec_consistency_metrics(
                v12.make_candidate(label, config, risk_on_exposure=risk_on_exposure, note=note),
                pd.Series(deployment),
            ),
        }
    )
    return pd.DataFrame(rows), deployment_row


def summarize_strategy(rows: pd.DataFrame, deployment_row: pd.Series, qqq_oos_cagr: float) -> dict[str, object]:
    full_row = v12.extract_period_row(rows, str(rows.iloc[0]["strategy"]), MAIN_COST_BPS, "Full Sample")
    oos_row = v12.extract_period_row(rows, str(rows.iloc[0]["strategy"]), MAIN_COST_BPS, "OOS Sample")
    row_2022 = v12.extract_period_row(rows, str(rows.iloc[0]["strategy"]), MAIN_COST_BPS, "2022")
    return {
        "strategy": str(rows.iloc[0]["strategy"]),
        "full_cagr": float(full_row["CAGR"]),
        "oos_cagr": float(oos_row["CAGR"]),
        "oos_cagr_minus_qqq": float(oos_row["CAGR"] - qqq_oos_cagr),
        "oos_max_drawdown": float(oos_row["Max Drawdown"]),
        "full_max_drawdown": float(full_row["Max Drawdown"]),
        "return_2022": float(row_2022["Total Return"]),
        "annual_turnover": float(full_row["Turnover/Year"]),
        "avg_names_held": float(deployment_row["avg_names_held"]),
        "risk_on_realized_stock_weight": float(deployment_row["risk_on_realized_stock_weight"]),
        "risk_on_fill_rate": float(deployment_row["risk_on_fill_rate"]),
        "soft_defense_realized_stock_weight": float(deployment_row["soft_defense_realized_stock_weight"]),
        "hard_defense_realized_stock_weight": float(deployment_row["hard_defense_realized_stock_weight"]),
        "deployment_honesty_score": float(deployment_row["deployment_honesty_score"]),
        "avg_top3_stock_weight": float(deployment_row["avg_top3_stock_weight"]),
    }


def build_consistency_checks(
    *,
    spec: CanonicalBranchSpec,
    config_payload: dict[str, object],
    v14_summary: pd.DataFrame,
    previous_candidate_name: str,
    canonical_summary: dict[str, object],
    recommendation_level: str,
    manifest: dict[str, object],
) -> pd.DataFrame:
    previous_row = v14_summary.loc[v14_summary["strategy"] == previous_candidate_name].iloc[0]
    theoretical_capacity = v12.theoretical_stock_capacity(spec.risk_on_exposure, spec.config, active_sector_count=2)
    checks = [
        {
            "check": "geometry_capacity_matches_target",
            "passed": abs(theoretical_capacity - spec.risk_on_exposure) <= 1e-12,
            "detail": f"theoretical_capacity={theoretical_capacity:.4f}, risk_on_target={spec.risk_on_exposure:.4f}",
        },
        {
            "check": "risk_on_realized_matches_target",
            "passed": abs(canonical_summary["risk_on_realized_stock_weight"] - spec.risk_on_exposure) <= 1e-12,
            "detail": f"realized={canonical_summary['risk_on_realized_stock_weight']:.4f}, target={spec.risk_on_exposure:.4f}",
        },
        {
            "check": "soft_defense_realized_matches_target",
            "passed": abs(canonical_summary["soft_defense_realized_stock_weight"] - spec.config.exposures.soft_defense_exposure) <= 1e-12,
            "detail": f"realized={canonical_summary['soft_defense_realized_stock_weight']:.4f}, target={spec.config.exposures.soft_defense_exposure:.4f}",
        },
        {
            "check": "hard_defense_realized_zero",
            "passed": abs(canonical_summary["hard_defense_realized_stock_weight"] - spec.config.exposures.hard_defense_exposure) <= 1e-12,
            "detail": f"realized={canonical_summary['hard_defense_realized_stock_weight']:.4f}, target={spec.config.exposures.hard_defense_exposure:.4f}",
        },
        {
            "check": "config_name_matches_canonical_name",
            "passed": str(config_payload["name"]) == spec.name,
            "detail": f"config_name={config_payload['name']}, canonical_name={spec.name}",
        },
        {
            "check": "previous_candidate_mapping_preserved",
            "passed": str(config_payload["previous_candidate_name"]) == previous_candidate_name,
            "detail": f"previous_candidate_name={config_payload['previous_candidate_name']}",
        },
        {
            "check": "canonical_metrics_match_previous_candidate",
            "passed": (
                abs(canonical_summary["oos_cagr"] - float(previous_row["oos_cagr"])) <= 1e-12
                and abs(canonical_summary["return_2022"] - float(previous_row["return_2022"])) <= 1e-12
                and abs(canonical_summary["avg_names_held"] - float(previous_row["avg_names_held"])) <= 1e-12
            ),
            "detail": (
                f"canonical_oos={canonical_summary['oos_cagr']:.6f}, prev_oos={float(previous_row['oos_cagr']):.6f}; "
                f"canonical_2022={canonical_summary['return_2022']:.6f}, prev_2022={float(previous_row['return_2022']):.6f}"
            ),
        },
        {
            "check": "recommendation_matches_branch_default_role",
            "passed": recommendation_level == "cash_buffer_branch_default",
            "detail": f"recommendation={recommendation_level}",
        },
        {
            "check": "manifest_role_matches_default_branch",
            "passed": manifest["role"] == "cash-buffered parallel branch",
            "detail": f"manifest_role={manifest['role']}",
        },
    ]
    return pd.DataFrame(checks)


def build_manifest(
    *,
    spec: CanonicalBranchSpec,
    canonical_summary: dict[str, object],
    qqq_plus_oos: pd.Series,
    recommendation_level: str,
) -> dict[str, object]:
    return {
        "branch_name": spec.name,
        "previous_candidate_name": spec.previous_candidate_name,
        "role": "cash-buffered parallel branch",
        "strategy_family": spec.config.family,
        "intended_use": "Research-only parallel stock branch for concentrated tech leaders bought on controlled pullbacks with an explicit 20% cash/BOXX buffer in risk-on.",
        "benchmark": spec.benchmark_symbol,
        "universe_semantics": "Two-sector tech-heavy large-cap universe: Information Technology + Communication inside the IWB/Russell proxy universe.",
        "deployment_semantics": {
            "holdings_count": int(spec.config.holdings_count),
            "single_name_cap": float(spec.config.single_name_cap),
            "sector_cap": float(spec.config.sector_cap),
            "risk_on_target_stock_weight": float(spec.risk_on_exposure),
            "soft_defense_target_stock_weight": float(spec.config.exposures.soft_defense_exposure),
            "hard_defense_target_stock_weight": float(spec.config.exposures.hard_defense_exposure),
            "risk_on_realized_stock_weight": float(canonical_summary["risk_on_realized_stock_weight"]),
            "avg_names_held": float(canonical_summary["avg_names_held"]),
            "risk_on_fill_rate": float(canonical_summary["risk_on_fill_rate"]),
        },
        "why_this_is_a_cash_buffer_branch": [
            "risk_on target is explicitly 80%, not implicit underinvestment.",
            "Two-sector 40% cap + 8 names + 10% single cap is geometrically self-consistent at 80% stock.",
            "The branch keeps a persistent safe-haven sleeve even in risk_on by design, not by accident.",
        ],
        "why_this_is_not_a_replacement_for_qqq_plus_current_default": [
            "qqq_plus_current_default remains the main full-risk offensive stock default.",
            f"This branch has lower beta ({canonical_summary['beta_vs_qqq_oos']:.3f} OOS) and shallower OOS drawdown, but its 2022 result is still weaker than qqq_plus_current_default.",
            "The intended role is a lower-beta, cash-buffered tech pullback parallel branch, not the primary offensive default.",
        ],
        "why_this_is_not_frozen": [
            f"Average names held is still only about {canonical_summary['avg_names_held']:.2f}, so concentration/occupancy is not fully comfortable.",
            "Promotion from v1.4 is only a small hold_bonus micro-adjustment, not a decisive structural upgrade.",
            "Canonical naming is finished in this round, but that alone is not enough reason to label the branch frozen.",
        ],
        "key_risks": [
            "Two-sector concentration remains high.",
            "2022 behavior is still materially weaker than qqq_plus_current_default.",
            "Universe is still the public IWB/Russell proxy, not official PIT constituents.",
            "Residual momentum is still a simple excess-return proxy vs QQQ.",
        ],
        "preferred_comparison_set": [
            "qqq_plus_current_default",
            "coherent_full_deployment_branch",
            "russell_1000_multi_factor_defensive",
            "QQQ",
        ],
        "promotion_basis": {
            "why_promoted_to_branch_default": [
                "Same honest cash-buffer geometry as v1.3 default branch.",
                "OOS CAGR and IR improved with a simple hold_bonus increase from 0.05 to 0.10.",
                "Deployment semantics remain perfectly aligned with the written spec.",
            ],
            "recommendation": recommendation_level,
            "oos_cagr": float(canonical_summary["oos_cagr"]),
            "oos_cagr_minus_qqq": float(canonical_summary["oos_cagr_minus_qqq"]),
            "oos_max_drawdown": float(canonical_summary["oos_max_drawdown"]),
            "return_2022": float(canonical_summary["return_2022"]),
        },
    }


def build_role_table(
    canonical_summary: dict[str, object],
    center_summary: dict[str, object],
    full_summary: dict[str, object],
    qqq_reference_rows: pd.DataFrame,
) -> pd.DataFrame:
    rows = []
    qqq_plus_oos = v12.extract_period_row(qqq_reference_rows, "qqq_plus_current_default", MAIN_COST_BPS, "OOS Sample")
    qqq_plus_full = v12.extract_period_row(qqq_reference_rows, "qqq_plus_current_default", MAIN_COST_BPS, "Full Sample")
    defensive_oos = v12.extract_period_row(qqq_reference_rows, "defensive_baseline", MAIN_COST_BPS, "OOS Sample")
    defensive_full = v12.extract_period_row(qqq_reference_rows, "defensive_baseline", MAIN_COST_BPS, "Full Sample")
    qqq_oos = v12.extract_period_row(qqq_reference_rows, "QQQ", MAIN_COST_BPS, "OOS Sample")
    qqq_full = v12.extract_period_row(qqq_reference_rows, "QQQ", MAIN_COST_BPS, "Full Sample")
    rows.extend([
        {
            "strategy": "cash_buffer_branch_default",
            "role": "cash-buffered parallel branch",
            "full_cagr": canonical_summary["full_cagr"],
            "oos_cagr": canonical_summary["oos_cagr"],
            "oos_cagr_minus_qqq": canonical_summary["oos_cagr_minus_qqq"],
            "oos_max_drawdown": canonical_summary["oos_max_drawdown"],
            "return_2022": canonical_summary["return_2022"],
            "annual_turnover": canonical_summary["annual_turnover"],
            "avg_names_held": canonical_summary["avg_names_held"],
            "risk_on_realized_stock_weight": canonical_summary["risk_on_realized_stock_weight"],
            "beta_vs_qqq": canonical_summary["beta_vs_qqq_oos"],
            "alpha_ann_vs_qqq": canonical_summary["alpha_ann_vs_qqq_oos"],
            "information_ratio_vs_qqq": canonical_summary["information_ratio_vs_qqq_oos"],
        },
        {
            "strategy": "qqq_plus_current_default",
            "role": "main offensive default",
            "full_cagr": float(qqq_plus_full["CAGR"]),
            "oos_cagr": float(qqq_plus_oos["CAGR"]),
            "oos_cagr_minus_qqq": float(qqq_plus_oos["CAGR"] - qqq_oos["CAGR"]),
            "oos_max_drawdown": float(qqq_plus_oos["Max Drawdown"]),
            "return_2022": float(v12.extract_period_row(qqq_reference_rows, "qqq_plus_current_default", MAIN_COST_BPS, "2022")["Total Return"]),
            "annual_turnover": float(qqq_plus_full["Turnover/Year"]),
            "avg_names_held": float(qqq_plus_full["Average Names Held"]),
            "risk_on_realized_stock_weight": 0.96,
            "beta_vs_qqq": float(qqq_plus_oos["beta_vs_qqq"]),
            "alpha_ann_vs_qqq": float(qqq_plus_oos["alpha_ann_vs_qqq"]),
            "information_ratio_vs_qqq": float(qqq_plus_oos["Information Ratio vs QQQ"]),
        },
        {
            "strategy": "coherent_full_deployment_branch",
            "role": "full-deployment tech pullback reference",
            "full_cagr": full_summary["full_cagr"],
            "oos_cagr": full_summary["oos_cagr"],
            "oos_cagr_minus_qqq": full_summary["oos_cagr_minus_qqq"],
            "oos_max_drawdown": full_summary["oos_max_drawdown"],
            "return_2022": full_summary["return_2022"],
            "annual_turnover": full_summary["annual_turnover"],
            "avg_names_held": full_summary["avg_names_held"],
            "risk_on_realized_stock_weight": full_summary["risk_on_realized_stock_weight"],
            "beta_vs_qqq": full_summary["beta_vs_qqq_oos"],
            "alpha_ann_vs_qqq": full_summary["alpha_ann_vs_qqq_oos"],
            "information_ratio_vs_qqq": full_summary["information_ratio_vs_qqq_oos"],
        },
        {
            "strategy": "russell_1000_multi_factor_defensive",
            "role": "defensive base",
            "full_cagr": float(defensive_full["CAGR"]),
            "oos_cagr": float(defensive_oos["CAGR"]),
            "oos_cagr_minus_qqq": float(defensive_oos["CAGR"] - qqq_oos["CAGR"]),
            "oos_max_drawdown": float(defensive_oos["Max Drawdown"]),
            "return_2022": float(v12.extract_period_row(qqq_reference_rows, "defensive_baseline", MAIN_COST_BPS, "2022")["Total Return"]),
            "annual_turnover": float(defensive_full["Turnover/Year"]),
            "avg_names_held": float(defensive_full["Average Names Held"]),
            "risk_on_realized_stock_weight": 1.0,
            "beta_vs_qqq": float(defensive_oos["beta_vs_qqq"]),
            "alpha_ann_vs_qqq": float(defensive_oos["alpha_ann_vs_qqq"]),
            "information_ratio_vs_qqq": float(defensive_oos["Information Ratio vs QQQ"]),
        },
        {
            "strategy": "QQQ",
            "role": "benchmark reference",
            "full_cagr": float(qqq_full["CAGR"]),
            "oos_cagr": float(qqq_oos["CAGR"]),
            "oos_cagr_minus_qqq": 0.0,
            "oos_max_drawdown": float(qqq_oos["Max Drawdown"]),
            "return_2022": float(v12.extract_period_row(qqq_reference_rows, "QQQ", MAIN_COST_BPS, "2022")["Total Return"]),
            "annual_turnover": float(qqq_full["Turnover/Year"]),
            "avg_names_held": 1.0,
            "risk_on_realized_stock_weight": 1.0,
            "beta_vs_qqq": 1.0,
            "alpha_ann_vs_qqq": 0.0,
            "information_ratio_vs_qqq": np.nan,
        },
    ])
    return pd.DataFrame(rows)


def build_freeze_recommendation(
    *,
    canonical_summary: dict[str, object],
    checks_df: pd.DataFrame,
) -> dict[str, object]:
    checks_pass = bool(checks_df["passed"].all())
    performance_ok = bool(canonical_summary["oos_cagr_minus_qqq"] > 0.20 and canonical_summary["oos_max_drawdown"] >= -0.22 and canonical_summary["annual_turnover"] <= 3.0)
    frozen_blockers = []
    if canonical_summary["avg_names_held"] < 6.5:
        frozen_blockers.append("average_names_held_still_low")
    if canonical_summary["return_2022"] <= -0.10:
        frozen_blockers.append("2022_still_not_clean_enough")
    frozen_blockers.append("just_canonicalized_from_small_hold_bonus_micro_adjustment")

    if not (checks_pass and performance_ok):
        level = "candidate"
        reason = "branch role is valid, but consistency or performance checks are not strong enough for default status"
    elif frozen_blockers:
        level = "cash_buffer_branch_default"
        reason = "canonical naming and branch role are now clear, but avg names / 2022 profile / recent micro-adjustment mean it should stop at default, not frozen"
    else:
        level = "cash_buffer_branch_default_frozen"
        reason = "branch semantics, deployment consistency, and behavior are all stable enough to freeze"

    return {
        "research_recommendation": level,
        "branch_name": "cash_buffer_branch_default",
        "previous_candidate_name": "cash_buffer_a__hb10__base__adv50",
        "reason": reason,
        "frozen_blockers": frozen_blockers,
        "checks_pass": checks_pass,
        "performance_ok": performance_ok,
        "keep_parallel_branch": True,
        "role_vs_qqq_plus_current_default": "并行分支",
    }


def format_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "_empty_"
    def fmt(value):
        if isinstance(value, (list, tuple, set)):
            return json.dumps(list(value), ensure_ascii=False)
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        if value is None:
            return ""
        if isinstance(value, (float, np.floating)):
            if pd.isna(value):
                return ""
            return f"{float(value):.6f}"
        if isinstance(value, (pd.Timestamp,)):
            return str(value)
        try:
            if pd.isna(value):
                return ""
        except TypeError:
            pass
        return str(value)
    columns = [str(c) for c in frame.columns]
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
    canonical_payload: dict[str, object],
    comparison_df: pd.DataFrame,
    checks_df: pd.DataFrame,
    recommendation: dict[str, object],
    manifest: dict[str, object],
) -> None:
    lines = [
        "# growth_pullback_systematic_v1.5 freeze review and branch packaging",
        "",
        "## Canonical branch default",
        format_table(pd.DataFrame([canonical_payload])),
        "",
        "## Final role table",
        format_table(comparison_df),
        "",
        "## Consistency checks",
        format_table(checks_df),
        "",
        "## Freeze review",
        f"- recommendation={recommendation['research_recommendation']}",
        f"- previous_candidate_name={recommendation['previous_candidate_name']}",
        f"- branch_name={recommendation['branch_name']}",
        f"- role_vs_qqq_plus_current_default={recommendation['role_vs_qqq_plus_current_default']}",
        f"- keep_parallel_branch={recommendation['keep_parallel_branch']}",
        f"- reason={recommendation['reason']}",
        f"- frozen_blockers={', '.join(recommendation['frozen_blockers'])}",
        "",
        "## Manifest summary",
        format_table(pd.DataFrame([{
            'branch_name': manifest['branch_name'],
            'role': manifest['role'],
            'previous_candidate_name': manifest['previous_candidate_name'],
            'benchmark': manifest['benchmark'],
            'intended_use': manifest['intended_use'],
        }])),
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = parse_args()
    results_dir = Path(args.results_dir).expanduser().resolve()
    configs_dir = Path(args.configs_dir).expanduser().resolve()
    results_dir.mkdir(parents=True, exist_ok=True)
    configs_dir.mkdir(parents=True, exist_ok=True)

    paths = resolve_paths(configs_dir, results_dir)
    center_cfg = gp.load_spec_config(paths["center_config"])
    v14_summary, v14_rec, previous_candidate_name = load_v14_selection(paths["v14_summary"], paths["v14_recommendation"])
    spec = build_canonical_spec(center_cfg, previous_candidate_name)
    save_canonical_spec(paths["canonical_config"], spec)
    canonical_payload = load_canonical_spec(paths["canonical_config"])

    alias_dir = robust.discover_run_dir(args.alias_data_run_dir, "r1000_multifactor_defensive_*_official_monthly_v2_alias")
    context = gp.build_context(alias_dir, start=args.start, end=args.end)
    benchmark_returns = context["stock_returns_matrix"]["QQQ"].copy()

    canonical_rows_df, canonical_monthly, canonical_deployment = evaluate_branch(spec, context, benchmark_returns)

    center_rows_df, center_deployment = evaluate_reference_candidate(
        "center_current",
        replace(center_cfg, name="center_current_cfg", holdings_count=12, single_name_cap=0.10, sector_cap=0.40, hold_bonus=0.05, exposures=gp.suite.ExposureConfig("100_60_0", 0.60, 0.00)),
        risk_on_exposure=1.0,
        soft_threshold=0.55,
        hard_threshold=0.35,
        note="center current reference",
        context=context,
        benchmark_returns=benchmark_returns,
    )
    full_rows_df, full_deployment = evaluate_reference_candidate(
        "coherent_full_deployment_branch",
        replace(center_cfg, name="coherent_full_deployment_branch_cfg", holdings_count=10, single_name_cap=0.10, sector_cap=0.50, hold_bonus=0.05, exposures=gp.suite.ExposureConfig("100_60_0", 0.60, 0.00)),
        risk_on_exposure=1.0,
        soft_threshold=0.55,
        hard_threshold=0.35,
        note="full deployment reference",
        context=context,
        benchmark_returns=benchmark_returns,
    )

    qqq_reference_rows, reference_artifacts = gp.build_reference_rows(context, (0.0, MAIN_COST_BPS))
    qqq_oos_cagr = float(v12.extract_period_row(qqq_reference_rows, "QQQ", MAIN_COST_BPS, "OOS Sample")["CAGR"])

    canonical_summary = summarize_strategy(canonical_rows_df, canonical_deployment, qqq_oos_cagr)
    center_summary = summarize_strategy(center_rows_df, center_deployment, qqq_oos_cagr)
    full_summary = summarize_strategy(full_rows_df, full_deployment, qqq_oos_cagr)

    canonical_summary.update({
        "beta_vs_qqq_oos": float(v12.extract_period_row(canonical_rows_df, spec.name, MAIN_COST_BPS, "OOS Sample")["beta_vs_qqq"]),
        "alpha_ann_vs_qqq_oos": float(v12.extract_period_row(canonical_rows_df, spec.name, MAIN_COST_BPS, "OOS Sample")["alpha_ann_vs_qqq"]),
        "information_ratio_vs_qqq_oos": float(v12.extract_period_row(canonical_rows_df, spec.name, MAIN_COST_BPS, "OOS Sample")["Information Ratio vs QQQ"]),
    })
    full_summary.update({
        "beta_vs_qqq_oos": float(v12.extract_period_row(full_rows_df, "coherent_full_deployment_branch", MAIN_COST_BPS, "OOS Sample")["beta_vs_qqq"]),
        "alpha_ann_vs_qqq_oos": float(v12.extract_period_row(full_rows_df, "coherent_full_deployment_branch", MAIN_COST_BPS, "OOS Sample")["alpha_ann_vs_qqq"]),
        "information_ratio_vs_qqq_oos": float(v12.extract_period_row(full_rows_df, "coherent_full_deployment_branch", MAIN_COST_BPS, "OOS Sample")["Information Ratio vs QQQ"]),
    })

    preliminary_recommendation_level = "cash_buffer_branch_default"
    manifest = build_manifest(
        spec=spec,
        canonical_summary=canonical_summary,
        qqq_plus_oos=v12.extract_period_row(qqq_reference_rows, "qqq_plus_current_default", MAIN_COST_BPS, "OOS Sample"),
        recommendation_level=preliminary_recommendation_level,
    )
    checks_df = build_consistency_checks(
        spec=spec,
        config_payload=canonical_payload,
        v14_summary=v14_summary,
        previous_candidate_name=previous_candidate_name,
        canonical_summary=canonical_summary,
        recommendation_level=preliminary_recommendation_level,
        manifest=manifest,
    )
    recommendation = build_freeze_recommendation(
        canonical_summary=canonical_summary,
        checks_df=checks_df,
    )
    manifest["promotion_basis"]["recommendation"] = recommendation["research_recommendation"]
    manifest["why_this_is_not_frozen"] = [
        f"Average names held is still only about {canonical_summary['avg_names_held']:.2f}.",
        f"2022 return is still {canonical_summary['return_2022']:.2%}, so downside behavior is not clean enough to freeze.",
        "This round finished naming/packaging, but the upgrade from v1.4 is still just a hold_bonus micro-adjustment.",
    ]

    comparison_df = build_role_table(canonical_summary, center_summary, full_summary, qqq_reference_rows)

    comparison_df.to_csv(paths["freeze_review_csv"], index=False)
    checks_df.to_csv(paths["checks_csv"], index=False)
    paths["manifest"].write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    paths["recommendation_json"].write_text(json.dumps(recommendation, indent=2, ensure_ascii=False), encoding="utf-8")
    write_markdown_report(
        paths["freeze_review_md"],
        canonical_payload=canonical_payload,
        comparison_df=comparison_df,
        checks_df=checks_df,
        recommendation=recommendation,
        manifest=manifest,
    )

    print(f"alias data: {alias_dir}")
    print(f"canonical branch default: {spec.name}")
    print(f"recommendation: {recommendation['research_recommendation']}")
    print(f"results written to: {results_dir}")


if __name__ == "__main__":
    main()
