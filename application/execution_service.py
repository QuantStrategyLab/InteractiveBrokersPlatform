"""Order execution helpers for InteractiveBrokersPlatform."""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


def get_market_prices(
    ib,
    symbols,
    *,
    fetch_quote_snapshots,
):
    """Fetch market prices for multiple symbols in one pass."""
    quotes = fetch_quote_snapshots(ib, symbols)
    return {symbol: quote.last_price for symbol, quote in quotes.items()}


def check_order_submitted(report, *, translator):
    """Check if order was accepted. DAY orders auto-expire at close if not filled."""
    order_id = report.broker_order_id
    status = report.status

    if status == "Filled":
        return (
            True,
            translator(
                "order_filled",
                symbol=report.symbol,
                side=report.side,
                qty=int(report.filled_quantity or report.quantity),
                price=f"{float(report.average_fill_price or 0.0):.2f}",
                order_id=order_id,
            ),
        )
    if status in {"PartiallyFilled", "Partial"}:
        return (
            True,
            translator(
                "order_partial",
                symbol=report.symbol,
                side=report.side,
                executed=int(report.filled_quantity or 0),
                qty=int(report.quantity or 0),
                price=f"{float(report.average_fill_price or 0.0):.2f}",
                order_id=order_id,
            ),
        )
    if status in ["Submitted", "PreSubmitted"]:
        return True, f"✅ {translator('submitted', order_id=order_id)}"
    return False, f"❌ {translator('failed', reason=status)}"


def get_available_buying_power(ib, fallback_buying_power):
    buying_power = fallback_buying_power
    for account_value in ib.accountValues():
        if account_value.tag == "AvailableFunds" and account_value.currency == "USD":
            buying_power = float(account_value.value)
    return buying_power


def _iter_open_orders(ib) -> list[Any]:
    open_trades = getattr(ib, "openTrades", None)
    if callable(open_trades):
        return list(open_trades() or [])
    open_orders = getattr(ib, "openOrders", None)
    if callable(open_orders):
        return list(open_orders() or [])
    return []


def _extract_open_order_symbol(order_like: Any) -> str | None:
    contract = getattr(order_like, "contract", None)
    if contract is None and hasattr(order_like, "order"):
        contract = getattr(order_like, "contract", None)
    symbol = getattr(contract, "symbol", None)
    if symbol is None and hasattr(order_like, "symbol"):
        symbol = getattr(order_like, "symbol")
    symbol_text = str(symbol or "").strip().upper()
    return symbol_text or None


def _extract_open_order_status(order_like: Any) -> str:
    order_status = getattr(order_like, "orderStatus", None)
    status = getattr(order_status, "status", None)
    if status is None:
        status = getattr(order_like, "status", None)
    return str(status or "").strip()


def _collect_pending_symbols(ib, symbols: set[str]) -> tuple[str, ...]:
    pending = []
    for order_like in _iter_open_orders(ib):
        status = _extract_open_order_status(order_like)
        if status in {"Cancelled", "ApiCancelled", "Inactive", "Filled"}:
            continue
        symbol = _extract_open_order_symbol(order_like)
        if symbol and symbol in symbols:
            pending.append(symbol)
    return tuple(sorted(dict.fromkeys(pending)))


def _iter_fills(ib) -> list[Any]:
    fills = getattr(ib, "fills", None)
    if callable(fills):
        return list(fills() or [])
    return []


def _extract_fill_symbol(fill_like: Any) -> str | None:
    contract = getattr(fill_like, "contract", None)
    symbol = getattr(contract, "symbol", None)
    symbol_text = str(symbol or "").strip().upper()
    return symbol_text or None


