"""Allocation resolution, quote collection, and direction planning."""
from __future__ import annotations

import math
from typing import Any

from interactive_brokers_platform.application.account_new_risk_gate_support import (
    scale_targets_for_admission,
    build_portfolio_from_account_values,
    build_snapshot_from_portfolio,
    copy_fuse_telemetry,
    evaluate_account_values_new_risk_admission,
    evaluate_persistent_account_new_risk_admission,
    fuse_telemetry_from_admission,
    is_effective_new_risk_gate_enabled,
    maybe_publish_attention_for_admission,
    new_risk_buy_prohibited,
    set_cycle_snapshot,
)
from interactive_brokers_platform.application.execution_kernel_adapter import consult_t1_live_submit
from quant_platform_kit.common.cash_sweep import should_sell_cash_sweep_to_fund_whole_share_buy
from quant_platform_kit.common.small_account_compatibility import (
    apply_small_account_cash_compatibility,
    build_small_account_allocation_drift_notes,
    format_small_account_allocation_drift_notes,
    format_small_account_cash_substitution_notes,
)

from interactive_brokers_platform.application._execution_account_state import (
    _facade_attr,
    _investable_buying_power,
    _SettledCashDeadlineIB,
    _normalize_account_ids,
    _normalize_date_like,
    _resolve_order_account_id,
    _serialize_quote_snapshot,
    get_market_prices,
)
from interactive_brokers_platform.application._execution_planning_helpers import (
    RebalanceExecutionOptions,
    SMALL_ACCOUNT_SAFE_HAVEN_CASH_SUBSTITUTE_LIMIT_USD,
    _DriftNotesContext,
    _RebalanceAllocationPlan,
    _RebalanceQuoteCollection,
    _apply_safe_haven_cash_substitution_to_weights,
    _apply_snapshot_price_fallbacks,
    _build_target_diff_rows,
    _build_target_hash,
    _display_text,
    _format_symbol_preview,
    _format_target_lines,
    _has_executable_option_plan,
    _limit_buy_price,
    _normalize_option_order_intents,
    _normalize_price_fallbacks,
    _option_intent_underliers,
    _planned_buy_order_quantity,
    _record_unsupported_option_intents,
    _require_live_risk_authority,
    _require_tqqq_effective_date,
    _resolve_execution_mode,
    _resolve_weight_allocation,
    _sell_order_quantity,
    _sum_safe_haven_weight,
)

def _build_claimed_submitter(
    *,
    submit_order_intent,
    account_values,
    signal_metadata: dict[str, Any],
    acquire_execution_claim,
    account_execution_session,
    options: RebalanceExecutionOptions,
):
    """Build the explicit broker order submitter with claim, session, and breaker gates."""
    if options.dry_run_only:
        return submit_order_intent

    def submit_claimed_order(ib, order_intent, **kwargs):
        from quant_platform_kit.common import execution_state as contract

        session_type = getattr(contract, "AccountSubmissionSession", ())
        if (
            not isinstance(account_execution_session, session_type)
            or account_execution_session.contract_version != "account_submission.v1"
        ):
            raise RuntimeError("IBKR account submission session/contract required")
        if (
            account_execution_session.broker != "ibkr"
            or account_execution_session.release_id != contract.submission_release_digest(options.runtime_release_receipt)
            or account_execution_session.intent_id
            != contract.submission_intent_digest(signal_metadata, options.strategy_profile)
            or account_execution_session.profile != options.strategy_profile
        ):
            raise RuntimeError("IBKR account session intent/release binding mismatch")
        _consult_t1 = _facade_attr("consult_t1_live_submit", consult_t1_live_submit)
        if acquire_execution_claim is None or not acquire_execution_claim():
            # N13 T1 consult-only: local claim still wins; ignore decision.
            _consult_t1(identity_held=False, dry_run_bypass=False)
            raise RuntimeError("IBKR execution claim required; refusing broker submission")
        # Shadow consult after successful claim; deny must not block submit.
        _consult_t1(identity_held=True, dry_run_bypass=False)
        portfolio = build_portfolio_from_account_values(account_values, signal_metadata=signal_metadata)
        new_risk = str(order_intent.side).strip().lower() == "buy" and is_effective_new_risk_gate_enabled(portfolio)
        if new_risk:
            latest = evaluate_persistent_account_new_risk_admission(
                account_values,
                signal_metadata=signal_metadata,
                session=account_execution_session,
            )
            if new_risk_buy_prohibited(latest):
                raise RuntimeError("IBKR persistent account breaker prohibits new risk")
        # Settlement preparation must finish before reserving the exact intent.
        grant = (
            None
            if isinstance(ib, _SettledCashDeadlineIB)
            else account_execution_session.reserve(order_intent, new_risk=new_risk)
        )
        return submit_order_intent(
            ib,
            order_intent,
            account_execution_session=account_execution_session,
            execution_grant=grant,
            **kwargs,
        )

    return submit_claimed_order


