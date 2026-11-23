"""Cycle lock acquisition and equity sell/buy execution steps."""
from __future__ import annotations

import math
import time
from typing import Any

from quant_platform_kit.common.quantity import (
    format_quantity,
)

from interactive_brokers_platform.application._execution_account_state import (
    _facade_attr,
    _floor_order_quantity,
    _investable_buying_power,
    SETTLED_CASH_MAX_AGE_SECONDS,
    _SettledCashDeadlineIB,
    _cash_value_for_currency,
    _record_order_outcome,
    _settled_cash_for_currency,
    check_order_submitted,
    get_available_buying_power,
)
from interactive_brokers_platform.application._execution_planning_helpers import (
    RebalanceExecutionOptions,
    _RebalanceAllocationPlan,
    _build_execution_lock_payload,
    _display_text,
    _extract_execution_report_fields,
    _format_symbol_preview,
    _limit_buy_price,
    _planned_buy_order_quantity,
    _projected_sell_release_value_for_report,
    _read_execution_lock,
    _resolve_execution_lock_path,
    _rotation_guard_should_block_buys,
    _sell_order_quantity,
    _try_create_execution_lock,
)
from interactive_brokers_platform.application._execution_allocation import (
    _record_blocked_step,
)

def _acquire_cycle_execution_lock(
    *,
    allocation_plan: _RebalanceAllocationPlan,
    target_hash: str,
    trade_logs: list[str],
    translator,
    options: RebalanceExecutionOptions,
) -> bool:
    execution_summary = allocation_plan.execution_summary
    lock_path = _resolve_execution_lock_path(
        strategy_profile=options.strategy_profile,
        account_group=options.account_group,
        service_name=options.service_name,
        trade_date=allocation_plan.trade_date,
        snapshot_date=allocation_plan.snapshot_date,
        execution_mode=allocation_plan.resolved_execution_mode,
        execution_lock_dir=options.execution_lock_dir,
    )
    lock_payload = _build_execution_lock_payload(
        strategy_profile=options.strategy_profile,
        account_group=options.account_group,
        service_name=options.service_name,
        account_ids=allocation_plan.normalized_account_ids,
        trade_date=allocation_plan.trade_date,
        snapshot_date=allocation_plan.snapshot_date,
        target_hash=target_hash,
        execution_mode=allocation_plan.resolved_execution_mode,
    )
    if not _try_create_execution_lock(lock_path, lock_payload):
        existing = _read_execution_lock(lock_path) or {}
        reason = (
            f"same_day_execution_locked:mode={allocation_plan.resolved_execution_mode}:"
            f"target_hash={existing.get('target_hash', '<unknown>')}"
        )
        execution_summary["lock_path"] = str(lock_path)
        _record_blocked_step(
            execution_summary,
            trade_logs,
            reason=reason,
            translator=translator,
            log_message=translator(
                "same_day_execution_locked",
                profile=_display_text(options.strategy_profile, fallback="<unknown>"),
                mode=allocation_plan.resolved_execution_mode,
                trade_date=_display_text(allocation_plan.trade_date, fallback="<none>"),
                snapshot_date=_display_text(allocation_plan.snapshot_date, fallback="<none>"),
                target_hash=_display_text(existing.get("target_hash"), fallback="<unknown>"),
                lock_path=str(lock_path),
            ),
        )
        return False
    execution_summary["lock_path"] = str(lock_path)
    trade_logs.append(
        translator(
            "execution_lock_acquired",
            mode=allocation_plan.resolved_execution_mode,
            trade_date=_display_text(allocation_plan.trade_date, fallback="<none>"),
            snapshot_date=_display_text(allocation_plan.snapshot_date, fallback="<none>"),
            lock_path=str(lock_path),
        )
    )
    execution_summary["execution_status"] = "executing"
    return True


