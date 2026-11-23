"""Execution planning, option intents, locks, and pricing helpers."""
from __future__ import annotations

import hashlib
import json
import math
import tempfile
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from quant_platform_kit.common.models import ExecutionReport
from quant_platform_kit.common.quantity import (
    format_quantity,
)

from interactive_brokers_platform.application._execution_account_state import (
    _facade_attr,
    _floor_order_quantity,
    _normalize_account_ids,
    _record_order_outcome,
    check_order_submitted,
)

DEFAULT_SAFE_HAVEN_CASH_SUBSTITUTE_THRESHOLD_USD = 1000.0
SMALL_ACCOUNT_SAFE_HAVEN_CASH_SUBSTITUTE_LIMIT_USD = 2000.0

_UNSET: Any = object()

def _round_weight(value: float) -> float:
    return round(float(value or 0.0), 8)


def _build_target_hash(target_weights: dict[str, float]) -> str:
    payload = [[str(symbol), _round_weight(weight)] for symbol, weight in sorted(target_weights.items())]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")).hexdigest()


def _sanitize_token(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return "none"
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in text)
    return safe or "none"


def _display_text(value: Any, *, fallback: str) -> str:
    text = str(value).strip() if value is not None else ""
    return text or fallback


def _resolve_weight_allocation(signal_metadata: dict[str, Any] | None) -> dict[str, Any]:
    metadata = dict(signal_metadata or {})
    allocation = dict(metadata.get("allocation") or {})
    if not allocation:
        raise ValueError("IBKR execution requires signal_metadata.allocation")
    if allocation.get("target_mode") != "weight":
        raise ValueError("IBKR execution requires allocation.target_mode=weight")
    return {
        "strategy_symbols": tuple(str(symbol).strip().upper() for symbol in allocation.get("strategy_symbols", ())),
        "risk_symbols": tuple(str(symbol).strip().upper() for symbol in allocation.get("risk_symbols", ())),
        "income_symbols": tuple(str(symbol).strip().upper() for symbol in allocation.get("income_symbols", ())),
        "safe_haven_symbols": tuple(
            str(symbol).strip().upper() for symbol in allocation.get("safe_haven_symbols", ())
        ),
        "targets": {
            str(symbol).strip().upper(): float(weight)
            for symbol, weight in dict(allocation.get("targets") or {}).items()
        },
    }


def _require_live_risk_authority(
    signal_metadata: dict[str, Any],
    allocation: dict[str, Any],
    *,
    strategy_profile: str | None,
    account_ids,
) -> dict[str, float]:
    """Bind the final order plan to the strategy decision approved by RiskEngine."""
    authority = signal_metadata.get("risk_authority")
    flags = signal_metadata.get("risk_flags") or ()
    if (
        signal_metadata.get("risk_gate") != "APPROVE"
        or "risk_gate:passed" not in flags
        or not isinstance(authority, dict)
        or authority.get("strategy_profile") != strategy_profile
        or _normalize_account_ids(authority.get("account_ids")) != _normalize_account_ids(account_ids)
        or authority.get("trade_date") != signal_metadata.get("trade_date")
        or authority.get("signal_date") != signal_metadata.get("signal_date")
        or authority.get("effective_date") != signal_metadata.get("effective_date")
        or authority.get("snapshot_as_of") != signal_metadata.get("snapshot_as_of")
        or not isinstance(authority.get("targets"), dict)
        or not isinstance(authority.get("max_target_values"), dict)
    ):
        raise RuntimeError("IBKR live execution requires a valid RiskEngine approval")
    targets = allocation["targets"]
    approved_targets = authority["targets"]
    caps = authority["max_target_values"]
    equity = authority.get("portfolio_equity")
    if type(equity) not in (int, float) or not math.isfinite(equity) or equity <= 0.0:
        raise RuntimeError("IBKR live RiskEngine portfolio equity is invalid")
    if set(targets) != set(approved_targets) or set(targets) != set(caps):
        raise RuntimeError("IBKR live allocation differs from RiskEngine approval")
    for symbol, target in targets.items():
        approved = approved_targets[symbol]
        cap = caps[symbol]
        if (
            type(approved) not in (int, float)
            or not math.isfinite(approved)
            or not math.isclose(target, approved, rel_tol=0.0, abs_tol=1e-9)
            or type(cap) not in (int, float)
            or not math.isfinite(cap)
            or cap < 0.0
            or not math.isclose(cap, approved * equity, rel_tol=1e-8, abs_tol=0.01)
        ):
            raise RuntimeError("IBKR live order limit is invalid")
    return {symbol: float(caps[symbol]) for symbol in targets}


