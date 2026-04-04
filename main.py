"""IBKR strategy runner for shared us_equity strategy profiles."""
import os
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

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
    ensure_event_loop as ibkr_ensure_event_loop,
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
from application.feature_snapshot_service import load_feature_snapshot_guarded
from application.rebalance_service import run_strategy_core as run_rebalance_cycle
from entrypoints.cloud_run import is_market_open_today
from runtime_config_support import (
    load_platform_runtime_settings,
    resolve_ib_gateway_ip_mode,
)
from strategy_loader import load_signal_logic_module

app = Flask(__name__)
ensure_event_loop = ibkr_ensure_event_loop
NEW_YORK_TZ = ZoneInfo("America/New_York")


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
STRATEGY_SIGNAL_SOURCE = getattr(STRATEGY_LOGIC, "SIGNAL_SOURCE", "market_data")
STRATEGY_STATUS_ICON = getattr(STRATEGY_LOGIC, "STATUS_ICON", "🐤")
SAFE_HAVEN = getattr(STRATEGY_LOGIC, "SAFE_HAVEN", "BIL")
RANKING_POOL = list(getattr(STRATEGY_LOGIC, "RANKING_POOL", ()))
CANARY_ASSETS = list(getattr(STRATEGY_LOGIC, "CANARY_ASSETS", ()))
TOP_N = getattr(STRATEGY_LOGIC, "TOP_N", None)
SMA_PERIOD = getattr(STRATEGY_LOGIC, "SMA_PERIOD", 200)
CANARY_BAD_THRESHOLD = getattr(STRATEGY_LOGIC, "CANARY_BAD_THRESHOLD", None)
REBALANCE_MONTHS = getattr(STRATEGY_LOGIC, "REBALANCE_MONTHS", None)
FEATURE_SNAPSHOT_PATH = RUNTIME_SETTINGS.feature_snapshot_path
FEATURE_SNAPSHOT_MANIFEST_PATH = RUNTIME_SETTINGS.feature_snapshot_manifest_path
FEATURE_REQUIRE_SNAPSHOT_MANIFEST = bool(getattr(STRATEGY_LOGIC, "REQUIRE_SNAPSHOT_MANIFEST", False))
FEATURE_SNAPSHOT_CONTRACT_VERSION = getattr(STRATEGY_LOGIC, "SNAPSHOT_CONTRACT_VERSION", None)
feature_runtime_loader = getattr(STRATEGY_LOGIC, "load_runtime_parameters", None)
FEATURE_RUNTIME_PARAMETERS = (
    feature_runtime_loader(
        config_path=RUNTIME_SETTINGS.strategy_config_path,
        logger=lambda message: print(message, flush=True),
    )
    if STRATEGY_SIGNAL_SOURCE == "feature_snapshot" and callable(feature_runtime_loader)
    else {}
)
HOLD_BONUS = FEATURE_RUNTIME_PARAMETERS.get(
    "hold_bonus",
    getattr(STRATEGY_LOGIC, "HOLD_BONUS", getattr(STRATEGY_LOGIC, "DEFAULT_HOLD_BONUS", 0.0)),
)
FEATURE_SIGNAL_KWARG_KEYS = tuple(
    getattr(
        STRATEGY_LOGIC,
        "FEATURE_SIGNAL_KWARG_KEYS",
        (
            "benchmark_symbol",
            "safe_haven",
            "holdings_count",
            "single_name_cap",
            "sector_cap",
            "hold_bonus",
            "soft_defense_exposure",
            "hard_defense_exposure",
            "soft_breadth_threshold",
            "hard_breadth_threshold",
        ),
    )
)
FEATURE_BENCHMARK_SYMBOL = FEATURE_RUNTIME_PARAMETERS.get(
    "benchmark_symbol",
    getattr(STRATEGY_LOGIC, "BENCHMARK_SYMBOL", "SPY"),
)
FEATURE_HOLDINGS_COUNT = FEATURE_RUNTIME_PARAMETERS.get(
    "holdings_count",
    getattr(STRATEGY_LOGIC, "DEFAULT_HOLDINGS_COUNT", 24),
)
FEATURE_SINGLE_NAME_CAP = FEATURE_RUNTIME_PARAMETERS.get(
    "single_name_cap",
    getattr(STRATEGY_LOGIC, "DEFAULT_SINGLE_NAME_CAP", 0.06),
)
FEATURE_SECTOR_CAP = FEATURE_RUNTIME_PARAMETERS.get(
    "sector_cap",
    getattr(STRATEGY_LOGIC, "DEFAULT_SECTOR_CAP", 0.20),
)
FEATURE_RISK_ON_EXPOSURE = FEATURE_RUNTIME_PARAMETERS.get(
    "risk_on_exposure",
    1.0,
)
FEATURE_SOFT_DEFENSE_EXPOSURE = FEATURE_RUNTIME_PARAMETERS.get(
    "soft_defense_exposure",
    getattr(STRATEGY_LOGIC, "DEFAULT_SOFT_DEFENSE_EXPOSURE", 0.50),
)
FEATURE_HARD_DEFENSE_EXPOSURE = FEATURE_RUNTIME_PARAMETERS.get(
    "hard_defense_exposure",
    getattr(STRATEGY_LOGIC, "DEFAULT_HARD_DEFENSE_EXPOSURE", 0.10),
)
FEATURE_SOFT_BREADTH_THRESHOLD = FEATURE_RUNTIME_PARAMETERS.get(
    "soft_breadth_threshold",
    getattr(STRATEGY_LOGIC, "DEFAULT_SOFT_BREADTH_THRESHOLD", 0.55),
)
FEATURE_HARD_BREADTH_THRESHOLD = FEATURE_RUNTIME_PARAMETERS.get(
    "hard_breadth_threshold",
    getattr(STRATEGY_LOGIC, "DEFAULT_HARD_BREADTH_THRESHOLD", 0.35),
)
FEATURE_RUNTIME_EXECUTION_WINDOW_TRADING_DAYS = FEATURE_RUNTIME_PARAMETERS.get(
    "runtime_execution_window_trading_days",
)
FEATURE_MIN_ADV20_USD = FEATURE_RUNTIME_PARAMETERS.get("min_adv20_usd")
FEATURE_SECTOR_WHITELIST = FEATURE_RUNTIME_PARAMETERS.get("sector_whitelist")
FEATURE_NORMALIZATION = FEATURE_RUNTIME_PARAMETERS.get("normalization")
FEATURE_SCORE_TEMPLATE = FEATURE_RUNTIME_PARAMETERS.get("score_template")
FEATURE_RESIDUAL_PROXY = FEATURE_RUNTIME_PARAMETERS.get("residual_proxy")
FEATURE_RUNTIME_CONFIG_NAME = FEATURE_RUNTIME_PARAMETERS.get(
    "runtime_config_name",
    RUNTIME_SETTINGS.strategy_profile,
)
FEATURE_RUNTIME_CONFIG_PATH = FEATURE_RUNTIME_PARAMETERS.get(
    "runtime_config_path",
    RUNTIME_SETTINGS.strategy_config_path,
)
FEATURE_RUNTIME_CONFIG_SOURCE = FEATURE_RUNTIME_PARAMETERS.get(
    "runtime_config_source",
    RUNTIME_SETTINGS.strategy_config_source,
)
RECONCILIATION_OUTPUT_PATH = RUNTIME_SETTINGS.reconciliation_output_path
strategy_check_sma = getattr(STRATEGY_LOGIC, "check_sma", None)
strategy_compute_13612w_momentum = getattr(STRATEGY_LOGIC, "compute_13612w_momentum", None)
strategy_compute_signals = STRATEGY_LOGIC.compute_signals

