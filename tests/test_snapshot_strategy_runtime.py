import pytest
from pathlib import Path
import hashlib
import json
from types import SimpleNamespace

import strategy_runtime as strategy_runtime_module


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _write_cash_buffer_manifest(snapshot_path: Path, config_path: Path, *, snapshot_as_of: str) -> Path:
    manifest_path = Path(f"{snapshot_path}.manifest.json")
    manifest_path.write_text(
        json.dumps(
            {
                "manifest_type": "feature_snapshot",
                "contract_version": "tech_communication_pullback_enhancement.feature_snapshot.v1",
                "strategy_profile": "tech_communication_pullback_enhancement",
                "config_name": "tech_communication_pullback_enhancement",
                "config_path": str(config_path),
                "config_sha256": _sha256_file(config_path),
                "snapshot_path": str(snapshot_path),
                "snapshot_sha256": _sha256_file(snapshot_path),
                "snapshot_as_of": snapshot_as_of,
            }
        ),
        encoding="utf-8",
    )
    return manifest_path


def _russell_top50_snapshot_rows() -> list[dict[str, object]]:
    base = [
        {
            "as_of": "2026-03-31",
            "symbol": "QQQ",
            "sector": "benchmark",
            "close": 500.0,
            "adv20_usd": 1_000_000_000.0,
            "history_days": 400,
            "mom_3m": 0.12,
            "mom_6m": 0.20,
            "mom_12_1": 0.30,
            "rel_mom_6m_vs_benchmark": 0.0,
            "rel_mom_6m_vs_broad_benchmark": 0.05,
            "high_252_gap": -0.01,
            "sma200_gap": 0.08,
            "vol_63": 0.20,
            "maxdd_126": -0.10,
            "eligible": False,
        },
        {
            "as_of": "2026-03-31",
            "symbol": "SPY",
            "sector": "benchmark",
            "close": 450.0,
            "adv20_usd": 1_000_000_000.0,
            "history_days": 400,
            "mom_3m": 0.08,
            "mom_6m": 0.15,
            "mom_12_1": 0.22,
            "rel_mom_6m_vs_benchmark": -0.05,
            "rel_mom_6m_vs_broad_benchmark": 0.0,
            "high_252_gap": -0.02,
            "sma200_gap": 0.05,
            "vol_63": 0.16,
            "maxdd_126": -0.08,
            "eligible": False,
        },
        {
            "as_of": "2026-03-31",
            "symbol": "BOXX",
            "sector": "cash",
            "close": 101.0,
            "adv20_usd": 30_000_000.0,
            "history_days": 400,
            "mom_3m": 0.01,
            "mom_6m": 0.02,
            "mom_12_1": 0.04,
            "rel_mom_6m_vs_benchmark": -0.18,
            "rel_mom_6m_vs_broad_benchmark": -0.13,
            "high_252_gap": 0.0,
            "sma200_gap": 0.01,
            "vol_63": 0.03,
            "maxdd_126": -0.01,
            "eligible": False,
        },
    ]
    leaders = (
        ("NVDA", "Information Technology", 0.30, 0.55, 0.45, 0.10, 0.15, -0.01, 0.35, -0.08),
        ("META", "Communication Services", 0.22, 0.38, 0.30, 0.02, 0.09, -0.03, 0.24, -0.09),
        ("MSFT", "Information Technology", 0.18, 0.32, 0.25, -0.01, 0.08, -0.02, 0.20, -0.07),
        ("AAPL", "Information Technology", 0.15, 0.28, 0.21, -0.03, 0.06, -0.04, 0.18, -0.10),
        ("AMZN", "Consumer Discretionary", 0.14, 0.26, 0.19, -0.04, 0.05, -0.06, 0.22, -0.11),
    )
    for symbol, sector, mom3, mom6, mom12, rel_qqq, rel_spy, high_gap, vol, maxdd in leaders:
        base.append(
            {
                "as_of": "2026-03-31",
                "symbol": symbol,
                "sector": sector,
                "close": 100.0,
                "adv20_usd": 100_000_000.0,
                "history_days": 400,
                "mom_3m": mom3,
                "mom_6m": mom6,
                "mom_12_1": mom12,
                "rel_mom_6m_vs_benchmark": rel_qqq,
                "rel_mom_6m_vs_broad_benchmark": rel_spy,
                "high_252_gap": high_gap,
                "sma200_gap": 0.08,
                "vol_63": vol,
                "maxdd_126": maxdd,
                "eligible": True,
            }
        )
    return base


