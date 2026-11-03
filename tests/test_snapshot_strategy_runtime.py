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
        STRATEGY_PROFILE="russell_top50_leader_rotation",
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
    assert result[4]["strategy_profile"] == "russell_top50_leader_rotation"
    assert result[4]["status_icon"] == "👑"
    assert result[4]["snapshot_guard_decision"] == "proceed"


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
    assert module.STRATEGY_RUNTIME.merged_runtime_config["cash_reserve_ratio"] == pytest.approx(0.05)


def test_global_etf_snapshot_filters_only_platform_cash_reserve():
    source = {"cash_reserve_ratio": 0.05, "top_n": 2, "unexpected_parameter": 1}
    config = strategy_runtime_module._snapshot_signal_runtime_config("global_etf_rotation", source)
    assert config == {"top_n": 2, "unexpected_parameter": 1}
    assert source["cash_reserve_ratio"] == 0.05
    assert strategy_runtime_module._snapshot_signal_runtime_config("another_profile", source) == source


def test_global_etf_snapshot_uses_filtered_execution_reserve_without_changing_weights():
    from us_equity_strategies.strategies.global_etf_rotation import compute_signals_from_feature_snapshot

    rows = [
        dict(as_of="2026-03-31", symbol="AAA", role="ranking_pool_etf", close=100,
             momentum_13612w=0.3, score=0.3, sma_pass=True, eligible=True, vol_126=0.2),
        dict(as_of="2026-03-31", symbol="SPY", role="canary_asset", close=100,
             momentum_13612w=0.1, score=0.1, sma_pass=True, eligible=False, vol_126=0.2),
    ]
    kwargs = {"as_of_date": "2026-03-31", "top_n": 2,
              "translator": lambda key, **_kwargs: key}
    with_reserve = {**kwargs, "cash_reserve_ratio": 0.05}
    filtered = strategy_runtime_module._snapshot_signal_runtime_config("global_etf_rotation", with_reserve)
    expected = compute_signals_from_feature_snapshot(rows, (), **kwargs)
    assert compute_signals_from_feature_snapshot(rows, (), **filtered) == expected
    assert expected[0] == {"AAA": 0.5, "BIL": 0.5}
    unknown = strategy_runtime_module._snapshot_signal_runtime_config(
        "global_etf_rotation", {**with_reserve, "unexpected_parameter": 1}
    )
    with pytest.raises(TypeError, match="unexpected_parameter"):
        compute_signals_from_feature_snapshot(rows, (), **unknown)


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


