"""Opt-in routing for routine cycles whose persisted reports feed a daily digest.

This decides notification delivery only. It never changes an execution result,
calls a broker, grants order permissions, or treats deferral as delivery.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence


def daily_digest_enabled(mode: str | None = None) -> bool:
    return (mode if mode is not None else os.getenv("QSL_ROUTINE_NOTIFICATION_MODE", "immediate")) == "daily_digest"


def defer_routine_cycle_to_digest(
    execution: Mapping[str, object],
    *,
    submitted_order_count: int,
    mode: str | None = None,
    submitted_orders: Sequence[Mapping[str, object]] | None = None,
    include_orders: bool | None = None,
) -> bool:
    """Defer only healthy cycles with no submitted order; order events stay live.

    Default ``QSL_ROUTINE_NOTIFICATION_MODE=immediate`` preserves existing
    callers. An owner can opt a verified producer/recipient into ``daily_digest``
    after validating the existing private report-to-digest replacement path.
    """
    if submitted_order_count != 0:
        return False
    return healthy_cycle_evidence(execution, submitted_order_count=submitted_order_count,
                                  submitted_orders=submitted_orders, mode=mode)


def healthy_cycle_evidence(
    execution: Mapping[str, object], *, submitted_order_count: int,
    submitted_orders: Sequence[Mapping[str, object]] | None = None,
    mode: str | None = None,
) -> bool:
    """Validate archived cycle facts without deciding when to send order events."""
    if not daily_digest_enabled(mode) or type(submitted_order_count) is not int or submitted_order_count < 0:
        return False
    if not isinstance(execution, Mapping):
        return False
    if str(execution.get("execution_status") or "").lower() not in {"no_action", "no_op", "completed", "executed"}:
        return False
    reason_text = str(execution.get("no_op_reason") or "")
    reason, separator, detail = reason_text.partition(":")
    if separator and (reason not in {"min_notional", "quantity_zero"}
                      or not re.fullmatch(r"[A-Z0-9.,_-]{1,100}", detail)):
        return False
    if reason not in (
        "", "no_action", "no_signal", "no_rebalance", "below_threshold",
        "rebalance_diff_below_threshold", "target_diff_below_threshold", "threshold_not_met",
        "min_notional", "quantity_zero", "execution_already_recorded",
        "outside_execution_window", "outside_monthly_execution_window",
    ):
        return False
    normal_skip_reasons = {"min_notional", "quantity_zero", "target_diff_below_threshold",
                           "rebalance_diff_below_threshold", "below_threshold", "threshold_not_met"}
    if any(str(skip).partition(":")[0] not in normal_skip_reasons
           for skip in execution.get("skipped_reasons", ()) or ()):
        return False
    if any(execution.get(key) for key in (
        "error", "errors", "fail_reason", "execution_blocked_reason",
        "plugin_error", "persistence_error", "report_persistence_error", "notification_error",
        "pending_reconciliation", "reconciliation_required", "market_data_retry_required",
        "notification_attention_reason_codes", "orders_pending_count",
        "account_new_risk_buy_blocked", "small_account_buy_blocked", "risk_flags",
        "execution_blocked", "execution_blocking_skips", "strategy_run_persistence_error",
        "strategy_plugin_error", "strategy_plugin_alert_error", "auth_expired",
        "strategy_plugin_alert_failed_count", "strategy_plugin_alert_telegram_failed_count",
        "data_error", "blocked_reason", "failed_order_count",
    )):
        # Only an explicit healthy no-order fact is eligible. Even a failed
        # routine notification or unclassified no-op reason stays immediate.
        return False
    if str(execution.get("risk_gate") or "").upper() in {"BLOCK", "BLOCKED", "REJECT", "REJECTED", "DENY", "DENIED"}:
        return False
    if any(execution.get(key) for key in ("orders_pending", "option_orders_pending",
                                          "orders_partially_filled", "option_orders_partially_filled")):
        return False
    if submitted_order_count:
        if submitted_orders is None or len(submitted_orders) != submitted_order_count:
            return False
        source_orders = list(execution.get("orders_submitted") or ()) + list(execution.get("option_orders_submitted") or ())
        if any(not isinstance(order, Mapping) or str(order.get("status") or "").lower() in
               {"rejected", "cancelled", "canceled", "inactive", "error"} for order in source_orders):
            return False
        return all(isinstance(order, Mapping) and
                   str(order.get("status") or "").lower() in {"filled", "completed", "executed"}
                   for order in submitted_orders)
    if execution.get("broker_submission_done") or any(
        execution.get(key) for key in (
            "orders_submitted", "orders_pending", "orders_filled", "orders_partially_filled",
            "option_orders_submitted", "option_orders_pending", "option_orders_filled",
            "option_orders_partially_filled", "orders_submitted_count", "option_orders_submitted_count",
        )
    ):
        return False
    return True


def immediate_plugin_signals(signals):
    """Filter notification inputs only; callers retain original strategy signals."""
    if not daily_digest_enabled():
        return signals
    return tuple(signal for signal in signals if not (
        str(getattr(signal, "canonical_route", "") or "").lower() in {"watch", "opportunity_watch", "panic_reversal", "taco_rebound"}
        and str(getattr(signal, "suggested_action", "") or "").lower() in {"notify_manual_review", "watch", "observe"}
        and not any(getattr(signal, key, None) for key in ("error", "errors", "risk_halt", "auth_expired"))
    ))


def routine_notification(sender, runtime, report, text, *, routine_order=True):
    """Route only explicitly classified routine call sites, never message text."""
    orders_enabled = os.getenv("QSL_DIGEST_ROUTINE_ORDERS", "true") == "true"
    if daily_digest_enabled() and (not routine_order or orders_enabled):
        report.setdefault("notifications", []).append({
            "delivery_status": "deferred_digest", "transport_acknowledged": False,
            "sink": "telegram", "routine_order": routine_order,
        })
        return False
    return sender(runtime, report, text)
