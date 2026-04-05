#!/usr/bin/env python3
"""Research-only daily overlay backtest for cash_buffer_branch_default."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import backtest_growth_pullback_suite as gp  # noqa: E402
import backtest_growth_pullback_v1_2_geometry_repair as v12  # noqa: E402
import backtest_stock_alpha_suite as suite  # noqa: E402
import backtest_stock_alpha_v1_robustness as robust  # noqa: E402


DEFAULT_RESULTS_DIR = SCRIPT_DIR / "results"
DEFAULT_CONFIGS_DIR = SCRIPT_DIR / "configs"
DEFAULT_BASELINE_CONFIG_PATH = DEFAULT_CONFIGS_DIR / "growth_pullback_cash_buffer_branch_default.json"
DEFAULT_QQQ_PLUS_RESULTS_PATH = DEFAULT_RESULTS_DIR / "stock_alpha_v1_1_spec_lock.csv"
QQQ_PLUS_CURRENT_DEFAULT_SCENARIO = "v11_liquid_50m_norm_universe_cross_sectional_h12_cap8_sector30_hold10"
MAIN_COST_BPS = 5.0
COST_LEVELS = (0.0, MAIN_COST_BPS, 10.0)
OOS_START = "2022-01-01"
COMPARISON_PERIODS = (
    ("Full Sample", None, None),
    ("OOS Sample", OOS_START, None),
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023+", "2023-01-01", None),
)
SAFE_HAVEN = suite.SAFE_HAVEN


@dataclass(frozen=True)
class BaselineSpec:
    config: gp.GrowthPullbackConfig
    risk_on_exposure: float
    name: str
    config_payload: dict[str, object]


@dataclass(frozen=True)
class PortfolioThrottleOverlayConfig:
    name: str
    levels: tuple[float, ...]
    signal_family: str
    recovery_days: int = 2


@dataclass(frozen=True)
class NameTrimOverlayConfig:
    name: str
    trigger_family: str
    trim_multiplier: float
    confirm_days: int = 2


@dataclass(frozen=True)
class OverlayStrategyConfig:
    name: str
    family: str
    portfolio_overlay: PortfolioThrottleOverlayConfig | None = None
    name_overlay: NameTrimOverlayConfig | None = None
    note: str = ""


@dataclass
class OverlayArtifacts:
    gross_returns: pd.Series
    weights_history: pd.DataFrame
    turnover_history: pd.Series
    selection_history: pd.DataFrame
    overlay_history: pd.DataFrame
    rolling_alpha: pd.Series
    trigger_stats: dict[str, float]


@dataclass
class NameTrimState:
    multiplier: float = 1.0
    below_count: int = 0
    above_count: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alias-data-run-dir", help="Prepared alias-fixed Russell data run dir")
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR))
    parser.add_argument("--configs-dir", default=str(DEFAULT_CONFIGS_DIR))
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    return parser.parse_args()


def load_baseline_spec(path: Path) -> BaselineSpec:
    payload = json.loads(path.read_text(encoding="utf-8"))
    config = gp.load_spec_config(path)
    return BaselineSpec(
        config=config,
        risk_on_exposure=float(payload["exposures"]["risk_on"]),
        name="cash_buffer_branch_default_monthly_baseline",
        config_payload=payload,
    )


def build_baseline_candidate(spec: BaselineSpec) -> v12.GeometryCandidate:
    return v12.make_candidate(
        spec.name,
        spec.config,
        risk_on_exposure=spec.risk_on_exposure,
        note="Monthly baseline from canonical cash_buffer_branch_default; month-start chooses names, non-rebalance days are no-op.",
    )


def build_portfolio_overlay_candidates() -> list[OverlayStrategyConfig]:
    return [
        OverlayStrategyConfig(
            name="daily_portfolio_throttle_overlay__held_breadth_5state",
            family="daily_portfolio_throttle_overlay",
            portfolio_overlay=PortfolioThrottleOverlayConfig(
                name="held_breadth_5state",
                levels=(0.80, 0.60, 0.40, 0.20, 0.00),
                signal_family="qqq_dma_plus_held_names_breadth",
                recovery_days=2,
            ),
            note="Use QQQ 20/50DMA plus held-name breadth deterioration; no stock switching mid-month.",
        ),
        OverlayStrategyConfig(
            name="daily_portfolio_throttle_overlay__qqq_dma_mom_6state",
            family="daily_portfolio_throttle_overlay",
            portfolio_overlay=PortfolioThrottleOverlayConfig(
                name="qqq_dma_mom_6state",
                levels=(0.80, 0.70, 0.60, 0.40, 0.20, 0.00),
                signal_family="qqq_dma_plus_short_term_momentum",
                recovery_days=2,
            ),
            note="Use only QQQ short-term trend / momentum to throttle total stock exposure.",
        ),
    ]


def build_name_overlay_candidates() -> list[OverlayStrategyConfig]:
    return [
        OverlayStrategyConfig(
            name="daily_name_level_trim_overlay__20dma_half_confirm2",
            family="daily_name_level_trim_overlay",
            name_overlay=NameTrimOverlayConfig(
                name="20dma_half_confirm2",
                trigger_family="close_below_20dma",
                trim_multiplier=0.50,
                confirm_days=2,
            ),
            note="Trim selected names by 50% after two closes below 20DMA; restore after two closes back above 20DMA.",
        ),
        OverlayStrategyConfig(
            name="daily_name_level_trim_overlay__50dma_zero_confirm2",
            family="daily_name_level_trim_overlay",
            name_overlay=NameTrimOverlayConfig(
                name="50dma_zero_confirm2",
                trigger_family="close_below_50dma",
                trim_multiplier=0.00,
                confirm_days=2,
            ),
            note="Drop selected names to 0% after two closes below 50DMA; restore after two closes back above 50DMA.",
        ),
    ]


def build_combo_candidate(
    portfolio_candidate: OverlayStrategyConfig,
    name_candidate: OverlayStrategyConfig,
) -> OverlayStrategyConfig:
    return OverlayStrategyConfig(
        name=f"daily_portfolio_plus_name_overlay__{portfolio_candidate.portfolio_overlay.name}__{name_candidate.name_overlay.name}",
        family="daily_portfolio_plus_name_overlay",
        portfolio_overlay=portfolio_candidate.portfolio_overlay,
        name_overlay=name_candidate.name_overlay,
        note="Combine the best portfolio throttle candidate with the best name-level trim candidate; still no mid-month name additions.",
    )


def build_close_matrix(context: dict[str, object]) -> pd.DataFrame:
    close_matrix = (
        context["merged_stock_prices"]
        .pivot_table(index="as_of", columns="symbol", values="close", aggfunc="last")
        .sort_index()
    )
    close_matrix.index = pd.to_datetime(close_matrix.index).normalize()
    close_matrix.columns = close_matrix.columns.astype(str).str.upper()
    return close_matrix.reindex(context["master_index"]).ffill()


def build_signal_matrices(close_matrix: pd.DataFrame) -> dict[str, pd.DataFrame]:
    return {
        "close": close_matrix,
        "ma20": close_matrix.rolling(20).mean(),
        "ma50": close_matrix.rolling(50).mean(),
        "mom5": close_matrix.pct_change(5),
    }


def safe_series_mean(values: Iterable[float]) -> float:
    clean = [float(v) for v in values if pd.notna(v)]
    return float(np.mean(clean)) if clean else float("nan")


def true_spell_lengths(flags: pd.Series) -> list[int]:
    if flags.empty:
        return []
    flags = flags.fillna(False).astype(bool)
    groups = (flags != flags.shift(fill_value=False)).cumsum()
    lengths = []
    for _, group in flags.groupby(groups):
        if bool(group.iloc[0]):
            lengths.append(int(len(group)))
    return lengths


def update_portfolio_state(current: float, desired: float, recovery_count: int, recovery_days: int) -> tuple[float, int, bool]:
    desired = float(desired)
    current = float(current)
    if pd.isna(desired):
        return current, 0, False
    if desired < current - 1e-12:
        return desired, 0, True
    if desired > current + 1e-12:
        recovery_count += 1
        if recovery_count >= recovery_days:
            return desired, 0, True
        return current, recovery_count, False
    return current, 0, False


def compute_held_breadth(
    date: pd.Timestamp,
    symbols: list[str],
    signal_matrices: Mapping[str, pd.DataFrame],
    window: str,
) -> float:
    if not symbols:
        return float("nan")
    close_row = signal_matrices["close"].loc[date, symbols]
    ma_row = signal_matrices[window].loc[date, symbols]
    valid = close_row.notna() & ma_row.notna()
    if not valid.any():
        return float("nan")
    return float((close_row[valid] > ma_row[valid]).mean())


def compute_portfolio_throttle_desired(
    overlay: PortfolioThrottleOverlayConfig,
    date: pd.Timestamp,
    monthly_base_stock_weight: float,
    monthly_names: list[str],
    signal_matrices: Mapping[str, pd.DataFrame],
) -> tuple[float, dict[str, float | str | bool]]:
    if monthly_base_stock_weight <= 0 or not monthly_names:
        return 0.0, {
            "signal_family": overlay.signal_family,
            "degraded_signal": False,
            "degraded_reason": "baseline_zero_stock_exposure",
            "held_breadth_20": float("nan"),
            "held_breadth_50": float("nan"),
            "qqq_sma20_gap": float("nan"),
            "qqq_sma50_gap": float("nan"),
            "qqq_mom5": float("nan"),
        }

    close = signal_matrices["close"]
    ma20 = signal_matrices["ma20"]
    ma50 = signal_matrices["ma50"]
    mom5 = signal_matrices["mom5"]
    qqq_close = close.at[date, "QQQ"] if "QQQ" in close.columns else float("nan")
    qqq_ma20 = ma20.at[date, "QQQ"] if "QQQ" in ma20.columns else float("nan")
    qqq_ma50 = ma50.at[date, "QQQ"] if "QQQ" in ma50.columns else float("nan")
    qqq_mom5 = mom5.at[date, "QQQ"] if "QQQ" in mom5.columns else float("nan")
    held_breadth_20 = compute_held_breadth(date, monthly_names, signal_matrices, "ma20")
    held_breadth_50 = compute_held_breadth(date, monthly_names, signal_matrices, "ma50")

    degraded = any(pd.isna(v) for v in (qqq_close, qqq_ma20, qqq_ma50, qqq_mom5))
    degraded_reason = ""
    if overlay.name == "held_breadth_5state" and pd.isna(held_breadth_20):
        degraded = True
    if overlay.name == "held_breadth_5state" and pd.isna(held_breadth_50):
        degraded = True

    if degraded:
        degraded_reason = "signal_warmup_or_missing"
        desired = monthly_base_stock_weight
    elif overlay.name == "held_breadth_5state":
        qqq_sma20_gap = float(qqq_close / qqq_ma20 - 1.0)
        qqq_sma50_gap = float(qqq_close / qqq_ma50 - 1.0)
        if qqq_sma50_gap < 0.0 and held_breadth_50 <= 0.25:
            desired = 0.0
        elif qqq_sma50_gap < 0.0 or held_breadth_50 <= 0.375:
            desired = 0.20
        elif qqq_sma20_gap < 0.0 or held_breadth_20 <= 0.50:
            desired = 0.40
        elif qqq_mom5 <= 0.0 or held_breadth_20 <= 0.625:
            desired = 0.60
        else:
            desired = 0.80
    elif overlay.name == "qqq_dma_mom_6state":
        qqq_sma20_gap = float(qqq_close / qqq_ma20 - 1.0)
        qqq_sma50_gap = float(qqq_close / qqq_ma50 - 1.0)
        if qqq_sma50_gap < -0.01 and qqq_mom5 < -0.03:
            desired = 0.0
        elif qqq_sma50_gap < 0.0 and qqq_mom5 < 0.0:
            desired = 0.20
        elif qqq_sma50_gap < 0.0 or qqq_sma20_gap < -0.01:
            desired = 0.40
        elif qqq_sma20_gap < 0.0:
            desired = 0.60
        elif qqq_mom5 < 0.0:
            desired = 0.70
        else:
            desired = 0.80
    else:
        raise ValueError(f"Unsupported portfolio overlay: {overlay.name}")

    desired = min(float(monthly_base_stock_weight), float(desired))
    return desired, {
        "signal_family": overlay.signal_family,
        "degraded_signal": bool(degraded),
        "degraded_reason": degraded_reason,
        "held_breadth_20": float(held_breadth_20) if pd.notna(held_breadth_20) else float("nan"),
        "held_breadth_50": float(held_breadth_50) if pd.notna(held_breadth_50) else float("nan"),
        "qqq_sma20_gap": float(qqq_close / qqq_ma20 - 1.0) if pd.notna(qqq_close) and pd.notna(qqq_ma20) and qqq_ma20 else float("nan"),
        "qqq_sma50_gap": float(qqq_close / qqq_ma50 - 1.0) if pd.notna(qqq_close) and pd.notna(qqq_ma50) and qqq_ma50 else float("nan"),
        "qqq_mom5": float(qqq_mom5) if pd.notna(qqq_mom5) else float("nan"),
    }


def update_name_trim_states(
    overlay: NameTrimOverlayConfig,
    date: pd.Timestamp,
    monthly_names: list[str],
    signal_matrices: Mapping[str, pd.DataFrame],
    states: Mapping[str, NameTrimState],
) -> tuple[dict[str, NameTrimState], dict[str, float], dict[str, object]]:
    if not monthly_names:
        return dict(states), {}, {"degraded_signal": False, "degraded_symbols": (), "trimmed_symbols": (), "state_changes": 0}

    next_states: dict[str, NameTrimState] = {symbol: NameTrimState(state.multiplier, state.below_count, state.above_count) for symbol, state in states.items()}
    multipliers: dict[str, float] = {}
    trimmed_symbols: list[str] = []
    degraded_symbols: list[str] = []
    state_changes = 0
    close = signal_matrices["close"]
    ma20 = signal_matrices["ma20"]
    ma50 = signal_matrices["ma50"]

    for symbol in monthly_names:
        state = next_states.setdefault(symbol, NameTrimState())
        px = close.at[date, symbol] if symbol in close.columns else float("nan")
        if overlay.trigger_family == "close_below_20dma":
            trigger_ma = ma20.at[date, symbol] if symbol in ma20.columns else float("nan")
            recover_ma = trigger_ma
        elif overlay.trigger_family == "close_below_50dma":
            trigger_ma = ma50.at[date, symbol] if symbol in ma50.columns else float("nan")
            recover_ma = trigger_ma
        else:
            raise ValueError(f"Unsupported name overlay trigger_family: {overlay.trigger_family}")

        if pd.isna(px) or pd.isna(trigger_ma) or pd.isna(recover_ma):
            degraded_symbols.append(symbol)
            multipliers[symbol] = float(state.multiplier)
            if state.multiplier < 1.0:
                trimmed_symbols.append(symbol)
            continue

        if state.multiplier >= 0.999999:
            if float(px) < float(trigger_ma):
                state.below_count += 1
            else:
                state.below_count = 0
            state.above_count = 0
            if state.below_count >= overlay.confirm_days:
                state.multiplier = float(overlay.trim_multiplier)
                state.below_count = 0
                state_changes += 1
        else:
            if float(px) > float(recover_ma):
                state.above_count += 1
            else:
                state.above_count = 0
            state.below_count = 0
            if state.above_count >= overlay.confirm_days:
                state.multiplier = 1.0
                state.above_count = 0
                state_changes += 1

        multipliers[symbol] = float(state.multiplier)
        if state.multiplier < 1.0:
            trimmed_symbols.append(symbol)

    return next_states, multipliers, {
        "degraded_signal": bool(degraded_symbols),
        "degraded_symbols": tuple(sorted(degraded_symbols)),
        "trimmed_symbols": tuple(sorted(trimmed_symbols)),
        "state_changes": int(state_changes),
    }


def build_effective_weights(
    monthly_target_weights: Mapping[str, float],
    monthly_names: list[str],
    *,
    monthly_base_stock_weight: float,
    portfolio_target: float | None,
    name_multipliers: Mapping[str, float] | None,
) -> dict[str, float]:
    trimmed_weights = {symbol: float(monthly_target_weights.get(symbol, 0.0)) for symbol in monthly_names}
    if name_multipliers:
        for symbol in monthly_names:
            trimmed_weights[symbol] = float(trimmed_weights[symbol] * float(name_multipliers.get(symbol, 1.0)))

    stock_target_limit = float(monthly_base_stock_weight if portfolio_target is None else min(monthly_base_stock_weight, portfolio_target))
    stock_weight_pre_scale = float(sum(trimmed_weights.values()))
    if stock_weight_pre_scale > stock_target_limit + 1e-12 and stock_weight_pre_scale > 0:
        scale = stock_target_limit / stock_weight_pre_scale
        trimmed_weights = {symbol: float(weight * scale) for symbol, weight in trimmed_weights.items()}
    final_stock_weight = float(sum(trimmed_weights.values()))

    weights = {symbol: weight for symbol, weight in trimmed_weights.items() if weight > 1e-12}
    if final_stock_weight < 1.0:
        weights[SAFE_HAVEN] = 1.0 - final_stock_weight
    return weights


def run_overlay_backtest(
    context: dict[str, object],
    baseline_candidate: v12.GeometryCandidate,
    overlay: OverlayStrategyConfig,
) -> OverlayArtifacts:
    index = context["stock_returns_matrix"].index
    rebalance_dates = set(context["raw_snapshots"])
    returns_matrix = context["stock_returns_matrix"]
    close_matrix = build_close_matrix(context)
    signal_matrices = build_signal_matrices(close_matrix)

    weights_history = pd.DataFrame(0.0, index=index, columns=sorted(set(returns_matrix.columns) | {SAFE_HAVEN}))
    gross_returns = pd.Series(0.0, index=index, name=overlay.name)
    turnover_history = pd.Series(0.0, index=index, name="turnover")
    selection_rows: list[dict[str, object]] = []
    overlay_rows: list[dict[str, object]] = []

    current_effective_weights: dict[str, float] = {SAFE_HAVEN: 1.0}
    current_monthly_holdings: set[str] = set()
    monthly_target_weights: dict[str, float] = {SAFE_HAVEN: 1.0}
    monthly_names: list[str] = []
    monthly_base_stock_weight = 0.0
    monthly_regime = "pre_start"
    monthly_breadth = float("nan")
    portfolio_state = 0.0
    portfolio_recovery_count = 0
    name_states: dict[str, NameTrimState] = {}

    for idx in range(len(index) - 1):
        date = pd.Timestamp(index[idx]).normalize()
        next_date = pd.Timestamp(index[idx + 1]).normalize()

        if date in rebalance_dates:
            monthly_target_weights, metadata = v12.build_target_weights_with_override(
                context["raw_snapshots"][date],
                current_monthly_holdings,
                baseline_candidate,
            )
            monthly_names = [symbol for symbol, weight in monthly_target_weights.items() if symbol != SAFE_HAVEN and weight > 1e-12]
            current_monthly_holdings = set(monthly_names)
            monthly_base_stock_weight = float(sum(monthly_target_weights.get(symbol, 0.0) for symbol in monthly_names))
            monthly_regime = str(metadata.get("regime", "unknown"))
            monthly_breadth = float(metadata.get("breadth_ratio", float("nan")))
            selection_rows.append(
                {
                    "rebalance_date": date,
                    "selected_symbols": tuple(monthly_names),
                    "selected_count": len(monthly_names),
                    "regime": monthly_regime,
                    "breadth_ratio": monthly_breadth,
                    "benchmark_symbol": metadata.get("benchmark_symbol"),
                    "benchmark_trend_positive": metadata.get("benchmark_trend_positive"),
                    "candidate_count": metadata.get("candidate_count"),
                    "stock_exposure": metadata.get("stock_exposure"),
                    "family": baseline_candidate.config.family,
                    "overlay_family": overlay.family,
                }
            )
            portfolio_state = monthly_base_stock_weight
            portfolio_recovery_count = 0
            name_states = {symbol: NameTrimState() for symbol in monthly_names}

        portfolio_target = None
        portfolio_signal_meta: dict[str, object] = {
            "signal_family": "none",
            "degraded_signal": False,
            "degraded_reason": "",
            "held_breadth_20": float("nan"),
            "held_breadth_50": float("nan"),
            "qqq_sma20_gap": float("nan"),
            "qqq_sma50_gap": float("nan"),
            "qqq_mom5": float("nan"),
        }
        if overlay.portfolio_overlay is not None:
            desired_target, portfolio_signal_meta = compute_portfolio_throttle_desired(
                overlay.portfolio_overlay,
                date,
                monthly_base_stock_weight,
                monthly_names,
                signal_matrices,
            )
            portfolio_state, portfolio_recovery_count, _transitioned = update_portfolio_state(
                portfolio_state,
                desired_target,
                portfolio_recovery_count,
                overlay.portfolio_overlay.recovery_days,
            )
            portfolio_target = min(monthly_base_stock_weight, float(portfolio_state))

        name_multipliers: dict[str, float] | None = None
        trim_meta: dict[str, object] = {"degraded_signal": False, "degraded_symbols": (), "trimmed_symbols": (), "state_changes": 0}
        if overlay.name_overlay is not None:
            name_states, name_multipliers, trim_meta = update_name_trim_states(
                overlay.name_overlay,
                date,
                monthly_names,
                signal_matrices,
                name_states,
            )

        effective_weights = build_effective_weights(
            monthly_target_weights,
            monthly_names,
            monthly_base_stock_weight=monthly_base_stock_weight,
            portfolio_target=portfolio_target,
            name_multipliers=name_multipliers,
        )
        effective_names = {symbol for symbol, weight in effective_weights.items() if symbol != SAFE_HAVEN and weight > 1e-12}
        if not effective_names.issubset(set(monthly_names)):
            raise RuntimeError(f"Overlay added new names mid-month on {date.date()}: {sorted(effective_names - set(monthly_names))}")

        turnover_history.at[next_date] = suite.compute_turnover(current_effective_weights, effective_weights)
        current_effective_weights = effective_weights

        for symbol, weight in current_effective_weights.items():
            if symbol not in weights_history.columns:
                weights_history[symbol] = 0.0
            weights_history.at[date, symbol] = weight

        next_returns = returns_matrix.loc[next_date].fillna(0.0)
        gross_returns.at[next_date] = sum(weight * float(next_returns.get(symbol, 0.0)) for symbol, weight in current_effective_weights.items())

        effective_stock_weight = float(sum(weight for symbol, weight in current_effective_weights.items() if symbol != SAFE_HAVEN))
        overlay_rows.append(
            {
                "date": date,
                "strategy": overlay.name,
                "overlay_family": overlay.family,
                "monthly_selected_symbols": tuple(monthly_names),
                "monthly_selected_count": len(monthly_names),
                "monthly_base_stock_weight": monthly_base_stock_weight,
                "effective_stock_weight": effective_stock_weight,
                "effective_boxx_weight": float(current_effective_weights.get(SAFE_HAVEN, 0.0)),
                "monthly_regime": monthly_regime,
                "monthly_breadth_ratio": monthly_breadth,
                "portfolio_target_stock_weight": float(portfolio_target) if portfolio_target is not None else monthly_base_stock_weight,
                "portfolio_overlay_active": bool((portfolio_target is not None) and (portfolio_target < monthly_base_stock_weight - 1e-12)),
                "trimmed_name_count": int(sum(1 for value in (name_multipliers or {}).values() if value < 1.0 - 1e-12)),
                "trimmed_name_symbols": tuple(sorted(symbol for symbol, value in (name_multipliers or {}).items() if value < 1.0 - 1e-12)),
                "name_overlay_active": bool(trim_meta.get("trimmed_symbols")),
                "degraded_signal": bool(portfolio_signal_meta.get("degraded_signal", False) or trim_meta.get("degraded_signal", False)),
                "degraded_reason": ";".join(filter(None, [str(portfolio_signal_meta.get("degraded_reason", "")), "name_signal_missing" if trim_meta.get("degraded_signal") else ""])),
                "signal_family": portfolio_signal_meta.get("signal_family", "none"),
                "held_breadth_20": portfolio_signal_meta.get("held_breadth_20", float("nan")),
                "held_breadth_50": portfolio_signal_meta.get("held_breadth_50", float("nan")),
                "qqq_sma20_gap": portfolio_signal_meta.get("qqq_sma20_gap", float("nan")),
                "qqq_sma50_gap": portfolio_signal_meta.get("qqq_sma50_gap", float("nan")),
                "qqq_mom5": portfolio_signal_meta.get("qqq_mom5", float("nan")),
                "state_change_count": int(trim_meta.get("state_changes", 0)),
            }
        )

    for symbol, weight in current_effective_weights.items():
        if symbol not in weights_history.columns:
            weights_history[symbol] = 0.0
        weights_history.at[index[-1], symbol] = weight

    overlay_history = pd.DataFrame(overlay_rows)
    selection_history = pd.DataFrame(selection_rows)
    rolling_alpha = robust.compute_rolling_capm_alpha_fast(gross_returns, returns_matrix["QQQ"].reindex(gross_returns.index).fillna(0.0))
    trigger_stats = summarize_overlay_state(overlay_history)
    return OverlayArtifacts(
        gross_returns=gross_returns,
        weights_history=weights_history.loc[:, (weights_history != 0.0).any(axis=0)],
        turnover_history=turnover_history,
        selection_history=selection_history,
        overlay_history=overlay_history,
        rolling_alpha=rolling_alpha,
        trigger_stats=trigger_stats,
    )


def summarize_overlay_state(overlay_history: pd.DataFrame) -> dict[str, float]:
    if overlay_history.empty:
        return {
            "overlay_trigger_frequency": float("nan"),
            "average_days_in_throttle": float("nan"),
            "average_days_trimmed": float("nan"),
            "days_in_throttle_share": float("nan"),
            "days_trimmed_share": float("nan"),
            "degraded_signal_share": float("nan"),
        }
    throttle_flags = overlay_history["portfolio_overlay_active"].fillna(False).astype(bool)
    trim_flags = overlay_history["name_overlay_active"].fillna(False).astype(bool)
    throttle_spells = true_spell_lengths(throttle_flags)
    trim_spells = true_spell_lengths(trim_flags)
    any_overlay = throttle_flags | trim_flags
    return {
        "overlay_trigger_frequency": float(any_overlay.mean()),
        "average_days_in_throttle": float(np.mean(throttle_spells)) if throttle_spells else 0.0,
        "average_days_trimmed": float(np.mean(trim_spells)) if trim_spells else 0.0,
        "days_in_throttle_share": float(throttle_flags.mean()),
        "days_trimmed_share": float(trim_flags.mean()),
        "degraded_signal_share": float(overlay_history["degraded_signal"].fillna(False).mean()),
    }


def compute_period_overlay_stats(
    overlay_history: pd.DataFrame,
    *,
    start: str | None,
    end: str | None,
) -> dict[str, float]:
    if overlay_history.empty:
        return {
            "Average Stock Weight": float("nan"),
            "Average BOXX Weight": float("nan"),
            "Overlay Trigger Frequency": float("nan"),
            "Average Days In Throttle": float("nan"),
            "Average Days Trimmed": float("nan"),
            "Days In Throttle Share": float("nan"),
            "Days Trimmed Share": float("nan"),
            "Degraded Signal Share": float("nan"),
        }
    frame = overlay_history.copy().set_index("date")
    frame.index = pd.to_datetime(frame.index).normalize()
    if start:
        frame = frame.loc[start:]
    if end:
        frame = frame.loc[:end]
    if frame.empty:
        return {
            "Average Stock Weight": float("nan"),
            "Average BOXX Weight": float("nan"),
            "Overlay Trigger Frequency": float("nan"),
            "Average Days In Throttle": float("nan"),
            "Average Days Trimmed": float("nan"),
            "Days In Throttle Share": float("nan"),
            "Days Trimmed Share": float("nan"),
            "Degraded Signal Share": float("nan"),
        }
    throttle_flags = frame["portfolio_overlay_active"].fillna(False).astype(bool)
    trim_flags = frame["name_overlay_active"].fillna(False).astype(bool)
    return {
        "Average Stock Weight": float(frame["effective_stock_weight"].mean()),
        "Average BOXX Weight": float(frame["effective_boxx_weight"].mean()),
        "Overlay Trigger Frequency": float((throttle_flags | trim_flags).mean()),
        "Average Days In Throttle": float(np.mean(true_spell_lengths(throttle_flags))) if throttle_flags.any() else 0.0,
        "Average Days Trimmed": float(np.mean(true_spell_lengths(trim_flags))) if trim_flags.any() else 0.0,
        "Days In Throttle Share": float(throttle_flags.mean()),
        "Days Trimmed Share": float(trim_flags.mean()),
        "Degraded Signal Share": float(frame["degraded_signal"].fillna(False).mean()),
    }


def candidate_rows_from_artifacts(
    strategy_name: str,
    family: str,
    artifacts: OverlayArtifacts,
    benchmark_returns: pd.Series,
    *,
    cost_bps_values: Iterable[float],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for cost_bps in cost_bps_values:
        net_returns = artifacts.gross_returns - artifacts.turnover_history.reindex(artifacts.gross_returns.index).fillna(0.0) * (float(cost_bps) / 10_000.0)
        rolling_alpha = robust.compute_rolling_capm_alpha_fast(net_returns, benchmark_returns.reindex(net_returns.index).fillna(0.0))
        for period_name, start, end in COMPARISON_PERIODS:
            metrics = robust.evaluate_period_metrics(
                net_returns,
                artifacts.weights_history,
                artifacts.turnover_history,
                benchmark_returns,
                start=start,
                end=end,
            )
            overlay_stats = compute_period_overlay_stats(artifacts.overlay_history, start=start, end=end)
            rolling_series = rolling_alpha if period_name == "Full Sample" else robust.compute_rolling_capm_alpha_fast(
                robust.slice_series_or_frame(net_returns, start, end),
                robust.slice_series_or_frame(benchmark_returns, start, end),
            )
            rows.append(
                {
                    "strategy": strategy_name,
                    "family": family,
                    "cost_bps_one_way": float(cost_bps),
                    "period": period_name,
                    **metrics,
                    **robust.compute_rolling_alpha_summary(rolling_series),
                    **overlay_stats,
                    **artifacts.trigger_stats,
                }
            )
    return rows


def reference_rows(
    strategy_name: str,
    family: str,
    gross_returns: pd.Series,
    weights_history: pd.DataFrame,
    turnover_history: pd.Series,
    benchmark_returns: pd.Series,
    *,
    cost_bps_values: Iterable[float],
) -> list[dict[str, object]]:
    overlay_history = pd.DataFrame(
        {
            "date": gross_returns.index,
            "effective_stock_weight": weights_history.drop(columns=[SAFE_HAVEN], errors="ignore").sum(axis=1).reindex(gross_returns.index).fillna(0.0).values,
            "effective_boxx_weight": weights_history.get(SAFE_HAVEN, pd.Series(0.0, index=weights_history.index)).reindex(gross_returns.index).fillna(0.0).values,
            "portfolio_overlay_active": False,
            "name_overlay_active": False,
            "degraded_signal": False,
        }
    )
    artifacts = OverlayArtifacts(
        gross_returns=gross_returns,
        weights_history=weights_history,
        turnover_history=turnover_history,
        selection_history=pd.DataFrame(),
        overlay_history=overlay_history,
        rolling_alpha=robust.compute_rolling_capm_alpha_fast(gross_returns, benchmark_returns.reindex(gross_returns.index).fillna(0.0)),
        trigger_stats=summarize_overlay_state(overlay_history),
    )
    return candidate_rows_from_artifacts(strategy_name, family, artifacts, benchmark_returns, cost_bps_values=cost_bps_values)


def percentile_rank(series: pd.Series, *, higher_is_better: bool) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    ranked = numeric.rank(method="average", pct=True) if higher_is_better else (-numeric).rank(method="average", pct=True)
    return ranked.fillna(0.0)


def build_family_selection_table(rows: pd.DataFrame, *, oos_benchmark_cagr: float) -> pd.DataFrame:
    main = rows.loc[(rows["cost_bps_one_way"] == MAIN_COST_BPS) & (rows["period"] == "OOS Sample")].copy()
    full = rows.loc[(rows["cost_bps_one_way"] == MAIN_COST_BPS) & (rows["period"] == "Full Sample")].copy().set_index("strategy")
    y2022 = rows.loc[(rows["cost_bps_one_way"] == MAIN_COST_BPS) & (rows["period"] == "2022")].copy().set_index("strategy")
    y2023 = rows.loc[(rows["cost_bps_one_way"] == MAIN_COST_BPS) & (rows["period"] == "2023+")].copy().set_index("strategy")
    if main.empty:
        return main
    main["full_cagr"] = main["strategy"].map(full["CAGR"])
    main["full_max_drawdown"] = main["strategy"].map(full["Max Drawdown"])
    main["return_2022"] = main["strategy"].map(y2022["Total Return"])
    main["cagr_2023_plus"] = main["strategy"].map(y2023["CAGR"])
    main["score_oos_rel_qqq"] = percentile_rank(main["CAGR"] - float(oos_benchmark_cagr), higher_is_better=True)
    main["score_oos_cagr"] = percentile_rank(main["CAGR"], higher_is_better=True)
    main["score_oos_ir"] = percentile_rank(main["Information Ratio vs QQQ"], higher_is_better=True)
    main["score_oos_alpha"] = percentile_rank(main["alpha_ann_vs_qqq"], higher_is_better=True)
    main["score_oos_maxdd"] = percentile_rank(main["Max Drawdown"], higher_is_better=True)
    main["score_2022"] = percentile_rank(main["return_2022"], higher_is_better=True)
    main["score_turnover"] = percentile_rank(main["Turnover/Year"], higher_is_better=False)
    main["overlay_selection_score"] = (
        main["score_oos_rel_qqq"] * 0.25
        + main["score_oos_ir"] * 0.20
        + main["score_oos_alpha"] * 0.15
        + main["score_oos_maxdd"] * 0.15
        + main["score_2022"] * 0.10
        + main["score_turnover"] * 0.10
        + main["score_oos_cagr"] * 0.05
    )
    return main.sort_values(
        by=["overlay_selection_score", "Information Ratio vs QQQ", "alpha_ann_vs_qqq", "CAGR"],
        ascending=[False, False, False, False],
    ).reset_index(drop=True)


def build_reference_rows_from_existing_results(
    strategy_name: str,
    family: str,
    source_csv: Path,
    *,
    scenario_name: str,
) -> list[dict[str, object]]:
    source = pd.read_csv(source_csv)
    frame = source.loc[source["scenario"] == scenario_name].copy()
    if frame.empty:
        raise RuntimeError(f"Scenario not found in {source_csv.name}: {scenario_name}")

    rows: list[dict[str, object]] = []
    for row in frame.itertuples(index=False):
        payload = row._asdict()
        rows.append(
            {
                "strategy": strategy_name,
                "family": family,
                "cost_bps_one_way": float(payload["cost_bps_one_way"]),
                "period": payload["period"],
                "Start": payload.get("Start"),
                "End": payload.get("End"),
                "Total Return": float(payload.get("Total Return", float("nan"))),
                "CAGR": float(payload.get("CAGR", float("nan"))),
                "Max Drawdown": float(payload.get("Max Drawdown", float("nan"))),
                "Volatility": float(payload.get("Volatility", float("nan"))),
                "Sharpe": float(payload.get("Sharpe", float("nan"))),
                "Sortino": float(payload.get("Sortino", float("nan"))),
                "Calmar": float(payload.get("Calmar", float("nan"))),
                "Turnover/Year": float(payload.get("Turnover/Year", float("nan"))),
                "Rebalances/Year": float(payload.get("Rebalances/Year", float("nan"))),
                "Average Names Held": float(payload.get("Average Names Held", float("nan"))),
                "Beta vs QQQ": float(payload.get("Beta vs QQQ", float("nan"))),
                "Information Ratio vs QQQ": float(payload.get("Information Ratio vs QQQ", float("nan"))),
                "Up Capture vs QQQ": float(payload.get("Up Capture vs QQQ", float("nan"))),
                "Down Capture vs QQQ": float(payload.get("Down Capture vs QQQ", float("nan"))),
                "2022 Return": float(payload.get("2022 Return", float("nan"))),
                "2023+ CAGR": float(payload.get("2023+ CAGR", float("nan"))),
                "beta_vs_qqq": float(payload.get("beta_vs_qqq", payload.get("Beta vs QQQ", float("nan")))),
                "alpha_ann_vs_qqq": float(payload.get("alpha_ann_vs_qqq", float("nan"))),
                "tracking_error_vs_qqq": float(payload.get("tracking_error_vs_qqq", float("nan"))),
                "information_ratio_vs_qqq": float(payload.get("information_ratio_vs_qqq", payload.get("Information Ratio vs QQQ", float("nan")))),
                "up_capture_vs_qqq": float(payload.get("up_capture_vs_qqq", payload.get("Up Capture vs QQQ", float("nan")))),
                "down_capture_vs_qqq": float(payload.get("down_capture_vs_qqq", payload.get("Down Capture vs QQQ", float("nan")))),
                "rolling_36m_alpha_mean": float(payload.get("rolling_36m_alpha_mean", float("nan"))),
                "rolling_36m_alpha_median": float(payload.get("rolling_36m_alpha_median", float("nan"))),
                "rolling_36m_alpha_last": float(payload.get("rolling_36m_alpha_last", float("nan"))),
                "rolling_36m_alpha_positive_ratio": float(payload.get("rolling_36m_alpha_positive_ratio", float("nan"))),
                "Average Stock Weight": float("nan"),
                "Average BOXX Weight": float("nan"),
                "Overlay Trigger Frequency": 0.0,
                "Average Days In Throttle": 0.0,
                "Average Days Trimmed": 0.0,
                "Days In Throttle Share": 0.0,
                "Days Trimmed Share": 0.0,
                "Degraded Signal Share": 0.0,
            }
        )
    return rows


def overlay_has_incremental_value(candidate_row: pd.Series, baseline_row: pd.Series) -> bool:
    return bool(
        float(candidate_row["CAGR"]) >= float(baseline_row["CAGR"]) - 0.03
        and (
            float(candidate_row["Max Drawdown"]) >= float(baseline_row["Max Drawdown"]) + 0.02
            or float(candidate_row["Information Ratio vs QQQ"]) >= float(baseline_row["Information Ratio vs QQQ"]) + 0.03
            or float(candidate_row["return_2022"]) >= float(baseline_row["return_2022"]) + 0.03
        )
    )


def format_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "_empty_"
    cols = [str(column) for column in frame.columns]
    lines = [
        "| " + " | ".join(cols) + " |",
        "| " + " | ".join(["---"] * len(cols)) + " |",
    ]
    for row in frame.itertuples(index=False, name=None):
        vals = []
        for value in row:
            if pd.isna(value):
                vals.append("")
            elif isinstance(value, (float, np.floating)):
                vals.append(f"{float(value):.6f}")
            else:
                vals.append(str(value))
        lines.append("| " + " | ".join(vals) + " |")
    return "\n".join(lines)


def write_summary_markdown(
    path: Path,
    *,
    baseline_payload: dict[str, object],
    baseline_row: pd.Series,
    family_scores: pd.DataFrame,
    comparison_rows: pd.DataFrame,
    recommendation: dict[str, object],
) -> None:
    best_portfolio = family_scores.loc[family_scores["family"] == "daily_portfolio_throttle_overlay"].head(1)
    best_name = family_scores.loc[family_scores["family"] == "daily_name_level_trim_overlay"].head(1)
    best_combo = family_scores.loc[family_scores["family"] == "daily_portfolio_plus_name_overlay"].head(1)
    lines = [
        "# cash_buffer_branch daily overlay research",
        "",
        "## Baseline",
        f"- strategy={baseline_payload['name']}",
        f"- family={baseline_payload['family']}",
        f"- monthly stock exposures={json.dumps(baseline_payload['exposures'], ensure_ascii=False)}",
        f"- holdings={baseline_payload['holdings_count']}",
        f"- single_name_cap={float(baseline_payload['single_name_cap']):.0%}",
        f"- sector_cap={float(baseline_payload['sector_cap']):.0%}",
        f"- hold_bonus={float(baseline_payload['hold_bonus']):.2f}",
        f"- benchmark={baseline_payload['benchmark_symbol']}",
        "- non-rebalance days=no-op baseline; overlay only changes exposure / existing-name weights / BOXX",
        "",
        "## Overlay families tested",
        "- daily_portfolio_throttle_overlay",
        "- daily_name_level_trim_overlay",
        "- daily_portfolio_plus_name_overlay (only if A/B both showed value)",
        "",
        "## Baseline reference (5 bps, OOS)",
        format_table(pd.DataFrame([baseline_row])[['strategy', 'CAGR', 'Max Drawdown', 'Information Ratio vs QQQ', 'alpha_ann_vs_qqq', 'return_2022', 'cagr_2023_plus', 'Turnover/Year', 'Average Stock Weight', 'Average BOXX Weight']]),
        "",
        "## Family selection table (5 bps, OOS)",
        format_table(
            family_scores[
                [
                    "strategy",
                    "family",
                    "overlay_selection_score",
                    "CAGR",
                    "Max Drawdown",
                    "Information Ratio vs QQQ",
                    "alpha_ann_vs_qqq",
                    "return_2022",
                    "cagr_2023_plus",
                    "Turnover/Year",
                    "Average Stock Weight",
                    "Average BOXX Weight",
                    "Overlay Trigger Frequency",
                    "Average Days In Throttle",
                    "Average Days Trimmed",
                ]
            ]
            .rename(
                columns={
                    "CAGR": "OOS CAGR",
                    "Max Drawdown": "OOS MaxDD",
                    "return_2022": "2022 Return",
                    "cagr_2023_plus": "2023+ CAGR",
                }
            )
            .head(12)
        ),
        "",
        "## Best portfolio overlay",
        format_table(best_portfolio) if not best_portfolio.empty else "_not tested_",
        "",
        "## Best name-level trim overlay",
        format_table(best_name) if not best_name.empty else "_not tested_",
        "",
        "## Best combo overlay",
        format_table(best_combo) if not best_combo.empty else "_skipped because A/B did not both clear the incremental-value filter_",
        "",
        "## 5 bps comparison set",
        format_table(comparison_rows[[
            'strategy', 'family', 'CAGR', 'cagr_minus_qqq', 'Max Drawdown', 'return_2022', 'cagr_2023_plus',
            'Turnover/Year', 'Average Names Held', 'Average Stock Weight', 'Average BOXX Weight',
            'beta_vs_qqq', 'alpha_ann_vs_qqq', 'Information Ratio vs QQQ', 'Up Capture vs QQQ', 'Down Capture vs QQQ',
            'Overlay Trigger Frequency', 'Average Days In Throttle', 'Average Days Trimmed'
        ]]),
        "",
        "## Recommendation",
        f"- overlay_has_incremental_value={recommendation['overlay_has_incremental_value']}",
        f"- best_overlay_family={recommendation['best_overlay_family']}",
        f"- best_overlay_strategy={recommendation['best_overlay_strategy']}",
        f"- recommended_upgrade_direction={recommendation['recommended_upgrade_direction']}",
        f"- next_step={recommendation['next_step']}",
        "",
        "## Explicit caveats",
        "- This stays research-only; no runtime / Cloud Run / paper config is changed here.",
        "- Month-start still chooses the stock list. Overlay never adds names mid-month.",
        "- Reduced stock exposure always parks in BOXX (no hidden new sleeve).",
        "- Daily signals can degrade to monthly baseline when short lookback indicators are unavailable; degraded-signal share is reported explicitly.",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = parse_args()
    results_dir = Path(args.results_dir).expanduser().resolve()
    results_dir.mkdir(parents=True, exist_ok=True)

    baseline_path = DEFAULT_BASELINE_CONFIG_PATH
    baseline_spec = load_baseline_spec(baseline_path)
    baseline_candidate = build_baseline_candidate(baseline_spec)

    alias_dir = robust.discover_run_dir(args.alias_data_run_dir, "r1000_multifactor_defensive_*_official_monthly_v2_alias")
    context = gp.build_context(alias_dir, start=args.start, end=args.end)
    benchmark_returns = context["stock_returns_matrix"]["QQQ"].copy()

    baseline_artifacts = run_overlay_backtest(
        context,
        baseline_candidate,
        OverlayStrategyConfig(name=baseline_spec.name, family="monthly_baseline", note="No overlay"),
    )
    all_rows = candidate_rows_from_artifacts(
        baseline_spec.name,
        "monthly_baseline",
        baseline_artifacts,
        benchmark_returns,
        cost_bps_values=COST_LEVELS,
    )

    portfolio_candidates = build_portfolio_overlay_candidates()
    name_candidates = build_name_overlay_candidates()
    overlay_artifacts_by_name: dict[str, OverlayArtifacts] = {baseline_spec.name: baseline_artifacts}

    for overlay in portfolio_candidates + name_candidates:
        artifacts = run_overlay_backtest(context, baseline_candidate, overlay)
        overlay_artifacts_by_name[overlay.name] = artifacts
        all_rows.extend(candidate_rows_from_artifacts(overlay.name, overlay.family, artifacts, benchmark_returns, cost_bps_values=COST_LEVELS))

    all_rows_df = pd.DataFrame(all_rows)
    qqq_oos_cagr = suite.compute_period_cagr(benchmark_returns, OOS_START, None)
    family_scores = build_family_selection_table(all_rows_df, oos_benchmark_cagr=qqq_oos_cagr)
    baseline_oos_row = family_scores.loc[family_scores["strategy"] == baseline_spec.name].iloc[0]
    best_portfolio_row = family_scores.loc[family_scores["family"] == "daily_portfolio_throttle_overlay"].iloc[0]
    best_name_row = family_scores.loc[family_scores["family"] == "daily_name_level_trim_overlay"].iloc[0]

    combo_candidate: OverlayStrategyConfig | None = None
    if overlay_has_incremental_value(best_portfolio_row, baseline_oos_row) and overlay_has_incremental_value(best_name_row, baseline_oos_row):
        portfolio_cfg = next(candidate for candidate in portfolio_candidates if candidate.name == str(best_portfolio_row["strategy"]))
        name_cfg = next(candidate for candidate in name_candidates if candidate.name == str(best_name_row["strategy"]))
        combo_candidate = build_combo_candidate(portfolio_cfg, name_cfg)
        combo_artifacts = run_overlay_backtest(context, baseline_candidate, combo_candidate)
        overlay_artifacts_by_name[combo_candidate.name] = combo_artifacts
        combo_candidate_rows = candidate_rows_from_artifacts(combo_candidate.name, combo_candidate.family, combo_artifacts, benchmark_returns, cost_bps_values=COST_LEVELS)
        all_rows_df = pd.concat([all_rows_df, pd.DataFrame(combo_candidate_rows)], ignore_index=True)
        family_scores = build_family_selection_table(all_rows_df, oos_benchmark_cagr=qqq_oos_cagr)

    # qqq_plus_current_default reference
    qqq_rows = build_reference_rows_from_existing_results(
        "qqq_plus_current_default",
        "reference",
        DEFAULT_QQQ_PLUS_RESULTS_PATH,
        scenario_name=QQQ_PLUS_CURRENT_DEFAULT_SCENARIO,
    )
    all_rows_df = pd.concat([all_rows_df, pd.DataFrame(qqq_rows)], ignore_index=True)

    # defensive baseline reference
    defensive_result = suite.run_defensive_backtest(
        context["stock_price_history"],
        context["universe_history"],
        start_date=str(pd.Timestamp(context["master_index"][0]).date()),
        end_date=str(pd.Timestamp(context["master_index"][-1]).date()),
        turnover_cost_bps=0.0,
    )
    defensive_rows = reference_rows(
        "russell_1000_multi_factor_defensive",
        "reference",
        defensive_result["portfolio_returns"].reindex(benchmark_returns.index).fillna(0.0),
        defensive_result["weights_history"].reindex(benchmark_returns.index).fillna(0.0),
        defensive_result["turnover_history"].reindex(benchmark_returns.index).fillna(0.0),
        benchmark_returns,
        cost_bps_values=COST_LEVELS,
    )
    all_rows_df = pd.concat([all_rows_df, pd.DataFrame(defensive_rows)], ignore_index=True)

    # QQQ reference
    qqq_only_weights = pd.DataFrame({"QQQ": 1.0}, index=benchmark_returns.index)
    qqq_only_turnover = pd.Series(0.0, index=benchmark_returns.index, name="turnover")
    qqq_ref_rows = reference_rows(
        "QQQ",
        "reference",
        benchmark_returns,
        qqq_only_weights,
        qqq_only_turnover,
        benchmark_returns,
        cost_bps_values=COST_LEVELS,
    )
    all_rows_df = pd.concat([all_rows_df, pd.DataFrame(qqq_ref_rows)], ignore_index=True)

    # final family_scores after references added (references will not affect overlay ranking if filtered)
    family_scores = build_family_selection_table(
        all_rows_df.loc[
            all_rows_df["family"].isin({"monthly_baseline", "daily_portfolio_throttle_overlay", "daily_name_level_trim_overlay", "daily_portfolio_plus_name_overlay"})
        ].copy(),
        oos_benchmark_cagr=qqq_oos_cagr,
    )
    baseline_oos_row = family_scores.loc[family_scores["strategy"] == baseline_spec.name].iloc[0]
    best_portfolio_row = family_scores.loc[family_scores["family"] == "daily_portfolio_throttle_overlay"].iloc[0]
    best_name_row = family_scores.loc[family_scores["family"] == "daily_name_level_trim_overlay"].iloc[0]
    best_combo_row = family_scores.loc[family_scores["family"] == "daily_portfolio_plus_name_overlay"].head(1)

    comparison_5bps = all_rows_df.loc[(all_rows_df["cost_bps_one_way"] == MAIN_COST_BPS) & (all_rows_df["period"] == "OOS Sample")].copy()
    qqq_oos_cagr = float(comparison_5bps.loc[comparison_5bps["strategy"] == "QQQ", "CAGR"].iloc[0])
    comparison_5bps["cagr_minus_qqq"] = comparison_5bps["CAGR"] - qqq_oos_cagr
    comparison_5bps["return_2022"] = comparison_5bps["strategy"].map(
        all_rows_df.loc[(all_rows_df["cost_bps_one_way"] == MAIN_COST_BPS) & (all_rows_df["period"] == "2022")].set_index("strategy")["Total Return"]
    )
    comparison_5bps["cagr_2023_plus"] = comparison_5bps["strategy"].map(
        all_rows_df.loc[(all_rows_df["cost_bps_one_way"] == MAIN_COST_BPS) & (all_rows_df["period"] == "2023+")].set_index("strategy")["CAGR"]
    )

    best_portfolio_strategy = str(best_portfolio_row["strategy"])
    best_name_strategy = str(best_name_row["strategy"])
    best_combo_strategy = str(best_combo_row.iloc[0]["strategy"]) if not best_combo_row.empty else None

    overlay_candidates_for_recommend = [
        comparison_5bps.loc[comparison_5bps["strategy"] == best_portfolio_strategy].iloc[0],
        comparison_5bps.loc[comparison_5bps["strategy"] == best_name_strategy].iloc[0],
    ]
    if best_combo_strategy:
        overlay_candidates_for_recommend.append(comparison_5bps.loc[comparison_5bps["strategy"] == best_combo_strategy].iloc[0])
    overlay_best = max(overlay_candidates_for_recommend, key=lambda row: (float(row["Information Ratio vs QQQ"]), float(row["alpha_ann_vs_qqq"]), float(row["CAGR"])))

    overlay_has_value = overlay_has_incremental_value(overlay_best, comparison_5bps.loc[comparison_5bps["strategy"] == baseline_spec.name].iloc[0])
    best_family = str(overlay_best["family"])
    if not overlay_has_value:
        recommended_upgrade_direction = "do_not_upgrade_overlay"
        next_step = "保持月频 baseline，不加 overlay"
    elif best_family == "daily_portfolio_throttle_overlay":
        recommended_upgrade_direction = "portfolio_level_throttle_only"
        next_step = "继续研究一个最小可行 daily overlay"
    elif best_family == "daily_name_level_trim_overlay":
        recommended_upgrade_direction = "name_level_trim_only"
        next_step = "继续研究一个最小可行 daily overlay"
    elif best_family == "daily_portfolio_plus_name_overlay":
        recommended_upgrade_direction = "portfolio_plus_name_overlay"
        next_step = "继续研究一个最小可行 daily overlay"
    else:
        recommended_upgrade_direction = "no_overlay"
        next_step = "保持月频 baseline，不加 overlay"

    recommendation = {
        "baseline_strategy": baseline_spec.name,
        "overlay_has_incremental_value": bool(overlay_has_value),
        "best_overlay_family": best_family,
        "best_overlay_strategy": str(overlay_best["strategy"]),
        "recommended_upgrade_direction": recommended_upgrade_direction,
        "answers": {
            "Q1_incremental_value": bool(overlay_has_value),
            "Q2_increment_source": (
                "更好的下跌保护和更平滑的风险暴露"
                if float(overlay_best["Max Drawdown"]) >= float(baseline_oos_row["Max Drawdown"]) and float(overlay_best["Information Ratio vs QQQ"]) >= float(baseline_oos_row["Information Ratio vs QQQ"])
                else "主要是更高频交易碰巧有效的证据不足，增量不明显"
            ),
            "Q3_upgrade_candidate": bool(overlay_has_value),
            "Q4_best_overlay_type": recommended_upgrade_direction,
            "Q5_complexity_risk": bool(float(overlay_best["Turnover/Year"]) > float(baseline_oos_row["Turnover/Year"]) * 1.6),
            "Q6_next_step": next_step,
        },
        "notes": {
            "portfolio_overlay_tested": [candidate.name for candidate in portfolio_candidates],
            "name_overlay_tested": [candidate.name for candidate in name_candidates],
            "combo_tested": [combo_candidate.name] if combo_candidate else [],
            "comparison_cost_bps": list(COST_LEVELS),
            "baseline_non_rebalance_days": "no_op",
            "mid_month_new_names_allowed": False,
        },
    }

    comparison_export = all_rows_df.copy()
    comparison_export.to_csv(results_dir / "cash_buffer_branch_daily_overlay_comparison.csv", index=False)
    write_summary_markdown(
        results_dir / "cash_buffer_branch_daily_overlay_summary.md",
        baseline_payload=baseline_spec.config_payload,
        baseline_row=baseline_oos_row,
        family_scores=family_scores,
        comparison_rows=comparison_5bps.loc[
            comparison_5bps["strategy"].isin(
                {
                    baseline_spec.name,
                    best_portfolio_strategy,
                    best_name_strategy,
                    *(set([best_combo_strategy]) if best_combo_strategy else set()),
                    "qqq_plus_current_default",
                    "russell_1000_multi_factor_defensive",
                    "QQQ",
                }
            )
        ].sort_values(by=["family", "CAGR"], ascending=[True, False]),
        recommendation=recommendation,
    )
    (results_dir / "cash_buffer_branch_daily_overlay_recommendation.json").write_text(
        json.dumps(recommendation, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
