"""Order execution helpers for IBKRQuant."""

from __future__ import annotations

import time


def get_market_prices(ib, symbols, *, fetch_quote_snapshots):
    """Fetch market prices for multiple symbols in one pass."""
    quotes = fetch_quote_snapshots(ib, symbols)
    return {symbol: quote.last_price for symbol, quote in quotes.items()}


def check_order_submitted(report, *, translator):
    """Check if order was accepted. DAY orders auto-expire at close if not filled."""
    order_id = report.broker_order_id
    status = report.status

    if status in ["Submitted", "PreSubmitted", "Filled"]:
        return True, f"✅ {translator('submitted', order_id=order_id)}"
    return False, f"❌ {translator('failed', reason=status)}"


def get_available_buying_power(ib, fallback_buying_power):
    buying_power = fallback_buying_power
    for account_value in ib.accountValues():
        if account_value.tag == "AvailableFunds" and account_value.currency == "USD":
            buying_power = float(account_value.value)
    return buying_power


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
    ranking_pool,
    safe_haven,
    cash_reserve_ratio,
    rebalance_threshold_ratio,
    limit_buy_premium,
    sell_settle_delay_sec,
):
    """Execute trades to reach target weights."""
    equity = account_values.get("equity", 0)
    if equity <= 0:
        return ["❌ No equity"]

    reserved = equity * cash_reserve_ratio
    investable = equity - reserved
    threshold = equity * rebalance_threshold_ratio

    all_symbols = set(target_weights.keys()) | set(positions.keys())
    strategy_symbols = set(ranking_pool + [safe_haven])
    all_symbols = all_symbols & strategy_symbols

    prices = get_market_prices(ib, all_symbols, fetch_quote_snapshots=fetch_quote_snapshots)

    current_mv = {}
    for symbol in all_symbols:
        qty = positions.get(symbol, {}).get("quantity", 0)
        price = prices.get(symbol, 0)
        current_mv[symbol] = qty * price

    target_mv = {symbol: investable * weight for symbol, weight in target_weights.items()}
    trade_logs = []

    sell_executed = False
    for symbol in all_symbols:
        current = current_mv.get(symbol, 0)
        target = target_mv.get(symbol, 0)
        if current > target + threshold:
            sell_value = current - target
            price = prices.get(symbol)
            if not price:
                continue
            qty = int(sell_value / price)
            if qty <= 0:
                continue

            report = submit_order_intent(
                ib,
                order_intent_cls(symbol=symbol, side="sell", quantity=qty),
            )
            ok, status_msg = check_order_submitted(report, translator=translator)
            trade_logs.append(translator("market_sell", symbol=symbol, qty=qty) + f" {status_msg}")
            if ok:
                sell_executed = True

    if sell_executed:
        time.sleep(sell_settle_delay_sec)

    buying_power = get_available_buying_power(
        ib,
        account_values.get("buying_power", 0),
    )

    for symbol, target in target_mv.items():
        current = current_mv.get(symbol, 0)
        if current < target - threshold:
            buy_value = min(target - current, buying_power * 0.95)
            price = prices.get(symbol)
            if not price or buy_value < 50:
                continue

            limit_price = round(price * limit_buy_premium, 2)
            qty = int(buy_value / limit_price)
            if qty <= 0:
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
            trade_logs.append(
                translator("limit_buy", symbol=symbol, qty=qty, price=f"{limit_price:.2f}") + f" {status_msg}"
            )
            if ok:
                buying_power -= qty * limit_price

    return trade_logs
