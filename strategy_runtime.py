from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from application.feature_snapshot_service import load_feature_snapshot_guarded
from quant_platform_kit.strategy_contracts import (
    StrategyContext,
    StrategyDecision,
    StrategyEntrypoint,
    StrategyRuntimeAdapter,
)
from runtime_config_support import PlatformRuntimeSettings
from strategy_loader import (
    load_strategy_definition,
    load_strategy_entrypoint_for_profile,
    load_strategy_runtime_adapter_for_profile,
)


DEFAULT_CASH_RESERVE_RATIO = 0.03
_FEATURE_SNAPSHOT_INPUT = "feature_snapshot"
_HISTORICAL_CLOSE_INPUT = "historical_close_loader"


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

    def evaluate(
        self,
        *,
        ib,
        current_holdings,
        historical_close_loader: Callable[..., Any],
        run_as_of: pd.Timestamp,
        translator: Callable[[str], str],
        pacing_sec: float,
    ) -> StrategyEvaluationResult:
        run_as_of = pd.Timestamp(run_as_of).normalize()
        if _FEATURE_SNAPSHOT_INPUT in self.required_inputs:
            return self._evaluate_feature_snapshot_strategy(
                current_holdings=current_holdings,
                run_as_of=run_as_of,
            )
        if _HISTORICAL_CLOSE_INPUT in self.required_inputs:
            return self._evaluate_market_data_strategy(
                ib=ib,
                current_holdings=current_holdings,
                historical_close_loader=historical_close_loader,
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
        run_as_of: pd.Timestamp,
        translator: Callable[[str], str],
        pacing_sec: float,
    ) -> StrategyEvaluationResult:
        runtime_config = dict(self.runtime_config)
        runtime_config.setdefault("translator", translator)
        runtime_config.setdefault("pacing_sec", float(pacing_sec))
        ctx = StrategyContext(
            as_of=run_as_of,
            market_data={"historical_close_loader": historical_close_loader},
            state={"current_holdings": tuple(current_holdings)},
            runtime_config=runtime_config,
            capabilities={"broker_client": ib},
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
        if safe_haven_symbol:
            metadata["safe_haven_symbol"] = safe_haven_symbol
        return StrategyEvaluationResult(decision=decision, metadata=metadata)

    def _evaluate_feature_snapshot_strategy(
        self,
        *,
        current_holdings,
        run_as_of: pd.Timestamp,
    ) -> StrategyEvaluationResult:
        if not self.runtime_settings.feature_snapshot_path:
            metadata = {
                "strategy_profile": self.profile,
                "feature_snapshot_path": None,
                "strategy_config_path": self.runtime_settings.strategy_config_path,
                "strategy_config_source": self.runtime_settings.strategy_config_source,
                "dry_run_only": self.runtime_settings.dry_run_only,
                "snapshot_guard_decision": "fail_closed",
                "fail_reason": "feature_snapshot_path_missing",
                "managed_symbols": (),
                "status_icon": "🛑",
            }
            decision = StrategyDecision(
                risk_flags=("no_execute",),
                diagnostics={
                    "signal_description": "feature snapshot required",
                    "status_description": "fail_closed | reason=feature_snapshot_path_missing",
                    "actionable": False,
                    "snapshot_guard_decision": "fail_closed",
                    "fail_reason": "feature_snapshot_path_missing",
                },
            )
            return StrategyEvaluationResult(decision=decision, metadata=metadata)

        runtime_config_name = str(
            self.merged_runtime_config.get("runtime_config_name")
            or self.runtime_settings.strategy_profile
        )
        runtime_config_path = self.merged_runtime_config.get("runtime_config_path") or self.runtime_settings.strategy_config_path
        runtime_config_source = self.merged_runtime_config.get("runtime_config_source") or self.runtime_settings.strategy_config_source
        benchmark_symbol = str(self.merged_runtime_config.get("benchmark_symbol") or "SPY").strip().upper()
        safe_haven_symbol = str(self.merged_runtime_config.get("safe_haven") or "BOXX").strip().upper()

        guard_result = load_feature_snapshot_guarded(
            self.runtime_settings.feature_snapshot_path,
            run_as_of=run_as_of,
            required_columns=self._required_feature_columns(),
            snapshot_date_columns=self._snapshot_date_columns(),
            max_snapshot_month_lag=self._max_snapshot_month_lag(),
            manifest_path=self.runtime_settings.feature_snapshot_manifest_path,
            require_manifest=self._require_snapshot_manifest(),
            expected_strategy_profile=self.profile,
            expected_config_name=runtime_config_name,
            expected_config_path=runtime_config_path,
            expected_contract_version=self._snapshot_contract_version(),
        )
        guard_metadata = dict(guard_result.metadata)
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
        if guard_metadata.get("snapshot_guard_decision") != "proceed":
            decision_text = str(guard_metadata.get("snapshot_guard_decision") or "fail_closed")
            reason = guard_metadata.get("fail_reason") or guard_metadata.get("no_op_reason")
            metadata = {
                "strategy_profile": self.profile,
                "strategy_config_path": runtime_config_path,
                "strategy_config_source": runtime_config_source,
                "dry_run_only": self.runtime_settings.dry_run_only,
                "managed_symbols": (),
                "status_icon": "🛑",
                **guard_metadata,
            }
            decision = StrategyDecision(
                risk_flags=("no_execute",),
                diagnostics={
                    "signal_description": "feature snapshot guard blocked execution",
                    "status_description": f"{decision_text} | reason={reason}",
                    "actionable": False,
                    "snapshot_guard_decision": decision_text,
                    "fail_reason": guard_metadata.get("fail_reason"),
                    "no_op_reason": guard_metadata.get("no_op_reason"),
                },
            )
            return StrategyEvaluationResult(decision=decision, metadata=metadata)

        feature_snapshot = guard_result.frame
        managed_symbols = self._extract_managed_symbols(
            feature_snapshot,
            benchmark_symbol=benchmark_symbol,
            safe_haven_symbol=safe_haven_symbol,
        )
        ctx = StrategyContext(
            as_of=run_as_of,
            market_data={"feature_snapshot": feature_snapshot},
            state={"current_holdings": tuple(current_holdings)},
            runtime_config=dict(self.runtime_config),
        )
        try:
            decision = self.entrypoint.evaluate(ctx)
        except Exception as exc:
            fail_reason = f"feature_snapshot_compute_failed:{type(exc).__name__}:{exc}"
            metadata = {
                "strategy_profile": self.profile,
                "strategy_config_path": runtime_config_path,
                "strategy_config_source": runtime_config_source,
                "dry_run_only": self.runtime_settings.dry_run_only,
                "managed_symbols": (),
                "status_icon": "🛑",
                **guard_metadata,
                "snapshot_guard_decision": "fail_closed",
                "fail_reason": fail_reason,
            }
            decision = StrategyDecision(
                risk_flags=("no_execute",),
                diagnostics={
                    "signal_description": "feature snapshot compute failed",
                    "status_description": f"fail_closed | reason={fail_reason}",
                    "actionable": False,
                    "snapshot_guard_decision": "fail_closed",
                    "fail_reason": fail_reason,
                },
            )
            return StrategyEvaluationResult(decision=decision, metadata=metadata)
        metadata = {
            "strategy_profile": self.profile,
            "feature_snapshot_path": self.runtime_settings.feature_snapshot_path,
            "strategy_config_path": runtime_config_path,
            "strategy_config_source": runtime_config_source,
            "safe_haven_symbol": safe_haven_symbol,
            "dry_run_only": self.runtime_settings.dry_run_only,
            "trade_date": run_as_of.date().isoformat(),
            "managed_symbols": managed_symbols,
            "status_icon": self.status_icon,
            **guard_metadata,
        }
        return StrategyEvaluationResult(decision=decision, metadata=metadata)

    def _extract_managed_symbols(
        self,
        feature_snapshot,
        *,
        benchmark_symbol: str,
        safe_haven_symbol: str,
    ) -> tuple[str, ...]:
        extractor = self.runtime_adapter.managed_symbols_extractor
        if extractor is None:
            if safe_haven_symbol:
                return (safe_haven_symbol,)
            return ()
        if callable(extractor):
            return tuple(
                extractor(
                    feature_snapshot,
                    benchmark_symbol=benchmark_symbol,
                    safe_haven=safe_haven_symbol,
                )
            )
        if safe_haven_symbol:
            return (safe_haven_symbol,)
        return ()

    def _required_feature_columns(self) -> tuple[str, ...] | frozenset[str]:
        return self.runtime_adapter.required_feature_columns

    def _snapshot_date_columns(self) -> tuple[str, ...]:
        return tuple(self.runtime_adapter.snapshot_date_columns)

    def _max_snapshot_month_lag(self) -> int:
        return int(self.runtime_adapter.max_snapshot_month_lag)

    def _require_snapshot_manifest(self) -> bool:
        return bool(self.runtime_adapter.require_snapshot_manifest)

    def _snapshot_contract_version(self) -> str | None:
        return self.runtime_adapter.snapshot_contract_version

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
