"""Top-level IBKR rebalance orchestration."""
from __future__ import annotations

from typing import Any

from interactive_brokers_platform.application.paper_execution_admission import evaluate_ibkr_paper_execution_admission

from interactive_brokers_platform.application._execution_account_state import (
    _PendingOrdersUnavailableError,
    _collect_same_day_filled_symbols,
    _investable_buying_power,
    _observe_pending_orders,
    _settled_cash_for_currency,
    get_available_buying_power,
)
from interactive_brokers_platform.application._execution_planning_helpers import (
    RebalanceExecutionOptions,
    _DriftNotesContext,
    _RebalanceAllocationPlan,
    _RebalanceOrderOutcome,
    _RebalanceQuoteCollection,
    _UNSET,
    _display_text,
    _execute_option_order_intents,
    _finalize_result,
    _resolve_rebalance_options,
)
from interactive_brokers_platform.application._execution_allocation import (
    _append_execution_detail_logs,
    _append_small_account_allocation_drift_notes,
    _build_claimed_submitter,
    _collect_rebalance_quotes,
    _plan_rebalance_directions,
    _prepare_post_quote_targets,
    _record_blocked_step,
    _record_quote_collection_summary,
    _resolve_rebalance_allocation,
)
from interactive_brokers_platform.application._execution_order_steps import (
    _acquire_cycle_execution_lock,
    _execute_buy_orders,
    _execute_sell_orders,
    _refresh_post_sell_buying_power,
    _resolve_buy_block_reason,
)

