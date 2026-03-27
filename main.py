"""
IBKR Global ETF Rotation Strategy.
Quarterly momentum rotation across 22 global ETFs + daily canary emergency.
Runs on Cloud Run; connects to IB Gateway on GCE via ib_insync, alerts via Telegram.
"""
import asyncio
import os
import time
import traceback
import requests
import numpy as np
import pandas as pd
import pytz
import pandas_market_calendars as mcal
from datetime import datetime, timedelta
from flask import Flask, request

import google.auth
try:
    from google.cloud import compute_v1
except ImportError:
    compute_v1 = None

from ib_insync import IB, Stock, MarketOrder, LimitOrder

app = Flask(__name__)


# ---------------------------------------------------------------------------
# GCE instance resolver: find IB Gateway by instance name instead of IP
# ---------------------------------------------------------------------------
def get_project_id():
    try:
        _, project_id = google.auth.default()
        return project_id if project_id else os.getenv("GOOGLE_CLOUD_PROJECT")
    except Exception:
        return os.getenv("GOOGLE_CLOUD_PROJECT")


def get_ib_gateway_ip_mode():
    mode = os.getenv("IB_GATEWAY_IP_MODE", "internal").strip().lower()
    if mode in {"internal", "external"}:
        return mode
    print(f"Invalid IB_GATEWAY_IP_MODE={mode!r}, defaulting to internal", flush=True)
    return "internal"


def resolve_gce_instance_ip(instance_name, zone):
    """Resolve GCE instance IP by name via Compute API."""
    if not compute_v1:
        print(f"google-cloud-compute not installed, using {instance_name} as host directly", flush=True)
        return instance_name
    try:
        ip_mode = get_ib_gateway_ip_mode()
        project = get_project_id()
        client = compute_v1.InstancesClient()
        instance = client.get(project=project, zone=zone, instance=instance_name)
        internal_ip = None
        external_ip = None
        for iface in instance.network_interfaces:
            if iface.network_i_p:
                internal_ip = iface.network_i_p
            for ac in iface.access_configs:
                if ac.nat_i_p:
                    external_ip = ac.nat_i_p

        candidates = (
            (("internal", internal_ip), ("external", external_ip))
            if ip_mode == "internal"
            else (("external", external_ip), ("internal", internal_ip))
        )
        for label, ip in candidates:
            if ip:
                print(f"Resolved {instance_name} → {ip} ({label}, mode={ip_mode})", flush=True)
                return ip
    except Exception as e:
        print(f"GCE resolve failed for {instance_name}: {e}, using as hostname", flush=True)
    return instance_name


def get_ib_host():
    """
    Resolve IB Gateway host.
    - If IB_GATEWAY_ZONE is set: resolve instance name via Compute API
    - If IB_GATEWAY_ZONE is not set: use IB_GATEWAY_HOST directly
    """
    host = os.getenv("IB_GATEWAY_HOST")
    if not host:
        raise EnvironmentError("IB_GATEWAY_HOST is required")
    zone = os.getenv("IB_GATEWAY_ZONE", "")
    if zone:
        return resolve_gce_instance_ip(host, zone)
    return host


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
IB_HOST = get_ib_host()
IB_PORT = int(os.getenv("IB_GATEWAY_PORT", "4001"))
IB_CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "1"))

TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
NOTIFY_LANG = os.getenv("NOTIFY_LANG", "en")

# Strategy parameters
RANKING_POOL = [
    'EWY', 'EWT', 'INDA', 'FXI', 'EWJ', 'VGK',  # International
    'VOO', 'XLK', 'SMH',                            # US broad market, tech, semis
    'GLD', 'SLV', 'USO', 'DBA',                    # Commodities
    'XLE', 'XLF', 'ITA',                           # US cyclical sectors
    'XLP', 'XLU', 'XLV', 'IHI',                   # US defensive sectors
    'VNQ', 'KRE',                                  # Real estate, regional banks
]
CANARY_ASSETS = ['SPY', 'EFA', 'EEM', 'AGG']
SAFE_HAVEN = 'BIL'

TOP_N = 2
SMA_PERIOD = 200
HOLD_BONUS = 0.02           # Existing holdings get +2% momentum bonus
CANARY_BAD_THRESHOLD = 4    # All 4 canaries bad → emergency defensive
REBALANCE_MONTHS = {3, 6, 9, 12}  # Quarterly: last trading day of Mar, Jun, Sep, Dec