def _init_execution_summary(
    *,
    resolved_execution_mode: str,
    normalized_account_ids: tuple[str, ...],
    order_account_id: str | None,
    trade_date: str | None,
    snapshot_date: str | None,
    safe_haven_symbol: str | None,
    option_order_intents: tuple[dict[str, Any], ...],
    option_underliers: tuple[str, ...],
    signal_metadata: dict[str, Any],
    account_values,
    options: RebalanceExecutionOptions,
) -> dict[str, Any]:
    return {
        "mode": resolved_execution_mode,
        "strategy_profile": options.strategy_profile,
        "account_group": options.account_group,
        "account_ids": list(normalized_account_ids),
        "order_account_id": order_account_id,
        "service_name": options.service_name,
        "trade_date": trade_date,
        "snapshot_as_of": snapshot_date,
        "safe_haven_symbol": safe_haven_symbol,
        "market_currency": str(options.market_currency or "USD").strip().upper(),
        "target_stock_weight": signal_metadata.get("target_stock_weight"),
        "realized_stock_weight": signal_metadata.get("realized_stock_weight"),
        "target_safe_haven_weight": signal_metadata.get("safe_haven_weight"),
        "realized_safe_haven_weight": signal_metadata.get("safe_haven_weight"),
        "orders_submitted": [],
        "orders_pending": [],
        "orders_filled": [],
        "orders_partially_filled": [],
        "orders_skipped": [],
        "option_order_intent_count": len(option_order_intents),
        "option_order_underliers": list(option_underliers),
        "option_orders_submitted": [],
        "option_orders_pending": [],
        "option_orders_filled": [],
        "option_orders_partially_filled": [],
        "option_orders_skipped": [],
        "skipped_reasons": [],
        "target_vs_current": [],
        "execution_status": "not_started",
        "no_op_reason": None,
        "cash_reserve_dollars": 0.0,
        "safe_haven_cash_substitute_threshold_usd": float(
            max(0.0, float(options.safe_haven_cash_substitute_threshold_usd or 0.0))
        ),
        "safe_haven_cash_substituted_symbols": [],
        "small_account_whole_share_substituted_symbols": [],
        "small_account_safe_haven_cash_substituted_symbols": [],
        "small_account_whole_share_cash_notes": [],
        "small_account_allocation_drift_notes": [],
        "small_account_buy_blocked": False,
        "small_account_buy_block_reason": None,
        "account_new_risk_buy_blocked": False,
        "account_new_risk_block_reason": None,
        "account_new_risk_reason_codes": [],
        "residual_cash_estimate": float(account_values.get("buying_power", 0.0) or 0.0),
        "projected_sell_release_value": 0.0,
        "current_stock_weight": 0.0,
        "current_safe_haven_weight": 0.0,
        "price_source_mode": "market_quote",
        "snapshot_price_fallback_used": False,
        "snapshot_price_fallback_symbols": [],
        "snapshot_price_fallback_count": 0,
        "lock_path": None,
        "paper_execution_admission": {},
    }


