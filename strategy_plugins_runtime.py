"""Strategy plugin signal presentation and alert dispatch."""

from __future__ import annotations


import sys

_FALLBACK_MODULE_NAME = __name__


class _MainModuleProxy:
    """Prefer ``main.<name>`` (facade-aware); fall back during module import."""

    def __getattr__(self, name):
        for key in ("main", "__main__"):
            main_mod = sys.modules.get(key)
            if main_mod is None:
                continue
            if getattr(main_mod, "__name__", None) in {
                "http_routes",
                "runtime_wiring",
                "notifications_runtime",
                "strategy_plugins_runtime",
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

def load_strategy_plugin_signals():
    return main.build_strategy_adapters().load_strategy_plugin_signals(getattr(main.RUNTIME_SETTINGS, 'strategy_plugin_mounts_json', None))

def attach_strategy_plugin_report(report, *, signals, error: str | None=None):
    main.build_strategy_adapters().attach_strategy_plugin_report(report, signals=signals, error=error)

def build_strategy_plugin_notification_lines(signals) -> tuple[str, ...]:
    return main.build_strategy_adapters().build_strategy_plugin_notification_lines(signals)

def build_strategy_plugin_error_notification_lines(error) -> tuple[str, ...]:
    return main.build_strategy_adapters().build_strategy_plugin_error_notification_lines(error)

def build_strategy_plugin_alert_messages(signals):
    return main.build_strategy_adapters().build_strategy_plugin_alert_messages(signals)

def build_strategy_plugin_alert_state_settings():
    return main.StrategyPluginAlertStateSettings.from_env(project_id=main.PROJECT_ID)

def build_strategy_plugin_alert_context_label() -> str:
    return main.build_alert_context_label(platform_id='ibkr', strategy_profile=main.STRATEGY_PROFILE, account_scope=main.ACCOUNT_GROUP, service_name=main.SERVICE_NAME, runtime_target=main.RUNTIME_SETTINGS.runtime_target)

def publish_strategy_plugin_alerts(signals, *, report=None):
    from quant_platform_kit.notifications.digest_routing import immediate_plugin_signals
    result = main.dispatch_strategy_plugin_alerts(immediate_plugin_signals(signals, mode='daily_digest'), notification_settings=main.RUNTIME_SETTINGS, translator=main.t, strategy_label=main.STRATEGY_PROFILE, context_label=main.build_strategy_plugin_alert_context_label(), state_settings=main.build_strategy_plugin_alert_state_settings(), log_message=print)
    if report is not None:
        result.attach_to_report(report)
    return result