def _require_tqqq_effective_date(signal_metadata: dict[str, Any], *, strategy_profile: str | None) -> None:
    if strategy_profile != "tqqq_growth_income":
        return
    raw_effective_date = signal_metadata.get("effective_date")
    try:
        effective_date = date.fromisoformat(raw_effective_date)
    except (TypeError, ValueError):
        raise RuntimeError("IBKR TQQQ execution requires a valid effective date") from None
    if effective_date != _facade_attr('datetime', datetime).now(ZoneInfo("America/New_York")).date():
        raise RuntimeError("IBKR TQQQ signal is not effective for this trading date")


def _normalize_option_order_intents(signal_metadata: dict[str, Any] | None) -> tuple[dict[str, Any], ...]:
    metadata = dict(signal_metadata or {})
    raw_payload = metadata.get("option_order_intents")
    if isinstance(raw_payload, dict):
        raw_intents = raw_payload.get("intents")
    else:
        raw_intents = raw_payload
    intents = []
    for intent in raw_intents or ():
        if isinstance(intent, dict):
            intents.append(dict(intent))
    return tuple(intents)


def _option_intent_underliers(option_intents: tuple[dict[str, Any], ...]) -> tuple[str, ...]:
    underliers = []
    for intent in option_intents:
        underlier = str(intent.get("underlier") or intent.get("symbol") or "").strip().upper()
        if underlier:
            underliers.append(underlier)
    return tuple(sorted(dict.fromkeys(underliers)))


def _is_executable_option_intent(intent: dict[str, Any]) -> bool:
    intent_type = str(intent.get("intent_type") or "").strip()
    action = str(intent.get("action") or "").strip().lower()
    if (
        str(intent.get("asset_class") or "").strip().lower() == "option"
        and intent_type == "single_leg_option"
        and action in {"buy_to_open", "sell_to_close"}
    ):
        return True
    return (
        str(intent.get("asset_class") or "").strip().lower() == "option"
        and intent_type == "multi_leg_option"
        and action == "sell_to_open_put_credit_spread"
    )


def _has_executable_option_plan(option_intents: tuple[dict[str, Any], ...]) -> bool:
    return any(_is_executable_option_intent(intent) for intent in option_intents)


def _format_option_intent_symbol(intent: dict[str, Any]) -> str:
    underlier = str(intent.get("underlier") or intent.get("symbol") or "").strip().upper()
    if str(intent.get("intent_type") or "").strip() == "multi_leg_option":
        expiration = str(intent.get("expiration") or "").strip()
        return f"{underlier} {expiration} PCS".strip()
    right = str(intent.get("right") or "").strip().upper()
    expiration = str(intent.get("expiration") or "").strip()
    strike = intent.get("strike")
    if underlier and right and expiration and strike not in {None, ""}:
        return f"{underlier} {expiration} {float(strike):g}{right}"
    return underlier or "<option>"


def _record_unsupported_option_intents(
    execution_summary: dict[str, Any],
    option_intents: tuple[dict[str, Any], ...],
) -> None:
    for intent in option_intents:
        if _is_executable_option_intent(intent):
            continue
        symbol = _format_option_intent_symbol(intent)
        reason = "unsupported_option_intent_type"
        execution_summary["option_orders_skipped"].append(
            {
                "symbol": symbol,
                "action": str(intent.get("action") or ""),
                "intent_type": str(intent.get("intent_type") or ""),
                "reason": reason,
            }
        )
        execution_summary["skipped_reasons"].append(f"{reason}:{symbol}")


def _build_single_leg_option_order_intent(
    order_intent_cls,
    intent: dict[str, Any],
    *,
    account_id: str | None,
):
    action = str(intent.get("action") or "").strip().lower()
    side = "buy" if action.startswith("buy") else "sell"
    return order_intent_cls(
        symbol=str(intent.get("underlier") or intent.get("symbol") or "").strip().upper(),
        side=side,
        quantity=float(intent.get("quantity") or 0.0),
        order_type=str(intent.get("order_type") or "limit").strip().lower(),
        limit_price=(
            float(intent["limit_price"])
            if intent.get("limit_price") not in {None, ""}
            else None
        ),
        time_in_force=str(intent.get("time_in_force") or "DAY").strip().upper(),
        account_id=account_id,
        metadata={
            **intent,
            "asset_class": "option",
            "intent_type": "single_leg_option",
            "security_type": "OPT",
        },
    )