def _apply_account_new_risk_gate(
    target_weights: dict[str, float],
    *,
    account_values,
    signal_metadata: dict[str, Any],
    account_execution_session,
    execution_summary: dict[str, Any],
    dry_run_only: bool,
) -> tuple[dict[str, float], bool, tuple[str, ...]]:
    portfolio = build_portfolio_from_account_values(account_values, signal_metadata=signal_metadata)
    if not is_effective_new_risk_gate_enabled(portfolio):
        set_cycle_snapshot(None)
        return target_weights, False, ()
    # Inner dry-run previews without a session retain their diagnostic
    # purpose; they make no persistence claim. Native cycles always restore
    # the journal, including no-target and dry-run cycles with a store.
    admission = _facade_attr('evaluate_account_values_new_risk_admission', evaluate_account_values_new_risk_admission)(account_values, signal_metadata=signal_metadata)
    if dry_run_only and account_execution_session is None:
        execution_summary["persistent_breaker_status"] = "NOT_PROVEN"
    else:
        admission = evaluate_persistent_account_new_risk_admission(
            account_values,
            signal_metadata=signal_metadata,
            session=account_execution_session,
        )
    account_new_risk_buy_blocked = new_risk_buy_prohibited(admission)
    account_new_risk_reason_codes = tuple(admission.reason_codes)
    copy_fuse_telemetry(execution_summary, fuse_telemetry_from_admission(admission))
    portfolio = build_portfolio_from_account_values(account_values, signal_metadata=signal_metadata)
    cycle_snapshot = build_snapshot_from_portfolio(portfolio)
    set_cycle_snapshot(cycle_snapshot)
    attention_counts = _facade_attr('maybe_publish_attention_for_admission', maybe_publish_attention_for_admission)(
        admission,
        portfolio=portfolio,
        snapshot=cycle_snapshot,
    )
    print(
        "[Attention notify] "
        f"sent={attention_counts.get('sent', 0)} "
        f"skipped={attention_counts.get('skipped', 0)} "
        f"failed={attention_counts.get('failed', 0)}",
        flush=True,
    )
    scaled_weights = scale_targets_for_admission(target_weights, admission, snapshot=cycle_snapshot)
    if admission.combined_scale is not None:
        print(
            f"[Envelope scale] combined_scale={admission.combined_scale} "
            "subject_to_stoploss_exit_authority",
            flush=True,
        )
    return scaled_weights, account_new_risk_buy_blocked, account_new_risk_reason_codes


