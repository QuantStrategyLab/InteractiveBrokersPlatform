"""Extract complete, explicitly identified broker facts for receipt linkage."""
from __future__ import annotations

import json
from collections.abc import Mapping

_KEYS = ("orders_filled", "orders_pending", "orders_partially_filled",
         "option_orders_filled", "option_orders_pending", "option_orders_partially_filled")


def extract_execution_evidence(execution_summary, *, dry_run):
    """Return order snapshots and individual execution fills, or unknown.

    Empty collections, previews, unknown submission outcomes, incomplete lists
    and missing or duplicate broker identities are not proof of no activity.
    A filled order snapshot is not itself an individual execution fill.
    """
    if (dry_run or not isinstance(execution_summary, Mapping)
            or execution_summary.get("outcome_unknown") or execution_summary.get("submission_halted")):
        return None, None
    for key in ("orders_skipped", "option_orders_skipped"):
        skipped = execution_summary.get(key)
        if not isinstance(skipped, (tuple, list)):
            return None, None
        if any(isinstance(item, Mapping) and str(item.get("status") or "").strip().lower()
               in {"rejected", "cancelled", "apicancelled"} for item in skipped):
            return None, None
    if not all(isinstance(execution_summary.get(key), (tuple, list)) for key in _KEYS):
        return None, None
    statuses = {
        "filled": {"filled"},
        "pending": {"pendingsubmit", "apipending", "apipendingsubmit", "submitted", "presubmitted"},
        "partially_filled": {"partiallyfilled", "partially_filled", "partial"},
    }
    for key in _KEYS:
        category = "partially_filled" if key.endswith("partially_filled") else key.rsplit("_", 1)[-1]
        if any(not isinstance(order, Mapping) or str(order.get("status") or "").strip().lower() not in statuses[category]
               for order in execution_summary[key]):
            return None, None
    orders = [item for key in _KEYS for item in execution_summary[key]]
    if not orders or any(not isinstance(order, Mapping) or not order.get("broker_order_id") for order in orders):
        return None, None
    identities = [str(order["broker_order_id"]) for order in orders]
    if len(identities) != len(set(identities)):
        return None, None
    try:
        orders = json.loads(json.dumps(orders, allow_nan=False))
    except (TypeError, ValueError):
        return None, None
    fills = []
    for order in orders:
        records = order.get("fills")
        if not isinstance(records, (tuple, list)):
            return orders, None
        status = str(order.get("status") or "").strip().lower()
        if status in {"filled", "partiallyfilled", "partially_filled", "partial"} and not records:
            return orders, None
        for record in records:
            if not isinstance(record, Mapping):
                return orders, None
            identity = next((record.get(key) for key in ("execution_id", "execId", "tradeId") if record.get(key)), None)
            if identity is None:
                return orders, None
            fills.append((str(identity), dict(record)))
    if not fills or len({key for key, _ in fills}) != len(fills):
        return orders, None
    try:
        return orders, json.loads(json.dumps([record for _, record in fills], allow_nan=False))
    except (TypeError, ValueError):
        return orders, None
