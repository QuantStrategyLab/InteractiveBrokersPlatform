"""Reviewed Python entry text passed to the existing digest-pinned image.

Only existing SDK read helpers and the existing isolated report writer run.
This module has no CLI parameters, strategy cycle, broker write or notification path.
"""
import os
import re
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace


def read_account_summary_cash(ib, account_ids, *, timeout_seconds=30,
                              clock=lambda: datetime.now(timezone.utc)):
    """One USD ledger summary, retaining only the selected account's USD rows.

    The pinned SDK fills accountValues and acctSummary separately. Listen to
    request-scoped callbacks before its cache overwrites duplicate fields, and require
    the matching accountSummaryEnd callback and SDK error propagation. All is
    the API's summary group; $LEDGER:USD selects per-currency cash tags. Rows
    for other managed accounts are neither buffered nor persisted. Identical
    decimal repeats within this request may share one observation; conflicting
    values remain invalid rather than choosing the first, last or larger value.
    """
    from application.ibkr_cash_observations import TAGS, capture_cash_observation_rows, build_cash_observations, _decimal_text
    if (not isinstance(account_ids, (tuple, list)) or len(account_ids) != 1
            or not re.fullmatch(r"(?:U|DU)[0-9]+", account_ids[0])
            or account_ids[0] not in ib.managedAccounts() or ib.wrapper.acctSummary
            or not 0 < timeout_seconds <= 30):
        raise ValueError("account_summary_source_not_fresh_or_scoped")
    rows, ended = [], False
    started = clock().isoformat()
    req_id = ib.client.getReqId()
    original = ib.wrapper.accountSummary
    original_end = ib.wrapper.accountSummaryEnd
    original_error_policy = ib.RaiseRequestErrors
    tags = tuple(sorted(TAGS))
    request_tags = "$LEDGER:USD"
    def collect(request_id, account, tag, value, currency):
        # The SDK event drops reqId. Its decoder resolves this wrapper method
        # per message, so preserve the original SDK callback and scope here.
        if not ended and request_id == req_id and account == account_ids[0] and tag in tags:
            rows.append(SimpleNamespace(account=account, tag=tag, value=value, currency=currency))
        original(request_id, account, tag, value, currency)
    def collect_end(request_id):
        nonlocal ended
        if request_id == req_id:
            ended = True
        original_end(request_id)
    future = None
    ib.wrapper.accountSummary = collect
    ib.wrapper.accountSummaryEnd = collect_end
    # With the SDK default False, request errors also resolve startReq's
    # future successfully. Completion alone is not accountSummaryEnd proof.
    ib.RaiseRequestErrors = True
    try:
        future = ib.wrapper.startReq(req_id)
        ib.client.reqAccountSummary(req_id, "All", request_tags)
        ib._run(asyncio.wait_for(future, timeout=timeout_seconds))
        if not ended:
            raise ValueError("account_summary_end_not_observed")
        finished = clock().isoformat()
    finally:
        ib.wrapper.accountSummary = original
        ib.wrapper.accountSummaryEnd = original_end
        ib.RaiseRequestErrors = original_error_policy
        if future is not None and not future.done():
            future.cancel()
        ib.client.cancelAccountSummary(req_id)
    diagnostics = {}
    for tag in tags:
        values = [row for row in rows if row.tag == tag]
        usd = [row for row in values if row.currency == "USD"]
        diagnostics[tag] = dict(usd_rows=len(usd), usd_invalid_values=sum(_decimal_text(row.value) is None for row in usd),
            usd_duplicate_rows=max(0, len(usd) - 1), base_rows=sum(row.currency == "BASE" for row in values),
            other_currency_rows=sum(row.currency not in {"USD", "BASE"} for row in values))
    observations = {}
    for row in rows:
        # Counts can diagnose unexpected currencies, but their values cannot
        # become USD observations or a conversion of the base-currency ledger.
        if row.currency != "USD":
            continue
        key = row.account, row.currency, row.tag
        amount = _decimal_text(row.value)
        if key not in observations:
            observations[key] = SimpleNamespace(account=row.account, currency=row.currency, tag=row.tag, value=amount)
        else:
            previous = observations[key]
            if amount is None or previous.value is None or Decimal(amount) != Decimal(previous.value):
                # Once ambiguous, a later repeat cannot revive this field.
                previous.value = None
    return dict(source_method="ibkr.reqAccountSummary", request_tags=request_tags, account_ids=list(account_ids),
        request_scoped=True, field_diagnostics=diagnostics,
        query_completed=True, snapshot_atomic=False, observed_started_at=started,
        observed_finished_at=finished, broker_cash_observations=build_cash_observations(
            capture_cash_observation_rows(observations.values(), account_ids=tuple(account_ids)), observed_at=finished))