def _build_multi_leg_option_order_intent(
    order_intent_cls,
    intent: dict[str, Any],
    *,
    account_id: str | None,
):
    return order_intent_cls(
        symbol=str(intent.get("underlier") or intent.get("symbol") or "").strip().upper(),
        side="sell",
        quantity=float(intent.get("quantity") or 0.0),
        order_type=str(intent.get("order_type") or "limit").strip().lower(),
        limit_price=(
            float(intent["limit_price"])
            if intent.get("limit_price") not in {None, ""}
            else None
        ),
        time_in_force=str(intent.get("time_in_force") or "DAY").strip().upper(),
        account_id=account_id,
        metadata={
            **intent,
            "asset_class": "option",
            "intent_type": "multi_leg_option",
            "security_type": "BAG",
        },
    )


def _execute_option_order_intents(
    ib,
    option_intents: tuple[dict[str, Any], ...],
    *,
    submit_order_intent,
    order_intent_cls,
    translator,
    execution_summary: dict[str, Any],
    trade_logs: list[str],
    dry_run_only: bool,
    order_account_id: str | None,
    buying_power: float,
) -> float:
    if not option_intents:
        return buying_power
    _record_unsupported_option_intents(execution_summary, option_intents)
    for intent in option_intents:
        if not _is_executable_option_intent(intent):
            continue
        symbol = _format_option_intent_symbol(intent)
        quantity = float(intent.get("quantity") or 0.0)
        if quantity <= 0.0:
            execution_summary["option_orders_skipped"].append(
                {"symbol": symbol, "action": intent.get("action"), "reason": "quantity_zero"}
            )
            continue
        limit_price = float(intent.get("limit_price") or 0.0)
        multiplier = float(intent.get("contract_multiplier") or 100.0)
        action = str(intent.get("action") or "").strip().lower()
        intent_type = str(intent.get("intent_type") or "").strip()
        estimated_notional = float(intent.get("max_notional_usd") or (quantity * limit_price * multiplier))
        max_loss = float(intent.get("max_loss_usd") or 0.0)
        if action.startswith("buy") and estimated_notional > max(0.0, buying_power * 0.95):
            execution_summary["option_orders_skipped"].append(
                {
                    "symbol": symbol,
                    "action": action,
                    "quantity": quantity,
                    "limit_price": limit_price,
                    "reason": "insufficient_buying_power",
                }
            )
            execution_summary["skipped_reasons"].append(f"option_insufficient_buying_power:{symbol}")
            continue
        if intent_type == "multi_leg_option" and max_loss > max(0.0, buying_power * 0.95):
            execution_summary["option_orders_skipped"].append(
                {
                    "symbol": symbol,
                    "action": action,
                    "quantity": quantity,
                    "limit_price": limit_price,
                    "reason": "insufficient_buying_power_for_defined_risk",
                }
            )
            execution_summary["skipped_reasons"].append(f"option_insufficient_buying_power:{symbol}")
            continue
        payload = {
            "symbol": symbol,
            "underlier": str(intent.get("underlier") or "").strip().upper(),
            "action": action,
            "quantity": quantity,
            "limit_price": limit_price,
            "status": "dry_run" if dry_run_only else "pending",
        }
        if dry_run_only:
            execution_summary["option_orders_submitted"].append(payload)
            trade_logs.append(f"DRY_RUN option {action} {symbol} {format_quantity(quantity)} @{limit_price:.2f}")
            if action.startswith("buy"):
                buying_power -= estimated_notional
            elif intent_type == "multi_leg_option":
                buying_power -= max_loss
            continue
        if intent_type == "multi_leg_option":
            order_intent = _build_multi_leg_option_order_intent(
                order_intent_cls,
                intent,
                account_id=order_account_id,
            )
        else:
            order_intent = _build_single_leg_option_order_intent(
                order_intent_cls,
                intent,
                account_id=order_account_id,
            )
        report = submit_order_intent(ib, order_intent)
        _, status_msg = check_order_submitted(report, translator=translator)
        status, broker_order_id, _, _ = _extract_execution_report_fields(report)
        order_payload = {
            **payload,
            "status": status,
            "broker_order_id": broker_order_id,
        }
        outcome = _record_order_outcome(
            execution_summary,
            order_payload,
            status=status,
            option_order=True,
        )
        trade_logs.append(f"option {action} {symbol} {format_quantity(quantity)} @{limit_price:.2f} {status_msg}")
        if outcome != "failed" and action.startswith("buy"):
            buying_power -= estimated_notional
        elif outcome != "failed" and intent_type == "multi_leg_option":
            buying_power -= max_loss
    return buying_power


