#!/usr/bin/env python3
"""
Research-only suite for a new US-equity strategy family:
`growth_pullback_systematic_v1`

Goal:
- systematize growth / leadership + controlled pullback entry ideas
- compare broad growth vs tech-heavy vs crypto-linked equity theme subsets
- keep everything inside US equities (no crypto assets, no Binance, no overlays)
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

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
DEFAULT_COSTS = (0.0, 5.0)
OOS_START = "2022-01-01"
COMPARISON_PERIODS = (
    ("Full Sample", None, None),
    ("OOS Sample", OOS_START, None),
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023+", "2023-01-01", None),
)
TECH_HEAVY_SECTORS = ("Information Technology", "Communication")
GROWTH_PULLBACK_NAME = "growth_pullback_systematic_v1"
MAIN_COST_BPS = 5.0

CRYPTO_THEME_ROWS = [
    {"symbol": "COIN", "theme_bucket": "exchange_broker", "reason": "Coinbase: spot + institutional crypto exchange / broker"},
    {"symbol": "MSTR", "theme_bucket": "btc_treasury_proxy", "reason": "MicroStrategy/Strategy: listed BTC treasury proxy"},
    {"symbol": "HOOD", "theme_bucket": "broker", "reason": "Robinhood: retail broker with material crypto trading exposure"},
    {"symbol": "SQ", "theme_bucket": "payments_wallet", "reason": "Block historical ticker SQ: Cash App / bitcoin exposure"},
    {"symbol": "XYZ", "theme_bucket": "payments_wallet", "reason": "Block current ticker XYZ: Cash App / bitcoin exposure"},
    {"symbol": "PYPL", "theme_bucket": "payments_wallet", "reason": "PayPal: crypto trading / wallet / stablecoin related initiatives"},
    {"symbol": "IBKR", "theme_bucket": "broker", "reason": "Interactive Brokers: access rails for crypto trading"},
    {"symbol": "CME", "theme_bucket": "exchange_infra", "reason": "CME: listed crypto futures / institutional market infrastructure"},
    {"symbol": "CBOE", "theme_bucket": "exchange_infra", "reason": "Cboe: crypto ETF / options market structure exposure"},
]
CRYPTO_THEME_SYMBOLS = tuple(row["symbol"] for row in CRYPTO_THEME_ROWS)

HYPOTHESES_NOTE = """# growth_pullback_systematic_v1 hypotheses

## Workspace scope
- This research stays entirely inside US equities.
- `crypto-linked equities` here means US-listed stocks with material crypto-linked business exposure.
- No spot / futures / on-chain crypto assets are used.
- No `CryptoLeaderRotation` logic is used.
- No `BinancePlatform` code or data path is used.

## Testable hypotheses

### H1. Best stock line may be broad growth / leadership, not technology-only
Research translation:
- Compare broad large-cap eligible / leadership subsets against tech-heavy subsets.
- If tech wins only in a narrow window, do not hard-code it as the default stock line.

### H2. Useful edge may be controlled pullback inside strong trends, not naive dip buying
Research translation:
- Separate moderate pullback + trend intact + recovery confirmation from falling knives.
- Run both `trend_only_control` and `naive_dip_buy_control` as explicit controls.

### H3. Price-first may be better than fake historical valuation overlays
Research translation:
- If the workspace lacks reliable point-in-time valuation/fundamental history, skip valuation in V1.
- Do not pretend current/retro valuation fields are point-in-time clean.

### H4. Crypto-linked equities may be a thematic bucket, not a primary stock universe
Research translation:
- Build a small, explicit, auditable crypto-linked equity list inside the US-equity universe.
- Test whether it has stable standalone alpha or is just a high-beta thematic bucket.

### H5. Human mistakes should become rule-design goals
Human weakness -> rule design target:
- 越跌越买 -> require trend intact and recovery confirmation before ranking a pullback highly.
- 对熟悉赛道过度集中 -> enforce holdings count, single-name cap, sector cap.
- 把好公司和好买点混为一谈 -> separate leader score from entry / pullback score.
- 没有统一退出或降暴露规则 -> keep benchmark + breadth regime and BOXX parking.

