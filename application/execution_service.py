"""Order execution helpers for InteractiveBrokersPlatform."""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from application.account_new_risk_gate_support import (
    scale_targets_for_admission,
    build_portfolio_from_account_values,
    build_snapshot_from_portfolio,
    evaluate_account_values_new_risk_admission,
    evaluate_persistent_account_new_risk_admission,
    is_account_new_risk_gate_enabled,
    maybe_publish_attention_for_admission,
    new_risk_buy_prohibited,
    set_cycle_snapshot,
)
from application.paper_execution_admission import evaluate_ibkr_paper_execution_admission
from application.execution_kernel_adapter import consult_t1_live_submit
from application.broker_reconciliation import (
    IBKRReconciliationReadError,
    load_read_only_open_trades,
)
from quant_platform_kit.common.cash_sweep import should_sell_cash_sweep_to_fund_whole_share_buy
from quant_platform_kit.common.models import ExecutionReport, QuoteSnapshot
from quant_platform_kit.common.small_account_compatibility import (
    apply_small_account_cash_compatibility,
    build_small_account_allocation_drift_notes,
    format_small_account_allocation_drift_notes,
    format_small_account_cash_substitution_notes,
)
from quant_platform_kit.common.quantity import (
    floor_to_quantity_step,
    format_quantity,
    normalize_order_quantity,
)


def get_market_prices(
    ib,
    symbols,
    *,
    fetch_quote_snapshots,
    quote_recorder=None,
):
    """Fetch market prices for multiple symbols in one pass."""
    quotes = fetch_quote_snapshots(ib, symbols)
    if quote_recorder is not None:
        for symbol, quote in quotes.items():
            quote_recorder(symbol, quote)
    return {symbol: quote.last_price for symbol, quote in quotes.items()}


def _serialize_quote_snapshot(snapshot, *, symbol: str | None = None) -> dict:
    if isinstance(snapshot, QuoteSnapshot):
        return {
            "symbol": str(snapshot.symbol or symbol or "").strip().upper(),
            "as_of": str(snapshot.as_of or ""),
            "last_price": float(snapshot.last_price or 0.0),
            "bid_price": snapshot.bid_price,
            "ask_price": snapshot.ask_price,
            "currency": str(snapshot.currency or "").strip(),
        }
    return {
        "symbol": str(getattr(snapshot, "symbol", "") or symbol or "").strip().upper(),
        "as_of": str(getattr(snapshot, "as_of", "") or ""),
        "last_price": float(getattr(snapshot, "last_price", 0.0) or 0.0),
        "bid_price": getattr(snapshot, "bid_price", None),
        "ask_price": getattr(snapshot, "ask_price", None),
        "currency": str(getattr(snapshot, "currency", "") or "").strip(),
    }


def check_order_submitted(report, *, translator):
    """Describe the broker report; a pending result is not fill confirmation."""
    order_id = report.broker_order_id
    status = report.status

    if status == "Filled":
        return (
            True,
            translator(
                "order_filled",
                symbol=report.symbol,
                side=report.side,
                qty=format_quantity(report.filled_quantity or report.quantity),
                price=f"{float(report.average_fill_price or 0.0):.2f}",
                order_id=order_id,
            ),
        )
    if status in {"PartiallyFilled", "Partial"}:
        return (
            True,
            translator(
                "order_partial",
                symbol=report.symbol,
                side=report.side,
                executed=format_quantity(report.filled_quantity or 0),
                qty=format_quantity(report.quantity or 0),
                price=f"{float(report.average_fill_price or 0.0):.2f}",
                order_id=order_id,
            ),
        )
    if status in {"PendingSubmit", "ApiPending", "ApiPendingSubmit", "Submitted", "PreSubmitted"}:
        return True, f"⏳ {translator('submitted', order_id=order_id, status=status)}"
    return False, f"❌ {translator('failed', reason=status)}"


_FILLED_ORDER_STATUSES = frozenset({"Filled"})
_PARTIALLY_FILLED_ORDER_STATUSES = frozenset({"PartiallyFilled", "Partial"})
_PENDING_ORDER_STATUSES = frozenset(
    {"PendingSubmit", "ApiPending", "ApiPendingSubmit", "Submitted", "PreSubmitted"}
)
_REJECTED_OR_CANCELLED_ORDER_STATUSES = frozenset(
    {"Rejected", "rejected", "Cancelled", "ApiCancelled"}
)


def _record_order_outcome(
    execution_summary: dict,
    order_payload: dict,
    *,
    status: object,
    option_order: bool = False,
) -> str:
    """Record the broker state without treating a non-terminal state as an execution.

    IBKR can acknowledge an order and reject or cancel it later. A report captured
    while the order is still pending is useful for reconciliation, but it must not
    make the rebalance appear complete.
    """
    normalized_status = str(status or "").strip()
    prefix = "option_orders" if option_order else "orders"
    known_statuses = (
        _FILLED_ORDER_STATUSES | _PARTIALLY_FILLED_ORDER_STATUSES
        | _PENDING_ORDER_STATUSES | _REJECTED_OR_CANCELLED_ORDER_STATUSES
    )
    if normalized_status not in known_statuses or (
        normalized_status not in _REJECTED_OR_CANCELLED_ORDER_STATUSES
        and not order_payload.get("broker_order_id")
    ):
        # Acceptance/remaining obligations are unknown. Retain the original
        # report and identity for reconciliation; never count this as a reject
        # or use its apparent proceeds to authorize another order.
        execution_summary[f"{prefix}_pending"].append(order_payload)
        execution_summary["submission_halted"] = True
        execution_summary["outcome_unknown"] = True
        execution_summary["execution_status"] = "pending_reconciliation"
        execution_summary["no_op_reason"] = "broker_order_outcome_unknown"
        execution_summary["skipped_reasons"].append(
            f"broker_order_outcome_unknown:{order_payload.get('symbol')}:{normalized_status or 'unknown'}"
        )
        return "unknown"
    if normalized_status in _FILLED_ORDER_STATUSES:
        execution_summary[f"{prefix}_filled"].append(order_payload)
        return "filled"
    if normalized_status in _PARTIALLY_FILLED_ORDER_STATUSES:
        execution_summary[f"{prefix}_partially_filled"].append(order_payload)
        return "partially_filled"
    if normalized_status in _PENDING_ORDER_STATUSES:
        execution_summary[f"{prefix}_pending"].append(order_payload)
        return "pending"
    execution_summary[f"{prefix}_skipped"].append(
        {**order_payload, "reason": normalized_status or "submit_failed"}
    )
    failure_prefix = "option_submit_failed" if option_order else "submit_failed"
    execution_summary["skipped_reasons"].append(
        f"{failure_prefix}:{order_payload.get('symbol')}:{normalized_status or 'unknown'}"
    )
    return "failed"


def _normalize_account_ids(account_ids=None) -> tuple[str, ...]:
    if account_ids is None:
        return ()
    if isinstance(account_ids, str):
        candidates = [account_ids]
    else:
        candidates = list(account_ids)
    normalized = []
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text:
            normalized.append(text)
    return tuple(dict.fromkeys(normalized))


def _matches_account(account_id: str | None, selected_account_ids: tuple[str, ...]) -> bool:
    if not selected_account_ids:
        return True
    return str(account_id or "").strip() in selected_account_ids


def _resolve_order_account_id(account_ids=None) -> str | None:
    normalized = _normalize_account_ids(account_ids)
    if len(normalized) > 1:
        raise ValueError(
            "IBKR live order routing requires a single account_id per runtime service; "
            f"got {len(normalized)} account_ids."
        )
    return normalized[0] if normalized else None


MARKET_CURRENCY_CASH_TAG_PRIORITY = (
    "$LEDGER-CashBalance",
    "$LEDGER-TotalCashBalance",
    "CashBalance",
    "TotalCashBalance",
    "SettledCash",
)


def _coerce_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _cash_value_for_currency(account_values, *, currency: str, account_ids=None) -> float | None:
    selected_account_ids = _normalize_account_ids(account_ids)
    market_currency = str(currency or "USD").strip().upper()
    values_by_account_currency: dict[tuple[str | None, str], dict[str, float]] = {}
    for account_value in account_values or ():
        account_id = str(getattr(account_value, "account", "") or "").strip() or None
        if not _matches_account(account_id, selected_account_ids):
            continue
        value_currency = str(getattr(account_value, "currency", "") or "").strip().upper()
        if not value_currency:
            continue
        value = _coerce_float(getattr(account_value, "value", None))
        if value is None:
            continue
        tag = str(getattr(account_value, "tag", "") or "").strip()
        values_by_account_currency.setdefault((account_id, value_currency), {})[tag] = value

    total = 0.0
    matched = False
    for (_account_id, value_currency), tag_values in values_by_account_currency.items():
        if value_currency != market_currency:
            continue
        for tag in MARKET_CURRENCY_CASH_TAG_PRIORITY:
            if tag in tag_values:
                total += float(tag_values[tag])
                matched = True
                break
    return total if matched else None


SETTLED_CASH_MAX_AGE_SECONDS = 30.0
SETTLED_CASH_REFRESH_TIMEOUT_SECONDS = 10.0


class _SettledCashDeadlineIB:
    """Keep settlement restrictions through preparation and the final write."""

    def __init__(self, ib, *, refresh_started, account_id, currency,
                 buying_power, reserved, quantity_step, minimum_order_notional):
        self._ib = ib
        self._refresh_started = refresh_started
        self._account_id = account_id
        self._currency = currency.upper()
        self.remaining_buying_power = buying_power
        self._reserved = reserved
        self._quantity_step = quantity_step
        self._minimum_order_notional = minimum_order_notional
        self._invalid = False
        self._contract = None
        self.prepared_quantity = None

    def __enter__(self):
        self._ib.accountValueEvent.connect(self._observe)
        return self

    def __exit__(self, *_exc):
        self._ib.accountValueEvent.disconnect(self._observe)

    def _observe(self, row):
        if (
            str(getattr(row, "account", "") or "").strip() != self._account_id
            or str(getattr(row, "currency", "") or "").strip().upper() != self._currency
            or str(getattr(row, "tag", "") or "").strip() != "SettledCash"
        ):
            return
        # Copy the restriction now: SDK/cache objects can be replaced or mutated.
        value = _coerce_float(getattr(row, "value", None))
        now = datetime.now(timezone.utc)
        invalid = value is None or not math.isfinite(value) or value <= 0
        invalid = invalid or bool(getattr(row, "stale", False))
        for field in ("as_of", "observed_at"):
            stamp = getattr(row, field, None)
            if stamp is not None and (
                not isinstance(stamp, datetime) or stamp.tzinfo is None
                or not 0 <= (now - stamp).total_seconds() <= SETTLED_CASH_MAX_AGE_SECONDS
            ):
                invalid = True
        self._invalid = self._invalid or invalid
        if not invalid:
            # Increases never replenish this cycle's shared remaining budget.
            self.remaining_buying_power = min(
                self.remaining_buying_power, _investable_buying_power(value, self._reserved),
            )

    def prepare_intent(self, intent, *, exchange, currency):
        """Finish blocking qualification, then bind the resized journal intent."""
        from ib_insync import Stock

        if (not self._account_id or currency.upper() != self._currency
                or intent.account_id != self._account_id):
            raise RuntimeError("settled_cash_scope_mismatch")
        contract = Stock(intent.symbol, exchange, currency)
        self._ib.qualifyContracts(contract)
        self._contract = contract
        quantity = min(intent.quantity, _floor_order_quantity(
            self.remaining_buying_power * 0.95 / intent.limit_price,
            quantity_step=self._quantity_step,
        ))
        if quantity <= 0 or quantity * intent.limit_price < self._minimum_order_notional:
            self._invalid = True
        # No grant exists yet: an invalid update can reject admission locally.
        if self._invalid:
            return None
        self.prepared_quantity = quantity
        return replace(intent, quantity=quantity)

    def qualifyContracts(self, contract):
        # The pinned executor qualifies once more. Reuse the prepared contract
        # without pumping another request after the final intent was reserved.
        if self._contract is None or (
            contract.symbol != self._contract.symbol
            or contract.secType != self._contract.secType
            or contract.currency != self._contract.currency
            or contract.exchange != self._contract.exchange
        ):
            raise RuntimeError("settled_cash_unprepared_contract")
        contract.update(self._contract)
        return [contract]

    def __getattr__(self, name):
        return getattr(self._ib, name)

    def placeOrder(self, contract, order):
        if (
            self._invalid or order.account != self._account_id
            or order.totalQuantity != self.prepared_quantity
            or contract.currency.upper() != self._currency
            or order.action != "BUY" or order.orderType != "LMT"
            or not math.isfinite(order.totalQuantity) or order.totalQuantity <= 0
            or not math.isfinite(order.lmtPrice) or order.lmtPrice <= 0
            or order.totalQuantity * order.lmtPrice > self.remaining_buying_power
        ):
            raise RuntimeError("settled_cash_changed: refusing IBKR placeOrder")
        if not 0 <= time.monotonic() - self._refresh_started <= SETTLED_CASH_MAX_AGE_SECONDS:
            # Raise inside session.dispatch: retain its consumed grant and
            # conservative UNKNOWN outcome instead of fabricating a rejection.
            raise RuntimeError("settled_cash_stale: refusing IBKR placeOrder")
        return self._ib.placeOrder(contract, order)


def _settled_cash_for_currency(ib, *, currency, account_ids=None) -> float | None:
    """Require settlement updates received during a completed, bounded refresh.

    accountValues() is only a cache. Its contents, and a refresh completion
    without a matching SettledCash update, cannot establish spending authority.
    """
    selected = _normalize_account_ids(account_ids)
    event = getattr(ib, "accountValueEvent", None)
    refresh = getattr(ib, "reqAccountUpdates", None)
    if (
        not callable(refresh) or not callable(getattr(event, "connect", None))
        or not callable(getattr(event, "disconnect", None))
    ):
        return None
    original_timeout = getattr(ib, "RequestTimeout", 0)
    original_raise_errors = getattr(ib, "RaiseRequestErrors", False)
    rows = []

    def observe(row):
        rows.append((row, time.monotonic()))

    try:
        # Preview callers may omit routing. Cache rows can identify the one
        # account to refresh, but their amounts never provide the buy budget.
        if not selected:
            selected = tuple(dict.fromkeys(
                str(getattr(row, "account", "") or "").strip()
                for row in ib.accountValues() or ()
                if str(getattr(row, "account", "") or "").strip()
            ))
        if len(selected) != 1:
            return None
        timeout = _coerce_float(original_timeout)
        ib.RequestTimeout = (
            min(timeout, SETTLED_CASH_REFRESH_TIMEOUT_SECONDS)
            if timeout is not None and math.isfinite(timeout) and timeout > 0
            else SETTLED_CASH_REFRESH_TIMEOUT_SECONDS
        )
        ib.RaiseRequestErrors = True
        event.connect(observe)
        started = time.monotonic()
        # Pinned ib_insync blocks until accountDownloadEnd or raises on timeout.
        refresh(selected[0])
        completed = time.monotonic()
        if not 0 <= completed - started <= SETTLED_CASH_MAX_AGE_SECONDS:
            return None
        now = datetime.now(timezone.utc)
        amounts = {}
        for row, received in rows:
            if str(getattr(row, "currency", "")).strip().upper() != currency.upper():
                continue
            account = str(getattr(row, "account", "") or "").strip() or None
            if not _matches_account(account, selected):
                continue
            if str(getattr(row, "tag", "")).strip() != "SettledCash":
                continue
            if account is None or (selected and account not in selected):
                return None
            if not 0 <= completed - received <= SETTLED_CASH_MAX_AGE_SECONDS:
                return None
            if getattr(row, "stale", False):
                return None
            for field in ("as_of", "observed_at"):
                stamp = getattr(row, field, None)
                if stamp is not None and (
                    not isinstance(stamp, datetime) or stamp.tzinfo is None
                    or not 0 <= (now - stamp).total_seconds() <= SETTLED_CASH_MAX_AGE_SECONDS
                ):
                    return None
            value = _coerce_float(getattr(row, "value", None))
            if value is None or not math.isfinite(value):
                return None
            if account in amounts and amounts[account] != value:
                return None
            amounts[account] = value
        if not amounts or (selected and set(amounts) != set(selected)):
            return None
        total = sum(amounts.values())
        return total if math.isfinite(total) else None
    except Exception:
        # Missing API, incomplete refresh and broker read errors stay unknown.
        return None
    finally:
        event.disconnect(observe)
        ib.RequestTimeout = original_timeout
        ib.RaiseRequestErrors = original_raise_errors