def _apply_snapshot_price_fallbacks(
    prices: dict[str, float],
    symbols,
    *,
    dry_run_only: bool,
    snapshot_price_fallbacks: dict[str, float] | None,
) -> tuple[dict[str, float], tuple[str, ...]]:
    del dry_run_only
    if not snapshot_price_fallbacks:
        return dict(prices), ()
    resolved = dict(prices)
    fallback_symbols: list[str] = []
    for symbol in symbols:
        normalized = str(symbol).strip().upper()
        try:
            existing_price = float(resolved.get(normalized, 0.0) or 0.0)
        except (TypeError, ValueError):
            existing_price = 0.0
        if existing_price > 0.0:
            resolved[normalized] = existing_price
            continue
        fallback_price = snapshot_price_fallbacks.get(normalized)
        if fallback_price and float(fallback_price) > 0:
            resolved[normalized] = float(fallback_price)
            fallback_symbols.append(normalized)
    return resolved, tuple(fallback_symbols)


def _normalize_price_fallbacks(signal_metadata: dict[str, Any] | None) -> dict[str, float]:
    metadata = dict(signal_metadata or {})
    raw_fallbacks = {}
    for key in ("dry_run_price_fallbacks", "price_fallbacks"):
        candidate = metadata.get(key)
        if isinstance(candidate, dict):
            raw_fallbacks.update(candidate)
    normalized: dict[str, float] = {}
    for symbol, price in raw_fallbacks.items():
        normalized_symbol = str(symbol).strip().upper()
        if not normalized_symbol:
            continue
        try:
            numeric_price = float(price)
        except (TypeError, ValueError):
            continue
        if numeric_price > 0.0:
            normalized[normalized_symbol] = numeric_price
    return normalized


def _format_symbol_preview(symbols: tuple[str, ...], *, limit: int = 3) -> str:
    if not symbols:
        return ""
    shown = [str(symbol).strip().upper() for symbol in symbols[:limit]]
    remaining = len(symbols) - len(shown)
    if remaining > 0:
        shown.append(f"+{remaining}")
    return ",".join(shown)