def test_compute_signals_uses_feature_snapshot_for_russell_top50(strategy_module_factory, monkeypatch):
    pytest.importorskip("pandas")

    module = strategy_module_factory(
        STRATEGY_PROFILE="russell_top50_leader_rotation_aggressive",
        IBKR_FEATURE_SNAPSHOT_PATH="/tmp/russell-top50.csv",
        IBKR_FEATURE_SNAPSHOT_MANIFEST_PATH="/tmp/russell-top50.csv.manifest.json",
        IBKR_RUN_AS_OF_DATE="2026-04-01",
    )

    observed = {}

    def fake_load_feature_snapshot_guarded(path, **_kwargs):
        observed["path"] = path
        return SimpleNamespace(
            frame=_russell_top50_snapshot_rows(),
            metadata={
                "snapshot_guard_decision": "proceed",
                "snapshot_as_of": "2026-03-31",
                "snapshot_path": path,
                "snapshot_age_days": 1,
            },
        )

    monkeypatch.setattr(
        strategy_runtime_module,
        "load_feature_snapshot_guarded",
        fake_load_feature_snapshot_guarded,
    )

    result = module.compute_signals(None, {"AAA"})

    assert observed["path"] == "/tmp/russell-top50.csv"
    assert result[4]["strategy_profile"] == "russell_top50_leader_rotation_aggressive"
    assert result[4]["status_icon"] == "👑"
    assert result[4]["snapshot_guard_decision"] == "proceed"


