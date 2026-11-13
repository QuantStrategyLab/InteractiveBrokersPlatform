"""Flask HTTP application and route handlers."""


# ruff: noqa: F403, F405
from __future__ import annotations


import importlib
import notifications_runtime as nr
import runtime_wiring as rw
import strategy_plugins_runtime as spr
from runtime_wiring import *  # noqa: F403, F401

from application.http_routes import (
    _dispatch_local_monitor,
    _handle_monitor_dispatch,
    _handle_probe,
    _handle_reconciliation,
    _handle_live_permission_probe,
    health_impl,
)

app = Flask(__name__)
ensure_event_loop = ibkr_ensure_event_loop
NEW_YORK_TZ = ZoneInfo("America/New_York")
STRATEGY_RUN_LOCK = threading.Lock()
def _is_execution_failure(execution_status, no_op_reason) -> bool:
    status = str(execution_status or "").strip().lower()
    if status in {"error", "failed", "failure"}:
        return True
    if status != "blocked":
        return False
    reason = str(no_op_reason or "").strip().lower()
    return not reason.startswith(EXPECTED_EXECUTION_GUARD_REASON_PREFIXES)
def t(key, **kwargs):
    return build_translator(NOTIFY_LANG)(key, **kwargs)
def log_runtime_event(log_context, event, **fields):
    return build_composer().build_reporting_adapters().log_event(log_context, event, **fields)
def build_execution_report(log_context, *, dry_run_only_override: bool | None = None):
    return build_composer(dry_run_only_override=dry_run_only_override).build_reporting_adapters().build_report(log_context)
def persist_execution_report(report, *, dry_run_only_override: bool | None = None):
    adapters = build_composer(dry_run_only_override=dry_run_only_override).build_reporting_adapters()
    diagnostics = report.get("diagnostics")
    if isinstance(diagnostics, Mapping) and diagnostics.get("maintenance_cash_observation") is True:
        prefix = adapters.report_gcs_prefix_uri
        if not prefix:
            raise RuntimeError("maintenance observation requires the existing private report destination")
        adapters = replace(adapters,
            report_base_dir=os.path.join(adapters.report_base_dir, "maintenance-cash") if adapters.report_base_dir else None,
            report_gcs_prefix_uri=prefix.rstrip("/") + "/maintenance-cash")
    return adapters.persist_execution_report(report)
def persist_reconciliation_report(report):
    safe_report = build_persistable_reconciliation_report(report)
    diagnostics = report.get("diagnostics")
    initial = diagnostics.get("initial_enrollment_private") if isinstance(diagnostics, Mapping) else None
    if initial is not None:
        adapters = build_composer(dry_run_only_override=True).build_reporting_adapters()
        if not adapters.report_gcs_prefix_uri:
            raise RuntimeError("initial enrollment requires the existing private report destination")
        if not isinstance(initial, Mapping) or set(initial) != {"facts", "facts_sha256"}:
            raise RuntimeError("initial enrollment private facts are invalid")
        from urllib.parse import urlsplit
        from google.cloud import storage
        prefix = urlsplit(adapters.report_gcs_prefix_uri)
        request_id = safe_report["diagnostics"].get("reconciliation_request_id")
        if prefix.scheme != "gs" or not prefix.netloc or not prefix.path.strip("/") or prefix.query or prefix.fragment or not request_id:
            raise RuntimeError("initial enrollment private destination or request is invalid")
        safe_report["diagnostics"]["initial_enrollment_private"] = dict(initial)
        name = prefix.path.strip("/") + "/initial-enrollment/" + request_id + ".json"
        blob = storage.Client().bucket(prefix.netloc).blob(name)
        blob.upload_from_string(json.dumps(safe_report, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False),
                                content_type="application/json", if_generation_match=0, retry=None)
        generation = str(blob.generation or "")
        if not generation.isascii() or not generation.isdigit() or int(generation) <= 0:
            raise RuntimeError("initial enrollment private generation is unknown")
        return f"gs://{prefix.netloc}/{name}#{generation}"
    return persist_execution_report(safe_report, dry_run_only_override=True)
def build_request_log_context():
    return build_composer().build_reporting_adapters().build_log_context(
        trace_header=request.headers.get("X-Cloud-Trace-Context"),
    )