def _execute_rebalance_orders(
    ib,
    *,
    positions,
    account_values,
    signal_metadata: dict[str, Any],
    allocation_plan: _RebalanceAllocationPlan,
    quote_collection: _RebalanceQuoteCollection,
    claimed_submitter,
    order_intent_cls,
    translator,
    options: RebalanceExecutionOptions,
) -> _RebalanceOrderOutcome:
    """Apply small-account compatibility, run broker safety checks, and execute orders."""
    execution_summary = allocation_plan.execution_summary
    _record_quote_collection_summary(execution_summary, quote_collection)
    prices = quote_collection.prices
    current_mv, _current_quantities, target_mv, trade_logs, target_hash, drift_context = (
        _prepare_post_quote_targets(
            positions=positions,
            account_values=account_values,
            allocation_plan=allocation_plan,
            prices=prices,
            translator=translator,
        )
    )
    if options.paper_execution_admission_enabled:
        paper_admission = evaluate_ibkr_paper_execution_admission(
            signal_metadata=signal_metadata,
            strategy_profile=options.strategy_profile,
            account_scope=options.account_group,
            positions=positions,
            prices=prices,
            target_market_values=target_mv,
            option_order_intents=allocation_plan.option_order_intents,
            runtime_release_receipt=options.runtime_release_receipt,
            expected_strategy_release=options.expected_strategy_release,
        )
        execution_summary["paper_execution_admission"] = paper_admission
        if not paper_admission["broker_write_allowed"]:
            _record_blocked_step(
                execution_summary,
                trade_logs,
                reason="paper_execution_admission_blocked",
                translator=translator,
            )
            return _RebalanceOrderOutcome(trade_logs=trade_logs, finalized_early=True)

    if allocation_plan.equity > 0:
        safe_haven_symbol = allocation_plan.safe_haven_symbol
        current_safe_haven_mv = current_mv.get(safe_haven_symbol, 0.0) if safe_haven_symbol else 0.0
        execution_summary["current_safe_haven_weight"] = float(current_safe_haven_mv / allocation_plan.equity)
        execution_summary["current_stock_weight"] = float(
            (sum(current_mv.values()) - current_safe_haven_mv) / allocation_plan.equity
        )

    try:
        pending_observation = _observe_pending_orders(
            ib,
            set(allocation_plan.all_symbols) | set(allocation_plan.option_underliers),
            account_ids=allocation_plan.normalized_account_ids,
        )
    except _PendingOrdersUnavailableError:
        _record_blocked_step(
            execution_summary,
            trade_logs,
            reason="pending_orders_unavailable",
            translator=translator,
        )
        return _RebalanceOrderOutcome(trade_logs=trade_logs, finalized_early=True)
    if pending_observation.symbols:
        pending_symbols_csv = ",".join(pending_observation.symbols)
        _record_blocked_step(
            execution_summary,
            trade_logs,
            reason=f"pending_orders_detected:{pending_symbols_csv}",
            translator=translator,
            log_message=translator(
                "pending_orders_detected",
                profile=_display_text(options.strategy_profile, fallback="<unknown>"),
                symbols=pending_symbols_csv,
            ),
        )
        return _RebalanceOrderOutcome(trade_logs=trade_logs, finalized_early=True)
    if pending_observation.cash_risk_unknown:
        # Reuse the existing BUY gate, without vetoing otherwise-qualified
        # reductions. The normal claim, lock and sell limits still apply.
        allocation_plan.account_new_risk_buy_blocked = True
        allocation_plan.account_new_risk_reason_codes = (
            *allocation_plan.account_new_risk_reason_codes,
            "PENDING_ORDER_CASH_RISK_UNKNOWN",
        )
        execution_summary["pending_order_cash_risk_unknown"] = True

    settled_cash = (
        _settled_cash_for_currency(
            ib,
            currency=options.market_currency,
            account_ids=allocation_plan.normalized_account_ids,
        )
        if options.cash_only_execution
        else None
    )
    settled_cash_ledger_unverified = options.cash_only_execution and settled_cash is None
    if settled_cash_ledger_unverified:
        allocation_plan.account_new_risk_buy_blocked = True
        allocation_plan.account_new_risk_reason_codes = (
            *allocation_plan.account_new_risk_reason_codes,
            "SETTLED_CASH_LEDGER_UNVERIFIED",
        )
        execution_summary["settled_cash_ledger_status"] = "UNKNOWN"
    elif options.cash_only_execution:
        execution_summary["settled_cash_ledger_status"] = "OBSERVED"

    _append_execution_detail_logs(
        trade_logs,
        allocation_plan=allocation_plan,
        quote_collection=quote_collection,
        current_mv=current_mv,
        signal_metadata=signal_metadata,
        translator=translator,
        dry_run_only=options.dry_run_only,
    )

    anticipated_buying_power = (
        max(0.0, settled_cash or 0.0)
        if options.cash_only_execution
        else get_available_buying_power(
            ib,
            account_values.get("buying_power", 0),
            account_ids=allocation_plan.normalized_account_ids,
            currency=options.market_currency,
            cash_only_execution=False,
        )
    )
    investable_anticipated_buying_power = _investable_buying_power(
        anticipated_buying_power,
        allocation_plan.reserved,
    )
    cash_sweep_quantity, plan_ended_early = _plan_rebalance_directions(
        positions=positions,
        allocation_plan=allocation_plan,
        current_mv=current_mv,
        target_mv=target_mv,
        prices=prices,
        investable_anticipated_buying_power=investable_anticipated_buying_power,
        pending_cash_risk_unknown=pending_observation.cash_risk_unknown,
        settled_cash_ledger_unverified=settled_cash_ledger_unverified,
        execution_summary=execution_summary,
        trade_logs=trade_logs,
        translator=translator,
        options=options,
        drift_context=drift_context,
    )
    if plan_ended_early:
        return _RebalanceOrderOutcome(trade_logs=trade_logs, finalized_early=True)

    same_day_filled_symbols = _collect_same_day_filled_symbols(
        ib,
        set(allocation_plan.all_symbols) | set(allocation_plan.option_underliers),
        allocation_plan.trade_date,
        account_ids=allocation_plan.normalized_account_ids,
    )
    if same_day_filled_symbols:
        filled_csv = ",".join(same_day_filled_symbols)
        _record_blocked_step(
            execution_summary,
            trade_logs,
            reason=f"same_day_fills_detected:{filled_csv}",
            translator=translator,
            log_message=translator(
                "same_day_fills_detected",
                profile=_display_text(options.strategy_profile, fallback="<unknown>"),
                mode=allocation_plan.resolved_execution_mode,
                symbols=filled_csv,
                trade_date=_display_text(allocation_plan.trade_date, fallback="<none>"),
            ),
        )
        return _RebalanceOrderOutcome(trade_logs=trade_logs, finalized_early=True)

    if not _acquire_cycle_execution_lock(
        allocation_plan=allocation_plan,
        target_hash=target_hash,
        trade_logs=trade_logs,
        translator=translator,
        options=options,
    ):
        return _RebalanceOrderOutcome(trade_logs=trade_logs, finalized_early=True)

    (
        sell_executed,
        projected_sell_release_value,
        dry_run_sale_proceeds,
        pending_sell_release_symbols,
        sell_unknown,
    ) = _execute_sell_orders(
        ib,
        positions=positions,
        allocation_plan=allocation_plan,
        current_mv=current_mv,
        target_mv=target_mv,
        prices=prices,
        cash_sweep_quantity=cash_sweep_quantity,
        claimed_submitter=claimed_submitter,
        order_intent_cls=order_intent_cls,
        translator=translator,
        trade_logs=trade_logs,
        dry_run_only=options.dry_run_only,
    )
    if sell_unknown:
        return _RebalanceOrderOutcome(trade_logs=trade_logs, finalized_early=True)

    buying_power = _refresh_post_sell_buying_power(
        ib,
        account_values=account_values,
        allocation_plan=allocation_plan,
        anticipated_buying_power=anticipated_buying_power,
        sell_executed=sell_executed,
        dry_run_sale_proceeds=dry_run_sale_proceeds,
        options=options,
    )
    investable_buying_power = _investable_buying_power(buying_power, allocation_plan.reserved)
    execution_summary["projected_sell_release_value"] = float(projected_sell_release_value)
    deduped_pending_releases = list(dict.fromkeys(pending_sell_release_symbols))
    buy_needed_symbols = [
        symbol
        for symbol, target in target_mv.items()
        if current_mv.get(symbol, 0.0) < float(target or 0.0) - allocation_plan.threshold
    ]
    buys_blocked_reason = _resolve_buy_block_reason(
        ib,
        signal_metadata=signal_metadata,
        allocation_plan=allocation_plan,
        current_mv=current_mv,
        target_mv=target_mv,
        prices=prices,
        investable_buying_power=investable_buying_power,
        pending_sell_release_symbols=deduped_pending_releases,
        buy_needed_symbols=buy_needed_symbols,
        trade_logs=trade_logs,
        translator=translator,
        options=options,
    )
    buy_unknown = _execute_buy_orders(
        ib,
        positions=positions,
        allocation_plan=allocation_plan,
        current_mv=current_mv,
        target_mv=target_mv,
        prices=prices,
        investable_buying_power=investable_buying_power,
        buy_needed_symbols=buy_needed_symbols,
        buys_blocked_reason=buys_blocked_reason,
        claimed_submitter=claimed_submitter,
        order_intent_cls=order_intent_cls,
        translator=translator,
        trade_logs=trade_logs,
        options=options,
    )
    if buy_unknown:
        return _RebalanceOrderOutcome(trade_logs=trade_logs, finalized_early=True)

    buying_power = _execute_option_order_intents(
        ib,
        allocation_plan.option_order_intents,
        submit_order_intent=claimed_submitter,
        order_intent_cls=order_intent_cls,
        translator=translator,
        execution_summary=execution_summary,
        trade_logs=trade_logs,
        dry_run_only=options.dry_run_only,
        order_account_id=allocation_plan.order_account_id,
        buying_power=buying_power,
    )
    return _RebalanceOrderOutcome(
        trade_logs=trade_logs,
        buying_power=buying_power,
        finalized_early=False,
        drift_context=drift_context,
    )


