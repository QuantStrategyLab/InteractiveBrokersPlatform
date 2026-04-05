#!/usr/bin/env python3
"""Spec normalization for growth_pullback_systematic_v1 tech-heavy branch."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import backtest_growth_pullback_suite as gp  # noqa: E402
import backtest_growth_pullback_v1_2_geometry_repair as v12  # noqa: E402
import backtest_stock_alpha_v1_robustness as robust  # noqa: E402


DEFAULT_RESULTS_DIR = SCRIPT_DIR / "results"
DEFAULT_CONFIGS_DIR = SCRIPT_DIR / "configs"
MAIN_COST_BPS = 5.0
COST_LEVELS = (0.0, MAIN_COST_BPS)
CENTER_CONFIG_FILENAME = "growth_pullback_systematic_v1_default.json"
STABLE_NEIGHBOR_CONFIG_FILENAME = "growth_pullback_systematic_v1_1_default.json"
COMPARISON_PERIODS = (
    ("Full Sample", None, None),
    ("OOS Sample", "2022-01-01", None),
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023+", "2023-01-01", None),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alias-data-run-dir", help="Prepared alias-fixed Russell data run dir")
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR))
    parser.add_argument("--configs-dir", default=str(DEFAULT_CONFIGS_DIR))
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    return parser.parse_args()


def build_candidates(center_cfg: gp.GrowthPullbackConfig, stable_cfg: gp.GrowthPullbackConfig) -> list[v12.GeometryCandidate]:
    feasible_cfg = replace(center_cfg, name=f"{center_cfg.name}__sector_cap_0p5", sector_cap=0.50)
    coherent_cash_cfg = replace(
        center_cfg,
        name="coherent_cash_buffer_branch_cfg",
        holdings_count=8,
        single_name_cap=0.10,
        sector_cap=0.40,
    )
    coherent_full_cfg = replace(
        center_cfg,
        name="coherent_full_deployment_branch_cfg",
        holdings_count=10,
        single_name_cap=0.10,
        sector_cap=0.50,
    )
    coherent_full_single8_cfg = replace(
        center_cfg,
        name="coherent_full_deployment_branch_single8_cfg",
        holdings_count=10,
        single_name_cap=0.08,
        sector_cap=0.50,
    )
    return [
        v12.make_candidate(
            "center_current",
            center_cfg,
            risk_on_exposure=1.0,
            note="Current prototype: nominal 12 names / 100% risk_on, but actual geometry binds to ~8 names and 80% stock",
        ),
        v12.make_candidate(
            "local_stable_neighbor",
            stable_cfg,
            risk_on_exposure=1.0,
            note="v1.1 local stable neighbor; lower single-cap makes the implicit cash buffer even larger",
        ),
        v12.make_candidate(
            "feasible_two_sector_50cap",
            feasible_cfg,
            risk_on_exposure=1.0,
            note="Previous geometry repair reference: two-sector 50% cap makes 12 names / 100% stock feasible",
        ),
        v12.make_candidate(
            "coherent_cash_buffer_branch",
            coherent_cash_cfg,
            risk_on_exposure=0.8,
            note="Honest cash-buffer branch: 8 names, 80% risk_on, two-sector 40% cap; nominal spec matches actual deployment",
        ),
        v12.make_candidate(
            "coherent_full_deployment_branch",
            coherent_full_cfg,
            risk_on_exposure=1.0,
            note="Honest full-deployment branch: 10 names, 100% risk_on, two-sector 50% cap; nominal spec matches actual deployment",
        ),
        v12.make_candidate(
            "coherent_full_deployment_branch_single8",
            coherent_full_single8_cfg,
            risk_on_exposure=1.0,
            note="Optional concentration-relief control: 10 names, sector 50%, single cap 8%; geometry still leaves 20% slack",
        ),
    ]


def resolve_config_paths(configs_dir: Path) -> tuple[Path, Path]:
    return (
        configs_dir / CENTER_CONFIG_FILENAME,
        configs_dir / STABLE_NEIGHBOR_CONFIG_FILENAME,
    )


def spec_consistency_metrics(candidate: v12.GeometryCandidate, summary_row: pd.Series) -> dict[str, float]:
    target_names = float(candidate.config.holdings_count)
    realized_names = float(summary_row["risk_on_avg_names"])
    target_stock = float(candidate.risk_on_exposure)
    realized_stock = float(summary_row["risk_on_realized_stock_weight"])
    names_alignment = max(0.0, 1.0 - abs(realized_names - target_names) / max(target_names, 1.0))
    stock_alignment = max(0.0, 1.0 - abs(realized_stock - target_stock) / max(target_stock, 1e-12))
    return {
        "target_names": target_names,
        "realized_names": realized_names,
        "target_stock_weight": target_stock,
        "realized_stock_weight": realized_stock,
        "names_alignment_score": names_alignment,
        "stock_alignment_score": stock_alignment,
        "deployment_honesty_score": 0.5 * names_alignment + 0.5 * stock_alignment,
    }


def percentile_rank(series: pd.Series, *, higher_is_better: bool) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    ranked = numeric.rank(method="average", pct=True) if higher_is_better else (-numeric).rank(method="average", pct=True)
    return ranked.fillna(0.0)


def add_normalization_scores(summary_df: pd.DataFrame) -> pd.DataFrame:
    scored = summary_df.copy()
    scored["score_oos_rel_qqq"] = percentile_rank(scored["oos_cagr_minus_qqq"], higher_is_better=True)
    scored["score_oos_maxdd"] = percentile_rank(scored["oos_max_drawdown"], higher_is_better=True)
    scored["score_2022"] = percentile_rank(scored["return_2022"], higher_is_better=True)
    scored["score_turnover"] = percentile_rank(scored["annual_turnover"], higher_is_better=False)
    scored["score_avg_names"] = percentile_rank(scored["avg_names_held"], higher_is_better=True)
    scored["score_top3_concentration"] = percentile_rank(scored["avg_top3_stock_weight"], higher_is_better=False)
    scored["score_fill_rate"] = percentile_rank(scored["risk_on_fill_rate"], higher_is_better=True)
    scored["score_honesty"] = percentile_rank(scored["deployment_honesty_score"], higher_is_better=True)
    scored["spec_normalization_score"] = (
        scored["score_oos_rel_qqq"] * 0.22
        + scored["score_oos_maxdd"] * 0.16
        + scored["score_fill_rate"] * 0.16
        + scored["score_honesty"] * 0.16
        + scored["score_turnover"] * 0.10
        + scored["score_2022"] * 0.08
        + scored["score_top3_concentration"] * 0.07
        + scored["score_avg_names"] * 0.05
    )
    return scored.sort_values(
        by=["spec_normalization_score", "oos_cagr_minus_qqq", "deployment_honesty_score", "oos_max_drawdown"],
        ascending=[False, False, False, False],
    ).reset_index(drop=True)


def build_recommendation(
    *,
    center_summary: pd.Series,
    cash_summary: pd.Series,
    full_summary: pd.Series,
    selected_summary: pd.Series,
    qqq_plus_oos: pd.Series,
) -> dict[str, object]:
    selected_label = str(selected_summary["strategy"])
    is_cash_branch = selected_label == "coherent_cash_buffer_branch"
    performance_ok = bool(
        float(selected_summary["oos_cagr_minus_qqq"]) > 0.10
        and float(selected_summary["oos_max_drawdown"]) >= -0.30
        and float(selected_summary["annual_turnover"]) <= 4.5
    )
    consistency_ok = bool(
        float(selected_summary["risk_on_fill_rate"]) >= 0.95
        and float(selected_summary["deployment_honesty_score"]) >= 0.95
    )
    occupancy_ok = bool(
        float(selected_summary["avg_names_held"]) >= 8.0
        and float(selected_summary["underfilled_month_share"]) <= 0.35
    )
    concentration_ok = bool(float(selected_summary["avg_top3_stock_weight"]) <= 0.55)
    center_is_cash_prototype = bool(
        float(center_summary["risk_on_fill_rate"]) <= 0.82
        and float(cash_summary["risk_on_fill_rate"]) >= 0.99
        and float(cash_summary["deployment_honesty_score"]) > float(center_summary["deployment_honesty_score"])
    )

    if not performance_ok:
        level = "discard"
        reason = "honest-spec candidates do not keep enough relative-to-QQQ edge"
    elif is_cash_branch:
        level = "cash_buffer_branch"
        reason = "the best honest interpretation is still an explicit cash-buffered tech pullback branch, not a standard full-risk offensive branch"
    elif consistency_ok and occupancy_ok and concentration_ok:
        level = "research_default"
        reason = "the selected honest spec is geometrically self-consistent and keeps strong enough performance"
    else:
        level = "research_default_candidate"
        reason = "the honest full-deployment branch still has edge, but occupancy / concentration / drawdown need more tightening"

    if float(selected_summary["oos_cagr"]) >= float(qqq_plus_oos["CAGR"]) and float(selected_summary["oos_max_drawdown"]) >= float(qqq_plus_oos["Max Drawdown"]):
        role = "替代者"
    elif float(selected_summary["oos_cagr_minus_qqq"]) > 0 and float(selected_summary["full_max_drawdown"]) >= -0.40:
        role = "并行分支"
    else:
        role = "次级实验"

    return {
        "center_strategy": str(center_summary["strategy"]),
        "selected_cash_branch": str(cash_summary["strategy"]),
        "selected_full_deployment_branch": str(full_summary["strategy"]),
        "selected_research_default": selected_label,
        "research_recommendation": level,
        "role_vs_qqq_plus_current_default": role,
        "reason": reason,
        "checks": {
            "center_is_cash_prototype": center_is_cash_prototype,
            "selected_oos_cagr_minus_qqq": float(selected_summary["oos_cagr_minus_qqq"]),
            "selected_oos_max_drawdown": float(selected_summary["oos_max_drawdown"]),
            "selected_annual_turnover": float(selected_summary["annual_turnover"]),
            "selected_risk_on_fill_rate": float(selected_summary["risk_on_fill_rate"]),
            "selected_deployment_honesty_score": float(selected_summary["deployment_honesty_score"]),
            "selected_avg_names_held": float(selected_summary["avg_names_held"]),
            "selected_underfilled_month_share": float(selected_summary["underfilled_month_share"]),
            "selected_avg_top3_stock_weight": float(selected_summary["avg_top3_stock_weight"]),
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
    candidate_summary: pd.DataFrame,
    deployment_summary: pd.DataFrame,
    comparison_5bps: pd.DataFrame,
    recommendation: dict[str, object],
) -> None:
    lines = [
        "# growth_pullback_systematic_v1.3 spec normalization",
        "",
        "## Honest spec candidates",
        format_table(candidate_summary[[
            "strategy", "target_names", "realized_names", "target_stock_weight", "realized_stock_weight", "risk_on_fill_rate", "deployment_honesty_score", "spec_normalization_score"
        ]]),
        "",
        "## Deployment consistency",
        format_table(deployment_summary[[
            "strategy", "avg_names_held", "risk_on_avg_names", "risk_on_target_stock_weight", "risk_on_realized_stock_weight", "risk_on_fill_rate", "underfilled_month_share", "avg_top1_stock_weight", "avg_top3_stock_weight", "avg_top5_stock_weight", "avg_safe_haven_weight"
        ]]),
        "",
        "## Main comparison (5 bps)",
        format_table(comparison_5bps[[
            "strategy", "period", "CAGR", "Max Drawdown", "Turnover/Year", "Average Names Held", "risk_on_realized_stock_weight", "beta_vs_qqq", "alpha_ann_vs_qqq", "Information Ratio vs QQQ", "Up Capture vs QQQ", "Down Capture vs QQQ"
        ]]),
        "",
        "## Recommendation",
        f"- research_recommendation={recommendation['research_recommendation']}",
        f"- selected_research_default={recommendation['selected_research_default']}",
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

    center_config_path, stable_config_path = resolve_config_paths(configs_dir)
    center_cfg = gp.load_spec_config(center_config_path)
    stable_cfg = gp.load_spec_config(stable_config_path)
    candidates = build_candidates(center_cfg, stable_cfg)

    alias_dir = robust.discover_run_dir(args.alias_data_run_dir, "r1000_multifactor_defensive_*_official_monthly_v2_alias")
    context = gp.build_context(alias_dir, start=args.start, end=args.end)
    benchmark_returns = context["stock_returns_matrix"]["QQQ"].copy()

    candidate_rows: list[dict[str, object]] = []
    deployment_summary_rows: list[dict[str, object]] = []
    for candidate in candidates:
        rows, artifacts_by_cost = v12.evaluate_candidate_rows(candidate, context, benchmark_returns, cost_bps_values=COST_LEVELS)
        candidate_rows.extend(rows)
        deployment_summary_rows.append(v12.summarize_deployment(v12.build_deployment_monthly(candidate, artifacts_by_cost[MAIN_COST_BPS], context)))

    candidate_df = pd.DataFrame(candidate_rows)
    deployment_summary_df = pd.DataFrame(deployment_summary_rows)

    qqq_reference_rows, reference_artifacts = gp.build_reference_rows(context, COST_LEVELS)
    qqq_oos = v12.extract_period_row(qqq_reference_rows, "qqq_plus_current_default", MAIN_COST_BPS, "OOS Sample")
    qqq_oos_cagr = float(v12.extract_period_row(qqq_reference_rows, "QQQ", MAIN_COST_BPS, "OOS Sample")["CAGR"])

    summary_rows = []
    for candidate in candidates:
        full_row = v12.extract_period_row(candidate_df, candidate.label, MAIN_COST_BPS, "Full Sample")
        oos_row = v12.extract_period_row(candidate_df, candidate.label, MAIN_COST_BPS, "OOS Sample")
        row_2022 = v12.extract_period_row(candidate_df, candidate.label, MAIN_COST_BPS, "2022")
        row_2023 = v12.extract_period_row(candidate_df, candidate.label, MAIN_COST_BPS, "2023+")
        dep = deployment_summary_df.loc[deployment_summary_df["strategy"] == candidate.label].iloc[0]
        summary_rows.append(
            {
                "strategy": candidate.label,
                "config_name": candidate.config.name,
                "full_cagr": float(full_row["CAGR"]),
                "full_max_drawdown": float(full_row["Max Drawdown"]),
                "oos_cagr": float(oos_row["CAGR"]),
                "oos_cagr_minus_qqq": float(oos_row["CAGR"] - qqq_oos_cagr),
                "oos_max_drawdown": float(oos_row["Max Drawdown"]),
                "return_2022": float(row_2022["Total Return"]),
                "cagr_2023_plus": float(row_2023["CAGR"]),
                "annual_turnover": float(full_row["Turnover/Year"]),
                **dep.to_dict(),
                **spec_consistency_metrics(candidate, dep),
            }
        )
    candidate_summary_df = add_normalization_scores(pd.DataFrame(summary_rows))

    center_summary = candidate_summary_df.loc[candidate_summary_df["strategy"] == "center_current"].iloc[0]
    cash_summary = candidate_summary_df.loc[candidate_summary_df["strategy"] == "coherent_cash_buffer_branch"].iloc[0]
    full_pool = candidate_summary_df.loc[candidate_summary_df["strategy"].isin(["coherent_full_deployment_branch", "coherent_full_deployment_branch_single8"])]
    full_summary = full_pool.iloc[0]

    honest_pool = candidate_summary_df.loc[candidate_summary_df["strategy"].isin([
        "coherent_cash_buffer_branch",
        "coherent_full_deployment_branch",
        "coherent_full_deployment_branch_single8",
    ])].copy()
    selected_summary = honest_pool.iloc[0]

    recommendation = build_recommendation(
        center_summary=center_summary,
        cash_summary=cash_summary,
        full_summary=full_summary,
        selected_summary=selected_summary,
        qqq_plus_oos=qqq_oos,
    )

    reference_deployment_rows = [
        v12.summarize_reference_deployment("qqq_plus_current_default", reference_artifacts["qqq_plus_current_default"][MAIN_COST_BPS]),
        v12.summarize_reference_deployment("aggressive_alt_spec", reference_artifacts["aggressive_alt_spec"][MAIN_COST_BPS]),
        v12.summarize_reference_deployment("defensive_baseline", reference_artifacts["defensive_baseline"][MAIN_COST_BPS]),
        {"strategy": "QQQ", "risk_on_realized_stock_weight": 1.0, "avg_names_held": 1.0},
    ]
    comparison_df = pd.concat(
        [candidate_df.loc[candidate_df["cost_bps_one_way"] == MAIN_COST_BPS].copy(), qqq_reference_rows.loc[qqq_reference_rows["cost_bps_one_way"] == MAIN_COST_BPS].copy()],
        ignore_index=True,
    )
    comparison_df = comparison_df.merge(
        pd.concat([deployment_summary_df[["strategy", "risk_on_realized_stock_weight", "avg_names_held"]], pd.DataFrame(reference_deployment_rows)], ignore_index=True),
        on="strategy",
        how="left",
    )

    candidate_summary_df.to_csv(results_dir / "growth_pullback_v1_3_spec_normalization.csv", index=False)
    deployment_summary_df.to_csv(results_dir / "growth_pullback_v1_3_deployment_consistency.csv", index=False)
    (results_dir / "growth_pullback_v1_3_recommendation.json").write_text(json.dumps(recommendation, indent=2, ensure_ascii=False), encoding="utf-8")
    write_markdown_report(
        results_dir / "growth_pullback_v1_3_spec_normalization.md",
        candidate_summary=candidate_summary_df,
        deployment_summary=deployment_summary_df,
        comparison_5bps=comparison_df,
        recommendation=recommendation,
    )

    print(f"alias data: {alias_dir}")
    print(f"selected research default: {recommendation['selected_research_default']}")
    print(f"recommendation: {recommendation['research_recommendation']}")
    print(f"results written to: {results_dir}")


if __name__ == "__main__":
    main()
