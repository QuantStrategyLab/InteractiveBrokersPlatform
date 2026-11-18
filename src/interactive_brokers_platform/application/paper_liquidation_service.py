"""Paper-account liquidation helper for controlled strategy switch rehearsals."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any


ACCEPTED_ORDER_STATUSES = {
    "PendingSubmit",
    "ApiPending",
    "ApiPendingSubmit",
    "Submitted",
    "PreSubmitted",
    "Filled",
    "PartiallyFilled",
    "Partial",
}


def _position_symbol(position: Any, *, fallback: str | None = None) -> str:
    if isinstance(position, dict):
        return str(position.get("symbol") or fallback or "").strip().upper()
    return str(getattr(position, "symbol", fallback or "") or "").strip().upper()


def _position_quantity(position: Any) -> float:
    if isinstance(position, dict):
        return float(position.get("quantity", position.get("position", 0.0)) or 0.0)
    return float(getattr(position, "quantity", getattr(position, "position", 0.0)) or 0.0)


def build_liquidation_intents(positions, *, order_intent_cls) -> tuple[Any, ...]:
    intents = []
    iterable = (
        positions.items()
        if isinstance(positions, dict)
        else ((None, position) for position in positions or ())
    )
    for symbol_hint, position in iterable:
        symbol = _position_symbol(position, fallback=symbol_hint)
        quantity = _position_quantity(position)
        if not symbol or quantity == 0:
            continue
        side = "sell" if quantity > 0 else "buy"
        intents.append(
            order_intent_cls(
                symbol=symbol,
                side=side,
                quantity=abs(quantity),
            )
        )
    return tuple(intents)


def _report_to_dict(report: Any) -> dict[str, Any]:
    if is_dataclass(report):
        return asdict(report)
    return {
        "symbol": getattr(report, "symbol", None),
        "side": getattr(report, "side", None),
        "quantity": getattr(report, "quantity", None),
        "status": getattr(report, "status", None),
        "broker_order_id": getattr(report, "broker_order_id", None),
    }


def execute_paper_liquidation(
    ib,
    positions,
    *,
    submit_order_intent,
    order_intent_cls,
    dry_run_only: bool,
) -> dict[str, Any]:
    intents = build_liquidation_intents(positions, order_intent_cls=order_intent_cls)
    summary: dict[str, Any] = {
        "mode": "dry_run" if dry_run_only else "paper",
        "positions_seen": len(positions or ()),
        "orders_submitted": [],
        "orders_skipped": [],
        "execution_status": "no_op" if not intents else "dry_run" if dry_run_only else "executing",
    }
    if not intents:
        return summary

    if dry_run_only:
        summary["orders_submitted"] = [
            {
                "symbol": intent.symbol,
                "side": intent.side,
                "quantity": intent.quantity,
                "status": "dry_run",
            }
            for intent in intents
        ]
        return summary

    for intent in intents:
        report = submit_order_intent(ib, intent)
        payload = _report_to_dict(report)
        status = str(payload.get("status") or "")
        if status in ACCEPTED_ORDER_STATUSES:
            summary["orders_submitted"].append(payload)
        else:
            summary["orders_skipped"].append({**payload, "reason": status or "submit_failed"})
    summary["execution_status"] = "submitted" if summary["orders_submitted"] else "blocked"
    return summary