def _previous_market_session(run_date: date) -> pd.Timestamp:
    import pandas_market_calendars as market_calendars

    sessions = market_calendars.get_calendar(MARKET_CALENDAR).valid_days(
        start_date=run_date - timedelta(days=14),
        end_date=run_date - timedelta(days=1),
    )
    if len(sessions) == 0:
        raise RuntimeError("IBKR previous completed market session is unavailable")
    return pd.Timestamp(sessions[-1]).tz_localize(None).normalize()
def compute_signals(ib, current_holdings, *, strategy_plugin_signals=()):
    return build_strategy_adapters().compute_signals(
        ib,
        current_holdings,
        strategy_plugin_signals=strategy_plugin_signals,
    )
def _has_signal_snapshot_details(snapshot: dict[str, object]) -> bool:
    return any(
        snapshot.get(field_name)
        for field_name in (
            "signal_as_of",
            "market_date",
            "latest_price_source",
            "target_weights",
            "target_values",
            "indicators",
            "signal",
            "status",
        )
    )
def _count_orders(*candidates) -> int:
    for candidate in candidates:
        if isinstance(candidate, (list, tuple)):
            return len(candidate)
    return 0
def _account_facts_match_runtime_target(account_facts: Mapping[str, object]) -> bool:
    """Only report physical account facts bound by the configured selector."""
    target = getattr(RUNTIME_SETTINGS, "runtime_target", None)
    selectors = getattr(target, "account_selector", ()) if target is not None else ()
    account_ids = account_facts.get("account_ids") if isinstance(account_facts, Mapping) else None
    if isinstance(selectors, str):
        selectors = (selectors,)
    if isinstance(account_ids, str):
        account_ids = (account_ids,)
    normalized_selectors = tuple(
        str(value or "").strip()
        for value in (selectors or ())
        if str(value or "").strip()
    )
    normalized_account_ids = tuple(
        str(value or "").strip()
        for value in (account_ids or ())
        if str(value or "").strip()
    )
    return (
        len(normalized_selectors) == 1
        and normalized_selectors[0].lower() != "default"
        and len(normalized_account_ids) == 1
        and normalized_account_ids[0] == normalized_selectors[0]
    )
def get_current_portfolio(ib):
    return build_broker_adapters().get_current_portfolio(ib)
def build_portfolio_snapshot(ib):
    return build_broker_adapters().build_portfolio_snapshot(
        ib,
        get_current_portfolio_fallback=get_current_portfolio,
    )
def _paper_command_consumer_session_date() -> str:
    return datetime.now(ZoneInfo(MARKET_TIMEZONE)).date().isoformat()
def _paper_command_consumer_runtime_is_isolated() -> bool:
    """Require a disabled, paper-only runtime before any Gateway read occurs."""

    runtime_target = getattr(RUNTIME_SETTINGS, "runtime_target", None)
    return bool(
        runtime_target is not None
        and bool(getattr(RUNTIME_SETTINGS, "dry_run_only", False))
        and not bool(getattr(RUNTIME_SETTINGS, "runtime_target_enabled", True))
        and str(getattr(RUNTIME_SETTINGS, "ib_gateway_mode", "") or "").strip().lower() == "paper"
        and str(getattr(runtime_target, "execution_mode", "") or "").strip().lower() == "paper"
        and bool(CASH_ONLY_EXECUTION)
    )