## V1 data-policy decision
- Valuation / fundamentals are skipped in V1.
- Reason: current workspace does not expose reliable point-in-time historical valuation/fundamental data.
- This is a deliberate data-quality decision, not a claim that valuation never matters.
"""


@dataclass(frozen=True)
class UniverseSpec:
    name: str
    normalization: str
    min_adv20_usd: float
    leadership_filter: bool = False
    sector_whitelist: tuple[str, ...] = ()
    symbol_whitelist: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class GrowthPullbackConfig:
    name: str
    family: str
    universe_spec: UniverseSpec
    score_template: str
    holdings_count: int
    single_name_cap: float
    sector_cap: float
    regime: suite.RegimeConfig
    exposures: suite.ExposureConfig
    hold_bonus: float = 0.10


@dataclass
class StrategyArtifacts:
    gross_returns: pd.Series
    weights_history: pd.DataFrame
    turnover_history: pd.Series
    selection_history: pd.DataFrame
    rolling_alpha: pd.Series
    turnover_profile: dict[str, float]
    sector_weights: dict[str, float]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--alias-data-run-dir",
        help="Prepared Russell data run with alias repair (defaults to newest official_monthly_v2_alias run)",
    )
    parser.add_argument(
        "--results-dir",
        default=str(DEFAULT_RESULTS_DIR),
        help="Directory where research outputs will be written",
    )
    parser.add_argument(
        "--configs-dir",
        default=str(DEFAULT_CONFIGS_DIR),
        help="Directory where default / aggressive research configs will be written",
    )
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument(
        "--cost-bps",
        nargs="*",
        type=float,
        default=list(DEFAULT_COSTS),
        help="One-way turnover costs in bps",
    )
    return parser.parse_args()


def normalization_label(value: str) -> str:
    mapping = {
        "sector": "sector",
        "universe": "universe_cross_sectional",
    }
    if value not in mapping:
        raise ValueError(f"Unsupported normalization: {value}")
    return mapping[value]


def normalization_from_label(label: str) -> str:
    reverse = {
        "sector": "sector",
        "universe": "universe",
        "universe_cross_sectional": "universe",
    }
    if label not in reverse:
        raise ValueError(f"Unsupported normalization label: {label}")
    return reverse[label]


def spec_to_dict(config: GrowthPullbackConfig, *, role: str) -> dict[str, object]:
    return {
        "role": role,
        "strategy": GROWTH_PULLBACK_NAME,
        "status": "research_only",
        "name": config.name,
        "family": config.family,
        "universe": config.universe_spec.name,
        "normalization": normalization_label(config.universe_spec.normalization),
        "min_adv20_usd": float(config.universe_spec.min_adv20_usd),
        "leadership_filter": bool(config.universe_spec.leadership_filter),
        "sector_whitelist": list(config.universe_spec.sector_whitelist),
        "symbol_whitelist": list(config.universe_spec.symbol_whitelist),
        "notes": config.universe_spec.notes,
        "score_template": config.score_template,
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
        "breadth_thresholds": {"soft": robust.SOFT_BREADTH_THRESHOLD, "hard": robust.HARD_BREADTH_THRESHOLD},
        "residual_proxy": "simple_excess_return_vs_QQQ",
        "cost_assumption_bps_one_way": MAIN_COST_BPS,
    }


def save_spec_config(path: Path, config: GrowthPullbackConfig, *, role: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(spec_to_dict(config, role=role), indent=2, ensure_ascii=False), encoding="utf-8")


def load_spec_config(path: Path) -> GrowthPullbackConfig:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return GrowthPullbackConfig(
        name=str(payload["name"]),
        family=str(payload["family"]),
        universe_spec=UniverseSpec(
            name=str(payload["universe"]),
            normalization=normalization_from_label(str(payload["normalization"])),
            min_adv20_usd=float(payload["min_adv20_usd"]),
            leadership_filter=bool(payload.get("leadership_filter", False)),
            sector_whitelist=tuple(payload.get("sector_whitelist", [])),
            symbol_whitelist=tuple(payload.get("symbol_whitelist", [])),
            notes=str(payload.get("notes", "")),
        ),
        score_template=str(payload["score_template"]),
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
            "growth_pullback_exposures",
            float(payload["exposures"]["soft_defense"]),
            float(payload["exposures"]["hard_defense"]),
        ),
        hold_bonus=float(payload["hold_bonus"]),
    )


def build_context(alias_run_dir: Path, *, start: str | None, end: str | None) -> dict[str, object]:
    universe_history, stock_price_history, prepared_start, prepared_end = suite.discover_prepared_data(alias_run_dir)
    effective_start = pd.Timestamp(start or prepared_start).normalize()
    effective_end = pd.Timestamp(end or prepared_end).normalize()
    etf_frames = suite.download_etf_ohlcv(
        ("QQQ", "SPY", "XLK", "SMH"),
        start=str(effective_start.date()),
        end=str((effective_end + pd.Timedelta(days=1)).date()),
    )
    master_index = suite.build_master_index(stock_price_history, etf_frames["close"])
    master_index = master_index[(master_index >= effective_start) & (master_index <= effective_end)]
    if master_index.empty:
        raise RuntimeError("No common dates remain after start/end filtering")

    extra_prices = suite.build_extra_etf_price_history(etf_frames, symbols=("QQQ", "SPY", "XLK", "SMH"))
    merged_stock_prices = suite.normalize_long_price_history(pd.concat([stock_price_history, extra_prices], ignore_index=True))
    _close_matrix, stock_returns_matrix = suite.build_asset_return_matrix(
        merged_stock_prices,
        master_index=master_index,
        required_symbols=(suite.SAFE_HAVEN, "SPY", "QQQ", "XLK", "SMH"),
    )
    feature_history = precompute_growth_pullback_feature_history(merged_stock_prices)
    rebalance_dates = sorted(suite.build_monthly_rebalance_dates(master_index))
    raw_snapshots = build_growth_raw_snapshots(universe_history, feature_history, rebalance_dates)
    return {
        "data_run_dir": alias_run_dir,
        "universe_history": universe_history,
        "stock_price_history": stock_price_history,
        "merged_stock_prices": merged_stock_prices,
        "master_index": master_index,
        "stock_returns_matrix": stock_returns_matrix,
        "feature_history": feature_history,
        "rebalance_dates": rebalance_dates,
        "raw_snapshots": raw_snapshots,
        "raw_snapshots_cache": {},
        "prepared_start": prepared_start,
        "prepared_end": prepared_end,
        "etf_frames": etf_frames,
    }


def precompute_growth_pullback_feature_history(price_history: pd.DataFrame) -> dict[str, pd.DataFrame]:
    feature_history: dict[str, pd.DataFrame] = {}
    for symbol, group in price_history.groupby("symbol", sort=False):
        history = group.sort_values("as_of").reset_index(drop=True).copy()
        closes = pd.to_numeric(history["close"], errors="coerce")
        volumes = pd.to_numeric(history["volume"], errors="coerce")
        returns = closes.pct_change()
        ma20 = closes.rolling(20).mean()
        ma50 = closes.rolling(50).mean()
        ma200 = closes.rolling(200).mean()
        rolling63max = closes.rolling(63).max()
        rolling126max = closes.rolling(126).max()
        rolling252max = closes.rolling(252).max()
        drawdown_126 = closes / closes.rolling(126).max() - 1.0
        drawdown_126 = drawdown_126.replace([np.inf, -np.inf], np.nan)
        maxdd_126 = drawdown_126.rolling(126).min()
        feature_history[str(symbol).upper()] = pd.DataFrame(
            {
                "as_of": history["as_of"],
                "close": closes,
                "volume": volumes,
                "adv20_usd": (closes * volumes).rolling(20).mean(),
                "history_days": np.arange(1, len(history) + 1, dtype=int),
                "mom_6_1": closes.shift(21) / closes.shift(147) - 1.0,
                "mom_12_1": closes.shift(21) / closes.shift(273) - 1.0,
                "sma20_gap": closes / ma20 - 1.0,
                "sma50_gap": closes / ma50 - 1.0,
                "sma200_gap": closes / ma200 - 1.0,
                "ma50_over_ma200": ma50 / ma200 - 1.0,
                "vol_63": returns.rolling(63).std(ddof=0) * math.sqrt(252),
                "maxdd_126": maxdd_126,
                "breakout_252": closes / rolling252max - 1.0,
                "dist_63_high": closes / rolling63max - 1.0,
                "dist_126_high": closes / rolling126max - 1.0,
                "rebound_20": closes / closes.shift(20) - 1.0,
            }
        )
    return feature_history


FEATURE_COLUMNS = (
    "close",
    "volume",
    "adv20_usd",
    "history_days",
    "mom_6_1",
    "mom_12_1",
    "sma20_gap",
    "sma50_gap",
    "sma200_gap",
    "ma50_over_ma200",
    "vol_63",
    "maxdd_126",
    "breakout_252",
    "dist_63_high",
    "dist_126_high",
    "rebound_20",
)


def empty_feature_row(symbol: str, as_of: pd.Timestamp, sector: str) -> dict[str, object]:
    row = {"as_of": as_of, "symbol": symbol, "sector": sector}
    for column in FEATURE_COLUMNS:
        row[column] = 0 if column == "history_days" else float("nan")
    return row


def lookup_growth_features(
    symbol: str,
    as_of: pd.Timestamp,
    feature_history_by_symbol: Mapping[str, pd.DataFrame],
    *,
    sector: str,
) -> dict[str, object]:
    history = feature_history_by_symbol.get(str(symbol).upper())
    if history is None or history.empty:
        return empty_feature_row(symbol, as_of, sector)
    cutoff = int(history["as_of"].searchsorted(as_of, side="right"))
    if cutoff <= 0:
        return empty_feature_row(symbol, as_of, sector)
    current = history.iloc[cutoff - 1]
    row = {"as_of": as_of, "symbol": symbol, "sector": sector}
    for column in FEATURE_COLUMNS:
        value = current[column]
        if column == "history_days":
            row[column] = int(value) if pd.notna(value) else 0
        else:
            row[column] = float(value) if pd.notna(value) else float("nan")
    return row


def build_growth_raw_snapshots(
    universe_history: pd.DataFrame,
    feature_history_by_symbol: Mapping[str, pd.DataFrame],
    rebalance_dates: Iterable[pd.Timestamp],
) -> dict[pd.Timestamp, pd.DataFrame]:
    snapshots: dict[pd.Timestamp, pd.DataFrame] = {}
    benchmark_symbols = ("SPY", "QQQ", "XLK", "SMH")
    for rebalance_date in sorted(pd.Timestamp(date).normalize() for date in rebalance_dates):
        active_universe = suite.resolve_active_universe(universe_history, rebalance_date)
        sector_map = dict(zip(active_universe["symbol"], active_universe["sector"]))
        symbols = active_universe["symbol"].astype(str).str.upper().tolist()
        for extra in benchmark_symbols:
            if extra not in symbols:
                symbols.append(extra)
        rows = [
            lookup_growth_features(
                symbol,
                rebalance_date,
                feature_history_by_symbol,
                sector=sector_map.get(symbol, "benchmark" if symbol in benchmark_symbols else "unknown"),
            )
            for symbol in symbols
        ]
        frame = pd.DataFrame(rows).sort_values("symbol").reset_index(drop=True)
        frame["base_eligible"] = (
            ~frame["symbol"].isin(benchmark_symbols + (suite.SAFE_HAVEN,))
            & frame["history_days"].ge(252)
            & frame["close"].gt(10.0)
            & frame["adv20_usd"].ge(20_000_000.0)
            & frame[[
                "mom_6_1",
                "mom_12_1",
                "sma20_gap",
                "sma50_gap",
                "sma200_gap",
                "ma50_over_ma200",
                "vol_63",
                "maxdd_126",
                "breakout_252",
                "dist_63_high",
                "dist_126_high",
                "rebound_20",
            ]].notna().all(axis=1)
        )
        snapshots[rebalance_date] = frame
    return snapshots


def build_universe_specs() -> dict[str, UniverseSpec]:
    return {
        "large_cap_eligible": UniverseSpec(
            name="large_cap_eligible",
            normalization="sector",
            min_adv20_usd=20_000_000.0,
            leadership_filter=False,
            notes="Broad large-cap / eligible baseline from IWB proxy universe",
        ),
        "leadership_growth": UniverseSpec(
            name="leadership_growth",
            normalization="sector",
            min_adv20_usd=50_000_000.0,
            leadership_filter=True,
            notes="Broad universe but leadership/growth prefilter; not tech-only",
        ),
        "tech_heavy": UniverseSpec(
            name="tech_heavy",
            normalization="universe",
            min_adv20_usd=50_000_000.0,
            sector_whitelist=TECH_HEAVY_SECTORS,
            leadership_filter=False,
            notes="Sector proxy only: Information Technology + Communication; no sub-industry data available",
        ),
        "crypto_linked_equity_theme": UniverseSpec(
            name="crypto_linked_equity_theme",
            normalization="universe",
            min_adv20_usd=20_000_000.0,
            symbol_whitelist=CRYPTO_THEME_SYMBOLS,
            leadership_filter=False,
            notes="Explicit curated US-equity crypto-linked theme bucket inside the large-cap proxy universe",
        ),
    }


def build_candidate_configs() -> list[GrowthPullbackConfig]:
    universe_specs = build_universe_specs()
    exposures = suite.ExposureConfig("100_60_0", 0.60, 0.00)
    regimes = {
        "qqq_breadth": suite.RegimeConfig("qqq_breadth", "QQQ", "broad"),
        "spy_breadth": suite.RegimeConfig("spy_breadth", "SPY", "broad"),
    }
    setups = (
        ("focused", 12, 0.10, 0.40, 0.05),
        ("balanced", 16, 0.08, 0.30, 0.10),
        ("diversified", 24, 0.06, 0.20, 0.15),
    )
    configs: list[GrowthPullbackConfig] = []

    # broad growth / leadership pullback: test broad and leadership universes, plus balanced/pullback-tilt score templates
    for universe_name in ("large_cap_eligible", "leadership_growth"):
        for score_template in ("balanced_pullback", "pullback_tilt"):
            for setup_name, holdings, single_cap, sector_cap, hold_bonus in setups:
                for regime in regimes.values():
                    configs.append(
                        GrowthPullbackConfig(
                            name=f"broad_{universe_name}_{score_template}_{setup_name}_{regime.name}",
                            family="broad_growth_leadership_pullback",
                            universe_spec=universe_specs[universe_name],
                            score_template=score_template,
                            holdings_count=holdings,
                            single_name_cap=single_cap,
                            sector_cap=sector_cap,
                            regime=regime,
                            exposures=exposures,
                            hold_bonus=hold_bonus,
                        )
                    )

    # tech-heavy pullback
    for setup_name, holdings, single_cap, sector_cap, hold_bonus in setups:
        for regime in regimes.values():
            configs.append(
                GrowthPullbackConfig(
                    name=f"tech_heavy_pullback_balanced_{setup_name}_{regime.name}",
                    family="tech_heavy_pullback",
                    universe_spec=universe_specs["tech_heavy"],
                    score_template="balanced_pullback",
                    holdings_count=holdings,
                    single_name_cap=single_cap,
                    sector_cap=sector_cap,
                    regime=regime,
                    exposures=exposures,
                    hold_bonus=hold_bonus,
                )
            )

    # crypto-linked equity theme pullback
    for setup_name, holdings, single_cap, sector_cap, hold_bonus in setups:
        for regime in regimes.values():
            configs.append(
                GrowthPullbackConfig(
                    name=f"crypto_theme_pullback_balanced_{setup_name}_{regime.name}",
                    family="crypto_equity_theme_pullback",
                    universe_spec=universe_specs["crypto_linked_equity_theme"],
                    score_template="balanced_pullback",
                    holdings_count=holdings,
                    single_name_cap=single_cap,
                    sector_cap=sector_cap,
                    regime=regime,
                    exposures=exposures,
                    hold_bonus=hold_bonus,
                )
            )

    # trend-only control
    for setup_name, holdings, single_cap, sector_cap, hold_bonus in setups:
        for regime in regimes.values():
            configs.append(
                GrowthPullbackConfig(
                    name=f"trend_only_control_{setup_name}_{regime.name}",
                    family="trend_only_control",
                    universe_spec=universe_specs["leadership_growth"],
                    score_template="trend_only",
                    holdings_count=holdings,
                    single_name_cap=single_cap,
                    sector_cap=sector_cap,
                    regime=regime,
                    exposures=exposures,
                    hold_bonus=hold_bonus,
                )
            )

    # naive dip-buy control
    for setup_name, holdings, single_cap, sector_cap, hold_bonus in setups:
        for regime in regimes.values():
            configs.append(
                GrowthPullbackConfig(
                    name=f"naive_dip_buy_control_{setup_name}_{regime.name}",
                    family="naive_dip_buy_control",
                    universe_spec=universe_specs["large_cap_eligible"],
                    score_template="naive_dip_buy",
                    holdings_count=holdings,
                    single_name_cap=single_cap,
                    sector_cap=sector_cap,
                    regime=regime,
                    exposures=exposures,
                    hold_bonus=hold_bonus,
                )
            )
    return configs


def apply_universe_filter(frame: pd.DataFrame, config: GrowthPullbackConfig) -> pd.DataFrame:
    eligible = frame.loc[frame["base_eligible"]].copy()
    eligible = eligible.loc[eligible["adv20_usd"] >= float(config.universe_spec.min_adv20_usd)].copy()
    if eligible.empty:
        return eligible

    if config.universe_spec.sector_whitelist:
        eligible = eligible.loc[eligible["sector"].isin(config.universe_spec.sector_whitelist)].copy()
    if config.universe_spec.symbol_whitelist:
        allowed = {str(symbol).upper() for symbol in config.universe_spec.symbol_whitelist}
        eligible = eligible.loc[eligible["symbol"].isin(allowed)].copy()
    if eligible.empty:
        return eligible

    if config.universe_spec.leadership_filter:
        leadership_proxy = (
            eligible["mom_12_1"] * 0.45
            + eligible["mom_6_1"] * 0.20
            + eligible["breakout_252"] * 0.20
            + eligible["sma200_gap"] * 0.15
        )
        cutoff = float(leadership_proxy.median())
        eligible = eligible.loc[(leadership_proxy >= cutoff) & (eligible["sma200_gap"] > 0)].copy()
    return eligible


def compute_family_features(scored: pd.DataFrame, benchmark_rows: pd.DataFrame) -> pd.DataFrame:
    qqq_row = benchmark_rows.loc[benchmark_rows["symbol"] == "QQQ"]
    if qqq_row.empty:
        raise RuntimeError("QQQ benchmark row missing from snapshot")
    qqq_mom_6_1 = float(qqq_row.iloc[-1]["mom_6_1"])
    qqq_mom_12_1 = float(qqq_row.iloc[-1]["mom_12_1"])

    scored = scored.copy()
    scored["excess_mom_6_1"] = scored["mom_6_1"] - qqq_mom_6_1
    scored["excess_mom_12_1"] = scored["mom_12_1"] - qqq_mom_12_1
    scored["drawdown_abs"] = scored["maxdd_126"].abs()
    scored["pullback_depth_63"] = (-scored["dist_63_high"]).clip(lower=0.0)
    scored["pullback_depth_126"] = (-scored["dist_126_high"]).clip(lower=0.0)
    scored["recent_weakness"] = (-scored["rebound_20"]).clip(lower=0.0)
    scored["trend_strength"] = (
        scored["sma200_gap"] * 0.45
        + scored["breakout_252"] * 0.35
        + scored["ma50_over_ma200"] * 0.20
    )
    scored["controlled_pullback_score"] = (
        -((scored["dist_63_high"] + 0.08).abs() * 0.55)
        -((scored["dist_126_high"] + 0.12).abs() * 0.25)
        -(((-scored["sma50_gap"]).clip(lower=0.0)) * 0.10)
        -(((-scored["sma200_gap"]).clip(lower=0.0)) * 0.10)
    )
    scored["recovery_confirmation"] = (
        scored["sma20_gap"] * 0.40
        + scored["sma50_gap"] * 0.35
        + scored["rebound_20"] * 0.25
    )
    group_median = scored.groupby("sector")["excess_mom_12_1"].transform("median") if scored["sector"].nunique() > 1 else pd.Series(float(scored["excess_mom_12_1"].median()), index=scored.index)
    scored["rel_strength_vs_group"] = scored["excess_mom_12_1"] - group_median
    scored["naive_dip_buy_signal"] = (
        scored["pullback_depth_63"] * 0.40
        + scored["pullback_depth_126"] * 0.30
        + scored["recent_weakness"] * 0.20
        + ((-scored["sma20_gap"]).clip(lower=0.0) * 0.10)
    )
    return scored


def _group_zscore(values: pd.Series, group_keys: pd.Series | None) -> pd.Series:
    if group_keys is None:
        return suite.zscore(values)
    return pd.to_numeric(values, errors="coerce").groupby(group_keys).transform(suite.zscore)


def score_candidates(
    frame: pd.DataFrame,
    current_holdings: set[str],
    config: GrowthPullbackConfig,
) -> pd.DataFrame:
    benchmark_rows = frame.loc[frame["symbol"].isin(["QQQ", "SPY", "XLK", "SMH"])].copy()
    eligible = apply_universe_filter(frame, config)
    if eligible.empty:
        return eligible
    scored = compute_family_features(eligible, benchmark_rows)

    if config.universe_spec.normalization == "sector":
        group_keys = scored["sector"] if scored["sector"].nunique() > 1 else None
    elif config.universe_spec.normalization == "universe":
        group_keys = None
    else:
        raise ValueError(f"Unsupported normalization: {config.universe_spec.normalization}")

    for column in (
        "excess_mom_12_1",
        "excess_mom_6_1",
        "trend_strength",
        "controlled_pullback_score",
        "recovery_confirmation",
        "rel_strength_vs_group",
        "vol_63",
        "drawdown_abs",
        "breakout_252",
        "naive_dip_buy_signal",
        "pullback_depth_63",
        "pullback_depth_126",
        "recent_weakness",
    ):
        scored[f"z_{column}"] = _group_zscore(scored[column], group_keys)

    if config.score_template == "balanced_pullback":
        scored["score"] = (
            scored["z_excess_mom_12_1"] * 0.25
            + scored["z_excess_mom_6_1"] * 0.20
            + scored["z_trend_strength"] * 0.15
            + scored["z_controlled_pullback_score"] * 0.15
            + scored["z_recovery_confirmation"] * 0.10
            + scored["z_rel_strength_vs_group"] * 0.10
            - scored["z_vol_63"] * 0.03
            - scored["z_drawdown_abs"] * 0.02
        )
    elif config.score_template == "pullback_tilt":
        scored["score"] = (
            scored["z_excess_mom_12_1"] * 0.18
            + scored["z_excess_mom_6_1"] * 0.12
            + scored["z_trend_strength"] * 0.12
            + scored["z_controlled_pullback_score"] * 0.25
            + scored["z_recovery_confirmation"] * 0.18
            + scored["z_rel_strength_vs_group"] * 0.08
            - scored["z_vol_63"] * 0.04
            - scored["z_drawdown_abs"] * 0.03
        )
    elif config.score_template == "trend_only":
        scored["score"] = (
            scored["z_excess_mom_12_1"] * 0.32
            + scored["z_excess_mom_6_1"] * 0.22
            + scored["z_trend_strength"] * 0.18
            + scored["z_breakout_252"] * 0.10
            + scored["z_rel_strength_vs_group"] * 0.10
            + scored["z_recovery_confirmation"] * 0.05
            - scored["z_vol_63"] * 0.03
            - scored["z_drawdown_abs"] * 0.02
        )
    elif config.score_template == "naive_dip_buy":
        scored["score"] = (
            scored["z_naive_dip_buy_signal"] * 0.35
            + scored["z_pullback_depth_126"] * 0.20
            + scored["z_recent_weakness"] * 0.15
            + scored["z_pullback_depth_63"] * 0.10
            + scored["z_excess_mom_12_1"] * 0.05
            + scored["z_rel_strength_vs_group"] * 0.05
            - scored["z_vol_63"] * 0.05
            - scored["z_drawdown_abs"] * 0.05
        )
    else:
        raise ValueError(f"Unsupported score_template: {config.score_template}")

    if current_holdings:
        scored.loc[scored["symbol"].isin(current_holdings), "score"] += float(config.hold_bonus)
    return scored


def select_breadth_ratio(frame: pd.DataFrame, eligible: pd.DataFrame, config: GrowthPullbackConfig) -> float:
    if config.regime.breadth_mode == "broad":
        return float((eligible["sma200_gap"] > 0).mean()) if not eligible.empty else 0.0
    if config.regime.breadth_mode == "sector_etf":
        breadth_rows = frame.loc[frame["symbol"].isin(config.regime.breadth_symbols)].copy()
        return float((breadth_rows["sma200_gap"] > 0).mean()) if not breadth_rows.empty else 0.0
    raise ValueError(f"Unsupported breadth_mode: {config.regime.breadth_mode}")


def build_target_weights(
    raw_snapshot: pd.DataFrame,
    current_holdings: set[str],
    config: GrowthPullbackConfig,
) -> tuple[dict[str, float], dict[str, object]]:
    frame = raw_snapshot.copy()
    benchmark_symbol = str(config.regime.benchmark_symbol).upper()
    benchmark_rows = frame.loc[frame["symbol"] == benchmark_symbol]
    benchmark_trend_positive = bool(
        (not benchmark_rows.empty)
        and pd.notna(benchmark_rows.iloc[-1]["sma200_gap"])
        and float(benchmark_rows.iloc[-1]["sma200_gap"]) > 0
    )

    eligible_for_breadth = apply_universe_filter(frame, config)
    breadth_ratio = select_breadth_ratio(frame, eligible_for_breadth, config)
    if (not benchmark_trend_positive) and breadth_ratio < robust.HARD_BREADTH_THRESHOLD:
        regime = "hard_defense"
        stock_exposure = float(config.exposures.hard_defense_exposure)
    elif (not benchmark_trend_positive) or breadth_ratio < robust.SOFT_BREADTH_THRESHOLD:
        regime = "soft_defense"
        stock_exposure = float(config.exposures.soft_defense_exposure)
    else:
        regime = "risk_on"
        stock_exposure = 1.0

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
            },
        )

    scored = score_candidates(frame, current_holdings, config)
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
            },
        )

    per_name_weight = min(float(config.single_name_cap), stock_exposure / len(selected))
    invested_weight = per_name_weight * len(selected)
    weights = {row.symbol: per_name_weight for row in selected.itertuples(index=False)}
    if invested_weight < 1.0:
        weights[suite.SAFE_HAVEN] = 1.0 - invested_weight
    metadata = {
        "regime": regime,
        "stock_exposure": stock_exposure,
        "breadth_ratio": breadth_ratio,
        "benchmark_symbol": benchmark_symbol,
        "benchmark_trend_positive": benchmark_trend_positive,
        "candidate_count": int(len(scored)),
        "selected_symbols": tuple(selected["symbol"].tolist()),
        "selected_sectors": tuple(selected["sector"].tolist()),
        "sector_slot_cap": sector_slot_cap,
    }
    return weights, metadata


def run_growth_pullback_backtest(
    raw_snapshots: Mapping[pd.Timestamp, pd.DataFrame],
    returns_matrix: pd.DataFrame,
    config: GrowthPullbackConfig,
) -> dict[str, object]:
    index = returns_matrix.index
    rebalance_dates = set(raw_snapshots)
    weights_history = pd.DataFrame(0.0, index=index, columns=sorted(set(returns_matrix.columns) | {suite.SAFE_HAVEN}))
    portfolio_returns = pd.Series(0.0, index=index, name=config.name)
    turnover_history = pd.Series(0.0, index=index, name="turnover")
    selection_rows: list[dict[str, object]] = []
    current_weights: dict[str, float] = {suite.SAFE_HAVEN: 1.0}
    current_holdings: set[str] = set()

    for idx in range(len(index) - 1):
        date = index[idx]
        next_date = index[idx + 1]
        if date in rebalance_dates:
            target_weights, metadata = build_target_weights(raw_snapshots[date], current_holdings, config)
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
                    "family": config.family,
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

    selection_history = pd.DataFrame(selection_rows)
    return {
        "gross_returns": portfolio_returns,
        "weights_history": weights_history.loc[:, (weights_history != 0.0).any(axis=0)],
        "turnover_history": turnover_history,
        "selection_history": selection_history,
    }


def config_fields(config: GrowthPullbackConfig) -> dict[str, object]:
    return {
        "family": config.family,
        "universe": config.universe_spec.name,
        "normalization": config.universe_spec.normalization,
        "normalization_label": normalization_label(config.universe_spec.normalization),
        "min_adv20_usd": float(config.universe_spec.min_adv20_usd),
        "leadership_filter": bool(config.universe_spec.leadership_filter),
        "sector_whitelist": json.dumps(list(config.universe_spec.sector_whitelist), ensure_ascii=False),
        "symbol_whitelist": json.dumps(list(config.universe_spec.symbol_whitelist), ensure_ascii=False),
        "score_template": config.score_template,
        "holdings_count": int(config.holdings_count),
        "single_name_cap": float(config.single_name_cap),
        "sector_cap": float(config.sector_cap),
        "hold_bonus": float(config.hold_bonus),
        "regime_name": config.regime.name,
        "benchmark_symbol": config.regime.benchmark_symbol,
        "breadth_mode": config.regime.breadth_mode,
        "breadth_symbols": json.dumps(list(config.regime.breadth_symbols), ensure_ascii=False),
        "soft_defense_exposure": float(config.exposures.soft_defense_exposure),
        "hard_defense_exposure": float(config.exposures.hard_defense_exposure),
        "notes": config.universe_spec.notes,
    }


def evaluate_candidate_rows(
    config: GrowthPullbackConfig,
    context: dict[str, object],
    benchmark_returns: pd.Series,
    *,
    cost_bps_values: Iterable[float],
) -> tuple[list[dict[str, object]], dict[float, StrategyArtifacts]]:
    result = run_growth_pullback_backtest(context["raw_snapshots"], context["stock_returns_matrix"], config)
    rows: list[dict[str, object]] = []
    artifacts_by_cost: dict[float, StrategyArtifacts] = {}
    for cost_bps in cost_bps_values:
        net_returns = result["gross_returns"] - result["turnover_history"].reindex(result["gross_returns"].index).fillna(0.0) * (float(cost_bps) / 10_000.0)
        rolling_alpha = robust.compute_rolling_capm_alpha_fast(net_returns, benchmark_returns)
        turnover_profile = robust.compute_turnover_profile(result["selection_history"], result["turnover_history"])
        sector_weights = robust.compute_average_sector_weights(result["weights_history"], result["selection_history"], context["universe_history"])
        artifacts_by_cost[float(cost_bps)] = StrategyArtifacts(
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
                    "strategy": config.name,
                    "cost_bps_one_way": float(cost_bps),
                    "period": period_name,
                    **config_fields(config),
                    **metrics,
                    **robust.compute_rolling_alpha_summary(rolling_series),
                    **turnover_profile,
                    "avg_sector_weights_json": json.dumps({k: float(v) for k, v in sector_weights.items()}, ensure_ascii=False),
                }
            )
    return rows, artifacts_by_cost


def build_reference_rows(context: dict[str, object], cost_bps_values: Iterable[float]) -> tuple[pd.DataFrame, dict[str, dict[float, object]]]:
    benchmark_returns = context["stock_returns_matrix"]["QQQ"].copy()
    qqq_default = v11.load_spec_config(DEFAULT_CONFIGS_DIR / "qqq_plus_stock_alpha_v1_1_default.json")
    qqq_aggressive = v11.load_spec_config(DEFAULT_CONFIGS_DIR / "qqq_plus_stock_alpha_v1_1_aggressive.json")
    default_rows, default_artifacts = v11.evaluate_final_strategy_rows("qqq_plus_current_default", qqq_default, context, benchmark_returns, cost_bps_values=cost_bps_values)
    aggressive_rows, aggressive_artifacts = v11.evaluate_final_strategy_rows("aggressive_alt_spec", qqq_aggressive, context, benchmark_returns, cost_bps_values=cost_bps_values)
    defensive_rows, defensive_artifacts = v11.evaluate_defensive_rows(context, cost_bps_values=cost_bps_values)
    qqq_rows = v11.evaluate_qqq_rows(benchmark_returns, cost_bps_values=cost_bps_values)
    rows = pd.DataFrame(default_rows + aggressive_rows + defensive_rows + qqq_rows)
    return rows, {
        "qqq_plus_current_default": default_artifacts,
        "aggressive_alt_spec": aggressive_artifacts,
        "defensive_baseline": defensive_artifacts,
    }


def summarize_candidates(ablations_df: pd.DataFrame, qqq_reference_df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    qqq_oos_cagr = float(qqq_reference_df.loc[(qqq_reference_df["period"] == "OOS Sample") & (qqq_reference_df["cost_bps_one_way"] == MAIN_COST_BPS), "CAGR"].iloc[0])
    candidate_rows = ablations_df.loc[ablations_df["cost_bps_one_way"] == MAIN_COST_BPS].copy()
    for strategy_name, group in candidate_rows.groupby("strategy", sort=False):
        full_row = group.loc[group["period"] == "Full Sample"].iloc[0]
        oos_row = group.loc[group["period"] == "OOS Sample"].iloc[0]
        row_2022 = group.loc[group["period"] == "2022"].iloc[0]
        row_2023 = group.loc[group["period"] == "2023+"].iloc[0]
        rows.append(
            {
                **config_fields(
                    GrowthPullbackConfig(
                        name=strategy_name,
                        family=str(full_row["family"]),
                        universe_spec=UniverseSpec(
                            name=str(full_row["universe"]),
                            normalization=str(full_row["normalization"]),
                            min_adv20_usd=float(full_row["min_adv20_usd"]),
                            leadership_filter=bool(full_row["leadership_filter"]),
                            sector_whitelist=tuple(json.loads(full_row["sector_whitelist"])),
                            symbol_whitelist=tuple(json.loads(full_row["symbol_whitelist"])),
                            notes=str(full_row["notes"]),
                        ),
                        score_template=str(full_row["score_template"]),
                        holdings_count=int(full_row["holdings_count"]),
                        single_name_cap=float(full_row["single_name_cap"]),
                        sector_cap=float(full_row["sector_cap"]),
                        regime=suite.RegimeConfig(str(full_row["regime_name"]), str(full_row["benchmark_symbol"]), str(full_row["breadth_mode"]), tuple(json.loads(full_row["breadth_symbols"]))),
                        exposures=suite.ExposureConfig("100_60_0", float(full_row["soft_defense_exposure"]), float(full_row["hard_defense_exposure"])),
                        hold_bonus=float(full_row["hold_bonus"]),
                    )
                ),
                "strategy": strategy_name,
                "full_cagr": float(full_row["CAGR"]),
                "full_information_ratio_vs_qqq": float(full_row["Information Ratio vs QQQ"]),
                "full_alpha_ann_vs_qqq": float(full_row["alpha_ann_vs_qqq"]),
                "oos_cagr": float(oos_row["CAGR"]),
                "oos_cagr_minus_qqq": float(oos_row["CAGR"] - qqq_oos_cagr),
                "oos_alpha_ann_vs_qqq": float(oos_row["alpha_ann_vs_qqq"]),
                "oos_max_drawdown": float(oos_row["Max Drawdown"]),
                "oos_rolling_alpha_positive_ratio": float(oos_row["rolling_36m_alpha_positive_ratio"]),
                "return_2022": float(row_2022["Total Return"]),
                "cagr_2023_plus": float(row_2023["CAGR"]),
                "annual_turnover": float(full_row["annual_turnover"]),
                "average_names_held": float(full_row["Average Names Held"]),
            }
        )
    return pd.DataFrame(rows)


def percentile_rank(series: pd.Series, *, higher_is_better: bool) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    ranked = numeric.rank(method="average", pct=True) if higher_is_better else (-numeric).rank(method="average", pct=True)
    return ranked.fillna(0.0)


def add_selection_scores(summary_df: pd.DataFrame) -> pd.DataFrame:
    scored = summary_df.copy()
    scored["score_oos_cagr_minus_qqq"] = percentile_rank(scored["oos_cagr_minus_qqq"], higher_is_better=True)
    scored["score_oos_alpha_ratio"] = percentile_rank(scored["oos_rolling_alpha_positive_ratio"], higher_is_better=True)
    scored["score_oos_maxdd"] = percentile_rank(scored["oos_max_drawdown"], higher_is_better=True)
    scored["score_2022"] = percentile_rank(scored["return_2022"], higher_is_better=True)
    scored["score_2023_plus"] = percentile_rank(scored["cagr_2023_plus"], higher_is_better=True)
    scored["score_full_ir"] = percentile_rank(scored["full_information_ratio_vs_qqq"], higher_is_better=True)
    scored["score_turnover"] = percentile_rank(scored["annual_turnover"], higher_is_better=False)
    scored["score_names_held"] = percentile_rank(scored["average_names_held"], higher_is_better=True)
    complexity_adj = {
        "broad_growth_leadership_pullback": 0.03,
        "trend_only_control": 0.01,
        "tech_heavy_pullback": 0.00,
        "crypto_equity_theme_pullback": -0.02,
        "naive_dip_buy_control": -0.05,
    }
    scored["complexity_adjustment"] = scored["family"].map(complexity_adj).fillna(0.0)
    scored["robustness_score"] = (
        scored["score_oos_cagr_minus_qqq"] * 0.25
        + scored["score_oos_alpha_ratio"] * 0.20
        + scored["score_oos_maxdd"] * 0.15
        + scored["score_2022"] * 0.10
        + scored["score_2023_plus"] * 0.10
        + scored["score_full_ir"] * 0.10
        + scored["score_turnover"] * 0.05
        + scored["score_names_held"] * 0.05
        + scored["complexity_adjustment"]
    )
    scored["full_cagr_rank"] = scored["full_cagr"].rank(method="dense", ascending=False)
    scored["robustness_rank"] = scored["robustness_score"].rank(method="dense", ascending=False)
    scored["core_viable"] = (
        (scored["family"] != "naive_dip_buy_control")
        & (scored["oos_cagr_minus_qqq"] > 0)
        & (scored["oos_rolling_alpha_positive_ratio"] >= 0.75)
        & (scored["oos_max_drawdown"] >= -0.45)
        & (scored["average_names_held"] >= 6)
    )
    return scored.sort_values(
        by=["robustness_score", "oos_cagr_minus_qqq", "full_information_ratio_vs_qqq", "full_cagr"],
        ascending=[False, False, False, False],
    ).reset_index(drop=True)


def pick_specs(selection_df: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.DataFrame]:
    ranked = selection_df.copy()
    default_pool = ranked.loc[ranked["core_viable"]].copy()
    if default_pool.empty:
        default_pool = ranked.loc[ranked["family"] != "naive_dip_buy_control"].copy()
    default_row = default_pool.iloc[0]

    aggressive_pool = ranked.loc[(ranked["family"] != "naive_dip_buy_control") & (ranked["strategy"] != default_row["strategy"])].copy()
    if aggressive_pool.empty:
        aggressive_pool = ranked.loc[ranked["strategy"] != default_row["strategy"]].copy()
    aggressive_row = aggressive_pool.sort_values(
        by=["full_cagr", "oos_cagr_minus_qqq", "cagr_2023_plus", "robustness_score"],
        ascending=[False, False, False, False],
    ).iloc[0]
    return default_row, aggressive_row, ranked


def build_family_best_table(selection_df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for family, group in selection_df.groupby("family", sort=False):
        top = group.sort_values(by=["robustness_score", "oos_cagr_minus_qqq", "full_cagr"], ascending=[False, False, False]).iloc[0]
        rows.append(top.to_dict())
    return pd.DataFrame(rows).sort_values(by=["robustness_score", "oos_cagr_minus_qqq"], ascending=[False, False]).reset_index(drop=True)


def build_comparison_table(
    family_best_df: pd.DataFrame,
    candidate_rows_df: pd.DataFrame,
    reference_rows_df: pd.DataFrame,
) -> pd.DataFrame:
    chosen = set(family_best_df["strategy"].tolist())
    candidate_table = candidate_rows_df.loc[(candidate_rows_df["strategy"].isin(chosen)) & (candidate_rows_df["cost_bps_one_way"] == MAIN_COST_BPS)].copy()
    reference_table = reference_rows_df.loc[
        (reference_rows_df["cost_bps_one_way"] == MAIN_COST_BPS)
        & (reference_rows_df["strategy"].isin(["qqq_plus_current_default", "aggressive_alt_spec", "defensive_baseline", "QQQ"]))
    ].copy()
    return pd.concat([candidate_table, reference_table], ignore_index=True)


def build_recommendation(
    *,
    family_best_df: pd.DataFrame,
    default_row: pd.Series,
    aggressive_row: pd.Series,
) -> dict[str, object]:
    broad_best = family_best_df.loc[family_best_df["family"] == "broad_growth_leadership_pullback"].iloc[0] if (family_best_df["family"] == "broad_growth_leadership_pullback").any() else None
    tech_best = family_best_df.loc[family_best_df["family"] == "tech_heavy_pullback"].iloc[0] if (family_best_df["family"] == "tech_heavy_pullback").any() else None
    crypto_best = family_best_df.loc[family_best_df["family"] == "crypto_equity_theme_pullback"].iloc[0] if (family_best_df["family"] == "crypto_equity_theme_pullback").any() else None

    if default_row["family"] == "broad_growth_leadership_pullback":
        main_line = "broad_growth_leadership_pullback"
    elif default_row["family"] == "tech_heavy_pullback":
        main_line = "tech_heavy_pullback"
    else:
        main_line = str(default_row["family"])

    crypto_decision = "only_as_secondary_thematic_bucket"
    if crypto_best is not None and bool(crypto_best["core_viable"]) and float(crypto_best["robustness_score"]) >= float(default_row["robustness_score"]) - 0.03:
        crypto_decision = "yes"
    if crypto_best is not None and (float(crypto_best["average_names_held"]) < 6 or float(crypto_best["oos_max_drawdown"]) < -0.45):
        crypto_decision = "only_as_secondary_thematic_bucket"
    if crypto_best is not None and (float(crypto_best["oos_cagr_minus_qqq"]) <= 0 or float(crypto_best["robustness_score"]) < 0.45):
        crypto_decision = "no"

    return {
        "strategy_family": GROWTH_PULLBACK_NAME,
        "default_research_spec": {
            "name": str(default_row["strategy"]),
            "family": str(default_row["family"]),
            "robustness_score": float(default_row["robustness_score"]),
            "full_cagr": float(default_row["full_cagr"]),
            "oos_cagr": float(default_row["oos_cagr"]),
            "oos_cagr_minus_qqq": float(default_row["oos_cagr_minus_qqq"]),
            "return_2022": float(default_row["return_2022"]),
            "cagr_2023_plus": float(default_row["cagr_2023_plus"]),
        },
        "aggressive_research_spec": {
            "name": str(aggressive_row["strategy"]),
            "family": str(aggressive_row["family"]),
            "robustness_score": float(aggressive_row["robustness_score"]),
            "full_cagr": float(aggressive_row["full_cagr"]),
            "oos_cagr": float(aggressive_row["oos_cagr"]),
            "oos_cagr_minus_qqq": float(aggressive_row["oos_cagr_minus_qqq"]),
        },
        "answers": {
            "A_user_preference_kernel": {
                "best_kept_core": "controlled_pullback_inside_strength" if default_row["family"] != "trend_only_control" else "broad_leadership_trend",
                "broad_growth_vs_tech": "broad_growth_or_leadership" if (broad_best is not None and tech_best is not None and float(broad_best["robustness_score"]) >= float(tech_best["robustness_score"])) else "tech_heavy",
                "crypto_theme": crypto_decision,
            },
            "B_remove_from_rules": [
                "越跌越买",
                "对熟悉赛道过度集中",
                "把好公司和好买点混为一谈",
                "没有统一的退出/降暴露规则",
            ],
            "C_stock_line_to_continue": main_line,
            "D_crypto_linked_equity_theme": crypto_decision,
            "E_next_main_direction": "continue_stock_line_spec_lock",
        },
        "family_best_summary": family_best_df[[
            "family", "strategy", "robustness_score", "full_cagr", "oos_cagr", "oos_cagr_minus_qqq", "return_2022", "cagr_2023_plus"
        ]].to_dict(orient="records"),
        "valuation_overlay_v1": {
            "included": False,
            "reason": "workspace lacks reliable point-in-time historical valuation/fundamental data",
        },
    }


def write_hypotheses_note(path: Path) -> None:
    path.write_text(HYPOTHESES_NOTE, encoding="utf-8")


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


def write_summary_markdown(
    path: Path,
    *,
    workspace_mapping: dict[str, object],
    family_best_df: pd.DataFrame,
    comparison_df: pd.DataFrame,
    recommendation: dict[str, object],
    crypto_theme_df: pd.DataFrame,
) -> None:
    lines = [
        "# growth_pullback_systematic_v1 summary",
        "",
        "## Workspace mapping",
        f"- qqq_plus suite: {workspace_mapping['qqq_plus_suite']}",
        f"- qqq_plus default config: {workspace_mapping['qqq_plus_default_config']}",
        f"- qqq_plus aggressive config: {workspace_mapping['qqq_plus_aggressive_config']}",
        f"- defensive strategy entry: {workspace_mapping['defensive_strategy_entry']}",
        f"- defensive backtest entry: {workspace_mapping['defensive_backtest_entry']}",
        f"- new research entry: {workspace_mapping['new_research_entry']}",
        f"- avoided repos: {', '.join(workspace_mapping['avoided_repos'])}",
        "",
        "## Family best table (5 bps)",
        format_table(family_best_df[[
            'family', 'strategy', 'robustness_score', 'full_cagr', 'oos_cagr', 'oos_cagr_minus_qqq', 'return_2022', 'cagr_2023_plus'
        ]]),
        "",
        "## Final comparison (5 bps)",
        format_table(comparison_df[[
            'strategy', 'period', 'family', 'CAGR', 'Total Return', 'Max Drawdown', 'Sharpe', 'Alpha_ann_vs_QQQ', 'Information Ratio vs QQQ', 'Turnover/Year'
        ]]),
        "",
        "## Crypto-linked theme list",
        format_table(crypto_theme_df[['symbol', 'theme_bucket', 'reason', 'present_in_iwb_proxy', 'first_seen_start_date']]),
        "",
        "## Recommendation",
        f"- default_research_spec={recommendation['default_research_spec']['name']}",
        f"- aggressive_research_spec={recommendation['aggressive_research_spec']['name']}",
        f"- stock_line_to_continue={recommendation['answers']['C_stock_line_to_continue']}",
        f"- crypto_linked_equity_theme={recommendation['answers']['D_crypto_linked_equity_theme']}",
        f"- next_main_direction={recommendation['answers']['E_next_main_direction']}",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def build_workspace_mapping() -> dict[str, object]:
    return {
        "qqq_plus_suite": str(SCRIPT_DIR / "backtest_stock_alpha_suite.py"),
        "qqq_plus_default_config": str(DEFAULT_CONFIGS_DIR / "qqq_plus_stock_alpha_v1_1_default.json"),
        "qqq_plus_aggressive_config": str(DEFAULT_CONFIGS_DIR / "qqq_plus_stock_alpha_v1_1_aggressive.json"),
        "defensive_strategy_entry": str(suite.US_EQUITY_STRATEGIES_ROOT / "src/us_equity_strategies/strategies/russell_1000_multi_factor_defensive.py"),
        "defensive_backtest_entry": str(suite.US_EQUITY_STRATEGIES_ROOT / "src/us_equity_strategies/backtests/russell_1000_multi_factor_defensive.py"),
        "interactivebrokers_research_dir": str(SCRIPT_DIR),
        "us_equity_strategies_read_only_reference": str(suite.US_EQUITY_STRATEGIES_ROOT / "src/us_equity_strategies"),
        "quant_platform_kit_shared_read_only": str(suite.QUANT_PLATFORM_KIT_ROOT / "src/quant_platform_kit"),
        "new_research_entry": str(SCRIPT_DIR / "backtest_growth_pullback_suite.py"),
        "avoided_repos": [
            "/Users/lisiyi/Projects/BinancePlatform",
            "/Users/lisiyi/Projects/CryptoLeaderRotation",
        ],
    }


def build_crypto_theme_audit(universe_history: pd.DataFrame) -> pd.DataFrame:
    rows = []
    symbols = universe_history["symbol"].astype(str).str.upper()
    for item in CRYPTO_THEME_ROWS:
        symbol = item["symbol"]
        history = universe_history.loc[symbols == symbol].copy()
        first_seen = str(pd.to_datetime(history["start_date"]).min().date()) if not history.empty else None
        rows.append({
            **item,
            "present_in_iwb_proxy": bool(not history.empty),
            "first_seen_start_date": first_seen,
        })
    return pd.DataFrame(rows)


def main() -> None:
    args = parse_args()
    results_dir = Path(args.results_dir).expanduser().resolve()
    configs_dir = Path(args.configs_dir).expanduser().resolve()
    results_dir.mkdir(parents=True, exist_ok=True)
    configs_dir.mkdir(parents=True, exist_ok=True)

    alias_dir = robust.discover_run_dir(args.alias_data_run_dir, "r1000_multifactor_defensive_*_official_monthly_v2_alias")
    context = build_context(alias_dir, start=args.start, end=args.end)
    benchmark_returns = context["stock_returns_matrix"]["QQQ"].copy()

    candidate_rows = []
    candidate_artifacts: dict[str, dict[float, StrategyArtifacts]] = {}
    for config in build_candidate_configs():
        rows, artifacts = evaluate_candidate_rows(config, context, benchmark_returns, cost_bps_values=args.cost_bps)
        candidate_rows.extend(rows)
        candidate_artifacts[config.name] = artifacts
    candidate_rows_df = pd.DataFrame(candidate_rows)
    candidate_rows_df["Alpha_ann_vs_QQQ"] = candidate_rows_df["alpha_ann_vs_qqq"]

    reference_rows_df, _reference_artifacts = build_reference_rows(context, args.cost_bps)
    reference_rows_df["family"] = reference_rows_df.get("family", pd.Series(index=reference_rows_df.index, dtype=object)).fillna("reference")
    reference_rows_df["Alpha_ann_vs_QQQ"] = reference_rows_df.get("alpha_ann_vs_qqq", pd.Series(index=reference_rows_df.index, dtype=float))

    qqq_reference_df = reference_rows_df.loc[(reference_rows_df["strategy"] == "QQQ") & (reference_rows_df["cost_bps_one_way"] == MAIN_COST_BPS)].copy()
    selection_df = summarize_candidates(candidate_rows_df, qqq_reference_df)
    selection_df = add_selection_scores(selection_df)
    default_row, aggressive_row, ranked_selection = pick_specs(selection_df)
    family_best_df = build_family_best_table(ranked_selection)
    comparison_df = build_comparison_table(family_best_df, candidate_rows_df, reference_rows_df)
    crypto_theme_df = build_crypto_theme_audit(context["universe_history"])
    recommendation = build_recommendation(family_best_df=family_best_df, default_row=default_row, aggressive_row=aggressive_row)

    # export configs for selected specs
    selected_configs = {config.name: config for config in build_candidate_configs()}
    save_spec_config(configs_dir / "growth_pullback_systematic_v1_default.json", selected_configs[str(default_row["strategy"])], role="default_research_spec")
    save_spec_config(configs_dir / "growth_pullback_systematic_v1_aggressive.json", selected_configs[str(aggressive_row["strategy"])], role="aggressive_research_spec")

    write_hypotheses_note(results_dir / "growth_pullback_hypotheses.md")
    candidate_rows_df.to_csv(results_dir / "growth_pullback_ablations.csv", index=False)
    comparison_df.to_csv(results_dir / "growth_pullback_equity_comparison.csv", index=False)
    ranked_selection.to_csv(results_dir / "growth_pullback_selection_score.csv", index=False)
    crypto_theme_df.to_csv(results_dir / "growth_pullback_crypto_theme_constituents.csv", index=False)
    (results_dir / "growth_pullback_recommendation.json").write_text(json.dumps(recommendation, indent=2, ensure_ascii=False), encoding="utf-8")
    workspace_mapping = build_workspace_mapping()
    (results_dir / "growth_pullback_workspace_mapping.json").write_text(json.dumps(workspace_mapping, indent=2, ensure_ascii=False), encoding="utf-8")
    write_summary_markdown(
        results_dir / "growth_pullback_summary.md",
        workspace_mapping=workspace_mapping,
        family_best_df=family_best_df,
        comparison_df=comparison_df,
        recommendation=recommendation,
        crypto_theme_df=crypto_theme_df,
    )

    print(f"alias data: {alias_dir}")
    print(f"default research spec: {default_row['strategy']}")
    print(f"aggressive research spec: {aggressive_row['strategy']}")
    print(f"results written to: {results_dir}")


if __name__ == "__main__":
    main()
