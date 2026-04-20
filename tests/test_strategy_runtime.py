from types import SimpleNamespace

import strategy_runtime as strategy_runtime_module
from quant_platform_kit.strategy_contracts import (
    PositionTarget,
    StrategyDecision,
    StrategyManifest,
    StrategyRuntimeAdapter,
)
from runtime_config_support import PlatformRuntimeSettings


def _build_runtime_settings(
    profile: str = "tech_communication_pullback_enhancement",
    *,
    display_name: str = "Tech/Communication Pullback Enhancement",
    target_mode: str = "weight",
) -> PlatformRuntimeSettings:
    return PlatformRuntimeSettings(
        project_id=None,
        ib_gateway_instance_name="127.0.0.1",
        ib_gateway_zone="",
        ib_gateway_mode="live",
        ib_gateway_ip_mode="internal",
        ib_client_id=1,
        strategy_profile=profile,
        strategy_display_name=display_name,
        strategy_domain="us_equity",
        strategy_target_mode=target_mode,
        strategy_artifact_root=None,
        strategy_artifact_dir=None,
        feature_snapshot_path="/tmp/snapshot.csv",
        feature_snapshot_manifest_path=None,
        strategy_config_path="/tmp/config.json",
        strategy_config_source="env",
        reconciliation_output_path=None,
        dry_run_only=True,
        account_group="default",
        service_name=None,
        account_ids=(),
        tg_token=None,
        tg_chat_id=None,
        notify_lang="en",
    )


def test_main_compute_signals_uses_strategy_runtime_decision(strategy_module, monkeypatch):
    observed = {}

    class FakeRuntime:
        def evaluate(
            self,
            *,
            ib,
            current_holdings,
            historical_close_loader,
            historical_candle_loader,
            run_as_of,
            translator,
            pacing_sec,
        ):
            observed["ib"] = ib
            observed["current_holdings"] = tuple(sorted(current_holdings))
            observed["run_as_of"] = str(run_as_of.date())
            observed["pacing_sec"] = pacing_sec
            observed["translator_sample"] = translator("equity")
            observed["historical_loader"] = historical_close_loader
            observed["historical_candle_loader"] = historical_candle_loader
            return type(
                "Evaluation",
                (),
                {
                    "decision": StrategyDecision(
                        positions=(
                            PositionTarget(symbol="AAA", target_weight=0.8),
                            PositionTarget(symbol="BIL", target_weight=0.2, role="safe_haven"),
                        ),
                        diagnostics={
                            "signal_description": "rotation signal",
                            "status_description": "canary=ok",
                        },
                    ),
                    "metadata": {
                        "strategy_profile": "global_etf_rotation",
                        "managed_symbols": ("AAA", "BIL"),
                        "status_icon": "🐤",
                        "dry_run_only": False,
                    },
                },
            )()

    monkeypatch.setattr(strategy_module, "STRATEGY_RUNTIME", FakeRuntime())
    monkeypatch.setattr(strategy_module, "resolve_run_as_of_date", lambda: strategy_module.pd.Timestamp("2026-04-07"))

    result = strategy_module.compute_signals("fake-ib", {"AAA"})

    assert result[0] == {"AAA": 0.8, "BIL": 0.2}
    assert result[1] == "rotation signal"
    assert result[2] is False
    assert result[3] == "canary=ok"
    assert result[4]["managed_symbols"] == ("AAA", "BIL")
    assert observed["ib"] == "fake-ib"
    assert observed["current_holdings"] == ("AAA",)
    assert observed["run_as_of"] == "2026-04-07"
    assert observed["pacing_sec"] == strategy_module.HIST_DATA_PACING_SEC
    assert observed["translator_sample"]
    assert callable(observed["historical_candle_loader"])