TG_TOKEN = RUNTIME_SETTINGS.tg_token
TG_CHAT_ID = RUNTIME_SETTINGS.tg_chat_id
NOTIFY_LANG = RUNTIME_SETTINGS.notify_lang

DEFAULT_CASH_RESERVE_RATIO = 0.03
CASH_RESERVE_RATIO = float(
    FEATURE_RUNTIME_PARAMETERS.get(
        "execution_cash_reserve_ratio",
        DEFAULT_CASH_RESERVE_RATIO,
    )
)
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


def resolve_run_as_of_date() -> pd.Timestamp:
    explicit = os.getenv("IBKR_RUN_AS_OF_DATE")
    if explicit:
        return pd.Timestamp(explicit).normalize()
    return pd.Timestamp(datetime.now(NEW_YORK_TZ).date())


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
    if strategy_compute_13612w_momentum is None:
        raise NotImplementedError(f"{STRATEGY_PROFILE} does not expose 13612W momentum")
    return strategy_compute_13612w_momentum(closes, as_of_date=as_of_date)


def check_sma(closes, period=SMA_PERIOD):
    if strategy_check_sma is None:
        raise NotImplementedError(f"{STRATEGY_PROFILE} does not expose SMA filtering")
    return strategy_check_sma(closes, period=period)


