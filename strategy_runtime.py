from __future__ import annotations

from collections.abc import Callable, Mapping
from importlib import metadata as importlib_metadata
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pandas as pd

from quant_platform_kit.common.feature_snapshot import load_feature_snapshot_guarded
from quant_platform_kit.common.feature_snapshot_runtime import (
    FeatureSnapshotContextRequest,
    FeatureSnapshotRuntimeSettings,
    evaluate_feature_snapshot_strategy,
)
from quant_platform_kit.common.capital_base import (
    CapitalBaseBinding,
    CapitalScope,
    CapitalValuationBasis,
    build_capital_base_snapshot,
)
from quant_platform_kit.common.strategy_plugins import attach_strategy_plugin_metadata
from quant_platform_kit.ibkr import (
    build_ibkr_strategy_context,
    build_benchmark_history_inputs,
    build_market_history_inputs,
    build_semiconductor_rotation_inputs,
    fetch_option_chain_snapshot,
)
from application.ibkr_portfolio import fetch_portfolio_snapshot
from quant_platform_kit.common.strategy_contracts import (
    StrategyDecision,
    StrategyEntrypoint,
    StrategyRuntimeAdapter,
    apply_runtime_policy_to_runtime_config,
    build_execution_timing_metadata,
    build_strategy_context_from_available_inputs,
)
from quant_platform_kit.common.runtime_inputs import (
    build_account_state_from_portfolio_snapshot,
    build_portfolio_snapshot_from_account_state,
    build_strategy_evaluation_inputs,
)
from quant_platform_kit.risk.contracts import RuntimeRiskLimits, SmallAccountRiskHoldPolicy
from runtime_config_support import PlatformRuntimeSettings
from us_equity_strategies.signals import resolve_external_market_signal_inputs
from strategy_loader import (
    load_strategy_definition,
    load_strategy_entrypoint_for_profile,
    load_strategy_runtime_adapter_for_profile,
)