def test_load_strategy_runtime_uses_entrypoint_defaults_and_runtime_adapter(monkeypatch):
    class FakeEntrypoint:
        manifest = StrategyManifest(
            profile="tech_communication_pullback_enhancement",
            domain="us_equity",
            display_name="Tech/Communication Pullback Enhancement",
            description="test",
            required_inputs=frozenset({"feature_snapshot"}),
            default_config={"safe_haven": "BOXX", "benchmark_symbol": "QQQ"},
        )

        def evaluate(self, ctx):
            return StrategyDecision()

    monkeypatch.setattr(
        strategy_runtime_module,
        "load_strategy_definition",
        lambda raw_profile: SimpleNamespace(profile="tech_communication_pullback_enhancement"),
    )
    monkeypatch.setattr(
        strategy_runtime_module,
        "load_strategy_entrypoint_for_profile",
        lambda raw_profile: FakeEntrypoint(),
    )
    monkeypatch.setattr(
        strategy_runtime_module,
        "load_strategy_runtime_adapter_for_profile",
        lambda raw_profile: StrategyRuntimeAdapter(
            status_icon="🧲",
            runtime_parameter_loader=lambda **_kwargs: {
                "benchmark_symbol": "SPY",
                "rebalance_months": (1, 4, 7, 10),
            },
        ),
    )

    runtime = strategy_runtime_module.load_strategy_runtime(
        "tech_communication_pullback_enhancement",
        runtime_settings=_build_runtime_settings(),
        logger=lambda _message: None,
    )

    assert runtime.entrypoint.manifest.profile == "tech_communication_pullback_enhancement"
    assert runtime.runtime_config["benchmark_symbol"] == "SPY"
    assert runtime.merged_runtime_config["safe_haven"] == "BOXX"
    assert runtime.merged_runtime_config["benchmark_symbol"] == "SPY"
    assert runtime.merged_runtime_config["rebalance_months"] == (1, 4, 7, 10)
    assert runtime.status_icon == "🧲"


def test_feature_snapshot_runtime_prefers_unified_runtime_adapter_metadata(monkeypatch):
    captured = {}

    class FakeEntrypoint:
        manifest = StrategyManifest(
            profile="tech_communication_pullback_enhancement",
            domain="us_equity",
            display_name="Tech/Communication Pullback Enhancement",
            description="test",
            required_inputs=frozenset({"feature_snapshot"}),
            default_config={"safe_haven": "BOXX", "benchmark_symbol": "QQQ"},
        )

        def evaluate(self, ctx):
            captured["market_data"] = dict(ctx.market_data)
            captured["portfolio"] = ctx.portfolio
            captured["runtime_config"] = dict(ctx.runtime_config)
            return StrategyDecision()

    runtime = strategy_runtime_module.LoadedStrategyRuntime(
        entrypoint=FakeEntrypoint(),
        runtime_adapter=StrategyRuntimeAdapter(
            status_icon="🧲",
            required_feature_columns=frozenset({"symbol", "close"}),
            snapshot_date_columns=("as_of",),
            max_snapshot_month_lag=2,
            require_snapshot_manifest=True,
            snapshot_contract_version="adapter.contract",
            managed_symbols_extractor=lambda *_args, **_kwargs: ("AAPL", "BOXX"),
        ),
        runtime_settings=_build_runtime_settings(),
        runtime_config={},
        merged_runtime_config={"safe_haven": "BOXX", "benchmark_symbol": "QQQ"},
        status_icon="🧲",
        logger=lambda _message: None,
    )

    def fake_guard(path, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            frame=[{"as_of": "2026-03-31", "symbol": "AAPL", "close": 1.0}],
            metadata={
                "snapshot_guard_decision": "proceed",
                "snapshot_as_of": "2026-03-31",
                "snapshot_path": path,
                "snapshot_age_days": 1,
            },
        )

    monkeypatch.setattr(strategy_runtime_module, "load_feature_snapshot_guarded", fake_guard)
    portfolio_snapshot = SimpleNamespace(total_equity=25000.0)
    monkeypatch.setattr(strategy_runtime_module, "fetch_portfolio_snapshot", lambda _ib: portfolio_snapshot)

    result = runtime.evaluate(
        ib="fake-ib",
        current_holdings={"AAPL"},
        historical_close_loader=lambda *_args, **_kwargs: None,
        run_as_of=strategy_runtime_module.pd.Timestamp("2026-04-01"),
        translator=lambda key, **_kwargs: key,
        pacing_sec=0.5,
    )

    assert captured["required_columns"] == frozenset({"symbol", "close"})
    assert captured["snapshot_date_columns"] == ("as_of",)
    assert captured["max_snapshot_month_lag"] == 2
    assert captured["require_manifest"] is True
    assert captured["expected_contract_version"] == "adapter.contract"
    assert captured["market_data"]["feature_snapshot"] == [{"as_of": "2026-03-31", "symbol": "AAPL", "close": 1.0}]
    assert captured["portfolio"] is portfolio_snapshot
    assert captured["runtime_config"]["translator"]("equity") == "equity"
    assert "pacing_sec" not in captured["runtime_config"]
    assert result.metadata["managed_symbols"] == ("AAPL", "BOXX")


