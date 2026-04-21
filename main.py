"""IBKR strategy runner for shared us_equity strategy profiles."""

import os
import threading
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

import google.auth
import pandas as pd
import requests
from flask import Flask, request

try:
    from google.cloud import compute_v1
except ImportError:
    compute_v1 = None

from application.cycle_result import coerce_strategy_cycle_result
from application.runtime_broker_adapters import build_runtime_broker_adapters
from application.runtime_composer import build_runtime_composer
from application.runtime_strategy_adapters import build_runtime_strategy_adapters
from application.rebalance_service import run_strategy_core as run_rebalance_cycle
from decision_mapper import map_strategy_decision
from entrypoints.cloud_run import is_market_open_today
from notifications.telegram import build_strategy_display_name, build_translator, send_telegram_message
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
)
from application.ibkr_order_execution import submit_order_intent
from application.execution_service import (
    check_order_submitted as application_check_order_submitted,
    execute_rebalance as application_execute_rebalance,
    get_market_prices as application_get_market_prices,
)
from application.paper_liquidation_service import execute_paper_liquidation
from runtime_logging import RuntimeLogContext, build_run_id, emit_runtime_log, extract_cloud_trace
from runtime_config_support import load_platform_runtime_settings, resolve_ib_gateway_ip_mode
from strategy_runtime import load_strategy_runtime

app = Flask(__name__)
ensure_event_loop = ibkr_ensure_event_loop
NEW_YORK_TZ = ZoneInfo("America/New_York")
STRATEGY_RUN_LOCK = threading.Lock()


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
    except Exception as exc:
        print(f"GCE resolve failed for {instance_name}: {exc}, using as hostname", flush=True)
    return instance_name


def get_ib_host():
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
    return 4002 if get_ib_gateway_mode() == "paper" else 4001


def get_ib_connect_timeout_seconds():
    raw_value = os.getenv("IBKR_CONNECT_TIMEOUT_SECONDS", "60")
    try:
        timeout_seconds = int(raw_value)
    except (TypeError, ValueError):
        print(f"Invalid IBKR_CONNECT_TIMEOUT_SECONDS={raw_value!r}; using 60", flush=True)
        return 60
    if timeout_seconds <= 0:
        print(f"Invalid IBKR_CONNECT_TIMEOUT_SECONDS={raw_value!r}; using 60", flush=True)
        return 60
    return timeout_seconds


def get_positive_int_env(name, default):
    raw_value = os.getenv(name, str(default))
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        print(f"Invalid {name}={raw_value!r}; using {default}", flush=True)
        return default
    if parsed <= 0:
        print(f"Invalid {name}={raw_value!r}; using {default}", flush=True)
        return default
    return parsed


def get_non_negative_float_env(name, default):
    raw_value = os.getenv(name, str(default))
    try:
        parsed = float(raw_value)
    except (TypeError, ValueError):
        print(f"Invalid {name}={raw_value!r}; using {default}", flush=True)
        return default
    if parsed < 0:
        print(f"Invalid {name}={raw_value!r}; using {default}", flush=True)
        return default
    return parsed


def _env_flag(name: str) -> bool:
    return str(os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


RUNTIME_SETTINGS = load_platform_runtime_settings(project_id_resolver=get_project_id)
IB_HOST = None
IB_PORT = get_ib_port()
IB_CLIENT_ID = RUNTIME_SETTINGS.ib_client_id
IB_CONNECT_TIMEOUT_SECONDS = get_ib_connect_timeout_seconds()
IB_CONNECT_ATTEMPTS = get_positive_int_env("IBKR_CONNECT_ATTEMPTS", 3)
IB_CONNECT_RETRY_DELAY_SECONDS = get_non_negative_float_env("IBKR_CONNECT_RETRY_DELAY_SECONDS", 5.0)
IB_CLIENT_ID_RETRY_OFFSET = get_positive_int_env("IBKR_CLIENT_ID_RETRY_OFFSET", 100)
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
REBALANCE_THRESHOLD_RATIO = 0.02
LIMIT_BUY_PREMIUM = 1.005
SELL_SETTLE_DELAY_SEC = 3
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
        "ib_connect_attempts": IB_CONNECT_ATTEMPTS,
        "ib_client_id_retry_offset": IB_CLIENT_ID_RETRY_OFFSET,
    },
)