def _available_funds_for_currency(account_values, *, currency: str, account_ids=None) -> float | None:
    selected_account_ids = _normalize_account_ids(account_ids)
    market_currency = str(currency or "USD").strip().upper()
    total = 0.0
    matched = False
    for account_value in account_values or ():
        account_id = str(getattr(account_value, "account", "") or "").strip() or None
        if not _matches_account(account_id, selected_account_ids):
            continue
        value_currency = str(getattr(account_value, "currency", "") or "").strip().upper()
        if value_currency != market_currency:
            continue
        if str(getattr(account_value, "tag", "") or "").strip() == "AvailableFunds":
            value = _coerce_float(getattr(account_value, "value", None))
            if value is not None:
                total += float(value)
                matched = True
    return total if matched else None


def get_available_buying_power(
    ib,
    fallback_buying_power,
    *,
    account_ids=None,
    currency="USD",
    cash_only_execution=True,
):
    selected_account_ids = _normalize_account_ids(account_ids)
    account_values = list(ib.accountValues() or ())
    if not cash_only_execution:
        available_funds = _available_funds_for_currency(
            account_values,
            currency=currency,
            account_ids=selected_account_ids,
        )
        if available_funds is not None:
            return max(0.0, float(available_funds))
        return max(0.0, float(fallback_buying_power or 0.0))
    currency_cash = _cash_value_for_currency(
        account_values,
        currency=currency,
        account_ids=selected_account_ids,
    )
    if currency_cash is not None:
        return max(0.0, float(currency_cash))
    return max(0.0, float(fallback_buying_power or 0.0))


class _PendingOrdersUnavailableError(RuntimeError):
    """Open-order evidence is incomplete; it cannot establish an empty book."""


@dataclass(frozen=True)
class _PendingOrderObservation:
    symbols: tuple[str, ...]
    cash_risk_unknown: bool = False


def _iter_open_orders(ib) -> list[Any]:
    try:
        return list(load_read_only_open_trades(ib))
    except IBKRReconciliationReadError:
        raise _PendingOrdersUnavailableError("all-client open-order snapshot unavailable") from None


def _extract_open_order_contract(order_like: Any) -> Any:
    contract = getattr(order_like, "contract", None)
    if contract is None:
        order = getattr(order_like, "order", None)
        contract = getattr(order, "contract", None)
    return contract


def _extract_open_order_symbol(order_like: Any) -> str | None:
    contract = _extract_open_order_contract(order_like)
    symbol = getattr(contract, "symbol", None)
    if symbol is None:
        symbol = getattr(order_like, "symbol", None)
    symbol_text = str(symbol or "").strip().upper()
    return symbol_text or None


def _extract_open_order_status(order_like: Any) -> str:
    order_status = getattr(order_like, "orderStatus", None)
    status = getattr(order_status, "status", None)
    if status is None:
        status = getattr(order_like, "status", None)
    return str(status or "").strip()


def _extract_open_order_account(order_like: Any) -> str | None:
    order = getattr(order_like, "order", None)
    for candidate in (
        getattr(order, "account", None),
        getattr(order_like, "account", None),
    ):
        text = str(candidate or "").strip()
        if text:
            return text
    return None


def _observe_pending_orders(ib, symbols: set[str], *, account_ids=None) -> _PendingOrderObservation:
    selected_account_ids = _normalize_account_ids(account_ids)
    pending = []
    cash_risk_unknown = False
    for order_like in _iter_open_orders(ib):
        status = _extract_open_order_status(order_like)
        if status in {"Cancelled", "ApiCancelled", "Inactive", "Filled"}:
            continue
        account_id = _extract_open_order_account(order_like)
        symbol = _extract_open_order_symbol(order_like)
        if selected_account_ids and account_id is None:
            contract = _extract_open_order_contract(order_like)
            if (
                symbol
                and symbol not in symbols
                and str(getattr(contract, "secType", "") or "").strip().upper() == "STK"
            ):
                # A known different stock cannot consume a managed stock's
                # sellable quantity, but unknown ownership still taints cash.
                # Keep reading: a later row can affect the managed securities.
                cash_risk_unknown = True
                continue
            raise _PendingOrdersUnavailableError("open-order account unavailable")
        if not _matches_account(account_id, selected_account_ids):
            continue
        if not symbol:
            raise _PendingOrdersUnavailableError("open-order symbol unavailable")
        if symbol in symbols:
            pending.append(symbol)
        else:
            # Another security in the same account can still consume cash or
            # collateral. This snapshot does not establish its remaining cost,
            # fees or whether a cash tag already nets that obligation. Reuse
            # the BUY gate instead of guessing an amount and deducting twice.
            cash_risk_unknown = True
    return _PendingOrderObservation(tuple(sorted(dict.fromkeys(pending))), cash_risk_unknown)


def _iter_fills(ib) -> list[Any]:
    fills = getattr(ib, "fills", None)
    if callable(fills):
        return list(fills() or [])
    return []


def _extract_fill_symbol(fill_like: Any) -> str | None:
    contract = getattr(fill_like, "contract", None)
    symbol = getattr(contract, "symbol", None)
    symbol_text = str(symbol or "").strip().upper()
    return symbol_text or None


def _extract_fill_account(fill_like: Any) -> str | None:
    execution = getattr(fill_like, "execution", None)
    for candidate in (
        getattr(execution, "acctNumber", None),
        getattr(execution, "account", None),
        getattr(fill_like, "account", None),
    ):
        text = str(candidate or "").strip()
        if text:
            return text
    return None