def _normalize_date_like(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    ts = pd.Timestamp(value)
    if getattr(ts, "tzinfo", None) is not None:
        ts = ts.tz_convert(None)
    else:
        ts = ts.tz_localize(None)
    return ts.normalize().date().isoformat()


def _extract_fill_date(fill_like: Any) -> str | None:
    execution = getattr(fill_like, "execution", None)
    for candidate in (
        getattr(execution, "time", None),
        getattr(fill_like, "time", None),
    ):
        normalized = _normalize_date_like(candidate)
        if normalized is not None:
            return normalized
    return None


def _collect_same_day_filled_symbols(ib, symbols: set[str], trade_date: str | None) -> tuple[str, ...]:
    if not trade_date:
        return ()
    matched = []
    for fill_like in _iter_fills(ib):
        symbol = _extract_fill_symbol(fill_like)
        if not symbol or symbol not in symbols:
            continue
        fill_date = _extract_fill_date(fill_like)
        if fill_date == trade_date:
            matched.append(symbol)
    return tuple(sorted(dict.fromkeys(matched)))


def _round_weight(value: float) -> float:
    return round(float(value or 0.0), 8)


def _build_target_hash(target_weights: dict[str, float]) -> str:
    payload = [[str(symbol), _round_weight(weight)] for symbol, weight in sorted(target_weights.items())]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")).hexdigest()


def _sanitize_token(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return "none"
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in text)
    return safe or "none"


def _display_text(value: Any, *, fallback: str) -> str:
    text = str(value).strip() if value is not None else ""
    return text or fallback


def _resolve_weight_allocation(signal_metadata: dict[str, Any] | None) -> dict[str, Any]:
    metadata = dict(signal_metadata or {})
    allocation = dict(metadata.get("allocation") or {})
    if not allocation:
        raise ValueError("IBKR execution requires signal_metadata.allocation")
    if allocation.get("target_mode") != "weight":
        raise ValueError("IBKR execution requires allocation.target_mode=weight")
    return {
        "strategy_symbols": tuple(str(symbol).strip().upper() for symbol in allocation.get("strategy_symbols", ())),
        "risk_symbols": tuple(str(symbol).strip().upper() for symbol in allocation.get("risk_symbols", ())),
        "income_symbols": tuple(str(symbol).strip().upper() for symbol in allocation.get("income_symbols", ())),
        "safe_haven_symbols": tuple(
            str(symbol).strip().upper() for symbol in allocation.get("safe_haven_symbols", ())
        ),
        "targets": {
            str(symbol).strip().upper(): float(weight)
            for symbol, weight in dict(allocation.get("targets") or {}).items()
        },
    }


def _apply_snapshot_price_fallbacks(
    prices: dict[str, float],
    symbols,
    *,
    dry_run_only: bool,
    snapshot_price_fallbacks: dict[str, float] | None,
) -> tuple[dict[str, float], tuple[str, ...]]:
    if not dry_run_only or not snapshot_price_fallbacks:
        return dict(prices), ()
    resolved = dict(prices)
    fallback_symbols: list[str] = []
    for symbol in symbols:
        normalized = str(symbol).strip().upper()
        if normalized in resolved:
            continue
        fallback_price = snapshot_price_fallbacks.get(normalized)
        if fallback_price and float(fallback_price) > 0:
            resolved[normalized] = float(fallback_price)
            fallback_symbols.append(normalized)
    return resolved, tuple(fallback_symbols)


def _format_symbol_preview(symbols: tuple[str, ...], *, limit: int = 3) -> str:
    if not symbols:
        return ""
    shown = [str(symbol).strip().upper() for symbol in symbols[:limit]]
    remaining = len(symbols) - len(shown)
    if remaining > 0:
        shown.append(f"+{remaining}")
    return ",".join(shown)


def _resolve_execution_lock_path(
    *,
    strategy_profile: str | None,
    account_group: str | None,
    service_name: str | None,
    trade_date: str | None,
    snapshot_date: str | None,
    dry_run_only: bool,
    execution_lock_dir: str | Path | None,
) -> Path:
    lock_dir = Path(execution_lock_dir) if execution_lock_dir else Path(tempfile.gettempdir()) / "ibkr_execution_locks"
    mode = "dry_run" if dry_run_only else "paper"
    scope = "__".join(
        [
            _sanitize_token(account_group or "default"),
            _sanitize_token(service_name or "service"),
            _sanitize_token(strategy_profile or "unknown"),
            _sanitize_token(mode),
            _sanitize_token(trade_date),
            _sanitize_token(snapshot_date or "no_snapshot"),
        ]
    )
    return lock_dir / f"{scope}.json"


def _read_execution_lock(lock_path: Path) -> dict[str, Any] | None:
    if not lock_path.exists():
        return None
    return json.loads(lock_path.read_text(encoding="utf-8"))


def _try_create_execution_lock(lock_path: Path, payload: dict[str, Any]) -> bool:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with lock_path.open("x", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        return True
    except FileExistsError:
        return False


def _build_execution_lock_payload(
    *,
    strategy_profile: str | None,
    account_group: str | None,
    service_name: str | None,
    account_ids: tuple[str, ...] | list[str] | None,
    trade_date: str | None,
    snapshot_date: str | None,
    target_hash: str,
    dry_run_only: bool,
) -> dict[str, Any]:
    return {
        "strategy_profile": strategy_profile,
        "account_group": account_group,
        "service_name": service_name,
        "account_ids": list(account_ids or ()),
        "trade_date": trade_date,
        "snapshot_date": snapshot_date,
        "mode": "dry_run" if dry_run_only else "paper",
        "target_hash": target_hash,
        "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }


def _format_target_lines(
    target_weights: dict[str, float],
    current_mv: dict[str, float],
    equity: float,
    *,
    translator,
) -> list[str]:
    current_weight = {
        symbol: (current_mv.get(symbol, 0.0) / equity if equity > 0 else 0.0)
        for symbol in set(target_weights) | set(current_mv)
    }
    target_lines = []
    for symbol, target_weight in sorted(target_weights.items(), key=lambda item: (-item[1], item[0])):
        delta = target_weight - current_weight.get(symbol, 0.0)
        target_lines.append(
            translator(
                "target_diff",
                symbol=symbol,
                current=f"{current_weight.get(symbol, 0.0):.1%}",
                target=f"{target_weight:.1%}",
                delta=f"{delta:.1%}",
            )
        )
    return target_lines


def _build_target_diff_rows(
    target_weights: dict[str, float],
    current_mv: dict[str, float],
    equity: float,
) -> list[dict[str, float | str]]:
    current_weight = {
        symbol: (current_mv.get(symbol, 0.0) / equity if equity > 0 else 0.0)
        for symbol in set(target_weights) | set(current_mv)
    }
    rows = []
    for symbol, target_weight in sorted(target_weights.items(), key=lambda item: (-item[1], item[0])):
        current_value = current_weight.get(symbol, 0.0)
        rows.append(
            {
                "symbol": symbol,
                "current_weight": current_value,
                "target_weight": float(target_weight),
                "delta_weight": float(target_weight - current_value),
            }
        )
    return rows


def _finalize_result(trade_logs, execution_summary, *, return_summary: bool):
    if return_summary:
        return trade_logs, execution_summary
    return trade_logs


def execute_rebalance(
    ib,
    target_weights,
    positions,
    account_values,
    *,
    fetch_quote_snapshots,
    submit_order_intent,
    order_intent_cls,
    translator,
    strategy_symbols=None,
    signal_metadata=None,
    strategy_profile=None,
    account_group=None,
    service_name=None,
    account_ids=None,
    dry_run_only=False,
    cash_reserve_ratio,
    rebalance_threshold_ratio,
    limit_buy_premium,
    sell_settle_delay_sec,
    execution_lock_dir=None,
    return_summary=False,
):
    """Execute trades to reach target weights."""
    del target_weights
    signal_metadata = signal_metadata or {}
    allocation = _resolve_weight_allocation(signal_metadata)
    target_weights = dict(allocation["targets"])
    strategy_symbols = tuple(allocation["strategy_symbols"])
    trade_date = str(signal_metadata.get("trade_date") or "").strip() or None
    snapshot_date = _normalize_date_like(signal_metadata.get("snapshot_as_of"))
    safe_haven_symbols = tuple(allocation["safe_haven_symbols"])
    safe_haven_symbol = safe_haven_symbols[0] if safe_haven_symbols else None
    equity = account_values.get("equity", 0)
    execution_summary = {
        "mode": "dry_run" if dry_run_only else "paper",
        "strategy_profile": strategy_profile,
        "trade_date": trade_date,
        "snapshot_as_of": snapshot_date,
        "safe_haven_symbol": safe_haven_symbol,
        "target_stock_weight": signal_metadata.get("target_stock_weight"),
        "realized_stock_weight": signal_metadata.get("realized_stock_weight"),
        "target_safe_haven_weight": signal_metadata.get("safe_haven_weight"),
        "realized_safe_haven_weight": signal_metadata.get("safe_haven_weight"),
        "orders_submitted": [],
        "orders_filled": [],
        "orders_partially_filled": [],
        "orders_skipped": [],
        "skipped_reasons": [],
        "target_vs_current": [],
        "execution_status": "not_started",
        "no_op_reason": None,
        "cash_reserve_dollars": 0.0,
        "residual_cash_estimate": float(account_values.get("buying_power", 0.0) or 0.0),
        "current_stock_weight": 0.0,
        "current_safe_haven_weight": 0.0,
        "price_source_mode": "market_quote",
        "snapshot_price_fallback_used": False,
        "snapshot_price_fallback_symbols": [],
        "snapshot_price_fallback_count": 0,
        "lock_path": None,
    }
    if equity <= 0:
        execution_summary["execution_status"] = "blocked"
        execution_summary["no_op_reason"] = "no_equity"
        return _finalize_result([translator("no_equity")], execution_summary, return_summary=return_summary)

    reserved = equity * cash_reserve_ratio
    investable = equity - reserved
    threshold = equity * rebalance_threshold_ratio
    execution_summary["cash_reserve_dollars"] = float(reserved)

    all_symbols = set(target_weights.keys()) | set(positions.keys())
    if strategy_symbols:
        all_symbols = all_symbols & set(strategy_symbols)

    snapshot_price_fallbacks = {
        str(symbol).strip().upper(): float(price)
        for symbol, price in dict(signal_metadata.get("dry_run_price_fallbacks") or {}).items()
        if price is not None
    }
    prices = get_market_prices(
        ib,
        all_symbols,
        fetch_quote_snapshots=fetch_quote_snapshots,
    )
    prices, snapshot_price_fallback_symbols = _apply_snapshot_price_fallbacks(
        prices,
        all_symbols,
        dry_run_only=dry_run_only,
        snapshot_price_fallbacks=snapshot_price_fallbacks,
    )
    execution_summary["snapshot_price_fallback_used"] = bool(snapshot_price_fallback_symbols)
    execution_summary["snapshot_price_fallback_symbols"] = list(snapshot_price_fallback_symbols)
    execution_summary["snapshot_price_fallback_count"] = len(snapshot_price_fallback_symbols)
    if snapshot_price_fallback_symbols:
        execution_summary["price_source_mode"] = "mixed_market_quote_snapshot_close"

    current_mv = {}
    for symbol in all_symbols:
        qty = positions.get(symbol, {}).get("quantity", 0)
        price = prices.get(symbol, 0)
        current_mv[symbol] = qty * price

    target_mv = {symbol: investable * weight for symbol, weight in target_weights.items()}
    trade_logs = []
    target_hash = _build_target_hash(target_weights)
    execution_summary["target_vs_current"] = _build_target_diff_rows(target_weights, current_mv, equity)
    if equity > 0:
        current_safe_haven_mv = current_mv.get(safe_haven_symbol, 0.0) if safe_haven_symbol else 0.0
        execution_summary["current_safe_haven_weight"] = float(current_safe_haven_mv / equity)
        execution_summary["current_stock_weight"] = float(
            (sum(current_mv.values()) - current_safe_haven_mv) / equity
        )

    pending_symbols = _collect_pending_symbols(ib, set(all_symbols))
    if pending_symbols:
        reason = f"pending_orders_detected:{','.join(pending_symbols)}"
        execution_summary["execution_status"] = "blocked"
        execution_summary["no_op_reason"] = reason
        execution_summary["skipped_reasons"].append(reason)
        trade_logs.append(
            translator(
                "pending_orders_detected",
                profile=_display_text(strategy_profile, fallback="<unknown>"),
                symbols=",".join(pending_symbols),
            )
        )
        return _finalize_result(trade_logs, execution_summary, return_summary=return_summary)

    trade_logs.append(
        " | ".join(
            [
                translator(
                    "execution_profile_detail",
                    profile=_display_text(strategy_profile, fallback="<unknown>"),
                ),
                translator(
                    "regime_detail",
                    value=_display_text(signal_metadata.get("regime"), fallback="<none>"),
                ),
                translator(
                    "breadth_detail",
                    value=f"{float(signal_metadata.get('breadth_ratio', 0.0) or 0.0):.1%}",
                ),
                translator(
                    "target_stock_detail",
                    value=f"{float(signal_metadata.get('target_stock_weight', 0.0) or 0.0):.1%}",
                ),
                translator(
                    "realized_stock_detail",
                    value=f"{float(signal_metadata.get('realized_stock_weight', 0.0) or 0.0):.1%}",
                ),
                translator(
                    "snapshot_as_of_detail",
                    value=_display_text(snapshot_date, fallback="<none>"),
                ),
                translator(
                    "trade_date_detail",
                    value=_display_text(trade_date, fallback="<none>"),
                ),
            ]
        )
    )
    if snapshot_price_fallback_symbols:
        trade_logs.append(
            translator(
                "dry_run_snapshot_prices",
                count=len(snapshot_price_fallback_symbols),
                symbols=_format_symbol_preview(snapshot_price_fallback_symbols),
            )
        )
    trade_logs.extend(_format_target_lines(target_weights, current_mv, equity, translator=translator))

    missing_price_symbols: list[str] = []
    insufficient_buying_power_symbols: list[str] = []
    min_notional_symbols: list[str] = []
    quantity_zero_symbols: list[str] = []

    has_sell_plan = False
    for symbol in all_symbols:
        current = current_mv.get(symbol, 0.0)
        target = target_mv.get(symbol, 0.0)
        if current <= target + threshold:
            continue
        price = prices.get(symbol)
        if not price:
            missing_price_symbols.append(symbol)
            continue
        if int((current - target) / price) > 0:
            has_sell_plan = True
            break
        quantity_zero_symbols.append(symbol)

    anticipated_buying_power = get_available_buying_power(
        ib,
        account_values.get("buying_power", 0),
    )
    has_buy_plan = False
    for symbol, target in target_mv.items():
        current = current_mv.get(symbol, 0.0)
        if current >= target - threshold:
            continue
        price = prices.get(symbol)
        if not price:
            missing_price_symbols.append(symbol)
            continue
        buy_value = min(target - current, anticipated_buying_power * 0.95)
        if buy_value <= 0:
            insufficient_buying_power_symbols.append(symbol)
            continue
        if buy_value < 50:
            min_notional_symbols.append(symbol)
            continue
        limit_price = round(price * limit_buy_premium, 2)
        qty = int(buy_value / limit_price) if limit_price > 0 else 0
        if qty > 0:
            has_buy_plan = True
            break
        quantity_zero_symbols.append(symbol)

    if not has_sell_plan and not has_buy_plan:
        reason = "target_diff_below_threshold"
        status = "no_op"
        if missing_price_symbols:
            symbols = ",".join(sorted(dict.fromkeys(missing_price_symbols)))
            reason = f"missing_price:{symbols}"
            status = "blocked"
            execution_summary["orders_skipped"].extend(
                {"symbol": symbol, "reason": "missing_price"}
                for symbol in sorted(dict.fromkeys(missing_price_symbols))
            )
        elif insufficient_buying_power_symbols:
            symbols = ",".join(sorted(dict.fromkeys(insufficient_buying_power_symbols)))
            reason = f"insufficient_buying_power:{symbols}"
            status = "blocked"
        elif min_notional_symbols:
            symbols = ",".join(sorted(dict.fromkeys(min_notional_symbols)))
            reason = f"min_notional:{symbols}"
        elif quantity_zero_symbols:
            symbols = ",".join(sorted(dict.fromkeys(quantity_zero_symbols)))
            reason = f"quantity_zero:{symbols}"

        execution_summary["execution_status"] = status
        execution_summary["no_op_reason"] = reason
        if reason != "target_diff_below_threshold":
            execution_summary["skipped_reasons"].append(reason)
            if status == "blocked":
                trade_logs.append(translator("failed", reason=reason))
        return _finalize_result(trade_logs, execution_summary, return_summary=return_summary)

    same_day_filled_symbols = _collect_same_day_filled_symbols(ib, set(all_symbols), trade_date)
    if same_day_filled_symbols:
        reason = f"same_day_fills_detected:{','.join(same_day_filled_symbols)}"
        execution_summary["execution_status"] = "blocked"
        execution_summary["no_op_reason"] = reason
        execution_summary["skipped_reasons"].append(reason)
        trade_logs.append(
            translator(
                "same_day_fills_detected",
                profile=_display_text(strategy_profile, fallback="<unknown>"),
                mode="dry_run" if dry_run_only else "paper",
                symbols=",".join(same_day_filled_symbols),
                trade_date=_display_text(trade_date, fallback="<none>"),
            )
        )
        return _finalize_result(trade_logs, execution_summary, return_summary=return_summary)

    lock_path = _resolve_execution_lock_path(
        strategy_profile=strategy_profile,
        account_group=account_group,
        service_name=service_name,
        trade_date=trade_date,
        snapshot_date=snapshot_date,
        dry_run_only=dry_run_only,
        execution_lock_dir=execution_lock_dir,
    )
    lock_payload = _build_execution_lock_payload(
        strategy_profile=strategy_profile,
        account_group=account_group,
        service_name=service_name,
        account_ids=tuple(account_ids or ()),
        trade_date=trade_date,
        snapshot_date=snapshot_date,
        target_hash=target_hash,
        dry_run_only=dry_run_only,
    )
    if not _try_create_execution_lock(lock_path, lock_payload):
        existing = _read_execution_lock(lock_path) or {}
        reason = (
            f"same_day_execution_locked:mode={'dry_run' if dry_run_only else 'paper'}:"
            f"target_hash={existing.get('target_hash', '<unknown>')}"
        )
        execution_summary["execution_status"] = "blocked"
        execution_summary["no_op_reason"] = reason
        execution_summary["skipped_reasons"].append(reason)
        execution_summary["lock_path"] = str(lock_path)
        trade_logs.append(
            translator(
                "same_day_execution_locked",
                profile=_display_text(strategy_profile, fallback="<unknown>"),
                mode="dry_run" if dry_run_only else "paper",
                trade_date=_display_text(trade_date, fallback="<none>"),
                snapshot_date=_display_text(snapshot_date, fallback="<none>"),
                target_hash=_display_text(existing.get("target_hash"), fallback="<unknown>"),
                lock_path=str(lock_path),
            )
        )
        return _finalize_result(trade_logs, execution_summary, return_summary=return_summary)
    execution_summary["lock_path"] = str(lock_path)
    trade_logs.append(
        translator(
            "execution_lock_acquired",
            mode="dry_run" if dry_run_only else "paper",
            trade_date=_display_text(trade_date, fallback="<none>"),
            snapshot_date=_display_text(snapshot_date, fallback="<none>"),
            lock_path=str(lock_path),
        )
    )
    execution_summary["execution_status"] = "executing"

    sell_executed = False
    for symbol in all_symbols:
        current = current_mv.get(symbol, 0)
        target = target_mv.get(symbol, 0)
        if current > target + threshold:
            sell_value = current - target
            price = prices.get(symbol)
            if not price:
                execution_summary["orders_skipped"].append({"symbol": symbol, "side": "sell", "reason": "missing_price"})
                execution_summary["skipped_reasons"].append(f"missing_price:{symbol}")
                continue
            qty = int(sell_value / price)
            if qty <= 0:
                execution_summary["orders_skipped"].append({"symbol": symbol, "side": "sell", "reason": "quantity_zero"})
                continue

            if dry_run_only:
                execution_summary["orders_submitted"].append(
                    {"symbol": symbol, "side": "sell", "quantity": qty, "status": "dry_run"}
                )
                trade_logs.append(f"DRY_RUN sell {symbol} {qty}")
                continue
            report = submit_order_intent(
                ib,
                order_intent_cls(symbol=symbol, side="sell", quantity=qty),
            )
            ok, status_msg = check_order_submitted(report, translator=translator)
            status = str(getattr(report, "status", "") or "")
            order_payload = {
                "symbol": symbol,
                "side": "sell",
                "quantity": qty,
                "status": status,
                "broker_order_id": getattr(report, "broker_order_id", None),
            }
            if status == "Filled":
                execution_summary["orders_filled"].append(order_payload)
            elif status in {"PartiallyFilled", "Partial"}:
                execution_summary["orders_partially_filled"].append(order_payload)
            elif ok:
                execution_summary["orders_submitted"].append(order_payload)
            else:
                execution_summary["orders_skipped"].append({**order_payload, "reason": status or "submit_failed"})
                execution_summary["skipped_reasons"].append(f"submit_failed:{symbol}:{status or 'unknown'}")
            trade_logs.append(translator("market_sell", symbol=symbol, qty=qty) + f" {status_msg}")
            if ok:
                sell_executed = True

    if sell_executed:
        time.sleep(sell_settle_delay_sec)

    buying_power = anticipated_buying_power if not sell_executed else get_available_buying_power(
        ib,
        account_values.get("buying_power", 0),
    )

    for symbol, target in target_mv.items():
        current = current_mv.get(symbol, 0)
        if current < target - threshold:
            buy_value = min(target - current, buying_power * 0.95)
            price = prices.get(symbol)
            if not price:
                execution_summary["orders_skipped"].append({"symbol": symbol, "side": "buy", "reason": "missing_price"})
                execution_summary["skipped_reasons"].append(f"missing_price:{symbol}")
                continue
            if buy_value < 50:
                execution_summary["orders_skipped"].append({"symbol": symbol, "side": "buy", "reason": "min_notional"})
                continue

            limit_price = round(price * limit_buy_premium, 2)
            qty = int(buy_value / limit_price)
            if qty <= 0:
                execution_summary["orders_skipped"].append({"symbol": symbol, "side": "buy", "reason": "quantity_zero"})
                continue

            if dry_run_only:
                execution_summary["orders_submitted"].append(
                    {
                        "symbol": symbol,
                        "side": "buy",
                        "quantity": qty,
                        "limit_price": limit_price,
                        "status": "dry_run",
                    }
                )
                trade_logs.append(f"DRY_RUN buy {symbol} {qty} @{limit_price:.2f}")
                buying_power -= qty * limit_price
                continue
            report = submit_order_intent(
                ib,
                order_intent_cls(
                    symbol=symbol,
                    side="buy",
                    quantity=qty,
                    order_type="limit",
                    limit_price=limit_price,
                    time_in_force="DAY",
                ),
            )
            ok, status_msg = check_order_submitted(report, translator=translator)
            status = str(getattr(report, "status", "") or "")
            order_payload = {
                "symbol": symbol,
                "side": "buy",
                "quantity": qty,
                "limit_price": limit_price,
                "status": status,
                "broker_order_id": getattr(report, "broker_order_id", None),
            }
            if status == "Filled":
                execution_summary["orders_filled"].append(order_payload)
            elif status in {"PartiallyFilled", "Partial"}:
                execution_summary["orders_partially_filled"].append(order_payload)
            elif ok:
                execution_summary["orders_submitted"].append(order_payload)
            else:
                execution_summary["orders_skipped"].append({**order_payload, "reason": status or "submit_failed"})
                execution_summary["skipped_reasons"].append(f"submit_failed:{symbol}:{status or 'unknown'}")
            trade_logs.append(
                translator("limit_buy", symbol=symbol, qty=qty, price=f"{limit_price:.2f}") + f" {status_msg}"
            )
            if ok:
                buying_power -= qty * limit_price

    execution_summary["execution_status"] = (
        "executed"
        if (
            execution_summary["orders_submitted"]
            or execution_summary["orders_filled"]
            or execution_summary["orders_partially_filled"]
        )
        else "no_op"
    )
    execution_summary["residual_cash_estimate"] = float(max(buying_power, 0.0))
    return _finalize_result(trade_logs, execution_summary, return_summary=return_summary)
