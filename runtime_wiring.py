"""Runtime configuration, adapters, and composer wiring."""

from __future__ import annotations


import sys

_FALLBACK_MODULE_NAME = __name__


class _MainModuleProxy:
    """Prefer ``main.<name>`` after re-exports; fall back during module import."""

    def __getattr__(self, name):
        main_mod = sys.modules.get("main")
        if main_mod is not None and name in getattr(main_mod, "__dict__", {}):
            return main_mod.__dict__[name]
        return getattr(sys.modules[_FALLBACK_MODULE_NAME], name)


main = _MainModuleProxy()

import hashlib
import json
import importlib
import os
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from flask import Flask, request

try:
    from quant_platform_kit.cloud import get_compute_discovery
except ImportError:
    get_compute_discovery = None

from application.cycle_result import coerce_strategy_cycle_result
from application.execution_receipt_adapter import (
    attach_cycle_execution_receipt,
    attach_terminal_fallback_execution_receipt,
)
from application.broker_reconciliation import (
    IBKRReconciliationReadError,
    build_reconciliation_candidate,
    collect_read_only_reconciliation_observations,
)
from application.reconciliation_reporting import (
    build_persistable_reconciliation_report,
    normalize_reconciliation_request_id,
    normalize_reconciliation_scheduler_job_sha256,
)
from application.runtime_broker_adapters import (
    IBKRGatewayUnavailableError,
    IBKRTradingPermissionError,
    build_runtime_broker_adapters,
)
from application.runtime_composer import build_runtime_composer
from application.runtime_deadline import (
    RuntimeDeadlineExceeded,
    classify_runtime_deadline_qualification_caller,
    classify_runtime_deadline_stage,
    enforce_runtime_deadline,
    recycle_current_process_after_response,
)
from application import signal_provenance
from application.runtime_strategy_adapters import (
    build_runtime_strategy_adapters,
    fetch_yfinance_historical_candles,
)
from application.rebalance_service import run_strategy_core as run_rebalance_cycle
from application.signal_snapshot import build_signal_snapshot
from decision_mapper import map_strategy_decision
from entrypoints.cloud_run import is_market_open_now
from runtime_execution_policy import (
    IBKR_FRACTIONAL_EQUITY_API_UNSUPPORTED_SKIP_REASON,
    dca_execution_unsupported_reason,
)
from notifications.telegram import build_strategy_display_name, build_translator, send_telegram_message
from notifications.unified_adapter import with_run as with_notification_run
from quant_platform_kit.notifications.strategy_plugin_alerts import (
    StrategyPluginAlertStateSettings,
    build_strategy_plugin_alert_context_label as build_alert_context_label,
    publish_strategy_plugin_alerts as dispatch_strategy_plugin_alerts,
)
from quant_platform_kit.common.runtime_assembly import build_runtime_assembly
from quant_platform_kit.common.execution_commands import build_execution_command_store_from_env
from quant_platform_kit.common.strategy_release import build_runtime_loaded_receipt
from quant_platform_kit.common.runtime_reports import (
    append_runtime_report_error,
    build_runtime_report_base,
    finalize_runtime_report,
    persist_runtime_report,
)
from quant_platform_kit.common.strategy_plugins import (
    build_strategy_plugin_report_payload,
    load_configured_strategy_plugin_signals,
    parse_strategy_plugin_mounts,
)
from quant_platform_kit.ibkr import (
    connect_ib as ibkr_connect_ib,
    ensure_event_loop as ibkr_ensure_event_loop,
    fetch_historical_price_candles,
    fetch_historical_price_series,
    fetch_quote_snapshots as qpk_fetch_quote_snapshots,
)
from application.ibkr_market_data import fetch_quote_snapshots_with_expected_error_summary
from application.ibkr_order_execution import probe_order_write_access, submit_order_intent
from application.monitor_dispatcher import (
    dispatch_due_monitor_targets,
    load_monitor_targets,
    lookback_minutes_from_env,
    max_workers_from_env,
    timeout_seconds_from_env,
)
from application.ibkr_portfolio import (
    IBKRPortfolioSnapshotUnavailableError,
    fetch_reconciled_paper_portfolio_snapshot,
    fetch_portfolio_snapshot,
)
from application.execution_service import (
    _SettledCashDeadlineIB,
    check_order_submitted as application_check_order_submitted,
    execute_rebalance as application_execute_rebalance,
    get_market_prices as application_get_market_prices,
)
from application.paper_liquidation_service import execute_paper_liquidation
from application.paper_execution_admission import resolve_paper_execution_admission_enabled
from application.paper_execution_command_consumer import (
    consume_due_paper_execution_commands,
    resolve_paper_execution_command_consumer_enabled,
)
from runtime_logging import build_run_id, emit_runtime_log, extract_cloud_trace
from runtime_config_support import (
    EXECUTION_BACKEND_GATEWAY,
    load_platform_runtime_settings,
    resolve_ib_gateway_ip_mode,
)
from strategy_runtime import load_strategy_runtime
from application.http_routes import (
    _dispatch_local_monitor,
    _handle_monitor_dispatch,
    _handle_probe,
    _handle_reconciliation,
    _handle_live_permission_probe,
    health_impl,
)

EXPECTED_EXECUTION_GUARD_REASON_PREFIXES = ('pending_orders_detected:', 'same_day_fills_detected:', 'same_day_execution_locked:')

def get_project_id():
    try:
        from quant_platform_kit.cloud import get_deployment_context
        return get_deployment_context().project_id
    except Exception:
        return main.os.getenv('GOOGLE_CLOUD_PROJECT')

