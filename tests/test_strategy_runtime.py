from types import SimpleNamespace

import strategy_runtime as strategy_runtime_module
from quant_platform_kit.strategy_contracts import (
    PositionTarget,
    StrategyDecision,
    StrategyManifest,
    StrategyRuntimeAdapter,
)
from runtime_config_support import PlatformRuntimeSettings


def _build_runtime_settings(profile: str = "tech_pullback_cash_buffer") -> PlatformRuntimeSettings:
    return PlatformRuntimeSettings(
        project_id=None,
        ib_gateway_instance_name="127.0.0.1",
        ib_gateway_zone="",
        ib_gateway_mode="live",
        ib_gateway_ip_mode="internal",
        ib_client_id=1,
        strategy_profile=profile,
        strategy_display_name="Tech Pullback Cash Buffer",
        strategy_domain="us_equity",
        strategy_target_mode="weight",
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


def test_load_strategy_runtime_uses_entrypoint_defaults_and_runtime_adapter(monkeypatch):
    class FakeEntrypoint:
        manifest = StrategyManifest(
            profile="tech_pullback_cash_buffer",
            domain="us_equity",
            display_name="Tech Pullback Cash Buffer",
            description="test",
            required_inputs=frozenset({"feature_snapshot"}),
            default_config={"safe_haven": "BOXX", "benchmark_symbol": "QQQ"},
        )

        def evaluate(self, ctx):
            return StrategyDecision()

    monkeypatch.setattr(
        strategy_runtime_module,
        "load_strategy_definition",
        lambda raw_profile: SimpleNamespace(profile="tech_pullback_cash_buffer"),
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
        "tech_pullback_cash_buffer",
        runtime_settings=_build_runtime_settings(),
        logger=lambda _message: None,
    )

    assert runtime.entrypoint.manifest.profile == "tech_pullback_cash_buffer"
    assert runtime.runtime_config["benchmark_symbol"] == "SPY"
    assert runtime.merged_runtime_config["safe_haven"] == "BOXX"
    assert runtime.merged_runtime_config["benchmark_symbol"] == "SPY"
    assert runtime.merged_runtime_config["rebalance_months"] == (1, 4, 7, 10)
    assert runtime.status_icon == "🧲"


def test_feature_snapshot_runtime_prefers_unified_runtime_adapter_metadata(monkeypatch):
    captured = {}

    class FakeEntrypoint:
        manifest = StrategyManifest(
            profile="tech_pullback_cash_buffer",
            domain="us_equity",
            display_name="Tech Pullback Cash Buffer",
            description="test",
            required_inputs=frozenset({"feature_snapshot"}),
            default_config={"safe_haven": "BOXX", "benchmark_symbol": "QQQ"},
        )

        def evaluate(self, ctx):
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

    result = runtime.evaluate(
        ib=None,
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
    assert result.metadata["managed_symbols"] == ("AAPL", "BOXX")


def test_feature_snapshot_runtime_fail_closes_on_entrypoint_exception(monkeypatch):
    class ExplodingEntrypoint:
        manifest = StrategyManifest(
            profile="tech_pullback_cash_buffer",
            domain="us_equity",
            display_name="Tech Pullback Cash Buffer",
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
