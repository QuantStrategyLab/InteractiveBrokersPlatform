"""Notification rendering helpers for InteractiveBrokersPlatform."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import math

from notifications.compact_adapter import adapt_compact_sections
from notifications.events import RenderedNotification
from quant_platform_kit.common.quantity import format_quantity
from quant_platform_kit.common.notification_localization import (
    localize_notification_text as _base_localize_notification_text,
)
from quant_platform_kit.notifications.renderer_base import (
    as_float_or_none as _as_float_or_none,
    build_timing_audit_lines as _build_timing_audit_lines_shared,
    build_tqqq_risk_control_lines as _build_tqqq_risk_control_lines_shared,
    compact_dashboard_lines,
    effective_volatility_delever_threshold as _effective_volatility_delever_threshold,
    format_percent as _format_percent,
    format_percentile as _format_percentile,
    format_sample_count as _format_sample_count,
    format_signal_snapshot_line as _format_signal_snapshot_line_shared,
    format_tqqq_volatility_delever_allocation_detail as _format_tqqq_volatility_delever_allocation_detail,
    format_volatility_delever_threshold_detail as _format_volatility_delever_threshold_detail,
    is_compact_dashboard_audit_line,
    is_truthy as _is_truthy,
    localize_price_source_label as _localize_price_source_label,
    localize_timing_contract as _localize_timing_contract,
    present as _present,
    relabel_dashboard_cash_labels as _relabel_dashboard_cash_labels_shared,
    resolve_execution,
    split_detail_segment as _split_detail_segment,
    split_labeled_text as _split_labeled_text,
    translator_uses_zh as _translator_uses_zh,
)

_EXTRA_ZH_REASON_REPLACEMENTS = (
    ("pending_orders_detected", "检测到未完成订单"),
    ("same_day_execution_locked", "当日执行锁已存在"),
    ("same_day_fills_detected", "检测到当日成交"),
    ("target_diff_below_threshold", "调仓差异低于阈值"),
    ("min_notional", "低于最小订单金额"),
    ("quantity_zero", "整数股数量为0"),
    ("fail_reason=", "失败原因="),
    ("decision=", "决策="),
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


def _extra_notification_lines(extra_notification_lines) -> list[str]:
    return [str(line).strip() for line in extra_notification_lines or () if str(line).strip()]


def _localize_notification_text(text: str, *, translator) -> str:
    return _base_localize_notification_text(
        text,
        translator=translator,
        extra_replacements=_EXTRA_ZH_REASON_REPLACEMENTS,
    )


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
        quantity = float(order.get("quantity") or 0.0)
        if symbol and quantity > 0:
            preview.append(f"{symbol} {format_quantity(quantity)}")
        elif symbol:
            preview.append(symbol)
    remaining = len(orders) - len(preview)
    if remaining > 0:
        preview.append(f"+{remaining}")
    return ", ".join(preview)


_SOFT_ORDER_SKIP_REASONS = frozenset(
    {
        "quantity_zero",
        "min_notional",
        "below_trade_threshold",
        "target_diff_below_threshold",
        "pending_sell_release",
        "negative_cash",
        "insufficient_buying_power",
    }
)
_HARD_ORDER_SKIP_STATUS = frozenset(
    {
        "cancelled",
        "canceled",
        "rejected",
        "inactive",
        "api cancelled",
        "error",
    }
)


def _normalize_order_skip_reason(order) -> str:
    reason = str(order.get("reason") or "").strip().lower()
    status = str(order.get("status") or "").strip()
    if reason and reason not in {"submit_failed", "unknown"}:
        return reason
    if status:
        normalized_status = status.lower()
        if normalized_status in _HARD_ORDER_SKIP_STATUS:
            return normalized_status
        if normalized_status not in {"", "submitted", "pendingsubmit", "presubmitted", "filled"}:
            return normalized_status
    return reason or "submit_failed"


def _is_soft_order_skip(order) -> bool:
    return _normalize_order_skip_reason(order) in _SOFT_ORDER_SKIP_REASONS


def _skip_reason_i18n_key(reason: str) -> str:
    normalized = str(reason or "").strip().lower().replace(" ", "_")
    return f"skip_reason_{normalized}"


def _localize_order_skip_reason(order, *, translator) -> str:
    reason = _normalize_order_skip_reason(order)
    key = _skip_reason_i18n_key(reason)
    localized = translator(key)
    if localized != key:
        return localized
    fallback = _localize_notification_text(f"reason={reason}", translator=translator)
    if fallback.startswith("reason="):
        return reason.replace("_", " ")
    return fallback


def _format_skip_order_detail(*, symbol, reason_text, quantity=None, translator) -> str:
    if quantity and float(quantity) > 0:
        return translator(
            "skip_order_detail_with_qty",
            symbol=symbol,
            quantity=format_quantity(quantity),
            reason=reason_text,
        )
    return translator(
        "skip_order_detail",
        symbol=symbol,
        reason=reason_text,
    )


def _summarize_skipped_orders(orders, *, translator, limit: int = 3) -> str:
    preview = []
    for order in orders[:limit]:
        symbol = str(order.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        quantity = float(order.get("quantity") or 0.0)
        reason_text = _localize_order_skip_reason(order, translator=translator)
        preview.append(
            _format_skip_order_detail(
                symbol=symbol,
                reason_text=reason_text,
                quantity=quantity if quantity > 0 else None,
                translator=translator,
            )
        )
    remaining = len(orders) - len(preview)
    if remaining > 0:
        preview.append(f"+{remaining}")
    return ", ".join(preview)


def _append_skipped_order_batch_lines(lines, skipped_orders, *, translator) -> None:
    if not skipped_orders:
        return
    buy_orders = [
        order for order in skipped_orders if str(order.get("side") or "").strip().lower() == "buy"
    ]
    sell_orders = [
        order for order in skipped_orders if str(order.get("side") or "").strip().lower() == "sell"
    ]
    for side, orders in (("buy", buy_orders), ("sell", sell_orders)):
        if not orders:
            continue
        soft_orders = [order for order in orders if _is_soft_order_skip(order)]
        hard_orders = [order for order in orders if not _is_soft_order_skip(order)]
        if soft_orders:
            lines.append(
                translator(
                    f"deferred_{side}_batch",
                    details=_summarize_skipped_orders(soft_orders, translator=translator),
                )
            )
        if hard_orders:
            lines.append(
                translator(
                    f"failed_{side}_batch",
                    details=_summarize_skipped_orders(hard_orders, translator=translator),
                )
            )


def _build_order_batch_lines(execution_summary, *, translator) -> list[str]:
    mode = str(execution_summary.get("mode") or "").strip().lower()
    order_groups = [
        ("orders_submitted", "dry_run" if mode == "dry_run" else "submitted"),
        ("orders_pending", "pending"),
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
    _append_skipped_order_batch_lines(
        lines,
        list(execution_summary.get("orders_skipped") or ()),
        translator=translator,
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

    order_fields = (
        "orders_submitted",
        "orders_pending",
        "orders_filled",
        "orders_partially_filled",
        "orders_skipped",
        "option_orders_submitted",
        "option_orders_pending",
        "option_orders_filled",
        "option_orders_partially_filled",
        "option_orders_skipped",
    )
    has_order_activity = any(execution_summary.get(field) for field in order_fields)
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
    elif no_op_reason and not has_order_activity:
        lines.append(
            translator(
                "no_order_plan_reason",
                reason=_localize_notification_text(f"reason={no_op_reason}", translator=translator),
            )
        )

    lines.extend(_build_order_batch_lines(execution_summary, translator=translator))

    for raw_line in trade_logs or ():
        text = _localize_notification_text(str(raw_line).strip(), translator=translator)
        if not text:
            continue
        if text.startswith(("目标差异 ", "target_diff ", "DRY_RUN buy ", "DRY_RUN sell ")):
            continue
        if text.startswith(("ℹ️", "📏")):
            continue
        if text.startswith(("🧪 dry-run估价:", "🧪 dry-run pricing:")):
            continue
        if "execution_lock_acquired" in text or "已获取执行锁" in text:
            continue
        if text.startswith(("profile=", "strategy_profile=", "execution_profile=", "策略=", "执行配置=")):
            continue
        if "same_day_execution_locked" in text or "当日执行锁已存在" in text:
            continue
        if text not in lines:
            lines.extend(_split_labeled_text(text))

    return lines


def _build_detailed_trade_lines(trade_logs, *, translator) -> list[str]:
    lines: list[str] = []
    for raw_line in trade_logs or ():
        text = _localize_notification_text(str(raw_line).strip(), translator=translator)
        if not text:
            continue
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


def _relabel_dashboard_cash_labels(text: str, *, cash_only_execution: bool) -> str:
    """Delegates to shared renderer_base; IBKR uses hardcoded labels (no translator)."""
    return _relabel_dashboard_cash_labels_shared(
        text, cash_only_execution=cash_only_execution, translator=None,
    )


def _format_compact_dashboard_text(text) -> str:
    lines = []
    for line in _format_dashboard_text(text).splitlines():
        if not line.strip() or is_compact_dashboard_audit_line(line):
            continue
        lines.append(line)
    return "\n".join(lines)


def _build_timing_audit_lines(signal_metadata, *, translator) -> list[str]:
    execution = resolve_execution(
        signal_metadata if isinstance(signal_metadata, Mapping) else {},
    )
    return _build_timing_audit_lines_shared(execution, translator=translator)


def _build_tqqq_risk_control_lines(signal_metadata, *, translator) -> list[str]:
    return _build_tqqq_risk_control_lines_shared(
        signal_metadata if isinstance(signal_metadata, Mapping) else {},
        translator=translator,
    )


def _format_signal_snapshot_line(snapshot, *, translator) -> str:
    return _format_signal_snapshot_line_shared(
        snapshot,
        translator=translator,
        localize_text=_localize_notification_text,
    )


def _strategy_dashboard_text(signal_metadata, *, translator) -> str:
    metadata = signal_metadata if isinstance(signal_metadata, Mapping) else {}
    raw_annotations = metadata.get("execution_annotations")
    annotations = raw_annotations if isinstance(raw_annotations, Mapping) else {}
    risk_source = {**metadata, **annotations}
    dashboard_text = _format_dashboard_text(
        annotations.get("dashboard_text")
        or metadata.get("dashboard_text")
        or metadata.get("dashboard")
        or ""
    )
    dashboard_text = _relabel_dashboard_cash_labels(
        dashboard_text,
        cash_only_execution=bool(metadata.get("cash_only_execution", True)),
    )
    risk_control_lines = _build_tqqq_risk_control_lines(risk_source, translator=translator)
    timing_lines = _build_timing_audit_lines(metadata, translator=translator)
    snapshot_line = _format_signal_snapshot_line(metadata.get("signal_snapshot"), translator=translator)
    audit_lines = [*risk_control_lines, *timing_lines, *([snapshot_line] if snapshot_line else [])]
    if not audit_lines:
        return dashboard_text
    if not dashboard_text:
        return "\n".join(audit_lines)
    return f"{dashboard_text}\n" + "\n".join(audit_lines)


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
    cash_only_execution = signal_metadata.get("cash_only_execution", True)
    buying_power_label = "buying_power" if cash_only_execution else "buying_power_margin"
    position_lines = []
    for symbol in sorted(positions.keys()):
        qty = positions[symbol]["quantity"]
        avg = positions[symbol]["avg_cost"]
        market_value = qty * avg
        position_lines.append(f"  - {symbol}: {format_quantity(qty)}股 | ${market_value:,.2f}")
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
    diagnostics_lines.extend(_build_timing_audit_lines(signal_metadata, translator=translator))
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
        f"  - {translator(buying_power_label)}: ${buying_power:,.2f}\n"
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


def _first_summary_text(text: str, *, translator) -> str:
    localized = _localize_notification_text(text, translator=translator)
    lines = _split_labeled_text(localized)
    return lines[0] if lines else ""


def _localize_signal_state(text: str, *, translator) -> str:
    value = str(text or "").strip()
    if value.lower() not in {"hold", "entry", "reduce", "exit", "idle"}:
        return value
    key = f"signal_state_{value.lower()}"
    translated = translator(key)
    return value if translated == key else str(translated)


def _extract_timing_lines(text: str) -> list[str]:
    return [
        line.strip()
        for line in _format_dashboard_text(text).splitlines()
        if line.strip().startswith("⏱")
    ]


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
    extra_notification_lines=(),
    include_dashboard: bool = False,
    account_equity_line: str = "",
    supplemental_lines=(),
) -> str:
    """Minimal user notification: strategy, total assets, and cycle/order result."""
    lines = [title]
    strategy_name = _format_text(strategy_display_name, fallback="<unknown>")
    lines.append(translator("strategy_label", name=strategy_name))
    total_assets_line = account_equity_line or _compact_total_assets_line(dashboard_text)
    if total_assets_line:
        lines.append(total_assets_line)
    lines.extend(
        adapt_compact_sections(
            dashboard_text,
            locale="zh" if _translator_uses_zh(translator) else "en",
            supplemental_lines=supplemental_lines,
        )
    )
    compact_body = [str(line).strip() for line in body_lines or () if str(line).strip()]
    if compact_body:
        lines.extend(compact_body)
    else:
        lines.append(translator("no_trades"))
    return "\n".join(lines)


def _compact_total_assets_line(dashboard_text: str) -> str:
    labels = (
        "总资产",
        "账户总权益",
        "净值",
        "total assets",
        "total account equity",
        "net assets",
        "net value",
        "equity",
    )
    for raw_line in str(dashboard_text or "").splitlines():
        for segment in raw_line.split(" | "):
            line = segment.strip().lstrip("- ").strip()
            lowered = line.lower()
            if any(label in lowered for label in labels) and ":" in line:
                return line if line.startswith("💰") else f"💰 {line}"
    return ""


def render_heartbeat_notification(
    *,
    dashboard,
    strategy_dashboard,
    no_op_text,
    signal_desc,
    status_desc,
    status_icon,
    translator,
    separator,
    strategy_display_name,
    extra_notification_lines=(),
    account_snapshot=None,
    compact_supplemental_lines=(),
) -> RenderedNotification:
    extra_lines = _extra_notification_lines(extra_notification_lines)
    snapshot = account_snapshot if isinstance(account_snapshot, Mapping) else {}
    currency = str(snapshot.get("currency") or "").strip().upper()
    observed_at = snapshot.get("observed_at")
    observed_at = observed_at.isoformat() if isinstance(observed_at, datetime) else str(observed_at or "").strip()
    amount_lines = []
    account_equity_line = ""
    verified = False
    for field, label in (("available_cash", "heartbeat_available_cash"), ("net_assets", "heartbeat_account_equity")):
        amount = snapshot.get(field)
        valid = (
            isinstance(amount, (int, float)) and not isinstance(amount, bool)
            and math.isfinite(amount) and bool(currency) and bool(observed_at)
            and (field != "net_assets" or amount > 0)
        )
        verified = verified or valid
        rendered_amount = translator(
            label,
            value=f"{currency} {amount:,.2f}" if valid else translator("heartbeat_unverified"),
        )
        amount_lines.append(rendered_amount)
        if field == "net_assets" and valid:
            account_equity_line = rendered_amount
    if verified:
        amount_lines.append(translator("heartbeat_observed_at", value=observed_at))
    detailed_parts = [translator("heartbeat_title"), *extra_lines, *amount_lines, dashboard, separator, no_op_text]
    detailed_text = "\n".join(str(part) for part in detailed_parts if str(part).strip())
    compact_text = _build_compact_message(
        title=translator("heartbeat_title"),
        strategy_display_name=strategy_display_name,
        signal_desc=signal_desc,
        status_desc=status_desc,
        status_icon=status_icon,
        translator=translator,
        separator=separator,
        body_lines=[no_op_text],
        dashboard_text=strategy_dashboard,
        extra_notification_lines=extra_lines,
        include_dashboard=True,
        account_equity_line=account_equity_line,
        supplemental_lines=compact_supplemental_lines,
    )
    return RenderedNotification(detailed_text=detailed_text, compact_text=compact_text)


def render_trade_notification(
    *,
    dashboard,
    strategy_dashboard,
    trade_logs,
    execution_summary,
    signal_desc,
    status_desc,
    status_icon,
    translator,
    separator,
    strategy_display_name,
    extra_notification_lines=(),
) -> RenderedNotification:
    extra_lines = _extra_notification_lines(extra_notification_lines)
    execution_summary = dict(execution_summary or {})
    has_order_events = any(
        execution_summary.get(field_name)
        for field_name in (
            "orders_submitted",
            "orders_pending",
            "orders_filled",
            "orders_partially_filled",
            "orders_skipped",
            "option_orders_submitted",
            "option_orders_pending",
            "option_orders_filled",
            "option_orders_partially_filled",
            "option_orders_skipped",
        )
    )
    if trade_logs or has_order_events:
        notification_trade_lines = _build_notification_trade_lines(
            trade_logs,
            execution_summary=execution_summary,
            translator=translator,
        )
        detailed_trade_lines = _build_detailed_trade_lines(
            trade_logs,
            translator=translator,
        )
        detailed_text = (
            f"{translator('rebalance_title')}\n"
            f"{chr(10).join(extra_lines) + chr(10) if extra_lines else ''}"
            f"{dashboard}\n"
            f"{separator}\n"
            f"{chr(10).join(detailed_trade_lines)}"
        )
        compact_text = _build_compact_message(
            title=translator("rebalance_title"),
            strategy_display_name=strategy_display_name,
            signal_desc=signal_desc,
            status_desc=status_desc,
            status_icon=status_icon,
            translator=translator,
            separator=separator,
            body_lines=notification_trade_lines,
            dashboard_text=strategy_dashboard,
            extra_notification_lines=extra_lines,
            include_dashboard=True,
            supplemental_lines=execution_summary.get("compact_supplemental_lines", ()),
        )
        return RenderedNotification(detailed_text=detailed_text, compact_text=compact_text)

    detailed_parts = [translator("heartbeat_title"), *extra_lines, dashboard, separator, translator("no_trades")]
    detailed_text = "\n".join(str(part) for part in detailed_parts if str(part).strip())
    compact_text = _build_compact_message(
        title=translator("heartbeat_title"),
        strategy_display_name=strategy_display_name,
        signal_desc=signal_desc,
        status_desc=status_desc,
        status_icon=status_icon,
        translator=translator,
        separator=separator,
        body_lines=[translator("no_trades")],
        dashboard_text=strategy_dashboard,
        extra_notification_lines=extra_lines,
        include_dashboard=True,
        supplemental_lines=execution_summary.get("compact_supplemental_lines", ()),
    )
    return RenderedNotification(detailed_text=detailed_text, compact_text=compact_text)
