"""Application orchestration for InteractiveBrokersPlatform."""

from __future__ import annotations

import json

from application.reconciliation_service import (
    build_reconciliation_record,
    write_reconciliation_record,
)


def _format_text(value, *, fallback: str) -> str:
    text = str(value).strip() if value is not None else ""
    return text or fallback


def _format_symbol_preview(symbols, *, limit: int = 3) -> str:
    normalized = [str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()]
    if not normalized:
        return ""
    shown = normalized[:limit]
    remaining = len(normalized) - len(shown)
    if remaining > 0:
        shown.append(f"+{remaining}")
    return ",".join(shown)


def _summarize_target_changes(target_vs_current, *, limit: int = 5) -> str | None:
    rows = []
    for row in target_vs_current or ():
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        delta = float(row.get("delta_weight") or 0.0)
        if abs(delta) < 0.001:
            continue
        rows.append((abs(delta), symbol, delta))
    if not rows:
        return None
    rows.sort(key=lambda item: (-item[0], item[1]))
    preview = [f"{symbol} {delta:+.1%}" for _abs_delta, symbol, delta in rows[:limit]]
    remaining = len(rows) - len(preview)
    if remaining > 0:
        preview.append(f"+{remaining}")
    return ", ".join(preview)


def _summarize_orders(orders, *, limit: int = 3) -> str:
    preview = []
    for order in orders[:limit]:
        symbol = str(order.get("symbol") or "").strip().upper()
        quantity = int(order.get("quantity") or 0)
        if symbol and quantity > 0:
            preview.append(f"{symbol} {quantity}")
        elif symbol:
            preview.append(symbol)
    remaining = len(orders) - len(preview)
    if remaining > 0:
        preview.append(f"+{remaining}")
    return ", ".join(preview)


def _build_order_batch_lines(execution_summary, *, translator) -> list[str]:
    mode = str(execution_summary.get("mode") or "").strip().lower()
    order_groups = [
        ("orders_submitted", "dry_run" if mode == "dry_run" else "submitted"),
        ("orders_filled", "filled"),
        ("orders_partially_filled", "partial"),
    ]
    lines: list[str] = []
    for field_name, prefix in order_groups:
        orders = list(execution_summary.get(field_name) or [])
        if not orders:
            continue
        buy_orders = [order for order in orders if str(order.get("side") or "").strip().lower() == "buy"]
        sell_orders = [order for order in orders if str(order.get("side") or "").strip().lower() == "sell"]
        if buy_orders:
            lines.append(
                translator(
                    f"{prefix}_buy_batch",
                    count=len(buy_orders),
                    details=_summarize_orders(buy_orders),
                )
            )
        if sell_orders:
            lines.append(
                translator(
                    f"{prefix}_sell_batch",
                    count=len(sell_orders),
                    details=_summarize_orders(sell_orders),
                )
            )
    return lines


def _build_notification_trade_lines(
    trade_logs,
    *,
    execution_summary,
    translator,
) -> list[str]:
    lines: list[str] = []
    execution_summary = dict(execution_summary or {})

    no_op_reason = str(execution_summary.get("no_op_reason") or "").strip()
    if no_op_reason.startswith("same_day_execution_locked:"):
        lines.append(
            translator(
                "same_day_execution_locked_notice",
                mode=_format_text(execution_summary.get("mode"), fallback="<none>"),
                trade_date=_format_text(execution_summary.get("trade_date"), fallback="<none>"),
                snapshot_date=_format_text(execution_summary.get("snapshot_as_of"), fallback="<none>"),
            )
        )

    fallback_symbols = tuple(execution_summary.get("snapshot_price_fallback_symbols") or ())
    if execution_summary.get("snapshot_price_fallback_used") and fallback_symbols:
        lines.append(
            translator(
                "dry_run_snapshot_prices",
                count=len(fallback_symbols),
                symbols=_format_symbol_preview(fallback_symbols),
            )
        )

    target_change_summary = _summarize_target_changes(execution_summary.get("target_vs_current"))
    if target_change_summary:
        lines.append(translator("target_diff_summary", details=target_change_summary))

    lines.extend(_build_order_batch_lines(execution_summary, translator=translator))

    for raw_line in trade_logs or ():
        text = str(raw_line).strip()
        if not text:
            continue
        if text.startswith(("目标差异 ", "target_diff ", "DRY_RUN buy ", "DRY_RUN sell ")):
            continue
        if text.startswith(("🧪 dry-run估价:", "🧪 dry-run pricing:")):
            continue
        if "execution_lock_acquired" in text or "已获取执行锁" in text:
            continue
        if text.startswith(("profile=", "strategy_profile=", "策略=")):
            continue
        if "same_day_execution_locked" in text or "当日执行锁已存在" in text:
            continue
        if text not in lines:
            lines.append(text)

    return lines


