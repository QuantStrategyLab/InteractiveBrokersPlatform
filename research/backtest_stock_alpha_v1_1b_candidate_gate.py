#!/usr/bin/env python3
"""
V1.1b candidate-centric gate review for qqq_plus_stock_alpha_v1.

Focus only on the current default_frozen_spec:
- freeze the center spec
- build first-order neighbors (one dimension, one step only)
- review local plateau around the center
- run monthly jackknife / block holdout on the center OOS sample
- keep the original global gate unchanged and add a candidate-centric recommendation layer
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import replace
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import backtest_stock_alpha_suite as suite  # noqa: E402
import backtest_stock_alpha_v1_1_spec_lock as v11  # noqa: E402
import backtest_stock_alpha_v1_robustness as robust  # noqa: E402


DEFAULT_RESULTS_DIR = SCRIPT_DIR / "results"
DEFAULT_CONFIGS_DIR = SCRIPT_DIR / "configs"
MAIN_COST_BPS = 5.0
SUPPLEMENTAL_COST_BPS = 0.0
COST_LEVELS = (MAIN_COST_BPS, SUPPLEMENTAL_COST_BPS)
OOS_START = "2022-01-01"
COMPARISON_PERIODS = (
    ("Full Sample", None, None),
    ("OOS Sample", OOS_START, None),
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023+", "2023-01-01", None),
)
LEGAL_STEP_VALUES = {
    "holdings_count": (12, 16),
    "single_name_cap": (0.08, 0.09, 0.10),
    "sector_cap": (0.25, 0.30, 0.35),
    "hold_bonus": (0.05, 0.10, 0.15),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--alias-data-run-dir",
        help="Prepared Russell data run with alias repair (defaults to newest official_monthly_v2_alias run)",
    )
    parser.add_argument(
        "--results-dir",
        default=str(DEFAULT_RESULTS_DIR),
        help="Directory where V1.1b outputs will be written",
    )
    parser.add_argument(
        "--configs-dir",
        default=str(DEFAULT_CONFIGS_DIR),
        help="Directory where V1.1 default/aggressive configs are stored",
    )
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    return parser.parse_args()


def build_context(alias_run_dir: Path, *, start: str | None, end: str | None) -> dict[str, object]:
    _universe, _prices, prepared_start, prepared_end = suite.discover_prepared_data(alias_run_dir)
    effective_start = pd.Timestamp(start or prepared_start).normalize()
    effective_end = pd.Timestamp(end or prepared_end).normalize()
    etf_frames = suite.download_etf_ohlcv(
        ("QQQ", "SPY", "XLK", "SMH"),
        start=str(effective_start.date()),
        end=str((effective_end + pd.Timedelta(days=1)).date()),
    )
    return robust.prepare_context(
        alias_run_dir,
        etf_frames=etf_frames,
        start_date=effective_start,
        end_date=effective_end,
    )


def build_first_order_neighbors(center: suite.OffensiveConfig) -> list[tuple[dict[str, object], suite.OffensiveConfig]]:
    neighbors: list[tuple[dict[str, object], suite.OffensiveConfig]] = []
    for field_name, values in LEGAL_STEP_VALUES.items():
        current_value = getattr(center, field_name)
        value_list = list(values)
        try:
            index = next(i for i, value in enumerate(value_list) if math.isclose(float(value), float(current_value), rel_tol=0.0, abs_tol=1e-12))
        except StopIteration:
            if current_value not in value_list:
                continue
            index = value_list.index(current_value)

        for direction, offset in (("down", -1), ("up", 1)):
            new_index = index + offset
            if new_index < 0 or new_index >= len(value_list):
                continue
            new_value = value_list[new_index]
            neighbor = replace(
                center,
                name=f"{center.name}__{field_name}_{direction}_{str(new_value).replace('.', 'p')}",
                **{field_name: new_value},
            )
            neighbors.append(
                (
                    {
                        "scenario_group": "candidate_neighbor",
                        "change_dimension": field_name,
                        "change_direction": direction,
                        "from_value": current_value,
                        "to_value": new_value,
                    },
                    neighbor,
                )
            )
    return neighbors


def evaluate_strategy_rows(
    *,
    strategy_label: str,
    config: suite.OffensiveConfig,
    context: dict[str, object],
    benchmark_returns: pd.Series,
    scenario_group: str,
    cost_levels: Iterable[float],
    meta: dict[str, object],
) -> tuple[list[dict[str, object]], dict[float, dict[str, object]]]:
    rows, artifacts_by_cost = v11.evaluate_final_strategy_rows(
        strategy_label,
        config,
        context,
        benchmark_returns,
        cost_bps_values=cost_levels,
    )
    for row in rows:
        row.update(meta)
    return rows, artifacts_by_cost


def compute_basic_return_metrics(strategy_returns: pd.Series, benchmark_returns: pd.Series) -> dict[str, float]:
    aligned = pd.concat(
        [strategy_returns.rename("strategy"), benchmark_returns.rename("benchmark")],
        axis=1,
    ).dropna()
    if aligned.empty:
        raise RuntimeError("No aligned returns remain after holdout removal")

    equity = (1.0 + aligned["strategy"]).cumprod()
    benchmark_equity = (1.0 + aligned["benchmark"]).cumprod()
    years = max((aligned.index[-1] - aligned.index[0]).days / 365.25, 1 / 365.25)
    relative = robust.compute_relative_stats(aligned["strategy"], aligned["benchmark"])

    return {
        "Start": str(aligned.index[0].date()),
        "End": str(aligned.index[-1].date()),
        "Total Return": float(equity.iloc[-1] - 1.0),
        "CAGR": float(equity.iloc[-1] ** (1.0 / years) - 1.0),
        "QQQ CAGR": float(benchmark_equity.iloc[-1] ** (1.0 / years) - 1.0),
        "Max Drawdown": float((equity / equity.cummax() - 1.0).min()),
        "alpha_ann_vs_qqq": float(relative["alpha_ann_vs_qqq"]),
        "information_ratio_vs_qqq": float(relative["information_ratio_vs_qqq"]),
    }


def build_monthly_jackknife(
    *,
    strategy_returns: pd.Series,
    benchmark_returns: pd.Series,
    cost_bps: float,
) -> tuple[pd.DataFrame, dict[str, float]]:
    strategy_oos = robust.slice_series_or_frame(strategy_returns, OOS_START, None).dropna()
    benchmark_oos = robust.slice_series_or_frame(benchmark_returns, OOS_START, None).reindex(strategy_oos.index).fillna(0.0)
    center_metrics = compute_basic_return_metrics(strategy_oos, benchmark_oos)
    months = sorted(strategy_oos.index.to_period("M").unique())
    rows = []
    for month in months:
        keep_mask = strategy_oos.index.to_period("M") != month
        kept_strategy = strategy_oos.loc[keep_mask]
        kept_benchmark = benchmark_oos.loc[keep_mask]
        metrics = compute_basic_return_metrics(kept_strategy, kept_benchmark)
        rows.append(
            {
                "holdout_type": "monthly_jackknife",
                "cost_bps_one_way": float(cost_bps),
                "removed_label": str(month),
                "removed_start": str(month.start_time.date()),
                "removed_end": str(month.end_time.date()),
                "removed_month_count": 1,
                "removed_trading_days": int((~keep_mask).sum()),
                **metrics,
                "delta_oos_cagr_vs_center": float(metrics["CAGR"] - center_metrics["CAGR"]),
                "delta_oos_alpha_ann_vs_center": float(metrics["alpha_ann_vs_qqq"] - center_metrics["alpha_ann_vs_qqq"]),
                "delta_oos_maxdd_vs_center": float(metrics["Max Drawdown"] - center_metrics["Max Drawdown"]),
                "alpha_positive_after_removal": bool(metrics["alpha_ann_vs_qqq"] > 0),
            }
        )
    frame = pd.DataFrame(rows).sort_values("removed_label").reset_index(drop=True)
    summary = {
        "center_oos_cagr": float(center_metrics["CAGR"]),
        "center_oos_alpha_ann_vs_qqq": float(center_metrics["alpha_ann_vs_qqq"]),
        "center_oos_maxdd": float(center_metrics["Max Drawdown"]),
        "positive_alpha_share": float(frame["alpha_positive_after_removal"].mean()) if not frame.empty else float("nan"),
    }
    return frame, summary


def build_block_holdout(
    *,
    strategy_returns: pd.Series,
    benchmark_returns: pd.Series,
    cost_bps: float,
    window_months: int,
) -> tuple[pd.DataFrame, dict[str, float]]:
    strategy_oos = robust.slice_series_or_frame(strategy_returns, OOS_START, None).dropna()
    benchmark_oos = robust.slice_series_or_frame(benchmark_returns, OOS_START, None).reindex(strategy_oos.index).fillna(0.0)
    center_metrics = compute_basic_return_metrics(strategy_oos, benchmark_oos)
    months = sorted(strategy_oos.index.to_period("M").unique())
    rows = []
    for start_idx in range(0, len(months) - window_months + 1):
        removed_months = months[start_idx : start_idx + window_months]
        keep_mask = ~strategy_oos.index.to_period("M").isin(removed_months)
        kept_strategy = strategy_oos.loc[keep_mask]
        kept_benchmark = benchmark_oos.loc[keep_mask]
        metrics = compute_basic_return_metrics(kept_strategy, kept_benchmark)
        rows.append(
            {
                "holdout_type": "block_holdout",
                "window_months": int(window_months),
                "cost_bps_one_way": float(cost_bps),
                "removed_label": f"{removed_months[0]}->{removed_months[-1]}",
                "removed_start": str(removed_months[0].start_time.date()),
                "removed_end": str(removed_months[-1].end_time.date()),
                "removed_month_count": int(window_months),
                "removed_trading_days": int((~keep_mask).sum()),
                **metrics,
                "delta_oos_cagr_vs_center": float(metrics["CAGR"] - center_metrics["CAGR"]),
                "delta_oos_alpha_ann_vs_center": float(metrics["alpha_ann_vs_qqq"] - center_metrics["alpha_ann_vs_qqq"]),
                "delta_oos_maxdd_vs_center": float(metrics["Max Drawdown"] - center_metrics["Max Drawdown"]),
                "alpha_positive_after_removal": bool(metrics["alpha_ann_vs_qqq"] > 0),
            }
        )
    frame = pd.DataFrame(rows).sort_values(["window_months", "removed_start"]).reset_index(drop=True)
    summary = {
        "window_months": int(window_months),
        "center_oos_cagr": float(center_metrics["CAGR"]),
        "center_oos_alpha_ann_vs_qqq": float(center_metrics["alpha_ann_vs_qqq"]),
        "positive_alpha_share": float(frame["alpha_positive_after_removal"].mean()) if not frame.empty else float("nan"),
    }
    return frame, summary


def build_candidate_plateau(neighbor_oos_rows: pd.DataFrame, center_row: pd.Series) -> tuple[pd.DataFrame, dict[str, float]]:
    frame = neighbor_oos_rows.copy()
    frame["delta_oos_cagr_vs_center"] = frame["CAGR"] - float(center_row["CAGR"])
    frame["delta_oos_alpha_ann_vs_center"] = frame["alpha_ann_vs_qqq"] - float(center_row["alpha_ann_vs_qqq"])
    frame["delta_oos_maxdd_vs_center"] = frame["Max Drawdown"] - float(center_row["Max Drawdown"])
    frame["abs_delta_oos_cagr_vs_center"] = frame["delta_oos_cagr_vs_center"].abs()

    neighbor_count = len(frame)
    summary = {
        "neighbor_count": int(neighbor_count),
        "neighbor_plateau_50bps_count": int((frame["abs_delta_oos_cagr_vs_center"] <= 0.005).sum()),
        "neighbor_plateau_100bps_count": int((frame["abs_delta_oos_cagr_vs_center"] <= 0.01).sum()),
        "neighbor_plateau_200bps_count": int((frame["abs_delta_oos_cagr_vs_center"] <= 0.02).sum()),
    }
    summary.update(
        {
            "neighbor_plateau_50bps_share": float(summary["neighbor_plateau_50bps_count"] / neighbor_count) if neighbor_count else float("nan"),
            "neighbor_plateau_100bps_share": float(summary["neighbor_plateau_100bps_count"] / neighbor_count) if neighbor_count else float("nan"),
            "neighbor_plateau_200bps_share": float(summary["neighbor_plateau_200bps_count"] / neighbor_count) if neighbor_count else float("nan"),
        }
    )
    return frame, summary


def build_candidate_recommendation(
    *,
    original_global_gate: dict[str, object],
    center_row: pd.Series,
    local_plateau_summary: dict[str, float],
    monthly_summary_5bps: dict[str, float],
    block6_summary_5bps: dict[str, float],
    block12_summary_5bps: dict[str, float],
) -> dict[str, object]:
    candidate_ok = bool(
        float(center_row["alpha_ann_vs_qqq"]) > 0.05
        and float(center_row["Max Drawdown"]) >= -0.35
        and float(local_plateau_summary["neighbor_plateau_100bps_share"]) >= 0.50
        and float(local_plateau_summary["neighbor_plateau_200bps_share"]) >= 0.67
        and float(monthly_summary_5bps["positive_alpha_share"]) >= 0.90
        and float(block6_summary_5bps["positive_alpha_share"]) >= 0.80
        and float(block12_summary_5bps["positive_alpha_share"]) >= 0.70
    )
    if candidate_ok:
        level = "shadow_candidate"
        reason = "center spec passes local-neighborhood and holdout review, though the original global gate still fails"
    else:
        level = "not_ready"
        reason = "center spec still fails at least one local-neighborhood or holdout stability check"

    return {
        "original_global_gate": original_global_gate,
        "candidate_centric_recommendation": level,
        "reason": reason,
        "checks": {
            "oos_alpha_ann_vs_qqq_5bps": float(center_row["alpha_ann_vs_qqq"]),
            "oos_max_drawdown_5bps": float(center_row["Max Drawdown"]),
            "neighbor_plateau_100bps_share": float(local_plateau_summary["neighbor_plateau_100bps_share"]),
            "neighbor_plateau_200bps_share": float(local_plateau_summary["neighbor_plateau_200bps_share"]),
            "monthly_jackknife_positive_alpha_share": float(monthly_summary_5bps["positive_alpha_share"]),
            "block_6m_positive_alpha_share": float(block6_summary_5bps["positive_alpha_share"]),
            "block_12m_positive_alpha_share": float(block12_summary_5bps["positive_alpha_share"]),
        },
        "thresholds": {
            "oos_alpha_ann_vs_qqq_min": 0.05,
            "oos_max_drawdown_floor": -0.35,
            "neighbor_plateau_100bps_share_min": 0.50,
            "neighbor_plateau_200bps_share_min": 0.67,
            "monthly_jackknife_positive_alpha_share_min": 0.90,
            "block_6m_positive_alpha_share_min": 0.80,
            "block_12m_positive_alpha_share_min": 0.70,
        },
    }


def format_table(frame: pd.DataFrame) -> str:
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
        lines.append("| " + " | ".join(fmt(v) for v in row) + " |")
    return "\n".join(lines)


def write_markdown_report(
    path: Path,
    *,
    center_config: suite.OffensiveConfig,
    center_row_5bps: pd.Series,
    neighbor_rows_5bps: pd.DataFrame,
    local_plateau_summary: dict[str, float],
    global_gate: dict[str, object],
    monthly_holdout: pd.DataFrame,
    block_holdout: pd.DataFrame,
    recommendation: dict[str, object],
    comparison_rows_5bps: pd.DataFrame,
) -> None:
    worst_months = monthly_holdout.sort_values("alpha_ann_vs_qqq").head(5)[[
        "removed_label",
        "CAGR",
        "alpha_ann_vs_qqq",
        "Max Drawdown",
        "delta_oos_alpha_ann_vs_center",
    ]]
    best_months = monthly_holdout.sort_values("alpha_ann_vs_qqq", ascending=False).head(5)[[
        "removed_label",
        "CAGR",
        "alpha_ann_vs_qqq",
        "Max Drawdown",
        "delta_oos_alpha_ann_vs_center",
    ]]
    block6 = block_holdout.loc[block_holdout["window_months"] == 6].copy()
    block12 = block_holdout.loc[block_holdout["window_months"] == 12].copy()
    worst_block6 = block6.sort_values("alpha_ann_vs_qqq").head(5)[[
        "removed_label",
        "CAGR",
        "alpha_ann_vs_qqq",
        "delta_oos_alpha_ann_vs_center",
    ]]
    best_block6 = block6.sort_values("alpha_ann_vs_qqq", ascending=False).head(5)[[
        "removed_label",
        "CAGR",
        "alpha_ann_vs_qqq",
        "delta_oos_alpha_ann_vs_center",
    ]]
    worst_block12 = block12.sort_values("alpha_ann_vs_qqq").head(5)[[
        "removed_label",
        "CAGR",
        "alpha_ann_vs_qqq",
        "delta_oos_alpha_ann_vs_center",
    ]]
    best_block12 = block12.sort_values("alpha_ann_vs_qqq", ascending=False).head(5)[[
        "removed_label",
        "CAGR",
        "alpha_ann_vs_qqq",
        "delta_oos_alpha_ann_vs_center",
    ]]

    lines = [
        "# qqq_plus_stock_alpha_v1.1b candidate-centric gate review",
        "",
        "## Frozen center spec",
        f"- universe={center_config.universe_filter.name}",
        f"- normalization={v11.normalization_label(center_config.group_normalization)}",
        f"- regime={center_config.regime.name}",
        f"- holdings={center_config.holdings_count}",
        f"- single_cap={center_config.single_name_cap:.0%}",
        f"- sector_cap={center_config.sector_cap:.0%}",
        f"- hold_bonus={center_config.hold_bonus:.2f}",
        f"- exposures=100/{int(center_config.exposures.soft_defense_exposure * 100)}/{int(center_config.exposures.hard_defense_exposure * 100)}",
        "- residual proxy=simple excess return vs QQQ",
        f"- cost assumption(main)={MAIN_COST_BPS:.0f} bps one-way",
        "",
        "## One-step neighborhood (5 bps, OOS)",
        format_table(neighbor_rows_5bps[[
            "strategy",
            "change_dimension",
            "change_direction",
            "from_value",
            "to_value",
            "CAGR",
            "alpha_ann_vs_qqq",
            "Max Drawdown",
            "delta_oos_cagr_vs_center",
            "delta_oos_alpha_ann_vs_center",
            "delta_oos_maxdd_vs_center",
        ]]),
        "",
        "## Candidate-centric plateau",
        f"- original_global_plateau_200bps_share={float(global_gate.get('plateau_200bps_share', float('nan'))):.1%}",
        f"- neighbor_count={int(local_plateau_summary['neighbor_count'])}",
        f"- neighbor_plateau_50bps_share={local_plateau_summary['neighbor_plateau_50bps_share']:.1%}",
        f"- neighbor_plateau_100bps_share={local_plateau_summary['neighbor_plateau_100bps_share']:.1%}",
        f"- neighbor_plateau_200bps_share={local_plateau_summary['neighbor_plateau_200bps_share']:.1%}",
        "",
        "## Monthly jackknife (5 bps)",
        f"- positive alpha share after removing any single month={monthly_holdout['alpha_positive_after_removal'].mean():.1%}",
        "### Worst 5 removed months (lowest post-removal alpha)",
        format_table(worst_months),
        "",
        "### Best 5 removed months (highest post-removal alpha)",
        format_table(best_months),
        "",
        "## Continuous block holdout (5 bps)",
        f"- 6m block positive alpha share={block6['alpha_positive_after_removal'].mean():.1%}",
        f"- 12m block positive alpha share={block12['alpha_positive_after_removal'].mean():.1%}",
        "### Worst 5 removed 6m blocks",
        format_table(worst_block6),
        "",
        "### Best 5 removed 6m blocks",
        format_table(best_block6),
        "",
        "### Worst 5 removed 12m blocks",
        format_table(worst_block12),
        "",
        "### Best 5 removed 12m blocks",
        format_table(best_block12),
        "",
        "## Main comparison (5 bps)",
        format_table(comparison_rows_5bps[[
            "strategy",
            "period",
            "CAGR",
            "Total Return",
            "Max Drawdown",
            "Sharpe",
            "alpha_ann_vs_qqq",
            "annual_turnover",
        ]]),
        "",
        "## Recommendation layers",
        f"- original_global_gate={global_gate.get('recommendation')}",
        f"- candidate_centric_recommendation={recommendation['candidate_centric_recommendation']}",
        f"- reason={recommendation['reason']}",
        "",
        "## Interpretation",
        "- 这里只复核 default_frozen_spec 自己附近是否稳，不再重新做更大网格搜参。",
        "- active share vs QQQ 仍显式留空，因为没有 QQQ 历史 constituent weights。",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = parse_args()
    results_dir = Path(args.results_dir).expanduser().resolve()
    configs_dir = Path(args.configs_dir).expanduser().resolve()
    results_dir.mkdir(parents=True, exist_ok=True)

    alias_dir = robust.discover_run_dir(args.alias_data_run_dir, "r1000_multifactor_defensive_*_official_monthly_v2_alias")
    context = build_context(alias_dir, start=args.start, end=args.end)
    benchmark_returns = context["stock_returns_matrix"]["QQQ"].copy()

    default_config = v11.load_spec_config(configs_dir / "qqq_plus_stock_alpha_v1_1_default.json")
    aggressive_config = v11.load_spec_config(configs_dir / "qqq_plus_stock_alpha_v1_1_aggressive.json")

    original_global_gate_payload = json.loads((results_dir / "stock_alpha_v1_1_recommendation.json").read_text(encoding="utf-8"))
    original_global_gate = dict(original_global_gate_payload.get("original_gate_v1_1_local_grid", {}))

    center_rows, center_artifacts = evaluate_strategy_rows(
        strategy_label="default_frozen_spec",
        config=default_config,
        context=context,
        benchmark_returns=benchmark_returns,
        scenario_group="candidate_center",
        cost_levels=COST_LEVELS,
        meta={
            "change_dimension": "center",
            "change_direction": "center",
            "from_value": np.nan,
            "to_value": np.nan,
            "is_center": True,
            "is_neighbor": False,
        },
    )

    neighbor_rows: list[dict[str, object]] = []
    neighbor_artifacts: dict[str, dict[float, dict[str, object]]] = {}
    for meta, neighbor_config in build_first_order_neighbors(default_config):
        rows, artifacts = evaluate_strategy_rows(
            strategy_label=neighbor_config.name,
            config=neighbor_config,
            context=context,
            benchmark_returns=benchmark_returns,
            scenario_group="candidate_neighbor",
            cost_levels=COST_LEVELS,
            meta={**meta, "is_center": False, "is_neighbor": True},
        )
        neighbor_rows.extend(rows)
        neighbor_artifacts[neighbor_config.name] = artifacts

    aggressive_rows, aggressive_artifacts = evaluate_strategy_rows(
        strategy_label="aggressive_alt_spec",
        config=aggressive_config,
        context=context,
        benchmark_returns=benchmark_returns,
        scenario_group="reference_strategy",
        cost_levels=COST_LEVELS,
        meta={
            "change_dimension": "reference",
            "change_direction": "reference",
            "from_value": np.nan,
            "to_value": np.nan,
            "is_center": False,
            "is_neighbor": False,
        },
    )
    defensive_rows, defensive_artifacts = v11.evaluate_defensive_rows(context, cost_bps_values=COST_LEVELS)
    for row in defensive_rows:
        row.update(
            {
                "change_dimension": "reference",
                "change_direction": "reference",
                "from_value": np.nan,
                "to_value": np.nan,
                "is_center": False,
                "is_neighbor": False,
            }
        )
    qqq_rows = v11.evaluate_qqq_rows(benchmark_returns, cost_bps_values=COST_LEVELS)
    for row in qqq_rows:
        row.update(
            {
                "change_dimension": "reference",
                "change_direction": "reference",
                "from_value": np.nan,
                "to_value": np.nan,
                "is_center": False,
                "is_neighbor": False,
            }
        )

    candidate_gate_df = pd.DataFrame(center_rows + neighbor_rows + aggressive_rows + defensive_rows + qqq_rows)

    center_row_5bps = candidate_gate_df.loc[
        (candidate_gate_df["strategy"] == "default_frozen_spec")
        & (candidate_gate_df["cost_bps_one_way"] == MAIN_COST_BPS)
        & (candidate_gate_df["period"] == "OOS Sample")
    ].iloc[0]
    neighbor_oos_rows_5bps = candidate_gate_df.loc[
        (candidate_gate_df["is_neighbor"])
        & (candidate_gate_df["cost_bps_one_way"] == MAIN_COST_BPS)
        & (candidate_gate_df["period"] == "OOS Sample")
    ].copy()
    neighbor_oos_rows_5bps, local_plateau_summary = build_candidate_plateau(neighbor_oos_rows_5bps, center_row_5bps)

    candidate_gate_df = candidate_gate_df.merge(
        neighbor_oos_rows_5bps[[
            "strategy",
            "delta_oos_cagr_vs_center",
            "delta_oos_alpha_ann_vs_center",
            "delta_oos_maxdd_vs_center",
            "abs_delta_oos_cagr_vs_center",
        ]],
        on="strategy",
        how="left",
    )

    monthly_frames = []
    monthly_summaries = {}
    block_frames = []
    block_summaries = {}
    for cost_bps in COST_LEVELS:
        strategy_returns = center_artifacts[float(cost_bps)]["net_returns"]
        monthly_frame, monthly_summary = build_monthly_jackknife(
            strategy_returns=strategy_returns,
            benchmark_returns=benchmark_returns,
            cost_bps=float(cost_bps),
        )
        monthly_frames.append(monthly_frame)
        monthly_summaries[float(cost_bps)] = monthly_summary
        for window_months in (6, 12):
            block_frame, block_summary = build_block_holdout(
                strategy_returns=strategy_returns,
                benchmark_returns=benchmark_returns,
                cost_bps=float(cost_bps),
                window_months=window_months,
            )
            block_frames.append(block_frame)
            block_summaries[(float(cost_bps), int(window_months))] = block_summary

    monthly_holdout_df = pd.concat(monthly_frames, ignore_index=True)
    block_holdout_df = pd.concat(block_frames, ignore_index=True)

    recommendation = build_candidate_recommendation(
        original_global_gate=original_global_gate,
        center_row=center_row_5bps,
        local_plateau_summary=local_plateau_summary,
        monthly_summary_5bps=monthly_summaries[MAIN_COST_BPS],
        block6_summary_5bps=block_summaries[(MAIN_COST_BPS, 6)],
        block12_summary_5bps=block_summaries[(MAIN_COST_BPS, 12)],
    )

    candidate_gate_df.to_csv(results_dir / "stock_alpha_v1_1b_candidate_gate.csv", index=False)
    monthly_holdout_df.to_csv(results_dir / "stock_alpha_v1_1b_monthly_holdout.csv", index=False)
    block_holdout_df.to_csv(results_dir / "stock_alpha_v1_1b_block_holdout.csv", index=False)
    (results_dir / "stock_alpha_v1_1b_recommendation.json").write_text(
        json.dumps(recommendation, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    comparison_rows_5bps = candidate_gate_df.loc[
        (candidate_gate_df["cost_bps_one_way"] == MAIN_COST_BPS)
        & (candidate_gate_df["period"].isin(["Full Sample", "OOS Sample", "2022", "2023+"]))
        & (candidate_gate_df["strategy"].isin(["default_frozen_spec", "aggressive_alt_spec", "defensive_baseline", "QQQ"])),
        ["strategy", "period", "CAGR", "Total Return", "Max Drawdown", "Sharpe", "alpha_ann_vs_qqq", "annual_turnover"],
    ].copy()

    write_markdown_report(
        results_dir / "stock_alpha_v1_1b_candidate_gate.md",
        center_config=default_config,
        center_row_5bps=center_row_5bps,
        neighbor_rows_5bps=neighbor_oos_rows_5bps,
        local_plateau_summary=local_plateau_summary,
        global_gate=original_global_gate,
        monthly_holdout=monthly_holdout_df.loc[monthly_holdout_df["cost_bps_one_way"] == MAIN_COST_BPS].copy(),
        block_holdout=block_holdout_df.loc[block_holdout_df["cost_bps_one_way"] == MAIN_COST_BPS].copy(),
        recommendation=recommendation,
        comparison_rows_5bps=comparison_rows_5bps,
    )

    print(f"alias data: {alias_dir}")
    print(f"default center: {default_config.name}")
    print(f"neighbor count: {int(local_plateau_summary['neighbor_count'])}")
    print(f"candidate-centric recommendation: {recommendation['candidate_centric_recommendation']}")
    print(f"results written to: {results_dir}")


if __name__ == "__main__":
    main()
