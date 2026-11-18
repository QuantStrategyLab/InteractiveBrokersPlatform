"""Runtime error / notification helpers for HTTP routes."""

from __future__ import annotations


import sys

_FALLBACK_MODULE_NAME = __name__


class _MainModuleProxy:
    """Prefer ``interactive_brokers_platform.main.<name>`` (facade-aware); fall back during module import."""

    def __getattr__(self, name):
        for key in ("interactive_brokers_platform.main", "__main__"):
            main_mod = sys.modules.get(key)
            if main_mod is None:
                continue
            if (
                key == "__main__"
                and getattr(getattr(main_mod, "__spec__", None), "name", None)
                != "interactive_brokers_platform.main"
            ):
                continue
            if getattr(main_mod, "__name__", None) in {
                "interactive_brokers_platform.http_routes",
                "interactive_brokers_platform.runtime_wiring",
                "interactive_brokers_platform.notifications_runtime",
                "interactive_brokers_platform.strategy_plugins_runtime",
            }:
                continue
            try:
                # Use getattr so ModuleType facades can resolve mutable globals.
                return getattr(main_mod, name)
            except AttributeError:
                continue
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

from interactive_brokers_platform.application.cycle_result import coerce_strategy_cycle_result
from interactive_brokers_platform.application.execution_receipt_adapter import (
    attach_cycle_execution_receipt,
    attach_terminal_fallback_execution_receipt,
)
from interactive_brokers_platform.application.broker_reconciliation import (
    IBKRReconciliationReadError,
    build_reconciliation_candidate,
    collect_read_only_reconciliation_observations,
)
from interactive_brokers_platform.application.reconciliation_reporting import (
    build_persistable_reconciliation_report,
    normalize_reconciliation_request_id,
    normalize_reconciliation_scheduler_job_sha256,
)
from interactive_brokers_platform.application.runtime_broker_adapters import (
    IBKRGatewayUnavailableError,
    IBKRTradingPermissionError,
    build_runtime_broker_adapters,
)
from interactive_brokers_platform.application.runtime_composer import build_runtime_composer
from interactive_brokers_platform.application.runtime_deadline import (
    RuntimeDeadlineExceeded,
    classify_runtime_deadline_qualification_caller,
    classify_runtime_deadline_stage,
    enforce_runtime_deadline,
    recycle_current_process_after_response,
)
from interactive_brokers_platform.application import signal_provenance
from interactive_brokers_platform.application.runtime_strategy_adapters import (
    build_runtime_strategy_adapters,
    fetch_yfinance_historical_candles,
)
from interactive_brokers_platform.application.rebalance_service import run_strategy_core as run_rebalance_cycle
from interactive_brokers_platform.application.signal_snapshot import build_signal_snapshot
from interactive_brokers_platform.decision_mapper import map_strategy_decision
from interactive_brokers_platform.entrypoints.cloud_run import is_market_open_now
from interactive_brokers_platform.runtime_execution_policy import (
    IBKR_FRACTIONAL_EQUITY_API_UNSUPPORTED_SKIP_REASON,
    dca_execution_unsupported_reason,
)
from interactive_brokers_platform.notifications.telegram import build_strategy_display_name, build_translator, send_telegram_message
from interactive_brokers_platform.notifications.unified_adapter import with_run as with_notification_run
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
from interactive_brokers_platform.application.ibkr_market_data import fetch_quote_snapshots_with_expected_error_summary
from interactive_brokers_platform.application.ibkr_order_execution import probe_order_write_access, submit_order_intent
from interactive_brokers_platform.application.monitor_dispatcher import (
    dispatch_due_monitor_targets,
    load_monitor_targets,
    lookback_minutes_from_env,
    max_workers_from_env,
    timeout_seconds_from_env,
)
from interactive_brokers_platform.application.ibkr_portfolio import (
    IBKRPortfolioSnapshotUnavailableError,
    fetch_reconciled_paper_portfolio_snapshot,
    fetch_portfolio_snapshot,
)
from interactive_brokers_platform.application.execution_service import (
    _SettledCashDeadlineIB,
    check_order_submitted as application_check_order_submitted,
    execute_rebalance as application_execute_rebalance,
    get_market_prices as application_get_market_prices,
)
from interactive_brokers_platform.application.paper_liquidation_service import execute_paper_liquidation
from interactive_brokers_platform.application.paper_execution_admission import resolve_paper_execution_admission_enabled
from interactive_brokers_platform.application.paper_execution_command_consumer import (
    consume_due_paper_execution_commands,
    resolve_paper_execution_command_consumer_enabled,
)
from interactive_brokers_platform.runtime_logging import build_run_id, emit_runtime_log, extract_cloud_trace
from interactive_brokers_platform.runtime_config_support import (
    EXECUTION_BACKEND_GATEWAY,
    load_platform_runtime_settings,
    resolve_ib_gateway_ip_mode,
)
from interactive_brokers_platform.strategy_runtime import load_strategy_runtime
from interactive_brokers_platform.application.http_routes import (
    _dispatch_local_monitor,
    _handle_monitor_dispatch,
    _handle_probe,
    _handle_reconciliation,
    _handle_live_permission_probe,
    health_impl,
)