def _resolve_weight_allocation(signal_metadata, *, required: bool) -> dict:
    metadata = dict(signal_metadata or {})
    allocation = dict(metadata.get("allocation") or {})
    if not allocation:
        if required:
            raise ValueError("IBKR execution requires signal_metadata.allocation")
        return {}
    if allocation.get("target_mode") != "weight":
        raise ValueError("IBKR execution requires allocation.target_mode=weight")
    targets = {
        str(symbol).strip().upper(): float(weight)
        for symbol, weight in dict(allocation.get("targets") or {}).items()
    }
    return {
        "strategy_symbols": tuple(str(symbol) for symbol in allocation.get("strategy_symbols", ())),
        "risk_symbols": tuple(str(symbol) for symbol in allocation.get("risk_symbols", ())),
        "income_symbols": tuple(str(symbol) for symbol in allocation.get("income_symbols", ())),
        "safe_haven_symbols": tuple(str(symbol) for symbol in allocation.get("safe_haven_symbols", ())),
        "targets": targets,
    }


def build_dashboard(
    positions,
    account_values,
    signal_desc,
    status_desc,
    *,
    strategy_profile=None,
    strategy_display_name=None,
    target_weights=None,
    signal_metadata=None,
    translator,
    separator,
    status_icon="🐤",
):
    equity = account_values.get("equity", 0)
    buying_power = account_values.get("buying_power", 0)
    position_lines = []
    for symbol in sorted(positions.keys()):
        qty = positions[symbol]["quantity"]
        avg = positions[symbol]["avg_cost"]
        market_value = qty * avg
        position_lines.append(f"  {symbol}: {qty}股 ${market_value:,.2f}")
    position_text = "\n".join(position_lines) if position_lines else translator("empty_positions")
    signal_metadata = signal_metadata or {}
    allocation = _resolve_weight_allocation(signal_metadata, required=False)
    target_lines = []
    for symbol, weight in sorted(allocation.get("targets", {}).items(), key=lambda item: (-item[1], item[0])):
        target_lines.append(f"  {symbol}: {weight:.1%}")
    target_text = "\n".join(target_lines) if target_lines else translator("empty_target_weights")
    regime = signal_metadata.get("regime")
    breadth_ratio = signal_metadata.get("breadth_ratio")
    target_stock_weight = signal_metadata.get("target_stock_weight")
    realized_stock_weight = signal_metadata.get("realized_stock_weight")
    safe_haven_weight = signal_metadata.get("safe_haven_weight")
    snapshot_as_of = signal_metadata.get("snapshot_as_of")
    strategy_name = _format_text(
        strategy_display_name,
        fallback=_format_text(strategy_profile, fallback="<unknown>"),
    )
    diagnostics = [
        translator("strategy_label", name=strategy_name),
        translator("regime_detail", value=_format_text(regime, fallback="<none>")) if regime is not None else None,
        translator("breadth_detail", value=f"{breadth_ratio:.1%}") if isinstance(breadth_ratio, (int, float)) else None,
        translator("target_stock_detail", value=f"{target_stock_weight:.1%}")
        if isinstance(target_stock_weight, (int, float))
        else None,
        translator("realized_stock_detail", value=f"{realized_stock_weight:.1%}")
        if isinstance(realized_stock_weight, (int, float))
        and isinstance(target_stock_weight, (int, float))
        and abs(float(realized_stock_weight) - float(target_stock_weight)) >= 0.01
        else None,
        translator("safe_haven_target_detail", value=f"{safe_haven_weight:.1%}")
        if isinstance(safe_haven_weight, (int, float))
        else None,
        translator("snapshot_as_of_detail", value=_format_text(snapshot_as_of, fallback="<none>")) if snapshot_as_of else None,
    ]
    diagnostics_text = " | ".join(part for part in diagnostics if part)
    return (
        f"{translator('equity')}: ${equity:,.2f} | {translator('buying_power')}: ${buying_power:,.2f}\n"
        f"{separator}\n"
        f"{position_text}\n"
        f"{separator}\n"
        f"{diagnostics_text}\n"
        f"{separator}\n"
        f"{status_icon} {status_desc}\n"
        f"🎯 {signal_desc}\n"
        f"{separator}\n"
        f"{translator('target_weights_title')}:\n{target_text}"
    )