def _build_rebalance_summary(
    execution_summary: dict[str, Any],
    *,
    trade_logs: list[str],
    translator,
    buying_power: float,
    drift_context: _DriftNotesContext | None = None,
) -> dict[str, Any]:
    """Finalize rebalance execution status, residual cash estimate, and drift notes."""
    has_terminal_order = bool(
        execution_summary["orders_submitted"]
        or execution_summary["orders_filled"]
        or execution_summary["orders_partially_filled"]
        or execution_summary["option_orders_submitted"]
        or execution_summary["option_orders_filled"]
        or execution_summary["option_orders_partially_filled"]
    )
    has_pending_order = bool(
        execution_summary["orders_pending"] or execution_summary["option_orders_pending"]
    )
    submission_failure = next(
        (
            reason
            for reason in execution_summary["skipped_reasons"]
            if reason.startswith(("submit_failed:", "option_submit_failed:"))
        ),
        None,
    )
    if has_pending_order:
        execution_summary["execution_status"] = "pending_reconciliation"
        execution_summary["no_op_reason"] = "broker_order_pending_confirmation"
    elif has_terminal_order:
        execution_summary["execution_status"] = "executed"
        execution_summary["no_op_reason"] = None
    elif submission_failure:
        execution_summary["execution_status"] = "blocked"
        execution_summary["no_op_reason"] = submission_failure
        trade_logs.append(translator("failed", reason=submission_failure))
    else:
        execution_summary["execution_status"] = "no_op"
    execution_summary["residual_cash_estimate"] = float(max(buying_power, 0.0))
    if drift_context is not None:
        _append_small_account_allocation_drift_notes(
            execution_summary,
            trade_logs,
            translator=translator,
            context=drift_context,
        )
    return execution_summary


