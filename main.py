"""
IBKR Global ETF Rotation Strategy.
Quarterly momentum rotation across 22 global ETFs + daily canary emergency.
Runs on Cloud Run; connects to IB Gateway on GCE via ib_insync, alerts via Telegram.
"""
import os
import time
import traceback
import requests
import pandas as pd
from flask import Flask, request

import google.auth
try:
    from google.cloud import compute_v1
except ImportError:
    compute_v1 = None

from quant_platform_kit.common.models import OrderIntent
from quant_platform_kit.ibkr import (
    connect_ib as ibkr_connect_ib,
    ensure_event_loop,
    fetch_historical_price_series,
    fetch_portfolio_snapshot,
    fetch_quote_snapshots,
    submit_order_intent,
)
from application.execution_service import (
    check_order_submitted as application_check_order_submitted,
    execute_rebalance as application_execute_rebalance,
    get_market_prices as application_get_market_prices,
)
from application.rebalance_service import run_strategy_core as run_rebalance_cycle
from entrypoints.cloud_run import is_market_open_today
from strategy.signals import (
    check_sma as strategy_check_sma,
    compute_13612w_momentum as strategy_compute_13612w_momentum,
    compute_signals as strategy_compute_signals,
)

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
    - Read IB_GATEWAY_INSTANCE_NAME only
    - If IB_GATEWAY_ZONE is set: resolve instance name via Compute API
    - If IB_GATEWAY_ZONE is not set: use the configured instance name directly
    """
    host = os.getenv("IB_GATEWAY_INSTANCE_NAME")
    if not host:
        raise EnvironmentError("IB_GATEWAY_INSTANCE_NAME is required")
    zone = os.getenv("IB_GATEWAY_ZONE", "")
    if zone:
        return resolve_gce_instance_ip(host, zone)
    return host


def get_ib_gateway_mode():
    mode = os.getenv("IB_GATEWAY_MODE", "").strip().lower()
    if not mode:
        raise EnvironmentError("IB_GATEWAY_MODE is required and must be either 'live' or 'paper'")
    if mode in {"live", "paper"}:
        return mode
    raise EnvironmentError("IB_GATEWAY_MODE must be either 'live' or 'paper'")


def get_ib_port():
    mode = get_ib_gateway_mode()
    return 4002 if mode == "paper" else 4001


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
IB_HOST = get_ib_host()
IB_PORT = get_ib_port()
IB_CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "1"))

TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("GLOBAL_TELEGRAM_CHAT_ID")
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


def connect_ib():
    return ibkr_connect_ib(IB_HOST, IB_PORT, IB_CLIENT_ID)


def get_historical_close(ib, symbol, duration="2 Y", bar_size="1 day"):
    """Fetch daily close prices from IBKR via QuantPlatformKit."""
    series = fetch_historical_price_series(
        ib,
        symbol,
        duration=duration,
        bar_size=bar_size,
    )
    if not series.points:
        return pd.Series(dtype=float)
    return pd.Series(
        data=[point.close for point in series.points],
        index=pd.to_datetime([point.as_of for point in series.points]),
    )


# ---------------------------------------------------------------------------
# Strategy logic
# ---------------------------------------------------------------------------
def compute_13612w_momentum(closes, as_of_date=None):
    return strategy_compute_13612w_momentum(closes, as_of_date=as_of_date)


def check_sma(closes, period=SMA_PERIOD):
    return strategy_check_sma(closes, period=period)


def compute_signals(ib, current_holdings):
    return strategy_compute_signals(
        ib,
        current_holdings,
        get_historical_close=get_historical_close,
        ranking_pool=RANKING_POOL,
        canary_assets=CANARY_ASSETS,
        safe_haven=SAFE_HAVEN,
        top_n=TOP_N,
        hold_bonus=HOLD_BONUS,
        canary_bad_threshold=CANARY_BAD_THRESHOLD,
        rebalance_months=REBALANCE_MONTHS,
        translator=t,
        pacing_sec=HIST_DATA_PACING_SEC,
        sma_period=SMA_PERIOD,
    )


# ---------------------------------------------------------------------------
# Portfolio execution
# ---------------------------------------------------------------------------
def get_current_portfolio(ib):
    """Get current positions and account values."""
    snapshot = fetch_portfolio_snapshot(ib)
    positions = {}
    for position in snapshot.positions:
        positions[position.symbol] = {
            'quantity': int(position.quantity),
            'avg_cost': float(position.average_cost or 0.0),
        }

    account_values = {
        'equity': snapshot.total_equity,
        'buying_power': snapshot.buying_power or 0.0,
    }

    return positions, account_values


def get_market_prices(ib, symbols):
    return application_get_market_prices(
        ib,
        symbols,
        fetch_quote_snapshots=fetch_quote_snapshots,
    )


def check_order_submitted(report):
    return application_check_order_submitted(report, translator=t)


def execute_rebalance(ib, target_weights, positions, account_values):
    return application_execute_rebalance(
        ib,
        target_weights,
        positions,
        account_values,
        fetch_quote_snapshots=fetch_quote_snapshots,
        submit_order_intent=submit_order_intent,
        order_intent_cls=OrderIntent,
        translator=t,
        ranking_pool=RANKING_POOL,
        safe_haven=SAFE_HAVEN,
        cash_reserve_ratio=CASH_RESERVE_RATIO,
        rebalance_threshold_ratio=REBALANCE_THRESHOLD_RATIO,
        limit_buy_premium=LIMIT_BUY_PREMIUM,
        sell_settle_delay_sec=SELL_SETTLE_DELAY_SEC,
    )


# ---------------------------------------------------------------------------
# Main strategy runner
# ---------------------------------------------------------------------------
def run_strategy_core():
    return run_rebalance_cycle(
        connect_ib=connect_ib,
        get_current_portfolio=get_current_portfolio,
        compute_signals=compute_signals,
        execute_rebalance=execute_rebalance,
        send_tg_message=send_tg_message,
        translator=t,
        separator=SEPARATOR,
    )


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
@app.route("/", methods=["POST", "GET"])
def handle_request():
    if request.method == "GET":
        return "OK - use POST to execute strategy", 200

    try:
        if not is_market_open_today():
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