def get_ib_gateway_ip_mode():
    raw_value = main.os.getenv('IB_GATEWAY_IP_MODE')
    if raw_value is None and 'RUNTIME_SETTINGS' in globals():
        raw_value = main.RUNTIME_SETTINGS.ib_gateway_ip_mode
    return main.resolve_ib_gateway_ip_mode(raw_value, logger=lambda message: print(message, flush=True))

def resolve_gce_instance_ip(instance_name, zone):
    gateway_project = getattr(main.RUNTIME_SETTINGS, 'ib_gateway_project_id', None)
    if not main.get_compute_discovery:
        if gateway_project:
            raise main.IBKRGatewayUnavailableError(f'GCE Gateway discovery unavailable for configured project {gateway_project}')
        print(f'quant_platform_kit.cloud not installed, using {instance_name} as host directly', flush=True)
        return instance_name
    try:
        ip_mode = main.get_ib_gateway_ip_mode()
        project = gateway_project or main.get_project_id()
        host = main.get_compute_discovery().resolve_instance_ip(instance_name, zone, project_id=project, prefer_internal=ip_mode == 'internal')
        if gateway_project and (not host or host == instance_name):
            raise ValueError('GCE Gateway discovery returned no address')
        return host
    except Exception as exc:
        if gateway_project:
            raise main.IBKRGatewayUnavailableError(f'GCE Gateway address unavailable in configured project {gateway_project}') from None
        print(f'GCE resolve failed for {instance_name}: {exc}, using as hostname', flush=True)
    return instance_name

def get_ib_host():
    global IB_HOST
    if IB_HOST:
        return IB_HOST
    host = main.RUNTIME_SETTINGS.ib_gateway_instance_name
    zone = main.RUNTIME_SETTINGS.ib_gateway_zone
    if getattr(main.RUNTIME_SETTINGS, 'ib_gateway_project_id', None) and (not zone):
        raise main.IBKRGatewayUnavailableError('GCE Gateway zone is required when a Gateway project is configured')
    if zone:
        host = main.resolve_gce_instance_ip(host, zone)
    IB_HOST = host
    return host

def refresh_ib_host():
    """Refresh a zoned gateway address while retaining last-known-good state."""
    global IB_HOST
    instance_name = main.RUNTIME_SETTINGS.ib_gateway_instance_name
    zone = main.RUNTIME_SETTINGS.ib_gateway_zone
    if not zone:
        return main.get_ib_host()
    cached_host = IB_HOST
    try:
        resolved_host = main.resolve_gce_instance_ip(instance_name, zone)
    except main.IBKRGatewayUnavailableError:
        IB_HOST = None
        raise
    except Exception as exc:
        print(f'GCE host refresh failed for {instance_name}: {exc}', flush=True)
        resolved_host = cached_host or instance_name
    if cached_host and resolved_host == instance_name:
        resolved_host = cached_host
    IB_HOST = resolved_host
    return resolved_host

def get_ib_gateway_mode():
    return main.RUNTIME_SETTINGS.ib_gateway_mode

def get_ib_port():
    configured_port = getattr(main.RUNTIME_SETTINGS, 'ib_gateway_port', None)
    if configured_port is not None:
        return configured_port
    return 4002 if main.get_ib_gateway_mode() == 'paper' else 4001

def get_ib_connect_timeout_seconds():
    raw_value = main.os.getenv('IBKR_CONNECT_TIMEOUT_SECONDS', '60')
    try:
        timeout_seconds = int(raw_value)
    except (TypeError, ValueError):
        print(f'Invalid IBKR_CONNECT_TIMEOUT_SECONDS={raw_value!r}; using 60', flush=True)
        return 60
    if timeout_seconds <= 0:
        print(f'Invalid IBKR_CONNECT_TIMEOUT_SECONDS={raw_value!r}; using 60', flush=True)
        return 60
    return timeout_seconds

def get_positive_int_env(name, default):
    raw_value = main.os.getenv(name, str(default))
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        print(f'Invalid {name}={raw_value!r}; using {default}', flush=True)
        return default
    if parsed <= 0:
        print(f'Invalid {name}={raw_value!r}; using {default}', flush=True)
        return default
    return parsed

def get_non_negative_float_env(name, default):
    raw_value = main.os.getenv(name, str(default))
    try:
        parsed = float(raw_value)
    except (TypeError, ValueError):
        print(f'Invalid {name}={raw_value!r}; using {default}', flush=True)
        return default
    if parsed < 0:
        print(f'Invalid {name}={raw_value!r}; using {default}', flush=True)
        return default
    return parsed

def _env_flag(name: str) -> bool:
    return str(main.os.getenv(name) or '').strip().lower() in {'1', 'true', 'yes', 'on'}
RUNTIME_SETTINGS = load_platform_runtime_settings(project_id_resolver=get_project_id)
IB_HOST = None
IB_PORT = get_ib_port()
IB_CLIENT_ID = RUNTIME_SETTINGS.ib_client_id
IB_CONNECT_TIMEOUT_SECONDS = get_ib_connect_timeout_seconds()
IB_CONNECT_ATTEMPTS = get_positive_int_env('IBKR_CONNECT_ATTEMPTS', 3)
IB_CONNECT_RETRY_DELAY_SECONDS = get_non_negative_float_env('IBKR_CONNECT_RETRY_DELAY_SECONDS', 5.0)
IB_CLIENT_ID_RETRY_OFFSET = get_positive_int_env('IBKR_CLIENT_ID_RETRY_OFFSET', 100)
IBKR_DRY_RUN_DEADLINE_SECONDS = get_positive_int_env('IBKR_DRY_RUN_DEADLINE_SECONDS', 105)
IBKR_DEADLINE_REPORT_GRACE_SECONDS = get_positive_int_env('IBKR_DEADLINE_REPORT_GRACE_SECONDS', 5)
STRATEGY_PROFILE = RUNTIME_SETTINGS.strategy_profile
STRATEGY_DISPLAY_NAME = RUNTIME_SETTINGS.strategy_display_name