@pytest.mark.skip(reason="Tech/Communication is research-only and no longer an IBKR live profile")
def test_compute_signals_loads_tech_communication_pullback_enhancement_runtime(strategy_module_factory, monkeypatch, tmp_path):
    pytest.importorskip("pandas")

    snapshot_path = tmp_path / "cash_buffer_snapshot.csv"
    config_path = tmp_path / "tech_communication_pullback_enhancement.json"
    snapshot_path.write_text(
        "\n".join(
            [
                "as_of,symbol,sector,close,volume,adv20_usd,history_days,mom_6_1,mom_12_1,sma20_gap,sma50_gap,sma200_gap,ma50_over_ma200,vol_63,maxdd_126,breakout_252,dist_63_high,dist_126_high,rebound_20,base_eligible",
                "2026-03-31,QQQ,benchmark,500,1000000,1000000000,400,0.20,0.30,0.03,0.05,0.08,0.04,0.22,-0.12,-0.01,-0.03,-0.05,0.04,false",
                "2026-03-31,BOXX,defense,101,200000,20000000,400,0.02,0.04,0.00,0.00,0.01,0.00,0.03,-0.01,0.00,-0.01,-0.01,0.00,false",
                "2026-03-31,AAPL,Information Technology,200,1000000,150000000,400,0.20,0.35,0.03,0.05,0.10,0.05,0.18,-0.08,-0.01,-0.03,-0.05,0.05,true",
                "2026-03-31,MSFT,Information Technology,350,1000000,150000000,400,0.18,0.33,0.03,0.05,0.09,0.04,0.17,-0.09,-0.02,-0.04,-0.06,0.04,true",
                "2026-03-31,NVDA,Information Technology,900,1000000,150000000,400,0.30,0.60,0.07,0.09,0.18,0.10,0.35,-0.05,-0.01,-0.02,-0.04,0.12,true",
                "2026-03-31,META,Communication,520,1000000,150000000,400,0.22,0.40,0.04,0.06,0.11,0.05,0.24,-0.07,-0.03,-0.05,-0.07,0.07,true",
                "2026-03-31,GOOGL,Communication,180,1000000,150000000,400,0.17,0.28,0.02,0.04,0.08,0.03,0.20,-0.08,-0.04,-0.06,-0.08,0.05,true",
                "2026-03-31,NFLX,Communication,620,1000000,150000000,400,0.18,0.31,0.03,0.05,0.09,0.04,0.22,-0.07,-0.03,-0.05,-0.07,0.05,true",
                "2026-03-31,TTWO,Communication,210,1000000,150000000,400,0.14,0.20,0.01,0.03,0.06,0.02,0.18,-0.09,-0.04,-0.06,-0.09,0.03,true",
                "2026-03-31,ADBE,Information Technology,600,1000000,150000000,400,0.16,0.27,0.02,0.04,0.07,0.03,0.19,-0.08,-0.05,-0.06,-0.08,0.04,true",
                "2026-03-31,CRM,Information Technology,320,1000000,150000000,400,0.15,0.26,0.02,0.03,0.07,0.03,0.18,-0.09,-0.05,-0.06,-0.09,0.03,true",
                "2026-03-31,NOW,Information Technology,780,1000000,150000000,400,0.16,0.29,0.02,0.04,0.08,0.03,0.19,-0.07,-0.04,-0.06,-0.08,0.05,true",
            ]
        ),
        encoding="utf-8",
    )
    config_path.write_text(
        json.dumps(
            {
                "name": "tech_communication_pullback_enhancement",
                "family": "tech_heavy_pullback",
                "branch_role": "cash-buffered parallel branch",
                "benchmark_symbol": "QQQ",
                "holdings_count": 8,
                "single_name_cap": 0.10,
                "sector_cap": 0.40,
                "hold_bonus": 0.10,
                "min_adv20_usd": 50_000_000.0,
                "normalization": "universe_cross_sectional",
                "score_template": "balanced_pullback",
                "sector_whitelist": ["Information Technology", "Communication"],
                "breadth_thresholds": {"soft": 0.55, "hard": 0.35},
                "exposures": {"risk_on": 0.8, "soft_defense": 0.6, "hard_defense": 0.0},
                "execution_cash_reserve_ratio": 0.0,
                "residual_proxy": "simple_excess_return_vs_QQQ",
            }
        ),
        encoding="utf-8",
    )
    _write_cash_buffer_manifest(snapshot_path, config_path, snapshot_as_of="2026-03-31")

    module = strategy_module_factory(
        STRATEGY_PROFILE="tech_communication_pullback_enhancement",
        IBKR_FEATURE_SNAPSHOT_PATH=str(snapshot_path),
        IBKR_FEATURE_SNAPSHOT_MANIFEST_PATH=str(Path(f"{snapshot_path}.manifest.json")),
        IBKR_STRATEGY_CONFIG_PATH=str(config_path),
        IBKR_RUN_AS_OF_DATE="2026-04-01",
    )
    result = module.compute_signals(None, {"AAPL"})

    assert result[0]["BOXX"] == pytest.approx(0.2)
    assert result[4]["strategy_profile"] == "tech_communication_pullback_enhancement"
    assert result[4]["strategy_config_source"] in {"env", "external_config"}
    assert result[4]["realized_stock_weight"] == pytest.approx(0.8)
    assert result[4]["snapshot_guard_decision"] == "proceed"
    assert module.CASH_RESERVE_RATIO == pytest.approx(0.0)


@pytest.mark.skip(reason="Tech/Communication is research-only and no longer an IBKR live profile")
def test_compute_signals_fail_closes_when_snapshot_missing(strategy_module_factory):
    module = strategy_module_factory(
        STRATEGY_PROFILE="tech_communication_pullback_enhancement",
        IBKR_FEATURE_SNAPSHOT_PATH="/tmp/definitely-missing-cash-buffer-snapshot.csv",
        IBKR_RUN_AS_OF_DATE="2026-04-01",
    )

    result = module.compute_signals(None, {"AAPL"})

    assert result[0] is None
    assert result[4]["snapshot_guard_decision"] == "fail_closed"
    assert "feature_snapshot_missing" in result[4]["fail_reason"]


