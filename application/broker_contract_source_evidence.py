"""Status-only coverage of existing runtime reports, never account authority.

Call only after the existing report identity/time guard. This schema records
cash projections and cycle observations, not an account pricing schedule,
security permission grant or a complete account-obligation enumeration.
"""
from collections.abc import Mapping

from application.ibkr_cash_observations import inspect_cash_observations

SCHEMA = "ibkr.broker_contract_source_evidence.v1"
GAPS = (
    "ACCOUNT_FEE_PLAN_NOT_ATTESTED_BY_RUNTIME_REPORT",
    "SECURITY_TRADING_PERMISSION_NOT_ATTESTED_BY_RUNTIME_REPORT",
    "FRACTIONAL_PERMISSION_NOT_ATTESTED_BY_RUNTIME_REPORT",
    "ACCOUNT_QUANTITY_RULES_NOT_ATTESTED_BY_RUNTIME_REPORT",
    "COMPLETE_ACCOUNT_OBLIGATIONS_NOT_ATTESTED_BY_RUNTIME_REPORT",
)
# These are actual producer paths, not a search for guessed financial keys.
PATHS = (
    "market_exchange",
    "summary.account_facts.cash", "summary.account_facts.broker_reported_positions",
    "summary.account_facts.broker_cash_observations",
    "summary.managed_symbols", "summary.execution_status", "summary.no_op_reason",
    "summary.orders_submitted_count", "execution_receipt",
    "diagnostics.failure_category", "diagnostics.skip_reason",
    "diagnostics.broker_reconciliation.evidence.open_orders_match",
)
CASH_TAGS = frozenset(("$LEDGER-CashBalance", "$LEDGER-TotalCashBalance", "CashBalance",
    "TotalCashBalance", "SettledCash"))


def _field(report, path):
    value = report
    for key in path.split("."):
        if not isinstance(value, Mapping) or key not in value:
            return "missing", None
        value = value[key]
    if value is None:
        return "null", None
    kind = "object" if isinstance(value, Mapping) else "array" if isinstance(value, (list, tuple)) else "scalar"
    return kind, value


def inspect_broker_contract_sources(report):
    """Return fixed categories only; neither amounts nor free text escape."""
    if not isinstance(report, Mapping) or report.get("schema_version") != "runtime_report.v1":
        raise ValueError("BROKER_SOURCE_REPORT_SCHEMA_REQUIRED")
    fields = {path: _field(report, path)[0] for path in PATHS}
    routing_kind, exchange = _field(report, "market_exchange")
    routing_status = "NOT_REPORTED" if routing_kind == "missing" else "NULL" if routing_kind == "null" else "INVALID"
    if (isinstance(exchange, str) and exchange and len(exchange) <= 64
            and exchange == exchange.strip() and exchange.isprintable()):
        routing_status = "SMART_REPORTED" if exchange == "SMART" else "OTHER_REPORTED"
    _, cash = _field(report, "summary.account_facts.cash")
    cash_tag, cash_status = None, "NOT_OBSERVED"
    if isinstance(cash, list):
        usd = [row for row in cash if isinstance(row, Mapping) and row.get("currency") == "USD"]
        if (len(usd) == 1 and isinstance(usd[0].get("source_tag"), str)
                and usd[0]["source_tag"] in CASH_TAGS):
            cash_tag, cash_status = usd[0]["source_tag"], "OBSERVED_TAG_ONLY"
        elif usd:
            cash_status = "INVALID_OR_AMBIGUOUS"
    else:
        cash_status = "UNKNOWN"
    _, facts = _field(report, "summary.account_facts")
    facts = facts if isinstance(facts, Mapping) else {}
    usd = [row for row in cash if isinstance(row, Mapping) and row.get("currency") == "USD"] if isinstance(cash, list) else []
    native = inspect_cash_observations(facts.get("broker_cash_observations"),
        observed_at=facts.get("observed_at"), selected_cash=usd[0] if len(usd) == 1 else None)
    settled_observed = native["fields"]["SettledCash"] == "OBSERVED" or (
        native["status"] == "NOT_REPORTED" and cash_tag == "SettledCash")
    _, failure = _field(report, "diagnostics.failure_category")
    _, skipped = _field(report, "diagnostics.skip_reason")
    _, execution_status = _field(report, "summary.execution_status")
    _, reason = _field(report, "summary.no_op_reason")
    # A bounded negative observation is useful; success or zero counts never
    # establishes product permissions or whole-account cash completeness.
    pending = execution_status == "pending_reconciliation" or (isinstance(reason, str)
        and reason.split(":", 1)[0] in {"pending_orders_detected", "pending_orders_unavailable",
            "broker_order_outcome_unknown", "broker_order_pending_confirmation", "pending_sell_release"})
    gaps = list(GAPS)
    if not settled_observed:
        gaps.append("USD_SETTLED_CASH_NOT_OBSERVED_IN_PROJECTED_CASH")
    return dict(schema_version=SCHEMA, status="SOURCE_INSPECTED_CONTRACT_GAPS",
        scope="SINGLE_RUNTIME_REPORT_ONLY", source_fields=fields,
        routing_observation=dict(status=routing_status, source_path="market_exchange",
            scope="RUNTIME_REPORT_CONFIGURATION_ONLY", actual_order_route_verified=False),
        native_cash_fields=native,
        usd_cash_observation=dict(status=cash_status, source_tag=cash_tag,
            settled_tag_observed=settled_observed, spendable_cash_verified=False),
        api_permission_failure_recorded=failure == "ibkr_trading_permission",
        fractional_api_block_recorded=skipped == "ibkr_fractional_equity_api_unsupported",
        cycle_pending_or_unknown_recorded=pending,
        whole_account_pending_state="UNKNOWN", account_fee_plan_verified=False,
        security_permissions_verified=False, fractional_permission_verified=False,
        account_quantity_rules_verified=False, complete_cash_obligations_verified=False,
        gaps=gaps, execution_authorized=False, settings_applied=False, breaker_reset=False)