def run_paper_execution_command_consumer() -> dict[str, object]:
    """Explicitly reconcile due commands without creating a broker order.

    This isolated route deliberately avoids the normal rebalance and execution
    adapters.  It opens the Gateway only after command release and delivery
    binding checks pass, then reads portfolio and quote evidence to simulate
    what would have been submitted.
    """

    if not _paper_command_consumer_runtime_is_isolated():
        raise RuntimeError(
            "paper command consumer requires disabled runtime target, IBKR_DRY_RUN_ONLY=true, "
            "paper Gateway mode, paper runtime target, and CASH_ONLY_EXECUTION=true"
        )
    if not resolve_paper_execution_command_consumer_enabled(
        env_reader=os.getenv,
        dry_run_only=bool(RUNTIME_SETTINGS.dry_run_only),
    ):
        raise RuntimeError("paper command consumer is not enabled")

    runtime_target = RUNTIME_SETTINGS.runtime_target
    expected_release = getattr(runtime_target, "strategy_release", None)
    expected_binding = {
        "platform": "ibkr",
        "account_scope": str(getattr(runtime_target, "account_scope", "") or "unknown"),
        "strategy_profile": str(getattr(runtime_target, "strategy_profile", "") or "unknown"),
    }
    store = build_execution_command_store_from_env(
        platform_env_prefix="IBKR",
        env_reader=os.getenv,
        project_id=PROJECT_ID,
    )
    if not store.cloud_prefix_uri and not store.local_dir:
        raise RuntimeError("IBKR paper command consumer requires an execution command store")

    reporting_adapters = build_composer(dry_run_only_override=True).build_reporting_adapters()
    log_context = reporting_adapters.build_log_context(
        trace_header=request.headers.get("X-Cloud-Trace-Context"),
    )
    report = reporting_adapters.build_report(log_context)
    ib = None
    quote_cache: dict[str, object] = {}

    def _ib():
        nonlocal ib
        if ib is None:
            # This uses a dry-run-only adapter; its permission validation does
            # not perform the normal live what-if order check.
            ib = build_broker_adapters(dry_run_only_override=True).connect_ib()
        return ib

    def _load_portfolio():
        return fetch_reconciled_paper_portfolio_snapshot(
            _ib(),
            account_ids=ACCOUNT_IDS,
            currency=MARKET_CURRENCY,
        )

    def _load_quote(symbol: str):
        normalized = str(symbol or "").strip().upper()
        if normalized not in quote_cache:
            quotes = fetch_market_quote_snapshots(_ib(), (normalized,))
            quote = quotes.get(normalized)
            if quote is None:
                raise IBKRPortfolioSnapshotUnavailableError(
                    "IBKR paper command reconciliation is missing a current quote."
                )
            quote_cache[normalized] = quote
        return quote_cache[normalized]

    try:
        reporting_adapters.log_event(
            log_context,
            "paper_execution_command_consumer_started",
            message="Starting isolated read-only IBKR paper command consumer",
        )
        result = consume_due_paper_execution_commands(
            store=store,
            as_of_session=_paper_command_consumer_session_date(),
            claimant=str(os.getenv("K_SERVICE") or "ibkr-paper-command-consumer"),
            portfolio_loader=_load_portfolio,
            quote_loader=_load_quote,
            managed_symbols=resolve_reporting_managed_symbols(),
            runtime_release_receipt=build_runtime_loaded_receipt(
                strategy_release=expected_release,
            ),
            expected_strategy_release=expected_release,
            expected_command_binding=expected_binding,
        )
        finalize_runtime_report(
            report,
            status="ok" if result.get("status") == "ok" else "skipped",
            summary={"paper_execution_command_consumer": result},
        )
        reporting_adapters.log_event(
            log_context,
            "paper_execution_command_consumer_completed",
            message="IBKR paper command consumer completed",
            result_status=result.get("status"),
            commands_count=len(tuple(result.get("commands") or ())),
        )
        return result
    except Exception as exc:
        append_runtime_report_error(
            report,
            stage="paper_execution_command_consumer",
            message=str(exc),
            error_type=type(exc).__name__,
        )
        finalize_runtime_report(report, status="error")
        reporting_adapters.log_event(
            log_context,
            "paper_execution_command_consumer_failed",
            message="IBKR paper command consumer failed",
            severity="ERROR",
            error_type=type(exc).__name__,
        )
        raise
    finally:
        try:
            report_path = reporting_adapters.persist_execution_report(report)
            print(f"execution_report {report_path}", flush=True)
        except Exception as persist_exc:
            print(f"failed to persist execution report: {persist_exc}", flush=True)
        disconnect_fn = getattr(ib, "disconnect", None)
        if callable(disconnect_fn):
            disconnect_fn()
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
    acquire_execution_claim=None,
    account_execution_session=None,
    dry_run_only_override: bool | None = None,
):
    return build_broker_adapters(dry_run_only_override=dry_run_only_override).execute_rebalance(
        ib,
        target_weights,
        positions,
        account_values,
        strategy_symbols=strategy_symbols,
        signal_metadata=signal_metadata,
        acquire_execution_claim=acquire_execution_claim,
        account_execution_session=account_execution_session,
    )
def _format_liquidation_orders(orders) -> str:
    return build_broker_adapters().format_liquidation_orders(orders)
