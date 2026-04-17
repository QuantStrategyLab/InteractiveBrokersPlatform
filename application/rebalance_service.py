"""Application orchestration for InteractiveBrokersPlatform."""

from __future__ import annotations

import json

from application.reconciliation_service import (
    build_reconciliation_record,
    write_reconciliation_record,
)


_ZH_REASON_REPLACEMENTS = (
    ("feature snapshot guard blocked execution", "特征快照校验阻止执行"),
    ("feature snapshot required", "需要特征快照"),
    ("feature snapshot compute failed", "特征快照计算失败"),
    ("feature_snapshot_download_failed", "特征快照下载失败"),
    ("feature_snapshot_compute_failed", "特征快照计算失败"),
    ("feature_snapshot_path_missing", "缺少特征快照路径"),
    ("feature_snapshot_missing", "特征快照不存在"),
    ("feature_snapshot_stale", "特征快照过旧"),
    ("feature_snapshot_manifest_missing", "缺少快照清单"),
    ("feature_snapshot_profile_mismatch", "快照策略名不匹配"),
    ("feature_snapshot_config_name_mismatch", "快照配置名不匹配"),
    ("feature_snapshot_config_path_mismatch", "快照配置路径不匹配"),
    ("feature_snapshot_contract_version_mismatch", "快照契约版本不匹配"),
    ("soxl_soxx_trend_income", "SOXL/SOXX 半导体趋势收益"),
    ("tqqq_growth_income", "TQQQ 增长收益"),
    ("global_etf_rotation", "全球 ETF 轮动"),
    ("russell_1000_multi_factor_defensive", "罗素1000多因子"),
    ("tech_communication_pullback_enhancement", "科技通信回调增强"),
    ("qqq_tech_enhancement", "科技通信回调增强"),
    ("mega_cap_leader_rotation_aggressive", "Mega Cap 激进龙头轮动"),
    ("mega_cap_leader_rotation_dynamic_top20", "Mega Cap 动态 Top20 龙头轮动"),
    ("mega_cap_leader_rotation_top50_balanced", "Mega Cap Top50 平衡龙头轮动"),
    ("dynamic_mega_leveraged_pullback", "Mega Cap 2x 回调策略"),
    ("outside_monthly_execution_window", "当前不在月度执行窗口"),
    ("no_execution_window_after_snapshot", "快照后没有可用执行窗口"),
    ("no-op", "不执行"),
    ("monthly snapshot cadence", "月度快照节奏"),
    ("waiting inside execution window", "等待进入执行窗口"),
    ("small_account_warning=true", "小账户提示=是"),
    ("portfolio_equity=", "净值="),
    ("min_recommended_equity=", "建议最低净值="),
    (
        "integer_shares_min_position_value_may_prevent_backtest_replication",
        "整数股和最小仓位限制可能导致实盘无法完全复现回测",
    ),
    (
        "integer-share minimum position sizing may prevent backtest replication",
        "整数股和最小仓位限制可能导致实盘无法完全复现回测",
    ),
    ("small account warning: portfolio equity", "小账户提示：净值"),
    ("small account warning", "小账户提示"),
    ("is below the recommended", "低于建议"),
    ("is below recommended", "低于建议"),
    ("snapshot_as_of=", "快照日期="),
    ("snapshot=", "快照日期="),
    ("allowed=", "允许日期="),
    ("<unknown>", "未知"),
    ("<none>", "无"),
    ("RISK-ON", "风险开启"),
    ("DE-LEVER", "降杠杆"),
    ("regime=hard_defense", "市场阶段=强防御"),
    ("regime=soft_defense", "市场阶段=软防御"),
    ("regime=risk_on", "市场阶段=进攻"),
    ("benchmark_trend=down", "基准趋势=向下"),
    ("benchmark_trend=up", "基准趋势=向上"),
    ("benchmark=down", "基准趋势=向下"),
    ("benchmark=up", "基准趋势=向上"),
    ("breadth=", "市场宽度="),
    ("target_stock=", "目标股票仓位="),
    ("realized_stock=", "实际股票仓位="),
    ("stock_exposure=", "股票目标仓位="),
    ("safe_haven=", "避险仓位="),
    ("selected=", "入选标的数="),
    ("top=", "前排标的="),
    ("no_selection", "无入选标的"),
    ("outside_execution_window", "当前不在执行窗口"),
    ("pending_orders_detected", "检测到未完成订单"),
    ("same_day_execution_locked", "当日执行锁已存在"),
    ("same_day_fills_detected", "检测到当日成交"),
    ("insufficient_buying_power", "购买力不足"),
    ("missing_price", "缺少报价"),
    ("no_equity", "无净值"),
    ("fail_closed", "关闭执行"),
    ("reason=", "原因="),
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


def _translator_uses_zh(translator) -> bool:
    sample = str(translator("no_trades"))
    return any("\u4e00" <= ch <= "\u9fff" for ch in sample)


def _localize_notification_text(text: str, *, translator) -> str:
    value = str(text or "").strip()
    if not value or not _translator_uses_zh(translator):
        return value
    localized = value
    for source, target in _ZH_REASON_REPLACEMENTS:
        localized = localized.replace(source, target)
    return localized


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
    localized_status_desc = _localize_notification_text(status_desc, translator=translator)
    localized_signal_desc = _localize_notification_text(signal_desc, translator=translator)
    return (
        f"{translator('equity')}: ${equity:,.2f} | {translator('buying_power')}: ${buying_power:,.2f}\n"
        f"{separator}\n"
        f"{position_text}\n"
        f"{separator}\n"
        f"{diagnostics_text}\n"
        f"{separator}\n"
        f"{status_icon} {localized_status_desc}\n"
        f"🎯 {localized_signal_desc}\n"
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
                no_op_text = f"{no_op_text} | {_localize_notification_text(f'decision={decision}', translator=translator)}"
            if no_op_reason:
                no_op_text = f"{no_op_text} | {_localize_notification_text(f'reason={no_op_reason}', translator=translator)}"
            if fail_reason:
                no_op_text = f"{no_op_text} | {_localize_notification_text(f'fail_reason={fail_reason}', translator=translator)}"
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
