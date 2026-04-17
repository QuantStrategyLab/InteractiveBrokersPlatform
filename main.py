"""IBKR strategy runner for shared us_equity strategy profiles."""
import os
import traceback
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
import pandas as pd
from flask import Flask, request

import google.auth
try:
    from google.cloud import compute_v1
except ImportError:
    compute_v1 = None

from notifications.telegram import build_strategy_display_name, build_translator, send_telegram_message
from quant_platform_kit.common.models import OrderIntent
from quant_platform_kit.common.runtime_reports import (
    append_runtime_report_error,
    build_runtime_report_base,
    finalize_runtime_report,
    persist_runtime_report,
)
from quant_platform_kit.ibkr import (
    connect_ib as ibkr_connect_ib,
    ensure_event_loop as ibkr_ensure_event_loop,
    fetch_historical_price_candles,
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
from application.paper_liquidation_service import execute_paper_liquidation
from application.rebalance_service import run_strategy_core as run_rebalance_cycle
from decision_mapper import map_strategy_decision
from entrypoints.cloud_run import is_market_open_today
from runtime_logging import (
    RuntimeLogContext,
    build_run_id,
    emit_runtime_log,
    extract_cloud_trace,
)
from runtime_config_support import (
    load_platform_runtime_settings,
    resolve_ib_gateway_ip_mode,
)
from strategy_runtime import load_strategy_runtime

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
    Resolve IB Gateway host lazily.
    - Read IB_GATEWAY_INSTANCE_NAME only
    - If IB_GATEWAY_ZONE is set: resolve instance name via Compute API
    - If IB_GATEWAY_ZONE is not set: use the configured instance name directly
    """
    global IB_HOST

    if IB_HOST:
        return IB_HOST

    host = RUNTIME_SETTINGS.ib_gateway_instance_name
    zone = RUNTIME_SETTINGS.ib_gateway_zone
    if zone:
        host = resolve_gce_instance_ip(host, zone)
    IB_HOST = host
    return host


def get_ib_gateway_mode():
    return RUNTIME_SETTINGS.ib_gateway_mode


def get_ib_port():
    mode = get_ib_gateway_mode()
    return 4002 if mode == "paper" else 4001


def get_ib_connect_timeout_seconds():
    raw_value = os.getenv("IBKR_CONNECT_TIMEOUT_SECONDS", "60")
    try:
        timeout_seconds = int(raw_value)
    except (TypeError, ValueError):
        print(
            f"Invalid IBKR_CONNECT_TIMEOUT_SECONDS={raw_value!r}; using 60",
            flush=True,
        )
        return 60
    if timeout_seconds <= 0:
        print(
            f"Invalid IBKR_CONNECT_TIMEOUT_SECONDS={raw_value!r}; using 60",
            flush=True,
        )
        return 60
    return timeout_seconds


def _env_flag(name: str) -> bool:
    return str(os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
RUNTIME_SETTINGS = load_platform_runtime_settings(project_id_resolver=get_project_id)
IB_HOST = None
IB_PORT = get_ib_port()
IB_CLIENT_ID = RUNTIME_SETTINGS.ib_client_id
IB_CONNECT_TIMEOUT_SECONDS = get_ib_connect_timeout_seconds()
STRATEGY_PROFILE = RUNTIME_SETTINGS.strategy_profile
STRATEGY_DISPLAY_NAME = RUNTIME_SETTINGS.strategy_display_name
ACCOUNT_GROUP = RUNTIME_SETTINGS.account_group
SERVICE_NAME = RUNTIME_SETTINGS.service_name
ACCOUNT_IDS = RUNTIME_SETTINGS.account_ids
PROJECT_ID = RUNTIME_SETTINGS.project_id

STRATEGY_RUNTIME = load_strategy_runtime(
    STRATEGY_PROFILE,
    runtime_settings=RUNTIME_SETTINGS,
    logger=lambda message: print(message, flush=True),
)
STRATEGY_ENTRYPOINT = STRATEGY_RUNTIME.entrypoint
STRATEGY_SIGNAL_SOURCE = (
    "feature_snapshot"
    if "feature_snapshot" in STRATEGY_RUNTIME.required_inputs
    else "market_data"
)
STRATEGY_STATUS_ICON = STRATEGY_RUNTIME.status_icon
FEATURE_RUNTIME_PARAMETERS = dict(STRATEGY_RUNTIME.runtime_config)
STRATEGY_RUNTIME_CONFIG = dict(STRATEGY_RUNTIME.merged_runtime_config)
SAFE_HAVEN = str(STRATEGY_RUNTIME_CONFIG.get("safe_haven") or "BIL")
RANKING_POOL = list(STRATEGY_RUNTIME_CONFIG.get("ranking_pool", ()))
CANARY_ASSETS = list(STRATEGY_RUNTIME_CONFIG.get("canary_assets", ()))
TOP_N = STRATEGY_RUNTIME_CONFIG.get("top_n")
SMA_PERIOD = int(STRATEGY_RUNTIME_CONFIG.get("sma_period", 200))
CANARY_BAD_THRESHOLD = STRATEGY_RUNTIME_CONFIG.get("canary_bad_threshold")
REBALANCE_MONTHS = STRATEGY_RUNTIME_CONFIG.get("rebalance_months")
FEATURE_SNAPSHOT_PATH = RUNTIME_SETTINGS.feature_snapshot_path
FEATURE_SNAPSHOT_MANIFEST_PATH = RUNTIME_SETTINGS.feature_snapshot_manifest_path
FEATURE_RUNTIME_CONFIG_PATH = (
    STRATEGY_RUNTIME_CONFIG.get("runtime_config_path")
    or RUNTIME_SETTINGS.strategy_config_path
)
FEATURE_RUNTIME_CONFIG_SOURCE = (
    STRATEGY_RUNTIME_CONFIG.get("runtime_config_source")
    or RUNTIME_SETTINGS.strategy_config_source
)
RECONCILIATION_OUTPUT_PATH = RUNTIME_SETTINGS.reconciliation_output_path
PAPER_LIQUIDATE_ONLY = _env_flag("IBKR_PAPER_LIQUIDATE_ONLY")

TG_TOKEN = RUNTIME_SETTINGS.tg_token
TG_CHAT_ID = RUNTIME_SETTINGS.tg_chat_id
NOTIFY_LANG = RUNTIME_SETTINGS.notify_lang

CASH_RESERVE_RATIO = STRATEGY_RUNTIME.cash_reserve_ratio
REBALANCE_THRESHOLD_RATIO = 0.02  # 2% of equity to trigger trades
LIMIT_BUY_PREMIUM = 1.005

# Execution
SELL_SETTLE_DELAY_SEC = 3

# IBKR pacing: delay between historical data requests
HIST_DATA_PACING_SEC = 0.5

SEPARATOR = "━━━━━━━━━━━━━━━━━━"


def t(key, **kwargs):
    return build_translator(NOTIFY_LANG)(key, **kwargs)


strategy_display_name = build_strategy_display_name(t)(
    STRATEGY_PROFILE,
    fallback_name=STRATEGY_DISPLAY_NAME,
)


RUNTIME_LOG_CONTEXT = RuntimeLogContext(
    platform="interactive_brokers",
    deploy_target="cloud_run",
    service_name=SERVICE_NAME or os.getenv("K_SERVICE", "interactive-brokers-platform"),
    strategy_profile=STRATEGY_PROFILE,
    account_scope=ACCOUNT_GROUP,
    account_group=ACCOUNT_GROUP,
    project_id=PROJECT_ID,
    instance_name=RUNTIME_SETTINGS.ib_gateway_instance_name,
    extra_fields={
        "account_ids": list(ACCOUNT_IDS),
        "strategy_target_mode": RUNTIME_SETTINGS.strategy_target_mode,
        "strategy_artifact_dir": RUNTIME_SETTINGS.strategy_artifact_dir,
        "strategy_display_name": STRATEGY_DISPLAY_NAME,
        "strategy_display_name_localized": strategy_display_name,
    },
)
LAST_CYCLE_DETAILS: dict[str, object] = {}


def send_tg_message(message):
    return send_telegram_message(
        message,
        token=TG_TOKEN,
        chat_id=TG_CHAT_ID,
        requests_module=requests,
    )


def connect_ib():
    host = get_ib_host()
    print(
        "Connecting to IB gateway "
        f"{host}:{IB_PORT} "
        f"(mode={RUNTIME_SETTINGS.ib_gateway_mode}, "
        f"client_id={IB_CLIENT_ID}, "
        f"timeout={IB_CONNECT_TIMEOUT_SECONDS}s)",
        flush=True,
    )
    return ibkr_connect_ib(
        host,
        IB_PORT,
        IB_CLIENT_ID,
        timeout=IB_CONNECT_TIMEOUT_SECONDS,
    )


def log_runtime_event(log_context, event, **fields):
    return emit_runtime_log(
        log_context,
        event,
        printer=lambda line: print(line, flush=True),
        **fields,
    )


def build_execution_report(log_context):
    managed_symbols = tuple(
        str(symbol)
        for symbol in (
            STRATEGY_RUNTIME_CONFIG.get("managed_symbols")
            or tuple(dict.fromkeys([*RANKING_POOL, SAFE_HAVEN])) if RANKING_POOL else (SAFE_HAVEN,)
        )
        if str(symbol or "").strip()
    )
    return build_runtime_report_base(
        platform=log_context.platform,
        deploy_target=log_context.deploy_target,
        service_name=log_context.service_name,
        strategy_profile=STRATEGY_PROFILE,
        strategy_domain=RUNTIME_SETTINGS.strategy_domain,
        account_scope=log_context.account_scope,
        account_group=log_context.account_group,
        run_id=log_context.run_id,
        run_source="cloud_run",
        dry_run=RUNTIME_SETTINGS.dry_run_only,
        started_at=datetime.now(timezone.utc),
        summary={
            "account_ids": list(ACCOUNT_IDS),
            "managed_symbols": list(managed_symbols),
            "signal_source": STRATEGY_SIGNAL_SOURCE,
            "status_icon": STRATEGY_STATUS_ICON,
            "safe_haven": SAFE_HAVEN,
            "strategy_display_name": STRATEGY_DISPLAY_NAME,
            "strategy_display_name_localized": strategy_display_name,
        },
        diagnostics={
            "strategy_config_source": FEATURE_RUNTIME_CONFIG_SOURCE,
            "ib_gateway_host": get_ib_host(),
            "ib_gateway_port": IB_PORT,
            "ib_gateway_mode": RUNTIME_SETTINGS.ib_gateway_mode,
            "ib_gateway_ip_mode": RUNTIME_SETTINGS.ib_gateway_ip_mode,
            "ib_client_id": IB_CLIENT_ID,
            "ib_connect_timeout_seconds": IB_CONNECT_TIMEOUT_SECONDS,
        },
        artifacts={
            "feature_snapshot_path": FEATURE_SNAPSHOT_PATH,
            "feature_snapshot_manifest_path": FEATURE_SNAPSHOT_MANIFEST_PATH,
            "strategy_config_path": FEATURE_RUNTIME_CONFIG_PATH,
            "reconciliation_output_path": RECONCILIATION_OUTPUT_PATH,
        },
    )


def persist_execution_report(report):
    persisted = persist_runtime_report(
        report,
        base_dir=os.getenv("EXECUTION_REPORT_OUTPUT_DIR"),
        gcs_prefix_uri=os.getenv("EXECUTION_REPORT_GCS_URI"),
        gcp_project_id=PROJECT_ID,
    )
    return persisted.gcs_uri or persisted.local_path


def build_request_log_context():
    return RUNTIME_LOG_CONTEXT.with_run(
        build_run_id(),
        trace=extract_cloud_trace(
            PROJECT_ID,
            request.headers.get("X-Cloud-Trace-Context"),
        ),
    )


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


def get_historical_candles(ib, symbol, duration="2 Y", bar_size="1 day"):
    """Fetch daily OHLC candles from IBKR via QuantPlatformKit."""
    return fetch_historical_price_candles(
        ib,
        symbol,
        duration=duration,
        bar_size=bar_size,
    )


# ---------------------------------------------------------------------------
# Strategy logic
# ---------------------------------------------------------------------------
def compute_signals(ib, current_holdings):
    evaluation = STRATEGY_RUNTIME.evaluate(
        ib=ib,
        current_holdings=current_holdings,
        historical_close_loader=get_historical_close,
        historical_candle_loader=get_historical_candles,
        run_as_of=resolve_run_as_of_date(),
        translator=t,
        pacing_sec=HIST_DATA_PACING_SEC,
    )
    return map_strategy_decision(
        evaluation.decision,
        strategy_profile=STRATEGY_PROFILE,
        runtime_metadata=evaluation.metadata,
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


def _format_liquidation_orders(orders) -> str:
    preview = []
    for order in orders or ():
        symbol = str(order.get("symbol") or "").strip().upper()
        side = str(order.get("side") or "").strip().lower()
        quantity = float(order.get("quantity") or 0.0)
        status = str(order.get("status") or "").strip()
        if symbol:
            preview.append(f"{symbol} {side} {quantity:g} {status}".strip())
    return ", ".join(preview) if preview else t("no_trades")


def run_paper_liquidation_cycle():
    if RUNTIME_SETTINGS.ib_gateway_mode != "paper":
        raise RuntimeError("IBKR_PAPER_LIQUIDATE_ONLY is only allowed when ib_gateway_mode=paper")

    ib = connect_ib()
    try:
        positions, _account_values = get_current_portfolio(ib)
        summary = execute_paper_liquidation(
            ib,
            positions,
            submit_order_intent=submit_order_intent,
            order_intent_cls=OrderIntent,
            dry_run_only=RUNTIME_SETTINGS.dry_run_only,
        )
        message = (
            f"{t('rebalance_title')}\n"
            f"{t('strategy_label', name=strategy_display_name)}\n"
            f"{t('paper_liquidation_only')}\n"
            f"{t('paper_liquidation_status', mode=summary['mode'], status=summary['execution_status'])}\n"
            f"{SEPARATOR}\n"
            f"{_format_liquidation_orders(summary.get('orders_submitted'))}"
        )
        send_tg_message(message)
        print(message, flush=True)
        global LAST_CYCLE_DETAILS
        LAST_CYCLE_DETAILS = {"execution_summary": summary}
        return "OK"
    finally:
        if ib is not None and hasattr(ib, "disconnect"):
            ib.disconnect()


# ---------------------------------------------------------------------------
# Main strategy runner
# ---------------------------------------------------------------------------
def run_strategy_core():
    global LAST_CYCLE_DETAILS
    if PAPER_LIQUIDATE_ONLY:
        return run_paper_liquidation_cycle()

    cycle_details: dict[str, object] = {}
    result = run_rebalance_cycle(
        connect_ib=connect_ib,
        get_current_portfolio=get_current_portfolio,
        compute_signals=compute_signals,
        execute_rebalance=execute_rebalance,
        send_tg_message=send_tg_message,
        translator=t,
        separator=SEPARATOR,
        strategy_display_name=strategy_display_name,
        reconciliation_output_path=RECONCILIATION_OUTPUT_PATH,
        result_hook=lambda payload: cycle_details.update(payload or {}),
    )
    LAST_CYCLE_DETAILS = cycle_details
    return result


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
@app.route("/", methods=["POST", "GET"])
def handle_request():
    if request.method == "GET":
        return "OK - use POST to execute strategy", 200

    global LAST_CYCLE_DETAILS
    LAST_CYCLE_DETAILS = {}
    log_context = build_request_log_context()
    report = build_execution_report(log_context)
    try:
        log_runtime_event(
            log_context,
            "strategy_cycle_received",
            message="Received strategy execution request",
            http_method=request.method,
        )
        if not is_market_open_today():
            log_runtime_event(
                log_context,
                "market_closed",
                message="Market closed; skip strategy execution",
            )
            finalize_runtime_report(
                report,
                status="skipped",
                diagnostics={"skip_reason": "market_closed"},
            )
            return "Market Closed", 200
        log_runtime_event(
            log_context,
            "strategy_cycle_started",
            message="Starting strategy execution",
        )
        result = run_strategy_core()
        cycle_details = dict(LAST_CYCLE_DETAILS or {})
        execution_summary = dict(cycle_details.get("execution_summary") or {})
        reconciliation_record = dict(cycle_details.get("reconciliation_record") or {})
        finalize_runtime_report(
            report,
            status="ok",
            summary={
                "result": result,
                "execution_status": execution_summary.get("execution_status") or reconciliation_record.get("execution_status"),
                "no_op_reason": execution_summary.get("no_op_reason") or reconciliation_record.get("no_op_reason"),
                "orders_submitted_count": len(execution_summary.get("orders_submitted") or reconciliation_record.get("orders_submitted") or ()),
                "orders_skipped_count": len(execution_summary.get("orders_skipped") or reconciliation_record.get("orders_skipped") or ()),
                "snapshot_price_fallback_used": bool(
                    execution_summary.get("snapshot_price_fallback_used")
                    or reconciliation_record.get("snapshot_price_fallback_used")
                ),
                "snapshot_price_fallback_count": int(
                    execution_summary.get("snapshot_price_fallback_count")
                    or reconciliation_record.get("snapshot_price_fallback_count")
                    or 0
                ),
            },
            diagnostics={
                "result": result,
                "price_source_mode": execution_summary.get("price_source_mode") or reconciliation_record.get("price_source_mode"),
                "snapshot_price_fallback_symbols": execution_summary.get("snapshot_price_fallback_symbols")
                or reconciliation_record.get("snapshot_price_fallback_symbols")
                or [],
            },
            artifacts={
                "reconciliation_record_path": cycle_details.get("reconciliation_record_path"),
            },
        )
        log_runtime_event(
            log_context,
            "strategy_cycle_completed",
            message="Strategy execution completed",
            result=result,
        )
        return result, 200
    except Exception as exc:
        append_runtime_report_error(
            report,
            stage="strategy_cycle",
            message=str(exc),
            error_type=type(exc).__name__,
        )
        finalize_runtime_report(report, status="error")
        log_runtime_event(
            log_context,
            "strategy_cycle_failed",
            message="Strategy execution failed",
            severity="ERROR",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        error_msg = f"{t('error_title')}\n{traceback.format_exc()}"
        send_tg_message(error_msg)
        print(error_msg, flush=True)
        return "Error", 500
    finally:
        try:
            report_path = persist_execution_report(report)
            print(f"execution_report {report_path}", flush=True)
        except Exception as persist_exc:
            print(f"failed to persist execution report: {persist_exc}", flush=True)


@app.route("/health", methods=["GET"])
def health():
    return "OK", 200


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))