def resolve_reporting_managed_symbols() -> tuple[str, ...]:
    configured_managed_symbols = STRATEGY_RUNTIME_CONFIG.get("managed_symbols")
    fallback_managed_symbols = tuple(dict.fromkeys([*RANKING_POOL, SAFE_HAVEN])) if RANKING_POOL else (SAFE_HAVEN,)
    return tuple(
        str(symbol)
        for symbol in (configured_managed_symbols or fallback_managed_symbols)
        if str(symbol or "").strip()
    )


def build_strategy_adapters():
    return build_runtime_strategy_adapters(
        strategy_runtime=STRATEGY_RUNTIME,
        strategy_profile=STRATEGY_PROFILE,
        translator=t,
        pacing_sec=HIST_DATA_PACING_SEC,
        resolve_run_as_of_date_fn=resolve_run_as_of_date,
        fetch_historical_price_series_fn=fetch_historical_price_series,
        fetch_historical_price_candles_fn=fetch_historical_price_candles,
        map_strategy_decision_fn=map_strategy_decision,
    )


def build_broker_adapters():
    return build_runtime_broker_adapters(
        host_resolver=get_ib_host,
        ib_port=IB_PORT,
        ib_client_id=IB_CLIENT_ID,
        connect_timeout_seconds=IB_CONNECT_TIMEOUT_SECONDS,
        connect_attempts=IB_CONNECT_ATTEMPTS,
        connect_retry_delay_seconds=IB_CONNECT_RETRY_DELAY_SECONDS,
        client_id_retry_offset=IB_CLIENT_ID_RETRY_OFFSET,
        ensure_event_loop_fn=ensure_event_loop,
        connect_ib_fn=ibkr_connect_ib,
        fetch_portfolio_snapshot_fn=fetch_portfolio_snapshot,
        fetch_quote_snapshots_fn=fetch_quote_snapshots,
        submit_order_intent_fn=submit_order_intent,
        application_get_market_prices_fn=application_get_market_prices,
        application_check_order_submitted_fn=application_check_order_submitted,
        application_execute_rebalance_fn=application_execute_rebalance,
        execute_paper_liquidation_fn=execute_paper_liquidation,
        translator=t,
        strategy_profile=STRATEGY_PROFILE,
        account_group=ACCOUNT_GROUP,
        service_name=SERVICE_NAME,
        account_ids=tuple(ACCOUNT_IDS),
        dry_run_only=RUNTIME_SETTINGS.dry_run_only,
        cash_reserve_ratio=CASH_RESERVE_RATIO,
        rebalance_threshold_ratio=REBALANCE_THRESHOLD_RATIO,
        limit_buy_premium=LIMIT_BUY_PREMIUM,
        sell_settle_delay_sec=SELL_SETTLE_DELAY_SEC,
        separator=SEPARATOR,
        strategy_display_name=strategy_display_name,
        sleep_fn=time.sleep,
        printer=print,
    )