def _record_submitted_equity_order(
    execution_summary: dict[str, Any],
    trade_logs: list[str],
    report: Any,
    *,
    symbol: str,
    side: str,
    quantity: float,
    order_account_id: str | None,
    translator,
    limit_price: float | None = None,
) -> str:
    status, broker_order_id, filled_quantity, average_fill_price = _extract_execution_report_fields(report)
    order_payload: dict[str, Any] = {
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "status": status,
        "broker_order_id": broker_order_id,
        "account_id": order_account_id,
        "filled_quantity": filled_quantity,
        "average_fill_price": average_fill_price,
    }
    if limit_price is not None:
        order_payload["limit_price"] = limit_price
    outcome = _record_order_outcome(execution_summary, order_payload, status=status)
    if outcome == "unknown":
        trade_logs.append(translator("failed", reason="broker_order_outcome_unknown"))
        return outcome
    _, status_msg = check_order_submitted(report, translator=translator)
    if side == "sell":
        log_prefix = translator("market_sell", symbol=symbol, qty=format_quantity(quantity))
    else:
        log_prefix = translator(
            "limit_buy",
            symbol=symbol,
            qty=format_quantity(quantity),
            price=f"{float(limit_price or 0.0):.2f}",
        )
    trade_logs.append(f"{log_prefix} {status_msg}")
    return outcome


def _resolve_sell_quantity(
    *,
    symbol: str,
    current: float,
    target: float,
    price: float | None,
    position_quantity: float,
    threshold: float,
    quantity_step: float,
    safe_haven_symbol: str | None,
    cash_sweep_quantity: int,
    execution_summary: dict[str, Any],
    pending_sell_release_symbols: list[str],
) -> float | int:
    is_cash_sweep = symbol == safe_haven_symbol and cash_sweep_quantity > 0
    if not is_cash_sweep and current <= target + threshold:
        return 0
    if not price:
        execution_summary["orders_skipped"].append({"symbol": symbol, "side": "sell", "reason": "missing_price"})
        execution_summary["skipped_reasons"].append(f"missing_price:{symbol}")
        return 0
    qty = _sell_order_quantity(
        current_value=current,
        target_value=target,
        price=price,
        position_quantity=position_quantity,
        quantity_step=quantity_step,
    )
    if is_cash_sweep:
        qty = max(int(cash_sweep_quantity), int(qty))
    if qty <= 0:
        if current > target + threshold:
            pending_sell_release_symbols.append(symbol)
        execution_summary["orders_skipped"].append({"symbol": symbol, "side": "sell", "reason": "quantity_zero"})
        return 0
    return qty


def _execute_sell_orders(
    ib,
    *,
    positions,
    allocation_plan: _RebalanceAllocationPlan,
    current_mv: dict[str, float],
    target_mv: dict[str, float],
    prices: dict[str, float],
    cash_sweep_quantity: int,
    claimed_submitter,
    order_intent_cls,
    translator,
    trade_logs: list[str],
    dry_run_only: bool,
) -> tuple[bool, float, float, list[str], bool]:
    execution_summary = allocation_plan.execution_summary
    sell_executed = False
    projected_sell_release_value = 0.0
    dry_run_sale_proceeds = 0.0
    pending_sell_release_symbols: list[str] = []

    for symbol in allocation_plan.all_symbols:
        current = current_mv.get(symbol, 0)
        target = target_mv.get(symbol, 0)
        price = prices.get(symbol)
        qty = _resolve_sell_quantity(
            symbol=symbol,
            current=current,
            target=target,
            price=price,
            position_quantity=positions.get(symbol, {}).get("quantity", 0),
            threshold=allocation_plan.threshold,
            quantity_step=allocation_plan.order_quantity_step,
            safe_haven_symbol=allocation_plan.safe_haven_symbol,
            cash_sweep_quantity=cash_sweep_quantity,
            execution_summary=execution_summary,
            pending_sell_release_symbols=pending_sell_release_symbols,
        )
        if qty <= 0:
            continue

        held_quantity = float(positions.get(symbol, {}).get("quantity", 0.0) or 0.0)
        if (
            symbol not in allocation_plan.strategy_symbols
            or not math.isfinite(float(qty))
            or float(qty) <= 0.0
            or float(qty) > held_quantity + 1e-9
            or not math.isfinite(float(price))
            or float(price) <= 0.0
        ):
            raise RuntimeError("IBKR live sell exceeds approved position or has invalid pricing")
        if allocation_plan.account_new_risk_buy_blocked:
            from quant_platform_kit.risk.account_new_risk_gate import is_proven_risk_reducing_order

            if not is_proven_risk_reducing_order(held_quantity, "sell", qty, pending_quantity=0.0):
                execution_summary["orders_skipped"].append(
                    {"symbol": symbol, "side": "sell", "reason": "risk_reduction_unproven"}
                )
                continue
        if dry_run_only:
            execution_summary["orders_submitted"].append(
                {"symbol": symbol, "side": "sell", "quantity": qty, "status": "dry_run"}
            )
            trade_logs.append(f"DRY_RUN sell {symbol} {format_quantity(qty)}")
            dry_run_sale_proceeds += float(qty) * float(price)
            continue
        report = claimed_submitter(
            ib,
            order_intent_cls(
                symbol=symbol,
                side="sell",
                quantity=qty,
                account_id=allocation_plan.order_account_id,
            ),
            **(
                {"reduction_position": held_quantity, "reduction_pending_quantity": 0.0}
                if allocation_plan.account_new_risk_buy_blocked
                else {}
            ),
        )
        outcome = _record_submitted_equity_order(
            execution_summary,
            trade_logs,
            report,
            symbol=symbol,
            side="sell",
            quantity=qty,
            order_account_id=allocation_plan.order_account_id,
            translator=translator,
        )
        if outcome == "unknown":
            return sell_executed, projected_sell_release_value, dry_run_sale_proceeds, pending_sell_release_symbols, True
        if outcome != "failed":
            sell_executed = True
            projected_sell_release_value += _projected_sell_release_value_for_report(
                report,
                fallback_price=price,
                fallback_quantity=qty,
            )

    return sell_executed, projected_sell_release_value, dry_run_sale_proceeds, pending_sell_release_symbols, False