def _resolve_execution_mode(*, dry_run_only: bool, execution_mode: str | None = None) -> str:
    if dry_run_only:
        return "dry_run"
    normalized = str(execution_mode or "paper").strip().lower().replace("-", "_")
    aliases = {
        "dryrun": "dry_run",
        "dry_run_only": "dry_run",
        "paper_trading": "paper",
        "live_trading": "live",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized == "dry_run":
        raise ValueError("execution_mode=dry_run requires dry_run_only=True")
    if normalized not in {"paper", "live"}:
        raise ValueError("execution_mode must be paper or live")
    return normalized


def _resolve_execution_lock_path(
    *,
    strategy_profile: str | None,
    account_group: str | None,
    service_name: str | None,
    trade_date: str | None,
    snapshot_date: str | None,
    execution_mode: str,
    execution_lock_dir: str | Path | None,
) -> Path:
    lock_dir = Path(execution_lock_dir) if execution_lock_dir else Path(tempfile.gettempdir()) / "ibkr_execution_locks"
    scope = "__".join(
        [
            _sanitize_token(account_group or "default"),
            _sanitize_token(service_name or "service"),
            _sanitize_token(strategy_profile or "unknown"),
            _sanitize_token(execution_mode),
            _sanitize_token(trade_date),
            _sanitize_token(snapshot_date or "no_snapshot"),
        ]
    )
    return lock_dir / f"{scope}.json"


def _read_execution_lock(lock_path: Path) -> dict[str, Any] | None:
    if not lock_path.exists():
        return None
    return json.loads(lock_path.read_text(encoding="utf-8"))


def _try_create_execution_lock(lock_path: Path, payload: dict[str, Any]) -> bool:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with lock_path.open("x", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        return True
    except FileExistsError:
        return False


def _build_execution_lock_payload(
    *,
    strategy_profile: str | None,
    account_group: str | None,
    service_name: str | None,
    account_ids: tuple[str, ...] | list[str] | None,
    trade_date: str | None,
    snapshot_date: str | None,
    target_hash: str,
    execution_mode: str,
) -> dict[str, Any]:
    return {
        "strategy_profile": strategy_profile,
        "account_group": account_group,
        "service_name": service_name,
        "account_ids": list(account_ids or ()),
        "trade_date": trade_date,
        "snapshot_date": snapshot_date,
        "mode": execution_mode,
        "target_hash": target_hash,
        "created_at": _facade_attr('datetime', datetime).now(timezone.utc).isoformat(timespec="seconds") + "Z",
    }


def _format_target_lines(
    target_weights: dict[str, float],
    current_mv: dict[str, float],
    equity: float,
    *,
    translator,
) -> list[str]:
    current_weight = {
        symbol: (current_mv.get(symbol, 0.0) / equity if equity > 0 else 0.0)
        for symbol in set(target_weights) | set(current_mv)
    }
    target_lines = []
    for symbol, target_weight in sorted(target_weights.items(), key=lambda item: (-item[1], item[0])):
        delta = target_weight - current_weight.get(symbol, 0.0)
        target_lines.append(
            translator(
                "target_diff",
                symbol=symbol,
                current=f"{current_weight.get(symbol, 0.0):.1%}",
                target=f"{target_weight:.1%}",
                delta=f"{delta:.1%}",
            )
        )
    return target_lines


def _build_target_diff_rows(
    target_weights: dict[str, float],
    current_mv: dict[str, float],
    equity: float,
) -> list[dict[str, float | str]]:
    current_weight = {
        symbol: (current_mv.get(symbol, 0.0) / equity if equity > 0 else 0.0)
        for symbol in set(target_weights) | set(current_mv)
    }
    rows = []
    for symbol, target_weight in sorted(target_weights.items(), key=lambda item: (-item[1], item[0])):
        current_value = current_weight.get(symbol, 0.0)
        rows.append(
            {
                "symbol": symbol,
                "current_weight": current_value,
                "target_weight": float(target_weight),
                "delta_weight": float(target_weight - current_value),
            }
        )
    return rows




def _estimated_buy_order_cost(
    *,
    buy_symbols,
    current_mv,
    target_mv,
    threshold,
    investable_buying_power,
    prices,
    limit_buy_premium,
    limit_buy_premium_by_symbol,
    quantity_step,
    minimum_order_notional,
) -> float:
    total_cost = 0.0
    for symbol in buy_symbols:
        current = float(current_mv.get(symbol, 0.0) or 0.0)
        target = float(target_mv.get(symbol, 0.0) or 0.0)
        if current >= target - threshold:
            continue
        buy_value = min(target - current, investable_buying_power)
        price = prices.get(symbol)
        if not price or buy_value < minimum_order_notional:
            continue
        limit_price = _limit_buy_price(symbol, price, limit_buy_premium, limit_buy_premium_by_symbol)
        qty = _floor_order_quantity(
            buy_value / limit_price,
            quantity_step=quantity_step,
        )
        if qty <= 0:
            continue
        total_cost += float(qty) * float(limit_price)
    return total_cost


def _rotation_guard_should_block_buys(
    *,
    pending_sell_release_symbols,
    buy_needed_symbols,
    current_mv,
    target_mv,
    threshold,
    investable_buying_power,
    prices,
    limit_buy_premium,
    limit_buy_premium_by_symbol,
    quantity_step,
    minimum_order_notional,
) -> bool:
    if not pending_sell_release_symbols or not buy_needed_symbols:
        return False
    estimated_cost = _estimated_buy_order_cost(
        buy_symbols=buy_needed_symbols,
        current_mv=current_mv,
        target_mv=target_mv,
        threshold=threshold,
        investable_buying_power=investable_buying_power,
        prices=prices,
        limit_buy_premium=limit_buy_premium,
        limit_buy_premium_by_symbol=limit_buy_premium_by_symbol,
        quantity_step=quantity_step,
        minimum_order_notional=minimum_order_notional,
    )
    return estimated_cost > investable_buying_power


def _sell_order_quantity(
    *,
    current_value,
    target_value,
    price,
    position_quantity,
    quantity_step,
):
    held_quantity = max(0.0, float(position_quantity or 0.0))
    if held_quantity <= 0.0:
        return 0

    target = max(0.0, float(target_value or 0.0))
    if target <= 0.0:
        return _floor_order_quantity(held_quantity, quantity_step=quantity_step)

    sell_value = max(0.0, float(current_value or 0.0) - target)
    if sell_value <= 0.0 or float(price or 0.0) <= 0.0:
        return 0
    return _floor_order_quantity(
        min(sell_value / float(price), held_quantity),
        quantity_step=quantity_step,
    )


def _limit_buy_premium_for_symbol(symbol, default_premium, premium_by_symbol=None) -> float:
    normalized_symbol = str(symbol or "").strip().upper()
    try:
        fallback = float(default_premium)
    except (TypeError, ValueError):
        fallback = 1.005
    if not isinstance(premium_by_symbol, dict):
        return fallback
    raw_value = premium_by_symbol.get(normalized_symbol)
    if raw_value is None:
        return fallback
    try:
        premium = float(raw_value)
    except (TypeError, ValueError):
        return fallback
    return premium if premium > 0.0 else fallback


def _limit_buy_price(symbol, price, default_premium, premium_by_symbol=None) -> float:
    return round(
        float(price) * _limit_buy_premium_for_symbol(symbol, default_premium, premium_by_symbol),
        2,
    )



def _positive_target_total(targets: dict[str, Any]) -> float:
    total = 0.0
    for value in dict(targets or {}).values():
        try:
            total += max(0.0, float(value or 0.0))
        except (TypeError, ValueError):
            continue
    return total


def _apply_safe_haven_cash_substitution_to_weights(
    target_weights: dict[str, float],
    *,
    safe_haven_symbols: tuple[str, ...],
    investable: float,
    threshold_usd: float,
) -> tuple[dict[str, float], tuple[str, ...]]:
    threshold = max(0.0, float(threshold_usd or 0.0))
    adjusted = {
        str(symbol).strip().upper(): float(weight or 0.0)
        for symbol, weight in dict(target_weights or {}).items()
    }
    if threshold <= 0.0:
        return adjusted, ()

    substituted: list[str] = []
    for symbol in safe_haven_symbols:
        normalized = str(symbol or "").strip().upper()
        if not normalized:
            continue
        target_notional = max(0.0, float(investable or 0.0) * float(adjusted.get(normalized, 0.0) or 0.0))
        if 0.0 < target_notional < threshold:
            adjusted[normalized] = 0.0
            substituted.append(normalized)
    return adjusted, tuple(dict.fromkeys(substituted))


def _planned_buy_order_quantity(
    symbol,
    *,
    buy_value,
    limit_price,
    quantity_step,
    investable_buying_power,
    held_quantity=0.0,
) -> tuple[float, bool]:
    """Round the approved buy budget down; retain the legacy result shape."""
    effective_limit_price = max(0.0, float(limit_price or 0.0))
    qty = (
        _floor_order_quantity(float(buy_value or 0.0) / effective_limit_price, quantity_step=quantity_step)
        if effective_limit_price > 0.0
        else 0.0
    )
    return qty, False


def _extract_execution_report_fields(
    report: Any,
) -> tuple[str, Any, Any, Any]:
    if isinstance(report, ExecutionReport):
        return (
            str(report.status or ""),
            report.broker_order_id,
            report.filled_quantity,
            report.average_fill_price,
        )
    return (
        str(getattr(report, "status", "") or ""),
        getattr(report, "broker_order_id", None),
        getattr(report, "filled_quantity", None),
        getattr(report, "average_fill_price", None),
    )


def _projected_sell_release_value_for_report(report, *, fallback_price=0.0, fallback_quantity=0.0) -> float:
    status, _broker_order_id, raw_filled_quantity, raw_average_fill_price = _extract_execution_report_fields(report)
    if status not in {"Filled", "PartiallyFilled", "Partial"}:
        return 0.0
    filled_quantity = float(raw_filled_quantity or 0.0)
    if status == "Filled" and filled_quantity <= 0.0:
        raw_quantity = report.quantity if isinstance(report, ExecutionReport) else getattr(report, "quantity", 0.0)
        filled_quantity = float(raw_quantity or fallback_quantity or 0.0)
    if filled_quantity <= 0.0:
        return 0.0
    fill_price = float(raw_average_fill_price or 0.0)
    if fill_price <= 0.0:
        fill_price = max(0.0, float(fallback_price or 0.0))
    if fill_price <= 0.0:
        return 0.0
    return filled_quantity * fill_price


def _finalize_result(trade_logs, execution_summary, *, return_summary: bool):
    if return_summary:
        return trade_logs, execution_summary
    return trade_logs


_UNSET: Any = object()


@dataclass(frozen=True, slots=True)
class RebalanceExecutionOptions:
    """Configuration options controlling one IBKR rebalance execution cycle."""

    cash_reserve_ratio: float
    rebalance_threshold_ratio: float
    limit_buy_premium: float
    sell_settle_delay_sec: float
    strategy_profile: str | None = None
    account_group: str | None = None
    service_name: str | None = None
    account_ids: Any = None
    dry_run_only: bool = False
    execution_mode: str | None = None
    cash_reserve_floor_usd: float = 0.0
    quantity_step: float = 1.0
    min_order_notional: float = 50.0
    safe_haven_cash_substitute_threshold_usd: float = _facade_attr('DEFAULT_SAFE_HAVEN_CASH_SUBSTITUTE_THRESHOLD_USD', DEFAULT_SAFE_HAVEN_CASH_SUBSTITUTE_THRESHOLD_USD)
    market_currency: str = "USD"
    limit_buy_premium_by_symbol: dict[str, float] | None = None
    execution_lock_dir: str | Path | None = None
    return_summary: bool = False
    cash_only_execution: bool = True
    paper_execution_admission_enabled: bool = False
    runtime_release_receipt: Any = None
    expected_strategy_release: Any = None


@dataclass(slots=True)
class _RebalanceAllocationPlan:
    strategy_symbols: tuple[str, ...]
    risk_symbols: tuple[str, ...]
    income_symbols: tuple[str, ...]
    safe_haven_symbols: tuple[str, ...]
    safe_haven_symbol: str | None
    target_weights: dict[str, float]
    approved_target_caps: dict[str, float]
    option_order_intents: tuple[dict[str, Any], ...]
    option_underliers: tuple[str, ...]
    has_executable_option_plan: bool
    trade_date: str | None
    snapshot_date: str | None
    resolved_execution_mode: str
    normalized_account_ids: tuple[str, ...]
    order_account_id: str | None
    equity: float
    reserved: float
    investable: float
    threshold: float
    order_quantity_step: float
    minimum_order_notional: float
    all_symbols: set[str]
    account_new_risk_buy_blocked: bool
    account_new_risk_reason_codes: tuple[str, ...]
    execution_summary: dict[str, Any]
    early_logs: list[str] | None = None


@dataclass(frozen=True, slots=True)
class _RebalanceQuoteCollection:
    prices: dict[str, float]
    quote_snapshots: list[dict[str, Any]]
    fallback_symbols: tuple[str, ...]
    fallback_source: str


@dataclass(frozen=True, slots=True)
class _DriftNotesContext:
    reference_target_mv: dict[str, float]
    current_mv: dict[str, float]
    current_quantities: dict[str, float]
    prices: dict[str, float]
    equity: float
    investable_buying_power: float


@dataclass(frozen=True, slots=True)
class _RebalanceOrderOutcome:
    trade_logs: list[str]
    buying_power: float = 0.0
    finalized_early: bool = False
    drift_context: _DriftNotesContext | None = None


def _resolve_rebalance_options(
    options: RebalanceExecutionOptions | None,
    **overrides: Any,
) -> RebalanceExecutionOptions:
    provided = {key: value for key, value in overrides.items() if value is not _UNSET}
    if options is not None:
        return replace(options, **provided) if provided else options
    missing = [
        name
        for name in (
            "cash_reserve_ratio",
            "rebalance_threshold_ratio",
            "limit_buy_premium",
            "sell_settle_delay_sec",
        )
        if name not in provided
    ]
    if missing:
        joined = ", ".join(repr(name) for name in missing)
        raise TypeError(f"execute_rebalance() missing required keyword-only argument(s): {joined}")
    return RebalanceExecutionOptions(**provided)


def _sum_safe_haven_weight(
    target_weights: dict[str, float],
    safe_haven_symbols: tuple[str, ...],
) -> float:
    return float(
        sum(
            float(target_weights.get(str(symbol or "").strip().upper(), 0.0) or 0.0)
            for symbol in safe_haven_symbols
        )
    )