CASH_RESERVE_RATIO = 0.03
REBALANCE_THRESHOLD_RATIO = 0.02  # 2% of equity to trigger trades
LIMIT_BUY_PREMIUM = 1.005

# Execution
SELL_SETTLE_DELAY_SEC = 3

# IBKR pacing: delay between historical data requests
HIST_DATA_PACING_SEC = 0.5

SEPARATOR = "━━━━━━━━━━━━━━━━━━"

# ---------------------------------------------------------------------------
# i18n
# ---------------------------------------------------------------------------
I18N = {
    "zh": {
        "rebalance_title":  "🔔 【调仓指令】",
        "heartbeat_title":  "💓 【心跳检测】",
        "error_title":      "🚨 【策略异常】",
        "canary_title":     "🐤 【金丝雀检查】",
        "equity":           "净值",
        "buying_power":     "购买力",
        "signal_label":     "信号",
        "no_trades":        "✅ 无需调仓",
        "emergency":        "🛡️ 金丝雀应急: {n_bad}/4 坏, 全部转入 {safe}",
        "quarterly":        "📊 季度调仓: Top {n} 轮动",
        "daily_check":      "📋 每日检查: 金丝雀正常, 持仓不变",
        "hold":             "💎 持仓不变",
        "market_sell":      "📉 [市价卖出] {symbol}: {qty}股",
        "limit_buy":        "📈 [限价买入] {symbol}: {qty}股 @ ${price}",
        "submitted":        "已下发 (ID: {order_id})",
        "failed":           "失败: {reason}",
        "order_filled":     "✅ 订单成交 | {symbol} {side} {qty}股 均价 ${price} (ID: {order_id})",
        "order_partial":    "⚠️ 部分成交 | {symbol} {side} {executed}/{qty}股 均价 ${price} (ID: {order_id})",
        "order_rejected":   "❌ 订单异常 | {symbol} {side} {qty}股 状态: {status} (ID: {order_id})",
    },
    "en": {
        "rebalance_title":  "🔔 【Trade Execution Report】",
        "heartbeat_title":  "💓 【Heartbeat】",
        "error_title":      "🚨 【Strategy Error】",
        "canary_title":     "🐤 【Canary Check】",
        "equity":           "Equity",
        "buying_power":     "Buying Power",
        "signal_label":     "Signal",
        "no_trades":        "✅ No rebalance needed",
        "emergency":        "🛡️ Canary Emergency: {n_bad}/4 bad, rotating to {safe}",
        "quarterly":        "📊 Quarterly Rebalance: Top {n} rotation",
        "daily_check":      "📋 Daily Check: canary OK, holding",
        "hold":             "💎 Hold positions",
        "market_sell":      "📉 [Market sell] {symbol}: {qty} shares",
        "limit_buy":        "📈 [Limit buy] {symbol}: {qty} shares @ ${price}",
        "submitted":        "submitted (ID: {order_id})",
        "failed":           "failed: {reason}",
        "order_filled":     "✅ Filled | {symbol} {side} {qty} shares avg ${price} (ID: {order_id})",
        "order_partial":    "⚠️ Partial | {symbol} {side} {executed}/{qty} shares avg ${price} (ID: {order_id})",
        "order_rejected":   "❌ Rejected | {symbol} {side} {qty} shares status: {status} (ID: {order_id})",
    },
}


def t(key, **kwargs):
    lang = NOTIFY_LANG if NOTIFY_LANG in I18N else "en"
    template = I18N[lang].get(key, key)
    return template.format(**kwargs) if kwargs else template


def send_tg_message(message):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        print(f"TG:\n{message}", flush=True)
        response = requests.post(
            url,
            json={"chat_id": TG_CHAT_ID, "text": message},
            timeout=10,
        )
        if not 200 <= response.status_code < 300:
            print(
                f"Telegram send failed with status {response.status_code}: {response.text}",
                flush=True,
            )
    except Exception as e:
        print(f"Telegram send failed: {e}", flush=True)


# ---------------------------------------------------------------------------
# IB Gateway connection
# ---------------------------------------------------------------------------
def ensure_event_loop():
    """ib_insync expects an event loop even inside Gunicorn worker threads."""
    try:
        loop = asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop

    if loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    return loop


def connect_ib():
    ensure_event_loop()
    ib = IB()
    ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID, timeout=20)
    return ib


