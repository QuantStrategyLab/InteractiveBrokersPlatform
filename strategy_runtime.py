from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

import pandas as pd

from quant_platform_kit.common.feature_snapshot import load_feature_snapshot_guarded
from quant_platform_kit.common.feature_snapshot_runtime import (
    FeatureSnapshotContextRequest,
    FeatureSnapshotRuntimeSettings,
    evaluate_feature_snapshot_strategy,
)
from quant_platform_kit.ibkr import (
    build_ibkr_strategy_context,
    build_benchmark_history_inputs,
    build_market_history_inputs,
    build_semiconductor_rotation_inputs,
    fetch_portfolio_snapshot,
)
from quant_platform_kit.strategy_contracts import (
    StrategyDecision,
    StrategyEntrypoint,
    StrategyRuntimeAdapter,
    build_strategy_context_from_available_inputs,
    build_strategy_evaluation_inputs,
)
from runtime_config_support import PlatformRuntimeSettings
from strategy_loader import (
    load_strategy_definition,
    load_strategy_entrypoint_for_profile,
    load_strategy_runtime_adapter_for_profile,
)


DEFAULT_CASH_RESERVE_RATIO = 0.03
_FEATURE_SNAPSHOT_INPUT = "feature_snapshot"
_MARKET_HISTORY_INPUT = "market_history"
_BENCHMARK_HISTORY_INPUT = "benchmark_history"
_DERIVED_INDICATORS_INPUT = "derived_indicators"
_PORTFOLIO_SNAPSHOT_INPUT = "portfolio_snapshot"