def build_composer():
    return build_runtime_composer(
        service_name=SERVICE_NAME or os.getenv("K_SERVICE", "interactive-brokers-platform"),
        strategy_profile=STRATEGY_PROFILE,
        strategy_domain=RUNTIME_SETTINGS.strategy_domain,
        account_group=ACCOUNT_GROUP,
        project_id=PROJECT_ID,
        instance_name=RUNTIME_SETTINGS.ib_gateway_instance_name,
        account_ids=tuple(ACCOUNT_IDS),
        strategy_target_mode=RUNTIME_SETTINGS.strategy_target_mode,
        strategy_artifact_dir=RUNTIME_SETTINGS.strategy_artifact_dir,
        strategy_display_name=STRATEGY_DISPLAY_NAME,
        strategy_display_name_localized=strategy_display_name,
        managed_symbols=resolve_reporting_managed_symbols(),
        signal_source=STRATEGY_SIGNAL_SOURCE,
        status_icon=STRATEGY_STATUS_ICON,
        safe_haven=SAFE_HAVEN,
        dry_run_only=RUNTIME_SETTINGS.dry_run_only,
        strategy_config_source=FEATURE_RUNTIME_CONFIG_SOURCE,
        ib_gateway_host_resolver=get_ib_host,
        ib_gateway_port=IB_PORT,
        ib_gateway_mode=RUNTIME_SETTINGS.ib_gateway_mode,
        ib_gateway_ip_mode=RUNTIME_SETTINGS.ib_gateway_ip_mode,
        ib_client_id=IB_CLIENT_ID,
        ib_connect_timeout_seconds=IB_CONNECT_TIMEOUT_SECONDS,
        feature_snapshot_path=FEATURE_SNAPSHOT_PATH,
        feature_snapshot_manifest_path=FEATURE_SNAPSHOT_MANIFEST_PATH,
        strategy_config_path=FEATURE_RUNTIME_CONFIG_PATH,
        reconciliation_output_path=RECONCILIATION_OUTPUT_PATH,
        translator=t,
        separator=SEPARATOR,
        send_message=send_tg_message,
        connect_ib_fn=connect_ib,
        build_portfolio_snapshot_fn=build_portfolio_snapshot,
        compute_signals_fn=compute_signals,
        execute_rebalance_fn=execute_rebalance,
        run_id_builder=build_run_id,
        event_logger=emit_runtime_log,
        report_builder=build_runtime_report_base,
        report_persister=persist_runtime_report,
        trace_extractor=extract_cloud_trace,
        env_reader=os.getenv,
        printer=print,
    )


def send_tg_message(message):
    return send_telegram_message(
        message,
        token=TG_TOKEN,
        chat_id=TG_CHAT_ID,
        requests_module=requests,
    )


def publish_notification(*, detailed_text, compact_text):
    build_composer().build_notification_adapters().publish_cycle_notification(
        detailed_text=detailed_text,
        compact_text=compact_text,
    )


def connect_ib():
    return build_broker_adapters().connect_ib()


def log_runtime_event(log_context, event, **fields):
    return build_composer().build_reporting_adapters().log_event(log_context, event, **fields)


def build_execution_report(log_context):
    return build_composer().build_reporting_adapters().build_report(log_context)


def persist_execution_report(report):
    return build_composer().build_reporting_adapters().persist_execution_report(report)


def build_request_log_context():
    return build_composer().build_reporting_adapters().build_log_context(
        trace_header=request.headers.get("X-Cloud-Trace-Context"),
    )


def resolve_run_as_of_date() -> pd.Timestamp:
    explicit = os.getenv("IBKR_RUN_AS_OF_DATE")
    if explicit:
        return pd.Timestamp(explicit).normalize()
    return pd.Timestamp(datetime.now(NEW_YORK_TZ).date())


def get_historical_close(ib, symbol, duration="2 Y", bar_size="1 day"):
    return build_strategy_adapters().get_historical_close(
        ib,
        symbol,
        duration=duration,
        bar_size=bar_size,
    )


def get_historical_candles(ib, symbol, duration="2 Y", bar_size="1 day"):
    return build_strategy_adapters().get_historical_candles(
        ib,
        symbol,
        duration=duration,
        bar_size=bar_size,
    )


def compute_signals(ib, current_holdings):
    return build_strategy_adapters().compute_signals(ib, current_holdings)


def get_current_portfolio(ib):
    return build_broker_adapters().get_current_portfolio(ib)


def build_portfolio_snapshot(ib):
    return build_broker_adapters().build_portfolio_snapshot(
        ib,
        get_current_portfolio_fallback=get_current_portfolio,
    )


def get_market_prices(ib, symbols):
    return build_broker_adapters().get_market_prices(ib, symbols)


