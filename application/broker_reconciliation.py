"""Read-only IBKR observations used by frozen-live reconciliation.

The module deliberately does not decide whether a baseline can resume.  It
only reads the broker surfaces that a higher-level reconciliation contract must
compare with a durable local ledger: configured account identity, positions,
cash, active orders, and recent executions.  Callers must hash the returned
normalised values locally and must never log or return them from an HTTP route.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from typing import Any

from quant_platform_kit.common.broker_reconciliation import (
    BrokerReconciliationEvidence,
    BrokerReconciliationFinding,
    build_broker_reconciliation_evidence,
    calculate_broker_observation_sha256,
    evaluate_broker_reconciliation_recovery,
)
from quant_platform_kit.common.execution_state import build_execution_marker_store_from_env


class IBKRReconciliationReadError(RuntimeError):
    """Raised when a required read-only broker surface cannot be reconciled."""


IBKR_RECONCILIATION_EXPECTED_DIGESTS_ENV = "IBKR_RECONCILIATION_EXPECTED_DIGESTS_JSON"
_EXPECTED_DIGEST_KEYS = (
    "account_scope_sha256",
    "positions_sha256",
    "cash_sha256",
    "open_orders_sha256",
    "recent_executions_sha256",
    "local_execution_ledger_sha256",
)

# These are actual cash-balance tags, in the same order as the execution
# snapshot's cash selector.  Margin capacity and mark-to-market account tags
# (for example AvailableFunds and NetLiquidation) must never enter a recovery
# digest: they legitimately move while no cash, order, or position changes.
_CASH_BALANCE_TAG_PRIORITY = (
    "$LEDGER-CashBalance",
    "$LEDGER-TotalCashBalance",
    "CashBalance",
    "TotalCashBalance",
    "SettledCash",
)


def _text(value: object) -> str:
    return str(value or "").strip()


def _number(value: object, *, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise IBKRReconciliationReadError(f"IBKR reconciliation is missing {field_name}.") from exc


def normalize_account_ids(account_ids: Iterable[str] | str | None) -> tuple[str, ...]:
    if account_ids is None:
        return ()
    candidates = (account_ids,) if isinstance(account_ids, str) else tuple(account_ids)
    normalized = tuple(dict.fromkeys(_text(account_id) for account_id in candidates if _text(account_id)))
    if not normalized:
        raise IBKRReconciliationReadError(
            "IBKR reconciliation requires one or more configured account ids."
        )
    return normalized


def _account_in_scope(account_id: object, *, selected_account_ids: tuple[str, ...], surface: str) -> bool:
    normalized = _text(account_id)
    if not normalized:
        raise IBKRReconciliationReadError(
            f"IBKR reconciliation received an unscoped {surface} record."
        )
    return normalized in selected_account_ids


def _safe_contract_fields(contract: Any) -> dict[str, object]:
    return {
        "con_id": _text(getattr(contract, "conId", "")),
        "symbol": _text(getattr(contract, "symbol", "")).upper(),
        "local_symbol": _text(getattr(contract, "localSymbol", "")),
        "sec_type": _text(getattr(contract, "secType", "")).upper(),
        "currency": _text(getattr(contract, "currency", "")).upper(),
    }


def _normalise_position(position: Any) -> dict[str, object]:
    return {
        "symbol": _text(getattr(position, "symbol", "")).upper(),
        "quantity": _number(getattr(position, "quantity", None), field_name="position quantity"),
        "average_cost": _number(
            getattr(position, "average_cost", None), field_name="position average cost"
        ),
        "currency": _text(getattr(position, "currency", "")).upper(),
    }


def _normalise_option_position(
    position: Mapping[str, object],
    *,
    selected_account_ids: tuple[str, ...],
) -> dict[str, object]:
    account_id = _text(position.get("account_id"))
    if not _account_in_scope(account_id, selected_account_ids=selected_account_ids, surface="option position"):
        raise IBKRReconciliationReadError(
            "IBKR reconciliation received an out-of-scope option position."
        )
    return {
        "account": account_id,
        "underlier": _text(position.get("underlier")).upper(),
        "local_symbol": _text(position.get("local_symbol")),
        "expiration": _text(position.get("expiration")),
        "right": _text(position.get("right")).upper(),
        "strike": _number(position.get("strike"), field_name="option strike"),
        "quantity": _number(position.get("quantity"), field_name="option quantity"),
        "average_cost": _number(position.get("average_cost"), field_name="option average cost"),
        "currency": _text(position.get("currency")).upper(),
    }


def _normalise_cash_balance(value: Mapping[str, object], *, selected_account_ids: tuple[str, ...]) -> dict[str, object]:
    account_id = _text(value.get("account_id"))
    if not _account_in_scope(account_id, selected_account_ids=selected_account_ids, surface="cash"):
        raise IBKRReconciliationReadError("IBKR reconciliation received an out-of-scope cash record.")
    numeric_tags = {
        _text(tag): _number(number, field_name=f"cash tag {_text(tag)}")
        for tag, number in value.items()
        if _text(tag) not in {"account_id", "currency"}
    }
    for tag in _CASH_BALANCE_TAG_PRIORITY:
        if tag in numeric_tags:
            return {
                "account": account_id,
                "currency": _text(value.get("currency")).upper(),
                "tags": {tag: numeric_tags[tag]},
            }
    raise IBKRReconciliationReadError(
        "IBKR reconciliation is missing a stable cash-balance tag."
    )


def _open_trade_account(trade: Any) -> object:
    order = getattr(trade, "order", None)
    return getattr(order, "account", None) or getattr(trade, "account", None)


def _normalise_open_trade(trade: Any, *, selected_account_ids: tuple[str, ...]) -> dict[str, object] | None:
    account_id = _open_trade_account(trade)
    if not _account_in_scope(account_id, selected_account_ids=selected_account_ids, surface="open order"):
        return None
    order = getattr(trade, "order", trade)
    status = getattr(trade, "orderStatus", None)
    contract = getattr(trade, "contract", None)
    if contract is None:
        raise IBKRReconciliationReadError("IBKR reconciliation received an open order without a contract.")
    return {
        "account": _text(account_id),
        "contract": _safe_contract_fields(contract),
        "perm_id": _text(getattr(order, "permId", "")),
        "action": _text(getattr(order, "action", "")).upper(),
        "order_type": _text(getattr(order, "orderType", "")).upper(),
        "total_quantity": _number(
            getattr(order, "totalQuantity", None), field_name="open order total quantity"
        ),
        "limit_price": _number(getattr(order, "lmtPrice", 0.0), field_name="open order limit price"),
        "aux_price": _number(getattr(order, "auxPrice", 0.0), field_name="open order aux price"),
        "status": _text(getattr(status, "status", "")),
        "filled": _number(getattr(status, "filled", 0.0), field_name="open order filled quantity"),
        "remaining": _number(
            getattr(status, "remaining", 0.0), field_name="open order remaining quantity"
        ),
    }


def _execution_account(fill: Any) -> object:
    execution = getattr(fill, "execution", None)
    account_id = getattr(execution, "acctNumber", None)
    if _text(account_id):
        return account_id
    order = getattr(getattr(fill, "trade", None), "order", None)
    return getattr(order, "account", None)


def _normalise_execution(fill: Any, *, selected_account_ids: tuple[str, ...]) -> dict[str, object] | None:
    account_id = _execution_account(fill)
    if not _account_in_scope(account_id, selected_account_ids=selected_account_ids, surface="execution"):
        return None
    execution = getattr(fill, "execution", None)
    contract = getattr(fill, "contract", None)
    if execution is None or contract is None:
        raise IBKRReconciliationReadError("IBKR reconciliation received an incomplete execution record.")
    return {
        "account": _text(account_id),
        "contract": _safe_contract_fields(contract),
        "execution_id": _text(getattr(execution, "execId", "")),
        "order_id": _text(getattr(execution, "orderId", "")),
        "time": _text(getattr(execution, "time", "")),
        "side": _text(getattr(execution, "side", "")).upper(),
        "shares": _number(getattr(execution, "shares", None), field_name="execution shares"),
        "price": _number(getattr(execution, "price", None), field_name="execution price"),
    }


def _sorted_records(records: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    return sorted((dict(record) for record in records), key=lambda record: repr(sorted(record.items())))


def load_read_only_open_trades(ib: Any) -> tuple[Any, ...]:
    """Read one completed all-client snapshot, retaining unresolved cache rows.

    This is the blocking ib_insync API, not the native fire-and-forget method.
    An empty local cache or a missing response cannot establish an empty book.
    Cached active trades remain conservative evidence until terminal updates or
    reconciliation resolve them; this reader never cancels or binds orders.
    """
    request_all_open_orders = getattr(ib, "reqAllOpenOrders", None)
    open_trades = getattr(ib, "openTrades", None)
    if not callable(request_all_open_orders) or not callable(open_trades):
        raise IBKRReconciliationReadError(
            "IBKR reconciliation requires read-only all-open-orders support."
        )
    original_timeout = getattr(ib, "RequestTimeout", 0)
    original_raise_errors = getattr(ib, "RaiseRequestErrors", False)
    try:
        timeout = float(original_timeout)
    except (TypeError, ValueError):
        timeout = 0.0
    # An operational read deadline, not a financial risk threshold. Preserve a
    # shorter existing deadline and restore both SDK settings on every exit.
    timeout = min(timeout, 10.0) if math.isfinite(timeout) and timeout > 0 else 10.0
    try:
        ib.RequestTimeout = timeout
        ib.RaiseRequestErrors = True
        snapshot = request_all_open_orders()
        cached = open_trades()
        if not isinstance(snapshot, (list, tuple)) or not isinstance(cached, (list, tuple)):
            raise IBKRReconciliationReadError("IBKR open-order snapshot is incomplete.")
        # ib_insync returns the same Trade objects through both surfaces. Do
        # not count those objects twice, or discard a locally pending submit
        # merely because it was absent from this point-in-time response.
        return tuple({id(trade): trade for trade in (*snapshot, *cached)}.values())
    except Exception:
        raise IBKRReconciliationReadError(
            "IBKR reconciliation could not load all active orders."
        ) from None
    finally:
        ib.RequestTimeout = original_timeout
        ib.RaiseRequestErrors = original_raise_errors


def _load_recent_executions(ib: Any, *, execution_filter: Any = None, require_response: bool = False) -> tuple[Any, ...]:
    request_executions = getattr(ib, "reqExecutions", None)
    if not callable(request_executions):
        raise IBKRReconciliationReadError(
            "IBKR reconciliation requires read-only recent-executions support."
        )
    original_raise_errors = getattr(ib, "RaiseRequestErrors", False)
    try:
        if require_response:
            ib.RaiseRequestErrors = True
        result = request_executions(execution_filter) if execution_filter is not None else request_executions()
        if require_response and not isinstance(result, (list, tuple)):
            raise IBKRReconciliationReadError("IBKR recent-executions response is incomplete.")
        return tuple(result or ())
    except Exception as exc:
        raise IBKRReconciliationReadError(
            "IBKR reconciliation could not load recent executions."
        ) from exc
    finally:
        if require_response:
            ib.RaiseRequestErrors = original_raise_errors


@dataclass(frozen=True)
class IBKRReconciliationObservations:
    """Sensitive, in-memory broker observations; never serialize to responses."""

    account_scope: Mapping[str, object]
    account_identity_match: bool
    positions: tuple[Mapping[str, object], ...]
    cash: tuple[Mapping[str, object], ...]
    open_orders: tuple[Mapping[str, object], ...]
    recent_executions: tuple[Mapping[str, object], ...]
    # The v1 receipt has no per-surface coverage information.
    comparison_covered: frozenset[str] = frozenset()
    # Optional display-only copy; unlike the legacy digest it preserves missing fields.
    initial_open_orders: tuple[Mapping[str, object], ...] | None = None
    initial_execution_scope: Mapping[str, str] | None = None
    initial_market_currency: str | None = None
    initial_snapshot_at: datetime | None = None
    initial_positions_complete: bool = False
    initial_cash_complete: bool = False


def build_initial_enrollment_facts(
    observations: IBKRReconciliationObservations, *, observed_at: datetime,
    ledger_records_count: int | None, ledger_truncated: bool | None = None,
) -> tuple[dict[str, object], str]:
    """Build private display facts and their version; never return or log the facts publicly."""
    if not observations.account_identity_match or observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise IBKRReconciliationReadError("Initial enrollment requires a scoped account and timestamp.")
    if ledger_records_count is not None and (type(ledger_records_count) is not int or ledger_records_count < 0):
        raise IBKRReconciliationReadError("Initial enrollment ledger count is invalid.")

    def display(value: object) -> object:
        if value is None or isinstance(value, str):
            return value
        if type(value) in (int, float):
            if not math.isfinite(value):
                raise IBKRReconciliationReadError("Initial enrollment numeric value is invalid.")
            return str(value)
        if isinstance(value, Mapping):
            return {str(k): display(v) for k, v in value.items() if k != "account"}
        raise IBKRReconciliationReadError("Initial enrollment field is invalid.")

    def rows(values: tuple[Mapping[str, object], ...]) -> list[object]:
        items = [display(item) for item in values]
        return sorted(items, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")))

    order_rows = observations.initial_open_orders
    order_status = "unknown" if order_rows is None else (
        "partial" if any(any(row.get(k) is None for k in ("limit_price", "aux_price", "filled", "remaining")) for row in order_rows)
        else "complete"
    )
    def scoped(kind: str, *, currency: str | None = None, start: str | None = None,
               end: str | None = None, limit: str | None = None, gateway: str | None = None,
               truncated: str | None = None) -> dict[str, str | None]:
        return {"kind": kind, "currency": currency, "window_start": start, "window_end": end,
                "limit_per_namespace": limit, "gateway_availability": gateway, "truncated": truncated}

    def surface(values: tuple[Mapping[str, object], ...], *, complete: bool, scope: dict[str, str | None]) -> dict[str, object]:
        return {"status": "complete" if complete else "partial", "items": rows(values) if complete or values else None, "scope": scope}

    snapshot_at = observations.initial_snapshot_at
    snapshot_time = (snapshot_at.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                     if snapshot_at is not None and snapshot_at.tzinfo is not None and snapshot_at.utcoffset() is not None else None)
    observed_time = observed_at.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    execution_scope = observations.initial_execution_scope
    ledger_complete = ledger_records_count is not None and ledger_truncated is False
    position_currencies = {str(row.get("currency") or "").upper() for row in observations.positions}
    cash_currencies = {str(row.get("currency") or "").upper() for row in observations.cash}
    position_scope_currency = observations.initial_market_currency if not position_currencies or position_currencies == {observations.initial_market_currency} else None
    cash_scope_currency = observations.initial_market_currency if not cash_currencies or cash_currencies == {observations.initial_market_currency} else None

    surfaces = {
        "positions": surface(observations.positions, complete=observations.initial_positions_complete and bool(snapshot_time),
                             scope=scoped("account_strategy_positions", currency=position_scope_currency, end=snapshot_time)),
        "cash": surface(observations.cash, complete=observations.initial_cash_complete and bool(snapshot_time),
                        scope=scoped("returned_account_cash", currency=cash_scope_currency, end=snapshot_time)),
        "open_orders": {"status": order_status, "items": None if order_rows is None else rows(order_rows),
                        "scope": scoped("all_client_open_orders", end=observed_time)},
        "recent_executions": surface(observations.recent_executions, complete=execution_scope is not None,
                                     scope=scoped("broker_requested_window", start=execution_scope.get("requested_from") if execution_scope else None,
                                                  end=observed_time, gateway=execution_scope.get("gateway_availability") if execution_scope else None)),
        "local_execution_ledger": {"status": "unknown" if ledger_records_count is None else "complete" if ledger_complete else "partial",
                                   "items": None if ledger_records_count is None else [{"record_count": str(ledger_records_count)}],
                                   "scope": scoped("bounded_live_claims_and_outcomes", end=observed_time, limit="256",
                                                   truncated=None if ledger_truncated is None else str(ledger_truncated).lower())},
    }
    facts = {"schema_version": "ibkr_initial_facts.v1",
             "account_scope_sha256": calculate_broker_observation_sha256(observations.account_scope),
             "observed_at": observed_at.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
             "surfaces": surfaces}
    payload = json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return facts, hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class IBKRReconciliationCandidate:
    """Public-safe recovery candidate; the raw broker observations are omitted."""

    evidence: BrokerReconciliationEvidence
    recovery_blockers: tuple[BrokerReconciliationFinding, ...]
    expected_digests_configured: bool
    execution_ledger_records_count: int
    comparison_covered: frozenset[str] = frozenset()
    initial_ledger_truncated: bool | None = None

    @property
    def permits_active_lkg(self) -> bool:
        return not self.recovery_blockers

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "schema_version": "ibkr_reconciliation_candidate.v1",
            "permits_active_lkg": self.permits_active_lkg,
            "expected_digests_configured": self.expected_digests_configured,
            "execution_ledger_records_count": self.execution_ledger_records_count,
            "recovery_blockers": [finding.value for finding in self.recovery_blockers],
            "evidence": self.evidence.to_dict(),
        }


def collect_read_only_reconciliation_observations(
    ib: Any,
    *,
    account_ids: Iterable[str] | str | None,
    fetch_portfolio_snapshot: Callable[..., Any],
    market_currency: str,
    cash_only_execution: bool,
    initial_enrollment: bool = False,
) -> IBKRReconciliationObservations:
    """Read and normalise all broker surfaces needed by a continuity check.

    ``fetch_portfolio_snapshot`` is injected so this adapter uses the existing
    market-aware platform portfolio reader.  This keeps reconciliation scoped
    to exactly the account/currency semantics used by execution.
    """

    selected_account_ids = normalize_account_ids(account_ids)
    managed_accounts_fn = getattr(ib, "managedAccounts", None)
    if not callable(managed_accounts_fn):
        raise IBKRReconciliationReadError(
            "IBKR reconciliation requires a managed-accounts identity response."
        )
    try:
        managed_accounts = {_text(account_id) for account_id in (managed_accounts_fn() or ()) if _text(account_id)}
    except Exception as exc:
        raise IBKRReconciliationReadError(
            "IBKR reconciliation could not load managed-account identity."
        ) from exc
    identity_match = set(selected_account_ids).issubset(managed_accounts)
    snapshot = fetch_portfolio_snapshot(
        ib,
        account_ids=selected_account_ids,
        currency=market_currency,
        cash_only_execution=cash_only_execution,
    )
    metadata = getattr(snapshot, "metadata", {})
    if not isinstance(metadata, Mapping):
        raise IBKRReconciliationReadError("IBKR reconciliation portfolio snapshot metadata is invalid.")
    cash_balances = metadata.get("cash_balances")
    if not isinstance(cash_balances, (list, tuple)):
        raise IBKRReconciliationReadError("IBKR reconciliation is missing scoped cash balances.")
    option_positions = metadata.get("option_positions") or ()
    if not isinstance(option_positions, (list, tuple)):
        raise IBKRReconciliationReadError("IBKR reconciliation option-position metadata is invalid.")

    if any(not isinstance(option_position, Mapping) for option_position in option_positions):
        raise IBKRReconciliationReadError("IBKR reconciliation option-position metadata is invalid.")
    if any(not isinstance(value, Mapping) for value in cash_balances):
        raise IBKRReconciliationReadError("IBKR reconciliation cash-balance metadata is invalid.")
    positions = [_normalise_position(position) for position in (getattr(snapshot, "positions", ()) or ())]
    positions.extend(
        _normalise_option_position(
            option_position,
            selected_account_ids=selected_account_ids,
        )
        for option_position in option_positions
    )
    open_orders = []
    initial_open_orders = [] if initial_enrollment else None
    for trade in load_read_only_open_trades(ib):
        record = _normalise_open_trade(trade, selected_account_ids=selected_account_ids)
        if record is None:
            continue
        open_orders.append(record)
        if initial_open_orders is not None:
            display = dict(record)
            order, order_status = getattr(trade, "order", None), getattr(trade, "orderStatus", None)
            for field, source, attribute in (("limit_price", order, "lmtPrice"), ("aux_price", order, "auxPrice"),
                                             ("filled", order_status, "filled"), ("remaining", order_status, "remaining")):
                if source is None or getattr(source, attribute, None) is None:
                    display[field] = None
            initial_open_orders.append(display)
    execution_scope = None
    if initial_enrollment:
        if len(selected_account_ids) != 1:
            raise IBKRReconciliationReadError("Initial enrollment requires exactly one account.")
        from ib_insync import ExecutionFilter
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        native_filter = ExecutionFilter(acctCode=selected_account_ids[0], time=start.strftime("%Y%m%d %H:%M:%S UTC"))
        native_executions = _load_recent_executions(ib, execution_filter=native_filter, require_response=True)
        execution_scope = {"requested_from": start.isoformat().replace("+00:00", "Z"),
                           "gateway_availability": "current_broker_day"}
    else:
        native_executions = _load_recent_executions(ib)
    executions = [
        record
        for fill in native_executions
        if (record := _normalise_execution(fill, selected_account_ids=selected_account_ids)) is not None
    ]
    return IBKRReconciliationObservations(
        account_scope={"account_ids": sorted(selected_account_ids)},
        account_identity_match=identity_match,
        comparison_covered=frozenset({"open_orders"}) if identity_match else frozenset(),
        positions=tuple(_sorted_records(positions)),
        cash=tuple(
            _sorted_records(
                _normalise_cash_balance(value, selected_account_ids=selected_account_ids)
                for value in cash_balances
            )
        ),
        open_orders=tuple(_sorted_records(open_orders)),
        recent_executions=tuple(_sorted_records(executions)),
        initial_open_orders=tuple(_sorted_records(initial_open_orders)) if initial_open_orders is not None else None,
        initial_execution_scope=execution_scope,
        initial_market_currency=_text(market_currency).upper() if initial_enrollment else None,
        initial_snapshot_at=getattr(snapshot, "as_of", None) if initial_enrollment else None,
        initial_positions_complete=metadata.get("initial_positions_source_complete") is True if initial_enrollment else False,
        initial_cash_complete=(bool(cash_balances) and all(_text(v.get("currency")) and _text(v.get("account_id")) for v in cash_balances)
                               and any(_text(v.get("currency")).upper() == _text(market_currency).upper() for v in cash_balances)) if initial_enrollment else False,
    )


def _resolve_expected_digests(
    *,
    env_reader: Callable[[str, str | None], str | None] = os.getenv,
) -> Mapping[str, str] | None:
    raw_value = env_reader(IBKR_RECONCILIATION_EXPECTED_DIGESTS_ENV, None)
    if not _text(raw_value):
        return None
    try:
        decoded = json.loads(str(raw_value))
    except (TypeError, ValueError) as exc:
        raise IBKRReconciliationReadError(
            "IBKR reconciliation expected-digest configuration is not valid JSON."
        ) from exc
    if not isinstance(decoded, Mapping):
        raise IBKRReconciliationReadError(
            "IBKR reconciliation expected-digest configuration must be an object."
        )
    normalized = {key: _text(decoded.get(key)) for key in _EXPECTED_DIGEST_KEYS}
    if any(not value for value in normalized.values()):
        raise IBKRReconciliationReadError(
            "IBKR reconciliation expected-digest configuration is incomplete."
        )
    return normalized


def _continuity_fields(runtime_target: Any) -> tuple[str, str, str]:
    continuity = getattr(runtime_target, "live_continuity", None)
    if continuity is None:
        raise IBKRReconciliationReadError(
            "IBKR reconciliation requires a frozen live-continuity baseline."
        )
    baseline_id = _text(getattr(continuity, "baseline_id", ""))
    baseline_target_sha256 = _text(getattr(continuity, "baseline_target_sha256", "")).lower()
    if not baseline_id or not baseline_target_sha256:
        raise IBKRReconciliationReadError(
            "IBKR reconciliation live-continuity baseline is incomplete."
        )
    # ``resolve_runtime_target_from_env`` verifies this digest against the
    # original RUNTIME_TARGET_JSON before it returns RuntimeTarget.  Do not
    # re-fingerprint ``RuntimeTarget.to_dict()`` here: that representation
    # adds derived execution fields, so a legacy JSON baseline could be
    # falsely reported as changed even though the deployed target is valid.
    # The broker evidence remains strictly bound to the startup-validated
    # baseline; this only removes a representation-level false mismatch.
    return baseline_id, baseline_target_sha256, baseline_target_sha256


def build_reconciliation_candidate(
    *,
    observations: IBKRReconciliationObservations,
    runtime_target: Any,
    platform_id: str,
    strategy_profile: str,
    account_group: str,
    project_id: str | None,
    env_reader: Callable[[str, str | None], str | None] = os.getenv,
    observed_at: datetime | None = None,
    initial_enrollment: bool = False,
) -> IBKRReconciliationCandidate:
    """Build a fail-closed, redacted recovery candidate from read-only data.

    The expected digests are deliberately optional for the *first* legacy
    probe.  If they do not yet exist, every state surface stays unmatched and
    the candidate remains in ``RECONCILE_ONLY``.  A trusted control plane must
    independently establish those digests before a later candidate can pass.
    """

    expected_digests = _resolve_expected_digests(env_reader=env_reader)
    account_scope_sha256 = calculate_broker_observation_sha256(observations.account_scope)
    positions_sha256 = calculate_broker_observation_sha256(observations.positions)
    cash_sha256 = calculate_broker_observation_sha256(observations.cash)
    open_orders_sha256 = calculate_broker_observation_sha256(observations.open_orders)
    recent_executions_sha256 = calculate_broker_observation_sha256(observations.recent_executions)
    execution_state_store = build_execution_marker_store_from_env(
        platform_env_prefix="IBKR",
        env_reader=env_reader,
        project_id=project_id,
    )
    ledger_args = dict(
            platform=platform_id,
            strategy_profile=strategy_profile,
            account_scope=account_group,
            execution_mode="live",
    )
    if initial_enrollment:
        local_execution_ledger_sha256, ledger_records_count, ledger_truncated = (
            execution_state_store.calculate_recent_ledger_digest_with_coverage(**ledger_args)
        )
    else:
        local_execution_ledger_sha256, ledger_records_count = execution_state_store.calculate_recent_ledger_digest(**ledger_args)
        ledger_truncated = None
    baseline_id, baseline_target_sha256, runtime_target_sha256 = _continuity_fields(runtime_target)

    def matches(key: str, actual_digest: str) -> bool:
        return expected_digests is not None and expected_digests[key] == actual_digest

    evidence = build_broker_reconciliation_evidence(
        platform_id=platform_id,
        strategy_profile=strategy_profile,
        account_scope_sha256=account_scope_sha256,
        baseline_id=baseline_id,
        baseline_target_sha256=baseline_target_sha256,
        runtime_target_sha256=runtime_target_sha256,
        observed_at=observed_at or datetime.now(timezone.utc),
        broker_connected=True,
        account_identity_match=observations.account_identity_match,
        positions_match=matches("positions_sha256", positions_sha256),
        cash_match=matches("cash_sha256", cash_sha256),
        open_orders_match=matches("open_orders_sha256", open_orders_sha256),
        recent_executions_match=matches("recent_executions_sha256", recent_executions_sha256),
        local_execution_ledger_match=matches(
            "local_execution_ledger_sha256", local_execution_ledger_sha256
        ),
        positions_sha256=positions_sha256,
        cash_sha256=cash_sha256,
        open_orders_sha256=open_orders_sha256,
        recent_executions_sha256=recent_executions_sha256,
        local_execution_ledger_sha256=local_execution_ledger_sha256,
    )
    blockers = evaluate_broker_reconciliation_recovery(
        evidence,
        now=observed_at or datetime.now(timezone.utc),
        expected_platform_id=platform_id,
        expected_strategy_profile=strategy_profile,
        expected_account_scope_sha256=(expected_digests or {}).get("account_scope_sha256"),
        expected_baseline_id=baseline_id,
        expected_runtime_target_sha256=runtime_target_sha256,
        expected_positions_sha256=(expected_digests or {}).get("positions_sha256"),
        expected_cash_sha256=(expected_digests or {}).get("cash_sha256"),
        expected_open_orders_sha256=(expected_digests or {}).get("open_orders_sha256"),
        expected_recent_executions_sha256=(expected_digests or {}).get("recent_executions_sha256"),
        expected_local_execution_ledger_sha256=(expected_digests or {}).get(
            "local_execution_ledger_sha256"
        ),
    )
    return IBKRReconciliationCandidate(
        evidence=evidence,
        recovery_blockers=blockers,
        expected_digests_configured=expected_digests is not None,
        execution_ledger_records_count=ledger_records_count,
        comparison_covered=observations.comparison_covered,
        initial_ledger_truncated=ledger_truncated,
    )


__all__ = [
    "IBKR_RECONCILIATION_EXPECTED_DIGESTS_ENV",
    "IBKRReconciliationCandidate",
    "IBKRReconciliationObservations",
    "IBKRReconciliationReadError",
    "build_reconciliation_candidate",
    "collect_read_only_reconciliation_observations",
    "normalize_account_ids",
]
