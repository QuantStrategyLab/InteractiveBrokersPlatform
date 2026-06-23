"""Notification rendering helpers for InteractiveBrokersPlatform."""

from __future__ import annotations

from collections.abc import Mapping
import re

from notifications.events import RenderedNotification
from quant_platform_kit.common.quantity import format_quantity
from quant_platform_kit.common.notification_localization import (
    localize_notification_text as _base_localize_notification_text,
    translator_uses_zh as _base_translator_uses_zh,
)

_PRICE_SOURCE_LABELS = {
    "longbridge_candlesticks": ("LongBridge 日线K线", "LongBridge daily candlesticks"),
    "schwab_daily_history_with_live_quote_overlay": ("Schwab 日线历史", "Schwab daily history"),
    "firstrade_ohlc_with_live_quote_overlay": ("Firstrade OHLC", "Firstrade OHLC"),
    "market_quote": ("实时行情报价", "market quote"),
    "mixed_market_quote_snapshot_close": (
        "实时行情报价 + 快照收盘价回补",
        "market quote + snapshot close fallback",
    ),
    "mixed_market_quote_historical_close": (
        "实时行情报价 + 历史收盘价回补",
        "market quote + historical close fallback",
    ),
    "snapshot_close": ("快照收盘价", "snapshot close"),
    "historical_close": ("历史收盘价", "historical close"),
    "market_data": ("市场数据", "market data"),
}

try:
    from quant_platform_kit.common.notification_localization import (
        localize_price_source_label as _shared_localize_price_source_label,
    )
except ImportError:  # pragma: no cover - compatibility with older pinned shared wheels
    _shared_localize_price_source_label = None


def _localize_price_source_label(value, *, translator=None, locale=None):
    source = str(value or "").strip()
    use_zh = _base_translator_uses_zh(translator) if translator is not None else str(locale or "").startswith("zh")
    if not source:
        return "未知" if use_zh else "unknown"
    label = _PRICE_SOURCE_LABELS.get(source)
    if label is not None:
        return label[0] if use_zh else label[1]
    if _shared_localize_price_source_label is not None:
        return _shared_localize_price_source_label(source, translator=translator, locale=locale)
    return source.replace("_", " ")

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


def _extra_notification_lines(extra_notification_lines) -> list[str]:
    return [str(line).strip() for line in extra_notification_lines or () if str(line).strip()]


def _localize_notification_text(text: str, *, translator) -> str:
    return _base_localize_notification_text(
        text,
        translator=translator,
        extra_replacements=_EXTRA_ZH_REASON_REPLACEMENTS,
    )