def _refresh_post_sell_buying_power(
    ib,
    *,
    account_values,
    allocation_plan: _RebalanceAllocationPlan,
    anticipated_buying_power: float,
    sell_executed: bool,
    dry_run_sale_proceeds: float,
    options: RebalanceExecutionOptions,
) -> float:
    if options.cash_only_execution:
        # A sale fill or ACK does not establish settlement. A fresh observation
        # supplies the budget; projected proceeds never supply spending authority.
        observed = _settled_cash_for_currency(
            ib,
            currency=options.market_currency,
            account_ids=allocation_plan.normalized_account_ids,
        )
        if observed is None:
            allocation_plan.execution_summary["settled_cash_ledger_status"] = "UNKNOWN"
        return max(0.0, observed or 0.0)
    if options.dry_run_only:
        return max(0.0, anticipated_buying_power + dry_run_sale_proceeds)
    if sell_executed:
        _facade_attr('time', time).sleep(options.sell_settle_delay_sec)
        return get_available_buying_power(
            ib,
            account_values.get("buying_power", 0),
            account_ids=allocation_plan.normalized_account_ids,
            currency=options.market_currency,
            cash_only_execution=False,
        )
    return anticipated_buying_power


def _resolve_buy_block_reason(
    ib,
    *,
    signal_metadata: dict[str, Any],
    allocation_plan: _RebalanceAllocationPlan,
    current_mv: dict[str, float],
    target_mv: dict[str, float],
    prices: dict[str, float],
    investable_buying_power: float,
    pending_sell_release_symbols: list[str],
    buy_needed_symbols: list[str],
    trade_logs: list[str],
    translator,
    options: RebalanceExecutionOptions,
) -> str | None:
    execution_summary = allocation_plan.execution_summary
    if bool(signal_metadata.get("small_account_warning")) and buy_needed_symbols:
        reason = "small_account_below_recommended_equity"
        execution_summary["small_account_buy_blocked"] = True
        execution_summary["small_account_buy_block_reason"] = reason
        execution_summary["skipped_reasons"].append(reason)
        trade_logs.append(
            translator(
                "buy_deferred_small_account",
                portfolio_equity=f"{float(allocation_plan.equity or 0.0):,.2f}",
                min_recommended_equity=f"{float(signal_metadata.get('min_recommended_equity_usd') or 0.0):,.2f}",
            )
        )
        return reason
    if allocation_plan.account_new_risk_buy_blocked and buy_needed_symbols:
        reason = "account_new_risk_gate"
        execution_summary["account_new_risk_buy_blocked"] = True
        execution_summary["account_new_risk_block_reason"] = reason
        execution_summary["account_new_risk_reason_codes"] = list(allocation_plan.account_new_risk_reason_codes)
        execution_summary["skipped_reasons"].append(reason)
        reason_text = ", ".join(allocation_plan.account_new_risk_reason_codes) or "NEW_RISK_PROHIBITED"
        trade_logs.append(
            translator(
                "buy_deferred",
                detail=f"[Account new-risk gate] {reason_text}",
            )
        )
        return reason
    if options.cash_only_execution and pending_sell_release_symbols and buy_needed_symbols:
        if _rotation_guard_should_block_buys(
            pending_sell_release_symbols=pending_sell_release_symbols,
            buy_needed_symbols=buy_needed_symbols,
            current_mv=current_mv,
            target_mv=target_mv,
            threshold=allocation_plan.threshold,
            investable_buying_power=investable_buying_power,
            prices=prices,
            limit_buy_premium=options.limit_buy_premium,
            limit_buy_premium_by_symbol=options.limit_buy_premium_by_symbol,
            quantity_step=allocation_plan.order_quantity_step,
            minimum_order_notional=allocation_plan.minimum_order_notional,
        ):
            release_symbols = _format_symbol_preview(tuple(pending_sell_release_symbols))
            execution_summary["pending_sell_release_symbols"] = list(pending_sell_release_symbols)
            execution_summary["skipped_reasons"].append(
                f"pending_sell_release:{','.join(pending_sell_release_symbols)}"
            )
            trade_logs.append(
                translator(
                    "buy_deferred_pending_sell_release",
                    symbols=release_symbols,
                )
            )
            return "pending_sell_release"
    if options.cash_only_execution:
        raw_cash = _cash_value_for_currency(
            list(ib.accountValues() or ()),
            currency=options.market_currency,
            account_ids=allocation_plan.normalized_account_ids,
        )
        if raw_cash is not None and float(raw_cash) < 0.0 and buy_needed_symbols:
            execution_summary["skipped_reasons"].append(f"negative_cash:{float(raw_cash):.2f}")
            trade_logs.append(
                translator(
                    "buy_deferred_negative_cash",
                    cash=f"{float(raw_cash):,.2f}",
                )
            )
            return "negative_cash"
    return None