def run_strategy_core(
    *,
    connect_ib,
    get_current_portfolio,
    compute_signals,
    execute_rebalance,
    send_tg_message,
    translator,
    separator,
    strategy_display_name=None,
    reconciliation_output_path=None,
    result_hook=None,
):
    ib = None
    try:
        ib = connect_ib()
        positions, account_values = get_current_portfolio(ib)
        current_holdings = set(positions.keys())
        signal_result = compute_signals(ib, current_holdings)
        if len(signal_result) == 5:
            target_weights, signal_desc, _is_emergency, status_desc, signal_metadata = signal_result
        else:
            target_weights, signal_desc, _is_emergency, status_desc = signal_result
            signal_metadata = {}
        allocation = _resolve_weight_allocation(signal_metadata, required=target_weights is not None)
        resolved_target_weights = dict(allocation.get("targets") or {}) if target_weights is not None else None

        dashboard = build_dashboard(
            positions,
            account_values,
            signal_desc,
            status_desc,
            strategy_profile=signal_metadata.get("strategy_profile"),
            strategy_display_name=strategy_display_name,
            target_weights=resolved_target_weights,
            signal_metadata=signal_metadata,
            translator=translator,
            separator=separator,
            status_icon=signal_metadata.get("status_icon", "🐤"),
        )

        if target_weights is None:
            decision = signal_metadata.get("snapshot_guard_decision")
            no_op_reason = signal_metadata.get("no_op_reason")
            fail_reason = signal_metadata.get("fail_reason")
            no_op_text = translator("no_trades")
            if decision:
                no_op_text = f"{no_op_text} | decision={decision}"
            if no_op_reason:
                no_op_text = f"{no_op_text} | reason={no_op_reason}"
            if fail_reason:
                no_op_text = f"{no_op_text} | fail_reason={fail_reason}"
            record = build_reconciliation_record(
                strategy_profile=signal_metadata.get("strategy_profile"),
                mode="dry_run" if signal_metadata.get("dry_run_only") else "paper",
                trade_date=signal_metadata.get("trade_date"),
                snapshot_as_of=signal_metadata.get("snapshot_as_of"),
                signal_metadata=signal_metadata,
                target_weights=None,
                execution_summary=None,
                no_op_reason=no_op_reason or fail_reason or decision,
            )
            record_path = write_reconciliation_record(record, output_path=reconciliation_output_path)
            print(
                "reconciliation_record "
                + json.dumps({"path": str(record_path), "status": record.get("execution_status"), "no_op_reason": record.get("no_op_reason")}, ensure_ascii=False),
                flush=True,
            )
            message = f"{translator('heartbeat_title')}\n{dashboard}\n{separator}\n{no_op_text}"
            send_tg_message(message)
            print(message, flush=True)
            if callable(result_hook):
                result_hook(
                    {
                        "result": "OK - heartbeat",
                        "signal_metadata": dict(signal_metadata or {}),
                        "target_weights": None,
                        "execution_summary": None,
                        "reconciliation_record": dict(record),
                        "reconciliation_record_path": str(record_path),
                    }
                )
            return "OK - heartbeat"

        execution_result = execute_rebalance(
            ib,
            resolved_target_weights,
            positions,
            account_values,
            strategy_symbols=allocation.get("strategy_symbols"),
            signal_metadata=signal_metadata,
        )
        if isinstance(execution_result, tuple) and len(execution_result) == 2:
            trade_logs, execution_summary = execution_result
        else:
            trade_logs = execution_result
            execution_summary = None
        record = build_reconciliation_record(
            strategy_profile=signal_metadata.get("strategy_profile"),
            mode="dry_run" if execution_summary and execution_summary.get("mode") == "dry_run" else "paper",
            trade_date=signal_metadata.get("trade_date"),
            snapshot_as_of=signal_metadata.get("snapshot_as_of"),
            signal_metadata=signal_metadata,
            target_weights=resolved_target_weights,
            execution_summary=execution_summary,
        )
        record_path = write_reconciliation_record(record, output_path=reconciliation_output_path)
        print(
            "reconciliation_record "
            + json.dumps(
                {
                    "path": str(record_path),
                    "status": record.get("execution_status"),
                    "orders_submitted": len(record.get("orders_submitted") or ()),
                    "orders_filled": len(record.get("orders_filled") or ()),
                    "orders_skipped": len(record.get("orders_skipped") or ()),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if trade_logs:
            notification_trade_lines = _build_notification_trade_lines(
                trade_logs,
                execution_summary=execution_summary,
                translator=translator,
            )
            trade_lines = "\n".join(notification_trade_lines)
            message = (
                f"{translator('rebalance_title')}\n"
                f"{dashboard}\n"
                f"{separator}\n"
                f"{trade_lines}"
            )
        else:
            message = f"{translator('heartbeat_title')}\n{dashboard}\n{separator}\n{translator('no_trades')}"

        send_tg_message(message)
        print(message, flush=True)
        if callable(result_hook):
            result_hook(
                {
                    "result": "OK - executed",
                    "signal_metadata": dict(signal_metadata or {}),
                    "target_weights": dict(resolved_target_weights or {}),
                    "execution_summary": dict(execution_summary or {}),
                    "reconciliation_record": dict(record),
                    "reconciliation_record_path": str(record_path),
                }
            )
        return "OK - executed"
    finally:
        if ib is not None and ib.isConnected():
            ib.disconnect()