def run_paper_liquidation_cycle():
    require_gateway_execution_backend()
    if RUNTIME_SETTINGS.ib_gateway_mode != "paper":
        raise RuntimeError("IBKR_PAPER_LIQUIDATE_ONLY is only allowed when ib_gateway_mode=paper")
    return build_broker_adapters().run_paper_liquidation_cycle(
        connect_ib_fn=connect_ib,
        get_current_portfolio_fn=get_current_portfolio,
        publish_notification_fn=publish_notification,
    )
def run_strategy_core(
    *,
    strategy_plugin_signals=(),
    strategy_plugin_error: str | None = None,
    dry_run_only_override: bool | None = None,
    notification_delivery_events: list[dict] | None = None,
):
    if PAPER_LIQUIDATE_ONLY and dry_run_only_override is None:
        return run_paper_liquidation_cycle()
    composer = build_composer(
        dry_run_only_override=dry_run_only_override,
        strategy_plugin_signals=strategy_plugin_signals,
    )
    try:
        rebalance_runtime = composer.build_rebalance_runtime(
            silent_cycle_notifications=bool(dry_run_only_override),
            notification_delivery_events=notification_delivery_events,
        )
    except TypeError as exc:
        if "notification_delivery_events" not in str(exc):
            raise
        rebalance_runtime = composer.build_rebalance_runtime(
            silent_cycle_notifications=bool(dry_run_only_override),
        )
    return run_rebalance_cycle(
        runtime=rebalance_runtime,
        config=composer.build_rebalance_config(
            extra_notification_lines=build_extra_notification_lines(
                strategy_plugin_signals,
                strategy_plugin_error=strategy_plugin_error,
            ),
            cash_only_execution=CASH_ONLY_EXECUTION,
            notification_attention_reason_codes=(
                ("strategy_plugin_error",) if strategy_plugin_error else ()
            ),
        ),
    )
def attach_signal_data_provenance(report) -> None:
    """Attach this run's non-sensitive signal data provenance; never raises."""
    try:
        provenance = signal_provenance.snapshot()
        if provenance and isinstance(report, dict):
            diagnostics = report.get("diagnostics")
            if not isinstance(diagnostics, dict):
                diagnostics = {}
                report["diagnostics"] = diagnostics
            diagnostics["signal_data_provenance"] = provenance
    except Exception:
        return