def _resolve_rebalance_allocation(
    *,
    positions,
    account_values,
    signal_metadata: dict[str, Any],
    account_execution_session,
    translator,
    options: RebalanceExecutionOptions,
) -> _RebalanceAllocationPlan:
    """Validate authority, resolve target weights, and apply account risk-gate scaling."""
    _require_tqqq_effective_date(signal_metadata, strategy_profile=options.strategy_profile)
    allocation = _resolve_weight_allocation(signal_metadata)
    target_weights = dict(allocation["targets"])
    approved_target_caps = _require_live_risk_authority(
        signal_metadata,
        allocation,
        strategy_profile=options.strategy_profile,
        account_ids=options.account_ids,
    )
    option_order_intents = _normalize_option_order_intents(signal_metadata)
    option_underliers = _option_intent_underliers(option_order_intents)
    has_executable_option_plan = _has_executable_option_plan(option_order_intents)
    if has_executable_option_plan:
        raise RuntimeError("IBKR live option intents lack final RiskEngine order limits")
    strategy_symbols = tuple(allocation["strategy_symbols"])
    trade_date = str(signal_metadata.get("trade_date") or "").strip() or None
    snapshot_date = _normalize_date_like(signal_metadata.get("snapshot_as_of"))
    resolved_execution_mode = _resolve_execution_mode(
        dry_run_only=options.dry_run_only,
        execution_mode=options.execution_mode,
    )
    safe_haven_symbols = tuple(allocation["safe_haven_symbols"])
    safe_haven_symbol = safe_haven_symbols[0] if safe_haven_symbols else None
    normalized_account_ids = _normalize_account_ids(options.account_ids)
    order_account_id = _resolve_order_account_id(normalized_account_ids)

    execution_summary = _init_execution_summary(
        resolved_execution_mode=resolved_execution_mode,
        normalized_account_ids=normalized_account_ids,
        order_account_id=order_account_id,
        trade_date=trade_date,
        snapshot_date=snapshot_date,
        safe_haven_symbol=safe_haven_symbol,
        option_order_intents=option_order_intents,
        option_underliers=option_underliers,
        signal_metadata=signal_metadata,
        account_values=account_values,
        options=options,
    )

    equity = float(account_values.get("equity", 0) or 0.0)
    cash_only_deleverage_mode = bool(signal_metadata.get("cash_only_deleverage_mode"))
    if equity <= 0:
        gross_positions = sum(
            float(details.get("quantity") or 0.0) * float(details.get("avg_cost") or 0.0)
            for details in dict(positions or {}).values()
        )
        if options.cash_only_execution and gross_positions > 0.0:
            equity = gross_positions
            cash_only_deleverage_mode = True
        else:
            execution_summary["execution_status"] = "blocked"
            execution_summary["no_op_reason"] = "no_equity"
            return _RebalanceAllocationPlan(
                strategy_symbols=strategy_symbols,
                risk_symbols=tuple(allocation["risk_symbols"]),
                income_symbols=tuple(allocation["income_symbols"]),
                safe_haven_symbols=safe_haven_symbols,
                safe_haven_symbol=safe_haven_symbol,
                target_weights=target_weights,
                approved_target_caps=approved_target_caps,
                option_order_intents=option_order_intents,
                option_underliers=option_underliers,
                has_executable_option_plan=has_executable_option_plan,
                trade_date=trade_date,
                snapshot_date=snapshot_date,
                resolved_execution_mode=resolved_execution_mode,
                normalized_account_ids=normalized_account_ids,
                order_account_id=order_account_id,
                equity=equity,
                reserved=0.0,
                investable=0.0,
                threshold=0.0,
                order_quantity_step=float(options.quantity_step or 1.0),
                minimum_order_notional=max(0.0, float(options.min_order_notional or 0.0)),
                all_symbols=set(),
                account_new_risk_buy_blocked=False,
                account_new_risk_reason_codes=(),
                execution_summary=execution_summary,
                early_logs=[translator("no_equity")],
            )
    execution_summary["cash_only_deleverage_mode"] = cash_only_deleverage_mode

    target_weights, account_new_risk_buy_blocked, account_new_risk_reason_codes = _apply_account_new_risk_gate(
        target_weights,
        account_values=account_values,
        signal_metadata=signal_metadata,
        account_execution_session=account_execution_session,
        execution_summary=execution_summary,
        dry_run_only=options.dry_run_only,
    )

    reserved = max(
        float(equity) * float(options.cash_reserve_ratio or 0.0),
        max(0.0, float(options.cash_reserve_floor_usd or 0.0)),
    )
    investable = equity - reserved
    target_weights, substituted_safe_haven_symbols = _apply_safe_haven_cash_substitution_to_weights(
        target_weights,
        safe_haven_symbols=safe_haven_symbols,
        investable=investable,
        threshold_usd=options.safe_haven_cash_substitute_threshold_usd,
    )
    execution_summary["safe_haven_cash_substituted_symbols"] = list(substituted_safe_haven_symbols)
    if safe_haven_symbols:
        execution_summary["realized_safe_haven_weight"] = _sum_safe_haven_weight(
            target_weights,
            safe_haven_symbols,
        )
    threshold = equity * options.rebalance_threshold_ratio
    order_quantity_step = float(options.quantity_step or 1.0)
    minimum_order_notional = max(0.0, float(options.min_order_notional or 0.0))
    execution_summary["cash_reserve_dollars"] = float(reserved)

    all_symbols = set(target_weights.keys()) | set(positions.keys())
    if strategy_symbols:
        all_symbols = all_symbols & set(strategy_symbols)

    return _RebalanceAllocationPlan(
        strategy_symbols=strategy_symbols,
        risk_symbols=tuple(allocation["risk_symbols"]),
        income_symbols=tuple(allocation["income_symbols"]),
        safe_haven_symbols=safe_haven_symbols,
        safe_haven_symbol=safe_haven_symbol,
        target_weights=target_weights,
        approved_target_caps=approved_target_caps,
        option_order_intents=option_order_intents,
        option_underliers=option_underliers,
        has_executable_option_plan=has_executable_option_plan,
        trade_date=trade_date,
        snapshot_date=snapshot_date,
        resolved_execution_mode=resolved_execution_mode,
        normalized_account_ids=normalized_account_ids,
        order_account_id=order_account_id,
        equity=equity,
        reserved=reserved,
        investable=investable,
        threshold=threshold,
        order_quantity_step=order_quantity_step,
        minimum_order_notional=minimum_order_notional,
        all_symbols=all_symbols,
        account_new_risk_buy_blocked=account_new_risk_buy_blocked,
        account_new_risk_reason_codes=account_new_risk_reason_codes,
        execution_summary=execution_summary,
    )


def _collect_rebalance_quotes(
    ib,
    symbols: set[str],
    *,
    signal_metadata: dict[str, Any],
    fetch_quote_snapshots,
    dry_run_only: bool,
) -> _RebalanceQuoteCollection:
    """Fetch market quotes, serialize snapshots, and apply snapshot price fallbacks."""
    quote_snapshots_by_symbol: dict[str, dict[str, Any]] = {}

    def record_quote_snapshot(symbol, snapshot) -> None:
        payload = _serialize_quote_snapshot(snapshot, symbol=symbol)
        resolved_symbol = payload.get("symbol")
        if resolved_symbol:
            quote_snapshots_by_symbol[resolved_symbol] = payload

    snapshot_price_fallbacks = _normalize_price_fallbacks(signal_metadata)
    price_fallback_source = str(signal_metadata.get("price_fallback_source") or "").strip() or (
        "snapshot_close" if signal_metadata.get("dry_run_price_fallbacks") else "close"
    )
    prices = get_market_prices(
        ib,
        symbols,
        fetch_quote_snapshots=fetch_quote_snapshots,
        quote_recorder=record_quote_snapshot,
    )
    prices, fallback_symbols = _apply_snapshot_price_fallbacks(
        prices,
        symbols,
        dry_run_only=dry_run_only,
        snapshot_price_fallbacks=snapshot_price_fallbacks,
    )
    return _RebalanceQuoteCollection(
        prices=prices,
        quote_snapshots=list(quote_snapshots_by_symbol.values()),
        fallback_symbols=fallback_symbols,
        fallback_source=price_fallback_source,
    )


