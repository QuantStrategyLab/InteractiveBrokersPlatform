"""
IBKR Global ETF Rotation Strategy.
Quarterly momentum rotation across 22 global ETFs + daily canary emergency.
Runs on Cloud Run; connects to IB Gateway on GCE via ib_insync, alerts via Telegram.
"""
import os
import traceback
import requests
import pandas as pd
from flask import Flask, request

import google.auth
try:
    from google.cloud import compute_v1
except ImportError:
    compute_v1 = None

from notifications.telegram import build_translator, send_telegram_message
from quant_platform_kit.common.models import OrderIntent
from quant_platform_kit.ibkr import (
    connect_ib as ibkr_connect_ib,
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
from runtime_config_support import (
    load_platform_runtime_settings,
    resolve_ib_gateway_ip_mode,
)
from strategy_loader import load_signal_logic_module

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
    raw_value = os.getenv("IB_GATEWAY_IP_MODE")
    if raw_value is None and "RUNTIME_SETTINGS" in globals():
        raw_value = RUNTIME_SETTINGS.ib_gateway_ip_mode
    return resolve_ib_gateway_ip_mode(
        raw_value,
        logger=lambda message: print(message, flush=True),
    )


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
    host = RUNTIME_SETTINGS.ib_gateway_instance_name
    zone = RUNTIME_SETTINGS.ib_gateway_zone
    if zone:
        return resolve_gce_instance_ip(host, zone)
    return host


def get_ib_gateway_mode():
    return RUNTIME_SETTINGS.ib_gateway_mode


def get_ib_port():
    mode = get_ib_gateway_mode()
    return 4002 if mode == "paper" else 4001


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
RUNTIME_SETTINGS = load_platform_runtime_settings(project_id_resolver=get_project_id)
IB_HOST = get_ib_host()
IB_PORT = get_ib_port()
IB_CLIENT_ID = RUNTIME_SETTINGS.ib_client_id
STRATEGY_PROFILE = RUNTIME_SETTINGS.strategy_profile
ACCOUNT_GROUP = RUNTIME_SETTINGS.account_group
SERVICE_NAME = RUNTIME_SETTINGS.service_name
ACCOUNT_IDS = RUNTIME_SETTINGS.account_ids

STRATEGY_LOGIC = load_signal_logic_module(STRATEGY_PROFILE)
RANKING_POOL = STRATEGY_LOGIC.RANKING_POOL
CANARY_ASSETS = STRATEGY_LOGIC.CANARY_ASSETS
SAFE_HAVEN = STRATEGY_LOGIC.SAFE_HAVEN
TOP_N = STRATEGY_LOGIC.TOP_N
SMA_PERIOD = STRATEGY_LOGIC.SMA_PERIOD
HOLD_BONUS = STRATEGY_LOGIC.HOLD_BONUS
CANARY_BAD_THRESHOLD = STRATEGY_LOGIC.CANARY_BAD_THRESHOLD
REBALANCE_MONTHS = STRATEGY_LOGIC.REBALANCE_MONTHS
strategy_check_sma = STRATEGY_LOGIC.check_sma
strategy_compute_13612w_momentum = STRATEGY_LOGIC.compute_13612w_momentum
strategy_compute_signals = STRATEGY_LOGIC.compute_signals

TG_TOKEN = RUNTIME_SETTINGS.tg_token
TG_CHAT_ID = RUNTIME_SETTINGS.tg_chat_id
NOTIFY_LANG = RUNTIME_SETTINGS.notify_lang

CASH_RESERVE_RATIO = 0.03
REBALANCE_THRESHOLD_RATIO = 0.02  # 2% of equity to trigger trades
LIMIT_BUY_PREMIUM = 1.005

# Execution
SELL_SETTLE_DELAY_SEC = 3

# IBKR pacing: delay between historical data requests
HIST_DATA_PACING_SEC = 0.5

SEPARATOR = "━━━━━━━━━━━━━━━━━━"

def t(key, **kwargs):
    return build_translator(NOTIFY_LANG)(key, **kwargs)


def send_tg_message(message):
    return send_telegram_message(
        message,
        token=TG_TOKEN,
        chat_id=TG_CHAT_ID,
        requests_module=requests,
    )


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