def _handle_request(
    *,
    dry_run_only_override: bool | None = None,
    response_body: str = "OK",
    dry_run_label: str = "strategy dry-run",
    persist_report: bool = True,
    report_collector: list[dict] | None = None,
):
    if request.method == "GET":
        if dry_run_only_override is None:
            return "OK - use POST to execute strategy", 200
        return f"{response_body} - use POST to run {dry_run_label}", 200
    if dry_run_only_override is None and not getattr(RUNTIME_SETTINGS, "runtime_target_enabled", True):
        return "Runtime Target Disabled", 200

    log_context = build_request_log_context()
    report = build_execution_report(log_context, dry_run_only_override=dry_run_only_override)
    signal_provenance.reset()
    if report_collector is not None:
        # Register the mutable report before any deadline-bound setup work so a
        # timeout while loading strategy plugins still leaves a report to persist.
        report_collector.append(report)
    execution_window = "dry_run" if dry_run_only_override else "execution"
    lock_acquired = False
    deadline_exceeded = False
    try:
        strategy_plugin_signals, strategy_plugin_error = load_strategy_plugin_signals()
        attach_strategy_plugin_report(
            report,
            signals=strategy_plugin_signals,
            error=strategy_plugin_error,
        )
        if not strategy_plugin_error:
            from notifications.unified_adapter import routine_observation_counts_from_signals
            report.setdefault("summary", {})["routine_observation_counts"] = routine_observation_counts_from_signals(strategy_plugin_signals)
        lock_acquired = STRATEGY_RUN_LOCK.acquire(blocking=False)
        log_runtime_event(
            log_context,
            "strategy_cycle_received",
            message="Received strategy dry-run request" if dry_run_only_override else "Received strategy execution request",
            http_method=request.method,
            execution_window=execution_window,
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
        market_open = is_market_open_now(
            calendar_name=MARKET_CALENDAR,
            timezone_name=MARKET_TIMEZONE,
            logger=lambda message: print(message, flush=True),
        )
        force_run = (os.getenv("IBKR_FORCE_RUN") or "").strip().lower() == "true"
        if not market_open and not force_run:
            log_runtime_event(
                log_context,
                "market_closed",
                message="Market closed; skip strategy execution",
                execution_window=execution_window,
                market=MARKET,
                market_calendar=MARKET_CALENDAR,
                market_timezone=MARKET_TIMEZONE,
            )
            finalize_runtime_report(
                report,
                status="skipped",
                diagnostics={"skip_reason": "market_closed"},
            )
            return "Market Closed", 200
        if force_run and not market_open:
            log_runtime_event(
                log_context,
                "market_hours_bypassed",
                message="Market hours bypassed for strategy execution",
                execution_window=execution_window,
                market=MARKET,
                market_calendar=MARKET_CALENDAR,
                market_timezone=MARKET_TIMEZONE,
            )
        unsupported_reason = dca_execution_unsupported_reason(STRATEGY_PROFILE)
        if unsupported_reason is not None:
            unsupported_message = (
                "Strategy requires fractional-share execution; IBKR TWS API does not support "
                "fractional equity orders (use desktop TWS for manual DCA or choose another platform)"
                if unsupported_reason == IBKR_FRACTIONAL_EQUITY_API_UNSUPPORTED_SKIP_REASON
                else "Strategy requires fractional-share execution; skip"
            )
            log_runtime_event(
                log_context,
                "strategy_execution_unsupported",
                message=unsupported_message,
                execution_window=execution_window,
                skip_reason=unsupported_reason,
                strategy_profile=STRATEGY_PROFILE,
                market=MARKET,
                market_calendar=MARKET_CALENDAR,
                market_timezone=MARKET_TIMEZONE,
            )
            finalize_runtime_report(
                report,
                status="skipped",
                diagnostics={"skip_reason": unsupported_reason},
            )
            return "Unsupported Strategy", 200
        log_runtime_event(
            log_context,
            "strategy_cycle_started",
            message="Starting strategy dry-run" if dry_run_only_override else "Starting strategy execution",
            execution_window=execution_window,
        )
        if dry_run_only_override is None:
            publish_strategy_plugin_alerts(strategy_plugin_signals, report=report)
        notification_delivery_events: list[dict] = []
        cycle_result = coerce_strategy_cycle_result(
            with_notification_run(
                report, run_strategy_core,
                strategy_plugin_signals=strategy_plugin_signals,
                strategy_plugin_error=strategy_plugin_error,
                dry_run_only_override=dry_run_only_override,
                notification_delivery_events=notification_delivery_events,
            )
        )
        execution_summary = dict(cycle_result.execution_summary or {})
        reconciliation_record = dict(cycle_result.reconciliation_record or {})
        signal_metadata = dict(cycle_result.signal_metadata or {})
        signal_snapshot = dict(signal_metadata.get("signal_snapshot") or {})
        if not signal_snapshot:
            signal_snapshot = build_signal_snapshot(
                platform="ibkr",
                strategy_profile=signal_metadata.get("strategy_profile") or STRATEGY_PROFILE,
                metadata=signal_metadata,
                target_weights=cycle_result.target_weights,
            )
        if execution_summary.get("price_source_mode") or reconciliation_record.get("price_source_mode"):
            signal_snapshot["latest_price_source"] = (
                execution_summary.get("price_source_mode")
                or reconciliation_record.get("price_source_mode")
            )
        fallback_used = bool(
            execution_summary.get("snapshot_price_fallback_used")
            or reconciliation_record.get("snapshot_price_fallback_used")
        )
        if fallback_used:
            signal_snapshot["data_freshness_warning"] = "snapshot_price_fallback_used"
        has_signal_snapshot = _has_signal_snapshot_details(signal_snapshot)
        if has_signal_snapshot:
            log_runtime_event(
                log_context,
                "strategy_signal_snapshot",
                message="Strategy signal snapshot",
                execution_window=execution_window,
                **signal_snapshot,
            )
        from application.settlement_reporting import settlement_enabled
        settlement_observation = None
        if not report.get("dry_run") and settlement_enabled():
            from application.settlement_route import runtime_settlement_store
            from application.order_settlement import settlement_summary
            from application.settlement_reporting import (
                publish_settlement_notices, settlement_candidate, settlement_receipt_summary,
            )
            from notifications.unified_adapter import event_for, persist_candidate
            import sys
            settlement_store = runtime_settlement_store(sys.modules[__name__])
            settlement_observation = settlement_summary(settlement_store)
            publish_settlement_notices(settlement_store, settlement_observation,
                send=send_tg_message, locale=NOTIFY_LANG,
                publish_candidate=lambda row: settlement_candidate(row, event_factory=event_for, persist=persist_candidate))
        report_summary = _build_cycle_report_summary(
            cycle_result,
            execution_summary,
            reconciliation_record,
            dry_run=bool(report.get("dry_run")),
        )
        if settlement_observation is not None:
            current = [row for row in settlement_observation["orders_submitted"]
                       if row["source_run_id"] == report.get("run_id")]
            report_summary["settlement_outcomes"] = current
            # The original execution state remains unchanged. The receipt can
            # consume this separate, broker-confirmed reporting observation.
            receipt_observation = {key: [row for row in value if row["source_run_id"] == report.get("run_id")]
                                   for key, value in settlement_observation.items() if isinstance(value, list)}
            receipt_summary = settlement_receipt_summary(execution_summary, receipt_observation)
        else:
            receipt_summary = execution_summary
        notification_delivery_log = _build_notification_delivery_log_for_report(
            platform="interactive_brokers",
            strategy_profile=STRATEGY_PROFILE,
            run_id=str(report.get("run_id") or ""),
            dry_run=bool(report.get("dry_run")),
            orders_previewed_count=report_summary.get("orders_previewed_count"),
            delivery_events=notification_delivery_events,
        )
        if notification_delivery_log:
            report_summary["notification_delivery_log"] = notification_delivery_log
        notification_delivery_summary = _build_notification_delivery_summary(
            notification_delivery_events
        )
        if notification_delivery_summary:
            report_summary["notification_delivery_summary"] = notification_delivery_summary
        execution_status = str(report_summary.get("execution_status") or "").strip().lower()
        execution_failed = _is_execution_failure(
            execution_status,
            report_summary.get("no_op_reason"),
        )
        if execution_failed:
            append_runtime_report_error(
                report,
                stage="strategy_execution",
                message=(
                    f"Strategy execution status is {execution_status}; "
                    f"reason={report_summary.get('no_op_reason') or 'unspecified'}"
                ),
                error_type="StrategyExecutionBlocked",
                failure_category="strategy_execution_blocked",
            )
        report_diagnostics = {
            "result": cycle_result.result,
            "price_source_mode": execution_summary.get("price_source_mode") or reconciliation_record.get("price_source_mode"),
            "snapshot_price_fallback_symbols": execution_summary.get("snapshot_price_fallback_symbols")
            or reconciliation_record.get("snapshot_price_fallback_symbols")
            or [],
            **({"signal_snapshot": signal_snapshot} if has_signal_snapshot else {}),
        }
        if execution_failed:
            report_diagnostics["failure_category"] = "strategy_execution_blocked"
        attach_cycle_execution_receipt(
            report,
            receipt_summary,
            reconciliation_record,
            execution_failed=execution_failed,
        )
        finalize_runtime_report(
            report,
            status="error" if execution_failed else "ok",
            summary=report_summary,
            diagnostics=report_diagnostics,
            artifacts={
                "reconciliation_record_path": cycle_result.reconciliation_record_path,
            },
        )
        log_runtime_event(
            log_context,
            "strategy_cycle_blocked" if execution_failed else "strategy_cycle_completed",
            message=(
                "Strategy execution blocked"
                if execution_failed
                else "Strategy dry-run completed"
                if dry_run_only_override
                else "Strategy execution completed"
            ),
            severity="ERROR" if execution_failed else "INFO",
            execution_window=execution_window,
            result=cycle_result.result,
            execution_status=report_summary.get("execution_status"),
            no_op_reason=report_summary.get("no_op_reason"),
        )
        return (response_body if dry_run_only_override else cycle_result.result), 200
    except RuntimeDeadlineExceeded as exc:
        deadline_exceeded = True
        append_runtime_report_error(
            report,
            stage="strategy_deadline",
            message=str(exc),
            error_type=type(exc).__name__,
        )
        finalize_runtime_report(
            report,
            status="error",
            diagnostics={
                "failure_category": "strategy_deadline",
                "timeout_seconds": IBKR_DRY_RUN_DEADLINE_SECONDS,
            },
        )
        raise
    except IBKRGatewayUnavailableError as exc:
        append_runtime_report_error(
            report,
            stage="ibkr_connect",
            message=str(exc),
            error_type=type(exc).__name__,
            failure_category="ibkr_gateway_unavailable",
        )
        finalize_runtime_report(
            report,
            status="error",
            diagnostics={"failure_category": "ibkr_gateway_unavailable"},
        )
        log_runtime_event(
            log_context,
            "ibkr_gateway_connect_failed",
            message="IBKR gateway connection attempts exhausted",
            severity="ERROR",
            error_type=type(exc).__name__,
            error_message=str(exc),
            failure_category="ibkr_gateway_unavailable",
        )
        error_msg = f"🚨 【IBKR 连接异常】\n{str(exc)}"
        _publish_runtime_failure_notification(
            detailed_text=error_msg,
            compact_text=error_msg,
            exc=exc,
        )
        return "Error", 503
    except (IBKRTradingPermissionError, IBKRPortfolioSnapshotUnavailableError) as exc:
        failure_category = (
            "ibkr_trading_permission"
            if isinstance(exc, IBKRTradingPermissionError)
            else "ibkr_portfolio_snapshot"
        )
        append_runtime_report_error(
            report,
            stage=failure_category,
            message=str(exc),
            error_type=type(exc).__name__,
            failure_category=failure_category,
        )
        finalize_runtime_report(
            report,
            status="error",
            diagnostics={"failure_category": failure_category},
        )
        log_runtime_event(
            log_context,
            f"{failure_category}_failed",
            message="IBKR execution readiness validation failed",
            severity="ERROR",
            error_type=type(exc).__name__,
            error_message=str(exc),
            failure_category=failure_category,
        )
        error_msg = f"🚨 【IBKR 执行条件异常】\n{str(exc)}"
        _publish_runtime_failure_notification(
            detailed_text=error_msg,
            compact_text=error_msg,
            exc=exc,
        )
        return "Error", 503
    except Exception as exc:
        append_runtime_report_error(
            report,
            stage="strategy_cycle",
            message="strategy_cycle_failed",
            error_type=type(exc).__name__,
        )
        finalize_runtime_report(report, status="error")
        log_runtime_event(
            log_context,
            "strategy_cycle_failed",
            message=t("runtime_failure_log"),
            severity="ERROR",
            error_type=type(exc).__name__,
            error_message="strategy_cycle_failed",
        )
        error_msg = _runtime_error_notification_message(exc)
        _publish_runtime_failure_notification(
            detailed_text=error_msg,
            compact_text=error_msg,
            exc=exc,
        )
        return "Error", 500
    finally:
        if lock_acquired:
            STRATEGY_RUN_LOCK.release()
        attach_signal_data_provenance(report)
        if "execution_receipt" not in report:
            try:
                attach_terminal_fallback_execution_receipt(report)
            except ValueError:
                # A legacy runtime target remains visible as evidence-missing;
                # reporting metadata must never change trading behavior.
                pass
        if not deadline_exceeded and persist_report:
            try:
                if dry_run_only_override is None:
                    report_path = persist_execution_report(report)
                else:
                    report_path = persist_execution_report(report, dry_run_only_override=dry_run_only_override)
                print(f"execution_report {report_path}", flush=True)
                from application.runtime_reporting_adapters import confirmed_gcs_report_uri
                if not confirmed_gcs_report_uri(report_path):
                    raise RuntimeError("durable_execution_report_unconfirmed")
            except Exception as persist_exc:
                print(f"failed to persist execution report: {persist_exc}", flush=True)
                if report.get("status") != "error":
                    from quant_platform_kit.common.operational_notification_localization import resolve_operational_notification_locale
                    zh = resolve_operational_notification_locale(NOTIFY_LANG) == "zh"
                    message = ("⚠️ 运行报告写入未确认；汇总观察可能缺失，请检查运行记录。" if zh else
                               "⚠️ Runtime report write unconfirmed; digest observations may be missing. Check run records.")
                    try:
                        if not send_tg_message(message):
                            print("notification_report_coverage_alert_failed", flush=True)
                    except Exception:
                        print("notification_report_coverage_alert_failed", flush=True)
def handle_request():
    if request.method != "POST":
        return "Method Not Allowed", 405
    return _route_with_runtime_error_fallback(_handle_request)
def _handle_dry_run_with_deadline(
    *,
    recycle_on_timeout: bool = True,
    timeout_state: dict[str, bool] | None = None,
):
    reports: list[dict] = []
    try:
        with enforce_runtime_deadline(
            IBKR_DRY_RUN_DEADLINE_SECONDS,
            operation="IBKR strategy dry-run request",
        ):
            response = _route_with_runtime_error_fallback(
                _handle_request,
                dry_run_only_override=True,
                response_body="Dry Run OK",
                dry_run_label="strategy dry-run",
                persist_report=False,
                report_collector=reports,
            )
    except RuntimeDeadlineExceeded as exc:
        timeout_stage = classify_runtime_deadline_stage(exc)
        qualification_caller = classify_runtime_deadline_qualification_caller(exc)
        if reports:
            diagnostics = reports[-1].setdefault("diagnostics", {})
            if isinstance(diagnostics, dict):
                diagnostics["timeout_stage"] = timeout_stage
                diagnostics["timeout_qualification_caller"] = qualification_caller
        _persist_dry_run_report_with_grace(reports, timeout_context=True)
        deadline_event = {
            "severity": "ERROR",
            "event": "strategy_cycle_deadline_exceeded",
            "message": "IBKR strategy dry-run exceeded its application deadline",
            "service_name": SERVICE_NAME or os.getenv("K_SERVICE"),
            "strategy_profile": STRATEGY_PROFILE,
            "timeout_seconds": IBKR_DRY_RUN_DEADLINE_SECONDS,
            "timeout_stage": timeout_stage,
            "timeout_qualification_caller": qualification_caller,
            "worker_recycle_required": True,
            "worker_recycle_scheduled": recycle_on_timeout,
        }
        if reports and reports[-1].get("run_id"):
            deadline_event["run_id"] = reports[-1]["run_id"]
        trace_id = request.headers.get("X-Cloud-Trace-Context", "").partition("/")[0]
        if PROJECT_ID and re.fullmatch(r"[0-9a-f]{32}", trace_id):
            deadline_event["logging.googleapis.com/trace"] = f"projects/{PROJECT_ID}/traces/{trace_id}"
        print(
            json.dumps(
                deadline_event,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            flush=True,
        )
        if timeout_state is not None:
            timeout_state["worker_recycle_required"] = True
        if recycle_on_timeout:
            recycle_current_process_after_response()
        return "Error", 503
    else:
        _persist_dry_run_report_with_grace(reports, timeout_context=False)
        return response
def _persist_dry_run_report_with_grace(
    reports: list[dict],
    *,
    timeout_context: bool,
) -> None:
    if not reports:
        return
    operation = "IBKR timeout report persistence" if timeout_context else "IBKR dry-run report persistence"
    try:
        with enforce_runtime_deadline(
            IBKR_DEADLINE_REPORT_GRACE_SECONDS,
            operation=operation,
        ):
            report_path = persist_execution_report(
                reports[-1],
                dry_run_only_override=True,
            )
            print(f"execution_report {report_path}", flush=True)
    except RuntimeDeadlineExceeded as persist_exc:
        print(f"dry-run report persistence timed out: {persist_exc}", flush=True)
    except Exception as persist_exc:
        print(f"failed to persist dry-run report: {persist_exc}", flush=True)
def handle_dry_run():
    return _handle_dry_run_with_deadline()
def handle_paper_execution_command_consumer():
    """Manual-only endpoint for paper command reconciliation evidence."""

    try:
        result = run_paper_execution_command_consumer()
    except Exception as exc:
        return _handle_route_runtime_error(exc, route_label="POST /paper-command-consumer")
    return json.dumps(result, ensure_ascii=False), 200, {"Content-Type": "application/json"}
def handle_probe():
    if request.method != "POST":
        return "Method Not Allowed", 405
    return _route_with_runtime_error_fallback(_handle_probe)
def handle_order_settlement():
    from application.settlement_route import handle_settle
    import sys
    return handle_settle(sys.modules[__name__])
def handle_reconciliation():
    return _route_with_runtime_error_fallback(
        _handle_reconciliation,
        route_label="broker-reconciliation",
    )
def handle_live_permission_probe():
    return _handle_live_permission_probe()
def handle_monitor_dispatch():
    return _route_with_runtime_error_fallback(
        _handle_monitor_dispatch,
        route_label="monitor-dispatch",
    )
def health():
    return health_impl()
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
