"""IBKR order submission adapters for platform-specific broker quirks."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from quant_platform_kit.common.models import ExecutionReport, OrderIntent
from quant_platform_kit.ibkr.execution import submit_order_intent as _submit_order_intent

DEFAULT_TIME_IN_FORCE = "DAY"


def _intent_with_default_time_in_force(order_intent: OrderIntent) -> OrderIntent:
    if order_intent.time_in_force:
        return order_intent
    return replace(order_intent, time_in_force=DEFAULT_TIME_IN_FORCE)


def _market_order_factory_with_time_in_force(
    market_order_factory: Callable[..., Any] | None,
    *,
    time_in_force: str,
) -> Callable[..., Any]:
    def factory(side: str, quantity: float) -> Any:
        factory_impl = market_order_factory
        if factory_impl is None:
            from ib_insync import MarketOrder

            factory_impl = MarketOrder
        order = factory_impl(side, quantity)
        order.tif = time_in_force
        return order

    return factory


def submit_order_intent(
    ib: Any,
    order_intent: OrderIntent,
    *,
    wait_seconds: float = 1.0,
    stock_factory: Callable[..., Any] | None = None,
    market_order_factory: Callable[..., Any] | None = None,
    limit_order_factory: Callable[..., Any] | None = None,
) -> ExecutionReport:
    """Submit an IBKR order with explicit TIF to avoid account-preset rejections."""

    intent = _intent_with_default_time_in_force(order_intent)
    return _submit_order_intent(
        ib,
        intent,
        wait_seconds=wait_seconds,
        stock_factory=stock_factory,
        market_order_factory=_market_order_factory_with_time_in_force(
            market_order_factory,
            time_in_force=intent.time_in_force or DEFAULT_TIME_IN_FORCE,
        ),
        limit_order_factory=limit_order_factory,
    )