def _parse_small_account_hold_policy(raw: Any) -> SmallAccountRiskHoldPolicy | None:
    """Parse optional deployment hold policy; invalid shapes return None."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        return None
    try:
        return SmallAccountRiskHoldPolicy(
            enabled=raw["enabled"],
            hold_below_nav=raw["hold_below_nav"],
            require_cash_only=raw.get("require_cash_only", True),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _installed_ues_revision() -> str | None:
    """Read the VCS revision of the installed UES distribution."""
    candidates: list[str] = []
    try:
        distribution = importlib_metadata.distribution("us-equity-strategies")
        raw_direct_url = distribution.read_text("direct_url.json")
        if raw_direct_url:
            candidates.append(raw_direct_url)
        locate_file = getattr(distribution, "locate_file", None)
        if callable(locate_file):
            direct_url_path = Path(str(locate_file("direct_url.json")))
            if direct_url_path.is_file():
                candidates.append(direct_url_path.read_text(encoding="utf-8"))
    except (ImportError, OSError, TypeError, ValueError, AttributeError):
        pass
    stamped = Path("/app/UES_REVISION")
    if stamped.is_file():
        stamped_revision = stamped.read_text(encoding="utf-8").strip()
        if stamped_revision:
            return stamped_revision
    for raw_direct_url in candidates:
        try:
            payload = json.loads(raw_direct_url)
        except (TypeError, ValueError):
            continue
        revision = payload.get("vcs_info", {}).get("commit_id")
        if isinstance(revision, str) and revision.strip():
            return revision.strip()
    return None


def _runtime_risk_binding_mismatch_reasons(
    *,
    policy_binding: Mapping[str, Any],
    policy: Mapping[str, Any],
    account_scope: str,
    runtime_scope: str,
    actual_account_hash: str,
    profile: str,
    target_release: Any,
    actual_ues_revision: str | None,
    execution_mode: str,
    cash_only_execution: bool,
    reserved_cash_ratio: Any,
    merged_runtime_config: Mapping[str, Any],
    actual_exit_buffer: Any,
) -> tuple[str, ...]:
    """Return stable reason codes for SOXL runtime-risk binding mismatches."""
    reasons: list[str] = []
    if not account_scope:
        reasons.append("missing_account_scope")
    if not runtime_scope:
        reasons.append("missing_runtime_scope")
    if not actual_account_hash:
        reasons.append("missing_account_hash")
    if target_release is None:
        reasons.append("missing_strategy_release")
    if str(policy_binding.get("account_scope") or "").strip() != account_scope:
        reasons.append("account_scope_mismatch")
    if str(policy_binding.get("runtime_scope") or "").strip() != runtime_scope:
        reasons.append("runtime_scope_mismatch")
    if str(policy_binding.get("account_hash") or "").strip() != actual_account_hash:
        reasons.append("account_hash_mismatch")
    if str(policy_binding.get("strategy_profile") or "").strip() != profile:
        reasons.append("strategy_profile_mismatch")
    expected_ues = str(policy_binding.get("ues_revision") or "").strip()
    release_ues = (
        str(getattr(target_release, "strategy_revision", "") or "").strip()
        if target_release is not None
        else ""
    )
    if expected_ues != release_ues:
        reasons.append("ues_revision_release_mismatch")
    if actual_ues_revision is None:
        reasons.append("ues_revision_unavailable")
    elif actual_ues_revision != expected_ues:
        reasons.append("ues_revision_installed_mismatch")
    if str(policy_binding.get("execution_mode") or "").strip().lower() != execution_mode:
        reasons.append("execution_mode_mismatch")
    if policy_binding.get("cash_only_execution") is not True:
        reasons.append("policy_cash_only_mismatch")
    if cash_only_execution is not True:
        reasons.append("settings_cash_only_mismatch")
    if policy_binding.get("reserved_cash_ratio") != merged_runtime_config.get("cash_reserve_ratio"):
        reasons.append("reserved_cash_merged_mismatch")
    if policy_binding.get("reserved_cash_ratio") != reserved_cash_ratio:
        reasons.append("reserved_cash_settings_mismatch")
    if policy_binding.get("reserved_cash_ratio") != 0.03:
        reasons.append("reserved_cash_ratio_mismatch")
    if policy_binding.get("options_enabled") is not False:
        reasons.append("options_enabled_mismatch")
    if any(
        merged_runtime_config.get(key) is not False
        for key in (
            "option_overlay_enabled",
            "option_growth_overlay_enabled",
            "option_income_overlay_enabled",
        )
    ):
        reasons.append("option_overlay_mismatch")
    if not isinstance(policy.get("exit_parameters"), Mapping):
        reasons.append("exit_parameters_missing")
    if actual_exit_buffer is None:
        reasons.append("trend_exit_buffer_missing")
    elif actual_exit_buffer != 0.02:
        reasons.append("trend_exit_buffer_mismatch")
    if dict(policy.get("exit_parameters") or {}) != {"trend_exit_buffer": 0.02}:
        reasons.append("exit_parameters_mismatch")
    elif dict(policy.get("exit_parameters") or {}) != {"trend_exit_buffer": actual_exit_buffer}:
        reasons.append("exit_parameters_buffer_mismatch")
    return tuple(reasons)


DEFAULT_CASH_RESERVE_RATIO = 0.0
DEFAULT_REBALANCE_THRESHOLD_RATIO = 0.02
_FEATURE_SNAPSHOT_INPUT = "feature_snapshot"
DCA_PROFILES = frozenset({"nasdaq_sp500_smart_dca", "ibit_smart_dca"})
IBIT_ZSCORE_EXIT_PROFILE = "ibit_smart_dca"
_MARKET_HISTORY_INPUT = "market_history"
_MARKET_DATA_INPUT = "market_data"
_BENCHMARK_HISTORY_INPUT = "benchmark_history"
_DERIVED_INDICATORS_INPUT = "derived_indicators"
_PORTFOLIO_SNAPSHOT_INPUT = "portfolio_snapshot"
_SOXL_PROFILE = "soxl_soxx_trend_income"

_OPTION_CHAIN_FETCH_RULES = {
    "tqqq_leaps_growth_v1": {
        "underlier": "TQQQ",
        "rights": ("C",),
        "min_dte": 540,
        "max_dte": 930,
        "target_dte": 730,
        "strike_range_pct": (0.45, 1.30),
        "max_expirations": 3,
        "max_contracts": 72,
    },
    "qqq_leaps_growth_v1": {
        "underlier": "QQQ",
        "rights": ("C",),
        "min_dte": 540,
        "max_dte": 930,
        "target_dte": 730,
        "strike_range_pct": (0.45, 1.30),
        "max_expirations": 3,
        "max_contracts": 72,
    },
    "soxx_put_credit_spread_income_v1": {
        "underlier": "SOXX",
        "rights": ("P",),
        "min_dte": 25,
        "max_dte": 65,
        "target_dte": 45,
        "strike_range_pct": (0.65, 1.02),
        "max_expirations": 3,
        "max_contracts": 72,
    },
}


def _get_direct_market_history_profiles() -> frozenset[str]:
    try:
        from hk_equity_strategies import get_direct_market_history_profiles
    except (ImportError, AttributeError):  # pragma: no cover - compatibility fallback
        return frozenset()
    return frozenset(
        str(profile).strip().lower()
        for profile in get_direct_market_history_profiles()
    )


def _requires_materialized_market_history(strategy_profile: str) -> bool:
    return str(strategy_profile or "").strip().lower() in _get_direct_market_history_profiles()


def _loaded_history_to_rows(history):
    if (
        hasattr(history, "items")
        and not hasattr(history, "columns")
        and not isinstance(history, Mapping)
    ):
        return [
            {"date": date_value, "close": close_value}
            for date_value, close_value in history.items()
        ]
    return history


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
    rebalance_threshold_ratio: float = DEFAULT_REBALANCE_THRESHOLD_RATIO
    cash_reserve_floor_usd: float = 0.0
    logger: Callable[[str], None] = print
    _last_capability_status: Mapping[str, str] | None = field(default=None, repr=False, compare=False)

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
        account_ids = tuple(self.runtime_settings.account_ids or ())
        snapshot_kwargs = {
            "currency": str(getattr(self.runtime_settings, "market_currency", "USD") or "USD"),
            "cash_only_execution": bool(
                getattr(self.runtime_settings, "cash_only_execution", True)
            ),
        }
        if required:
            if account_ids:
                return fetch_portfolio_snapshot(ib, account_ids=account_ids, **snapshot_kwargs)
            return fetch_portfolio_snapshot(ib, **snapshot_kwargs)
        try:
            if account_ids:
                return fetch_portfolio_snapshot(ib, account_ids=account_ids, **snapshot_kwargs)
            return fetch_portfolio_snapshot(ib, **snapshot_kwargs)
        except Exception as exc:
            self.logger(
                "strategy_dashboard_portfolio_snapshot_failed | "
                f"profile={self.profile} error_type={type(exc).__name__} error={exc}"
            )
            return None

    @staticmethod
    def _attach_strategy_plugin_metadata(
        portfolio_snapshot: Any | None,
        strategy_plugin_signals,
    ) -> Any | None:
        if portfolio_snapshot is None or not strategy_plugin_signals:
            return portfolio_snapshot
        return attach_strategy_plugin_metadata(portfolio_snapshot, tuple(strategy_plugin_signals or ()))

    def _with_consecutive_loss_metadata(self, portfolio_snapshot: Any | None) -> Any | None:
        """Stamp trailing consecutive_losses onto portfolio metadata before evaluate."""
        if portfolio_snapshot is None:
            return None
        metadata = dict(getattr(portfolio_snapshot, "metadata", None) or {})
        if metadata.get("consecutive_losses") is not None:
            return portfolio_snapshot
        try:
            from quant_platform_kit.strategy_lifecycle.live_equity import resolve_consecutive_losses
            from quant_platform_kit.strategy_lifecycle.performance_monitor import infer_strategy_domain

            streak = resolve_consecutive_losses(
                domain=infer_strategy_domain(self.profile),
                strategy_profile=self.profile,
            )
        except Exception as exc:
            self.logger(
                "strategy_consecutive_losses_resolve_failed | "
                f"profile={self.profile} error_type={type(exc).__name__} error={exc}"
            )
            return portfolio_snapshot
        if streak is None:
            return portfolio_snapshot
        metadata["consecutive_losses"] = int(streak)
        return replace(portfolio_snapshot, metadata=metadata)

    def _build_capital_base_capabilities(
        self,
        portfolio_snapshot: Any | None,
    ) -> tuple[dict[str, Any], str]:
        """Attach strict v2 evidence only to an explicitly scoped IBKR snapshot."""

        runtime_target = self.runtime_settings.runtime_target
        if portfolio_snapshot is None:
            return {}, "unavailable:portfolio_snapshot"
        if runtime_target is None:
            return {}, "unavailable:runtime_target"
        account_scope = str(runtime_target.account_scope or "").strip()
        runtime_scope = str(
            runtime_target.service_name or runtime_target.deployment_selector or ""
        ).strip()
        if not account_scope or not runtime_scope:
            return {}, "unavailable:runtime_scope"
        metadata = getattr(portfolio_snapshot, "metadata", {})
        if not isinstance(metadata, Mapping):
            return {}, "unavailable:portfolio_metadata"
        if metadata.get("total_equity_source") != "broker_net_liquidation":
            return {}, "unavailable:unverified_net_liquidation"
        source_digest = str(metadata.get("source_digest_sha256") or "").strip()
        if not source_digest:
            return {}, "unavailable:source_digest"
        try:
            from dataclasses import replace as dc_replace

            capital_snapshot = portfolio_snapshot
            broker_nlv = metadata.get("broker_net_liquidation")
            if broker_nlv is not None:
                capital_snapshot = dc_replace(
                    portfolio_snapshot,
                    total_equity=float(broker_nlv),
                )
            capital_base = build_capital_base_snapshot(
                capital_snapshot,
                account_scope=account_scope,
                runtime_scope=runtime_scope,
                strategy_scope=self.profile,
                reported_currency="USD",
                target_currency="USD",
                fx_rate_to_target=1.0,
                source_digest_sha256=source_digest,
                capital_scope=CapitalScope.ACCOUNT,
                valuation_basis=CapitalValuationBasis.BROKER_ACCOUNT_NET_LIQUIDATION,
            )
            binding = CapitalBaseBinding(
                account_scope=account_scope,
                runtime_scope=runtime_scope,
                strategy_scope=self.profile,
                target_currency="USD",
                capital_scope=CapitalScope.ACCOUNT,
                valuation_basis=CapitalValuationBasis.BROKER_ACCOUNT_NET_LIQUIDATION,
            )
        except (TypeError, ValueError):
            return {}, "unavailable:invalid_capital_evidence"
        return {
            "capital_base": capital_base,
            "capital_base_binding": binding,
        }, "verified:broker_account_net_liquidation"


    def _build_runtime_risk_capabilities(
        self,
        available_inputs: Mapping[str, Any],
        capabilities: Mapping[str, Any],
    ) -> tuple[dict[str, Any], str]:
        """Bind explicit limits to the deployed account and installed UES."""
        if self.profile != _SOXL_PROFILE:
            return dict(capabilities), "unavailable:profile_not_supported"
        policy = self.runtime_settings.trusted_runtime_risk_policy
        runtime_target = self.runtime_settings.runtime_target
        snapshot = available_inputs.get("portfolio_snapshot")
        binding = capabilities.get("capital_base_binding")
        if not isinstance(policy, Mapping):
            return {**capabilities, "runtime_risk_limits": object()}, "unavailable:runtime_risk_policy"
        if runtime_target is None or snapshot is None or binding is None:
            return {**capabilities, "runtime_risk_limits": object()}, "unavailable:runtime_binding"
        expected_policy_keys = {
            "binding",
            "allowed_symbols",
            "product_leverage_factors",
            "nominal_caps",
            "total_nominal_exposure_cap",
            "total_effective_exposure_cap",
            "max_positions",
            "exit_parameters",
        }
        optional_policy_keys = {"small_account_hold"}
        policy_keys = set(policy)
        if (
            not expected_policy_keys.issubset(policy_keys)
            or (policy_keys - expected_policy_keys - optional_policy_keys)
            or not isinstance(policy.get("binding"), Mapping)
        ):
            return {**capabilities, "runtime_risk_limits": object()}, "unavailable:invalid_runtime_risk_policy"

        target_release = runtime_target.strategy_release
        policy_binding = policy["binding"]
        expected_binding_keys = {
            "account_scope",
            "runtime_scope",
            "account_hash",
            "strategy_profile",
            "ues_revision",
            "execution_mode",
            "cash_only_execution",
            "reserved_cash_ratio",
            "options_enabled",
        }
        if set(policy_binding) != expected_binding_keys:
            return {**capabilities, "runtime_risk_limits": object()}, "unavailable:invalid_runtime_binding"
        metadata = getattr(snapshot, "metadata", {})
        account_scope = str(runtime_target.account_scope or "").strip()
        runtime_scope = str(runtime_target.service_name or runtime_target.deployment_selector or "").strip()
        actual_account_hash = str(metadata.get("account_hash") or "").strip() if isinstance(metadata, Mapping) else ""
        actual_ues_revision = _installed_ues_revision()
        actual_exit_buffer = self.merged_runtime_config.get("trend_exit_buffer")
        mismatch_reasons = _runtime_risk_binding_mismatch_reasons(
            policy_binding=policy_binding,
            policy=policy,
            account_scope=account_scope,
            runtime_scope=runtime_scope,
            actual_account_hash=actual_account_hash,
            profile=self.profile,
            target_release=target_release,
            actual_ues_revision=actual_ues_revision,
            execution_mode=str(runtime_target.execution_mode or ""),
            cash_only_execution=self.runtime_settings.cash_only_execution,
            reserved_cash_ratio=self.runtime_settings.reserved_cash_ratio,
            merged_runtime_config=self.merged_runtime_config,
            actual_exit_buffer=actual_exit_buffer,
        )
        if mismatch_reasons:
            self.logger(
                "strategy_runtime_binding_mismatch | "
                f"profile={self.profile} reasons={','.join(mismatch_reasons)} "
                f"installed_ues_revision={actual_ues_revision!r} "
                f"policy_ues_revision={str(policy_binding.get('ues_revision') or '').strip()!r} "
                f"account_hash={actual_account_hash!r} "
                f"trend_exit_buffer={actual_exit_buffer!r}"
            )
            return {**capabilities, "runtime_risk_limits": object()}, "unavailable:runtime_binding_mismatch"
        try:
            limits = RuntimeRiskLimits(
                allowed_symbols=tuple(policy["allowed_symbols"]),
                product_leverage_factors=policy["product_leverage_factors"],
                nominal_caps=policy["nominal_caps"],
                total_nominal_exposure_cap=policy["total_nominal_exposure_cap"],
                total_effective_exposure_cap=policy["total_effective_exposure_cap"],
                max_positions=policy["max_positions"],
            )
        except (TypeError, ValueError):
            return {**capabilities, "runtime_risk_limits": object()}, "unavailable:invalid_runtime_risk_limits"
        capability_payload: dict[str, Any] = {
            **capabilities,
            "runtime_risk_limits": limits,
            "cash_only_execution": bool(self.runtime_settings.cash_only_execution),
        }
        hold_policy = _parse_small_account_hold_policy(policy.get("small_account_hold"))
        if isinstance(policy.get("small_account_hold"), Mapping) and hold_policy is None:
            return {
                **capabilities,
                "runtime_risk_limits": object(),
            }, "unavailable:invalid_small_account_hold"
        if hold_policy is not None:
            if hold_policy.require_cash_only and self.runtime_settings.cash_only_execution is not True:
                return {
                    **capabilities,
                    "runtime_risk_limits": object(),
                }, "unavailable:small_account_hold_cash_only"
            capability_payload["small_account_hold_policy"] = hold_policy
        return capability_payload, "verified:runtime_risk_limits"

    def _build_context_capabilities(
        self,
        *,
        ib: Any | None,
        portfolio_snapshot: Any | None,
    ) -> dict[str, Any]:
        capabilities: dict[str, Any] = {}
        if ib is not None:
            capabilities["broker_client"] = ib
        capital_base_capabilities, capital_status = self._build_capital_base_capabilities(
            portfolio_snapshot
        )
        capabilities.update(capital_base_capabilities)
        capabilities, risk_status = self._build_runtime_risk_capabilities(
            {"portfolio_snapshot": portfolio_snapshot},
            capabilities,
        )
        status = {
            "capital_base_status": capital_status,
            "runtime_risk_status": risk_status,
        }
        object.__setattr__(self, "_last_capability_status", status)
        if not (
            capital_status.startswith("verified:")
            and risk_status.startswith("verified:")
        ):
            self.logger(
                "strategy_runtime_capability_status | "
                f"profile={self.profile} capital_base_status={capital_status} "
                f"runtime_risk_status={risk_status}"
            )
        return capabilities

    def _capability_status_metadata(self, portfolio_snapshot: Any | None = None) -> dict[str, str]:
        status = getattr(self, "_last_capability_status", None)
        if isinstance(status, dict):
            return dict(status)
        capital_status = self._build_capital_base_capabilities(portfolio_snapshot)[1]
        return {
            "capital_base_status": capital_status,
            "runtime_risk_status": "unavailable:runtime_risk_policy",
        }

    def _prepare_portfolio_snapshot(
        self,
        portfolio_snapshot: Any | None,
        strategy_symbols=(),
        strategy_plugin_signals=(),
        *,
        project: bool = True,
    ) -> Any | None:
        snapshot = portfolio_snapshot
        if project:
            snapshot = self._project_portfolio_snapshot(snapshot, strategy_symbols)
        snapshot = self._attach_strategy_plugin_metadata(snapshot, strategy_plugin_signals)
        return self._with_consecutive_loss_metadata(snapshot)

    @staticmethod
    def _normalize_symbols(symbols) -> tuple[str, ...]:
        normalized = []
        for symbol in symbols or ():
            text = str(symbol or "").strip().upper()
            if text:
                normalized.append(text)
        return tuple(dict.fromkeys(normalized))

    def _build_price_fallback_symbol_list(
        self,
        decision: StrategyDecision,
        *,
        managed_symbols: tuple[str, ...],
        current_holdings,
    ) -> tuple[str, ...]:
        decision_symbols = [getattr(position, "symbol", "") for position in decision.positions]
        candidates = [
            *decision_symbols,
            *(current_holdings or ()),
        ]
        if not candidates:
            candidates.extend(managed_symbols)
        return self._normalize_symbols(candidates)

    @staticmethod
    def _extract_latest_positive_close(price_history) -> float | None:
        if price_history is None:
            return None
        if isinstance(price_history, pd.DataFrame):
            if price_history.empty:
                return None
            if "close" in price_history.columns:
                series = price_history["close"]
            else:
                series = price_history.iloc[:, 0]
            values = pd.to_numeric(series, errors="coerce").dropna()
            values = values[values > 0]
            if values.empty:
                return None
            return float(values.iloc[-1])
        if isinstance(price_history, pd.Series):
            values = pd.to_numeric(price_history, errors="coerce").dropna()
            values = values[values > 0]
            if values.empty:
                return None
            return float(values.iloc[-1])
        values = []
        try:
            iterator = iter(price_history)
        except TypeError:
            iterator = iter((price_history,))
        for item in iterator:
            if isinstance(item, Mapping):
                candidate = item.get("close")
            else:
                candidate = getattr(item, "close", item)
            try:
                numeric = float(candidate)
            except (TypeError, ValueError):
                continue
            if numeric > 0.0:
                values.append(numeric)
        return float(values[-1]) if values else None

    @staticmethod
    def _as_bool(value: Any, *, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "y", "on"}:
                return True
            if normalized in {"0", "false", "no", "n", "off"}:
                return False
        return bool(value)

    @staticmethod
    def _option_overlay_recipe_live_allowed(recipe: str) -> bool:
        try:
            from us_equity_strategies.option_overlay import OPTION_OVERLAY_RESEARCH_CANDIDATES
        except Exception:
            return False
        candidate = OPTION_OVERLAY_RESEARCH_CANDIDATES.get(str(recipe or "").strip())
        if not isinstance(candidate, Mapping):
            return False
        return (
            str(candidate.get("status") or "").strip().lower() == "live"
            and candidate.get("promotion_evidence") is True
        )

    def _active_option_overlay_recipes(
        self,
        runtime_config: Mapping[str, Any],
        portfolio_snapshot: Any | None,
    ) -> tuple[str, ...]:
        total_equity = float(getattr(portfolio_snapshot, "total_equity", 0.0) or 0.0)
        recipes = []
        for family in ("growth", "income"):
            prefix = f"option_{family}_overlay"
            if not self._as_bool(runtime_config.get(f"{prefix}_enabled"), default=False):
                continue
            recipe = str(runtime_config.get(f"{prefix}_recipe") or "").strip()
            if recipe not in _OPTION_CHAIN_FETCH_RULES:
                continue
            if not self._option_overlay_recipe_live_allowed(recipe):
                continue
            start_usd = float(runtime_config.get(f"{prefix}_start_usd") or 0.0)
            if portfolio_snapshot is None or total_equity < max(0.0, start_usd):
                continue
            recipes.append(recipe)
        return tuple(dict.fromkeys(recipes))

    def _fetch_option_chains_for_runtime(
        self,
        ib,
        runtime_config: Mapping[str, Any],
        portfolio_snapshot: Any | None,
    ) -> dict[str, Any]:
        if ib is None:
            return {}
        overlay_config = dict(self.merged_runtime_config)
        overlay_config.update(dict(runtime_config or {}))
        chains: dict[str, Any] = {}
        for recipe in self._active_option_overlay_recipes(overlay_config, portfolio_snapshot):
            rule = _OPTION_CHAIN_FETCH_RULES[recipe]
            underlier = str(rule["underlier"])
            try:
                chains[underlier] = fetch_option_chain_snapshot(
                    ib,
                    underlier,
                    rights=tuple(rule["rights"]),
                    min_dte=int(rule["min_dte"]),
                    max_dte=int(rule["max_dte"]),
                    target_dte=int(rule["target_dte"]),
                    max_expirations=int(rule["max_expirations"]),
                    strike_range_pct=tuple(rule["strike_range_pct"]),
                    max_contracts=int(rule["max_contracts"]),
                )
            except Exception as exc:
                self.logger(
                    "option_chain_fetch_failed | "
                    f"profile={self.profile} recipe={recipe} underlier={underlier} "
                    f"error_type={type(exc).__name__} error={exc}"
                )
        return chains

    def _build_historical_close_map(
        self,
        ib,
        historical_close_loader: Callable[..., Any],
        symbols: tuple[str, ...],
    ) -> dict[str, float]:
        close_map: dict[str, float] = {}
        for symbol in self._normalize_symbols(symbols):
            try:
                price_history = historical_close_loader(
                    ib,
                    symbol,
                    duration="10 D",
                    bar_size="1 day",
                )
            except Exception as exc:
                self.logger(
                    "historical_price_fallback_failed | "
                    f"profile={self.profile} symbol={symbol} error_type={type(exc).__name__} error={exc}"
                )
                continue
            latest_close = self._extract_latest_positive_close(price_history)
            if latest_close is not None:
                close_map[symbol] = latest_close
        return close_map

    def _market_history_symbols(self) -> tuple[str, ...]:
        raw_symbols = (
            self.merged_runtime_config.get("universe_symbols")
            or dict(getattr(self.entrypoint.manifest, "default_config", {}) or {}).get("universe_symbols")
            or self.merged_runtime_config.get("managed_symbols")
            or ()
        )
        if isinstance(raw_symbols, str):
            raw_symbols = raw_symbols.replace(";", ",").split(",")
        return tuple(
            dict.fromkeys(
                str(symbol).strip()
                for symbol in raw_symbols
                if str(symbol).strip()
            )
        )

    def _configured_strategy_symbols(self, *, include_ranking_pool: bool = False) -> tuple[str, ...]:
        candidates: list[str] = []
        raw_managed = self.merged_runtime_config.get("managed_symbols", ())
        if isinstance(raw_managed, str):
            raw_managed = raw_managed.replace(";", ",").split(",")
        candidates.extend(str(symbol) for symbol in raw_managed or ())
        if include_ranking_pool:
            raw_pool = self.merged_runtime_config.get("ranking_pool", ())
            if isinstance(raw_pool, str):
                raw_pool = raw_pool.replace(";", ",").split(",")
            candidates.extend(str(symbol) for symbol in raw_pool or ())
        safe_haven_symbol = str(self.merged_runtime_config.get("safe_haven") or "").strip()
        if safe_haven_symbol and candidates:
            candidates.append(safe_haven_symbol)
        return tuple(
            dict.fromkeys(
                symbol.strip().upper()
                for symbol in candidates
                if symbol.strip()
            )
        )

    def _project_portfolio_snapshot(self, portfolio_snapshot: Any | None, strategy_symbols) -> Any | None:
        if portfolio_snapshot is None or not strategy_symbols:
            return portfolio_snapshot
        if not hasattr(portfolio_snapshot, "positions"):
            return portfolio_snapshot
        from us_equity_strategies.cash_only_equity import (
            normalize_account_state_from_snapshot,
        )

        account_state = build_account_state_from_portfolio_snapshot(
            portfolio_snapshot,
            strategy_symbols=strategy_symbols,
        )
        account_state = normalize_account_state_from_snapshot(
            account_state,
            portfolio_snapshot,
            cash_only_execution=getattr(self.runtime_settings, "cash_only_execution", True),
        )
        return build_portfolio_snapshot_from_account_state(
            account_state,
            strategy_symbols=strategy_symbols,
            as_of=getattr(portfolio_snapshot, "as_of", None),
            metadata=getattr(portfolio_snapshot, "metadata", {}) or {},
        )

    def _enrich_portfolio_metadata(
        self,
        metadata: dict[str, Any],
        portfolio_snapshot: Any | None,
    ) -> dict[str, Any]:
        if portfolio_snapshot is None:
            return metadata
        from us_equity_strategies.cash_only_equity import resolve_raw_cash_from_snapshot

        enriched = dict(metadata)
        enriched["cash_only_execution"] = bool(getattr(self.runtime_settings, "cash_only_execution", True))
        enriched["portfolio_total_equity"] = float(getattr(portfolio_snapshot, "total_equity", 0.0) or 0.0)
        enriched["liquid_cash"] = resolve_raw_cash_from_snapshot(portfolio_snapshot)
        enriched["market_values"] = {
            str(getattr(position, "symbol", "") or "").strip().upper(): float(
                getattr(position, "market_value", 0.0) or 0.0
            )
            for position in getattr(portfolio_snapshot, "positions", ()) or ()
            if str(getattr(position, "symbol", "") or "").strip()
        }
        from application.portfolio_risk_diagnostics import extract_portfolio_risk_diagnostics

        enriched.update(extract_portfolio_risk_diagnostics(portfolio_snapshot))
        return enriched

    def _build_market_history_inputs(
        self,
        ib,
        historical_close_loader: Callable[..., Any],
    ) -> Mapping[str, Any]:
        if not _requires_materialized_market_history(self.profile):
            return build_market_history_inputs(historical_close_loader)
        return {
            _MARKET_HISTORY_INPUT: {
                symbol: _loaded_history_to_rows(historical_close_loader(ib, symbol))
                for symbol in self._market_history_symbols()
            }
        }

    def _build_direct_market_data_inputs(
        self,
        ib,
        historical_close_loader: Callable[..., Any],
    ) -> Mapping[str, Any]:
        if self.profile != "us_equity_combo_leveraged":
            raise ValueError(f"Unsupported market_data strategy profile {self.profile!r}")

        trend_symbol = str(self.merged_runtime_config.get("market_data_trend_symbol") or "SPY").strip().upper()
        raw_regime_symbols = self.merged_runtime_config.get("market_data_regime_symbols") or ("SPY", "QQQ", "SOXX")
        if isinstance(raw_regime_symbols, str):
            regime_symbols = tuple(
                symbol.strip().upper()
                for symbol in raw_regime_symbols.split(",")
                if symbol.strip()
            )
        else:
            regime_symbols = tuple(
                str(symbol).strip().upper()
                for symbol in raw_regime_symbols
                if str(symbol).strip()
            )
        regime_symbols = tuple(dict.fromkeys((trend_symbol, *regime_symbols)))
        ma_window = int(
            self.merged_runtime_config.get(
                "market_data_ma_window",
                self.merged_runtime_config.get("sma_period", 200),
            )
        )
        slope_window = int(self.merged_runtime_config.get("market_data_slope_window") or 20)
        if ma_window <= 0:
            raise ValueError("market_data_ma_window must be positive")
        if slope_window <= 1:
            raise ValueError("market_data_slope_window must be greater than 1")

        def load_close_series(symbol: str) -> pd.Series:
            history = historical_close_loader(
                ib,
                symbol,
                duration=str(self.merged_runtime_config.get("market_data_history_duration") or "2 Y"),
                bar_size=str(self.merged_runtime_config.get("market_data_history_bar_size") or "1 day"),
            )
            if isinstance(history, pd.DataFrame):
                if "close" in history.columns:
                    close_values = history["close"]
                else:
                    close_values = history.iloc[:, 0]
            elif isinstance(history, pd.Series):
                close_values = history
            else:
                close_values = [
                    item.get("close") if isinstance(item, Mapping) else getattr(item, "close", item)
                    for item in (history or ())
                ]
            close_series = pd.to_numeric(pd.Series(close_values), errors="coerce").dropna()
            close_series = close_series[close_series > 0]
            if len(close_series) < ma_window:
                raise ValueError(
                    f"{self.profile} requires at least {ma_window} positive {symbol} closes, "
                    f"got {len(close_series)}"
                )
            return close_series

        market_data: dict[str, Any] = {
            "trend_symbol": trend_symbol,
            "trend_symbols": regime_symbols,
            "trend_ma_window": ma_window,
            "trend_slope_window": slope_window,
            "regime_indicators": {},
        }
        for symbol in regime_symbols:
            close_series = load_close_series(symbol)
            price = float(close_series.iloc[-1])
            ma_value = float(close_series.tail(ma_window).mean())
            rolling_ma = close_series.rolling(slope_window).mean()
            slope_positive = bool(len(rolling_ma.dropna()) >= 2 and rolling_ma.iloc[-1] > rolling_ma.iloc[-2])
            key = symbol.lower()
            market_data[f"{key}_above_ma200"] = price > ma_value
            market_data[f"{key}_price"] = price
            market_data[f"{key}_ma200"] = ma_value
            market_data[f"{key}_ma20_slope_positive"] = slope_positive
            market_data["regime_indicators"][key] = {
                "price": price,
                "ma200": ma_value,
                "above_ma200": price > ma_value,
                "ma20_slope_positive": slope_positive,
                "history_observation_count": int(len(close_series)),
            }
            if symbol == trend_symbol:
                market_data.update(
                    {
                        "trend_price": price,
                        "trend_ma": ma_value,
                        "history_observation_count": int(len(close_series)),
                    }
                )
        return {
            _MARKET_DATA_INPUT: market_data,
        }

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
        if portfolio_snapshot is not None and context_adapter.portfolio_input_name:
            available_inputs.add(context_adapter.portfolio_input_name)
        evaluation_inputs = build_strategy_evaluation_inputs(
            available_inputs=available_inputs,
            market_inputs=market_inputs,
            portfolio_snapshot=portfolio_snapshot,
        )
        capabilities = self._build_context_capabilities(
            ib=ib,
            portfolio_snapshot=portfolio_snapshot,
        )
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
        strategy_plugin_signals=(),
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
                strategy_plugin_signals=strategy_plugin_signals,
            )
        if _MARKET_DATA_INPUT in self.required_inputs:
            return self._evaluate_direct_market_data_strategy(
                ib=ib,
                current_holdings=current_holdings,
                historical_close_loader=historical_close_loader,
                run_as_of=run_as_of,
                translator=translator,
                pacing_sec=pacing_sec,
                strategy_plugin_signals=strategy_plugin_signals,
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
                strategy_plugin_signals=strategy_plugin_signals,
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
                strategy_plugin_signals=strategy_plugin_signals,
            )
        raise ValueError(
            f"Unsupported required_inputs for IBKR strategy profile {self.profile!r}: "
            f"{', '.join(sorted(self.required_inputs)) or '<none>'}"
        )

    def _evaluate_direct_market_data_strategy(
        self,
        *,
        ib,
        current_holdings,
        historical_close_loader: Callable[..., Any],
        run_as_of: pd.Timestamp,
        translator: Callable[[str], str],
        pacing_sec: float,
        strategy_plugin_signals=(),
    ) -> StrategyEvaluationResult:
        runtime_config = dict(self.runtime_config)
        runtime_config.setdefault("translator", translator)
        runtime_config.setdefault("pacing_sec", float(pacing_sec))
        apply_runtime_policy_to_runtime_config(runtime_config, self.runtime_adapter)
        portfolio_snapshot = self._fetch_portfolio_snapshot_for_context(
            ib,
            required=False,
        )
        portfolio_snapshot = self._prepare_portfolio_snapshot(
            portfolio_snapshot,
            self._configured_strategy_symbols(include_ranking_pool=True),
            strategy_plugin_signals,
        )
        option_chains = self._fetch_option_chains_for_runtime(ib, runtime_config, portfolio_snapshot)
        if option_chains:
            runtime_config["option_chains"] = option_chains
        ctx = self._build_strategy_context(
            runtime_adapter=self.runtime_adapter,
            as_of=run_as_of,
            market_inputs=self._build_direct_market_data_inputs(ib, historical_close_loader),
            portfolio_snapshot=portfolio_snapshot,
            runtime_config=runtime_config,
            current_holdings=current_holdings,
            ib=ib,
        )
        decision = self.entrypoint.evaluate(ctx)
        managed_symbols = tuple(
            dict.fromkeys(
                str(position.symbol).strip().upper()
                for position in decision.positions
                if str(position.symbol or "").strip()
            )
        )
        price_fallbacks = self._build_historical_close_map(
            ib,
            historical_close_loader,
            self._build_price_fallback_symbol_list(
                decision,
                managed_symbols=managed_symbols,
                current_holdings=current_holdings,
            ),
        )
        metadata = {
            "strategy_profile": self.profile,
            **self._capability_status_metadata(portfolio_snapshot),
            "managed_symbols": managed_symbols,
            "status_icon": self.status_icon,
            "dry_run_only": self.runtime_settings.dry_run_only,
            **build_execution_timing_metadata(
                signal_date=run_as_of,
                signal_effective_after_trading_days=(
                    self.runtime_adapter.runtime_policy.signal_effective_after_trading_days
                ),
            ),
        }
        if portfolio_snapshot is not None:
            metadata = self._enrich_portfolio_metadata(metadata, portfolio_snapshot)
        if "BOXX" in managed_symbols:
            metadata["safe_haven_symbol"] = "BOXX"
        if price_fallbacks:
            metadata["price_fallbacks"] = price_fallbacks
            metadata["dry_run_price_fallbacks"] = price_fallbacks
            metadata["price_fallback_source"] = "historical_close"
        return StrategyEvaluationResult(decision=decision, metadata=metadata)

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
        strategy_plugin_signals=(),
    ) -> StrategyEvaluationResult:
        runtime_config = dict(self.runtime_config)
        runtime_config.setdefault("translator", translator)
        runtime_config.setdefault("pacing_sec", float(pacing_sec))
        apply_runtime_policy_to_runtime_config(runtime_config, self.runtime_adapter)
        requires_portfolio = (
            _PORTFOLIO_SNAPSHOT_INPUT in self.required_inputs
            or self.runtime_adapter.portfolio_input_name == _PORTFOLIO_SNAPSHOT_INPUT
        )
        portfolio_snapshot = self._fetch_portfolio_snapshot_for_context(
            ib,
            required=requires_portfolio,
        )
        portfolio_snapshot = self._prepare_portfolio_snapshot(
            portfolio_snapshot,
            self._configured_strategy_symbols(include_ranking_pool=True),
            strategy_plugin_signals,
        )
        option_chains = self._fetch_option_chains_for_runtime(ib, runtime_config, portfolio_snapshot)
        if option_chains:
            runtime_config["option_chains"] = option_chains
        ctx = self._build_strategy_context(
            runtime_adapter=self.runtime_adapter,
            as_of=run_as_of,
            market_inputs=self._build_market_history_inputs(ib, historical_close_loader),
            portfolio_snapshot=portfolio_snapshot,
            runtime_config=runtime_config,
            current_holdings=current_holdings,
            ib=ib,
        )
        decision = self.entrypoint.evaluate(ctx)
        safe_haven_symbol = str(self.merged_runtime_config.get("safe_haven") or "").strip().upper() or None
        managed_config_symbols = tuple(
            str(symbol) for symbol in self.merged_runtime_config.get("managed_symbols", ())
        )
        ranking_pool = tuple(str(symbol) for symbol in self.merged_runtime_config.get("ranking_pool", ()))
        managed_candidates = [*managed_config_symbols, *ranking_pool]
        if safe_haven_symbol:
            managed_candidates.append(safe_haven_symbol)
        managed_symbols = tuple(dict.fromkeys(managed_candidates))
        price_fallbacks = self._build_historical_close_map(
            ib,
            historical_close_loader,
            self._build_price_fallback_symbol_list(
                decision,
                managed_symbols=managed_symbols,
                current_holdings=current_holdings,
            ),
        )
        metadata = {
            "strategy_profile": self.profile,
            **self._capability_status_metadata(portfolio_snapshot),
            "managed_symbols": managed_symbols,
            "status_icon": self.status_icon,
            "dry_run_only": self.runtime_settings.dry_run_only,
            **build_execution_timing_metadata(
                signal_date=run_as_of,
                signal_effective_after_trading_days=(
                    self.runtime_adapter.runtime_policy.signal_effective_after_trading_days
                ),
            ),
        }
        if portfolio_snapshot is not None:
            metadata = self._enrich_portfolio_metadata(metadata, portfolio_snapshot)
        if safe_haven_symbol:
            metadata["safe_haven_symbol"] = safe_haven_symbol
        if price_fallbacks:
            metadata["price_fallbacks"] = price_fallbacks
            metadata["dry_run_price_fallbacks"] = price_fallbacks
            metadata["price_fallback_source"] = "historical_close"
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
        strategy_plugin_signals=(),
    ) -> StrategyEvaluationResult:
        runtime_config = dict(self.runtime_config)
        runtime_config.setdefault("translator", translator)
        apply_runtime_policy_to_runtime_config(runtime_config, self.runtime_adapter)
        managed_symbols = self._configured_strategy_symbols()
        portfolio_snapshot = self._fetch_portfolio_snapshot_for_context(ib, required=True)
        portfolio_snapshot = self._prepare_portfolio_snapshot(
            portfolio_snapshot,
            managed_symbols,
            strategy_plugin_signals,
        )
        option_chains = self._fetch_option_chains_for_runtime(ib, runtime_config, portfolio_snapshot)
        if option_chains:
            runtime_config["option_chains"] = option_chains
        market_inputs = self._build_value_target_market_inputs(
            ib=ib,
            historical_close_loader=historical_close_loader,
            historical_candle_loader=historical_candle_loader,
            as_of=run_as_of,
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
        ctx = replace(
            ctx,
            capabilities=self._build_context_capabilities(
                ib=ib,
                portfolio_snapshot=portfolio_snapshot,
            ),
        )
        decision = self.entrypoint.evaluate(ctx)
        safe_haven_symbol = next(
            (position.symbol for position in decision.positions if position.role == "safe_haven"),
            None,
        )
        price_fallbacks = self._build_historical_close_map(
            ib,
            historical_close_loader,
            self._build_price_fallback_symbol_list(
                decision,
                managed_symbols=managed_symbols,
                current_holdings=current_holdings,
            ),
        )
        metadata = self._enrich_portfolio_metadata(
            {
            "strategy_profile": self.profile,
            **self._capability_status_metadata(portfolio_snapshot),
            "managed_symbols": managed_symbols,
            "status_icon": self.status_icon,
            "dry_run_only": self.runtime_settings.dry_run_only,
            **build_execution_timing_metadata(
                signal_date=run_as_of,
                signal_effective_after_trading_days=(
                    self.runtime_adapter.runtime_policy.signal_effective_after_trading_days
                ),
            ),
            },
            portfolio_snapshot,
        )
        if safe_haven_symbol:
            metadata["safe_haven_symbol"] = str(safe_haven_symbol)
        benchmark_symbol = market_inputs.get("benchmark_symbol")
        if benchmark_symbol:
            metadata["benchmark_symbol"] = str(benchmark_symbol)
        if price_fallbacks:
            metadata["price_fallbacks"] = price_fallbacks
            metadata["dry_run_price_fallbacks"] = price_fallbacks
            metadata["price_fallback_source"] = "historical_close"
        return StrategyEvaluationResult(decision=decision, metadata=metadata)

    def _build_value_target_market_inputs(
        self,
        *,
        ib,
        historical_close_loader: Callable[..., Any],
        historical_candle_loader: Callable[..., Any] | None,
        as_of: pd.Timestamp,
    ) -> dict[str, Any]:
        if _DERIVED_INDICATORS_INPUT in self.required_inputs:
            external_market_inputs = resolve_external_market_signal_inputs(
                strategy_profile=self.profile,
                available_inputs=self.required_inputs,
                runtime_settings=self.runtime_settings,
                as_of=as_of,
                logger=self.logger,
            )
            if external_market_inputs:
                return external_market_inputs
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
        strategy_plugin_signals=(),
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
            portfolio_snapshot = self._prepare_portfolio_snapshot(
                portfolio_snapshot,
                strategy_plugin_signals=strategy_plugin_signals,
                project=False,
            )
            if portfolio_snapshot is not None:
                portfolio_snapshot_holder["portfolio_snapshot"] = portfolio_snapshot
            option_chains = self._fetch_option_chains_for_runtime(ib, runtime_config, portfolio_snapshot)
            if option_chains:
                runtime_config["option_chains"] = option_chains
            market_inputs: dict[str, Any] = {_FEATURE_SNAPSHOT_INPUT: feature_snapshot}
            if _MARKET_HISTORY_INPUT in self.required_inputs:
                market_inputs.update(self._build_market_history_inputs(ib, historical_close_loader))
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
            capabilities = self._build_context_capabilities(
                ib=ib,
                portfolio_snapshot=portfolio_snapshot,
            )
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
            price_fallbacks = self._build_snapshot_close_map(
                feature_snapshot,
                managed_symbols=managed_symbols,
            )
            return {
                "trade_date": run_as_of.date().isoformat(),
                "price_fallbacks": price_fallbacks,
                "dry_run_price_fallbacks": price_fallbacks,
                "price_fallback_source": "snapshot_close",
            }

        result = evaluate_feature_snapshot_strategy(
            entrypoint=self.entrypoint,
            runtime_adapter=self.runtime_adapter,
            runtime_settings=FeatureSnapshotRuntimeSettings(
                feature_snapshot_path=self.runtime_settings.feature_snapshot_path,
                feature_snapshot_manifest_path=self.runtime_settings.feature_snapshot_manifest_path,
                feature_snapshot_fallback_mode=self.runtime_settings.feature_snapshot_fallback_mode,
                feature_snapshot_fallback_cache_dir=self.runtime_settings.feature_snapshot_fallback_cache_dir,
                feature_snapshot_fallback_max_stale_days=(
                    self.runtime_settings.feature_snapshot_fallback_max_stale_days
                ),
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
    if (
        _FEATURE_SNAPSHOT_INPUT in frozenset(entrypoint.manifest.required_inputs)
        or runtime_settings.strategy_config_path
    ):
        runtime_config = runtime.load_runtime_parameters()
    runtime_config.update(_build_runtime_overrides(runtime_settings))

    merged_runtime_config = dict(entrypoint.manifest.default_config)
    merged_runtime_config.update(runtime_config)
    strategy_cash_reserve_ratio = float(
        merged_runtime_config.get(
            "execution_cash_reserve_ratio",
            DEFAULT_CASH_RESERVE_RATIO,
        )
    )
    strategy_rebalance_threshold_ratio = float(
        merged_runtime_config.get(
            "execution_rebalance_threshold_ratio",
            merged_runtime_config.get(
                "rebalance_threshold_ratio",
                DEFAULT_REBALANCE_THRESHOLD_RATIO,
            ),
        )
    )
    platform_cash_reserve_ratio = runtime_settings.reserved_cash_ratio
    if platform_cash_reserve_ratio is not None:
        strategy_cash_reserve_ratio = max(
            strategy_cash_reserve_ratio,
            float(platform_cash_reserve_ratio),
        )
    return LoadedStrategyRuntime(
        entrypoint=entrypoint,
        runtime_adapter=runtime_adapter,
        runtime_settings=runtime_settings,
        runtime_config=runtime_config,
        merged_runtime_config=merged_runtime_config,
        status_icon=runtime_adapter.status_icon,
        cash_reserve_ratio=strategy_cash_reserve_ratio,
        rebalance_threshold_ratio=max(0.0, strategy_rebalance_threshold_ratio),
        cash_reserve_floor_usd=float(runtime_settings.reserved_cash_floor_usd or 0.0),
        logger=logger,
    )


def _build_runtime_overrides(runtime_settings: PlatformRuntimeSettings) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    reserved_cash_floor_usd = getattr(runtime_settings, "reserved_cash_floor_usd", 0.0)
    reserved_cash_ratio = getattr(runtime_settings, "reserved_cash_ratio", None)
    if float(reserved_cash_floor_usd or 0.0) > 0.0:
        overrides["reserved_cash_floor_usd"] = float(reserved_cash_floor_usd)
    if reserved_cash_ratio is not None and float(reserved_cash_ratio or 0.0) > 0.0:
        overrides["reserved_cash_ratio"] = float(reserved_cash_ratio)
        overrides["cash_reserve_ratio"] = float(reserved_cash_ratio)
    if (
        str(getattr(runtime_settings, "strategy_profile", "") or "").strip() == _SOXL_PROFILE
        and bool(getattr(runtime_settings, "cash_only_execution", True))
    ):
        overrides["option_overlay_enabled"] = False
        overrides["option_growth_overlay_enabled"] = False
        overrides["option_income_overlay_enabled"] = False
    income_layer_enabled = getattr(runtime_settings, "income_layer_enabled", None)
    income_layer_start_usd = getattr(runtime_settings, "income_layer_start_usd", None)
    income_layer_max_ratio = getattr(runtime_settings, "income_layer_max_ratio", None)
    if income_layer_enabled is not None:
        overrides["income_layer_enabled"] = income_layer_enabled
    if income_layer_start_usd is not None:
        overrides["income_layer_start_usd"] = income_layer_start_usd
    if income_layer_max_ratio is not None:
        overrides["income_layer_max_ratio"] = income_layer_max_ratio
    _apply_dca_runtime_overrides(runtime_settings, overrides)
    _apply_ibit_zscore_exit_runtime_overrides(runtime_settings, overrides)
    return overrides


def _apply_dca_runtime_overrides(
    runtime_settings: PlatformRuntimeSettings,
    overrides: dict[str, Any],
) -> None:
    if runtime_settings.strategy_profile not in DCA_PROFILES:
        return
    dca_mode = getattr(runtime_settings, "dca_mode", None)
    dca_base_investment_usd = getattr(runtime_settings, "dca_base_investment_usd", None)
    if dca_mode is not None:
        overrides["investment_amount_mode"] = "fixed"
        overrides["smart_multiplier_enabled"] = dca_mode == "smart"
    if dca_base_investment_usd is not None:
        overrides["base_investment_usd"] = dca_base_investment_usd


def _apply_ibit_zscore_exit_runtime_overrides(
    runtime_settings: PlatformRuntimeSettings,
    overrides: dict[str, Any],
) -> None:
    if runtime_settings.strategy_profile != IBIT_ZSCORE_EXIT_PROFILE:
        return
    for setting_name, override_name in (
        ("ibit_zscore_exit_enabled", "ibit_zscore_exit_enabled"),
        ("ibit_zscore_exit_mode", "ibit_zscore_exit_mode"),
        ("ibit_zscore_exit_parking_symbol", "ibit_zscore_exit_parking_symbol"),
        ("ibit_zscore_exit_risk_reduced_exposure", "ibit_zscore_exit_risk_reduced_exposure"),
        ("ibit_zscore_exit_risk_off_exposure", "ibit_zscore_exit_risk_off_exposure"),
        (
            "ibit_zscore_exit_allow_outside_execution_window",
            "ibit_zscore_exit_allow_outside_execution_window",
        ),
    ):
        value = getattr(runtime_settings, setting_name, None)
        if value is not None:
            overrides[override_name] = value
