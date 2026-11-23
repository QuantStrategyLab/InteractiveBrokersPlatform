"""Account state, quotes, settled cash, and pending-order helpers."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from interactive_brokers_platform.application.broker_reconciliation import (
    IBKRReconciliationReadError,
    load_read_only_open_trades,
)
from quant_platform_kit.common.models import QuoteSnapshot
from quant_platform_kit.common.quantity import (
    floor_to_quantity_step,
    format_quantity,
    normalize_order_quantity,
)


def _facade_attr(name: str, default: Any) -> Any:
    import sys
    mod = sys.modules.get("interactive_brokers_platform.application.execution_service")
    return getattr(mod, name, default) if mod is not None else default

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


def _floor_order_quantity(quantity, *, quantity_step):
    return normalize_order_quantity(floor_to_quantity_step(quantity, quantity_step))



def _investable_buying_power(buying_power: float, reserved_cash: float) -> float:
    return max(0.0, float(buying_power or 0.0) - max(0.0, float(reserved_cash or 0.0)))



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
        now = _facade_attr('datetime', datetime).now(timezone.utc)
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
        now = _facade_attr('datetime', datetime).now(timezone.utc)
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