@pytest.mark.skip(reason="Tech/Communication is research-only and no longer an IBKR live profile")
def test_compute_signals_fail_closes_when_snapshot_is_stale(strategy_module_factory, tmp_path):
    snapshot_path = tmp_path / "stale_snapshot.csv"
    snapshot_path.write_text(
        "\n".join(
            [
                "as_of,symbol,sector,close,adv20_usd,history_days,mom_6_1,mom_12_1,sma20_gap,sma50_gap,sma200_gap,ma50_over_ma200,vol_63,maxdd_126,breakout_252,dist_63_high,dist_126_high,rebound_20,base_eligible",
                "2026-01-31,QQQ,benchmark,500,1000000000,400,0.20,0.30,0.03,0.05,0.08,0.04,0.22,-0.12,-0.01,-0.03,-0.05,0.04,false",
                "2026-01-31,BOXX,defense,101,20000000,400,0.02,0.04,0.00,0.00,0.01,0.00,0.03,-0.01,0.00,-0.01,-0.01,0.00,false",
                "2026-01-31,AAPL,Information Technology,200,150000000,400,0.20,0.35,0.03,0.05,0.10,0.05,0.18,-0.08,-0.01,-0.03,-0.05,0.05,true",
            ]
        ),
        encoding="utf-8",
    )

    module = strategy_module_factory(
        STRATEGY_PROFILE="tech_communication_pullback_enhancement",
        IBKR_FEATURE_SNAPSHOT_PATH=str(snapshot_path),
        IBKR_RUN_AS_OF_DATE="2026-04-05",
    )

    result = module.compute_signals(None, {"AAPL"})

    assert result[0] is None
    assert result[4]["snapshot_guard_decision"] == "fail_closed"
    assert "feature_snapshot_stale" in result[4]["fail_reason"]


@pytest.mark.skip(reason="Tech/Communication is research-only and no longer an IBKR live profile")
def test_compute_signals_fail_closes_when_manifest_missing(strategy_module_factory, tmp_path):
    snapshot_path = tmp_path / "snapshot.csv"
    config_path = tmp_path / "tech_communication_pullback_enhancement.json"
    snapshot_path.write_text(
        "\n".join(
            [
                "as_of,symbol,sector,close,adv20_usd,history_days,mom_6_1,mom_12_1,sma20_gap,sma50_gap,sma200_gap,ma50_over_ma200,vol_63,maxdd_126,breakout_252,dist_63_high,dist_126_high,rebound_20,base_eligible",
                "2026-03-31,QQQ,benchmark,500,1000000000,400,0.20,0.30,0.03,0.05,0.08,0.04,0.22,-0.12,-0.01,-0.03,-0.05,0.04,false",
                "2026-03-31,BOXX,defense,101,20000000,400,0.02,0.04,0.00,0.00,0.01,0.00,0.03,-0.01,0.00,-0.01,-0.01,0.00,false",
                "2026-03-31,AAPL,Information Technology,200,150000000,400,0.20,0.35,0.03,0.05,0.10,0.05,0.18,-0.08,-0.01,-0.03,-0.05,0.05,true",
            ]
        ),
        encoding="utf-8",
    )
    config_path.write_text(
        json.dumps(
            {
                "name": "tech_communication_pullback_enhancement",
                "family": "tech_heavy_pullback",
                "branch_role": "cash-buffered parallel branch",
                "benchmark_symbol": "QQQ",
                "holdings_count": 8,
                "single_name_cap": 0.10,
                "sector_cap": 0.40,
                "hold_bonus": 0.10,
                "min_adv20_usd": 50_000_000.0,
                "normalization": "universe_cross_sectional",
                "score_template": "balanced_pullback",
                "sector_whitelist": ["Information Technology", "Communication"],
                "breadth_thresholds": {"soft": 0.55, "hard": 0.35},
                "exposures": {"risk_on": 0.8, "soft_defense": 0.6, "hard_defense": 0.0},
                "execution_cash_reserve_ratio": 0.0,
                "residual_proxy": "simple_excess_return_vs_QQQ",
            }
        ),
        encoding="utf-8",
    )

    module = strategy_module_factory(
        STRATEGY_PROFILE="tech_communication_pullback_enhancement",
        IBKR_FEATURE_SNAPSHOT_PATH=str(snapshot_path),
        IBKR_STRATEGY_CONFIG_PATH=str(config_path),
        IBKR_RUN_AS_OF_DATE="2026-04-01",
    )

    result = module.compute_signals(None, {"AAPL"})

    assert result[0] is None
    assert result[4]["snapshot_guard_decision"] == "fail_closed"
    assert "feature_snapshot_manifest_missing" in result[4]["fail_reason"]


