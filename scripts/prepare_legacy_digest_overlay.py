"""Prepare the two notification-only changes for verified legacy IBKR images."""
from __future__ import annotations

import argparse
import ast
from pathlib import Path

REBALANCE = "application/rebalance_service.py"
MAIN = "main.py"
OLD_ROUTE = """    if defer_routine_cycle_to_digest(evidence, submitted_order_count=1 if has_orders else 0):
        return False
"""
ORDER_GUARD = '''    if has_orders and _verified_legacy_orders_for_digest(evidence):
        return False
'''
ORDER_HELPER = '''def _verified_legacy_orders_for_digest(evidence):
    import os

    if (os.getenv("QSL_ROUTINE_NOTIFICATION_MODE") != "daily_digest"
            or os.getenv("QSL_DIGEST_ROUTINE_ORDERS", "false") != "true"):
        return False
    if str(evidence.get("execution_status") or "").strip().lower() not in {"completed", "executed"}:
        return False
    if any(evidence.get(key) for key in (
        "orders_pending", "option_orders_pending", "orders_partially_filled", "option_orders_partially_filled",
        "orders_pending_count", "option_orders_pending_count", "pending_reconciliation", "reconciliation_required",
        "outcome_unknown", "submission_halted", "market_data_retry_required", "auth_expired", "auth_failure",
        "risk_flags", "strategy_plugin_error", "strategy_run_persistence_error", "failed_order_count",
    )):
        return False
    batches = [evidence.get(key) for key in (
        "orders_submitted", "option_orders_submitted", "orders_filled", "option_orders_filled",
    )]
    if any(batch is not None and not isinstance(batch, (list, tuple)) for batch in batches):
        return False
    orders = [order for batch in batches if batch for order in batch]
    return bool(orders) and all(
        isinstance(order, Mapping)
        and str(order.get("status") or "").strip().lower() == "filled"
        and any(str(order.get(key) or "").strip() for key in ("broker_order_id", "order_id", "orderId", "permId"))
        for order in orders
    )


'''
ORIGINAL_ARCHIVE = '''                print(f"execution_report {report_path}", flush=True)
            except Exception as persist_exc:
                print(f"failed to persist execution report: {persist_exc}", flush=True)
'''
NEW_ARCHIVE = '''                print(f"execution_report {report_path}", flush=True)
                if (os.getenv("QSL_ROUTINE_NOTIFICATION_MODE") == "daily_digest"
                        and dry_run_only_override is not True):
                    cloud_uri = (report.get("artifacts") or {}).get("runtime_report_cloud_uri")
                    if not isinstance(cloud_uri, str) or not cloud_uri.startswith("gs://"):
                        raise RuntimeError("execution_report_cloud_archive_unconfirmed")
            except Exception as persist_exc:
                print(f"failed to persist execution report: {persist_exc}", flush=True)
                if (os.getenv("QSL_ROUTINE_NOTIFICATION_MODE") == "daily_digest"
                        and dry_run_only_override is not True):
                    archive_error = RuntimeError("execution_report_cloud_archive_unconfirmed")
                    error_message = ("IBKR 日报数据归档未确认。"
                                     if os.getenv("NOTIFY_LANG", "zh").lower().startswith("zh")
                                     else "IBKR daily report archive not confirmed.")
                    try:
                        _publish_runtime_failure_notification(
                            detailed_text=error_message, compact_text=error_message, exc=archive_error,
                        )
                    except Exception:
                        print(t("runtime_notification_delivery_failed"), flush=True)
'''


def patch_rebalance(source):
    route, = [item for item in ast.parse(source).body if isinstance(item, ast.FunctionDef)
              and item.name == "_should_publish_execution_notification"]
    original = ast.get_source_segment(source, route)
    if original.count(OLD_ROUTE) != 1 or "_verified_legacy_orders_for_digest" in source:
        raise ValueError("legacy_notification_route_source_changed")
    updated = source.replace(original, ORDER_HELPER + original.replace(OLD_ROUTE, OLD_ROUTE + ORDER_GUARD), 1)
    ast.parse(updated)
    return updated


def patch_archive(source):
    if source.count(ORIGINAL_ARCHIVE) != 1 or "execution_report_cloud_archive_unconfirmed" in source:
        raise ValueError("legacy_archive_source_changed")
    updated = source.replace(ORIGINAL_ARCHIVE, NEW_ARCHIVE, 1)
    ast.parse(updated)
    return updated


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("serving_source", type=Path)
    parser.add_argument("overlay", type=Path)
    args = parser.parse_args()
    files = {path: transform((args.serving_source / path).read_text())
             for path, transform in ((REBALANCE, patch_rebalance), (MAIN, patch_archive))}
    for path, text in files.items():
        dest = args.overlay / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("x") as file:
            file.write(text)


if __name__ == "__main__":
    main()