def _normalize_plugin_mounts_strategy(raw_mounts_json: str | None) -> str | None:
    if not raw_mounts_json:
        return raw_mounts_json
    try:
        mounts = main.json.loads(raw_mounts_json)
    except main.json.JSONDecodeError:
        return raw_mounts_json
    if not isinstance(mounts, dict):
        return raw_mounts_json
    plugins = mounts.get('strategy_plugins')
    if not isinstance(plugins, list):
        return raw_mounts_json
    changed = False
    for entry in plugins:
        if not isinstance(entry, dict):
            continue
        old = entry.get('strategy')
        if old and old != main.STRATEGY_PROFILE:
            entry['strategy'] = main.STRATEGY_PROFILE
            changed = True
            print(f'[config-sync] Plugin mount strategy corrected: {old} → {main.STRATEGY_PROFILE}', flush=True)
    return main.json.dumps(mounts, ensure_ascii=False) if changed else raw_mounts_json
if hasattr(RUNTIME_SETTINGS, 'strategy_plugin_mounts_json'):
    normalized = _normalize_plugin_mounts_strategy(RUNTIME_SETTINGS.strategy_plugin_mounts_json)
    if normalized != RUNTIME_SETTINGS.strategy_plugin_mounts_json:
        object.__setattr__(RUNTIME_SETTINGS, 'strategy_plugin_mounts_json', normalized)