@dataclass(frozen=True)
class StrategyEvaluationResult:
    decision: StrategyDecision
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LoadedStrategyRuntime:
    entrypoint: StrategyEntrypoint
    runtime_settings: PlatformRuntimeSettings
    runtime_adapter: StrategyRuntimeAdapter
    runtime_config: Mapping[str, Any] = field(default_factory=dict)
    merged_runtime_config: Mapping[str, Any] = field(default_factory=dict)
    status_icon: str = "🐤"
    cash_reserve_ratio: float = DEFAULT_CASH_RESERVE_RATIO
    logger: Callable[[str], None] = print

    @property
    def profile(self) -> str:
        return self.entrypoint.manifest.profile

    @property
    def required_inputs(self) -> frozenset[str]:
        return frozenset(self.entrypoint.manifest.required_inputs)

    def _runtime_adapter_with_portfolio(
        self,
        runtime_adapter: StrategyRuntimeAdapter,
        portfolio_snapshot: Any | None,
    ) -> StrategyRuntimeAdapter:
        if portfolio_snapshot is None or runtime_adapter.portfolio_input_name:
            return runtime_adapter
        available_inputs = set(runtime_adapter.available_inputs or self.required_inputs)
        available_inputs.update(self.required_inputs)
        available_inputs.add(_PORTFOLIO_SNAPSHOT_INPUT)
        return replace(
            runtime_adapter,
            available_inputs=frozenset(available_inputs),
            portfolio_input_name=_PORTFOLIO_SNAPSHOT_INPUT,
        )

    def _fetch_portfolio_snapshot_for_context(self, ib, *, required: bool) -> Any | None:
        if ib is None and not required:
            return None
        if required:
            return fetch_portfolio_snapshot(ib)
        try:
            return fetch_portfolio_snapshot(ib)
        except Exception as exc:
            self.logger(
                "strategy_dashboard_portfolio_snapshot_failed | "
                f"profile={self.profile} error_type={type(exc).__name__} error={exc}"
            )
            return None

    def _build_strategy_context(
        self,
        *,
        runtime_adapter: StrategyRuntimeAdapter,
        as_of: pd.Timestamp,
        market_inputs: Mapping[str, Any],
        portfolio_snapshot: Any | None,
        runtime_config: Mapping[str, Any],
        current_holdings,
        ib,
    ):
        context_adapter = self._runtime_adapter_with_portfolio(runtime_adapter, portfolio_snapshot)
        available_inputs = set(context_adapter.available_inputs or self.required_inputs)
        available_inputs.update(self.required_inputs)
        evaluation_inputs = build_strategy_evaluation_inputs(
            available_inputs=available_inputs,
            market_inputs=market_inputs,
            portfolio_snapshot=portfolio_snapshot,
        )
        capabilities = {}
        if ib is not None:
            capabilities["broker_client"] = ib
        return build_strategy_context_from_available_inputs(
            entrypoint=self.entrypoint,
            runtime_adapter=context_adapter,
            as_of=as_of,
            available_inputs=evaluation_inputs,
            runtime_config=runtime_config,
            state={"current_holdings": tuple(current_holdings)},
            capabilities=capabilities,
        )

    def evaluate(
        self,
        *,
        ib,
        current_holdings,
        historical_close_loader: Callable[..., Any],
        historical_candle_loader: Callable[..., Any] | None = None,
        run_as_of: pd.Timestamp,
        translator: Callable[[str], str],
        pacing_sec: float,
    ) -> StrategyEvaluationResult:
        run_as_of = pd.Timestamp(run_as_of).normalize()
        if _FEATURE_SNAPSHOT_INPUT in self.required_inputs:
            return self._evaluate_feature_snapshot_strategy(
                ib=ib,
                current_holdings=current_holdings,
                historical_close_loader=historical_close_loader,
                historical_candle_loader=historical_candle_loader,
                run_as_of=run_as_of,
                translator=translator,
                pacing_sec=pacing_sec,
            )
        if _MARKET_HISTORY_INPUT in self.required_inputs:
            return self._evaluate_market_data_strategy(
                ib=ib,
                current_holdings=current_holdings,
                historical_close_loader=historical_close_loader,
                historical_candle_loader=historical_candle_loader,
                run_as_of=run_as_of,
                translator=translator,
                pacing_sec=pacing_sec,
            )
        if _PORTFOLIO_SNAPSHOT_INPUT in self.required_inputs and (
            _DERIVED_INDICATORS_INPUT in self.required_inputs
            or _BENCHMARK_HISTORY_INPUT in self.required_inputs
        ):
            return self._evaluate_value_target_strategy(
                ib=ib,
                current_holdings=current_holdings,
                historical_close_loader=historical_close_loader,
                historical_candle_loader=historical_candle_loader,
                run_as_of=run_as_of,
                translator=translator,
                pacing_sec=pacing_sec,
            )
        raise ValueError(
            f"Unsupported required_inputs for IBKR strategy profile {self.profile!r}: "
            f"{', '.join(sorted(self.required_inputs)) or '<none>'}"
        )

    def _evaluate_market_data_strategy(
        self,
        *,
        ib,
        current_holdings,
        historical_close_loader: Callable[..., Any],
        historical_candle_loader: Callable[..., Any] | None,
        run_as_of: pd.Timestamp,
        translator: Callable[[str], str],
        pacing_sec: float,
    ) -> StrategyEvaluationResult:
        runtime_config = dict(self.runtime_config)
        runtime_config.setdefault("translator", translator)
        runtime_config.setdefault("pacing_sec", float(pacing_sec))
        portfolio_snapshot = self._fetch_portfolio_snapshot_for_context(ib, required=False)
        ctx = self._build_strategy_context(
            runtime_adapter=self.runtime_adapter,
            as_of=run_as_of,
            market_inputs=build_market_history_inputs(historical_close_loader),
            portfolio_snapshot=portfolio_snapshot,
            runtime_config=runtime_config,
            current_holdings=current_holdings,
            ib=ib,
        )
        decision = self.entrypoint.evaluate(ctx)
        safe_haven_symbol = str(self.merged_runtime_config.get("safe_haven") or "").strip().upper() or None
        ranking_pool = tuple(str(symbol) for symbol in self.merged_runtime_config.get("ranking_pool", ()))
        managed_candidates = list(ranking_pool)
        if safe_haven_symbol:
            managed_candidates.append(safe_haven_symbol)
        managed_symbols = tuple(dict.fromkeys(managed_candidates))
        metadata = {
            "strategy_profile": self.profile,
            "managed_symbols": managed_symbols,
            "status_icon": self.status_icon,
            "dry_run_only": self.runtime_settings.dry_run_only,
        }
        if portfolio_snapshot is not None:
            metadata["portfolio_total_equity"] = float(getattr(portfolio_snapshot, "total_equity", 0.0) or 0.0)
        if safe_haven_symbol:
            metadata["safe_haven_symbol"] = safe_haven_symbol
        return StrategyEvaluationResult(decision=decision, metadata=metadata)

    def _evaluate_value_target_strategy(
        self,
        *,
        ib,
        current_holdings,
        historical_close_loader: Callable[..., Any],
        historical_candle_loader: Callable[..., Any] | None,
        run_as_of: pd.Timestamp,
        translator: Callable[[str], str],
        pacing_sec: float,
    ) -> StrategyEvaluationResult:
        runtime_config = dict(self.runtime_config)
        runtime_config.setdefault("translator", translator)
        portfolio_snapshot = fetch_portfolio_snapshot(ib)
        market_inputs = self._build_value_target_market_inputs(
            ib=ib,
            historical_close_loader=historical_close_loader,
            historical_candle_loader=historical_candle_loader,
        )
        ctx = build_ibkr_strategy_context(
            entrypoint=self.entrypoint,
            runtime_adapter=self.runtime_adapter,
            as_of=run_as_of,
            market_inputs=market_inputs,
            portfolio_snapshot=portfolio_snapshot,
            runtime_config=runtime_config,
            current_holdings=current_holdings,
            ib=ib,
        )
        decision = self.entrypoint.evaluate(ctx)
        managed_symbols = tuple(
            str(symbol) for symbol in self.merged_runtime_config.get("managed_symbols", ())
        )
        safe_haven_symbol = next(
            (position.symbol for position in decision.positions if position.role == "safe_haven"),
            None,
        )
        metadata = {
            "strategy_profile": self.profile,
            "managed_symbols": managed_symbols,
            "status_icon": self.status_icon,
            "dry_run_only": self.runtime_settings.dry_run_only,
            "portfolio_total_equity": float(portfolio_snapshot.total_equity),
        }
        if safe_haven_symbol:
            metadata["safe_haven_symbol"] = str(safe_haven_symbol)
        benchmark_symbol = market_inputs.get("benchmark_symbol")
        if benchmark_symbol:
            metadata["benchmark_symbol"] = str(benchmark_symbol)
        return StrategyEvaluationResult(decision=decision, metadata=metadata)

    def _build_value_target_market_inputs(
        self,
        *,
        ib,
        historical_close_loader: Callable[..., Any],
        historical_candle_loader: Callable[..., Any] | None,
    ) -> dict[str, Any]:
        if _DERIVED_INDICATORS_INPUT in self.required_inputs:
            return build_semiconductor_rotation_inputs(
                ib,
                historical_close_loader,
                trend_ma_window=int(self.merged_runtime_config.get("trend_ma_window", 150)),
            )
        if _BENCHMARK_HISTORY_INPUT in self.required_inputs:
            if historical_candle_loader is None:
                raise ValueError(
                    f"IBKR strategy profile {self.profile!r} requires benchmark_history but no candle loader was provided"
                )
            benchmark_symbol = str(self.merged_runtime_config.get("benchmark_symbol") or "QQQ").strip().upper()
            market_inputs = build_benchmark_history_inputs(
                ib,
                historical_candle_loader,
                benchmark_symbol=benchmark_symbol,
            )
            market_inputs["benchmark_symbol"] = benchmark_symbol
            return market_inputs
        raise ValueError(
            f"Unsupported value-target required_inputs for IBKR strategy profile {self.profile!r}: "
            f"{', '.join(sorted(self.required_inputs)) or '<none>'}"
        )

    def _evaluate_feature_snapshot_strategy(
        self,
        *,
        ib,
        current_holdings,
        historical_close_loader: Callable[..., Any],
        historical_candle_loader: Callable[..., Any] | None,
        run_as_of: pd.Timestamp,
        translator: Callable[[str], str],
        pacing_sec: float,
    ) -> StrategyEvaluationResult:
        del pacing_sec
        runtime_config_path = self.merged_runtime_config.get("runtime_config_path") or self.runtime_settings.strategy_config_path
        benchmark_symbol = str(self.merged_runtime_config.get("benchmark_symbol") or "SPY").strip().upper()
        portfolio_snapshot_holder: dict[str, Any] = {}
        runtime_config = dict(self.runtime_config)
        runtime_config.setdefault("translator", translator)

        def build_available_inputs(feature_snapshot) -> Mapping[str, Any]:
            requires_portfolio = (
                _PORTFOLIO_SNAPSHOT_INPUT in self.required_inputs
                or self.runtime_adapter.portfolio_input_name == _PORTFOLIO_SNAPSHOT_INPUT
            )
            portfolio_snapshot = self._fetch_portfolio_snapshot_for_context(
                ib,
                required=requires_portfolio,
            )
            if portfolio_snapshot is not None:
                portfolio_snapshot_holder["portfolio_snapshot"] = portfolio_snapshot
            market_inputs: dict[str, Any] = {_FEATURE_SNAPSHOT_INPUT: feature_snapshot}
            if _MARKET_HISTORY_INPUT in self.required_inputs:
                market_inputs.update(build_market_history_inputs(historical_close_loader))
            if _BENCHMARK_HISTORY_INPUT in self.required_inputs:
                if historical_candle_loader is None:
                    raise ValueError(
                        f"IBKR strategy profile {self.profile!r} requires benchmark_history but no candle loader was provided"
                    )
                market_inputs.update(
                    build_benchmark_history_inputs(
                        ib,
                        historical_candle_loader,
                        benchmark_symbol=benchmark_symbol,
                    )
                )
            return market_inputs

        def build_context(request: FeatureSnapshotContextRequest):
            portfolio_snapshot = portfolio_snapshot_holder.get("portfolio_snapshot")
            runtime_adapter = self._runtime_adapter_with_portfolio(
                request.runtime_adapter,
                portfolio_snapshot,
            )
            available_inputs = dict(request.available_inputs)
            if portfolio_snapshot is not None:
                available_inputs[_PORTFOLIO_SNAPSHOT_INPUT] = portfolio_snapshot
            capabilities = {}
            if ib is not None:
                capabilities["broker_client"] = ib
            return build_strategy_context_from_available_inputs(
                entrypoint=request.entrypoint,
                runtime_adapter=runtime_adapter,
                as_of=request.as_of,
                available_inputs=available_inputs,
                runtime_config=request.runtime_config,
                state={"current_holdings": tuple(current_holdings)},
                capabilities=capabilities,
            )

        def log_guard_metadata(guard_metadata: Mapping[str, Any]) -> None:
            self.logger(
                "snapshot_manifest_summary | "
                f"profile={self.profile} decision={guard_metadata.get('snapshot_guard_decision')} "
                f"snapshot_path={guard_metadata.get('snapshot_path')} "
                f"snapshot_as_of={guard_metadata.get('snapshot_as_of')} "
                f"snapshot_age_days={guard_metadata.get('snapshot_age_days')} "
                f"snapshot_file_ts={guard_metadata.get('snapshot_file_timestamp')} "
                f"manifest_path={guard_metadata.get('snapshot_manifest_path')} "
                f"manifest_exists={guard_metadata.get('snapshot_manifest_exists')} "
                f"manifest_contract={guard_metadata.get('snapshot_manifest_contract_version')} "
                f"expected_config={runtime_config_path} "
                f"expected_profile={self.profile}"
            )

        def build_extra_metadata(
            feature_snapshot,
            managed_symbols: tuple[str, ...],
            _decision: StrategyDecision,
        ) -> Mapping[str, Any]:
            return {
                "trade_date": run_as_of.date().isoformat(),
                "dry_run_price_fallbacks": self._build_snapshot_close_map(
                    feature_snapshot,
                    managed_symbols=managed_symbols,
                ),
            }

        result = evaluate_feature_snapshot_strategy(
            entrypoint=self.entrypoint,
            runtime_adapter=self.runtime_adapter,
            runtime_settings=FeatureSnapshotRuntimeSettings(
                feature_snapshot_path=self.runtime_settings.feature_snapshot_path,
                feature_snapshot_manifest_path=self.runtime_settings.feature_snapshot_manifest_path,
                strategy_config_path=self.runtime_settings.strategy_config_path,
                strategy_config_source=self.runtime_settings.strategy_config_source,
                dry_run_only=self.runtime_settings.dry_run_only,
            ),
            runtime_config=runtime_config,
            merged_runtime_config=self.merged_runtime_config,
            as_of=run_as_of,
            base_managed_symbols=(),
            status_icon=self.status_icon,
            default_benchmark_symbol="SPY",
            default_safe_haven_symbol="BOXX",
            build_available_inputs=build_available_inputs,
            context_builder=build_context,
            snapshot_loader=load_feature_snapshot_guarded,
            on_guard_metadata=log_guard_metadata,
            extra_success_metadata=build_extra_metadata,
            catch_evaluation_errors=True,
        )
        return StrategyEvaluationResult(decision=result.decision, metadata=result.metadata)

    def _build_snapshot_close_map(
        self,
        feature_snapshot,
        *,
        managed_symbols: tuple[str, ...],
    ) -> dict[str, float]:
        if not managed_symbols:
            return {}
        try:
            frame = pd.DataFrame(feature_snapshot)
        except Exception:
            return {}
        if frame.empty or "symbol" not in frame.columns or "close" not in frame.columns:
            return {}
        frame = frame.copy()
        frame["symbol"] = frame["symbol"].astype(str).str.strip().str.upper()
        frame = frame[frame["symbol"].isin({str(symbol).strip().upper() for symbol in managed_symbols})]
        if frame.empty:
            return {}
        close_series = pd.to_numeric(frame["close"], errors="coerce")
        frame = frame.assign(close_numeric=close_series)
        frame = frame[frame["close_numeric"].notna() & frame["close_numeric"].gt(0)]
        if frame.empty:
            return {}
        deduped = frame.drop_duplicates(subset=["symbol"], keep="last")
        return {
            str(row["symbol"]): float(row["close_numeric"])
            for _, row in deduped.iterrows()
        }

    def load_runtime_parameters(self) -> dict[str, Any]:
        runtime_loader = self.runtime_adapter.runtime_parameter_loader
        if not callable(runtime_loader):
            return {}
        return dict(
            runtime_loader(
                config_path=self.runtime_settings.strategy_config_path,
                logger=self.logger,
            )
            or {}
        )


