"""Application orchestration for InteractiveBrokersPlatform."""

from __future__ import annotations

import json

from application.reconciliation_service import (
    build_reconciliation_record,
    write_reconciliation_record,
)


def build_dashboard(
    positions,
    account_values,
    signal_desc,
    status_desc,
    *,
    strategy_profile=None,
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
    position_text = "\n".join(position_lines) if position_lines else "  (空仓)"
    signal_metadata = signal_metadata or {}
    target_lines = []
    if target_weights:
        for symbol, weight in sorted(target_weights.items(), key=lambda item: (-item[1], item[0])):
            target_lines.append(f"  {symbol}: {weight:.1%}")
    target_text = "\n".join(target_lines) if target_lines else "  (无目标持仓)"
    profile_line = f"strategy_profile={strategy_profile}" if strategy_profile else "strategy_profile=<unknown>"
    regime = signal_metadata.get("regime")
    breadth_ratio = signal_metadata.get("breadth_ratio")
    target_stock_weight = signal_metadata.get("target_stock_weight")
    realized_stock_weight = signal_metadata.get("realized_stock_weight")
    safe_haven_weight = signal_metadata.get("safe_haven_weight")
    config_source = signal_metadata.get("strategy_config_source")
    snapshot_as_of = signal_metadata.get("snapshot_as_of")
    snapshot_path = signal_metadata.get("feature_snapshot_path") or signal_metadata.get("snapshot_path")
    snapshot_age_days = signal_metadata.get("snapshot_age_days")
    snapshot_file_timestamp = signal_metadata.get("snapshot_file_timestamp")
    snapshot_decision = signal_metadata.get("snapshot_guard_decision")
    diagnostics = [
        profile_line,
        f"regime={regime}" if regime else None,
        f"breadth={breadth_ratio:.1%}" if isinstance(breadth_ratio, (int, float)) else None,
        f"risk_target={target_stock_weight:.1%}" if isinstance(target_stock_weight, (int, float)) else None,
        f"realized_stock={realized_stock_weight:.1%}" if isinstance(realized_stock_weight, (int, float)) else None,
        f"safe_haven_target={safe_haven_weight:.1%}" if isinstance(safe_haven_weight, (int, float)) else None,
        f"snapshot_decision={snapshot_decision}" if snapshot_decision else None,
        f"snapshot_as_of={snapshot_as_of}" if snapshot_as_of else None,
        f"snapshot_age_days={snapshot_age_days}" if isinstance(snapshot_age_days, (int, float)) else None,
        f"snapshot_file_ts={snapshot_file_timestamp}" if snapshot_file_timestamp else None,
        f"snapshot_path={snapshot_path}" if snapshot_path else None,
        f"config_source={config_source}" if config_source else None,
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
        f"Target Weights:\n{target_text}"
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
    reconciliation_output_path=None,
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

        dashboard = build_dashboard(
            positions,
            account_values,
            signal_desc,
            status_desc,
            strategy_profile=signal_metadata.get("strategy_profile"),
            target_weights=target_weights,
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
            return "OK - heartbeat"

        execution_result = execute_rebalance(
            ib,
            target_weights,
            positions,
            account_values,
            strategy_symbols=signal_metadata.get("managed_symbols"),
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
            target_weights=target_weights,
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
            trade_lines = "\n".join(trade_logs)
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
        return "OK - executed"
    finally:
        if ib is not None and ib.isConnected():
            ib.disconnect()