for _env_name in ('MONITOR_DISPATCH_TARGETS_JSON', 'IBKR_MONITOR_DISPATCH_TARGETS_JSON'):
    _raw = os.environ.get(_env_name)
    if _raw:
        try:
            _targets = json.loads(_raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(_targets, dict):
            continue
        _entries = _targets.get('targets')
        if not isinstance(_entries, list):
            continue
        _changed = False
        for _entry in _entries:
            if isinstance(_entry, dict):
                _old = _entry.get('strategy_profile')
                if _old and _old != STRATEGY_PROFILE:
                    _entry['strategy_profile'] = STRATEGY_PROFILE
                    _changed = True
                    print(f'[config-sync] {_env_name} strategy_profile corrected: {_old} → {STRATEGY_PROFILE}', flush=True)
        if _changed:
            os.environ[_env_name] = json.dumps(_targets, ensure_ascii=False)
ACCOUNT_GROUP = RUNTIME_SETTINGS.account_group
SERVICE_NAME = RUNTIME_SETTINGS.service_name
ACCOUNT_IDS = RUNTIME_SETTINGS.account_ids
PROJECT_ID = RUNTIME_SETTINGS.project_id
EXECUTION_BACKEND = RUNTIME_SETTINGS.execution_backend
QUANTCONNECT_PROJECT_ID = getattr(RUNTIME_SETTINGS, 'quantconnect_project_id', None)
QUANTCONNECT_NODE_ID = getattr(RUNTIME_SETTINGS, 'quantconnect_node_id', None)
MARKET = RUNTIME_SETTINGS.market
MARKET_CALENDAR = RUNTIME_SETTINGS.market_calendar
MARKET_CURRENCY = RUNTIME_SETTINGS.market_currency
MARKET_DATA_SYMBOL_SUFFIX = RUNTIME_SETTINGS.market_data_symbol_suffix
MARKET_EXCHANGE = RUNTIME_SETTINGS.market_exchange
MARKET_TIMEZONE = RUNTIME_SETTINGS.market_timezone
STRATEGY_RUNTIME = load_strategy_runtime(STRATEGY_PROFILE, runtime_settings=RUNTIME_SETTINGS, logger=lambda message: print(message, flush=True))
STRATEGY_ENTRYPOINT = STRATEGY_RUNTIME.entrypoint
STRATEGY_SIGNAL_SOURCE = 'feature_snapshot' if 'feature_snapshot' in STRATEGY_RUNTIME.required_inputs else 'market_data'
STRATEGY_STATUS_ICON = STRATEGY_RUNTIME.status_icon
SIGNAL_EFFECTIVE_AFTER_TRADING_DAYS = getattr(getattr(STRATEGY_RUNTIME.runtime_adapter, 'runtime_policy', None), 'signal_effective_after_trading_days', None)
FEATURE_RUNTIME_PARAMETERS = dict(STRATEGY_RUNTIME.runtime_config)
STRATEGY_RUNTIME_CONFIG = dict(STRATEGY_RUNTIME.merged_runtime_config)
SAFE_HAVEN = str(STRATEGY_RUNTIME_CONFIG.get('safe_haven') or 'BIL')
RANKING_POOL = list(STRATEGY_RUNTIME_CONFIG.get('ranking_pool', ()))
CANARY_ASSETS = list(STRATEGY_RUNTIME_CONFIG.get('canary_assets', ()))
TOP_N = STRATEGY_RUNTIME_CONFIG.get('top_n')
SMA_PERIOD = int(STRATEGY_RUNTIME_CONFIG.get('sma_period', 200))
CANARY_BAD_THRESHOLD = STRATEGY_RUNTIME_CONFIG.get('canary_bad_threshold')
REBALANCE_MONTHS = STRATEGY_RUNTIME_CONFIG.get('rebalance_months')
FEATURE_SNAPSHOT_PATH = RUNTIME_SETTINGS.feature_snapshot_path
FEATURE_SNAPSHOT_MANIFEST_PATH = RUNTIME_SETTINGS.feature_snapshot_manifest_path
FEATURE_RUNTIME_CONFIG_PATH = STRATEGY_RUNTIME_CONFIG.get('runtime_config_path') or RUNTIME_SETTINGS.strategy_config_path
FEATURE_RUNTIME_CONFIG_SOURCE = STRATEGY_RUNTIME_CONFIG.get('runtime_config_source') or RUNTIME_SETTINGS.strategy_config_source
RECONCILIATION_OUTPUT_PATH = RUNTIME_SETTINGS.reconciliation_output_path
PAPER_LIQUIDATE_ONLY = _env_flag('IBKR_PAPER_LIQUIDATE_ONLY')
TG_TOKEN = RUNTIME_SETTINGS.tg_token
TG_CHAT_ID = RUNTIME_SETTINGS.tg_chat_id
from quant_platform_kit.notifications.cycle_channel import resolve_cycle_channel_and_url
NOTIFY_LANG = RUNTIME_SETTINGS.notify_lang
_NOTIFICATION_CHANNEL, _NOTIFICATION_WEBHOOK_URL = resolve_cycle_channel_and_url(explicit_channel=RUNTIME_SETTINGS.notification_channel, telegram_token=TG_TOKEN, telegram_chat_id=TG_CHAT_ID, wecom_url=RUNTIME_SETTINGS.wecom_webhook_url, dingtalk_url=RUNTIME_SETTINGS.dingtalk_webhook_url, feishu_url=RUNTIME_SETTINGS.feishu_webhook_url, serverchan_url=RUNTIME_SETTINGS.serverchan_webhook_url)
CASH_RESERVE_RATIO = STRATEGY_RUNTIME.cash_reserve_ratio
CASH_ONLY_EXECUTION = getattr(RUNTIME_SETTINGS, 'cash_only_execution', True)
CASH_RESERVE_FLOOR_USD = getattr(STRATEGY_RUNTIME, 'cash_reserve_floor_usd', 0.0)
REBALANCE_THRESHOLD_RATIO = STRATEGY_RUNTIME.rebalance_threshold_ratio
LIMIT_BUY_PREMIUM = 1.005
DEFAULT_LIMIT_BUY_PREMIUM_BY_SYMBOL = {'SOXL': 1.015, 'TQQQ': 1.01}
SELL_SETTLE_DELAY_SEC = 3
HIST_DATA_PACING_SEC = 0.5
SEPARATOR = '━━━━━━━━━━━━━━━━━━'

def _load_limit_buy_premium_by_symbol(*env_names: str) -> dict[str, float]:
    raw_value = ''
    for env_name in env_names:
        value = main.os.getenv(env_name)
        if value and value.strip():
            raw_value = value.strip()
            break
    if not raw_value:
        return dict(main.DEFAULT_LIMIT_BUY_PREMIUM_BY_SYMBOL)
    try:
        payload = main.json.loads(raw_value)
    except main.json.JSONDecodeError as exc:
        raise ValueError(f'Invalid limit buy premium map JSON: {raw_value!r}') from exc
    if not isinstance(payload, dict):
        raise ValueError('Limit buy premium map must be a JSON object keyed by symbol.')
    parsed: dict[str, float] = {}
    for symbol, premium in payload.items():
        symbol_text = str(symbol or '').strip().upper()
        if not symbol_text:
            continue
        premium_value = float(premium)
        if premium_value <= 0.0:
            raise ValueError(f'Limit buy premium for {symbol_text} must be positive.')
        parsed[symbol_text] = premium_value
    return parsed
LIMIT_BUY_PREMIUM_BY_SYMBOL = _load_limit_buy_premium_by_symbol('IBKR_LIMIT_BUY_PREMIUM_BY_SYMBOL_JSON', 'LIMIT_BUY_PREMIUM_BY_SYMBOL_JSON')

def t(key, **kwargs):
    return main.build_translator(main.NOTIFY_LANG)(key, **kwargs)
strategy_display_name = build_strategy_display_name(t, NOTIFY_LANG)(STRATEGY_PROFILE, fallback_name=STRATEGY_DISPLAY_NAME, metadata=RUNTIME_SETTINGS.strategy_metadata)
RUNTIME_LOG_CONTEXT = build_runtime_assembly(platform='interactive_brokers', deploy_target='cloud_run', service_name=SERVICE_NAME or os.getenv('K_SERVICE', 'interactive-brokers-platform'), strategy_profile=STRATEGY_PROFILE, runtime_target=RUNTIME_SETTINGS.runtime_target, account_scope=ACCOUNT_GROUP, account_group=ACCOUNT_GROUP, project_id=PROJECT_ID, instance_name=RUNTIME_SETTINGS.ib_gateway_instance_name, extra_context_fields={'account_ids': list(ACCOUNT_IDS), 'strategy_target_mode': RUNTIME_SETTINGS.strategy_target_mode, 'strategy_artifact_dir': RUNTIME_SETTINGS.strategy_artifact_dir, 'strategy_display_name': STRATEGY_DISPLAY_NAME, 'strategy_display_name_localized': strategy_display_name, 'execution_backend': EXECUTION_BACKEND, 'ib_connect_attempts': IB_CONNECT_ATTEMPTS, 'ib_client_id_retry_offset': IB_CLIENT_ID_RETRY_OFFSET, 'quantconnect_project_id': QUANTCONNECT_PROJECT_ID, 'quantconnect_node_id': QUANTCONNECT_NODE_ID, 'market': MARKET, 'market_calendar': MARKET_CALENDAR, 'market_currency': MARKET_CURRENCY, 'market_data_symbol_suffix': MARKET_DATA_SYMBOL_SUFFIX, 'market_exchange': MARKET_EXCHANGE, 'market_timezone': MARKET_TIMEZONE}).build_log_context(run_id='')

def resolve_reporting_managed_symbols() -> tuple[str, ...]:
    configured_managed_symbols = main.STRATEGY_RUNTIME_CONFIG.get('managed_symbols')
    fallback_managed_symbols = tuple(dict.fromkeys([*main.RANKING_POOL, main.SAFE_HAVEN])) if main.RANKING_POOL else (main.SAFE_HAVEN,)
    return tuple((str(symbol) for symbol in configured_managed_symbols or fallback_managed_symbols if str(symbol or '').strip()))

def build_strategy_adapters():
    return main.build_runtime_strategy_adapters(strategy_runtime=main.STRATEGY_RUNTIME, strategy_profile=main.STRATEGY_PROFILE, translator=main.t, pacing_sec=main.HIST_DATA_PACING_SEC, resolve_run_as_of_date_fn=main.resolve_run_as_of_date, fetch_historical_price_series_fn=main.fetch_market_historical_price_series, fetch_historical_price_candles_fn=main.fetch_market_historical_price_candles, map_strategy_decision_fn=main.map_strategy_decision, fallback_historical_candles_fn=main.fetch_market_fallback_historical_candles, build_strategy_plugin_report_payload_fn=main.build_strategy_plugin_report_payload, load_configured_strategy_plugin_signals_fn=main.load_configured_strategy_plugin_signals, parse_strategy_plugin_mounts_fn=main.parse_strategy_plugin_mounts)

def format_market_data_symbol(symbol: str) -> str:
    value = str(symbol or '').strip().upper()
    if not value or not main.MARKET_DATA_SYMBOL_SUFFIX or '.' in value:
        return value
    return f'{value}{main.MARKET_DATA_SYMBOL_SUFFIX}'

def fetch_market_historical_price_series(ib, symbol, **kwargs):
    return main.fetch_historical_price_series(ib, str(symbol).strip().upper(), exchange=main.MARKET_EXCHANGE, currency=main.MARKET_CURRENCY, **kwargs)

def fetch_market_historical_price_candles(ib, symbol, **kwargs):
    return main.fetch_historical_price_candles(ib, str(symbol).strip().upper(), exchange=main.MARKET_EXCHANGE, currency=main.MARKET_CURRENCY, **kwargs)

def fetch_market_fallback_historical_candles(symbol, **kwargs):
    return main.fetch_yfinance_historical_candles(main.format_market_data_symbol(symbol), **kwargs)

def fetch_market_quote_snapshots(ib, symbols, **kwargs):
    return main.fetch_quote_snapshots_with_expected_error_summary(ib, symbols, fetch_quote_snapshots=main.qpk_fetch_quote_snapshots, exchange=main.MARKET_EXCHANGE, currency=main.MARKET_CURRENCY, **kwargs)

def submit_market_order_intent(ib, order_intent, *, account_execution_session=None, execution_grant=None, **kwargs):
    from quant_platform_kit.common import execution_state as contract
    session_type = getattr(contract, 'AccountSubmissionSession', ())
    if not isinstance(account_execution_session, session_type) or account_execution_session.broker != 'ibkr' or account_execution_session.contract_version != 'account_submission.v1':
        raise RuntimeError('IBKR account submission session/contract required')
    if isinstance(ib, main._SettledCashDeadlineIB) and execution_grant is None:
        prepared = ib.prepare_intent(order_intent, exchange=main.MARKET_EXCHANGE, currency=main.MARKET_CURRENCY)
        if prepared is None:
            from quant_platform_kit.common.models import ExecutionReport
            return ExecutionReport(symbol=order_intent.symbol, side=order_intent.side, quantity=order_intent.quantity, status='rejected', raw_payload={'detail': 'settled_cash_changed_before_reservation'})
        order_intent = prepared
        execution_grant = account_execution_session.reserve(order_intent, new_risk=True)

    def submit_with_observation():
        from application.settlement_reporting import settlement_enabled

        def submit(broker):
            return main.submit_order_intent(broker, order_intent, stock_exchange=main.MARKET_EXCHANGE, stock_currency=main.MARKET_CURRENCY, **kwargs)
        if not settlement_enabled():
            return submit(ib)
        from application.order_settlement import SettlementStore, track_submission
        from notifications.unified_adapter import _RUN
        source = _RUN.get() or {}
        wait = float(main.os.getenv('IBKR_ORDER_SETTLEMENT_WAIT_SECONDS', '5'))
        if not 0 <= wait <= 30:
            raise ValueError('settlement_wait_invalid')
        return track_submission(ib, order_intent, submit=submit, store=SettlementStore(account_execution_session.store, account_id=account_execution_session.account_id, profile=account_execution_session.profile), attempt_id=execution_grant.attempt_id, source_run_id=source.get('run_id'), wait_seconds=wait, observation_ib=ib._ib if isinstance(ib, main._SettledCashDeadlineIB) else ib, source_started_at=source.get('started_at'))
    return account_execution_session.dispatch(execution_grant, order_intent, submit_with_observation)

def probe_market_order_write_access(ib):
    """Probe order-write access with a low-notional safe-haven what-if buy.

    Do not use the first managed growth symbol (e.g. SOXL): one share can exceed
    small-account initial margin and emit Error 201 even though write access is OK.
    """
    if len(main.ACCOUNT_IDS) != 1:
        raise RuntimeError('IBKR what-if permission probe requires exactly one account_id')
    probe_symbol = str(main.SAFE_HAVEN or 'BIL').strip().upper() or 'BIL'
    return main.probe_order_write_access(ib, symbol=probe_symbol, account_id=main.ACCOUNT_IDS[0], stock_exchange=main.MARKET_EXCHANGE, stock_currency=main.MARKET_CURRENCY)

def fetch_market_portfolio_snapshot(ib, **kwargs):
    return main.fetch_portfolio_snapshot(ib, currency=main.MARKET_CURRENCY, cash_only_execution=main.CASH_ONLY_EXECUTION, **kwargs)

def build_broker_adapters(*, dry_run_only_override: bool | None=None):
    effective_dry_run_only = main.RUNTIME_SETTINGS.dry_run_only if dry_run_only_override is None else bool(dry_run_only_override)
    effective_execution_mode = 'dry_run' if effective_dry_run_only else main.RUNTIME_SETTINGS.ib_gateway_mode
    expected_strategy_release = main.RUNTIME_SETTINGS.runtime_target.strategy_release if main.RUNTIME_SETTINGS.runtime_target is not None else None

    def connect_runtime_ib(host, port, client_id, *, timeout):
        return main.ibkr_connect_ib(host, port, client_id, timeout=timeout, readonly=effective_dry_run_only)
    return main.build_runtime_broker_adapters(host_resolver=main.get_ib_host, refresh_host_fn=main.refresh_ib_host, trading_permission_probe_fn=main.probe_market_order_write_access, ib_port=main.IB_PORT, ib_client_id=main.IB_CLIENT_ID, connect_timeout_seconds=main.IB_CONNECT_TIMEOUT_SECONDS, connect_attempts=main.IB_CONNECT_ATTEMPTS, connect_retry_delay_seconds=main.IB_CONNECT_RETRY_DELAY_SECONDS, client_id_retry_offset=main.IB_CLIENT_ID_RETRY_OFFSET, ensure_event_loop_fn=main.ensure_event_loop, connect_ib_fn=connect_runtime_ib, fetch_portfolio_snapshot_fn=main.fetch_market_portfolio_snapshot, fetch_quote_snapshots_fn=main.fetch_market_quote_snapshots, submit_order_intent_fn=main.submit_market_order_intent, application_get_market_prices_fn=main.application_get_market_prices, application_check_order_submitted_fn=main.application_check_order_submitted, application_execute_rebalance_fn=main.application_execute_rebalance, execute_paper_liquidation_fn=main.execute_paper_liquidation, translator=main.t, strategy_profile=main.STRATEGY_PROFILE, account_group=main.ACCOUNT_GROUP, service_name=main.SERVICE_NAME, account_ids=tuple(main.ACCOUNT_IDS), dry_run_only=effective_dry_run_only, cash_only_execution=main.CASH_ONLY_EXECUTION, cash_reserve_ratio=main.CASH_RESERVE_RATIO, cash_reserve_floor_usd=main.CASH_RESERVE_FLOOR_USD, rebalance_threshold_ratio=main.REBALANCE_THRESHOLD_RATIO, limit_buy_premium=main.LIMIT_BUY_PREMIUM, limit_buy_premium_by_symbol=main.LIMIT_BUY_PREMIUM_BY_SYMBOL, quantity_step=main.RUNTIME_SETTINGS.quantity_step, min_order_notional=main.RUNTIME_SETTINGS.min_order_notional, safe_haven_cash_substitute_threshold_usd=main.RUNTIME_SETTINGS.safe_haven_cash_substitute_threshold_usd, sell_settle_delay_sec=main.SELL_SETTLE_DELAY_SEC, separator=main.SEPARATOR, strategy_display_name=main.strategy_display_name, sleep_fn=main.time.sleep, market_currency=main.MARKET_CURRENCY, execution_mode=effective_execution_mode, paper_execution_admission_enabled=main.resolve_paper_execution_admission_enabled(env_reader=main.os.getenv, dry_run_only=effective_dry_run_only, execution_mode=effective_execution_mode, execution_environment=main.RUNTIME_SETTINGS.runtime_target.execution_environment if main.RUNTIME_SETTINGS.runtime_target is not None else None), runtime_release_receipt=main.build_runtime_loaded_receipt(strategy_release=expected_strategy_release), expected_strategy_release=expected_strategy_release, printer=print)

def build_composer(*, dry_run_only_override: bool | None=None, strategy_plugin_signals=()):
    effective_dry_run_only = main.RUNTIME_SETTINGS.dry_run_only if dry_run_only_override is None else bool(dry_run_only_override)

    def compute_signals_fn(ib, current_holdings):
        if strategy_plugin_signals:
            return main.compute_signals(ib, current_holdings, strategy_plugin_signals=strategy_plugin_signals)
        return main.compute_signals(ib, current_holdings)

    def connect_ib_for_cycle():
        return main.connect_ib(read_only=bool(effective_dry_run_only), validate_trading_permissions=not bool(effective_dry_run_only))
    return main.build_runtime_composer(service_name=main.SERVICE_NAME or main.os.getenv('K_SERVICE', 'interactive-brokers-platform'), strategy_profile=main.STRATEGY_PROFILE, strategy_domain=main.RUNTIME_SETTINGS.strategy_domain, account_group=main.ACCOUNT_GROUP, project_id=main.PROJECT_ID, instance_name=main.RUNTIME_SETTINGS.ib_gateway_instance_name, account_ids=tuple(main.ACCOUNT_IDS), strategy_target_mode=main.RUNTIME_SETTINGS.strategy_target_mode, strategy_artifact_dir=main.RUNTIME_SETTINGS.strategy_artifact_dir, strategy_display_name=main.STRATEGY_DISPLAY_NAME, strategy_display_name_localized=main.strategy_display_name, managed_symbols=main.resolve_reporting_managed_symbols(), signal_effective_after_trading_days=main.SIGNAL_EFFECTIVE_AFTER_TRADING_DAYS, signal_source=main.STRATEGY_SIGNAL_SOURCE, status_icon=main.STRATEGY_STATUS_ICON, safe_haven=main.SAFE_HAVEN, dry_run_only=effective_dry_run_only, strategy_config_source=main.FEATURE_RUNTIME_CONFIG_SOURCE, ib_gateway_host_resolver=main.get_ib_host, ib_gateway_port=main.IB_PORT, ib_gateway_mode=main.RUNTIME_SETTINGS.ib_gateway_mode, ib_gateway_ip_mode=main.RUNTIME_SETTINGS.ib_gateway_ip_mode, ib_client_id=main.IB_CLIENT_ID, ib_connect_timeout_seconds=main.IB_CONNECT_TIMEOUT_SECONDS, feature_snapshot_path=main.FEATURE_SNAPSHOT_PATH, feature_snapshot_manifest_path=main.FEATURE_SNAPSHOT_MANIFEST_PATH, strategy_config_path=main.FEATURE_RUNTIME_CONFIG_PATH, reconciliation_output_path=main.RECONCILIATION_OUTPUT_PATH, translator=main.t, separator=main.SEPARATOR, send_message=main.send_tg_message, notification_channel=main._NOTIFICATION_CHANNEL, connect_ib_fn=connect_ib_for_cycle, build_portfolio_snapshot_fn=main.build_portfolio_snapshot, compute_signals_fn=compute_signals_fn, execute_rebalance_fn=lambda ib, target_weights, positions, account_values, **kwargs: main.execute_rebalance(ib, target_weights, positions, account_values, dry_run_only_override=effective_dry_run_only, **kwargs), run_id_builder=main.build_run_id, event_logger=main.emit_runtime_log, report_builder=main.build_runtime_report_base, report_persister=main.persist_runtime_report, trace_extractor=main.extract_cloud_trace, env_reader=main.os.getenv, printer=print, runtime_target=main.RUNTIME_SETTINGS.runtime_target, extra_reporting_fields={'execution_backend': main.EXECUTION_BACKEND, 'quantconnect_project_id': main.QUANTCONNECT_PROJECT_ID, 'quantconnect_node_id': main.QUANTCONNECT_NODE_ID, 'market': main.MARKET, 'market_calendar': main.MARKET_CALENDAR, 'market_currency': main.MARKET_CURRENCY, 'market_data_symbol_suffix': main.MARKET_DATA_SYMBOL_SUFFIX, 'market_exchange': main.MARKET_EXCHANGE, 'market_timezone': main.MARKET_TIMEZONE})

def require_gateway_execution_backend():
    if main.EXECUTION_BACKEND == main.EXECUTION_BACKEND_GATEWAY:
        return
    raise RuntimeError(f'IBKR execution_backend={main.EXECUTION_BACKEND!r} is configured; Gateway connection and direct order execution are disabled for this service. Run the QuantConnect deployment/algorithm path for this account group.')

def connect_ib(*, read_only: bool=False, validate_trading_permissions: bool=True):
    """Connect to the Gateway with an optional read-only session boundary."""
    main.require_gateway_execution_backend()
    if read_only and (type(main.IB_CLIENT_ID) is not int or main.IB_CLIENT_ID <= 0):
        raise RuntimeError('read-only Gateway access requires a configured nonzero client ID')
    adapter_dry_run_override = True if read_only else None
    adapters = main.build_broker_adapters(dry_run_only_override=adapter_dry_run_override)
    if read_only:
        adapters = main.replace(adapters, connect_attempts=1)
    return adapters.connect_ib(validate_trading_permissions=validate_trading_permissions, redact_connection_diagnostics=read_only)

def resolve_run_as_of_date() -> pd.Timestamp:
    explicit = main.os.getenv('IBKR_RUN_AS_OF_DATE')
    if explicit:
        return main.pd.Timestamp(explicit).normalize()
    run_date = main.datetime.now(main.NEW_YORK_TZ).date()
    if main.STRATEGY_PROFILE == 'tqqq_growth_income' and main.RUNTIME_SETTINGS.runtime_target is not None and (main.RUNTIME_SETTINGS.runtime_target.execution_mode == 'live'):
        return main._previous_market_session(run_date)
    return main.pd.Timestamp(run_date)

def get_historical_close(ib, symbol, duration='2 Y', bar_size='1 day'):
    return main.build_strategy_adapters().get_historical_close(ib, symbol, duration=duration, bar_size=bar_size)

def get_historical_candles(ib, symbol, duration='2 Y', bar_size='1 day'):
    return main.build_strategy_adapters().get_historical_candles(ib, symbol, duration=duration, bar_size=bar_size)

def _build_cycle_report_summary(cycle_result, execution_summary, reconciliation_record, *, dry_run: bool) -> dict:
    from application.reconciliation_service import account_brake_submission_status
    brake_status = account_brake_submission_status(getattr(cycle_result, 'signal_metadata', None))
    orders_submitted_count = main._count_orders(execution_summary.get('orders_submitted'), reconciliation_record.get('orders_submitted'))
    if brake_status is not None:
        orders_submitted_count = None
    orders_skipped_count = main._count_orders(execution_summary.get('orders_skipped'), reconciliation_record.get('orders_skipped'))
    orders_previewed_count = orders_submitted_count if dry_run else 0
    summary = {'result': cycle_result.result, 'execution_status': execution_summary.get('execution_status') or reconciliation_record.get('execution_status'), 'no_op_reason': execution_summary.get('no_op_reason') or reconciliation_record.get('no_op_reason'), 'orders_submitted_count': orders_submitted_count, 'orders_previewed_count': orders_previewed_count, 'orders_skipped_count': orders_skipped_count, 'dry_run_order_preview_available': None if dry_run and orders_previewed_count is None else bool(dry_run and orders_previewed_count > 0), 'snapshot_price_fallback_used': bool(execution_summary.get('snapshot_price_fallback_used') or reconciliation_record.get('snapshot_price_fallback_used')), 'snapshot_price_fallback_count': int(execution_summary.get('snapshot_price_fallback_count') or reconciliation_record.get('snapshot_price_fallback_count') or 0)}
    if brake_status is not None:
        summary['account_brake_submission_status'] = brake_status
        summary['orders_submitted_coverage'] = 'strategy_only_account_brake_details_unavailable'
    if main._account_facts_match_runtime_target(cycle_result.account_facts):
        summary['account_facts'] = cycle_result.account_facts
    quote_snapshot = execution_summary.get('quote_snapshot') or reconciliation_record.get('quote_snapshot')
    if quote_snapshot:
        summary['quote_snapshot'] = quote_snapshot
    from notifications.unified_adapter import digest_evidence
    summary['notification_digest'] = digest_evidence(execution_summary, dry_run=dry_run)
    outcome_keys = ('orders_filled', 'orders_pending', 'orders_partially_filled', 'option_orders_filled', 'option_orders_pending', 'option_orders_partially_filled')
    if not dry_run and brake_status is None and all((isinstance(execution_summary.get(key), (list, tuple)) for key in outcome_keys)):
        summary['orders_submitted_count'] = summary['notification_digest']['order_count']
    return summary

def _build_notification_delivery_log_for_report(*, platform: str, strategy_profile: str, run_id: str, dry_run: bool, orders_previewed_count: int | None, delivery_events: list[dict]) -> dict:
    events = [dict(event) for event in delivery_events if dict(event).get('delivery_status') == 'sent']
    if not dry_run or orders_previewed_count is None or orders_previewed_count <= 0 or (not events):
        return {}
    return {'notification_schema_version': 'hk_live_enablement_notification.v1', 'notification_event_type': 'hk_snapshot_live_enablement_dry_run', 'notification_correlation_id': str(run_id or ''), 'locales': ['en', 'zh-Hans'], 'profile': str(strategy_profile or ''), 'platform': str(platform or ''), 'validation_status': 'passed', 'orders_previewed': int(orders_previewed_count), 'delivery_events': events, 'notification_contains_profile': True, 'notification_contains_platform': True, 'notification_contains_validation_status': True, 'notification_contains_order_preview_summary': True, 'notification_redacts_sensitive_fields': True, 'redaction_policy': 'raw notification text is not persisted; only sha256 and length are recorded'}

def _build_notification_delivery_summary(delivery_events: list[dict]) -> dict:
    safe_fields = ('sink', 'delivery_status', 'transport_acknowledged', 'error_type', 'compact_text_sha256', 'compact_text_length')
    events = [{key: event[key] for key in safe_fields if key in event} for event in (dict(item) for item in delivery_events)]
    if not events:
        return {}
    sent_count = sum((event.get('delivery_status') == 'sent' for event in events))
    failed_count = sum((event.get('delivery_status') == 'failed' for event in events))
    return {'attempted_count': len(events), 'sent_count': sent_count, 'failed_count': failed_count, 'all_acknowledged': failed_count == 0 and sent_count == len(events), 'delivery_events': events}

def build_account_notification_lines() -> tuple[str, ...]:
    account_ids = tuple((str(account_id).strip() for account_id in main.ACCOUNT_IDS if str(account_id).strip()))
    if not account_ids:
        return ()
    return (main.t('account_ids_detail', account_ids=', '.join(account_ids)),)

def build_extra_notification_lines(strategy_plugin_signals=(), *, strategy_plugin_error: str | None=None) -> tuple[str, ...]:
    return (main.t('market_scope_detail', market=main.MARKET, currency=main.MARKET_CURRENCY, exchange=main.MARKET_EXCHANGE, calendar=main.MARKET_CALENDAR), *main.build_account_notification_lines(), *main.build_strategy_plugin_notification_lines(strategy_plugin_signals), *main.build_strategy_plugin_error_notification_lines(strategy_plugin_error))