def _record_quote_collection_summary(
    execution_summary: dict[str, Any],
    quote_collection: _RebalanceQuoteCollection,
) -> None:
    fallback_symbols = quote_collection.fallback_symbols
    has_fallback = bool(fallback_symbols)
    execution_summary["snapshot_price_fallback_used"] = has_fallback
    execution_summary["snapshot_price_fallback_symbols"] = list(fallback_symbols)
    execution_summary["snapshot_price_fallback_count"] = len(fallback_symbols)
    execution_summary["price_fallback_used"] = has_fallback
    execution_summary["price_fallback_symbols"] = list(fallback_symbols)
    execution_summary["price_fallback_count"] = len(fallback_symbols)
    execution_summary["price_fallback_source"] = quote_collection.fallback_source if has_fallback else None
    execution_summary["quote_snapshot"] = {
        "quotes": quote_collection.quote_snapshots,
    }
    if has_fallback:
        execution_summary["price_source_mode"] = f"mixed_market_quote_{quote_collection.fallback_source}"


def _append_small_account_allocation_drift_notes(
    execution_summary: dict[str, Any],
    trade_logs: list[str],
    *,
    translator,
    context: _DriftNotesContext,
) -> None:
    if execution_summary.get("execution_status") == "blocked":
        return
    if execution_summary.get("small_account_allocation_drift_notes"):
        return
    submitted_orders = (
        tuple(execution_summary.get("orders_submitted") or ())
        + tuple(execution_summary.get("orders_pending") or ())
        + tuple(execution_summary.get("orders_filled") or ())
        + tuple(execution_summary.get("orders_partially_filled") or ())
    )
    notes = build_small_account_allocation_drift_notes(
        target_values=context.reference_target_mv,
        current_values=context.current_mv,
        current_quantities=context.current_quantities,
        prices=context.prices,
        submitted_orders=submitted_orders,
        total_value=float(context.equity or 0.0),
        cash_value=context.investable_buying_power,
    )
    if not notes:
        return
    execution_summary["small_account_allocation_drift_notes"] = list(notes)
    trade_logs.extend(format_small_account_allocation_drift_notes(notes, translator=translator))