def test_feature_snapshot_runtime_can_add_daily_market_benchmark_and_portfolio_inputs(monkeypatch):
    captured = {}

    class FakeEntrypoint:
        manifest = StrategyManifest(
            profile="dynamic_mega_leveraged_pullback",
            domain="us_equity",
            display_name="Dynamic Mega Leveraged Pullback",
            description="test",
            required_inputs=frozenset({"feature_snapshot", "market_history", "benchmark_history", "portfolio_snapshot"}),
            default_config={"safe_haven": "BOXX", "benchmark_symbol": "QQQ"},
        )

        def evaluate(self, ctx):
            captured["market_data"] = dict(ctx.market_data)
            captured["portfolio"] = ctx.portfolio
            captured["capabilities"] = dict(ctx.capabilities)
            return StrategyDecision(
                positions=(
                    PositionTarget(symbol="NVDL", target_weight=0.4),
                    PositionTarget(symbol="BOXX", target_weight=0.6, role="safe_haven"),
                ),
                diagnostics={"signal_description": "leveraged", "status_description": "ok"},
            )

    runtime = strategy_runtime_module.LoadedStrategyRuntime(
        entrypoint=FakeEntrypoint(),
        runtime_adapter=StrategyRuntimeAdapter(
            status_icon="2x",
            available_inputs=frozenset({"feature_snapshot", "market_history", "benchmark_history", "portfolio_snapshot"}),
            required_feature_columns=frozenset({"symbol", "underlying_symbol"}),
            snapshot_date_columns=("as_of",),
            max_snapshot_month_lag=1,
            require_snapshot_manifest=False,
            snapshot_contract_version=None,
            managed_symbols_extractor=lambda *_args, **_kwargs: ("NVDL", "BOXX"),
            portfolio_input_name="portfolio_snapshot",
        ),
        runtime_settings=_build_runtime_settings(profile="dynamic_mega_leveraged_pullback"),
        runtime_config={},
        merged_runtime_config={"safe_haven": "BOXX", "benchmark_symbol": "QQQ"},
        status_icon="2x",
        logger=lambda _message: None,
    )

    monkeypatch.setattr(
        strategy_runtime_module,
        "load_feature_snapshot_guarded",
        lambda path, **_kwargs: SimpleNamespace(
            frame=[{"as_of": "2026-03-31", "symbol": "NVDL", "underlying_symbol": "NVDA"}],
            metadata={
                "snapshot_guard_decision": "proceed",
                "snapshot_as_of": "2026-03-31",
                "snapshot_path": path,
                "snapshot_age_days": 1,
            },
        ),
    )
    portfolio_snapshot = SimpleNamespace(total_equity=50000.0)
    monkeypatch.setattr(strategy_runtime_module, "fetch_portfolio_snapshot", lambda _ib: portfolio_snapshot)

    def close_loader(_ib, symbol, **_kwargs):
        return [100.0, 101.0] if symbol == "NVDA" else []

    def candle_loader(_ib, symbol, duration="2 Y", bar_size="1 day"):
        assert symbol == "QQQ"
        assert duration == "2 Y"
        assert bar_size == "1 day"
        return [{"close": 100.0, "high": 101.0, "low": 99.0}]

    result = runtime.evaluate(
        ib="fake-ib",
        current_holdings={"NVDL"},
        historical_close_loader=close_loader,
        historical_candle_loader=candle_loader,
        run_as_of=strategy_runtime_module.pd.Timestamp("2026-04-01"),
        translator=lambda key, **_kwargs: key,
        pacing_sec=0.5,
    )

    assert captured["market_data"]["feature_snapshot"][0]["symbol"] == "NVDL"
    assert captured["market_data"]["market_history"] is close_loader
    assert captured["market_data"]["benchmark_history"][0]["high"] == 101.0
    assert captured["portfolio"] is portfolio_snapshot
    assert captured["capabilities"]["broker_client"] == "fake-ib"
    assert result.metadata["managed_symbols"] == ("NVDL", "BOXX")