@pytest.fixture
def russell_regime_platform(tmp_path, monkeypatch):
    from dataclasses import replace
    from tests.test_strategy_runtime import _build_runtime_settings
    from quant_platform_kit.common.models import PortfolioSnapshot
    from decision_mapper import map_strategy_decision
    import pandas as pd

    payload = dict(plugin="market_regime_control", schema_version="market_regime_control.v1",
                   as_of="2026-04-01", canonical_route="risk_on",
                   source_metadata={"producer": "synthetic-producer", "revision": "synthetic-revision"})
    binding = dict(plugin="market_regime_control", schema_version="market_regime_control.v1",
                   as_of="2026-04-01", valid_until="2026-04-01",
                   payload_sha256=hashlib.sha256(json.dumps(payload, sort_keys=True,
                       separators=(",", ":"), ensure_ascii=True).encode()).hexdigest())
    config = dict(market_regime_control_expected=binding, market_regime_control=payload)
    path = tmp_path / "strategy.json"
    path.write_text(json.dumps(config))
    settings = replace(_build_runtime_settings(profile="russell_top50_leader_rotation"),
                       strategy_config_path=str(path), ib_gateway_mode="paper", runtime_target_enabled=False)
    runtime = strategy_runtime_module.load_strategy_runtime(
        settings.strategy_profile, runtime_settings=settings, logger=lambda _: None)
    # Synthetic artifact IO and portfolio; real UES strategy, context and risk gate.
    rows = _russell_top50_snapshot_rows()
    fake_symbols = dict(zip(("NVDA", "META", "MSFT", "AAPL", "AMZN"),
                            ("FAKEA", "FAKEB", "FAKEC", "FAKED", "FAKEE")))
    rows = [{**row, "symbol": fake_symbols.get(row["symbol"], row["symbol"])} for row in rows]
    monkeypatch.setattr(strategy_runtime_module, "load_feature_snapshot_guarded", lambda *a, **k:
        SimpleNamespace(frame=rows, metadata={"snapshot_guard_decision": "proceed"}))
    portfolio = PortfolioSnapshot(as_of=pd.Timestamp("2026-04-01", tz="UTC").to_pydatetime(),
                                  total_equity=10000.0, buying_power=10000.0, positions=())
    monkeypatch.setattr(strategy_runtime_module.LoadedStrategyRuntime,
                        "_fetch_portfolio_snapshot_for_context", lambda *a, **k: portfolio)

    from quant_platform_kit.risk.contracts import RuntimeRiskLimits
    symbols = ("FAKEA", "FAKEB", "FAKEC", "FAKED", "FAKEE", "BOXX", "BIL")
    limits = RuntimeRiskLimits(
        allowed_symbols=symbols, product_leverage_factors={symbol: 1 for symbol in symbols},
        nominal_caps={symbol: 1.0 for symbol in symbols}, total_nominal_exposure_cap=1.0,
        total_effective_exposure_cap=1.0, max_positions=len(symbols))
    # Supply explicit synthetic limits; never replace or skip the actual risk gate.
    monkeypatch.setattr(strategy_runtime_module.LoadedStrategyRuntime,
        "_build_runtime_risk_capabilities",
        lambda self, inputs, capabilities: ({**capabilities, "runtime_risk_limits": limits}, "verified:synthetic"))

    def evaluate():
        result = runtime.evaluate(ib=None, current_holdings=set(), historical_close_loader=lambda *a, **k: None,
            run_as_of=pd.Timestamp("2026-04-01"), translator=lambda key: key, pacing_sec=0)
        mapped = map_strategy_decision(result.decision, strategy_profile=settings.strategy_profile,
                                       runtime_metadata={**result.metadata, "portfolio_total_equity": portfolio.total_equity})
        return result.decision, mapped[0]
    return path, config, evaluate


def test_russell_regime_valid_binding_final_weights(russell_regime_platform):
    _, _, evaluate = russell_regime_platform
    decision, weights = evaluate()
    assert "no_execute" not in decision.risk_flags
    assert decision.diagnostics["risk_gate"] == "APPROVE", (decision.risk_flags, decision.diagnostics.get("reason"))
    assert weights and any(symbol.startswith("FAKE") for symbol in weights)


@pytest.mark.parametrize("defect", ["missing_binding", "missing_payload", "schema", "digest", "as_of", "expired", "payload_mutated"])
def test_russell_regime_invalid_binding_no_weights(russell_regime_platform, defect):
    path, config, evaluate = russell_regime_platform
    if defect == "missing_binding":
        del config["market_regime_control_expected"]
    elif defect == "missing_payload":
        del config["market_regime_control"]
    elif defect == "payload_mutated":
        config["market_regime_control"]["source_metadata"]["revision"] = "synthetic-replacement"
    else:
        key, value = {"schema": ("schema_version", "synthetic.invalid"), "digest": ("payload_sha256", "0" * 64),
                      "as_of": ("as_of", "2026-03-31"), "expired": ("valid_until", "2026-03-31")}[defect]
        config["market_regime_control_expected"][key] = value
    path.write_text(json.dumps(config))
    decision, weights = evaluate()
    assert "no_execute" in decision.risk_flags
    assert weights is None
    assert decision.diagnostics["market_regime_control_binding_valid"] is False


def test_russell_regime_revocation_reread(russell_regime_platform):
    path, config, evaluate = russell_regime_platform
    assert evaluate()[1]
    del config["market_regime_control_expected"]
    path.write_text(json.dumps(config))
    assert evaluate()[1] is None


def test_russell_regime_read_error_redacted(russell_regime_platform, monkeypatch):
    _, _, evaluate = russell_regime_platform
    def fail(*args, **kwargs):
        raise OSError("synthetic-private-detail")
    monkeypatch.setattr(strategy_runtime_module.Path, "read_text", fail)
    with pytest.raises(ValueError, match="^Invalid Russell regime configuration$") as error:
        evaluate()
    assert error.value.__suppress_context__ is True
    assert "synthetic-private-detail" not in str(error.value)