def _prepare_post_quote_targets(
    *,
    positions,
    account_values,
    allocation_plan: _RebalanceAllocationPlan,
    prices: dict[str, float],
    translator,
) -> tuple[dict[str, float], dict[str, float], dict[str, float], list[str], str, _DriftNotesContext]:
    execution_summary = allocation_plan.execution_summary
    target_weights = allocation_plan.target_weights
    current_mv = {
        symbol: positions.get(symbol, {}).get("quantity", 0) * prices.get(symbol, 0)
        for symbol in allocation_plan.all_symbols
    }
    current_quantities = {
        symbol: float(positions.get(symbol, {}).get("quantity", 0.0) or 0.0)
        for symbol in allocation_plan.all_symbols
    }
    target_mv = {symbol: allocation_plan.investable * weight for symbol, weight in target_weights.items()}
    candidate_symbols = tuple(
        dict.fromkeys(
            str(symbol or "").strip().upper()
            for symbol in allocation_plan.risk_symbols + allocation_plan.income_symbols
            if str(symbol or "").strip()
        )
    )
    if not candidate_symbols:
        candidate_symbols = tuple(
            str(symbol or "").strip().upper()
            for symbol in target_mv
            if str(symbol or "").strip().upper() not in allocation_plan.safe_haven_symbols
        )
    small_account_reference_target_mv = {
        symbol: target_mv.get(symbol, 0.0)
        for symbol in candidate_symbols
        if symbol in target_mv
    }
    small_account_compatibility = apply_small_account_cash_compatibility(
        target_mv,
        prices,
        candidate_symbols=candidate_symbols,
        safe_haven_cash_symbols=allocation_plan.safe_haven_symbols,
        quantity_step=allocation_plan.order_quantity_step,
        cash_substitute_limit_usd=_facade_attr('SMALL_ACCOUNT_SAFE_HAVEN_CASH_SUBSTITUTE_LIMIT_USD', SMALL_ACCOUNT_SAFE_HAVEN_CASH_SUBSTITUTE_LIMIT_USD),
    )
    target_mv = dict(small_account_compatibility.targets)
    for symbol, value in target_mv.items():
        cap = allocation_plan.approved_target_caps.get(symbol)
        if cap is None or not math.isfinite(float(value)) or float(value) > cap + 0.01:
            raise RuntimeError("IBKR target exceeds RiskEngine order limit")
    small_account_substituted_symbols = small_account_compatibility.whole_share_substituted_symbols
    for symbol in small_account_substituted_symbols:
        target_weights[symbol] = 0.0
    small_account_safe_haven_cash_substituted_symbols = list(
        small_account_compatibility.safe_haven_cash_substituted_symbols
    )
    for symbol in small_account_safe_haven_cash_substituted_symbols:
        target_weights[symbol] = 0.0
    if allocation_plan.safe_haven_symbols:
        execution_summary["realized_safe_haven_weight"] = _sum_safe_haven_weight(
            target_weights,
            allocation_plan.safe_haven_symbols,
        )
    execution_summary["small_account_whole_share_substituted_symbols"] = list(
        small_account_substituted_symbols
    )
    execution_summary["small_account_safe_haven_cash_substituted_symbols"] = (
        small_account_safe_haven_cash_substituted_symbols
    )
    execution_summary["small_account_existing_whole_share_retained_symbols"] = []
    execution_summary["small_account_whole_share_bootstrap_symbols"] = []
    execution_summary["small_account_whole_share_cash_notes"] = list(
        small_account_compatibility.cash_substitution_notes
    )
    trade_logs = list(
        format_small_account_cash_substitution_notes(
            small_account_compatibility.cash_substitution_notes,
            translator=translator,
        )
    )
    target_hash = _build_target_hash(target_weights)
    execution_summary["target_vs_current"] = _build_target_diff_rows(
        target_weights,
        current_mv,
        allocation_plan.equity,
    )
    drift_context = _DriftNotesContext(
        reference_target_mv=small_account_reference_target_mv,
        current_mv=current_mv,
        current_quantities=current_quantities,
        prices=prices,
        equity=allocation_plan.equity,
        investable_buying_power=_investable_buying_power(
            float(account_values.get("buying_power", 0.0) or 0.0),
            allocation_plan.reserved,
        ),
    )
    return current_mv, current_quantities, target_mv, trade_logs, target_hash, drift_context


def _record_blocked_step(
    execution_summary: dict[str, Any],
    trade_logs: list[str],
    *,
    reason: str,
    translator,
    log_message: str | None = None,
) -> None:
    execution_summary["execution_status"] = "blocked"
    execution_summary["no_op_reason"] = reason
    execution_summary["skipped_reasons"].append(reason)
    trade_logs.append(log_message if log_message is not None else translator("failed", reason=reason))


def _append_execution_detail_logs(
    trade_logs: list[str],
    *,
    allocation_plan: _RebalanceAllocationPlan,
    quote_collection: _RebalanceQuoteCollection,
    current_mv: dict[str, float],
    signal_metadata: dict[str, Any],
    translator,
    dry_run_only: bool,
) -> None:
    trade_logs.append(
        " | ".join(
            [
                translator(
                    "execution_profile_detail",
                    profile=_display_text(allocation_plan.execution_summary["strategy_profile"], fallback="<unknown>"),
                ),
                translator(
                    "regime_detail",
                    value=_display_text(signal_metadata.get("regime"), fallback="<none>"),
                ),
                translator(
                    "breadth_detail",
                    value=f"{float(signal_metadata.get('breadth_ratio', 0.0) or 0.0):.1%}",
                ),
                translator(
                    "target_stock_detail",
                    value=f"{float(signal_metadata.get('target_stock_weight', 0.0) or 0.0):.1%}",
                ),
                translator(
                    "realized_stock_detail",
                    value=f"{float(signal_metadata.get('realized_stock_weight', 0.0) or 0.0):.1%}",
                ),
                translator(
                    "snapshot_as_of_detail",
                    value=_display_text(allocation_plan.snapshot_date, fallback="<none>"),
                ),
                translator(
                    "trade_date_detail",
                    value=_display_text(allocation_plan.trade_date, fallback="<none>"),
                ),
            ]
        )
    )
    if quote_collection.fallback_symbols:
        trade_logs.append(
            translator(
                "dry_run_snapshot_prices" if dry_run_only else "price_fallback_prices",
                count=len(quote_collection.fallback_symbols),
                symbols=_format_symbol_preview(quote_collection.fallback_symbols),
            )
        )
    trade_logs.extend(
        _format_target_lines(
            allocation_plan.target_weights,
            current_mv,
            allocation_plan.equity,
            translator=translator,
        )
    )


