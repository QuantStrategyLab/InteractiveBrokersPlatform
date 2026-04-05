import pytest
from pathlib import Path
import hashlib
import json
from types import SimpleNamespace


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
                "contract_version": "tech_pullback_cash_buffer.feature_snapshot.v1",
                "strategy_profile": "tech_pullback_cash_buffer",
                "config_name": "tech_pullback_cash_buffer",
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


def test_compute_signals_uses_feature_snapshot_for_russell_1000(strategy_module_factory, monkeypatch):
    pytest.importorskip("pandas")

    module = strategy_module_factory(
        STRATEGY_PROFILE="russell_1000_multi_factor_defensive",
        IBKR_FEATURE_SNAPSHOT_PATH="/tmp/r1000.csv",
    )

    observed = {}

    def fake_load_feature_snapshot_guarded(path, **_kwargs):
        observed["path"] = path
        return SimpleNamespace(
            frame=[
                {
                    "as_of": "2026-03-31",
                    "symbol": "SPY",
                    "sector": "benchmark",
                    "mom_6_1": 0.1,
                    "mom_12_1": 0.1,
                    "sma200_gap": 0.1,
                    "vol_63": 0.1,
                    "maxdd_126": 0.1,
                    "eligible": False,
                }
            ],
            metadata={
                "snapshot_guard_decision": "proceed",
                "snapshot_as_of": "2026-03-31",
                "snapshot_path": path,
                "snapshot_age_days": 1,
            },
        )

    monkeypatch.setattr(module, "load_feature_snapshot_guarded", fake_load_feature_snapshot_guarded)
    monkeypatch.setattr(
        module,
        "strategy_compute_signals",
        lambda snapshot, holdings, **kwargs: (
            {"BOXX": 1.0},
            "signal",
            False,
            "breadth=0.0%",
            {"managed_symbols": ("BOXX",), "status_icon": "📏"},
        ),
    )

    result = module.compute_signals(None, {"AAA"})

    assert observed["path"] == "/tmp/r1000.csv"
    assert result[0] == {"BOXX": 1.0}
    assert result[4]["status_icon"] == "📏"
    assert result[4]["snapshot_guard_decision"] == "proceed"


def test_compute_signals_loads_tech_pullback_cash_buffer_runtime(strategy_module_factory, monkeypatch, tmp_path):
    pytest.importorskip("pandas")

    snapshot_path = tmp_path / "cash_buffer_snapshot.csv"
    config_path = tmp_path / "tech_pullback_cash_buffer.json"
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
                "name": "tech_pullback_cash_buffer",
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
        STRATEGY_PROFILE="tech_pullback_cash_buffer",
        IBKR_FEATURE_SNAPSHOT_PATH=str(snapshot_path),
        IBKR_FEATURE_SNAPSHOT_MANIFEST_PATH=str(Path(f"{snapshot_path}.manifest.json")),
        IBKR_STRATEGY_CONFIG_PATH=str(config_path),
        IBKR_RUN_AS_OF_DATE="2026-04-01",
    )
    result = module.compute_signals(None, {"AAPL"})

    assert result[0]["BOXX"] == pytest.approx(0.2)
    assert result[4]["strategy_profile"] == "tech_pullback_cash_buffer"
    assert result[4]["strategy_config_source"] in {"env", "external_config"}
    assert result[4]["realized_stock_weight"] == pytest.approx(0.8)
    assert result[4]["snapshot_guard_decision"] == "proceed"
    assert module.CASH_RESERVE_RATIO == pytest.approx(0.0)


def test_compute_signals_fail_closes_when_snapshot_missing(strategy_module_factory):
    module = strategy_module_factory(
        STRATEGY_PROFILE="tech_pullback_cash_buffer",
        IBKR_FEATURE_SNAPSHOT_PATH="/tmp/definitely-missing-cash-buffer-snapshot.csv",
        IBKR_RUN_AS_OF_DATE="2026-04-01",
    )

    result = module.compute_signals(None, {"AAPL"})

    assert result[0] is None
    assert result[4]["snapshot_guard_decision"] == "fail_closed"
    assert "feature_snapshot_missing" in result[4]["fail_reason"]


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
        STRATEGY_PROFILE="tech_pullback_cash_buffer",
        IBKR_FEATURE_SNAPSHOT_PATH=str(snapshot_path),
        IBKR_RUN_AS_OF_DATE="2026-04-05",
    )

    result = module.compute_signals(None, {"AAPL"})

    assert result[0] is None
    assert result[4]["snapshot_guard_decision"] == "fail_closed"
    assert "feature_snapshot_stale" in result[4]["fail_reason"]


def test_compute_signals_fail_closes_when_manifest_missing(strategy_module_factory, tmp_path):
    snapshot_path = tmp_path / "snapshot.csv"
    config_path = tmp_path / "tech_pullback_cash_buffer.json"
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
                "name": "tech_pullback_cash_buffer",
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
        STRATEGY_PROFILE="tech_pullback_cash_buffer",
        IBKR_FEATURE_SNAPSHOT_PATH=str(snapshot_path),
        IBKR_STRATEGY_CONFIG_PATH=str(config_path),
        IBKR_RUN_AS_OF_DATE="2026-04-01",
    )

    result = module.compute_signals(None, {"AAPL"})

    assert result[0] is None
    assert result[4]["snapshot_guard_decision"] == "fail_closed"
    assert "feature_snapshot_manifest_missing" in result[4]["fail_reason"]


def test_global_etf_rotation_keeps_default_cash_reserve(strategy_module_factory):
    module = strategy_module_factory(
        STRATEGY_PROFILE="global_etf_rotation",
    )

    assert module.CASH_RESERVE_RATIO == pytest.approx(0.03)