def get_historical_close(ib, symbol, duration="2 Y", bar_size="1 day"):
    """Fetch daily close prices from IBKR for a US stock/ETF."""
    contract = Stock(symbol, 'SMART', 'USD')
    ib.qualifyContracts(contract)  # Fix I4: qualify before requesting data
    bars = ib.reqHistoricalData(
        contract,
        endDateTime='',
        durationStr=duration,
        barSizeSetting=bar_size,
        whatToShow='ADJUSTED_LAST',
        useRTH=True,
        formatDate=1,
    )
    if not bars:
        return pd.Series(dtype=float)
    df = pd.DataFrame(bars)
    df['date'] = pd.to_datetime(df['date'])
    df = df.set_index('date')
    return df['close']


# ---------------------------------------------------------------------------
# Strategy logic
# ---------------------------------------------------------------------------
def compute_13612w_momentum(closes, as_of_date=None):
    """Compute Keller's 13612W momentum score from monthly close prices."""
    if len(closes) < 253:  # ~12 months of daily data
        return float('nan')

    me = closes.resample('ME').last().dropna()
    if as_of_date is not None:
        me = me[me.index <= as_of_date]

    if len(me) < 13:
        return float('nan')

    cur = me.iloc[-1]
    lookbacks = {1: 12, 3: 4, 6: 2, 12: 1}
    s = 0.0
    for months, weight in lookbacks.items():
        if len(me) < months + 1:
            return float('nan')
        prior = me.iloc[-(months + 1)]
        if prior == 0 or pd.isna(prior):
            return float('nan')
        s += weight * (cur / prior - 1)
    return s / 19


def check_sma(closes, period=SMA_PERIOD):
    """Return True if last close > SMA."""
    if len(closes) < period:
        return False
    return bool(closes.iloc[-1] > closes.iloc[-period:].mean())