def execute_rebalance(
    ib,
    target_weights,
    positions,
    account_values,
    *,
    fetch_quote_snapshots,
    submit_order_intent,
    order_intent_cls,
    translator,
    strategy_symbols=None,
    signal_metadata=None,
    acquire_execution_claim=None,
    account_execution_session=None,
    strategy_profile=_UNSET,
    account_group=_UNSET,
    service_name=_UNSET,
    account_ids=_UNSET,
    dry_run_only=_UNSET,
    execution_mode=_UNSET,
    cash_reserve_ratio=_UNSET,
    rebalance_threshold_ratio=_UNSET,
    limit_buy_premium=_UNSET,
    sell_settle_delay_sec=_UNSET,
    cash_reserve_floor_usd=_UNSET,
    quantity_step=_UNSET,
    min_order_notional=_UNSET,
    safe_haven_cash_substitute_threshold_usd=_UNSET,
    market_currency=_UNSET,
    limit_buy_premium_by_symbol=_UNSET,
    execution_lock_dir=_UNSET,
    return_summary=_UNSET,
    cash_only_execution=_UNSET,
    paper_execution_admission_enabled=_UNSET,
    runtime_release_receipt=_UNSET,
    expected_strategy_release=_UNSET,
    options: RebalanceExecutionOptions | None = None,
):
    """Execute trades to reach target weights."""
    del target_weights, strategy_symbols
    resolved_options = _resolve_rebalance_options(
        options,
        strategy_profile=strategy_profile,
        account_group=account_group,
        service_name=service_name,
        account_ids=account_ids,
        dry_run_only=dry_run_only,
        execution_mode=execution_mode,
        cash_reserve_ratio=cash_reserve_ratio,
        rebalance_threshold_ratio=rebalance_threshold_ratio,
        limit_buy_premium=limit_buy_premium,
        sell_settle_delay_sec=sell_settle_delay_sec,
        cash_reserve_floor_usd=cash_reserve_floor_usd,
        quantity_step=quantity_step,
        min_order_notional=min_order_notional,
        safe_haven_cash_substitute_threshold_usd=safe_haven_cash_substitute_threshold_usd,
        market_currency=market_currency,
        limit_buy_premium_by_symbol=limit_buy_premium_by_symbol,
        execution_lock_dir=execution_lock_dir,
        return_summary=return_summary,
        cash_only_execution=cash_only_execution,
        paper_execution_admission_enabled=paper_execution_admission_enabled,
        runtime_release_receipt=runtime_release_receipt,
        expected_strategy_release=expected_strategy_release,
    )
    normalized_signal_metadata = signal_metadata or {}
    claimed_submitter = _build_claimed_submitter(
        submit_order_intent=submit_order_intent,
        account_values=account_values,
        signal_metadata=normalized_signal_metadata,
        acquire_execution_claim=acquire_execution_claim,
        account_execution_session=account_execution_session,
        options=resolved_options,
    )
    allocation_plan = _resolve_rebalance_allocation(
        positions=positions,
        account_values=account_values,
        signal_metadata=normalized_signal_metadata,
        account_execution_session=account_execution_session,
        translator=translator,
        options=resolved_options,
    )
    if allocation_plan.early_logs is not None:
        return _finalize_result(
            allocation_plan.early_logs,
            allocation_plan.execution_summary,
            return_summary=resolved_options.return_summary,
        )
    quote_collection = _collect_rebalance_quotes(
        ib,
        allocation_plan.all_symbols,
        signal_metadata=normalized_signal_metadata,
        fetch_quote_snapshots=fetch_quote_snapshots,
        dry_run_only=resolved_options.dry_run_only,
    )
    order_outcome = _execute_rebalance_orders(
        ib,
        positions=positions,
        account_values=account_values,
        signal_metadata=normalized_signal_metadata,
        allocation_plan=allocation_plan,
        quote_collection=quote_collection,
        claimed_submitter=claimed_submitter,
        order_intent_cls=order_intent_cls,
        translator=translator,
        options=resolved_options,
    )
    if order_outcome.finalized_early:
        return _finalize_result(
            order_outcome.trade_logs,
            allocation_plan.execution_summary,
            return_summary=resolved_options.return_summary,
        )
    summary = _build_rebalance_summary(
        allocation_plan.execution_summary,
        trade_logs=order_outcome.trade_logs,
        translator=translator,
        buying_power=order_outcome.buying_power,
        drift_context=order_outcome.drift_context,
    )
    return _finalize_result(
        order_outcome.trade_logs,
        summary,
        return_summary=resolved_options.return_summary,
    )