def test_market_history_runtime_uses_canonical_market_history_key(monkeypatch):
    captured = {}

    class FakeEntrypoint:
        manifest = StrategyManifest(
            profile="global_etf_rotation",
            domain="us_equity",
            display_name="Global ETF Rotation",
            description="test",
            required_inputs=frozenset({"market_history"}),
            default_config={"safe_haven": "BIL", "ranking_pool": ("AAA",)},
        )

        def evaluate(self, ctx):
            captured["market_data"] = dict(ctx.market_data)
            captured["portfolio"] = ctx.portfolio
            return StrategyDecision()

    def loader(*_args, **_kwargs):
        return None

    runtime = strategy_runtime_module.LoadedStrategyRuntime(
        entrypoint=FakeEntrypoint(),
        runtime_adapter=StrategyRuntimeAdapter(status_icon="🐤"),
        runtime_settings=_build_runtime_settings(profile="global_etf_rotation"),
        runtime_config={},
        merged_runtime_config={"safe_haven": "BIL", "ranking_pool": ("AAA",)},
        status_icon="🐤",
        logger=lambda _message: None,
    )
    portfolio_snapshot = SimpleNamespace(total_equity=1200.0)
    monkeypatch.setattr(strategy_runtime_module, "fetch_portfolio_snapshot", lambda _ib: portfolio_snapshot)

    runtime.evaluate(
        ib="fake-ib",
        current_holdings={"AAA"},
        historical_close_loader=loader,
        run_as_of=strategy_runtime_module.pd.Timestamp("2026-04-01"),
        translator=lambda key, **_kwargs: key,
        pacing_sec=0.5,
    )

    assert captured["market_data"]["market_history"] is loader
    assert captured["portfolio"] is portfolio_snapshot
    assert "historical_close_loader" not in captured["market_data"]


def test_feature_snapshot_runtime_fail_closes_on_entrypoint_exception(monkeypatch):
    class ExplodingEntrypoint:
        manifest = StrategyManifest(
            profile="tech_communication_pullback_enhancement",
            domain="us_equity",
            display_name="Tech/Communication Pullback Enhancement",
            description="test",
            required_inputs=frozenset({"feature_snapshot"}),
            default_config={"safe_haven": "BOXX"},
        )

        def evaluate(self, ctx):
            raise RuntimeError("boom")

    runtime = strategy_runtime_module.LoadedStrategyRuntime(
        entrypoint=ExplodingEntrypoint(),
        runtime_adapter=StrategyRuntimeAdapter(
            status_icon="📏",
            required_feature_columns=frozenset(),
            snapshot_date_columns=("as_of",),
            max_snapshot_month_lag=1,
            require_snapshot_manifest=False,
            snapshot_contract_version=None,
            managed_symbols_extractor=lambda *_args, **_kwargs: ("AAA", "BOXX"),
        ),
        runtime_settings=_build_runtime_settings(),
        runtime_config={},
        merged_runtime_config={"safe_haven": "BOXX"},
        status_icon="📏",
        logger=lambda _message: None,
    )

    monkeypatch.setattr(
        strategy_runtime_module,
        "load_feature_snapshot_guarded",
        lambda path, **_kwargs: SimpleNamespace(
            frame=[{"as_of": "2026-03-31", "symbol": "AAA"}],
            metadata={
                "snapshot_guard_decision": "proceed",
                "snapshot_as_of": "2026-03-31",
                "snapshot_path": path,
                "snapshot_age_days": 1,
            },
        ),
    )

    result = runtime.evaluate(
        ib=None,
        current_holdings={"AAA"},
        historical_close_loader=lambda *_args, **_kwargs: None,
        run_as_of=strategy_runtime_module.pd.Timestamp("2026-04-01"),
        translator=lambda key, **_kwargs: key,
        pacing_sec=0.5,
    )

    assert result.metadata["snapshot_guard_decision"] == "fail_closed"
    assert "feature_snapshot_compute_failed:RuntimeError:boom" in result.metadata["fail_reason"]
    assert result.decision.diagnostics["signal_description"] == "feature snapshot compute failed"