def _split_env_list(value: str | None) -> tuple[str, ...]:
    return tuple((item.strip() for item in str(value or '').replace(';', ',').split(',') if item.strip()))

def send_tg_message(message):
    from quant_platform_kit.notifications.cycle_channel import build_cycle_sender
    sender = build_cycle_sender(channel=main._NOTIFICATION_CHANNEL, telegram_token=main.TG_TOKEN, telegram_chat_id=main.TG_CHAT_ID, webhook_url=main._NOTIFICATION_WEBHOOK_URL)
    return sender(main._with_platform_notification_prefix(message))

def _platform_notification_prefix() -> str:
    runtime_target = getattr(main.RUNTIME_SETTINGS, 'runtime_target', None)
    account_selector = getattr(runtime_target, 'account_selector', None) or ()
    for selector in account_selector:
        label = main._normalize_account_prefix_label(selector)
        if label:
            return f'[{label}]'
    raw_scope = getattr(runtime_target, 'account_scope', None) or main.ACCOUNT_GROUP
    label = main._normalize_account_prefix_label(raw_scope)
    if not label:
        return ''
    return f'[{label}]'

def _normalize_account_prefix_label(value: object) -> str:
    label = str(value or '').strip().upper()
    if not label or label in {'DEFAULT', 'LIVE', 'US'}:
        return ''
    if label.startswith('LIVE-U') and len(label) > len('LIVE-U'):
        return label.removeprefix('LIVE-')
    return label

def _with_platform_notification_prefix(message: str) -> str:
    prefix = main._platform_notification_prefix()
    text = str(message or '')
    if not prefix or text.lstrip().startswith(prefix):
        return text
    return f'{prefix} {text}'

def publish_notification(*, detailed_text, compact_text):
    return main.build_composer().build_notification_adapters().publish_cycle_notification(detailed_text=detailed_text, compact_text=compact_text)

def _runtime_error_notification_targets() -> tuple[tuple[str, str], ...]:
    targets: list[tuple[str, str]] = []
    if main.TG_TOKEN and main.TG_CHAT_ID:
        targets.append((main.TG_TOKEN, main.TG_CHAT_ID))
    seen: set[tuple[str, str]] = set()
    unique_targets: list[tuple[str, str]] = []
    for target in targets:
        if target in seen:
            continue
        seen.add(target)
        unique_targets.append(target)
    return tuple(unique_targets)

def _runtime_error_notification_message(exc: Exception, *, route_label: str | None=None) -> str:
    title = 'runtime_failure_title'
    return '\n'.join((main.t(title), main.t('strategy_label', name=main.strategy_display_name or main.STRATEGY_PROFILE), main.t('runtime_failure_context', context=main.ACCOUNT_GROUP), main.t('runtime_failure_result'), main.t('runtime_failure_action')))

def _notify_runtime_error(exc: Exception, *, route_label: str | None=None) -> bool:
    from interactive_brokers_platform.notifications.unified_adapter import publish_runtime_failure
    return publish_runtime_failure(exc, lambda: main._notify_runtime_error_legacy(exc, route_label=route_label))

def _notify_runtime_error_legacy(exc: Exception, *, route_label: str | None=None) -> bool:
    targets = main._runtime_error_notification_targets()
    if not targets:
        print(main.t('runtime_notification_missing_target'), flush=True)
        return False
    message = main._runtime_error_notification_message(exc, route_label=route_label)
    outcomes = []
    for token, chat_id in targets:
        outcomes.append(main.send_telegram_message(main._with_platform_notification_prefix(message), token=token, chat_id=chat_id, requests_module=main.requests))
    return bool(outcomes) and all(outcomes)

def _publish_runtime_failure_notification(*, detailed_text: str, compact_text: str, exc: Exception) -> bool:
    from interactive_brokers_platform.notifications.unified_adapter import enabled as unified_enabled, publish_runtime_failure
    if unified_enabled():
        return publish_runtime_failure(exc, lambda: main.publish_notification(detailed_text=detailed_text, compact_text=compact_text))
    try:
        if main.publish_notification(detailed_text=detailed_text, compact_text=compact_text):
            return True
        return main._notify_runtime_error(exc)
    except Exception:
        print(main.t('runtime_notification_delivery_failed'), flush=True)
        return main._notify_runtime_error(exc)

def _handle_route_runtime_error(exc: Exception, *, route_label: str | None=None):
    print(main.t('runtime_failure_log'), flush=True)
    main._notify_runtime_error(exc, route_label=route_label)
    return ('Error', 500)

def _route_with_runtime_error_fallback(handler, *args, route_label: str | None=None, **kwargs):
    try:
        return handler(*args, **kwargs)
    except Exception as exc:
        return main._handle_route_runtime_error(exc, route_label=route_label)
