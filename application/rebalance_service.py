"""Application orchestration for InteractiveBrokersPlatform."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json
import re

from application.cycle_result import StrategyCycleResult
from application.runtime_dependencies import IBKRRebalanceConfig, IBKRRebalanceRuntime
from application.reconciliation_service import (
    build_reconciliation_record,
    write_reconciliation_record,
)
from notifications.events import NotificationPublisher
from notifications import renderers as notification_renderers
from quant_platform_kit.common.models import PortfolioSnapshot, Position
from quant_platform_kit.common.notification_localization import (
    localize_notification_text as _base_localize_notification_text,
    translator_uses_zh as _base_translator_uses_zh,
)
from quant_platform_kit.common.port_adapters import CallableNotificationPort, CallablePortfolioPort


_EXTRA_ZH_REASON_REPLACEMENTS = (
    ("pending_orders_detected", "检测到未完成订单"),
    ("same_day_execution_locked", "当日执行锁已存在"),
    ("same_day_fills_detected", "检测到当日成交"),
    ("fail_reason=", "失败原因="),
    ("decision=", "决策="),
)
_DETAIL_FIELD_SPLIT_RE = re.compile(r"\s+(?=[^\s=:：]+[=:：])")


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


def _translator_uses_zh(translator) -> bool:
    return _base_translator_uses_zh(translator)


def _localize_notification_text(text: str, *, translator) -> str:
    return _base_localize_notification_text(
        text,
        translator=translator,
        extra_replacements=_EXTRA_ZH_REASON_REPLACEMENTS,
    )


def _render_notification_context_text(
    notification_context: Mapping[str, object] | None,
    *,
    translator,
    fallback: str = "",
) -> str:
    if not isinstance(notification_context, Mapping):
        return fallback
    key = str(notification_context.get("code") or "").strip()
    if not key:
        return fallback
    params = dict(notification_context.get("params") or {})
    rendered = translator(key, **params)
    return fallback if rendered == key else str(rendered)


def _translate_snapshot_guard_decision(decision: object, *, translator) -> str:
    value = str(decision or "").strip()
    if not value:
        return ""
    key = f"snapshot_guard_decision_{value}"
    translated = translator(key)
    return value if translated == key else str(translated)


def _split_detail_segment(text: str) -> list[str]:
    value = str(text or "").strip()
    if not value:
        return []
    if "=" not in value and ":" not in value and "：" not in value:
        return [value]
    return [part.strip() for part in _DETAIL_FIELD_SPLIT_RE.split(value) if part.strip()]


def _split_labeled_text(text: str) -> list[str]:
    segments = [segment.strip() for segment in str(text or "").split(" | ") if segment.strip()]
    if not segments:
        return []
    lines = [segments[0]]
    for segment in segments[1:]:
        lines.extend(_split_detail_segment(segment))
    return lines


def _format_prefixed_text(prefix: str, text: str) -> list[str]:
    parts = _split_labeled_text(text)
    if not parts:
        return []
    lines = [f"{prefix} {parts[0]}".strip()]
    lines.extend(f"  - {part}" for part in parts[1:])
    return lines


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
        text = _localize_notification_text(str(raw_line).strip(), translator=translator)
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
            lines.extend(_split_labeled_text(text))

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


def _format_dashboard_text(text) -> str:
    lines = [line.rstrip() for line in str(text or "").strip().splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _build_timing_audit_lines(signal_metadata, *, translator) -> list[str]:
    metadata = signal_metadata if isinstance(signal_metadata, Mapping) else {}
    raw_annotations = metadata.get("execution_annotations")
    annotations = raw_annotations if isinstance(raw_annotations, Mapping) else {}
    signal_date = str(annotations.get("signal_date") or metadata.get("signal_date") or "").strip()
    effective_date = str(annotations.get("effective_date") or metadata.get("effective_date") or "").strip()
    contract = str(
        annotations.get("execution_timing_contract")
        or metadata.get("execution_timing_contract")
        or ""
    ).strip()
    if not signal_date and not effective_date and not contract:
        return []
    label = "⏱ 执行时点" if _translator_uses_zh(translator) else "⏱ Timing"
    if signal_date and effective_date:
        value = f"{signal_date} -> {effective_date}"
    else:
        value = signal_date or effective_date or contract
    if contract and contract not in value:
        value = f"{value} ({contract})" if value else contract
    return [f"{label}: {value}"]


def _strategy_dashboard_text(signal_metadata, *, translator) -> str:
    metadata = signal_metadata if isinstance(signal_metadata, Mapping) else {}
    raw_annotations = metadata.get("execution_annotations")
    annotations = raw_annotations if isinstance(raw_annotations, Mapping) else {}
    dashboard_text = _format_dashboard_text(
        annotations.get("dashboard_text")
        or metadata.get("dashboard_text")
        or metadata.get("dashboard")
        or ""
    )
    timing_lines = _build_timing_audit_lines(metadata, translator=translator)
    if not timing_lines:
        return dashboard_text
    if not dashboard_text:
        return "\n".join(timing_lines)
    return f"{dashboard_text}\n" + "\n".join(timing_lines)


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
    signal_metadata = signal_metadata or {}
    strategy_dashboard = _strategy_dashboard_text(signal_metadata, translator=translator)
    if strategy_dashboard:
        return strategy_dashboard
    equity = account_values.get("equity", 0)
    buying_power = account_values.get("buying_power", 0)
    position_lines = []
    for symbol in sorted(positions.keys()):
        qty = positions[symbol]["quantity"]
        avg = positions[symbol]["avg_cost"]
        market_value = qty * avg
        position_lines.append(f"  - {symbol}: {qty}股 | ${market_value:,.2f}")
    position_text = "\n".join(position_lines) if position_lines else translator("empty_positions")
    allocation = _resolve_weight_allocation(signal_metadata, required=False)
    target_lines = []
    for symbol, weight in sorted(allocation.get("targets", {}).items(), key=lambda item: (-item[1], item[0])):
        target_lines.append(f"  - {symbol}: {weight:.1%}")
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
    diagnostics_lines = [f"  - {part}" for part in diagnostics if part]
    diagnostics_text = "\n".join(diagnostics_lines)
    localized_status_desc = _localize_notification_text(status_desc, translator=translator)
    localized_signal_desc = _localize_notification_text(signal_desc, translator=translator)
    status_lines = _format_prefixed_text(status_icon, localized_status_desc)
    signal_lines = _format_prefixed_text("🎯", localized_signal_desc)
    status_text = "\n".join(status_lines)
    signal_text = "\n".join(signal_lines)
    return (
        f"{translator('account_summary_title')}\n"
        f"  - {translator('equity')}: ${equity:,.2f}\n"
        f"  - {translator('buying_power')}: ${buying_power:,.2f}\n"
        f"{separator}\n"
        f"{translator('positions_title')}\n"
        f"{position_text}\n"
        f"{separator}\n"
        f"{translator('execution_summary_title')}\n"
        f"{diagnostics_text}\n"
        f"{separator}\n"
        f"{status_text}\n"
        f"{signal_text}\n"
        f"{separator}\n"
        f"{translator('target_weights_title')}:\n{target_text}"
    )


def _first_prefixed_line(prefix: str, text: str, *, translator) -> str | None:
    localized = _localize_notification_text(text, translator=translator)
    lines = _format_prefixed_text(prefix, localized)
    return lines[0] if lines else None


def _build_compact_message(
    *,
    title: str,
    strategy_display_name: str | None,
    signal_desc: str,
    status_desc: str,
    status_icon: str,
    translator,
    separator: str,
    body_lines,
    dashboard_text: str = "",
) -> str:
    lines = [title]
    strategy_name = _format_text(strategy_display_name, fallback="<unknown>")
    lines.append(translator("strategy_label", name=strategy_name))
    dashboard = _format_dashboard_text(dashboard_text)
    if dashboard:
        lines.append(separator)
        lines.extend(dashboard.splitlines())
    status_line = _first_prefixed_line(status_icon, status_desc, translator=translator)
    if status_line:
        lines.append(status_line)
    signal_line = _first_prefixed_line("🎯", signal_desc, translator=translator)
    if signal_line:
        lines.append(signal_line)
    compact_body = [str(line).strip() for line in body_lines or () if str(line).strip()]
    if compact_body:
        lines.append(separator)
        lines.extend(compact_body)
    return "\n".join(lines)


def _legacy_portfolio_snapshot(ib, *, get_current_portfolio) -> PortfolioSnapshot:
    positions, account_values = get_current_portfolio(ib)
    snapshot_positions = tuple(
        Position(
            symbol=str(symbol).strip().upper(),
            quantity=float(details.get("quantity") or 0),
            market_value=float(details.get("quantity") or 0) * float(details.get("avg_cost") or 0.0),
            average_cost=float(details.get("avg_cost") or 0.0),
        )
        for symbol, details in dict(positions or {}).items()
    )
    return PortfolioSnapshot(
        as_of=datetime.now(timezone.utc),
        total_equity=float(account_values.get("equity") or 0.0),
        buying_power=float(account_values.get("buying_power") or 0.0),
        positions=snapshot_positions,
    )


def _snapshot_to_portfolio_view(snapshot) -> tuple[dict[str, dict[str, float | int]], dict[str, float]]:
    positions = {}
    for position in getattr(snapshot, "positions", ()) or ():
        positions[str(position.symbol).strip().upper()] = {
            "quantity": int(position.quantity),
            "avg_cost": float(position.average_cost or 0.0),
        }
    account_values = {
        "equity": float(getattr(snapshot, "total_equity", 0.0) or 0.0),
        "buying_power": float(getattr(snapshot, "buying_power", 0.0) or 0.0),
    }
    return positions, account_values


def run_strategy_core(
    *,
    runtime: IBKRRebalanceRuntime | None = None,
    config: IBKRRebalanceConfig | None = None,
    connect_ib=None,
    get_current_portfolio=None,
    compute_signals=None,
    execute_rebalance=None,
    send_tg_message=None,
    translator=None,
    separator=None,
    strategy_display_name=None,
    reconciliation_output_path=None,
):
    if runtime is None:
        if not all((connect_ib, get_current_portfolio, compute_signals, execute_rebalance, send_tg_message)):
            raise ValueError("Legacy IBKR rebalance call requires connect_ib/get_current_portfolio/compute_signals/execute_rebalance/send_tg_message")
        runtime = IBKRRebalanceRuntime(
            connect_ib=connect_ib,
            portfolio_port_factory=lambda ib: CallablePortfolioPort(
                lambda: _legacy_portfolio_snapshot(ib, get_current_portfolio=get_current_portfolio)
            ),
            compute_signals=compute_signals,
            execute_rebalance=execute_rebalance,
            notifications=CallableNotificationPort(send_tg_message),
        )
    if config is None:
        if translator is None or separator is None:
            raise ValueError("IBKR rebalance config requires translator and separator")
        config = IBKRRebalanceConfig(
            translator=translator,
            separator=separator,
            strategy_display_name=strategy_display_name,
            reconciliation_output_path=reconciliation_output_path,
        )

    notification_publisher = NotificationPublisher(
        log_message=lambda message: print(message, flush=True),
        send_message=runtime.notifications.send_text,
    )
    ib = None
    try:
        ib = runtime.connect_ib()
        snapshot = runtime.portfolio_port_factory(ib).get_portfolio_snapshot()
        positions, account_values = _snapshot_to_portfolio_view(snapshot)
        current_holdings = set(positions.keys())
        signal_result = runtime.compute_signals(ib, current_holdings)
        if len(signal_result) == 5:
            target_weights, signal_desc, _is_emergency, status_desc, signal_metadata = signal_result
        else:
            target_weights, signal_desc, _is_emergency, status_desc = signal_result
            signal_metadata = {}
        allocation = _resolve_weight_allocation(signal_metadata, required=target_weights is not None)
        resolved_target_weights = dict(allocation.get("targets") or {}) if target_weights is not None else None

        dashboard = notification_renderers.build_dashboard(
            positions,
            account_values,
            signal_desc,
            status_desc,
            strategy_profile=signal_metadata.get("strategy_profile"),
            strategy_display_name=config.strategy_display_name,
            target_weights=resolved_target_weights,
            signal_metadata=signal_metadata,
            translator=config.translator,
            separator=config.separator,
            status_icon=signal_metadata.get("status_icon", "🐤"),
        )
        strategy_dashboard = _strategy_dashboard_text(signal_metadata, translator=config.translator)

        if target_weights is None:
            decision = signal_metadata.get("snapshot_guard_decision")
            no_op_reason = signal_metadata.get("no_op_reason")
            fail_reason = signal_metadata.get("fail_reason")
            notification_context = signal_metadata.get("notification_context")
            status_context = (
                notification_context.get("status")
                if isinstance(notification_context, Mapping)
                else None
            )
            rendered_status = _render_notification_context_text(
                status_context,
                translator=config.translator,
                fallback="",
            )
            no_op_segments = [config.translator("no_trades")]
            if rendered_status:
                no_op_segments.append(rendered_status)
            else:
                if decision:
                    no_op_segments.append(
                        config.translator(
                            "snapshot_decision_detail",
                            value=_translate_snapshot_guard_decision(decision, translator=config.translator),
                        )
                    )
                if no_op_reason:
                    no_op_segments.append(
                        _localize_notification_text(f"reason={no_op_reason}", translator=config.translator)
                    )
                if fail_reason:
                    no_op_segments.append(
                        _localize_notification_text(f"fail_reason={fail_reason}", translator=config.translator)
                    )
            no_op_text = " | ".join(segment for segment in no_op_segments if str(segment).strip())
            no_op_text = "\n".join(_split_labeled_text(no_op_text))
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
            record_path = write_reconciliation_record(record, output_path=config.reconciliation_output_path)
            print(
                "reconciliation_record "
                + json.dumps({"path": str(record_path), "status": record.get("execution_status"), "no_op_reason": record.get("no_op_reason")}, ensure_ascii=False),
                flush=True,
            )
            notification_publisher.publish(
                notification_renderers.render_heartbeat_notification(
                    dashboard=dashboard,
                    strategy_dashboard=strategy_dashboard,
                    no_op_text=no_op_text,
                    signal_desc=signal_desc,
                    status_desc=status_desc,
                    status_icon=signal_metadata.get("status_icon", "🐤"),
                    translator=config.translator,
                    separator=config.separator,
                    strategy_display_name=config.strategy_display_name,
                )
            )
            return StrategyCycleResult(
                result="OK - heartbeat",
                signal_metadata=dict(signal_metadata or {}),
                target_weights=None,
                execution_summary={},
                reconciliation_record=dict(record),
                reconciliation_record_path=str(record_path),
            )

        execution_result = runtime.execute_rebalance(
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
        record_path = write_reconciliation_record(record, output_path=config.reconciliation_output_path)
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
        notification_publisher.publish(
            notification_renderers.render_trade_notification(
                dashboard=dashboard,
                strategy_dashboard=strategy_dashboard,
                trade_logs=trade_logs,
                execution_summary=execution_summary,
                signal_desc=signal_desc,
                status_desc=status_desc,
                status_icon=signal_metadata.get("status_icon", "🐤"),
                translator=config.translator,
                separator=config.separator,
                strategy_display_name=config.strategy_display_name,
            )
        )
        return StrategyCycleResult(
            result="OK - executed",
            signal_metadata=dict(signal_metadata or {}),
            target_weights=dict(resolved_target_weights or {}),
            execution_summary=dict(execution_summary or {}),
            reconciliation_record=dict(record),
            reconciliation_record_path=str(record_path),
        )
    finally:
        if ib is not None and ib.isConnected():
            ib.disconnect()