def test_global_etf_rotation_defaults_to_no_cash_reserve(strategy_module_factory):
    module = strategy_module_factory(
        STRATEGY_PROFILE="global_etf_rotation",
    )

    assert module.CASH_RESERVE_RATIO == pytest.approx(0.0)


def test_platform_reserved_cash_policy_can_raise_strategy_cash_reserve(strategy_module_factory):
    module = strategy_module_factory(
        STRATEGY_PROFILE="global_etf_rotation",
        IBKR_MIN_RESERVED_CASH_USD="250",
        IBKR_RESERVED_CASH_RATIO="0.05",
    )

    assert module.CASH_RESERVE_RATIO == pytest.approx(0.05)
    assert module.CASH_RESERVE_FLOOR_USD == pytest.approx(250.0)


@pytest.mark.skip(reason="Tech/Communication is research-only and no longer an IBKR live profile")
def test_compute_signals_exposes_dry_run_price_fallbacks(strategy_module_factory, tmp_path):
    pytest.importorskip("pandas")

    snapshot_path = tmp_path / "snapshot.csv"
    config_path = tmp_path / "tech_communication_pullback_enhancement.json"
    snapshot_path.write_text(
        "\n".join(
            [
                "as_of,symbol,sector,close,adv20_usd,history_days,mom_6_1,mom_12_1,sma20_gap,sma50_gap,sma200_gap,ma50_over_ma200,vol_63,maxdd_126,breakout_252,dist_63_high,dist_126_high,rebound_20,base_eligible",
                "2026-03-31,QQQ,benchmark,500,1000000000,400,0.20,0.30,0.03,0.05,0.08,0.04,0.22,-0.12,-0.01,-0.03,-0.05,0.04,false",
                "2026-03-31,BOXX,defense,101,20000000,400,0.02,0.04,0.00,0.00,0.01,0.00,0.03,-0.01,0.00,-0.01,-0.01,0.00,false",
                "2026-03-31,AAPL,Information Technology,200,150000000,400,0.20,0.35,0.03,0.05,0.10,0.05,0.18,-0.08,-0.01,-0.03,-0.05,0.05,true",
                "2026-03-31,MSFT,Information Technology,350,150000000,400,0.18,0.33,0.03,0.05,0.09,0.04,0.17,-0.09,-0.02,-0.04,-0.06,0.04,true",
            ]
        ),
        encoding="utf-8",
    )
    config_path.write_text(
        json.dumps(
            {
                "name": "tech_communication_pullback_enhancement",
                "family": "tech_heavy_pullback",
                "branch_role": "cash-buffered parallel branch",
                "benchmark_symbol": "QQQ",
                "holdings_count": 2,
                "single_name_cap": 0.5,
                "sector_cap": 1.0,
                "hold_bonus": 0.0,
                "min_adv20_usd": 1.0,
                "normalization": "universe_cross_sectional",
                "score_template": "balanced_pullback",
                "sector_whitelist": ["Information Technology"],
                "breadth_thresholds": {"soft": 0.55, "hard": 0.35},
                "exposures": {"risk_on": 1.0, "soft_defense": 1.0, "hard_defense": 0.0},
                "execution_cash_reserve_ratio": 0.0,
                "residual_proxy": "simple_excess_return_vs_QQQ",
            }
        ),
        encoding="utf-8",
    )
    _write_cash_buffer_manifest(snapshot_path, config_path, snapshot_as_of="2026-03-31")

    module = strategy_module_factory(
        STRATEGY_PROFILE="tech_communication_pullback_enhancement",
        IBKR_FEATURE_SNAPSHOT_PATH=str(snapshot_path),
        IBKR_FEATURE_SNAPSHOT_MANIFEST_PATH=str(Path(f"{snapshot_path}.manifest.json")),
        IBKR_STRATEGY_CONFIG_PATH=str(config_path),
        IBKR_RUN_AS_OF_DATE="2026-04-01",
    )
    result = module.compute_signals(None, set())

    assert result[4]["dry_run_price_fallbacks"]["BOXX"] == pytest.approx(101.0)
    assert result[4]["dry_run_price_fallbacks"]["AAPL"] == pytest.approx(200.0)