def read_native_funding_inputs(ib, account_ids, *, configured_quantity_step, timeout_seconds=10,
                               clock=lambda: datetime.now(timezone.utc)):
    """Read a session's API orders and TQQQ metadata; never infer full obligations.

    The frozen image lacks the newer all-open-orders loader. Reuse the pinned
    SDK's completed async request and the original reconciliation normalizer,
    retaining both response and unresolved cache rows without binding orders.
    """
    from application.broker_reconciliation import _normalise_open_trade
    from application.ibkr_cash_observations import _decimal_text
    from ib_insync import Stock
    if (not isinstance(account_ids, (list, tuple)) or len(account_ids) != 1
            or not re.fullmatch(r"(?:U|DU)[0-9]+", account_ids[0])
            or account_ids[0] not in ib.managedAccounts() or type(ib.client.clientId) is not int
            or ib.client.clientId <= 0 or not 0 < timeout_seconds <= 10):
        raise ValueError("funding_source_not_scoped")
    started = clock().isoformat()
    snapshot = ib._run(asyncio.wait_for(ib.reqAllOpenOrdersAsync(), timeout_seconds))
    cached = ib.openTrades()
    if not isinstance(snapshot, (list, tuple)) or not isinstance(cached, (list, tuple)):
        raise ValueError("orders_response_incomplete")
    records = []
    for trade in {id(row): row for row in (*snapshot, *cached)}.values():
        row = _normalise_open_trade(trade, selected_account_ids=tuple(account_ids))
        if row is not None:
            records.append(row)
    orders_finished = clock().isoformat()
    details = ib._run(asyncio.wait_for(ib.reqContractDetailsAsync(Stock("TQQQ", "SMART", "USD")), timeout_seconds))
    rules = dict(status="UNAVAILABLE", symbol="TQQQ", currency="USD")
    if isinstance(details, (list, tuple)) and len(details) == 1:
        detail, contract = details[0], details[0].contract
        if (contract.symbol == "TQQQ" and contract.secType == "STK" and contract.currency == "USD"
                and type(contract.conId) is int and contract.conId > 0 and ib.client.serverVersion() >= 164):
            from decimal import Decimal
            sizes = {name: _decimal_text(getattr(detail, name, None)) for name in ("minSize", "sizeIncrement")}
            if all(value is not None and Decimal(value) > 0 for value in sizes.values()):
                rules.update(status="OBSERVED", con_id=contract.conId, **sizes)
    return dict(schema_version="ibkr.funding_inputs.v1", account_ids=list(account_ids),
        source_scope="TWS_SESSION_API_ORDERS_AND_CONTRACT_METADATA", snapshot_atomic=False,
        observed_started_at=started, orders_finished_at=orders_finished, observed_finished_at=clock().isoformat(),
        open_orders_query_completed=True, open_order_count=len(records), open_orders=records,
        contract_rules=rules, configured_quantity_step=_decimal_text(configured_quantity_step),
        complete_cash_obligations_verified=False, account_fee_plan_verified=False, account_trading_permissions_verified=False)


def observe(runtime, build_facts, env, *, summary_reader=None, funding_reader=None, report_sink=None):
    job, execution = env.get("CLOUD_RUN_JOB", ""), env.get("CLOUD_RUN_EXECUTION", "")
    if (not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", job)
            or not re.fullmatch(re.escape(job) + r"-[a-z0-9]+", execution)
            or any(env.get(key) != value for key, value in (
                ("CLOUD_RUN_TASK_INDEX", "0"), ("CLOUD_RUN_TASK_ATTEMPT", "0"), ("CLOUD_RUN_TASK_COUNT", "1")))
            or env.get("K_REVISION") or env.get("K_SERVICE")):
        raise ValueError("job_identity_invalid")
    context = runtime.build_composer(dry_run_only_override=True).build_reporting_adapters().build_log_context()
    report = runtime.build_execution_report(context, dry_run_only_override=True)
    report.setdefault("diagnostics", {}).update(
        maintenance_cash_observation=True, execution_kind="cloud_run_job",
        cloud_run_job=job, cloud_run_execution=execution, task_index=0, task_attempt=0, task_count=1)
    ib, result, stage = None, 1, "connection"
    try:
        ib = runtime.connect_ib(read_only=True, validate_trading_permissions=False)
        stage = "snapshot"
        facts = build_facts(runtime.build_portfolio_snapshot(ib))
        stage = "account_identity"
        if not facts or not runtime._account_facts_match_runtime_target(facts):
            raise ValueError("account_identity_unverified")
        summary = {"account_facts": facts}
        if summary_reader is not None:
            stage = "account_summary"
            summary["broker_account_summary"] = summary_reader(ib, facts["account_ids"])
        if funding_reader is not None:
            stage = "funding_inputs"
            summary["broker_funding_inputs"] = funding_reader(ib, facts["account_ids"])
        runtime.finalize_runtime_report(report, status="ok", summary=summary)
        result = 0
    except Exception:
        # Errors may contain broker endpoints or financial values. Preserve a
        # fixed failure state, never copy the exception into output or report.
        runtime.finalize_runtime_report(report, status="error", diagnostics={
            "cash_observation_failed": True, "cash_observation_failure_stage": stage})
    finally:
        if ib is not None:
            try:
                ib.disconnect()
            except Exception:
                result = 1
        uri = runtime.persist_execution_report(report, dry_run_only_override=True)
        if not isinstance(uri, str) or not uri.startswith("gs://"):
            raise ValueError("private_report_save_unverified")
        if report_sink is None:
            print("execution_report " + uri, flush=True)
        else:
            report_sink(uri)
    return result


if __name__ == "__main__":
    import main
    from application.account_facts import build_ibkr_account_facts
    raise SystemExit(observe(main, build_ibkr_account_facts, os.environ, summary_reader=read_account_summary_cash,
        funding_reader=lambda ib, ids: read_native_funding_inputs(ib, ids,
            configured_quantity_step=main.RUNTIME_SETTINGS.quantity_step)))
