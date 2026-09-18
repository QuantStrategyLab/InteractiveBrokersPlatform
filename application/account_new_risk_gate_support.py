"""IBKR adapter for QPK account-level NEW_RISK gate (fail-closed)."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any

from quant_platform_kit.risk.account_new_risk_gate import (
    AccountNewRiskGateError,
    InjectedReconciliationSnapshot,
    NewRiskAdmissionResult,
    NewRiskDisposition,
    evaluate_new_risk_admission,
)
from quant_platform_kit.risk.contracts import RuntimeRiskLimits
from quant_platform_kit.risk.cycle_new_risk_health import (
    CycleNewRiskHealthEvidence,
    apply_cycle_new_risk_health_axes,
)
from quant_platform_kit.risk.production_drift_new_risk import (
    resolve_production_drift_status_from_store,
)

ACCOUNT_NEW_RISK_GATE_ENV = "ACCOUNT_NEW_RISK_GATE"
_MAX_DAILY_LOSS_ENV_KEYS = ("IBKR_MAX_DAILY_LOSS_USD", "MAX_DAILY_LOSS_USD")

_DEFAULT_STRATEGY_PROFILE = "soxl_soxx_trend_income"
_DEFAULT_DOMAIN = "us_equity"

# Minimal carrier for gate-only daily-loss axis; not a production RRL binding.
_DAILY_LOSS_LIMIT_CARRIER_SYMBOL = "SPY"

_cycle_snapshot: InjectedReconciliationSnapshot | None = None


def is_account_new_risk_gate_enabled() -> bool:
    """Production default on; set ACCOUNT_NEW_RISK_GATE=0 only for tests."""
    return str(os.environ.get(ACCOUNT_NEW_RISK_GATE_ENV, "") or "").strip() != "0"


def _coerce_optional_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _positive_limit_or_none(value: object) -> float | None:
    """Accept only finite positive limits; never invent a production default."""
    number = _coerce_optional_float(value)
    if number is None or number <= 0.0:
        return None
    return number


def _mapping_or_empty(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _resolve_equity_usd(portfolio: Mapping[str, Any], execution: Mapping[str, Any] | None) -> float | None:
    for key in ("total_equity", "total_strategy_equity", "equity"):
        equity = _coerce_optional_float(portfolio.get(key))
        if equity is not None and equity > 0.0:
            return equity
    if execution is not None:
        equity = _coerce_optional_float(execution.get("portfolio_total_equity"))
        if equity is not None and equity > 0.0:
            return equity
    return None


def _portfolio_metadata(portfolio: Mapping[str, Any]) -> Mapping[str, Any]:
    metadata = portfolio.get("metadata")
    return metadata if isinstance(metadata, Mapping) else {}


def _resolve_unknown_pending(portfolio: Mapping[str, Any], projection: Mapping[str, Any]) -> bool:
    """Only an explicit unknown-pending signal counts; absence is never treated as pending."""
    if "unknown_pending_orders" in projection:
        return bool(projection.get("unknown_pending_orders"))
    if portfolio.get("unknown_pending_orders") is not None:
        return bool(portfolio.get("unknown_pending_orders"))
    return bool(_portfolio_metadata(portfolio).get("unknown_pending_orders"))


def _resolve_durable_breaker_open(portfolio: Mapping[str, Any], projection: Mapping[str, Any]) -> bool:
    """Only an explicit durable OPEN state trips the breaker; absence is never OPEN."""
    for source in (projection, portfolio, _portfolio_metadata(portfolio)):
        value = source.get("durable_circuit_breaker_state")
        if value is not None:
            return str(value).strip().upper() == "OPEN"
    return False


def build_account_new_risk_snapshot(
    portfolio: Mapping[str, Any],
    *,
    execution: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a cycle-health projection from evidence available this cycle.

    ``observation_ok`` reflects whether equity resolved this cycle;
    ``unknown_pending`` and ``durable_breaker_open`` only trip on an explicit
    signal from the portfolio or its ``metadata`` -- their absence is never
    mistaken for a durable OPEN breaker or pending reconciliation. Explicit
    keys already present on an injected ``account_new_risk_snapshot`` always
    win over the projected axes (tests / HITL overrides).
    """
    projection = dict(portfolio.get("account_new_risk_snapshot") or {})
    equity_usd = _coerce_optional_float(projection.get("equity_usd"))
    if equity_usd is None:
        equity_usd = _resolve_equity_usd(portfolio, execution)

    evidence = CycleNewRiskHealthEvidence(
        observation_ok=equity_usd is not None,
        unknown_pending=_resolve_unknown_pending(portfolio, projection),
        durable_breaker_open=_resolve_durable_breaker_open(portfolio, projection),
    )
    projection = apply_cycle_new_risk_health_axes(projection, evidence)
    projection["equity_usd"] = equity_usd
    return projection