def check_order_submitted(report):
    return build_broker_adapters().check_order_submitted(report)


def execute_rebalance(
    ib,
    target_weights,
    positions,
    account_values,
    *,
    strategy_symbols=None,
    signal_metadata=None,
):
    return build_broker_adapters().execute_rebalance(
        ib,
        target_weights,
        positions,
        account_values,
        strategy_symbols=strategy_symbols,
        signal_metadata=signal_metadata,
    )


def _format_liquidation_orders(orders) -> str:
    return build_broker_adapters().format_liquidation_orders(orders)


def run_paper_liquidation_cycle():
    if RUNTIME_SETTINGS.ib_gateway_mode != "paper":
        raise RuntimeError("IBKR_PAPER_LIQUIDATE_ONLY is only allowed when ib_gateway_mode=paper")
    return build_broker_adapters().run_paper_liquidation_cycle(
        connect_ib_fn=connect_ib,
        get_current_portfolio_fn=get_current_portfolio,
        publish_notification_fn=publish_notification,
    )


def run_strategy_core():
    if PAPER_LIQUIDATE_ONLY:
        return run_paper_liquidation_cycle()
    composer = build_composer()
    return run_rebalance_cycle(
        runtime=composer.build_rebalance_runtime(),
        config=composer.build_rebalance_config(),
    )


@app.route("/", methods=["POST", "GET"])
def handle_request():
    if request.method == "GET":
        return "OK - use POST to execute strategy", 200

    log_context = build_request_log_context()
    report = build_execution_report(log_context)
    lock_acquired = STRATEGY_RUN_LOCK.acquire(blocking=False)
    try:
        log_runtime_event(
            log_context,
            "strategy_cycle_received",
            message="Received strategy execution request",
            http_method=request.method,
        )
        if not lock_acquired:
            log_runtime_event(
                log_context,
                "strategy_cycle_already_running",
                message="Another strategy execution is already running; skip overlapping request",
                severity="WARNING",
            )
            finalize_runtime_report(
                report,
                status="skipped",
                diagnostics={"skip_reason": "already_running"},
            )
            return "Already Running", 200
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
        cycle_result = coerce_strategy_cycle_result(run_strategy_core())
        execution_summary = dict(cycle_result.execution_summary or {})
        reconciliation_record = dict(cycle_result.reconciliation_record or {})
        finalize_runtime_report(
            report,
            status="ok",
            summary={
                "result": cycle_result.result,
                "execution_status": execution_summary.get("execution_status") or reconciliation_record.get("execution_status"),
                "no_op_reason": execution_summary.get("no_op_reason") or reconciliation_record.get("no_op_reason"),
                "orders_submitted_count": len(
                    execution_summary.get("orders_submitted")
                    or reconciliation_record.get("orders_submitted")
                    or ()
                ),
                "orders_skipped_count": len(
                    execution_summary.get("orders_skipped")
                    or reconciliation_record.get("orders_skipped")
                    or ()
                ),
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
                "result": cycle_result.result,
                "price_source_mode": execution_summary.get("price_source_mode") or reconciliation_record.get("price_source_mode"),
                "snapshot_price_fallback_symbols": execution_summary.get("snapshot_price_fallback_symbols")
                or reconciliation_record.get("snapshot_price_fallback_symbols")
                or [],
            },
            artifacts={
                "reconciliation_record_path": cycle_result.reconciliation_record_path,
            },
        )
        log_runtime_event(
            log_context,
            "strategy_cycle_completed",
            message="Strategy execution completed",
            result=cycle_result.result,
        )
        return cycle_result.result, 200
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
        publish_notification(detailed_text=error_msg, compact_text=error_msg)
        return "Error", 500
    finally:
        if lock_acquired:
            STRATEGY_RUN_LOCK.release()
        try:
            report_path = persist_execution_report(report)
            print(f"execution_report {report_path}", flush=True)
        except Exception as persist_exc:
            print(f"failed to persist execution report: {persist_exc}", flush=True)


@app.route("/health", methods=["GET"])
def health():
    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