def _execute_buy_orders(
    ib,
    *,
    positions,
    allocation_plan: _RebalanceAllocationPlan,
    current_mv: dict[str, float],
    target_mv: dict[str, float],
    prices: dict[str, float],
    investable_buying_power: float,
    buy_needed_symbols: list[str],
    buys_blocked_reason: str | None,
    claimed_submitter,
    order_intent_cls,
    translator,
    trade_logs: list[str],
    options: RebalanceExecutionOptions,
) -> bool:
    execution_summary = allocation_plan.execution_summary
    threshold = allocation_plan.threshold
    for symbol, target in target_mv.items():
        if buys_blocked_reason:
            if symbol in buy_needed_symbols:
                execution_summary["orders_skipped"].append(
                    {"symbol": symbol, "side": "buy", "reason": buys_blocked_reason}
                )
            continue
        current = current_mv.get(symbol, 0)
        if current >= target - threshold:
            continue
        refresh_started = 0.0
        if options.cash_only_execution:
            refresh_started = time.monotonic()
            # A previous order may take longer than the freshness bound.
            # Refresh each candidate without resetting the shared budget.
            observed = _settled_cash_for_currency(
                ib,
                currency=options.market_currency,
                account_ids=allocation_plan.normalized_account_ids,
            )
            investable_buying_power = min(
                investable_buying_power,
                _investable_buying_power(max(0.0, observed or 0.0), allocation_plan.reserved),
            )
            if observed is None or observed <= 0:
                reason = "settled_cash_ledger_unverified" if observed is None else "insufficient_settled_cash"
                if observed is None:
                    execution_summary["settled_cash_ledger_status"] = "UNKNOWN"
                execution_summary["orders_skipped"].append({"symbol": symbol, "side": "buy", "reason": reason})
                execution_summary["skipped_reasons"].append(f"{reason}:{symbol}")
                continue
        buy_value = min(target - current, investable_buying_power * 0.95)
        price = prices.get(symbol)
        if not price:
            execution_summary["orders_skipped"].append({"symbol": symbol, "side": "buy", "reason": "missing_price"})
            execution_summary["skipped_reasons"].append(f"missing_price:{symbol}")
            continue
        if buy_value < allocation_plan.minimum_order_notional:
            execution_summary["orders_skipped"].append({"symbol": symbol, "side": "buy", "reason": "min_notional"})
            continue

        limit_price = _limit_buy_price(
            symbol,
            price,
            options.limit_buy_premium,
            options.limit_buy_premium_by_symbol,
        )
        held_quantity = max(0.0, float(positions.get(symbol, {}).get("quantity", 0.0) or 0.0))
        qty, _forced_whole_share = _facade_attr("_planned_buy_order_quantity", _planned_buy_order_quantity)(
            symbol,
            buy_value=buy_value,
            limit_price=limit_price,
            quantity_step=allocation_plan.order_quantity_step,
            investable_buying_power=investable_buying_power,
            held_quantity=held_quantity,
        )
        if qty <= 0:
            execution_summary["orders_skipped"].append({"symbol": symbol, "side": "buy", "reason": "quantity_zero"})
            continue

        order_cost = float(qty) * float(limit_price)
        if order_cost > investable_buying_power:
            qty = _floor_order_quantity(
                investable_buying_power / limit_price,
                quantity_step=allocation_plan.order_quantity_step,
            )
            order_cost = float(qty) * float(limit_price)
        if qty <= 0 or order_cost > investable_buying_power:
            execution_summary["orders_skipped"].append(
                {"symbol": symbol, "side": "buy", "reason": "insufficient_buying_power"}
            )
            execution_summary["skipped_reasons"].append(f"insufficient_buying_power:{symbol}")
            continue

        remaining_approved = allocation_plan.approved_target_caps[symbol] - float(current_mv.get(symbol, 0.0) or 0.0)
        if (
            symbol not in allocation_plan.strategy_symbols
            or not math.isfinite(float(qty))
            or not math.isfinite(float(limit_price))
            or float(qty) <= 0.0
            or float(limit_price) <= 0.0
            or order_cost > remaining_approved + 1e-6
        ):
            raise RuntimeError("IBKR live buy exceeds the approved RiskEngine order limit")
        if options.cash_only_execution and not 0 <= time.monotonic() - refresh_started <= SETTLED_CASH_MAX_AGE_SECONDS:
            execution_summary["settled_cash_ledger_status"] = "UNKNOWN"
            execution_summary["orders_skipped"].append({"symbol": symbol, "side": "buy", "reason": "settled_cash_stale"})
            execution_summary["skipped_reasons"].append(f"settled_cash_stale:{symbol}")
            continue
        if options.dry_run_only:
            execution_summary["orders_submitted"].append(
                {
                    "symbol": symbol,
                    "side": "buy",
                    "quantity": qty,
                    "limit_price": limit_price,
                    "status": "dry_run",
                }
            )
            trade_logs.append(f"DRY_RUN buy {symbol} {format_quantity(qty)} @{limit_price:.2f}")
            investable_buying_power -= qty * limit_price
            continue
        intent = order_intent_cls(
            symbol=symbol,
            side="buy",
            quantity=qty,
            order_type="limit",
            limit_price=limit_price,
            time_in_force="DAY",
            account_id=allocation_plan.order_account_id,
        )
        if options.cash_only_execution:
            with _SettledCashDeadlineIB(
                ib,
                refresh_started=refresh_started,
                account_id=allocation_plan.order_account_id,
                currency=options.market_currency,
                buying_power=investable_buying_power,
                reserved=allocation_plan.reserved,
                quantity_step=allocation_plan.order_quantity_step,
                minimum_order_notional=allocation_plan.minimum_order_notional,
            ) as settled_ib:
                report = claimed_submitter(settled_ib, intent)
                investable_buying_power = min(investable_buying_power, settled_ib.remaining_buying_power)
                if settled_ib.prepared_quantity is not None:
                    qty = settled_ib.prepared_quantity
        else:
            report = claimed_submitter(ib, intent)
        outcome = _record_submitted_equity_order(
            execution_summary,
            trade_logs,
            report,
            symbol=symbol,
            side="buy",
            quantity=qty,
            order_account_id=allocation_plan.order_account_id,
            translator=translator,
            limit_price=limit_price,
        )
        if outcome == "unknown":
            return True
        if outcome != "failed":
            investable_buying_power -= qty * limit_price
    return False