def compute_signals(ib, current_holdings):
    """
    Compute target weights.
    Returns (weights_dict, signal_description, is_emergency, canary_str).
    """
    # Fetch all price data with pacing (Fix C3)
    all_tickers = list(set(RANKING_POOL + CANARY_ASSETS + [SAFE_HAVEN]))
    price_data = {}
    for ticker in all_tickers:
        try:
            closes = get_historical_close(ib, ticker)
            if len(closes) > 0:
                price_data[ticker] = closes
        except Exception as e:
            print(f"Warning: failed to fetch {ticker}: {e}", flush=True)
        time.sleep(HIST_DATA_PACING_SEC)  # Fix C3: IBKR pacing between requests

    # --- Step 1: Daily canary check ---
    n_bad = 0
    canary_details = []
    for c in CANARY_ASSETS:
        if c not in price_data:
            n_bad += 1
            canary_details.append(f"{c}:❌(no data)")
            continue
        mom = compute_13612w_momentum(price_data[c])
        if np.isnan(mom) or mom < 0:
            n_bad += 1
            canary_details.append(f"{c}:❌({mom:.3f})" if not np.isnan(mom) else f"{c}:❌(nan)")
        else:
            canary_details.append(f"{c}:✅({mom:.3f})")

    canary_str = ', '.join(canary_details)

    if n_bad >= CANARY_BAD_THRESHOLD:
        # Emergency: all defensive
        signal_desc = t("emergency", n_bad=n_bad, safe=SAFE_HAVEN)
        return {SAFE_HAVEN: 1.0}, signal_desc, True, canary_str

    # --- Step 2: Check if this is a quarterly rebalance day ---
    tz_ny = pytz.timezone('America/New_York')
    now_ny = datetime.now(tz_ny)
    nyse = mcal.get_calendar('NYSE')

    is_rebal_day = False
    if now_ny.month in REBALANCE_MONTHS:
        month_start = now_ny.replace(day=1)
        month_end = (month_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        schedule = nyse.schedule(start_date=month_start, end_date=month_end)
        if not schedule.empty:
            last_trading_day = schedule.index[-1].date()
            is_rebal_day = (now_ny.date() == last_trading_day)

    if not is_rebal_day:
        signal_desc = t("daily_check")
        return None, signal_desc, False, canary_str

    # --- Step 3: Quarterly rebalance ---
    scores = {}
    for ticker in RANKING_POOL:
        if ticker not in price_data:
            continue
        mom = compute_13612w_momentum(price_data[ticker])
        if np.isnan(mom):
            continue
        if not check_sma(price_data[ticker]):
            continue
        bonus = HOLD_BONUS if ticker in current_holdings else 0.0
        scores[ticker] = mom + bonus

    sorted_tickers = sorted(scores.items(), key=lambda x: -x[1])
    top = sorted_tickers[:TOP_N]

    if len(top) == 0:
        signal_desc = t("emergency", n_bad="SMA", safe=SAFE_HAVEN)
        return {SAFE_HAVEN: 1.0}, signal_desc, False, canary_str

    per = 1.0 / TOP_N
    weights = {ticker: per for ticker, _ in top}
    if len(top) < TOP_N:
        weights[SAFE_HAVEN] = weights.get(SAFE_HAVEN, 0) + per * (TOP_N - len(top))

    top_str = ', '.join(f"{ticker}({score:.3f})" for ticker, score in top)
    signal_desc = t("quarterly", n=TOP_N) + f"\n  Top: {top_str}"

    return weights, signal_desc, False, canary_str


# ---------------------------------------------------------------------------
# Portfolio execution
# ---------------------------------------------------------------------------
def get_current_portfolio(ib):
    """Get current positions and account values."""
    ib.reqPositions()
    time.sleep(1)

    positions = {}
    for pos in ib.positions():
        symbol = pos.contract.symbol
        if pos.position != 0:
            positions[symbol] = {
                'quantity': int(pos.position),
                'avg_cost': float(pos.avgCost),
            }

    account_values = {}
    for av in ib.accountValues():
        if av.tag == 'NetLiquidation' and av.currency == 'USD':
            account_values['equity'] = float(av.value)
        if av.tag == 'AvailableFunds' and av.currency == 'USD':
            account_values['buying_power'] = float(av.value)

    return positions, account_values


def get_market_prices(ib, symbols):
    """Fetch market prices for multiple symbols in one pass. Fix C4: no redundant calls."""
    prices = {}
    contracts = []
    for symbol in symbols:
        contract = Stock(symbol, 'SMART', 'USD')
        ib.qualifyContracts(contract)
        contracts.append((symbol, contract))

    # Request all at once
    tickers = {}
    for symbol, contract in contracts:
        tickers[symbol] = ib.reqMktData(contract, '', False, False)

    time.sleep(3)  # Single wait for all

    for symbol, contract in contracts:
        ib.cancelMktData(contract)
        tk = tickers[symbol]
        price = tk.marketPrice()
        if np.isnan(price) or price <= 0:
            price = tk.close
        if not np.isnan(price) and price > 0:
            prices[symbol] = float(price)

    return prices


def check_order_submitted(trade, symbol, side_text, qty):
    """Check if order was accepted. DAY orders auto-expire at close if not filled."""
    order_id = trade.order.orderId
    status = trade.orderStatus.status

    if status in ['Submitted', 'PreSubmitted', 'Filled']:
        return True, f"✅ {t('submitted', order_id=order_id)}"
    else:
        return False, f"❌ {t('failed', reason=status)}"


def execute_rebalance(ib, target_weights, positions, account_values):
    """Execute trades to reach target weights."""
    equity = account_values.get('equity', 0)
    if equity <= 0:
        return ["❌ No equity"]

    reserved = equity * CASH_RESERVE_RATIO
    investable = equity - reserved
    threshold = equity * REBALANCE_THRESHOLD_RATIO

    # Get all prices in one batch (Fix C4)
    all_symbols = set(target_weights.keys()) | set(positions.keys())
    strategy_symbols = set(RANKING_POOL + [SAFE_HAVEN])
    all_symbols = all_symbols & strategy_symbols

    prices = get_market_prices(ib, all_symbols)

    # Current market values
    current_mv = {}
    for symbol in all_symbols:
        qty = positions.get(symbol, {}).get('quantity', 0)
        price = prices.get(symbol, 0)
        current_mv[symbol] = qty * price

    # Target market values
    target_mv = {}
    for symbol, weight in target_weights.items():
        target_mv[symbol] = investable * weight

    trade_logs = []

    # --- Sell phase ---
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

            contract = Stock(symbol, 'SMART', 'USD')
            ib.qualifyContracts(contract)
            order = MarketOrder('SELL', qty)
            trade = ib.placeOrder(contract, order)
            time.sleep(1)

            ok, status_msg = check_order_submitted(trade, symbol, "Sell", qty)
            log = t("market_sell", symbol=symbol, qty=qty) + f" {status_msg}"
            trade_logs.append(log)
            if ok:
                sell_executed = True

    if sell_executed:
        time.sleep(SELL_SETTLE_DELAY_SEC)

    # --- Buy phase ---
    account_values_new = {}
    for av in ib.accountValues():
        if av.tag == 'AvailableFunds' and av.currency == 'USD':
            account_values_new['buying_power'] = float(av.value)
    buying_power = account_values_new.get('buying_power', account_values.get('buying_power', 0))

    for symbol, target in target_mv.items():
        current = current_mv.get(symbol, 0)
        if current < target - threshold:
            buy_value = min(target - current, buying_power * 0.95)
            price = prices.get(symbol)
            if not price or buy_value < 50:
                continue

            limit_price = round(price * LIMIT_BUY_PREMIUM, 2)
            qty = int(buy_value / limit_price)
            if qty <= 0:
                continue

            contract = Stock(symbol, 'SMART', 'USD')
            ib.qualifyContracts(contract)
            order = LimitOrder('BUY', qty, limit_price)
            order.tif = 'DAY'  # Auto-expire at close if not filled
            trade = ib.placeOrder(contract, order)
            time.sleep(1)

            ok, status_msg = check_order_submitted(trade, symbol, "Buy", qty)
            log = t("limit_buy", symbol=symbol, qty=qty, price=f"{limit_price:.2f}") + f" {status_msg}"
            trade_logs.append(log)
            if ok:
                buying_power -= qty * limit_price

    return trade_logs


# ---------------------------------------------------------------------------
# Main strategy runner
# ---------------------------------------------------------------------------
def run_strategy_core():
    ib = None  # Fix C5: safe disconnect
    try:
        ib = connect_ib()

        # Get current state
        positions, account_values = get_current_portfolio(ib)
        equity = account_values.get('equity', 0)
        buying_power = account_values.get('buying_power', 0)

        current_holdings = set(positions.keys())

        # Compute signals
        target_weights, signal_desc, is_emergency, canary_str = compute_signals(ib, current_holdings)

        # Build dashboard using account values (no extra market data calls) Fix C4
        pos_lines = []
        for symbol in sorted(positions.keys()):
            qty = positions[symbol]['quantity']
            avg = positions[symbol]['avg_cost']
            mv = qty * avg
            pos_lines.append(f"  {symbol}: {qty}股 ${mv:,.2f}")
        pos_str = '\n'.join(pos_lines) if pos_lines else "  (空仓)"

        dashboard = (
            f"{t('equity')}: ${equity:,.2f} | {t('buying_power')}: ${buying_power:,.2f}\n"
            f"{SEPARATOR}\n"
            f"{pos_str}\n"
            f"{SEPARATOR}\n"
            f"🐤 {canary_str}\n"
            f"🎯 {signal_desc}"
        )

        if target_weights is None:
            msg = f"{t('heartbeat_title')}\n{dashboard}\n{SEPARATOR}\n{t('no_trades')}"
            send_tg_message(msg)
            print(msg, flush=True)
            return "OK - heartbeat"

        # Execute rebalance
        trade_logs = execute_rebalance(ib, target_weights, positions, account_values)

        if trade_logs:
            trades_str = '\n'.join(trade_logs)
            msg = (
                f"{t('rebalance_title')}\n"
                f"{dashboard}\n"
                f"{SEPARATOR}\n"
                f"{trades_str}"
            )
        else:
            msg = f"{t('heartbeat_title')}\n{dashboard}\n{SEPARATOR}\n{t('no_trades')}"

        send_tg_message(msg)
        print(msg, flush=True)
        return "OK - executed"

    finally:
        if ib is not None and ib.isConnected():  # Fix C5: safe disconnect
            ib.disconnect()


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
@app.route("/", methods=["POST", "GET"])
def handle_request():
    if request.method == "GET":
        return "OK - use POST to execute strategy", 200

    try:
        tz_ny = pytz.timezone('America/New_York')
        now_ny = datetime.now(tz_ny)
        nyse = mcal.get_calendar('NYSE')
        schedule = nyse.schedule(start_date=now_ny.date(), end_date=now_ny.date())
        if schedule.empty:
            return "Market Closed", 200
        result = run_strategy_core()
        return result, 200
    except Exception:
        error_msg = f"{t('error_title')}\n{traceback.format_exc()}"
        send_tg_message(error_msg)
        print(error_msg, flush=True)
        return "Error", 500


@app.route("/health", methods=["GET"])
def health():
    return "OK", 200


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))