def _cash_sweep_sale_quantity_to_fund_buy(
    *,
    max_quantity: int,
    candidate_symbols: tuple[str, ...],
    safe_haven_symbol: str | None,
    cash_sweep_price: float,
    investable_anticipated_buying_power: float,
    target_mv: dict[str, float],
    current_mv: dict[str, float],
    threshold: float,
    prices: dict[str, float],
    options: RebalanceExecutionOptions,
) -> int:
    if max_quantity <= 0 or not safe_haven_symbol or cash_sweep_price <= 0.0:
        return 0
    base_buying_power = max(0.0, float(investable_anticipated_buying_power))
    funding_needs = []
    for symbol in candidate_symbols:
        underweight_value = target_mv[symbol] - current_mv.get(symbol, 0.0)
        if underweight_value <= threshold:
            continue
        ask = prices.get(symbol)
        if not ask or ask <= 0.0:
            continue
        funding_needs.append(
            (
                underweight_value,
                _limit_buy_price(
                    symbol,
                    ask,
                    options.limit_buy_premium,
                    options.limit_buy_premium_by_symbol,
                ),
            )
        )
    if should_sell_cash_sweep_to_fund_whole_share_buy(
        float(max_quantity),
        cash_sweep_price,
        base_buying_power,
        funding_needs,
    ):
        return int(max_quantity)
    return 0