def _normalize_date_like(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert(None)
    else:
        ts = ts.tz_localize(None)
    return ts.normalize().date().isoformat()


def _extract_fill_date(fill_like: Any) -> str | None:
    execution = getattr(fill_like, "execution", None)
    for candidate in (
        getattr(execution, "time", None),
        getattr(fill_like, "time", None),
    ):
        normalized = _normalize_date_like(candidate)
        if normalized is not None:
            return normalized
    return None


def _collect_same_day_filled_symbols(
    ib,
    symbols: set[str],
    trade_date: str | None,
    *,
    account_ids=None,
) -> tuple[str, ...]:
    if not trade_date:
        return ()
    selected_account_ids = _normalize_account_ids(account_ids)
    matched = []
    for fill_like in _iter_fills(ib):
        if not _matches_account(_extract_fill_account(fill_like), selected_account_ids):
            continue
        symbol = _extract_fill_symbol(fill_like)
        if not symbol or symbol not in symbols:
            continue
        fill_date = _extract_fill_date(fill_like)
        if fill_date == trade_date:
            matched.append(symbol)
    return tuple(sorted(dict.fromkeys(matched)))


def _round_weight(value: float) -> float:
    return round(float(value or 0.0), 8)


def _build_target_hash(target_weights: dict[str, float]) -> str:
    payload = [[str(symbol), _round_weight(weight)] for symbol, weight in sorted(target_weights.items())]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")).hexdigest()


def _sanitize_token(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return "none"
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in text)
    return safe or "none"


def _display_text(value: Any, *, fallback: str) -> str:
    text = str(value).strip() if value is not None else ""
    return text or fallback


def _resolve_weight_allocation(signal_metadata: dict[str, Any] | None) -> dict[str, Any]:
    metadata = dict(signal_metadata or {})
    allocation = dict(metadata.get("allocation") or {})
    if not allocation:
        raise ValueError("IBKR execution requires signal_metadata.allocation")
    if allocation.get("target_mode") != "weight":
        raise ValueError("IBKR execution requires allocation.target_mode=weight")
    return {
        "strategy_symbols": tuple(str(symbol).strip().upper() for symbol in allocation.get("strategy_symbols", ())),
        "risk_symbols": tuple(str(symbol).strip().upper() for symbol in allocation.get("risk_symbols", ())),
        "income_symbols": tuple(str(symbol).strip().upper() for symbol in allocation.get("income_symbols", ())),
        "safe_haven_symbols": tuple(
            str(symbol).strip().upper() for symbol in allocation.get("safe_haven_symbols", ())
        ),
        "targets": {
            str(symbol).strip().upper(): float(weight)
            for symbol, weight in dict(allocation.get("targets") or {}).items()
        },
    }


def _require_live_risk_authority(
    signal_metadata: dict[str, Any],
    allocation: dict[str, Any],
    *,
    strategy_profile: str | None,
    account_ids,
) -> dict[str, float]:
    """Bind the final order plan to the strategy decision approved by RiskEngine."""
    authority = signal_metadata.get("risk_authority")
    flags = signal_metadata.get("risk_flags") or ()
    if (
        signal_metadata.get("risk_gate") != "APPROVE"
        or "risk_gate:passed" not in flags
        or not isinstance(authority, dict)
        or authority.get("strategy_profile") != strategy_profile
        or _normalize_account_ids(authority.get("account_ids")) != _normalize_account_ids(account_ids)
        or authority.get("trade_date") != signal_metadata.get("trade_date")
        or authority.get("signal_date") != signal_metadata.get("signal_date")
        or authority.get("effective_date") != signal_metadata.get("effective_date")
        or authority.get("snapshot_as_of") != signal_metadata.get("snapshot_as_of")
        or not isinstance(authority.get("targets"), dict)
        or not isinstance(authority.get("max_target_values"), dict)
    ):
        raise RuntimeError("IBKR live execution requires a valid RiskEngine approval")
    targets = allocation["targets"]
    approved_targets = authority["targets"]
    caps = authority["max_target_values"]
    equity = authority.get("portfolio_equity")
    if type(equity) not in (int, float) or not math.isfinite(equity) or equity <= 0.0:
        raise RuntimeError("IBKR live RiskEngine portfolio equity is invalid")
    if set(targets) != set(approved_targets) or set(targets) != set(caps):
        raise RuntimeError("IBKR live allocation differs from RiskEngine approval")
    for symbol, target in targets.items():
        approved = approved_targets[symbol]
        cap = caps[symbol]
        if (
            type(approved) not in (int, float)
            or not math.isfinite(approved)
            or not math.isclose(target, approved, rel_tol=0.0, abs_tol=1e-9)
            or type(cap) not in (int, float)
            or not math.isfinite(cap)
            or cap < 0.0
            or not math.isclose(cap, approved * equity, rel_tol=1e-8, abs_tol=0.01)
        ):
            raise RuntimeError("IBKR live order limit is invalid")
    return {symbol: float(caps[symbol]) for symbol in targets}


def _require_tqqq_effective_date(signal_metadata: dict[str, Any], *, strategy_profile: str | None) -> None:
    if strategy_profile != "tqqq_growth_income":
        return
    raw_effective_date = signal_metadata.get("effective_date")
    try:
        effective_date = date.fromisoformat(raw_effective_date)
    except (TypeError, ValueError):
        raise RuntimeError("IBKR TQQQ execution requires a valid effective date") from None
    if effective_date != datetime.now(ZoneInfo("America/New_York")).date():
        raise RuntimeError("IBKR TQQQ signal is not effective for this trading date")


def _normalize_option_order_intents(signal_metadata: dict[str, Any] | None) -> tuple[dict[str, Any], ...]:
    metadata = dict(signal_metadata or {})
    raw_payload = metadata.get("option_order_intents")
    if isinstance(raw_payload, dict):
        raw_intents = raw_payload.get("intents")
    else:
        raw_intents = raw_payload
    intents = []
    for intent in raw_intents or ():
        if isinstance(intent, dict):
            intents.append(dict(intent))
    return tuple(intents)


def _option_intent_underliers(option_intents: tuple[dict[str, Any], ...]) -> tuple[str, ...]:
    underliers = []
    for intent in option_intents:
        underlier = str(intent.get("underlier") or intent.get("symbol") or "").strip().upper()
        if underlier:
            underliers.append(underlier)
    return tuple(sorted(dict.fromkeys(underliers)))


def _is_executable_option_intent(intent: dict[str, Any]) -> bool:
    intent_type = str(intent.get("intent_type") or "").strip()
    action = str(intent.get("action") or "").strip().lower()
    if (
        str(intent.get("asset_class") or "").strip().lower() == "option"
        and intent_type == "single_leg_option"
        and action in {"buy_to_open", "sell_to_close"}
    ):
        return True
    return (
        str(intent.get("asset_class") or "").strip().lower() == "option"
        and intent_type == "multi_leg_option"
        and action == "sell_to_open_put_credit_spread"
    )


def _has_executable_option_plan(option_intents: tuple[dict[str, Any], ...]) -> bool:
    return any(_is_executable_option_intent(intent) for intent in option_intents)


def _format_option_intent_symbol(intent: dict[str, Any]) -> str:
    underlier = str(intent.get("underlier") or intent.get("symbol") or "").strip().upper()
    if str(intent.get("intent_type") or "").strip() == "multi_leg_option":
        expiration = str(intent.get("expiration") or "").strip()
        return f"{underlier} {expiration} PCS".strip()
    right = str(intent.get("right") or "").strip().upper()
    expiration = str(intent.get("expiration") or "").strip()
    strike = intent.get("strike")
    if underlier and right and expiration and strike not in {None, ""}:
        return f"{underlier} {expiration} {float(strike):g}{right}"
    return underlier or "<option>"


def _record_unsupported_option_intents(
    execution_summary: dict[str, Any],
    option_intents: tuple[dict[str, Any], ...],
) -> None:
    for intent in option_intents:
        if _is_executable_option_intent(intent):
            continue
        symbol = _format_option_intent_symbol(intent)
        reason = "unsupported_option_intent_type"
        execution_summary["option_orders_skipped"].append(
            {
                "symbol": symbol,
                "action": str(intent.get("action") or ""),
                "intent_type": str(intent.get("intent_type") or ""),
                "reason": reason,
            }
        )
        execution_summary["skipped_reasons"].append(f"{reason}:{symbol}")


def _build_single_leg_option_order_intent(
    order_intent_cls,
    intent: dict[str, Any],
    *,
    account_id: str | None,
):
    action = str(intent.get("action") or "").strip().lower()
    side = "buy" if action.startswith("buy") else "sell"
    return order_intent_cls(
        symbol=str(intent.get("underlier") or intent.get("symbol") or "").strip().upper(),
        side=side,
        quantity=float(intent.get("quantity") or 0.0),
        order_type=str(intent.get("order_type") or "limit").strip().lower(),
        limit_price=(
            float(intent["limit_price"])
            if intent.get("limit_price") not in {None, ""}
            else None
        ),
        time_in_force=str(intent.get("time_in_force") or "DAY").strip().upper(),
        account_id=account_id,
        metadata={
            **intent,
            "asset_class": "option",
            "intent_type": "single_leg_option",
            "security_type": "OPT",
        },
    )


def _build_multi_leg_option_order_intent(
    order_intent_cls,
    intent: dict[str, Any],
    *,
    account_id: str | None,
):
    return order_intent_cls(
        symbol=str(intent.get("underlier") or intent.get("symbol") or "").strip().upper(),
        side="sell",
        quantity=float(intent.get("quantity") or 0.0),
        order_type=str(intent.get("order_type") or "limit").strip().lower(),
        limit_price=(
            float(intent["limit_price"])
            if intent.get("limit_price") not in {None, ""}
            else None
        ),
        time_in_force=str(intent.get("time_in_force") or "DAY").strip().upper(),
        account_id=account_id,
        metadata={
            **intent,
            "asset_class": "option",
            "intent_type": "multi_leg_option",
            "security_type": "BAG",
        },
    )


def _execute_option_order_intents(
    ib,
    option_intents: tuple[dict[str, Any], ...],
    *,
    submit_order_intent,
    order_intent_cls,
    translator,
    execution_summary: dict[str, Any],
    trade_logs: list[str],
    dry_run_only: bool,
    order_account_id: str | None,
    buying_power: float,
) -> float:
    if not option_intents:
        return buying_power
    _record_unsupported_option_intents(execution_summary, option_intents)
    for intent in option_intents:
        if not _is_executable_option_intent(intent):
            continue
        symbol = _format_option_intent_symbol(intent)
        quantity = float(intent.get("quantity") or 0.0)
        if quantity <= 0.0:
            execution_summary["option_orders_skipped"].append(
                {"symbol": symbol, "action": intent.get("action"), "reason": "quantity_zero"}
            )
            continue
        limit_price = float(intent.get("limit_price") or 0.0)
        multiplier = float(intent.get("contract_multiplier") or 100.0)
        action = str(intent.get("action") or "").strip().lower()
        intent_type = str(intent.get("intent_type") or "").strip()
        estimated_notional = float(intent.get("max_notional_usd") or (quantity * limit_price * multiplier))
        max_loss = float(intent.get("max_loss_usd") or 0.0)
        if action.startswith("buy") and estimated_notional > max(0.0, buying_power * 0.95):
            execution_summary["option_orders_skipped"].append(
                {
                    "symbol": symbol,
                    "action": action,
                    "quantity": quantity,
                    "limit_price": limit_price,
                    "reason": "insufficient_buying_power",
                }
            )
            execution_summary["skipped_reasons"].append(f"option_insufficient_buying_power:{symbol}")
            continue
        if intent_type == "multi_leg_option" and max_loss > max(0.0, buying_power * 0.95):
            execution_summary["option_orders_skipped"].append(
                {
                    "symbol": symbol,
                    "action": action,
                    "quantity": quantity,
                    "limit_price": limit_price,
                    "reason": "insufficient_buying_power_for_defined_risk",
                }
            )
            execution_summary["skipped_reasons"].append(f"option_insufficient_buying_power:{symbol}")
            continue
        payload = {
            "symbol": symbol,
            "underlier": str(intent.get("underlier") or "").strip().upper(),
            "action": action,
            "quantity": quantity,
            "limit_price": limit_price,
            "status": "dry_run" if dry_run_only else "pending",
        }
        if dry_run_only:
            execution_summary["option_orders_submitted"].append(payload)
            trade_logs.append(f"DRY_RUN option {action} {symbol} {format_quantity(quantity)} @{limit_price:.2f}")
            if action.startswith("buy"):
                buying_power -= estimated_notional
            elif intent_type == "multi_leg_option":
                buying_power -= max_loss
            continue
        if intent_type == "multi_leg_option":
            order_intent = _build_multi_leg_option_order_intent(
                order_intent_cls,
                intent,
                account_id=order_account_id,
            )
        else:
            order_intent = _build_single_leg_option_order_intent(
                order_intent_cls,
                intent,
                account_id=order_account_id,
            )
        report = submit_order_intent(ib, order_intent)
        _, status_msg = check_order_submitted(report, translator=translator)
        status, broker_order_id, _, _ = _extract_execution_report_fields(report)
        order_payload = {
            **payload,
            "status": status,
            "broker_order_id": broker_order_id,
        }
        outcome = _record_order_outcome(
            execution_summary,
            order_payload,
            status=status,
            option_order=True,
        )
        trade_logs.append(f"option {action} {symbol} {format_quantity(quantity)} @{limit_price:.2f} {status_msg}")
        if outcome != "failed" and action.startswith("buy"):
            buying_power -= estimated_notional
        elif outcome != "failed" and intent_type == "multi_leg_option":
            buying_power -= max_loss
    return buying_power


def _apply_snapshot_price_fallbacks(
    prices: dict[str, float],
    symbols,
    *,
    dry_run_only: bool,
    snapshot_price_fallbacks: dict[str, float] | None,
) -> tuple[dict[str, float], tuple[str, ...]]:
    del dry_run_only
    if not snapshot_price_fallbacks:
        return dict(prices), ()
    resolved = dict(prices)
    fallback_symbols: list[str] = []
    for symbol in symbols:
        normalized = str(symbol).strip().upper()
        try:
            existing_price = float(resolved.get(normalized, 0.0) or 0.0)
        except (TypeError, ValueError):
            existing_price = 0.0
        if existing_price > 0.0:
            resolved[normalized] = existing_price
            continue
        fallback_price = snapshot_price_fallbacks.get(normalized)
        if fallback_price and float(fallback_price) > 0:
            resolved[normalized] = float(fallback_price)
            fallback_symbols.append(normalized)
    return resolved, tuple(fallback_symbols)


def _normalize_price_fallbacks(signal_metadata: dict[str, Any] | None) -> dict[str, float]:
    metadata = dict(signal_metadata or {})
    raw_fallbacks = {}
    for key in ("dry_run_price_fallbacks", "price_fallbacks"):
        candidate = metadata.get(key)
        if isinstance(candidate, dict):
            raw_fallbacks.update(candidate)
    normalized: dict[str, float] = {}
    for symbol, price in raw_fallbacks.items():
        normalized_symbol = str(symbol).strip().upper()
        if not normalized_symbol:
            continue
        try:
            numeric_price = float(price)
        except (TypeError, ValueError):
            continue
        if numeric_price > 0.0:
            normalized[normalized_symbol] = numeric_price
    return normalized


def _format_symbol_preview(symbols: tuple[str, ...], *, limit: int = 3) -> str:
    if not symbols:
        return ""
    shown = [str(symbol).strip().upper() for symbol in symbols[:limit]]
    remaining = len(symbols) - len(shown)
    if remaining > 0:
        shown.append(f"+{remaining}")
    return ",".join(shown)


def _resolve_execution_mode(*, dry_run_only: bool, execution_mode: str | None = None) -> str:
    if dry_run_only:
        return "dry_run"
    normalized = str(execution_mode or "paper").strip().lower().replace("-", "_")
    aliases = {
        "dryrun": "dry_run",
        "dry_run_only": "dry_run",
        "paper_trading": "paper",
        "live_trading": "live",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized == "dry_run":
        raise ValueError("execution_mode=dry_run requires dry_run_only=True")
    if normalized not in {"paper", "live"}:
        raise ValueError("execution_mode must be paper or live")
    return normalized


def _resolve_execution_lock_path(
    *,
    strategy_profile: str | None,
    account_group: str | None,
    service_name: str | None,
    trade_date: str | None,
    snapshot_date: str | None,
    execution_mode: str,
    execution_lock_dir: str | Path | None,
) -> Path:
    lock_dir = Path(execution_lock_dir) if execution_lock_dir else Path(tempfile.gettempdir()) / "ibkr_execution_locks"
    scope = "__".join(
        [
            _sanitize_token(account_group or "default"),
            _sanitize_token(service_name or "service"),
            _sanitize_token(strategy_profile or "unknown"),
            _sanitize_token(execution_mode),
            _sanitize_token(trade_date),
            _sanitize_token(snapshot_date or "no_snapshot"),
        ]
    )
    return lock_dir / f"{scope}.json"


def _read_execution_lock(lock_path: Path) -> dict[str, Any] | None:
    if not lock_path.exists():
        return None
    return json.loads(lock_path.read_text(encoding="utf-8"))


def _try_create_execution_lock(lock_path: Path, payload: dict[str, Any]) -> bool:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with lock_path.open("x", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        return True
    except FileExistsError:
        return False


def _build_execution_lock_payload(
    *,
    strategy_profile: str | None,
    account_group: str | None,
    service_name: str | None,
    account_ids: tuple[str, ...] | list[str] | None,
    trade_date: str | None,
    snapshot_date: str | None,
    target_hash: str,
    execution_mode: str,
) -> dict[str, Any]:
    return {
        "strategy_profile": strategy_profile,
        "account_group": account_group,
        "service_name": service_name,
        "account_ids": list(account_ids or ()),
        "trade_date": trade_date,
        "snapshot_date": snapshot_date,
        "mode": execution_mode,
        "target_hash": target_hash,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z",
    }


def _format_target_lines(
    target_weights: dict[str, float],
    current_mv: dict[str, float],
    equity: float,
    *,
    translator,
) -> list[str]:
    current_weight = {
        symbol: (current_mv.get(symbol, 0.0) / equity if equity > 0 else 0.0)
        for symbol in set(target_weights) | set(current_mv)
    }
    target_lines = []
    for symbol, target_weight in sorted(target_weights.items(), key=lambda item: (-item[1], item[0])):
        delta = target_weight - current_weight.get(symbol, 0.0)
        target_lines.append(
            translator(
                "target_diff",
                symbol=symbol,
                current=f"{current_weight.get(symbol, 0.0):.1%}",
                target=f"{target_weight:.1%}",
                delta=f"{delta:.1%}",
            )
        )
    return target_lines


def _build_target_diff_rows(
    target_weights: dict[str, float],
    current_mv: dict[str, float],
    equity: float,
) -> list[dict[str, float | str]]:
    current_weight = {
        symbol: (current_mv.get(symbol, 0.0) / equity if equity > 0 else 0.0)
        for symbol in set(target_weights) | set(current_mv)
    }
    rows = []
    for symbol, target_weight in sorted(target_weights.items(), key=lambda item: (-item[1], item[0])):
        current_value = current_weight.get(symbol, 0.0)
        rows.append(
            {
                "symbol": symbol,
                "current_weight": current_value,
                "target_weight": float(target_weight),
                "delta_weight": float(target_weight - current_value),
            }
        )
    return rows


def _floor_order_quantity(quantity, *, quantity_step):
    return normalize_order_quantity(floor_to_quantity_step(quantity, quantity_step))


def _investable_buying_power(buying_power: float, reserved_cash: float) -> float:
    return max(0.0, float(buying_power or 0.0) - max(0.0, float(reserved_cash or 0.0)))


def _estimated_buy_order_cost(
    *,
    buy_symbols,
    current_mv,
    target_mv,
    threshold,
    investable_buying_power,
    prices,
    limit_buy_premium,
    limit_buy_premium_by_symbol,
    quantity_step,
    minimum_order_notional,
) -> float:
    total_cost = 0.0
    for symbol in buy_symbols:
        current = float(current_mv.get(symbol, 0.0) or 0.0)
        target = float(target_mv.get(symbol, 0.0) or 0.0)
        if current >= target - threshold:
            continue
        buy_value = min(target - current, investable_buying_power)
        price = prices.get(symbol)
        if not price or buy_value < minimum_order_notional:
            continue
        limit_price = _limit_buy_price(symbol, price, limit_buy_premium, limit_buy_premium_by_symbol)
        qty = _floor_order_quantity(
            buy_value / limit_price,
            quantity_step=quantity_step,
        )
        if qty <= 0:
            continue
        total_cost += float(qty) * float(limit_price)
    return total_cost


def _rotation_guard_should_block_buys(
    *,
    pending_sell_release_symbols,
    buy_needed_symbols,
    current_mv,
    target_mv,
    threshold,
    investable_buying_power,
    prices,
    limit_buy_premium,
    limit_buy_premium_by_symbol,
    quantity_step,
    minimum_order_notional,
) -> bool:
    if not pending_sell_release_symbols or not buy_needed_symbols:
        return False
    estimated_cost = _estimated_buy_order_cost(
        buy_symbols=buy_needed_symbols,
        current_mv=current_mv,
        target_mv=target_mv,
        threshold=threshold,
        investable_buying_power=investable_buying_power,
        prices=prices,
        limit_buy_premium=limit_buy_premium,
        limit_buy_premium_by_symbol=limit_buy_premium_by_symbol,
        quantity_step=quantity_step,
        minimum_order_notional=minimum_order_notional,
    )
    return estimated_cost > investable_buying_power


def _sell_order_quantity(
    *,
    current_value,
    target_value,
    price,
    position_quantity,
    quantity_step,
):
    held_quantity = max(0.0, float(position_quantity or 0.0))
    if held_quantity <= 0.0:
        return 0

    target = max(0.0, float(target_value or 0.0))
    if target <= 0.0:
        return _floor_order_quantity(held_quantity, quantity_step=quantity_step)

    sell_value = max(0.0, float(current_value or 0.0) - target)
    if sell_value <= 0.0 or float(price or 0.0) <= 0.0:
        return 0
    return _floor_order_quantity(
        min(sell_value / float(price), held_quantity),
        quantity_step=quantity_step,
    )


def _limit_buy_premium_for_symbol(symbol, default_premium, premium_by_symbol=None) -> float:
    normalized_symbol = str(symbol or "").strip().upper()
    try:
        fallback = float(default_premium)
    except (TypeError, ValueError):
        fallback = 1.005
    if not isinstance(premium_by_symbol, dict):
        return fallback
    raw_value = premium_by_symbol.get(normalized_symbol)
    if raw_value is None:
        return fallback
    try:
        premium = float(raw_value)
    except (TypeError, ValueError):
        return fallback
    return premium if premium > 0.0 else fallback


def _limit_buy_price(symbol, price, default_premium, premium_by_symbol=None) -> float:
    return round(
        float(price) * _limit_buy_premium_for_symbol(symbol, default_premium, premium_by_symbol),
        2,
    )


DEFAULT_SAFE_HAVEN_CASH_SUBSTITUTE_THRESHOLD_USD = 1000.0
SMALL_ACCOUNT_SAFE_HAVEN_CASH_SUBSTITUTE_LIMIT_USD = 2000.0


def _positive_target_total(targets: dict[str, Any]) -> float:
    total = 0.0
    for value in dict(targets or {}).values():
        try:
            total += max(0.0, float(value or 0.0))
        except (TypeError, ValueError):
            continue
    return total


def _apply_safe_haven_cash_substitution_to_weights(
    target_weights: dict[str, float],
    *,
    safe_haven_symbols: tuple[str, ...],
    investable: float,
    threshold_usd: float,
) -> tuple[dict[str, float], tuple[str, ...]]:
    threshold = max(0.0, float(threshold_usd or 0.0))
    adjusted = {
        str(symbol).strip().upper(): float(weight or 0.0)
        for symbol, weight in dict(target_weights or {}).items()
    }
    if threshold <= 0.0:
        return adjusted, ()

    substituted: list[str] = []
    for symbol in safe_haven_symbols:
        normalized = str(symbol or "").strip().upper()
        if not normalized:
            continue
        target_notional = max(0.0, float(investable or 0.0) * float(adjusted.get(normalized, 0.0) or 0.0))
        if 0.0 < target_notional < threshold:
            adjusted[normalized] = 0.0
            substituted.append(normalized)
    return adjusted, tuple(dict.fromkeys(substituted))


def _planned_buy_order_quantity(
    symbol,
    *,
    buy_value,
    limit_price,
    quantity_step,
    investable_buying_power,
    held_quantity=0.0,
) -> tuple[float, bool]:
    """Round the approved buy budget down; retain the legacy result shape."""
    effective_limit_price = max(0.0, float(limit_price or 0.0))
    qty = (
        _floor_order_quantity(float(buy_value or 0.0) / effective_limit_price, quantity_step=quantity_step)
        if effective_limit_price > 0.0
        else 0.0
    )
    return qty, False


def _extract_execution_report_fields(
    report: Any,
) -> tuple[str, Any, Any, Any]:
    if isinstance(report, ExecutionReport):
        return (
            str(report.status or ""),
            report.broker_order_id,
            report.filled_quantity,
            report.average_fill_price,
        )
    return (
        str(getattr(report, "status", "") or ""),
        getattr(report, "broker_order_id", None),
        getattr(report, "filled_quantity", None),
        getattr(report, "average_fill_price", None),
    )


def _projected_sell_release_value_for_report(report, *, fallback_price=0.0, fallback_quantity=0.0) -> float:
    status, _broker_order_id, raw_filled_quantity, raw_average_fill_price = _extract_execution_report_fields(report)
    if status not in {"Filled", "PartiallyFilled", "Partial"}:
        return 0.0
    filled_quantity = float(raw_filled_quantity or 0.0)
    if status == "Filled" and filled_quantity <= 0.0:
        raw_quantity = report.quantity if isinstance(report, ExecutionReport) else getattr(report, "quantity", 0.0)
        filled_quantity = float(raw_quantity or fallback_quantity or 0.0)
    if filled_quantity <= 0.0:
        return 0.0
    fill_price = float(raw_average_fill_price or 0.0)
    if fill_price <= 0.0:
        fill_price = max(0.0, float(fallback_price or 0.0))
    if fill_price <= 0.0:
        return 0.0
    return filled_quantity * fill_price


def _finalize_result(trade_logs, execution_summary, *, return_summary: bool):
    if return_summary:
        return trade_logs, execution_summary
    return trade_logs


_UNSET: Any = object()


@dataclass(frozen=True, slots=True)
class RebalanceExecutionOptions:
    """Configuration options controlling one IBKR rebalance execution cycle."""

    cash_reserve_ratio: float
    rebalance_threshold_ratio: float
    limit_buy_premium: float
    sell_settle_delay_sec: float
    strategy_profile: str | None = None
    account_group: str | None = None
    service_name: str | None = None
    account_ids: Any = None
    dry_run_only: bool = False
    execution_mode: str | None = None
    cash_reserve_floor_usd: float = 0.0
    quantity_step: float = 1.0
    min_order_notional: float = 50.0
    safe_haven_cash_substitute_threshold_usd: float = DEFAULT_SAFE_HAVEN_CASH_SUBSTITUTE_THRESHOLD_USD
    market_currency: str = "USD"
    limit_buy_premium_by_symbol: dict[str, float] | None = None
    execution_lock_dir: str | Path | None = None
    return_summary: bool = False
    cash_only_execution: bool = True
    paper_execution_admission_enabled: bool = False
    runtime_release_receipt: Any = None
    expected_strategy_release: Any = None


@dataclass(slots=True)
class _RebalanceAllocationPlan:
    strategy_symbols: tuple[str, ...]
    risk_symbols: tuple[str, ...]
    income_symbols: tuple[str, ...]
    safe_haven_symbols: tuple[str, ...]
    safe_haven_symbol: str | None
    target_weights: dict[str, float]
    approved_target_caps: dict[str, float]
    option_order_intents: tuple[dict[str, Any], ...]
    option_underliers: tuple[str, ...]
    has_executable_option_plan: bool
    trade_date: str | None
    snapshot_date: str | None
    resolved_execution_mode: str
    normalized_account_ids: tuple[str, ...]
    order_account_id: str | None
    equity: float
    reserved: float
    investable: float
    threshold: float
    order_quantity_step: float
    minimum_order_notional: float
    all_symbols: set[str]
    account_new_risk_buy_blocked: bool
    account_new_risk_reason_codes: tuple[str, ...]
    execution_summary: dict[str, Any]
    early_logs: list[str] | None = None


@dataclass(frozen=True, slots=True)
class _RebalanceQuoteCollection:
    prices: dict[str, float]
    quote_snapshots: list[dict[str, Any]]
    fallback_symbols: tuple[str, ...]
    fallback_source: str


@dataclass(frozen=True, slots=True)
class _DriftNotesContext:
    reference_target_mv: dict[str, float]
    current_mv: dict[str, float]
    current_quantities: dict[str, float]
    prices: dict[str, float]
    equity: float
    investable_buying_power: float


@dataclass(frozen=True, slots=True)
class _RebalanceOrderOutcome:
    trade_logs: list[str]
    buying_power: float = 0.0
    finalized_early: bool = False
    drift_context: _DriftNotesContext | None = None


def _resolve_rebalance_options(
    options: RebalanceExecutionOptions | None,
    **overrides: Any,
) -> RebalanceExecutionOptions:
    provided = {key: value for key, value in overrides.items() if value is not _UNSET}
    if options is not None:
        return replace(options, **provided) if provided else options
    missing = [
        name
        for name in (
            "cash_reserve_ratio",
            "rebalance_threshold_ratio",
            "limit_buy_premium",
            "sell_settle_delay_sec",
        )
        if name not in provided
    ]
    if missing:
        joined = ", ".join(repr(name) for name in missing)
        raise TypeError(f"execute_rebalance() missing required keyword-only argument(s): {joined}")
    return RebalanceExecutionOptions(**provided)


def _sum_safe_haven_weight(
    target_weights: dict[str, float],
    safe_haven_symbols: tuple[str, ...],
) -> float:
    return float(
        sum(
            float(target_weights.get(str(symbol or "").strip().upper(), 0.0) or 0.0)
            for symbol in safe_haven_symbols
        )
    )


def _build_claimed_submitter(
    *,
    submit_order_intent,
    account_values,
    signal_metadata: dict[str, Any],
    acquire_execution_claim,
    account_execution_session,
    options: RebalanceExecutionOptions,
):
    """Build the explicit broker order submitter with claim, session, and breaker gates."""
    if options.dry_run_only:
        return submit_order_intent

    def submit_claimed_order(ib, order_intent, **kwargs):
        from quant_platform_kit.common import execution_state as contract

        session_type = getattr(contract, "AccountSubmissionSession", ())
        if (
            not isinstance(account_execution_session, session_type)
            or account_execution_session.contract_version != "account_submission.v1"
        ):
            raise RuntimeError("IBKR account submission session/contract required")
        if (
            account_execution_session.broker != "ibkr"
            or account_execution_session.release_id != contract.submission_release_digest(options.runtime_release_receipt)
            or account_execution_session.intent_id
            != contract.submission_intent_digest(signal_metadata, options.strategy_profile)
            or account_execution_session.profile != options.strategy_profile
        ):
            raise RuntimeError("IBKR account session intent/release binding mismatch")
        if acquire_execution_claim is None or not acquire_execution_claim():
            # N13 T1 consult-only: local claim still wins; ignore decision.
            consult_t1_live_submit(identity_held=False, dry_run_bypass=False)
            raise RuntimeError("IBKR execution claim required; refusing broker submission")
        # Shadow consult after successful claim; deny must not block submit.
        consult_t1_live_submit(identity_held=True, dry_run_bypass=False)
        new_risk = str(order_intent.side).strip().lower() == "buy" and is_account_new_risk_gate_enabled()
        if new_risk:
            latest = evaluate_persistent_account_new_risk_admission(
                account_values,
                signal_metadata=signal_metadata,
                session=account_execution_session,
            )
            if new_risk_buy_prohibited(latest):
                raise RuntimeError("IBKR persistent account breaker prohibits new risk")
        # Settlement preparation must finish before reserving the exact intent.
        grant = (
            None
            if isinstance(ib, _SettledCashDeadlineIB)
            else account_execution_session.reserve(order_intent, new_risk=new_risk)
        )
        return submit_order_intent(
            ib,
            order_intent,
            account_execution_session=account_execution_session,
            execution_grant=grant,
            **kwargs,
        )

    return submit_claimed_order


def _init_execution_summary(
    *,
    resolved_execution_mode: str,
    normalized_account_ids: tuple[str, ...],
    order_account_id: str | None,
    trade_date: str | None,
    snapshot_date: str | None,
    safe_haven_symbol: str | None,
    option_order_intents: tuple[dict[str, Any], ...],
    option_underliers: tuple[str, ...],
    signal_metadata: dict[str, Any],
    account_values,
    options: RebalanceExecutionOptions,
) -> dict[str, Any]:
    return {
        "mode": resolved_execution_mode,
        "strategy_profile": options.strategy_profile,
        "account_group": options.account_group,
        "account_ids": list(normalized_account_ids),
        "order_account_id": order_account_id,
        "service_name": options.service_name,
        "trade_date": trade_date,
        "snapshot_as_of": snapshot_date,
        "safe_haven_symbol": safe_haven_symbol,
        "market_currency": str(options.market_currency or "USD").strip().upper(),
        "target_stock_weight": signal_metadata.get("target_stock_weight"),
        "realized_stock_weight": signal_metadata.get("realized_stock_weight"),
        "target_safe_haven_weight": signal_metadata.get("safe_haven_weight"),
        "realized_safe_haven_weight": signal_metadata.get("safe_haven_weight"),
        "orders_submitted": [],
        "orders_pending": [],
        "orders_filled": [],
        "orders_partially_filled": [],
        "orders_skipped": [],
        "option_order_intent_count": len(option_order_intents),
        "option_order_underliers": list(option_underliers),
        "option_orders_submitted": [],
        "option_orders_pending": [],
        "option_orders_filled": [],
        "option_orders_partially_filled": [],
        "option_orders_skipped": [],
        "skipped_reasons": [],
        "target_vs_current": [],
        "execution_status": "not_started",
        "no_op_reason": None,
        "cash_reserve_dollars": 0.0,
        "safe_haven_cash_substitute_threshold_usd": float(
            max(0.0, float(options.safe_haven_cash_substitute_threshold_usd or 0.0))
        ),
        "safe_haven_cash_substituted_symbols": [],
        "small_account_whole_share_substituted_symbols": [],
        "small_account_safe_haven_cash_substituted_symbols": [],
        "small_account_whole_share_cash_notes": [],
        "small_account_allocation_drift_notes": [],
        "small_account_buy_blocked": False,
        "small_account_buy_block_reason": None,
        "account_new_risk_buy_blocked": False,
        "account_new_risk_block_reason": None,
        "account_new_risk_reason_codes": [],
        "residual_cash_estimate": float(account_values.get("buying_power", 0.0) or 0.0),
        "projected_sell_release_value": 0.0,
        "current_stock_weight": 0.0,
        "current_safe_haven_weight": 0.0,
        "price_source_mode": "market_quote",
        "snapshot_price_fallback_used": False,
        "snapshot_price_fallback_symbols": [],
        "snapshot_price_fallback_count": 0,
        "lock_path": None,
        "paper_execution_admission": {},
    }


def _apply_account_new_risk_gate(
    target_weights: dict[str, float],
    *,
    account_values,
    signal_metadata: dict[str, Any],
    account_execution_session,
    execution_summary: dict[str, Any],
    dry_run_only: bool,
) -> tuple[dict[str, float], bool, tuple[str, ...]]:
    if not is_account_new_risk_gate_enabled():
        set_cycle_snapshot(None)
        return target_weights, False, ()
    # Inner dry-run previews without a session retain their diagnostic
    # purpose; they make no persistence claim. Native cycles always restore
    # the journal, including no-target and dry-run cycles with a store.
    admission = evaluate_account_values_new_risk_admission(account_values, signal_metadata=signal_metadata)
    if dry_run_only and account_execution_session is None:
        execution_summary["persistent_breaker_status"] = "NOT_PROVEN"
    else:
        admission = evaluate_persistent_account_new_risk_admission(
            account_values,
            signal_metadata=signal_metadata,
            session=account_execution_session,
        )
    account_new_risk_buy_blocked = new_risk_buy_prohibited(admission)
    account_new_risk_reason_codes = tuple(admission.reason_codes)
    portfolio = build_portfolio_from_account_values(account_values, signal_metadata=signal_metadata)
    cycle_snapshot = build_snapshot_from_portfolio(portfolio)
    set_cycle_snapshot(cycle_snapshot)
    attention_counts = maybe_publish_attention_for_admission(
        admission,
        portfolio=portfolio,
        snapshot=cycle_snapshot,
    )
    print(
        "[Attention notify] "
        f"sent={attention_counts.get('sent', 0)} "
        f"skipped={attention_counts.get('skipped', 0)} "
        f"failed={attention_counts.get('failed', 0)}",
        flush=True,
    )
    scaled_weights = scale_targets_for_admission(target_weights, admission, snapshot=cycle_snapshot)
    if admission.combined_scale is not None:
        print(
            f"[Envelope scale] combined_scale={admission.combined_scale} "
            "subject_to_stoploss_exit_authority",
            flush=True,
        )
    return scaled_weights, account_new_risk_buy_blocked, account_new_risk_reason_codes


def _resolve_rebalance_allocation(
    *,
    positions,
    account_values,
    signal_metadata: dict[str, Any],
    account_execution_session,
    translator,
    options: RebalanceExecutionOptions,
) -> _RebalanceAllocationPlan:
    """Validate authority, resolve target weights, and apply account risk-gate scaling."""
    _require_tqqq_effective_date(signal_metadata, strategy_profile=options.strategy_profile)
    allocation = _resolve_weight_allocation(signal_metadata)
    target_weights = dict(allocation["targets"])
    approved_target_caps = _require_live_risk_authority(
        signal_metadata,
        allocation,
        strategy_profile=options.strategy_profile,
        account_ids=options.account_ids,
    )
    option_order_intents = _normalize_option_order_intents(signal_metadata)
    option_underliers = _option_intent_underliers(option_order_intents)
    has_executable_option_plan = _has_executable_option_plan(option_order_intents)
    if has_executable_option_plan:
        raise RuntimeError("IBKR live option intents lack final RiskEngine order limits")
    strategy_symbols = tuple(allocation["strategy_symbols"])
    trade_date = str(signal_metadata.get("trade_date") or "").strip() or None
    snapshot_date = _normalize_date_like(signal_metadata.get("snapshot_as_of"))
    resolved_execution_mode = _resolve_execution_mode(
        dry_run_only=options.dry_run_only,
        execution_mode=options.execution_mode,
    )
    safe_haven_symbols = tuple(allocation["safe_haven_symbols"])
    safe_haven_symbol = safe_haven_symbols[0] if safe_haven_symbols else None
    normalized_account_ids = _normalize_account_ids(options.account_ids)
    order_account_id = _resolve_order_account_id(normalized_account_ids)

    execution_summary = _init_execution_summary(
        resolved_execution_mode=resolved_execution_mode,
        normalized_account_ids=normalized_account_ids,
        order_account_id=order_account_id,
        trade_date=trade_date,
        snapshot_date=snapshot_date,
        safe_haven_symbol=safe_haven_symbol,
        option_order_intents=option_order_intents,
        option_underliers=option_underliers,
        signal_metadata=signal_metadata,
        account_values=account_values,
        options=options,
    )

    equity = float(account_values.get("equity", 0) or 0.0)
    cash_only_deleverage_mode = bool(signal_metadata.get("cash_only_deleverage_mode"))
    if equity <= 0:
        gross_positions = sum(
            float(details.get("quantity") or 0.0) * float(details.get("avg_cost") or 0.0)
            for details in dict(positions or {}).values()
        )
        if options.cash_only_execution and gross_positions > 0.0:
            equity = gross_positions
            cash_only_deleverage_mode = True
        else:
            execution_summary["execution_status"] = "blocked"
            execution_summary["no_op_reason"] = "no_equity"
            return _RebalanceAllocationPlan(
                strategy_symbols=strategy_symbols,
                risk_symbols=tuple(allocation["risk_symbols"]),
                income_symbols=tuple(allocation["income_symbols"]),
                safe_haven_symbols=safe_haven_symbols,
                safe_haven_symbol=safe_haven_symbol,
                target_weights=target_weights,
                approved_target_caps=approved_target_caps,
                option_order_intents=option_order_intents,
                option_underliers=option_underliers,
                has_executable_option_plan=has_executable_option_plan,
                trade_date=trade_date,
                snapshot_date=snapshot_date,
                resolved_execution_mode=resolved_execution_mode,
                normalized_account_ids=normalized_account_ids,
                order_account_id=order_account_id,
                equity=equity,
                reserved=0.0,
                investable=0.0,
                threshold=0.0,
                order_quantity_step=float(options.quantity_step or 1.0),
                minimum_order_notional=max(0.0, float(options.min_order_notional or 0.0)),
                all_symbols=set(),
                account_new_risk_buy_blocked=False,
                account_new_risk_reason_codes=(),
                execution_summary=execution_summary,
                early_logs=[translator("no_equity")],
            )
    execution_summary["cash_only_deleverage_mode"] = cash_only_deleverage_mode

    target_weights, account_new_risk_buy_blocked, account_new_risk_reason_codes = _apply_account_new_risk_gate(
        target_weights,
        account_values=account_values,
        signal_metadata=signal_metadata,
        account_execution_session=account_execution_session,
        execution_summary=execution_summary,
        dry_run_only=options.dry_run_only,
    )

    reserved = max(
        float(equity) * float(options.cash_reserve_ratio or 0.0),
        max(0.0, float(options.cash_reserve_floor_usd or 0.0)),
    )
    investable = equity - reserved
    target_weights, substituted_safe_haven_symbols = _apply_safe_haven_cash_substitution_to_weights(
        target_weights,
        safe_haven_symbols=safe_haven_symbols,
        investable=investable,
        threshold_usd=options.safe_haven_cash_substitute_threshold_usd,
    )
    execution_summary["safe_haven_cash_substituted_symbols"] = list(substituted_safe_haven_symbols)
    if safe_haven_symbols:
        execution_summary["realized_safe_haven_weight"] = _sum_safe_haven_weight(
            target_weights,
            safe_haven_symbols,
        )
    threshold = equity * options.rebalance_threshold_ratio
    order_quantity_step = float(options.quantity_step or 1.0)
    minimum_order_notional = max(0.0, float(options.min_order_notional or 0.0))
    execution_summary["cash_reserve_dollars"] = float(reserved)

    all_symbols = set(target_weights.keys()) | set(positions.keys())
    if strategy_symbols:
        all_symbols = all_symbols & set(strategy_symbols)

    return _RebalanceAllocationPlan(
        strategy_symbols=strategy_symbols,
        risk_symbols=tuple(allocation["risk_symbols"]),
        income_symbols=tuple(allocation["income_symbols"]),
        safe_haven_symbols=safe_haven_symbols,
        safe_haven_symbol=safe_haven_symbol,
        target_weights=target_weights,
        approved_target_caps=approved_target_caps,
        option_order_intents=option_order_intents,
        option_underliers=option_underliers,
        has_executable_option_plan=has_executable_option_plan,
        trade_date=trade_date,
        snapshot_date=snapshot_date,
        resolved_execution_mode=resolved_execution_mode,
        normalized_account_ids=normalized_account_ids,
        order_account_id=order_account_id,
        equity=equity,
        reserved=reserved,
        investable=investable,
        threshold=threshold,
        order_quantity_step=order_quantity_step,
        minimum_order_notional=minimum_order_notional,
        all_symbols=all_symbols,
        account_new_risk_buy_blocked=account_new_risk_buy_blocked,
        account_new_risk_reason_codes=account_new_risk_reason_codes,
        execution_summary=execution_summary,
    )


def _collect_rebalance_quotes(
    ib,
    symbols: set[str],
    *,
    signal_metadata: dict[str, Any],
    fetch_quote_snapshots,
    dry_run_only: bool,
) -> _RebalanceQuoteCollection:
    """Fetch market quotes, serialize snapshots, and apply snapshot price fallbacks."""
    quote_snapshots_by_symbol: dict[str, dict[str, Any]] = {}

    def record_quote_snapshot(symbol, snapshot) -> None:
        payload = _serialize_quote_snapshot(snapshot, symbol=symbol)
        resolved_symbol = payload.get("symbol")
        if resolved_symbol:
            quote_snapshots_by_symbol[resolved_symbol] = payload

    snapshot_price_fallbacks = _normalize_price_fallbacks(signal_metadata)
    price_fallback_source = str(signal_metadata.get("price_fallback_source") or "").strip() or (
        "snapshot_close" if signal_metadata.get("dry_run_price_fallbacks") else "close"
    )
    prices = get_market_prices(
        ib,
        symbols,
        fetch_quote_snapshots=fetch_quote_snapshots,
        quote_recorder=record_quote_snapshot,
    )
    prices, fallback_symbols = _apply_snapshot_price_fallbacks(
        prices,
        symbols,
        dry_run_only=dry_run_only,
        snapshot_price_fallbacks=snapshot_price_fallbacks,
    )
    return _RebalanceQuoteCollection(
        prices=prices,
        quote_snapshots=list(quote_snapshots_by_symbol.values()),
        fallback_symbols=fallback_symbols,
        fallback_source=price_fallback_source,
    )


def _record_quote_collection_summary(
    execution_summary: dict[str, Any],
    quote_collection: _RebalanceQuoteCollection,
) -> None:
    fallback_symbols = quote_collection.fallback_symbols
    has_fallback = bool(fallback_symbols)
    execution_summary["snapshot_price_fallback_used"] = has_fallback
    execution_summary["snapshot_price_fallback_symbols"] = list(fallback_symbols)
    execution_summary["snapshot_price_fallback_count"] = len(fallback_symbols)
    execution_summary["price_fallback_used"] = has_fallback
    execution_summary["price_fallback_symbols"] = list(fallback_symbols)
    execution_summary["price_fallback_count"] = len(fallback_symbols)
    execution_summary["price_fallback_source"] = quote_collection.fallback_source if has_fallback else None
    execution_summary["quote_snapshot"] = {
        "quotes": quote_collection.quote_snapshots,
    }
    if has_fallback:
        execution_summary["price_source_mode"] = f"mixed_market_quote_{quote_collection.fallback_source}"


def _append_small_account_allocation_drift_notes(
    execution_summary: dict[str, Any],
    trade_logs: list[str],
    *,
    translator,
    context: _DriftNotesContext,
) -> None:
    if execution_summary.get("execution_status") == "blocked":
        return
    if execution_summary.get("small_account_allocation_drift_notes"):
        return
    submitted_orders = (
        tuple(execution_summary.get("orders_submitted") or ())
        + tuple(execution_summary.get("orders_pending") or ())
        + tuple(execution_summary.get("orders_filled") or ())
        + tuple(execution_summary.get("orders_partially_filled") or ())
    )
    notes = build_small_account_allocation_drift_notes(
        target_values=context.reference_target_mv,
        current_values=context.current_mv,
        current_quantities=context.current_quantities,
        prices=context.prices,
        submitted_orders=submitted_orders,
        total_value=float(context.equity or 0.0),
        cash_value=context.investable_buying_power,
    )
    if not notes:
        return
    execution_summary["small_account_allocation_drift_notes"] = list(notes)
    trade_logs.extend(format_small_account_allocation_drift_notes(notes, translator=translator))


def _prepare_post_quote_targets(
    *,
    positions,
    account_values,
    allocation_plan: _RebalanceAllocationPlan,
    prices: dict[str, float],
    translator,
) -> tuple[dict[str, float], dict[str, float], dict[str, float], list[str], str, _DriftNotesContext]:
    execution_summary = allocation_plan.execution_summary
    target_weights = allocation_plan.target_weights
    current_mv = {
        symbol: positions.get(symbol, {}).get("quantity", 0) * prices.get(symbol, 0)
        for symbol in allocation_plan.all_symbols
    }
    current_quantities = {
        symbol: float(positions.get(symbol, {}).get("quantity", 0.0) or 0.0)
        for symbol in allocation_plan.all_symbols
    }
    target_mv = {symbol: allocation_plan.investable * weight for symbol, weight in target_weights.items()}
    candidate_symbols = tuple(
        dict.fromkeys(
            str(symbol or "").strip().upper()
            for symbol in allocation_plan.risk_symbols + allocation_plan.income_symbols
            if str(symbol or "").strip()
        )
    )
    if not candidate_symbols:
        candidate_symbols = tuple(
            str(symbol or "").strip().upper()
            for symbol in target_mv
            if str(symbol or "").strip().upper() not in allocation_plan.safe_haven_symbols
        )
    small_account_reference_target_mv = {
        symbol: target_mv.get(symbol, 0.0)
        for symbol in candidate_symbols
        if symbol in target_mv
    }
    small_account_compatibility = apply_small_account_cash_compatibility(
        target_mv,
        prices,
        candidate_symbols=candidate_symbols,
        safe_haven_cash_symbols=allocation_plan.safe_haven_symbols,
        quantity_step=allocation_plan.order_quantity_step,
        cash_substitute_limit_usd=SMALL_ACCOUNT_SAFE_HAVEN_CASH_SUBSTITUTE_LIMIT_USD,
    )
    target_mv = dict(small_account_compatibility.targets)
    for symbol, value in target_mv.items():
        cap = allocation_plan.approved_target_caps.get(symbol)
        if cap is None or not math.isfinite(float(value)) or float(value) > cap + 0.01:
            raise RuntimeError("IBKR target exceeds RiskEngine order limit")
    small_account_substituted_symbols = small_account_compatibility.whole_share_substituted_symbols
    for symbol in small_account_substituted_symbols:
        target_weights[symbol] = 0.0
    small_account_safe_haven_cash_substituted_symbols = list(
        small_account_compatibility.safe_haven_cash_substituted_symbols
    )
    for symbol in small_account_safe_haven_cash_substituted_symbols:
        target_weights[symbol] = 0.0
    if allocation_plan.safe_haven_symbols:
        execution_summary["realized_safe_haven_weight"] = _sum_safe_haven_weight(
            target_weights,
            allocation_plan.safe_haven_symbols,
        )
    execution_summary["small_account_whole_share_substituted_symbols"] = list(
        small_account_substituted_symbols
    )
    execution_summary["small_account_safe_haven_cash_substituted_symbols"] = (
        small_account_safe_haven_cash_substituted_symbols
    )
    execution_summary["small_account_existing_whole_share_retained_symbols"] = []
    execution_summary["small_account_whole_share_bootstrap_symbols"] = []
    execution_summary["small_account_whole_share_cash_notes"] = list(
        small_account_compatibility.cash_substitution_notes
    )
    trade_logs = list(
        format_small_account_cash_substitution_notes(
            small_account_compatibility.cash_substitution_notes,
            translator=translator,
        )
    )
    target_hash = _build_target_hash(target_weights)
    execution_summary["target_vs_current"] = _build_target_diff_rows(
        target_weights,
        current_mv,
        allocation_plan.equity,
    )
    drift_context = _DriftNotesContext(
        reference_target_mv=small_account_reference_target_mv,
        current_mv=current_mv,
        current_quantities=current_quantities,
        prices=prices,
        equity=allocation_plan.equity,
        investable_buying_power=_investable_buying_power(
            float(account_values.get("buying_power", 0.0) or 0.0),
            allocation_plan.reserved,
        ),
    )
    return current_mv, current_quantities, target_mv, trade_logs, target_hash, drift_context


def _record_blocked_step(
    execution_summary: dict[str, Any],
    trade_logs: list[str],
    *,
    reason: str,
    translator,
    log_message: str | None = None,
) -> None:
    execution_summary["execution_status"] = "blocked"
    execution_summary["no_op_reason"] = reason
    execution_summary["skipped_reasons"].append(reason)
    trade_logs.append(log_message if log_message is not None else translator("failed", reason=reason))


def _append_execution_detail_logs(
    trade_logs: list[str],
    *,
    allocation_plan: _RebalanceAllocationPlan,
    quote_collection: _RebalanceQuoteCollection,
    current_mv: dict[str, float],
    signal_metadata: dict[str, Any],
    translator,
    dry_run_only: bool,
) -> None:
    trade_logs.append(
        " | ".join(
            [
                translator(
                    "execution_profile_detail",
                    profile=_display_text(allocation_plan.execution_summary["strategy_profile"], fallback="<unknown>"),
                ),
                translator(
                    "regime_detail",
                    value=_display_text(signal_metadata.get("regime"), fallback="<none>"),
                ),
                translator(
                    "breadth_detail",
                    value=f"{float(signal_metadata.get('breadth_ratio', 0.0) or 0.0):.1%}",
                ),
                translator(
                    "target_stock_detail",
                    value=f"{float(signal_metadata.get('target_stock_weight', 0.0) or 0.0):.1%}",
                ),
                translator(
                    "realized_stock_detail",
                    value=f"{float(signal_metadata.get('realized_stock_weight', 0.0) or 0.0):.1%}",
                ),
                translator(
                    "snapshot_as_of_detail",
                    value=_display_text(allocation_plan.snapshot_date, fallback="<none>"),
                ),
                translator(
                    "trade_date_detail",
                    value=_display_text(allocation_plan.trade_date, fallback="<none>"),
                ),
            ]
        )
    )
    if quote_collection.fallback_symbols:
        trade_logs.append(
            translator(
                "dry_run_snapshot_prices" if dry_run_only else "price_fallback_prices",
                count=len(quote_collection.fallback_symbols),
                symbols=_format_symbol_preview(quote_collection.fallback_symbols),
            )
        )
    trade_logs.extend(
        _format_target_lines(
            allocation_plan.target_weights,
            current_mv,
            allocation_plan.equity,
            translator=translator,
        )
    )


def _cash_sweep_sale_quantity_to_fund_buy(
    *,
    max_quantity: int,
    candidate_symbols: tuple[str, ...],
    safe_haven_symbol: str | None,
    cash_sweep_price: float,
    investable_anticipated_buying_power: float,
    target_mv: dict[str, float],
    current_mv: dict[str, float],
    threshold: float,
    prices: dict[str, float],
    options: RebalanceExecutionOptions,
) -> int:
    if max_quantity <= 0 or not safe_haven_symbol or cash_sweep_price <= 0.0:
        return 0
    base_buying_power = max(0.0, float(investable_anticipated_buying_power))
    funding_needs = []
    for symbol in candidate_symbols:
        underweight_value = target_mv[symbol] - current_mv.get(symbol, 0.0)
        if underweight_value <= threshold:
            continue
        ask = prices.get(symbol)
        if not ask or ask <= 0.0:
            continue
        funding_needs.append(
            (
                underweight_value,
                _limit_buy_price(
                    symbol,
                    ask,
                    options.limit_buy_premium,
                    options.limit_buy_premium_by_symbol,
                ),
            )
        )
    if should_sell_cash_sweep_to_fund_whole_share_buy(
        float(max_quantity),
        cash_sweep_price,
        base_buying_power,
        funding_needs,
    ):
        return int(max_quantity)
    return 0


def _plan_rebalance_directions(
    *,
    positions,
    allocation_plan: _RebalanceAllocationPlan,
    current_mv: dict[str, float],
    target_mv: dict[str, float],
    prices: dict[str, float],
    investable_anticipated_buying_power: float,
    pending_cash_risk_unknown: bool,
    settled_cash_ledger_unverified: bool,
    execution_summary: dict[str, Any],
    trade_logs: list[str],
    translator,
    options: RebalanceExecutionOptions,
    drift_context: _DriftNotesContext,
) -> tuple[int, bool]:
    missing_price_symbols: list[str] = []
    insufficient_buying_power_symbols: list[str] = []
    min_notional_symbols: list[str] = []
    quantity_zero_symbols: list[str] = []
    threshold = allocation_plan.threshold
    safe_haven_symbol = allocation_plan.safe_haven_symbol
    cash_sweep_quantity = 0
    cash_sweep_price = float(prices.get(safe_haven_symbol, 0.0) or 0.0) if safe_haven_symbol else 0.0

    has_sell_plan = False
    for symbol in allocation_plan.all_symbols:
        current = current_mv.get(symbol, 0.0)
        target = target_mv.get(symbol, 0.0)
        if current <= target + threshold:
            continue
        price = prices.get(symbol)
        if not price:
            missing_price_symbols.append(symbol)
            continue
        qty = _sell_order_quantity(
            current_value=current,
            target_value=target,
            price=price,
            position_quantity=positions.get(symbol, {}).get("quantity", 0),
            quantity_step=allocation_plan.order_quantity_step,
        )
        if qty > 0:
            has_sell_plan = True
            break
        quantity_zero_symbols.append(symbol)

    funding_buy_candidates = [
        symbol
        for symbol in target_mv
        if symbol != safe_haven_symbol
        and (target_mv[symbol] - current_mv.get(symbol, 0.0)) > threshold
        and abs(target_mv[symbol] - current_mv.get(symbol, 0.0)) > allocation_plan.minimum_order_notional
    ]
    if allocation_plan.account_new_risk_buy_blocked:
        funding_buy_candidates = []
    if (
        not has_sell_plan
        and funding_buy_candidates
        and safe_haven_symbol
        and cash_sweep_price > 0.0
        and float(positions.get(safe_haven_symbol, {}).get("quantity", 0.0) or 0.0) > 0.0
    ):
        cash_sweep_quantity = _cash_sweep_sale_quantity_to_fund_buy(
            max_quantity=int(float(positions.get(safe_haven_symbol, {}).get("quantity", 0.0) or 0.0)),
            candidate_symbols=tuple(funding_buy_candidates),
            safe_haven_symbol=safe_haven_symbol,
            cash_sweep_price=cash_sweep_price,
            investable_anticipated_buying_power=investable_anticipated_buying_power,
            target_mv=target_mv,
            current_mv=current_mv,
            threshold=threshold,
            prices=prices,
            options=options,
        )
        if cash_sweep_quantity > 0:
            has_sell_plan = True

    has_buy_plan = False
    for symbol, target in target_mv.items():
        current = current_mv.get(symbol, 0.0)
        if current >= target - threshold:
            continue
        price = prices.get(symbol)
        if not price:
            missing_price_symbols.append(symbol)
            continue
        buy_value = min(target - current, investable_anticipated_buying_power * 0.95)
        if buy_value <= 0:
            insufficient_buying_power_symbols.append(symbol)
            continue
        if buy_value < allocation_plan.minimum_order_notional:
            min_notional_symbols.append(symbol)
            continue
        limit_price = _limit_buy_price(
            symbol,
            price,
            options.limit_buy_premium,
            options.limit_buy_premium_by_symbol,
        )
        qty, _forced_whole_share = _planned_buy_order_quantity(
            symbol,
            buy_value=buy_value,
            limit_price=limit_price,
            quantity_step=allocation_plan.order_quantity_step,
            investable_buying_power=investable_anticipated_buying_power,
            held_quantity=max(0.0, float(positions.get(symbol, {}).get("quantity", 0.0) or 0.0)),
        )
        if qty > 0:
            has_buy_plan = True
            break
        quantity_zero_symbols.append(symbol)

    if (
        has_buy_plan
        and not options.dry_run_only
        and "PERSISTENT_BREAKER_UNAVAILABLE" in allocation_plan.account_new_risk_reason_codes
    ):
        raise RuntimeError("IBKR persistent breaker session/contract unavailable; refusing new risk")

    if (
        (pending_cash_risk_unknown or settled_cash_ledger_unverified)
        and any(target_mv[s] > current_mv.get(s, 0.0) + threshold for s in target_mv)
        and not has_sell_plan
    ):
        # No qualified reduction will be submitted. Reject before taking the
        # daily execution lock, so a later complete observation can be checked
        # normally without clearing any execution or reconciliation marker.
        reason = "account_new_risk_gate"
        execution_summary["execution_status"] = "blocked"
        execution_summary["no_op_reason"] = reason
        execution_summary["account_new_risk_buy_blocked"] = True
        execution_summary["account_new_risk_block_reason"] = reason
        execution_summary["account_new_risk_reason_codes"] = list(allocation_plan.account_new_risk_reason_codes)
        execution_summary["skipped_reasons"].append(reason)
        trade_logs.append(
            translator(
                "buy_deferred",
                detail="[Account new-risk gate] " + ", ".join(allocation_plan.account_new_risk_reason_codes),
            )
        )
        return cash_sweep_quantity, True

    if not has_sell_plan and not has_buy_plan:
        reason = "target_diff_below_threshold"
        status = "no_op"
        if missing_price_symbols:
            symbols = ",".join(sorted(dict.fromkeys(missing_price_symbols)))
            reason = f"missing_price:{symbols}"
            status = "blocked"
            execution_summary["orders_skipped"].extend(
                {"symbol": symbol, "reason": "missing_price"}
                for symbol in sorted(dict.fromkeys(missing_price_symbols))
            )
        elif insufficient_buying_power_symbols:
            symbols = ",".join(sorted(dict.fromkeys(insufficient_buying_power_symbols)))
            reason = f"insufficient_buying_power:{symbols}"
            status = "blocked"
        elif min_notional_symbols:
            symbols = ",".join(sorted(dict.fromkeys(min_notional_symbols)))
            reason = f"min_notional:{symbols}"
        elif quantity_zero_symbols:
            symbols = ",".join(sorted(dict.fromkeys(quantity_zero_symbols)))
            reason = f"quantity_zero:{symbols}"

        if not (reason == "target_diff_below_threshold" and allocation_plan.has_executable_option_plan):
            _record_unsupported_option_intents(execution_summary, allocation_plan.option_order_intents)
            if execution_summary["option_orders_skipped"] and reason == "target_diff_below_threshold":
                reason = "option_orders_skipped"
        execution_summary["execution_status"] = status
        execution_summary["no_op_reason"] = reason
        if reason != "target_diff_below_threshold":
            execution_summary["skipped_reasons"].append(reason)
            if status == "blocked":
                trade_logs.append(translator("failed", reason=reason))
        if not (reason == "target_diff_below_threshold" and allocation_plan.has_executable_option_plan):
            _append_small_account_allocation_drift_notes(
                execution_summary,
                trade_logs,
                translator=translator,
                context=drift_context,
            )
            return cash_sweep_quantity, True

    return cash_sweep_quantity, False


def _acquire_cycle_execution_lock(
    *,
    allocation_plan: _RebalanceAllocationPlan,
    target_hash: str,
    trade_logs: list[str],
    translator,
    options: RebalanceExecutionOptions,
) -> bool:
    execution_summary = allocation_plan.execution_summary
    lock_path = _resolve_execution_lock_path(
        strategy_profile=options.strategy_profile,
        account_group=options.account_group,
        service_name=options.service_name,
        trade_date=allocation_plan.trade_date,
        snapshot_date=allocation_plan.snapshot_date,
        execution_mode=allocation_plan.resolved_execution_mode,
        execution_lock_dir=options.execution_lock_dir,
    )
    lock_payload = _build_execution_lock_payload(
        strategy_profile=options.strategy_profile,
        account_group=options.account_group,
        service_name=options.service_name,
        account_ids=allocation_plan.normalized_account_ids,
        trade_date=allocation_plan.trade_date,
        snapshot_date=allocation_plan.snapshot_date,
        target_hash=target_hash,
        execution_mode=allocation_plan.resolved_execution_mode,
    )
    if not _try_create_execution_lock(lock_path, lock_payload):
        existing = _read_execution_lock(lock_path) or {}
        reason = (
            f"same_day_execution_locked:mode={allocation_plan.resolved_execution_mode}:"
            f"target_hash={existing.get('target_hash', '<unknown>')}"
        )
        execution_summary["lock_path"] = str(lock_path)
        _record_blocked_step(
            execution_summary,
            trade_logs,
            reason=reason,
            translator=translator,
            log_message=translator(
                "same_day_execution_locked",
                profile=_display_text(options.strategy_profile, fallback="<unknown>"),
                mode=allocation_plan.resolved_execution_mode,
                trade_date=_display_text(allocation_plan.trade_date, fallback="<none>"),
                snapshot_date=_display_text(allocation_plan.snapshot_date, fallback="<none>"),
                target_hash=_display_text(existing.get("target_hash"), fallback="<unknown>"),
                lock_path=str(lock_path),
            ),
        )
        return False
    execution_summary["lock_path"] = str(lock_path)
    trade_logs.append(
        translator(
            "execution_lock_acquired",
            mode=allocation_plan.resolved_execution_mode,
            trade_date=_display_text(allocation_plan.trade_date, fallback="<none>"),
            snapshot_date=_display_text(allocation_plan.snapshot_date, fallback="<none>"),
            lock_path=str(lock_path),
        )
    )
    execution_summary["execution_status"] = "executing"
    return True


def _record_submitted_equity_order(
    execution_summary: dict[str, Any],
    trade_logs: list[str],
    report: Any,
    *,
    symbol: str,
    side: str,
    quantity: float,
    order_account_id: str | None,
    translator,
    limit_price: float | None = None,
) -> str:
    status, broker_order_id, filled_quantity, average_fill_price = _extract_execution_report_fields(report)
    order_payload: dict[str, Any] = {
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "status": status,
        "broker_order_id": broker_order_id,
        "account_id": order_account_id,
        "filled_quantity": filled_quantity,
        "average_fill_price": average_fill_price,
    }
    if limit_price is not None:
        order_payload["limit_price"] = limit_price
    outcome = _record_order_outcome(execution_summary, order_payload, status=status)
    if outcome == "unknown":
        trade_logs.append(translator("failed", reason="broker_order_outcome_unknown"))
        return outcome
    _, status_msg = check_order_submitted(report, translator=translator)
    if side == "sell":
        log_prefix = translator("market_sell", symbol=symbol, qty=format_quantity(quantity))
    else:
        log_prefix = translator(
            "limit_buy",
            symbol=symbol,
            qty=format_quantity(quantity),
            price=f"{float(limit_price or 0.0):.2f}",
        )
    trade_logs.append(f"{log_prefix} {status_msg}")
    return outcome


def _resolve_sell_quantity(
    *,
    symbol: str,
    current: float,
    target: float,
    price: float | None,
    position_quantity: float,
    threshold: float,
    quantity_step: float,
    safe_haven_symbol: str | None,
    cash_sweep_quantity: int,
    execution_summary: dict[str, Any],
    pending_sell_release_symbols: list[str],
) -> float | int:
    is_cash_sweep = symbol == safe_haven_symbol and cash_sweep_quantity > 0
    if not is_cash_sweep and current <= target + threshold:
        return 0
    if not price:
        execution_summary["orders_skipped"].append({"symbol": symbol, "side": "sell", "reason": "missing_price"})
        execution_summary["skipped_reasons"].append(f"missing_price:{symbol}")
        return 0
    qty = _sell_order_quantity(
        current_value=current,
        target_value=target,
        price=price,
        position_quantity=position_quantity,
        quantity_step=quantity_step,
    )
    if is_cash_sweep:
        qty = max(int(cash_sweep_quantity), int(qty))
    if qty <= 0:
        if current > target + threshold:
            pending_sell_release_symbols.append(symbol)
        execution_summary["orders_skipped"].append({"symbol": symbol, "side": "sell", "reason": "quantity_zero"})
        return 0
    return qty


def _execute_sell_orders(
    ib,
    *,
    positions,
    allocation_plan: _RebalanceAllocationPlan,
    current_mv: dict[str, float],
    target_mv: dict[str, float],
    prices: dict[str, float],
    cash_sweep_quantity: int,
    claimed_submitter,
    order_intent_cls,
    translator,
    trade_logs: list[str],
    dry_run_only: bool,
) -> tuple[bool, float, float, list[str], bool]:
    execution_summary = allocation_plan.execution_summary
    sell_executed = False
    projected_sell_release_value = 0.0
    dry_run_sale_proceeds = 0.0
    pending_sell_release_symbols: list[str] = []

    for symbol in allocation_plan.all_symbols:
        current = current_mv.get(symbol, 0)
        target = target_mv.get(symbol, 0)
        price = prices.get(symbol)
        qty = _resolve_sell_quantity(
            symbol=symbol,
            current=current,
            target=target,
            price=price,
            position_quantity=positions.get(symbol, {}).get("quantity", 0),
            threshold=allocation_plan.threshold,
            quantity_step=allocation_plan.order_quantity_step,
            safe_haven_symbol=allocation_plan.safe_haven_symbol,
            cash_sweep_quantity=cash_sweep_quantity,
            execution_summary=execution_summary,
            pending_sell_release_symbols=pending_sell_release_symbols,
        )
        if qty <= 0:
            continue

        held_quantity = float(positions.get(symbol, {}).get("quantity", 0.0) or 0.0)
        if (
            symbol not in allocation_plan.strategy_symbols
            or not math.isfinite(float(qty))
            or float(qty) <= 0.0
            or float(qty) > held_quantity + 1e-9
            or not math.isfinite(float(price))
            or float(price) <= 0.0
        ):
            raise RuntimeError("IBKR live sell exceeds approved position or has invalid pricing")
        if allocation_plan.account_new_risk_buy_blocked:
            from quant_platform_kit.risk.account_new_risk_gate import is_proven_risk_reducing_order

            if not is_proven_risk_reducing_order(held_quantity, "sell", qty, pending_quantity=0.0):
                execution_summary["orders_skipped"].append(
                    {"symbol": symbol, "side": "sell", "reason": "risk_reduction_unproven"}
                )
                continue
        if dry_run_only:
            execution_summary["orders_submitted"].append(
                {"symbol": symbol, "side": "sell", "quantity": qty, "status": "dry_run"}
            )
            trade_logs.append(f"DRY_RUN sell {symbol} {format_quantity(qty)}")
            dry_run_sale_proceeds += float(qty) * float(price)
            continue
        report = claimed_submitter(
            ib,
            order_intent_cls(
                symbol=symbol,
                side="sell",
                quantity=qty,
                account_id=allocation_plan.order_account_id,
            ),
            **(
                {"reduction_position": held_quantity, "reduction_pending_quantity": 0.0}
                if allocation_plan.account_new_risk_buy_blocked
                else {}
            ),
        )
        outcome = _record_submitted_equity_order(
            execution_summary,
            trade_logs,
            report,
            symbol=symbol,
            side="sell",
            quantity=qty,
            order_account_id=allocation_plan.order_account_id,
            translator=translator,
        )
        if outcome == "unknown":
            return sell_executed, projected_sell_release_value, dry_run_sale_proceeds, pending_sell_release_symbols, True
        if outcome != "failed":
            sell_executed = True
            projected_sell_release_value += _projected_sell_release_value_for_report(
                report,
                fallback_price=price,
                fallback_quantity=qty,
            )

    return sell_executed, projected_sell_release_value, dry_run_sale_proceeds, pending_sell_release_symbols, False


def _refresh_post_sell_buying_power(
    ib,
    *,
    account_values,
    allocation_plan: _RebalanceAllocationPlan,
    anticipated_buying_power: float,
    sell_executed: bool,
    dry_run_sale_proceeds: float,
    options: RebalanceExecutionOptions,
) -> float:
    if options.cash_only_execution:
        # A sale fill or ACK does not establish settlement. A fresh observation
        # supplies the budget; projected proceeds never supply spending authority.
        observed = _settled_cash_for_currency(
            ib,
            currency=options.market_currency,
            account_ids=allocation_plan.normalized_account_ids,
        )
        if observed is None:
            allocation_plan.execution_summary["settled_cash_ledger_status"] = "UNKNOWN"
        return max(0.0, observed or 0.0)
    if options.dry_run_only:
        return max(0.0, anticipated_buying_power + dry_run_sale_proceeds)
    if sell_executed:
        time.sleep(options.sell_settle_delay_sec)
        return get_available_buying_power(
            ib,
            account_values.get("buying_power", 0),
            account_ids=allocation_plan.normalized_account_ids,
            currency=options.market_currency,
            cash_only_execution=False,
        )
    return anticipated_buying_power


def _resolve_buy_block_reason(
    ib,
    *,
    signal_metadata: dict[str, Any],
    allocation_plan: _RebalanceAllocationPlan,
    current_mv: dict[str, float],
    target_mv: dict[str, float],
    prices: dict[str, float],
    investable_buying_power: float,
    pending_sell_release_symbols: list[str],
    buy_needed_symbols: list[str],
    trade_logs: list[str],
    translator,
    options: RebalanceExecutionOptions,
) -> str | None:
    execution_summary = allocation_plan.execution_summary
    if bool(signal_metadata.get("small_account_warning")) and buy_needed_symbols:
        reason = "small_account_below_recommended_equity"
        execution_summary["small_account_buy_blocked"] = True
        execution_summary["small_account_buy_block_reason"] = reason
        execution_summary["skipped_reasons"].append(reason)
        trade_logs.append(
            translator(
                "buy_deferred_small_account",
                portfolio_equity=f"{float(allocation_plan.equity or 0.0):,.2f}",
                min_recommended_equity=f"{float(signal_metadata.get('min_recommended_equity_usd') or 0.0):,.2f}",
            )
        )
        return reason
    if allocation_plan.account_new_risk_buy_blocked and buy_needed_symbols:
        reason = "account_new_risk_gate"
        execution_summary["account_new_risk_buy_blocked"] = True
        execution_summary["account_new_risk_block_reason"] = reason
        execution_summary["account_new_risk_reason_codes"] = list(allocation_plan.account_new_risk_reason_codes)
        execution_summary["skipped_reasons"].append(reason)
        reason_text = ", ".join(allocation_plan.account_new_risk_reason_codes) or "NEW_RISK_PROHIBITED"
        trade_logs.append(
            translator(
                "buy_deferred",
                detail=f"[Account new-risk gate] {reason_text}",
            )
        )
        return reason
    if options.cash_only_execution and pending_sell_release_symbols and buy_needed_symbols:
        if _rotation_guard_should_block_buys(
            pending_sell_release_symbols=pending_sell_release_symbols,
            buy_needed_symbols=buy_needed_symbols,
            current_mv=current_mv,
            target_mv=target_mv,
            threshold=allocation_plan.threshold,
            investable_buying_power=investable_buying_power,
            prices=prices,
            limit_buy_premium=options.limit_buy_premium,
            limit_buy_premium_by_symbol=options.limit_buy_premium_by_symbol,
            quantity_step=allocation_plan.order_quantity_step,
            minimum_order_notional=allocation_plan.minimum_order_notional,
        ):
            release_symbols = _format_symbol_preview(tuple(pending_sell_release_symbols))
            execution_summary["pending_sell_release_symbols"] = list(pending_sell_release_symbols)
            execution_summary["skipped_reasons"].append(
                f"pending_sell_release:{','.join(pending_sell_release_symbols)}"
            )
            trade_logs.append(
                translator(
                    "buy_deferred_pending_sell_release",
                    symbols=release_symbols,
                )
            )
            return "pending_sell_release"
    if options.cash_only_execution:
        raw_cash = _cash_value_for_currency(
            list(ib.accountValues() or ()),
            currency=options.market_currency,
            account_ids=allocation_plan.normalized_account_ids,
        )
        if raw_cash is not None and float(raw_cash) < 0.0 and buy_needed_symbols:
            execution_summary["skipped_reasons"].append(f"negative_cash:{float(raw_cash):.2f}")
            trade_logs.append(
                translator(
                    "buy_deferred_negative_cash",
                    cash=f"{float(raw_cash):,.2f}",
                )
            )
            return "negative_cash"
    return None


def _execute_buy_orders(
    ib,
    *,
    positions,
    allocation_plan: _RebalanceAllocationPlan,
    current_mv: dict[str, float],
    target_mv: dict[str, float],
    prices: dict[str, float],
    investable_buying_power: float,
    buy_needed_symbols: list[str],
    buys_blocked_reason: str | None,
    claimed_submitter,
    order_intent_cls,
    translator,
    trade_logs: list[str],
    options: RebalanceExecutionOptions,
) -> bool:
    execution_summary = allocation_plan.execution_summary
    threshold = allocation_plan.threshold
    for symbol, target in target_mv.items():
        if buys_blocked_reason:
            if symbol in buy_needed_symbols:
                execution_summary["orders_skipped"].append(
                    {"symbol": symbol, "side": "buy", "reason": buys_blocked_reason}
                )
            continue
        current = current_mv.get(symbol, 0)
        if current >= target - threshold:
            continue
        refresh_started = 0.0
        if options.cash_only_execution:
            refresh_started = time.monotonic()
            # A previous order may take longer than the freshness bound.
            # Refresh each candidate without resetting the shared budget.
            observed = _settled_cash_for_currency(
                ib,
                currency=options.market_currency,
                account_ids=allocation_plan.normalized_account_ids,
            )
            investable_buying_power = min(
                investable_buying_power,
                _investable_buying_power(max(0.0, observed or 0.0), allocation_plan.reserved),
            )
            if observed is None or observed <= 0:
                reason = "settled_cash_ledger_unverified" if observed is None else "insufficient_settled_cash"
                if observed is None:
                    execution_summary["settled_cash_ledger_status"] = "UNKNOWN"
                execution_summary["orders_skipped"].append({"symbol": symbol, "side": "buy", "reason": reason})
                execution_summary["skipped_reasons"].append(f"{reason}:{symbol}")
                continue
        buy_value = min(target - current, investable_buying_power * 0.95)
        price = prices.get(symbol)
        if not price:
            execution_summary["orders_skipped"].append({"symbol": symbol, "side": "buy", "reason": "missing_price"})
            execution_summary["skipped_reasons"].append(f"missing_price:{symbol}")
            continue
        if buy_value < allocation_plan.minimum_order_notional:
            execution_summary["orders_skipped"].append({"symbol": symbol, "side": "buy", "reason": "min_notional"})
            continue

        limit_price = _limit_buy_price(
            symbol,
            price,
            options.limit_buy_premium,
            options.limit_buy_premium_by_symbol,
        )
        held_quantity = max(0.0, float(positions.get(symbol, {}).get("quantity", 0.0) or 0.0))
        qty, _forced_whole_share = _planned_buy_order_quantity(
            symbol,
            buy_value=buy_value,
            limit_price=limit_price,
            quantity_step=allocation_plan.order_quantity_step,
            investable_buying_power=investable_buying_power,
            held_quantity=held_quantity,
        )
        if qty <= 0:
            execution_summary["orders_skipped"].append({"symbol": symbol, "side": "buy", "reason": "quantity_zero"})
            continue

        order_cost = float(qty) * float(limit_price)
        if order_cost > investable_buying_power:
            qty = _floor_order_quantity(
                investable_buying_power / limit_price,
                quantity_step=allocation_plan.order_quantity_step,
            )
            order_cost = float(qty) * float(limit_price)
        if qty <= 0 or order_cost > investable_buying_power:
            execution_summary["orders_skipped"].append(
                {"symbol": symbol, "side": "buy", "reason": "insufficient_buying_power"}
            )
            execution_summary["skipped_reasons"].append(f"insufficient_buying_power:{symbol}")
            continue

        remaining_approved = allocation_plan.approved_target_caps[symbol] - float(current_mv.get(symbol, 0.0) or 0.0)
        if (
            symbol not in allocation_plan.strategy_symbols
            or not math.isfinite(float(qty))
            or not math.isfinite(float(limit_price))
            or float(qty) <= 0.0
            or float(limit_price) <= 0.0
            or order_cost > remaining_approved + 1e-6
        ):
            raise RuntimeError("IBKR live buy exceeds the approved RiskEngine order limit")
        if options.cash_only_execution and not 0 <= time.monotonic() - refresh_started <= SETTLED_CASH_MAX_AGE_SECONDS:
            execution_summary["settled_cash_ledger_status"] = "UNKNOWN"
            execution_summary["orders_skipped"].append({"symbol": symbol, "side": "buy", "reason": "settled_cash_stale"})
            execution_summary["skipped_reasons"].append(f"settled_cash_stale:{symbol}")
            continue
        if options.dry_run_only:
            execution_summary["orders_submitted"].append(
                {
                    "symbol": symbol,
                    "side": "buy",
                    "quantity": qty,
                    "limit_price": limit_price,
                    "status": "dry_run",
                }
            )
            trade_logs.append(f"DRY_RUN buy {symbol} {format_quantity(qty)} @{limit_price:.2f}")
            investable_buying_power -= qty * limit_price
            continue
        intent = order_intent_cls(
            symbol=symbol,
            side="buy",
            quantity=qty,
            order_type="limit",
            limit_price=limit_price,
            time_in_force="DAY",
            account_id=allocation_plan.order_account_id,
        )
        if options.cash_only_execution:
            with _SettledCashDeadlineIB(
                ib,
                refresh_started=refresh_started,
                account_id=allocation_plan.order_account_id,
                currency=options.market_currency,
                buying_power=investable_buying_power,
                reserved=allocation_plan.reserved,
                quantity_step=allocation_plan.order_quantity_step,
                minimum_order_notional=allocation_plan.minimum_order_notional,
            ) as settled_ib:
                report = claimed_submitter(settled_ib, intent)
                investable_buying_power = min(investable_buying_power, settled_ib.remaining_buying_power)
                if settled_ib.prepared_quantity is not None:
                    qty = settled_ib.prepared_quantity
        else:
            report = claimed_submitter(ib, intent)
        outcome = _record_submitted_equity_order(
            execution_summary,
            trade_logs,
            report,
            symbol=symbol,
            side="buy",
            quantity=qty,
            order_account_id=allocation_plan.order_account_id,
            translator=translator,
            limit_price=limit_price,
        )
        if outcome == "unknown":
            return True
        if outcome != "failed":
            investable_buying_power -= qty * limit_price
    return False


def _execute_rebalance_orders(
    ib,
    *,
    positions,
    account_values,
    signal_metadata: dict[str, Any],
    allocation_plan: _RebalanceAllocationPlan,
    quote_collection: _RebalanceQuoteCollection,
    claimed_submitter,
    order_intent_cls,
    translator,
    options: RebalanceExecutionOptions,
) -> _RebalanceOrderOutcome:
    """Apply small-account compatibility, run broker safety checks, and execute orders."""
    execution_summary = allocation_plan.execution_summary
    _record_quote_collection_summary(execution_summary, quote_collection)
    prices = quote_collection.prices
    current_mv, _current_quantities, target_mv, trade_logs, target_hash, drift_context = (
        _prepare_post_quote_targets(
            positions=positions,
            account_values=account_values,
            allocation_plan=allocation_plan,
            prices=prices,
            translator=translator,
        )
    )
    if options.paper_execution_admission_enabled:
        paper_admission = evaluate_ibkr_paper_execution_admission(
            signal_metadata=signal_metadata,
            strategy_profile=options.strategy_profile,
            account_scope=options.account_group,
            positions=positions,
            prices=prices,
            target_market_values=target_mv,
            option_order_intents=allocation_plan.option_order_intents,
            runtime_release_receipt=options.runtime_release_receipt,
            expected_strategy_release=options.expected_strategy_release,
        )
        execution_summary["paper_execution_admission"] = paper_admission
        if not paper_admission["broker_write_allowed"]:
            _record_blocked_step(
                execution_summary,
                trade_logs,
                reason="paper_execution_admission_blocked",
                translator=translator,
            )
            return _RebalanceOrderOutcome(trade_logs=trade_logs, finalized_early=True)

    if allocation_plan.equity > 0:
        safe_haven_symbol = allocation_plan.safe_haven_symbol
        current_safe_haven_mv = current_mv.get(safe_haven_symbol, 0.0) if safe_haven_symbol else 0.0
        execution_summary["current_safe_haven_weight"] = float(current_safe_haven_mv / allocation_plan.equity)
        execution_summary["current_stock_weight"] = float(
            (sum(current_mv.values()) - current_safe_haven_mv) / allocation_plan.equity
        )

    try:
        pending_observation = _observe_pending_orders(
            ib,
            set(allocation_plan.all_symbols) | set(allocation_plan.option_underliers),
            account_ids=allocation_plan.normalized_account_ids,
        )
    except _PendingOrdersUnavailableError:
        _record_blocked_step(
            execution_summary,
            trade_logs,
            reason="pending_orders_unavailable",
            translator=translator,
        )
        return _RebalanceOrderOutcome(trade_logs=trade_logs, finalized_early=True)
    if pending_observation.symbols:
        pending_symbols_csv = ",".join(pending_observation.symbols)
        _record_blocked_step(
            execution_summary,
            trade_logs,
            reason=f"pending_orders_detected:{pending_symbols_csv}",
            translator=translator,
            log_message=translator(
                "pending_orders_detected",
                profile=_display_text(options.strategy_profile, fallback="<unknown>"),
                symbols=pending_symbols_csv,
            ),
        )
        return _RebalanceOrderOutcome(trade_logs=trade_logs, finalized_early=True)
    if pending_observation.cash_risk_unknown:
        # Reuse the existing BUY gate, without vetoing otherwise-qualified
        # reductions. The normal claim, lock and sell limits still apply.
        allocation_plan.account_new_risk_buy_blocked = True
        allocation_plan.account_new_risk_reason_codes = (
            *allocation_plan.account_new_risk_reason_codes,
            "PENDING_ORDER_CASH_RISK_UNKNOWN",
        )
        execution_summary["pending_order_cash_risk_unknown"] = True

    settled_cash = (
        _settled_cash_for_currency(
            ib,
            currency=options.market_currency,
            account_ids=allocation_plan.normalized_account_ids,
        )
        if options.cash_only_execution
        else None
    )
    settled_cash_ledger_unverified = options.cash_only_execution and settled_cash is None
    if settled_cash_ledger_unverified:
        allocation_plan.account_new_risk_buy_blocked = True
        allocation_plan.account_new_risk_reason_codes = (
            *allocation_plan.account_new_risk_reason_codes,
            "SETTLED_CASH_LEDGER_UNVERIFIED",
        )
        execution_summary["settled_cash_ledger_status"] = "UNKNOWN"
    elif options.cash_only_execution:
        execution_summary["settled_cash_ledger_status"] = "OBSERVED"

    _append_execution_detail_logs(
        trade_logs,
        allocation_plan=allocation_plan,
        quote_collection=quote_collection,
        current_mv=current_mv,
        signal_metadata=signal_metadata,
        translator=translator,
        dry_run_only=options.dry_run_only,
    )

    anticipated_buying_power = (
        max(0.0, settled_cash or 0.0)
        if options.cash_only_execution
        else get_available_buying_power(
            ib,
            account_values.get("buying_power", 0),
            account_ids=allocation_plan.normalized_account_ids,
            currency=options.market_currency,
            cash_only_execution=False,
        )
    )
    investable_anticipated_buying_power = _investable_buying_power(
        anticipated_buying_power,
        allocation_plan.reserved,
    )
    cash_sweep_quantity, plan_ended_early = _plan_rebalance_directions(
        positions=positions,
        allocation_plan=allocation_plan,
        current_mv=current_mv,
        target_mv=target_mv,
        prices=prices,
        investable_anticipated_buying_power=investable_anticipated_buying_power,
        pending_cash_risk_unknown=pending_observation.cash_risk_unknown,
        settled_cash_ledger_unverified=settled_cash_ledger_unverified,
        execution_summary=execution_summary,
        trade_logs=trade_logs,
        translator=translator,
        options=options,
        drift_context=drift_context,
    )
    if plan_ended_early:
        return _RebalanceOrderOutcome(trade_logs=trade_logs, finalized_early=True)

    same_day_filled_symbols = _collect_same_day_filled_symbols(
        ib,
        set(allocation_plan.all_symbols) | set(allocation_plan.option_underliers),
        allocation_plan.trade_date,
        account_ids=allocation_plan.normalized_account_ids,
    )
    if same_day_filled_symbols:
        filled_csv = ",".join(same_day_filled_symbols)
        _record_blocked_step(
            execution_summary,
            trade_logs,
            reason=f"same_day_fills_detected:{filled_csv}",
            translator=translator,
            log_message=translator(
                "same_day_fills_detected",
                profile=_display_text(options.strategy_profile, fallback="<unknown>"),
                mode=allocation_plan.resolved_execution_mode,
                symbols=filled_csv,
                trade_date=_display_text(allocation_plan.trade_date, fallback="<none>"),
            ),
        )
        return _RebalanceOrderOutcome(trade_logs=trade_logs, finalized_early=True)

    if not _acquire_cycle_execution_lock(
        allocation_plan=allocation_plan,
        target_hash=target_hash,
        trade_logs=trade_logs,
        translator=translator,
        options=options,
    ):
        return _RebalanceOrderOutcome(trade_logs=trade_logs, finalized_early=True)

    (
        sell_executed,
        projected_sell_release_value,
        dry_run_sale_proceeds,
        pending_sell_release_symbols,
        sell_unknown,
    ) = _execute_sell_orders(
        ib,
        positions=positions,
        allocation_plan=allocation_plan,
        current_mv=current_mv,
        target_mv=target_mv,
        prices=prices,
        cash_sweep_quantity=cash_sweep_quantity,
        claimed_submitter=claimed_submitter,
        order_intent_cls=order_intent_cls,
        translator=translator,
        trade_logs=trade_logs,
        dry_run_only=options.dry_run_only,
    )
    if sell_unknown:
        return _RebalanceOrderOutcome(trade_logs=trade_logs, finalized_early=True)

    buying_power = _refresh_post_sell_buying_power(
        ib,
        account_values=account_values,
        allocation_plan=allocation_plan,
        anticipated_buying_power=anticipated_buying_power,
        sell_executed=sell_executed,
        dry_run_sale_proceeds=dry_run_sale_proceeds,
        options=options,
    )
    investable_buying_power = _investable_buying_power(buying_power, allocation_plan.reserved)
    execution_summary["projected_sell_release_value"] = float(projected_sell_release_value)
    deduped_pending_releases = list(dict.fromkeys(pending_sell_release_symbols))
    buy_needed_symbols = [
        symbol
        for symbol, target in target_mv.items()
        if current_mv.get(symbol, 0.0) < float(target or 0.0) - allocation_plan.threshold
    ]
    buys_blocked_reason = _resolve_buy_block_reason(
        ib,
        signal_metadata=signal_metadata,
        allocation_plan=allocation_plan,
        current_mv=current_mv,
        target_mv=target_mv,
        prices=prices,
        investable_buying_power=investable_buying_power,
        pending_sell_release_symbols=deduped_pending_releases,
        buy_needed_symbols=buy_needed_symbols,
        trade_logs=trade_logs,
        translator=translator,
        options=options,
    )
    buy_unknown = _execute_buy_orders(
        ib,
        positions=positions,
        allocation_plan=allocation_plan,
        current_mv=current_mv,
        target_mv=target_mv,
        prices=prices,
        investable_buying_power=investable_buying_power,
        buy_needed_symbols=buy_needed_symbols,
        buys_blocked_reason=buys_blocked_reason,
        claimed_submitter=claimed_submitter,
        order_intent_cls=order_intent_cls,
        translator=translator,
        trade_logs=trade_logs,
        options=options,
    )
    if buy_unknown:
        return _RebalanceOrderOutcome(trade_logs=trade_logs, finalized_early=True)

    buying_power = _execute_option_order_intents(
        ib,
        allocation_plan.option_order_intents,
        submit_order_intent=claimed_submitter,
        order_intent_cls=order_intent_cls,
        translator=translator,
        execution_summary=execution_summary,
        trade_logs=trade_logs,
        dry_run_only=options.dry_run_only,
        order_account_id=allocation_plan.order_account_id,
        buying_power=buying_power,
    )
    return _RebalanceOrderOutcome(
        trade_logs=trade_logs,
        buying_power=buying_power,
        finalized_early=False,
        drift_context=drift_context,
    )


def _build_rebalance_summary(
    execution_summary: dict[str, Any],
    *,
    trade_logs: list[str],
    translator,
    buying_power: float,
    drift_context: _DriftNotesContext | None = None,
) -> dict[str, Any]:
    """Finalize rebalance execution status, residual cash estimate, and drift notes."""
    has_terminal_order = bool(
        execution_summary["orders_submitted"]
        or execution_summary["orders_filled"]
        or execution_summary["orders_partially_filled"]
        or execution_summary["option_orders_submitted"]
        or execution_summary["option_orders_filled"]
        or execution_summary["option_orders_partially_filled"]
    )
    has_pending_order = bool(
        execution_summary["orders_pending"] or execution_summary["option_orders_pending"]
    )
    submission_failure = next(
        (
            reason
            for reason in execution_summary["skipped_reasons"]
            if reason.startswith(("submit_failed:", "option_submit_failed:"))
        ),
        None,
    )
    if has_pending_order:
        execution_summary["execution_status"] = "pending_reconciliation"
        execution_summary["no_op_reason"] = "broker_order_pending_confirmation"
    elif has_terminal_order:
        execution_summary["execution_status"] = "executed"
        execution_summary["no_op_reason"] = None
    elif submission_failure:
        execution_summary["execution_status"] = "blocked"
        execution_summary["no_op_reason"] = submission_failure
        trade_logs.append(translator("failed", reason=submission_failure))
    else:
        execution_summary["execution_status"] = "no_op"
    execution_summary["residual_cash_estimate"] = float(max(buying_power, 0.0))
    if drift_context is not None:
        _append_small_account_allocation_drift_notes(
            execution_summary,
            trade_logs,
            translator=translator,
            context=drift_context,
        )
    return execution_summary


def execute_rebalance(
    ib,
    target_weights,
    positions,
    account_values,
    *,
    fetch_quote_snapshots,
    submit_order_intent,
    order_intent_cls,
    translator,
    strategy_symbols=None,
    signal_metadata=None,
    acquire_execution_claim=None,
    account_execution_session=None,
    strategy_profile=_UNSET,
    account_group=_UNSET,
    service_name=_UNSET,
    account_ids=_UNSET,
    dry_run_only=_UNSET,
    execution_mode=_UNSET,
    cash_reserve_ratio=_UNSET,
    rebalance_threshold_ratio=_UNSET,
    limit_buy_premium=_UNSET,
    sell_settle_delay_sec=_UNSET,
    cash_reserve_floor_usd=_UNSET,
    quantity_step=_UNSET,
    min_order_notional=_UNSET,
    safe_haven_cash_substitute_threshold_usd=_UNSET,
    market_currency=_UNSET,
    limit_buy_premium_by_symbol=_UNSET,
    execution_lock_dir=_UNSET,
    return_summary=_UNSET,
    cash_only_execution=_UNSET,
    paper_execution_admission_enabled=_UNSET,
    runtime_release_receipt=_UNSET,
    expected_strategy_release=_UNSET,
    options: RebalanceExecutionOptions | None = None,
):
    """Execute trades to reach target weights."""
    del target_weights, strategy_symbols
    resolved_options = _resolve_rebalance_options(
        options,
        strategy_profile=strategy_profile,
        account_group=account_group,
        service_name=service_name,
        account_ids=account_ids,
        dry_run_only=dry_run_only,
        execution_mode=execution_mode,
        cash_reserve_ratio=cash_reserve_ratio,
        rebalance_threshold_ratio=rebalance_threshold_ratio,
        limit_buy_premium=limit_buy_premium,
        sell_settle_delay_sec=sell_settle_delay_sec,
        cash_reserve_floor_usd=cash_reserve_floor_usd,
        quantity_step=quantity_step,
        min_order_notional=min_order_notional,
        safe_haven_cash_substitute_threshold_usd=safe_haven_cash_substitute_threshold_usd,
        market_currency=market_currency,
        limit_buy_premium_by_symbol=limit_buy_premium_by_symbol,
        execution_lock_dir=execution_lock_dir,
        return_summary=return_summary,
        cash_only_execution=cash_only_execution,
        paper_execution_admission_enabled=paper_execution_admission_enabled,
        runtime_release_receipt=runtime_release_receipt,
        expected_strategy_release=expected_strategy_release,
    )
    normalized_signal_metadata = signal_metadata or {}
    claimed_submitter = _build_claimed_submitter(
        submit_order_intent=submit_order_intent,
        account_values=account_values,
        signal_metadata=normalized_signal_metadata,
        acquire_execution_claim=acquire_execution_claim,
        account_execution_session=account_execution_session,
        options=resolved_options,
    )
    allocation_plan = _resolve_rebalance_allocation(
        positions=positions,
        account_values=account_values,
        signal_metadata=normalized_signal_metadata,
        account_execution_session=account_execution_session,
        translator=translator,
        options=resolved_options,
    )
    if allocation_plan.early_logs is not None:
        return _finalize_result(
            allocation_plan.early_logs,
            allocation_plan.execution_summary,
            return_summary=resolved_options.return_summary,
        )
    quote_collection = _collect_rebalance_quotes(
        ib,
        allocation_plan.all_symbols,
        signal_metadata=normalized_signal_metadata,
        fetch_quote_snapshots=fetch_quote_snapshots,
        dry_run_only=resolved_options.dry_run_only,
    )
    order_outcome = _execute_rebalance_orders(
        ib,
        positions=positions,
        account_values=account_values,
        signal_metadata=normalized_signal_metadata,
        allocation_plan=allocation_plan,
        quote_collection=quote_collection,
        claimed_submitter=claimed_submitter,
        order_intent_cls=order_intent_cls,
        translator=translator,
        options=resolved_options,
    )
    if order_outcome.finalized_early:
        return _finalize_result(
            order_outcome.trade_logs,
            allocation_plan.execution_summary,
            return_summary=resolved_options.return_summary,
        )
    summary = _build_rebalance_summary(
        allocation_plan.execution_summary,
        trade_logs=order_outcome.trade_logs,
        translator=translator,
        buying_power=order_outcome.buying_power,
        drift_context=order_outcome.drift_context,
    )
    return _finalize_result(
        order_outcome.trade_logs,
        summary,
        return_summary=resolved_options.return_summary,
    )