def _localize_timing_contract(contract: str, *, translator) -> str:
    value = str(contract or "").strip()
    if not value:
        return ""
    if value == "same_trading_day":
        return "当日执行" if _translator_uses_zh(translator) else "same trading day"
    if value == "next_trading_day":
        return "次一交易日执行" if _translator_uses_zh(translator) else "next trading day"
    match = re.fullmatch(r"next_(\d+)_trading_days", value)
    if match:
        count = int(match.group(1))
        if _translator_uses_zh(translator):
            return f"{count}个交易日后执行"
        return f"next {count} trading days"
    return _localize_notification_text(value, translator=translator)


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
        quantity = float(order.get("quantity") or 0.0)
        if symbol and quantity > 0:
            preview.append(f"{symbol} {format_quantity(quantity)}")
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
        ("orders_skipped", "skipped"),
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
    elif no_op_reason:
        lines.append(
            translator(
                "no_order_plan_reason",
                reason=_localize_notification_text(f"reason={no_op_reason}", translator=translator),
            )
        )

    fallback_symbols = tuple(execution_summary.get("snapshot_price_fallback_symbols") or ())
    if execution_summary.get("snapshot_price_fallback_used") and fallback_symbols:
        lines.append(
            translator(
                "dry_run_snapshot_prices"
                if execution_summary.get("mode") == "dry_run"
                else "price_fallback_prices",
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
    localized_contract = _localize_timing_contract(contract, translator=translator)
    if signal_date and effective_date:
        value = f"{signal_date} -> {effective_date}"
    else:
        value = signal_date or effective_date or localized_contract
    if localized_contract and localized_contract not in value:
        value = f"{value} ({localized_contract})" if value else localized_contract
    return [f"{label}: {value}"]


def _format_percent(value) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "n/a"


def _as_float_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_percentile(value) -> str:
    try:
        percentile = float(value) * 100
    except (TypeError, ValueError):
        return "p?"
    if float(percentile).is_integer():
        return f"p{int(percentile)}"
    return f"p{percentile:.1f}"


def _format_sample_count(value) -> str:
    try:
        count = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if float(count).is_integer():
        return str(int(count))
    return f"{count:.1f}"


def _present(value) -> bool:
    return value not in (None, "")


def _is_truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _effective_volatility_delever_threshold(signal_metadata, *, prefix: str):
    mode = str(signal_metadata.get(f"{prefix}_threshold_mode") or "").strip().lower()
    dynamic_threshold = signal_metadata.get(f"{prefix}_dynamic_threshold")
    if mode == "rolling_percentile" and _present(dynamic_threshold):
        return dynamic_threshold
    return signal_metadata.get(f"{prefix}_threshold")


def _format_volatility_delever_threshold_detail(signal_metadata, *, prefix: str, translator) -> str:
    mode = str(signal_metadata.get(f"{prefix}_threshold_mode") or "").strip().lower()
    fixed_threshold = signal_metadata.get(f"{prefix}_threshold")
    dynamic_threshold = signal_metadata.get(f"{prefix}_dynamic_threshold")
    if mode == "rolling_percentile":
        kwargs = {
            "percentile": _format_percentile(signal_metadata.get(f"{prefix}_dynamic_percentile")),
            "lookback": _format_sample_count(signal_metadata.get(f"{prefix}_dynamic_lookback")),
            "min_periods": _format_sample_count(signal_metadata.get(f"{prefix}_dynamic_min_periods")),
            "sample_count": _format_sample_count(signal_metadata.get(f"{prefix}_dynamic_sample_count")),
            "floor": _format_percent(signal_metadata.get(f"{prefix}_dynamic_floor")),
            "cap": _format_percent(signal_metadata.get(f"{prefix}_dynamic_cap")),
            "fixed_threshold": _format_percent(fixed_threshold),
        }
        if _present(dynamic_threshold):
            return translator("blend_gate_volatility_threshold_detail_dynamic", **kwargs)
        return translator("blend_gate_volatility_threshold_detail_dynamic_fallback", **kwargs)
    return translator(
        "blend_gate_volatility_threshold_detail_fixed",
        threshold=_format_percent(fixed_threshold),
    )


def _format_tqqq_volatility_delever_allocation_detail(
    signal_metadata,
    *,
    prefix: str,
    redirect_symbol: str,
    translator,
) -> str:
    retained_ratio = _as_float_or_none(signal_metadata.get(f"{prefix}_retained_ratio"))
    redirected_ratio = _as_float_or_none(signal_metadata.get(f"{prefix}_redirected_ratio"))
    if retained_ratio is None:
        retained_ratio = _as_float_or_none(signal_metadata.get(f"{prefix}_retention_ratio"))
    if redirected_ratio is None and retained_ratio is not None:
        redirected_ratio = max(0.0, min(1.0, 1.0 - retained_ratio))
    return translator(
        "tqqq_volatility_delever_allocation_detail",
        retained_ratio=_format_percent(retained_ratio),
        redirected_ratio=_format_percent(redirected_ratio),
        redirect_symbol=redirect_symbol or "QQQ",
    )


def _build_tqqq_risk_control_lines(signal_metadata, *, translator) -> list[str]:
    prefix = "dual_drive_volatility_delever"
    if not _is_truthy(signal_metadata.get(f"{prefix}_applied")):
        return []
    redirect_symbol = str(signal_metadata.get(f"{prefix}_redirect_symbol") or "QQQ").strip().upper()
    window = str(signal_metadata.get(f"{prefix}_window") or "5").strip()
    threshold = _effective_volatility_delever_threshold(signal_metadata, prefix=prefix)
    threshold_detail = _format_volatility_delever_threshold_detail(
        signal_metadata,
        prefix=prefix,
        translator=translator,
    )
    allocation_detail = _format_tqqq_volatility_delever_allocation_detail(
        signal_metadata,
        prefix=prefix,
        redirect_symbol=redirect_symbol or "QQQ",
        translator=translator,
    )
    if str(signal_metadata.get(f"{prefix}_trigger_reason") or "").strip() == "hysteresis_hold":
        return [
            translator(
                "risk_control_tqqq_volatility_delever_hysteresis_dynamic",
                window=window,
                volatility=_format_percent(signal_metadata.get(f"{prefix}_metric")),
                exit_threshold=_format_percent(signal_metadata.get(f"{prefix}_exit_threshold")),
                threshold=_format_percent(threshold),
                threshold_detail=threshold_detail,
                source_symbol="TQQQ",
                redirect_symbol=redirect_symbol or "QQQ",
                allocation_detail=allocation_detail,
            )
        ]
    return [
        translator(
            "risk_control_tqqq_volatility_delever_applied_dynamic",
            window=window,
            volatility=_format_percent(signal_metadata.get(f"{prefix}_metric")),
            threshold=_format_percent(threshold),
            threshold_detail=threshold_detail,
            source_symbol="TQQQ",
            redirect_symbol=redirect_symbol or "QQQ",
            allocation_detail=allocation_detail,
        )
    ]


def _format_signal_snapshot_line(snapshot, *, translator) -> str:
    if not isinstance(snapshot, Mapping):
        return ""
    market_date = str(snapshot.get("market_date") or snapshot.get("signal_as_of") or "").strip()
    source = str(snapshot.get("latest_price_source") or "").strip()
    warning = snapshot.get("data_freshness_warning")
    if not market_date and not source and warning in (None, "", False):
        return ""
    if _translator_uses_zh(translator):
        parts = [
            f"日期 {market_date or '未知'}",
            f"数据源 {_localize_price_source_label(source, translator=translator)}",
        ]
        if warning not in (None, "", False):
            parts.append(f"提示 {_localize_notification_text(warning, translator=translator)}")
        return "🧾 信号快照: " + " | ".join(parts)
    parts = [
        f"date {market_date or 'unknown'}",
        f"source {_localize_price_source_label(source, translator=translator)}",
    ]
    if warning not in (None, "", False):
        parts.append(f"warning {warning}")
    return "🧾 Signal snapshot: " + " | ".join(parts)


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
) -> str:
    lines = [title]
    strategy_name = _format_text(strategy_display_name, fallback="<unknown>")
    lines.append(translator("strategy_label", name=strategy_name))
    lines.extend(_extra_notification_lines(extra_notification_lines))
    dashboard = _format_dashboard_text(dashboard_text)
    if include_dashboard and dashboard:
        lines.append(separator)
        lines.extend(dashboard.splitlines())
    elif dashboard:
        lines.extend(_extract_timing_lines(dashboard))
    status_summary = _first_summary_text(status_desc, translator=translator)
    signal_summary = _first_summary_text(signal_desc, translator=translator)
    status_summary = _localize_signal_state(status_summary, translator=translator)
    signal_summary = _localize_signal_state(signal_summary, translator=translator)
    status_line = f"{status_icon} {status_summary}".strip() if status_summary else None
    if status_line and status_summary != signal_summary:
        lines.append(status_line)
    signal_line = f"🎯 {signal_summary}".strip() if signal_summary else None
    if signal_line:
        lines.append(signal_line)
    compact_body = [str(line).strip() for line in body_lines or () if str(line).strip()]
    if compact_body:
        lines.append(separator)
        lines.extend(compact_body)
    return "\n".join(lines)


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
) -> RenderedNotification:
    extra_lines = _extra_notification_lines(extra_notification_lines)
    detailed_parts = [translator("heartbeat_title"), *extra_lines, dashboard, separator, no_op_text]
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
            "orders_filled",
            "orders_partially_filled",
            "orders_skipped",
            "option_orders_submitted",
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
    )
    return RenderedNotification(detailed_text=detailed_text, compact_text=compact_text)
