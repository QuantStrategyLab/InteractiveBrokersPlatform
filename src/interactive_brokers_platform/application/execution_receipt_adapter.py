"""Bounded execution-receipt facts derived from IBKR reconciliation results."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
import math

from quant_platform_kit.common.execution_receipts import (
    attach_runtime_execution_receipt,
    resolve_execution_receipt_fact,
)


_SUBMITTED_KEYS = ("orders_submitted", "option_orders_submitted")
_PENDING_KEYS = ("orders_pending", "option_orders_pending")
_PARTIAL_FILL_KEYS = ("orders_partially_filled", "option_orders_partially_filled")
_FILLED_KEYS = ("orders_filled", "option_orders_filled")
_FAILURE_STATUSES = frozenset({"error", "failed", "failure"})
# Only promote these explicit cycle reasons. Broad no-ops stay ``no_action``
# so existing digests and projections keep digest-compatible outcomes.
_NO_SIGNAL_REASON_HEADS = frozenset({"no_signal"})
_NO_REBALANCE_REASON_HEADS = frozenset({"no_rebalance", "target_diff_below_threshold"})


def attach_cycle_execution_receipt(
    report: dict[str, Any],
    execution_summary: Mapping[str, Any],
    reconciliation_record: Mapping[str, Any],
    *,
    execution_failed: bool,
) -> dict[str, Any]:
    """Attach the strongest fact supplied by IBKR's own reconciliation data.

    Submission arrays are intentionally only submission evidence.  A fill is
    emitted solely when the reconciliation result contains its explicit filled
    array; no status label or local marker is promoted to a fill.
    """

    runtime_loaded = report.get("runtime_release_receipt")
    if (
        isinstance(runtime_loaded, Mapping)
        and runtime_loaded.get("attestation_state") == "legacy_unattested"
        and runtime_loaded.get("strategy_release") is None
    ):
        # Legacy targets remain evidence-missing; optional reporting must not
        # turn an already-completed cycle into an HTTP failure and retry.
        return report

    summary = _combined_summary(execution_summary, reconciliation_record)
    status = str(summary.get("execution_status") or "").strip().lower()
    filled = _confirmed_fills(summary, _FILLED_KEYS)
    partial = _confirmed_fills(summary, _PARTIAL_FILL_KEYS, partial=True)
    uncertain_fills = ((_has_any(summary, _FILLED_KEYS) and not filled)
                       or (_has_any(summary, _PARTIAL_FILL_KEYS) and not partial))
    reconciliation_required = (status == "pending_reconciliation" or _has_any(summary, _PENDING_KEYS)
                               or uncertain_fills)
    outcome, confirmation = resolve_execution_receipt_fact(
        dry_run=bool(report.get("dry_run")),
        submission_attempted=_has_any(summary, _SUBMITTED_KEYS),
        partially_filled=partial,
        filled=filled,
        reconciliation_required=reconciliation_required,
        risk_blocked=status == "blocked" and not execution_failed,
        failed=execution_failed or status in _FAILURE_STATUSES,
    )
    if outcome == "no_action" and not bool(report.get("dry_run")):
        explicit = _explicit_non_action_outcome(summary)
        if explicit is not None:
            outcome = explicit
            confirmation = "not_applicable"
    return attach_runtime_execution_receipt(
        report,
        outcome=outcome,
        broker_confirmation=confirmation,
    )


def attach_terminal_fallback_execution_receipt(report: dict[str, Any]) -> dict[str, Any]:
    """Attach a no-action or uncertainty fact for a report that exited early."""

    failed = str(report.get("status") or "").strip().lower() == "error"
    outcome, confirmation = resolve_execution_receipt_fact(
        dry_run=bool(report.get("dry_run")),
        submission_attempted=failed,
        failed=failed,
    )
    return attach_runtime_execution_receipt(
        report,
        outcome=outcome,
        broker_confirmation=confirmation,
    )


def _combined_summary(
    execution_summary: Mapping[str, Any],
    reconciliation_record: Mapping[str, Any],
) -> dict[str, Any]:
    return {**dict(reconciliation_record or {}), **dict(execution_summary or {})}


def _explicit_non_action_outcome(summary: Mapping[str, Any]) -> str | None:
    """Map only explicit cycle reasons to no_signal / no_rebalance."""

    reason = str(summary.get("no_op_reason") or "").strip().lower()
    if not reason:
        return None
    head = reason.split(":", 1)[0].strip()
    if head in _NO_SIGNAL_REASON_HEADS:
        return "no_signal"
    if head in _NO_REBALANCE_REASON_HEADS:
        return "no_rebalance"
    return None


def _has_any(summary: Mapping[str, Any], keys: tuple[str, ...]) -> bool:
    return any(bool(tuple(summary.get(key) or ())) for key in keys)


def _confirmed_fills(summary, keys, *, partial=False):
    rows = [row for key in keys for row in summary.get(key, ()) or ()]
    if not rows:
        return False
    try:
        for row in rows:
            qty, filled, price = (float(row[key]) for key in ('quantity', 'filled_quantity', 'average_fill_price'))
            if (not row.get('broker_order_id') or not all(math.isfinite(x) and x > 0 for x in (qty, filled, price))
                    or filled > qty):
                return False
            if partial:
                if (row.get('broker_confirmed_terminal') is not True or row.get('status') != 'Cancelled'
                        or not filled < qty):
                    return False
            elif row.get('status') != 'Filled' or filled != qty:
                return False
    except (KeyError, TypeError, ValueError):
        return False
    return True