def build_portfolio_from_account_values(
    account_values: Mapping[str, Any],
    *,
    signal_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project a portfolio dict from an existing IBKR account_values read."""
    metadata = dict(signal_metadata or {})
    portfolio: dict[str, Any] = {
        "total_equity": _coerce_optional_float(account_values.get("equity")),
    }
    for key in ("unknown_pending_orders", "durable_circuit_breaker_state"):
        if key in account_values:
            portfolio[key] = account_values[key]
        elif key in metadata:
            portfolio[key] = metadata[key]
    portfolio["account_new_risk_snapshot"] = build_account_new_risk_snapshot(
        {
            **portfolio,
            "account_new_risk_snapshot": metadata.get("account_new_risk_snapshot"),
        }
    )
    for key in ("peak_equity_usd", "drawdown_from_peak", "realized_vol"):
        if key in metadata:
            portfolio[key] = metadata[key]
    return portfolio


def _resolve_strategy_profile(portfolio: Mapping[str, Any], projection: Mapping[str, Any]) -> str:
    for source in (projection, portfolio, _portfolio_metadata(portfolio)):
        value = source.get("strategy_profile")
        if isinstance(value, str) and value.strip():
            return value.strip()
    env = str(os.environ.get("STRATEGY_PROFILE") or "").strip()
    return env or _DEFAULT_STRATEGY_PROFILE


def _resolve_domain(portfolio: Mapping[str, Any], projection: Mapping[str, Any]) -> str:
    for source in (projection, portfolio, _portfolio_metadata(portfolio)):
        value = source.get("strategy_domain") or source.get("domain")
        if isinstance(value, str) and value.strip():
            return value.strip()
    env = str(os.environ.get("STRATEGY_DOMAIN") or "").strip()
    return env or _DEFAULT_DOMAIN


def _resolve_production_drift_status(
    portfolio: Mapping[str, Any], projection: Mapping[str, Any]
) -> str | None:
    """Prefer explicit inject; else read-only PerformanceStore (Policy A). Fail-soft on store errors."""
    for source in (projection, portfolio, _portfolio_metadata(portfolio)):
        raw = source.get("production_drift_status")
        if raw is not None and raw != "":
            return str(raw).strip()
    return resolve_production_drift_status_from_store(
        strategy_profile=_resolve_strategy_profile(portfolio, projection),
        domain=_resolve_domain(portfolio, projection),
    )


def _resolve_drawdown_from_peak(
    *,
    equity_usd: float | None,
    peak_equity_usd: float | None,
    explicit: float | None,
) -> float | None:
    if explicit is not None:
        return explicit
    if equity_usd is None or peak_equity_usd is None or peak_equity_usd <= 0.0:
        return None
    return max(0.0, 1.0 - (equity_usd / peak_equity_usd))


def _resolve_explicit_daily_loss_usd(
    projection: Mapping[str, Any],
    portfolio: Mapping[str, Any],
    execution: Mapping[str, Any] | None,
) -> float | None:
    """Pass through an explicit daily_loss_usd fact only; never invent one."""
    for source in (projection, portfolio, _mapping_or_empty(execution)):
        if "daily_loss_usd" in source:
            return _coerce_optional_float(source.get("daily_loss_usd"))
    return None


def _max_daily_loss_from_runtime_target_json() -> float | None:
    """Read max_daily_loss_usd from RUNTIME_TARGET_JSON when present; soft-omit on errors."""
    raw_target = os.environ.get("RUNTIME_TARGET_JSON")
    if raw_target is None or not str(raw_target).strip():
        return None
    try:
        payload = json.loads(raw_target)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    policy = payload.get("runtime_risk_limits")
    if not isinstance(policy, dict) or "max_daily_loss_usd" not in policy:
        return None
    return _positive_limit_or_none(policy.get("max_daily_loss_usd"))


def resolve_max_daily_loss_usd(
    portfolio: Mapping[str, Any] | None = None,
) -> float | None:
    """Resolve an explicit max_daily_loss_usd; omit the axis when unset.

    Priority: account_new_risk_snapshot / portfolio key → RUNTIME_TARGET_JSON →
    IBKR_MAX_DAILY_LOSS_USD / MAX_DAILY_LOSS_USD. No approved production default.
    """
    if portfolio is not None:
        projection = _mapping_or_empty(portfolio.get("account_new_risk_snapshot"))
        for source in (projection, portfolio):
            if "max_daily_loss_usd" in source:
                return _positive_limit_or_none(source.get("max_daily_loss_usd"))
    policy_limit = _max_daily_loss_from_runtime_target_json()
    if policy_limit is not None:
        return policy_limit
    for key in _MAX_DAILY_LOSS_ENV_KEYS:
        raw = os.environ.get(key)
        if raw is None or not str(raw).strip():
            continue
        limit = _positive_limit_or_none(raw)
        if limit is not None:
            return limit
    return None


def runtime_risk_limits_for_daily_loss_axis(
    max_daily_loss_usd: float | None,
) -> RuntimeRiskLimits | None:
    """Build admission-only limits carrying ``max_daily_loss_usd``, or omit.

    SPY/1.0 caps are a minimal legal RuntimeRiskLimits carrier for the gate only —
    not a production RRL binding and not an exposure raise.
    """
    if max_daily_loss_usd is None:
        return None
    symbol = _DAILY_LOSS_LIMIT_CARRIER_SYMBOL
    return RuntimeRiskLimits(
        allowed_symbols=(symbol,),
        product_leverage_factors={symbol: 1},
        nominal_caps={symbol: 1.0},
        total_nominal_exposure_cap=1.0,
        total_effective_exposure_cap=1.0,
        max_positions=1,
        max_daily_loss_usd=max_daily_loss_usd,
    )


def build_snapshot_from_portfolio(
    portfolio: Mapping[str, Any],
    *,
    execution: Mapping[str, Any] | None = None,
) -> InjectedReconciliationSnapshot:
    """Project an injected reconciliation snapshot from an existing portfolio read."""
    projection = build_account_new_risk_snapshot(portfolio, execution=execution)
    equity_usd = _coerce_optional_float(projection.get("equity_usd"))
    if equity_usd is None:
        equity_usd = _resolve_equity_usd(portfolio, execution)
    peak_equity_usd = (
        _coerce_optional_float(projection.get("peak_equity_usd"))
        if "peak_equity_usd" in projection
        else _coerce_optional_float(portfolio.get("peak_equity_usd"))
    )
    explicit_dd = (
        _coerce_optional_float(projection.get("drawdown_from_peak"))
        if "drawdown_from_peak" in projection
        else _coerce_optional_float(portfolio.get("drawdown_from_peak"))
    )
    return InjectedReconciliationSnapshot(
        observation_status=str(projection.get("observation_status") or "UNAVAILABLE"),
        reconciliation_status=str(projection.get("reconciliation_status") or "UNVERIFIED"),
        circuit_breaker_state=str(projection.get("circuit_breaker_state") or "OPEN"),
        equity_usd=equity_usd,
        peak_equity_usd=peak_equity_usd,
        drawdown_from_peak=_resolve_drawdown_from_peak(
            equity_usd=equity_usd,
            peak_equity_usd=peak_equity_usd,
            explicit=explicit_dd,
        ),
        realized_vol=_coerce_optional_float(projection.get("realized_vol"))
        if "realized_vol" in projection
        else _coerce_optional_float(portfolio.get("realized_vol")),
        production_drift_status=_resolve_production_drift_status(portfolio, projection),
        daily_loss_usd=_resolve_explicit_daily_loss_usd(projection, portfolio, execution),
    )


def evaluate_portfolio_new_risk_admission(
    portfolio: Mapping[str, Any],
    *,
    execution: Mapping[str, Any] | None = None,
) -> NewRiskAdmissionResult:
    try:
        snapshot = build_snapshot_from_portfolio(portfolio, execution=execution)
        limits = runtime_risk_limits_for_daily_loss_axis(resolve_max_daily_loss_usd(portfolio))
        return evaluate_new_risk_admission(snapshot, limits)
    except AccountNewRiskGateError:
        return NewRiskAdmissionResult(
            disposition=NewRiskDisposition.NEW_RISK_PROHIBITED,
            reason_codes=("SNAPSHOT_VALIDATION_FAIL_CLOSED",),
        )


def evaluate_account_values_new_risk_admission(
    account_values: Mapping[str, Any],
    *,
    signal_metadata: Mapping[str, Any] | None = None,
) -> NewRiskAdmissionResult:
    portfolio = build_portfolio_from_account_values(account_values, signal_metadata=signal_metadata)
    return evaluate_portfolio_new_risk_admission(portfolio)


def new_risk_buy_prohibited(result: NewRiskAdmissionResult) -> bool:
    return result.disposition == NewRiskDisposition.NEW_RISK_PROHIBITED


def apply_combined_scale(value: float, scale: float | None) -> float:
    """Apply a valid reducing scale; missing or out-of-range values are a no-op."""
    if scale is None or not math.isfinite(scale) or not 0.0 < scale <= 1.0:
        return value
    return value * scale



_ATTENTION_PLATFORM = "ibkr"
_OPERATIONAL_UNCERTAIN_REASONS = frozenset(
    {
        "EQUITY_UNKNOWN_FAIL_CLOSED",
        "SNAPSHOT_VALIDATION_FAIL_CLOSED",
        "RECONCILIATION_NOT_VERIFIED",
        "CIRCUIT_BREAKER_OPEN",
        "UNKNOWN_PENDING_ORDERS",
    }
)
_attention_sent_keys: set[str] = set()


def _resolve_attention_strategy_profile(portfolio: Mapping[str, Any]) -> str:
    projection = _mapping_or_empty(portfolio.get("account_new_risk_snapshot"))
    for source in (projection, portfolio, _mapping_or_empty(portfolio.get("metadata"))):
        value = source.get("strategy_profile")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(os.environ.get("STRATEGY_PROFILE") or "").strip() or _DEFAULT_STRATEGY_PROFILE


def _resolve_attention_account_alias(
    portfolio: Mapping[str, Any],
    execution: Mapping[str, Any] | None,
) -> str:
    for source in (
        portfolio,
        _mapping_or_empty(portfolio.get("metadata")),
        _mapping_or_empty(execution),
    ):
        for key in ("account_alias", "account_hash", "account_id", "account"):
            value = source.get(key)
            if value is not None and str(value).strip():
                text = str(value).strip()
                return text[-8:] if len(text) > 8 else text
    for env_key in ("IBKR_ACCOUNT_ID", "ACCOUNT_ALIAS"):
        env_alias = str(os.environ.get(env_key) or "").strip()
        if env_alias:
            return env_alias[-8:] if len(env_alias) > 8 else env_alias
    return "unknown"


def maybe_publish_attention_for_admission(
    admission: NewRiskAdmissionResult,
    *,
    portfolio: Mapping[str, Any],
    execution: Mapping[str, Any] | None = None,
    snapshot: InjectedReconciliationSnapshot | None = None,
    telegram_sender: Any | None = None,
    log_message: Any = print,
) -> Mapping[str, int]:
    """Publish ACTION/HALT attention when NEW_RISK / ops axes require a page.

    Dedupes on transition keys within the process. Never grants live, raises RRL,
    or invents daily-loss facts.
    """

    try:
        from quant_platform_kit.risk.attention import (
            AttentionAxes,
            evaluate_attention,
            resolve_mandate_dd_budget,
        )
        from quant_platform_kit.risk.attention_notify import publish_attention_telegram_transition
    except ImportError:
        try:
            log_message("attention_telegram_skipped reason=attention_api_unavailable")
        except TypeError:
            log_message("attention_telegram_skipped reason=attention_api_unavailable", flush=True)
        return {"sent": 0, "skipped": 1, "failed": 0}

    reasons = tuple(admission.reason_codes or ())
    prohibited = new_risk_buy_prohibited(admission)
    operational_uncertain = any(code in _OPERATIONAL_UNCERTAIN_REASONS for code in reasons)
    profile = _resolve_attention_strategy_profile(portfolio)
    drawdown = None if snapshot is None else snapshot.drawdown_from_peak
    decision = evaluate_attention(
        AttentionAxes(
            new_risk_prohibited=True if prohibited else None,
            operational_uncertain=True if operational_uncertain else None,
            drawdown_from_peak=drawdown,
            mandate_dd_budget=resolve_mandate_dd_budget(profile),
        )
    )
    return publish_attention_telegram_transition(
        decision=decision,
        platform=_ATTENTION_PLATFORM,
        account_alias=_resolve_attention_account_alias(portfolio, execution),
        strategy_profile=profile,
        previous_level=None,
        already_sent_keys=list(_attention_sent_keys),
        record_sent_key=_attention_sent_keys.add,
        telegram_sender=telegram_sender,
        log_message=log_message,
    )


def reset_attention_sent_keys_for_tests() -> None:
    _attention_sent_keys.clear()


def get_cycle_snapshot() -> InjectedReconciliationSnapshot | None:
    return _cycle_snapshot


def set_cycle_snapshot(snapshot: InjectedReconciliationSnapshot | None) -> None:
    global _cycle_snapshot
    _cycle_snapshot = snapshot


@contextmanager
def account_new_risk_gate_cycle(
    account_values: Mapping[str, Any],
    *,
    signal_metadata: Mapping[str, Any] | None = None,
):
    """Bind one account_values projection for the current execution cycle."""
    previous = _cycle_snapshot
    portfolio = build_portfolio_from_account_values(account_values, signal_metadata=signal_metadata)
    set_cycle_snapshot(build_snapshot_from_portfolio(portfolio))
    try:
        yield
    finally:
        set_cycle_snapshot(previous)


def evaluate_cycle_new_risk_admission() -> NewRiskAdmissionResult:
    if _cycle_snapshot is None:
        return NewRiskAdmissionResult(
            disposition=NewRiskDisposition.NEW_RISK_PROHIBITED,
            reason_codes=("EQUITY_UNKNOWN_FAIL_CLOSED",),
        )
    try:
        limits = runtime_risk_limits_for_daily_loss_axis(resolve_max_daily_loss_usd())
        return evaluate_new_risk_admission(_cycle_snapshot, limits)
    except AccountNewRiskGateError:
        return NewRiskAdmissionResult(
            disposition=NewRiskDisposition.NEW_RISK_PROHIBITED,
            reason_codes=("SNAPSHOT_VALIDATION_FAIL_CLOSED",),
        )