def _plan_rebalance_directions(
    *,
    positions,
    allocation_plan: _RebalanceAllocationPlan,
    current_mv: dict[str, float],
    target_mv: dict[str, float],
    prices: dict[str, float],
    investable_anticipated_buying_power: float,
    pending_cash_risk_unknown: bool,
    settled_cash_ledger_unverified: bool,
    execution_summary: dict[str, Any],
    trade_logs: list[str],
    translator,
    options: RebalanceExecutionOptions,
    drift_context: _DriftNotesContext,
) -> tuple[int, bool]:
    missing_price_symbols: list[str] = []
    insufficient_buying_power_symbols: list[str] = []
    min_notional_symbols: list[str] = []
    quantity_zero_symbols: list[str] = []
    threshold = allocation_plan.threshold
    safe_haven_symbol = allocation_plan.safe_haven_symbol
    cash_sweep_quantity = 0
    cash_sweep_price = float(prices.get(safe_haven_symbol, 0.0) or 0.0) if safe_haven_symbol else 0.0

    has_sell_plan = False
    for symbol in allocation_plan.all_symbols:
        current = current_mv.get(symbol, 0.0)
        target = target_mv.get(symbol, 0.0)
        if current <= target + threshold:
            continue
        price = prices.get(symbol)
        if not price:
            missing_price_symbols.append(symbol)
            continue
        qty = _sell_order_quantity(
            current_value=current,
            target_value=target,
            price=price,
            position_quantity=positions.get(symbol, {}).get("quantity", 0),
            quantity_step=allocation_plan.order_quantity_step,
        )
        if qty > 0:
            has_sell_plan = True
            break
        quantity_zero_symbols.append(symbol)

    funding_buy_candidates = [
        symbol
        for symbol in target_mv
        if symbol != safe_haven_symbol
        and (target_mv[symbol] - current_mv.get(symbol, 0.0)) > threshold
        and abs(target_mv[symbol] - current_mv.get(symbol, 0.0)) > allocation_plan.minimum_order_notional
    ]
    if allocation_plan.account_new_risk_buy_blocked:
        funding_buy_candidates = []
    if (
        not has_sell_plan
        and funding_buy_candidates
        and safe_haven_symbol
        and cash_sweep_price > 0.0
        and float(positions.get(safe_haven_symbol, {}).get("quantity", 0.0) or 0.0) > 0.0
    ):
        cash_sweep_quantity = _cash_sweep_sale_quantity_to_fund_buy(
            max_quantity=int(float(positions.get(safe_haven_symbol, {}).get("quantity", 0.0) or 0.0)),
            candidate_symbols=tuple(funding_buy_candidates),
            safe_haven_symbol=safe_haven_symbol,
            cash_sweep_price=cash_sweep_price,
            investable_anticipated_buying_power=investable_anticipated_buying_power,
            target_mv=target_mv,
            current_mv=current_mv,
            threshold=threshold,
            prices=prices,
            options=options,
        )
        if cash_sweep_quantity > 0:
            has_sell_plan = True

    has_buy_plan = False
    for symbol, target in target_mv.items():
        current = current_mv.get(symbol, 0.0)
        if current >= target - threshold:
            continue
        price = prices.get(symbol)
        if not price:
            missing_price_symbols.append(symbol)
            continue
        buy_value = min(target - current, investable_anticipated_buying_power * 0.95)
        if buy_value <= 0:
            insufficient_buying_power_symbols.append(symbol)
            continue
        if buy_value < allocation_plan.minimum_order_notional:
            min_notional_symbols.append(symbol)
            continue
        limit_price = _limit_buy_price(
            symbol,
            price,
            options.limit_buy_premium,
            options.limit_buy_premium_by_symbol,
        )
        qty, _forced_whole_share = _facade_attr("_planned_buy_order_quantity", _planned_buy_order_quantity)(
            symbol,
            buy_value=buy_value,
            limit_price=limit_price,
            quantity_step=allocation_plan.order_quantity_step,
            investable_buying_power=investable_anticipated_buying_power,
            held_quantity=max(0.0, float(positions.get(symbol, {}).get("quantity", 0.0) or 0.0)),
        )
        if qty > 0:
            has_buy_plan = True
            break
        quantity_zero_symbols.append(symbol)

    if (
        has_buy_plan
        and not options.dry_run_only
        and "PERSISTENT_BREAKER_UNAVAILABLE" in allocation_plan.account_new_risk_reason_codes
    ):
        raise RuntimeError("IBKR persistent breaker session/contract unavailable; refusing new risk")

    if (
        (pending_cash_risk_unknown or settled_cash_ledger_unverified)
        and any(target_mv[s] > current_mv.get(s, 0.0) + threshold for s in target_mv)
        and not has_sell_plan
    ):
        # No qualified reduction will be submitted. Reject before taking the
        # daily execution lock, so a later complete observation can be checked
        # normally without clearing any execution or reconciliation marker.
        reason = "account_new_risk_gate"
        execution_summary["execution_status"] = "blocked"
        execution_summary["no_op_reason"] = reason
        execution_summary["account_new_risk_buy_blocked"] = True
        execution_summary["account_new_risk_block_reason"] = reason
        execution_summary["account_new_risk_reason_codes"] = list(allocation_plan.account_new_risk_reason_codes)
        execution_summary["skipped_reasons"].append(reason)
        trade_logs.append(
            translator(
                "buy_deferred",
                detail="[Account new-risk gate] " + ", ".join(allocation_plan.account_new_risk_reason_codes),
            )
        )
        return cash_sweep_quantity, True

    if not has_sell_plan and not has_buy_plan:
        reason = "target_diff_below_threshold"
        status = "no_op"
        if missing_price_symbols:
            symbols = ",".join(sorted(dict.fromkeys(missing_price_symbols)))
            reason = f"missing_price:{symbols}"
            status = "blocked"
            execution_summary["orders_skipped"].extend(
                {"symbol": symbol, "reason": "missing_price"}
                for symbol in sorted(dict.fromkeys(missing_price_symbols))
            )
        elif insufficient_buying_power_symbols:
            symbols = ",".join(sorted(dict.fromkeys(insufficient_buying_power_symbols)))
            reason = f"insufficient_buying_power:{symbols}"
            status = "blocked"
        elif min_notional_symbols:
            symbols = ",".join(sorted(dict.fromkeys(min_notional_symbols)))
            reason = f"min_notional:{symbols}"
        elif quantity_zero_symbols:
            symbols = ",".join(sorted(dict.fromkeys(quantity_zero_symbols)))
            reason = f"quantity_zero:{symbols}"

        if not (reason == "target_diff_below_threshold" and allocation_plan.has_executable_option_plan):
            _record_unsupported_option_intents(execution_summary, allocation_plan.option_order_intents)
            if execution_summary["option_orders_skipped"] and reason == "target_diff_below_threshold":
                reason = "option_orders_skipped"
        execution_summary["execution_status"] = status
        execution_summary["no_op_reason"] = reason
        if reason != "target_diff_below_threshold":
            execution_summary["skipped_reasons"].append(reason)
            if status == "blocked":
                trade_logs.append(translator("failed", reason=reason))
        if not (reason == "target_diff_below_threshold" and allocation_plan.has_executable_option_plan):
            _append_small_account_allocation_drift_notes(
                execution_summary,
                trade_logs,
                translator=translator,
                context=drift_context,
            )
            return cash_sweep_quantity, True

    return cash_sweep_quantity, False