def load_strategy_runtime(
    raw_profile: str | None,
    *,
    runtime_settings: PlatformRuntimeSettings,
    logger: Callable[[str], None],
) -> LoadedStrategyRuntime:
    strategy_definition = load_strategy_definition(raw_profile)
    entrypoint = load_strategy_entrypoint_for_profile(strategy_definition.profile)
    runtime_adapter = load_strategy_runtime_adapter_for_profile(strategy_definition.profile)
    runtime = LoadedStrategyRuntime(
        entrypoint=entrypoint,
        runtime_adapter=runtime_adapter,
        runtime_settings=runtime_settings,
        logger=logger,
    )
    runtime_config: dict[str, Any] = {}
    if _FEATURE_SNAPSHOT_INPUT in frozenset(entrypoint.manifest.required_inputs):
        runtime_config = runtime.load_runtime_parameters()

    merged_runtime_config = dict(entrypoint.manifest.default_config)
    merged_runtime_config.update(runtime_config)
    return LoadedStrategyRuntime(
        entrypoint=entrypoint,
        runtime_adapter=runtime_adapter,
        runtime_settings=runtime_settings,
        runtime_config=runtime_config,
        merged_runtime_config=merged_runtime_config,
        status_icon=runtime_adapter.status_icon,
        cash_reserve_ratio=float(
            merged_runtime_config.get(
                "execution_cash_reserve_ratio",
                DEFAULT_CASH_RESERVE_RATIO,
            )
        ),
        logger=logger,
    )