def compute_signals(ib, current_holdings):
    if STRATEGY_SIGNAL_SOURCE == "feature_snapshot":
        run_as_of = resolve_run_as_of_date()
        if not FEATURE_SNAPSHOT_PATH:
            return (
                None,
                "feature snapshot required",
                False,
                "fail_closed | reason=feature_snapshot_path_missing",
                {
                    "strategy_profile": STRATEGY_PROFILE,
                    "feature_snapshot_path": None,
                    "strategy_config_path": FEATURE_RUNTIME_CONFIG_PATH,
                    "strategy_config_source": FEATURE_RUNTIME_CONFIG_SOURCE,
                    "dry_run_only": RUNTIME_SETTINGS.dry_run_only,
                    "snapshot_guard_decision": "fail_closed",
                    "fail_reason": "feature_snapshot_path_missing",
                    "managed_symbols": (),
                    "status_icon": "🛑",
                },
            )
        guard_result = load_feature_snapshot_guarded(
            FEATURE_SNAPSHOT_PATH,
            run_as_of=run_as_of,
            required_columns=getattr(STRATEGY_LOGIC, "REQUIRED_FEATURE_COLUMNS", ()),
            snapshot_date_columns=getattr(
                STRATEGY_LOGIC,
                "SNAPSHOT_DATE_COLUMNS",
                ("as_of", "snapshot_date"),
            ),
            max_snapshot_month_lag=int(
                getattr(STRATEGY_LOGIC, "MAX_SNAPSHOT_MONTH_LAG", 1)
            ),
            manifest_path=FEATURE_SNAPSHOT_MANIFEST_PATH,
            require_manifest=FEATURE_REQUIRE_SNAPSHOT_MANIFEST,
            expected_strategy_profile=STRATEGY_PROFILE,
            expected_config_name=FEATURE_RUNTIME_CONFIG_NAME,
            expected_config_path=FEATURE_RUNTIME_CONFIG_PATH,
            expected_contract_version=FEATURE_SNAPSHOT_CONTRACT_VERSION,
        )
        guard_metadata = dict(guard_result.metadata)
        print(
            "snapshot_manifest_summary | "
            f"profile={STRATEGY_PROFILE} decision={guard_metadata.get('snapshot_guard_decision')} "
            f"snapshot_path={guard_metadata.get('snapshot_path')} "
            f"snapshot_as_of={guard_metadata.get('snapshot_as_of')} "
            f"snapshot_age_days={guard_metadata.get('snapshot_age_days')} "
            f"snapshot_file_ts={guard_metadata.get('snapshot_file_timestamp')} "
            f"manifest_path={guard_metadata.get('snapshot_manifest_path')} "
            f"manifest_exists={guard_metadata.get('snapshot_manifest_exists')} "
            f"manifest_contract={guard_metadata.get('snapshot_manifest_contract_version')} "
            f"expected_config={FEATURE_RUNTIME_CONFIG_PATH} "
            f"expected_profile={STRATEGY_PROFILE}",
            flush=True,
        )
        if guard_result.metadata.get("snapshot_guard_decision") != "proceed":
            decision = guard_metadata.get("snapshot_guard_decision")
            reason = guard_metadata.get("fail_reason") or guard_metadata.get("no_op_reason")
            return (
                None,
                "feature snapshot guard blocked execution",
                False,
                f"{decision} | reason={reason}",
                {
                    "strategy_profile": STRATEGY_PROFILE,
                    "strategy_config_path": FEATURE_RUNTIME_CONFIG_PATH,
                    "strategy_config_source": FEATURE_RUNTIME_CONFIG_SOURCE,
                    "dry_run_only": RUNTIME_SETTINGS.dry_run_only,
                    "managed_symbols": (),
                    "status_icon": "🛑",
                    **guard_metadata,
                },
            )
        feature_snapshot = guard_result.frame
        feature_kwargs = {
            "benchmark_symbol": FEATURE_BENCHMARK_SYMBOL,
            "safe_haven": SAFE_HAVEN,
            "holdings_count": FEATURE_HOLDINGS_COUNT,
            "single_name_cap": FEATURE_SINGLE_NAME_CAP,
            "sector_cap": FEATURE_SECTOR_CAP,
            "hold_bonus": HOLD_BONUS,
            "risk_on_exposure": FEATURE_RISK_ON_EXPOSURE,
            "soft_defense_exposure": FEATURE_SOFT_DEFENSE_EXPOSURE,
            "hard_defense_exposure": FEATURE_HARD_DEFENSE_EXPOSURE,
            "soft_breadth_threshold": FEATURE_SOFT_BREADTH_THRESHOLD,
            "hard_breadth_threshold": FEATURE_HARD_BREADTH_THRESHOLD,
            "min_adv20_usd": FEATURE_MIN_ADV20_USD,
            "sector_whitelist": FEATURE_SECTOR_WHITELIST,
            "normalization": FEATURE_NORMALIZATION,
            "score_template": FEATURE_SCORE_TEMPLATE,
            "run_as_of": run_as_of,
            "runtime_execution_window_trading_days": FEATURE_RUNTIME_EXECUTION_WINDOW_TRADING_DAYS,
            "runtime_config_name": FEATURE_RUNTIME_CONFIG_NAME,
            "runtime_config_path": FEATURE_RUNTIME_CONFIG_PATH,
            "runtime_config_source": FEATURE_RUNTIME_CONFIG_SOURCE,
            "residual_proxy": FEATURE_RESIDUAL_PROXY,
        }
        feature_kwargs = {
            key: value
            for key, value in feature_kwargs.items()
            if key in FEATURE_SIGNAL_KWARG_KEYS and value is not None
        }
        try:
            result = strategy_compute_signals(
                feature_snapshot,
                current_holdings,
                **feature_kwargs,
            )
        except Exception as exc:
            return (
                None,
                "feature snapshot compute failed",
                False,
                f"fail_closed | reason=feature_snapshot_compute_failed:{type(exc).__name__}:{exc}",
                {
                    "strategy_profile": STRATEGY_PROFILE,
                    "strategy_config_path": FEATURE_RUNTIME_CONFIG_PATH,
                    "strategy_config_source": FEATURE_RUNTIME_CONFIG_SOURCE,
                    "dry_run_only": RUNTIME_SETTINGS.dry_run_only,
                    "managed_symbols": (),
                    "status_icon": "🛑",
                    **guard_metadata,
                    "snapshot_guard_decision": "fail_closed",
                    "fail_reason": f"feature_snapshot_compute_failed:{type(exc).__name__}:{exc}",
                },
            )
        if len(result) == 5:
            target_weights, signal_desc, is_emergency, status_desc, metadata = result
            return (
                target_weights,
                signal_desc,
                is_emergency,
                status_desc,
                {
                    "strategy_profile": STRATEGY_PROFILE,
                    "feature_snapshot_path": FEATURE_SNAPSHOT_PATH,
                    "strategy_config_path": FEATURE_RUNTIME_CONFIG_PATH,
                    "strategy_config_source": FEATURE_RUNTIME_CONFIG_SOURCE,
                    "safe_haven_symbol": SAFE_HAVEN,
                    "dry_run_only": RUNTIME_SETTINGS.dry_run_only,
                    "trade_date": run_as_of.date().isoformat(),
                    **guard_metadata,
                    **metadata,
                },
            )
        target_weights, signal_desc, is_emergency, status_desc = result
        return (
            target_weights,
            signal_desc,
            is_emergency,
            status_desc,
            {
                "strategy_profile": STRATEGY_PROFILE,
                "feature_snapshot_path": FEATURE_SNAPSHOT_PATH,
                "strategy_config_path": FEATURE_RUNTIME_CONFIG_PATH,
                "strategy_config_source": FEATURE_RUNTIME_CONFIG_SOURCE,
                "safe_haven_symbol": SAFE_HAVEN,
                "dry_run_only": RUNTIME_SETTINGS.dry_run_only,
                "trade_date": run_as_of.date().isoformat(),
                **guard_metadata,
                "managed_symbols": tuple(
                    getattr(
                        STRATEGY_LOGIC,
                        "extract_managed_symbols",
                    )(feature_snapshot, benchmark_symbol=FEATURE_BENCHMARK_SYMBOL, safe_haven=SAFE_HAVEN)
                ),
                "status_icon": STRATEGY_STATUS_ICON,
            },
        )

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
    ) + (
        {
            "strategy_profile": STRATEGY_PROFILE,
            "managed_symbols": tuple(RANKING_POOL + [SAFE_HAVEN]),
            "status_icon": STRATEGY_STATUS_ICON,
            "safe_haven_symbol": SAFE_HAVEN,
            "dry_run_only": RUNTIME_SETTINGS.dry_run_only,
        },
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


def execute_rebalance(
    ib,
    target_weights,
    positions,
    account_values,
    *,
    strategy_symbols=None,
    signal_metadata=None,
):
    return application_execute_rebalance(
        ib,
        target_weights,
        positions,
        account_values,
        fetch_quote_snapshots=fetch_quote_snapshots,
        submit_order_intent=submit_order_intent,
        order_intent_cls=OrderIntent,
        translator=t,
        strategy_symbols=strategy_symbols,
        signal_metadata=signal_metadata or {},
        strategy_profile=STRATEGY_PROFILE,
        account_group=ACCOUNT_GROUP,
        service_name=SERVICE_NAME,
        account_ids=ACCOUNT_IDS,
        dry_run_only=RUNTIME_SETTINGS.dry_run_only,
        cash_reserve_ratio=CASH_RESERVE_RATIO,
        rebalance_threshold_ratio=REBALANCE_THRESHOLD_RATIO,
        limit_buy_premium=LIMIT_BUY_PREMIUM,
        sell_settle_delay_sec=SELL_SETTLE_DELAY_SEC,
        return_summary=True,
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
        reconciliation_output_path=RECONCILIATION_OUTPUT_PATH,
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
