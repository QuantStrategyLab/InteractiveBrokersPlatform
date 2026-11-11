"""Prepare the two notification-only changes for verified legacy IBKR images."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

REBALANCE = "application/rebalance_service.py"
MAIN = "main.py"
ADAPTER = "notifications/unified_adapter.py"
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


def patch_etf_attention(source, reviewed):
    def functions(text):
        return {node.name: ast.get_source_segment(text, node) for node in ast.parse(text).body
                if isinstance(node, ast.FunctionDef)}

    before, after = functions(source), functions(reviewed)
    if ("daily_check" in before["_notification_evidence"]
            or "daily_check" not in after["_notification_evidence"]
            or "RUN_LINK_MISSING" not in after["_cycle_attention_notification"]
            or "RUN_LINK_MISSING" in before["_cycle_attention_notification"]):
        raise ValueError("etf_notification_source_changed")
    for name in ("_notification_evidence", "_cycle_attention_notification"):
        source = source.replace(before[name], after[name], 1)
    ast.parse(source)
    return source


def build_verified_image(base, *, etf_attention=False):
    """Build only the selected source changes over the exact serving image."""
    if not re.fullmatch(r"[a-z0-9-]+-docker.pkg.dev/[a-z0-9_./-]+@sha256:[a-f0-9]{64}", base):
        raise ValueError("legacy_serving_image_not_immutable")

    def command(*args, timeout=45):
        return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL, timeout=timeout).strip()

    def image_file(image, name):
        return command("docker", "run", "--rm", "--network=none", "--entrypoint=cat", image, "/app/" + name)

    repo = base.split("@")[0]
    registry = repo.split("/")[0]
    command("gcloud", "auth", "configure-docker", registry, "--quiet")
    command("docker", "pull", base, timeout=180)
    if etf_attention:
        sources = {ADAPTER: patch_etf_attention(image_file(base, ADAPTER), Path(ADAPTER).read_text())}
        tag_prefix = "etf-attention"
    else:
        sources = {name: transform(image_file(base, name) + "\n")
                   for name, transform in ((REBALANCE, patch_rebalance), (MAIN, patch_archive))}
        tag_prefix = "legacy-daily-routing"
    combined = "".join(sources.values()).encode()
    tag_input = base.encode() + combined if etf_attention else combined
    tag = f"{repo}:{tag_prefix}-" + hashlib.sha256(tag_input).hexdigest()[:12]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for name, source in sources.items():
            dst = root / "source" / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(source)
        (root / "Dockerfile").write_text(f"FROM {base}\nCOPY source/ /app/\n")
        command("docker", "build", "--network=none", "-t", tag, directory, timeout=180)
    old, candidate = json.loads(command("docker", "image", "inspect", base, tag))
    original_config = {k: v for k, v in old["Config"].items() if k != "Image"}
    candidate_config = {k: v for k, v in candidate["Config"].items() if k != "Image"}
    if not (original_config == candidate_config and old["Os"] == candidate["Os"]
            and old["Architecture"] == candidate["Architecture"]
            and candidate["RootFS"]["Layers"][:-1] == old["RootFS"]["Layers"]):
        raise ValueError("legacy_overlay_container_configuration_changed")
    for name, source in sources.items():
        if image_file(tag, name) != source.rstrip():
            raise ValueError("legacy_overlay_source_readback_mismatch")
    command("docker", "push", tag, timeout=180)
    digest = command("gcloud", "artifacts", "docker", "images", "describe", tag, "--format=value(image_summary.digest)")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise ValueError("legacy_overlay_digest_unconfirmed")
    head = command("git", "ls-remote", "origin", "refs/heads/main").split()
    if len(head) != 2 or head[0] != os.environ.get("GITHUB_SHA"):
        raise ValueError("reviewed_branch_changed")
    return repo + "@" + digest


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