def test_value_target_runtime_builds_semiconductor_inputs(monkeypatch):
    captured = {}

    class FakeEntrypoint:
        manifest = StrategyManifest(
            profile="soxl_soxx_trend_income",
            domain="us_equity",
            display_name="SOXL/SOXX Semiconductor Trend Income",
            description="test",
            required_inputs=frozenset({"derived_indicators", "portfolio_snapshot"}),
            default_config={"managed_symbols": ("SOXL", "SOXX", "QQQI", "SPYI", "BOXX")},
        )

        def evaluate(self, ctx):
            captured["market_data"] = dict(ctx.market_data)
            captured["portfolio"] = ctx.portfolio
            captured["runtime_config"] = dict(ctx.runtime_config)
            return StrategyDecision(
                positions=(
                    PositionTarget(symbol="SOXL", target_value=30000.0),
                    PositionTarget(symbol="BOXX", target_value=20000.0, role="safe_haven"),
                )
            )

    runtime = strategy_runtime_module.LoadedStrategyRuntime(
        entrypoint=FakeEntrypoint(),
        runtime_adapter=StrategyRuntimeAdapter(status_icon="🚀", portfolio_input_name="portfolio_snapshot"),
        runtime_settings=_build_runtime_settings(profile="soxl_soxx_trend_income"),
        runtime_config={},
        merged_runtime_config={"managed_symbols": ("SOXL", "SOXX", "QQQI", "SPYI", "BOXX")},
        status_icon="🚀",
        logger=lambda _message: None,
    )

    portfolio_snapshot = SimpleNamespace(total_equity=50000.0)
    monkeypatch.setattr(strategy_runtime_module, "fetch_portfolio_snapshot", lambda _ib: portfolio_snapshot)

    def fake_loader(_ib, symbol, duration="2 Y", bar_size="1 day"):
        if symbol == "SOXL":
            return [100.0] * 170
        if symbol == "SOXX":
            return [200.0] * 170
        raise AssertionError(symbol)

    result = runtime.evaluate(
        ib="fake-ib",
        current_holdings={"SOXL"},
        historical_close_loader=fake_loader,
        run_as_of=strategy_runtime_module.pd.Timestamp("2026-04-01"),
        translator=lambda key, **_kwargs: key,
        pacing_sec=0.5,
    )

    assert captured["market_data"]["derived_indicators"]["soxl"]["price"] == 100.0
    assert captured["market_data"]["derived_indicators"]["soxl"]["ma_trend"] == 100.0
    assert captured["market_data"]["derived_indicators"]["soxx"]["price"] == 200.0
    assert captured["portfolio"] is portfolio_snapshot
    assert "pacing_sec" not in captured["runtime_config"]
    assert result.metadata["portfolio_total_equity"] == 50000.0
    assert result.metadata["managed_symbols"] == ("SOXL", "SOXX", "QQQI", "SPYI", "BOXX")


def test_value_target_runtime_builds_tqqq_inputs(monkeypatch):
    captured = {}

    class FakeEntrypoint:
        manifest = StrategyManifest(
            profile="tqqq_growth_income",
            domain="us_equity",
            display_name="TQQQ Growth Income",
            description="test",
            required_inputs=frozenset({"benchmark_history", "portfolio_snapshot"}),
            default_config={
                "benchmark_symbol": "QQQ",
                "managed_symbols": ("TQQQ", "QQQ", "BOXX", "SPYI", "QQQI"),
            },
        )

        def evaluate(self, ctx):
            captured["market_data"] = dict(ctx.market_data)
            captured["portfolio"] = ctx.portfolio
            captured["runtime_config"] = dict(ctx.runtime_config)
            return StrategyDecision(
                positions=(
                    PositionTarget(symbol="TQQQ", target_value=30000.0),
                    PositionTarget(symbol="BOXX", target_value=20000.0, role="safe_haven"),
                )
            )

    runtime = strategy_runtime_module.LoadedStrategyRuntime(
        entrypoint=FakeEntrypoint(),
        runtime_adapter=StrategyRuntimeAdapter(status_icon="🐤", portfolio_input_name="portfolio_snapshot"),
        runtime_settings=_build_runtime_settings(
            profile="tqqq_growth_income",
            display_name="TQQQ Growth Income",
            target_mode="value",
        ),
        runtime_config={},
        merged_runtime_config={
            "benchmark_symbol": "QQQ",
            "managed_symbols": ("TQQQ", "QQQ", "BOXX", "SPYI", "QQQI"),
        },
        status_icon="🐤",
        logger=lambda _message: None,
    )

    portfolio_snapshot = SimpleNamespace(total_equity=50000.0)
    monkeypatch.setattr(strategy_runtime_module, "fetch_portfolio_snapshot", lambda _ib: portfolio_snapshot)

    def fake_candle_loader(_ib, symbol, duration="2 Y", bar_size="1 day"):
        assert symbol == "QQQ"
        assert duration == "2 Y"
        assert bar_size == "1 day"
        return [
            {"close": 100.0, "high": 101.0, "low": 99.0}
            for _ in range(220)
        ]

    result = runtime.evaluate(
        ib="fake-ib",
        current_holdings={"TQQQ"},
        historical_close_loader=lambda *_args, **_kwargs: None,
        historical_candle_loader=fake_candle_loader,
        run_as_of=strategy_runtime_module.pd.Timestamp("2026-04-01"),
        translator=lambda key, **_kwargs: key,
        pacing_sec=0.5,
    )

    assert len(captured["market_data"]["benchmark_history"]) == 220
    assert captured["market_data"]["benchmark_history"][0]["high"] == 101.0
    assert captured["portfolio"] is portfolio_snapshot
    assert "pacing_sec" not in captured["runtime_config"]
    assert result.metadata["portfolio_total_equity"] == 50000.0
    assert result.metadata["benchmark_symbol"] == "QQQ"
    assert result.metadata["managed_symbols"] == ("TQQQ", "QQQ", "BOXX", "SPYI", "QQQI")
