"""Translate a stop rejection into a scoped, sell-only protective exit.

This is separate from ordinary RiskEngine approval. The existing account claim,
journal grant and final submit wrapper remain mandatory for every submission.
"""
from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import math
from zoneinfo import ZoneInfo


_STOP_PROFILES = frozenset({"tqqq_growth_income", "soxl_soxx_trend_income"})


def stop_submission_identity(metadata):
    """Use event identity for claims/digests, preserving source timestamps as facts."""
    day = metadata["protective_exit"]["trade_date"]
    return {**metadata, "trade_date": day, "signal_date": day, "effective_date": day,
            "snapshot_as_of": None,
            "execution_timing_contract": "protective_stop:" + metadata["stop_event_id"]}


def stop_exit_projection(decision, *, profile, runtime_metadata):
    """Return an explicit zero allocation only for the exact native stop code."""
    diagnostics = dict(decision.diagnostics)
    if (profile not in _STOP_PROFILES or decision.positions or decision.budgets
            or decision.risk_flags != ("rejected:stop_loss",)
            or diagnostics.get("risk_gate") != "REJECT"):
        return None
    pnl = diagnostics.get("unrealized_pnl_pct")
    symbols = runtime_metadata.get("managed_symbols")
    accounts = tuple(runtime_metadata.get("account_ids") or ())
    trade_date = runtime_metadata.get("trade_date")
    if (type(pnl) not in (int, float) or not math.isfinite(pnl) or pnl >= 0
            or not isinstance(symbols, (tuple, list)) or not symbols
            or any(not isinstance(s, str) or not s or s != s.strip().upper() for s in symbols)
            or len(set(symbols)) != len(symbols) or len(accounts) != 1 or not accounts[0]
            or runtime_metadata.get("snapshot_guard_decision") == "fail_closed"):
        return None
    try:
        date.fromisoformat(trade_date)
    except (TypeError, ValueError):
        return None
    descriptor = {"strategy_profile": profile, "account_ids": accounts,
                  "trade_date": trade_date, "symbols": tuple(sorted(symbols))}
    # One stop event per account/profile/day/scope; changing a mark cannot retry it.
    event = hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()
    metadata = {**runtime_metadata, **diagnostics, "strategy_profile": profile,
                "risk_flags": ("rejected:stop_loss", "no_execute"), "actionable": True,
                "stop_event_id": event, "protective_exit": descriptor,
                "execution_timing_contract": "protective_stop:" + event,
                "allocation": {"target_mode": "weight", "strategy_symbols": descriptor["symbols"],
                               "risk_symbols": descriptor["symbols"], "income_symbols": (),
                               "safe_haven_symbols": (), "targets": dict.fromkeys(descriptor["symbols"], 0.)}}
    metadata.pop("risk_authority", None)
    metadata.pop("option_order_intents", None)
    return dict(metadata["allocation"]["targets"]), metadata


def execute_stop_exit(ib, *, metadata, allocation, positions, profile, account_ids,
                      session, submit, order_intent_cls, observe_pending, dry_run, admit=None):
    """Sell confirmed long quantities, without inferring completion from an ACK."""
    from quant_platform_kit.common.execution_state import AccountSubmissionSession
    from quant_platform_kit.common.strategy_contracts import StrategyDecision

    expected = stop_exit_projection(StrategyDecision(risk_flags=("rejected:stop_loss",),
        diagnostics={"risk_gate": metadata.get("risk_gate"),
                     "unrealized_pnl_pct": metadata.get("unrealized_pnl_pct")}),
        profile=profile, runtime_metadata=metadata)
    if (expected is None or metadata.get("risk_flags") != ("rejected:stop_loss", "no_execute")
            or metadata.get("risk_authority") is not None
            or tuple(account_ids or ()) != tuple(metadata["protective_exit"].get("account_ids") or ())
            or metadata.get("protective_exit") != expected[1]["protective_exit"]
            or metadata.get("stop_event_id") != expected[1]["stop_event_id"]
            or metadata.get("execution_timing_contract") != expected[1]["execution_timing_contract"]
            or allocation != {k: v for k, v in expected[1]["allocation"].items() if k != "target_mode"}):
        raise RuntimeError("IBKR protective stop scope or event binding mismatch")
    symbols = tuple(allocation["strategy_symbols"])
    rows = [{"symbol": s, "stop_event_id": metadata["stop_event_id"],
             "remaining_quantity": None, "final_fill_outcome": "UNKNOWN"} for s in symbols]
    summary = {"stop_event_id": metadata["stop_event_id"], "protective_exits": rows,
               "execution_status": "blocked", "no_op_reason": None, "outcome_unknown": True,
               "orders_submitted": [], "orders_filled": [], "orders_pending": [],
               "orders_partially_filled": [], "orders_skipped": []}

    def blocked(reason):
        summary["no_op_reason"] = reason
        return [reason], summary

    if metadata["trade_date"] != datetime.now(ZoneInfo("America/New_York")).date().isoformat():
        return blocked("stale_stop_event")
    quantities = {s: positions.get(s, {}).get("quantity", 0.) for s in symbols}
    if any(type(q) not in (int, float) or not math.isfinite(q) or q < 0 for q in quantities.values()):
        return blocked("stop_position_quantity_unproven")
    if dry_run or metadata.get("dry_run_only") is True:
        for row in rows:
            row.update(remaining_quantity=quantities[row["symbol"]], final_fill_outcome="NOT_PROVEN")
        summary.update(execution_status="dry_run_completed", outcome_unknown=False)
        return ["protective_stop_preview"], summary
    if admit is not None:
        admission = admit()
        summary["paper_execution_admission"] = admission
        if not admission["broker_write_allowed"]:
            return blocked("paper_execution_admission_blocked")
    if not isinstance(session, AccountSubmissionSession):
        return blocked("stop_account_session_unavailable")
    if session.account_id != metadata["protective_exit"]["account_ids"][0]:
        return blocked("stop_account_session_binding_mismatch")
    # Never retry even a terminal rejection with the same event. A restart has
    # no original grant and no authority to resolve or replace an old attempt.
    state = session.read_state()
    if any(a["intent_id"] == session.intent_id for a in state["attempts"].values()):
        return blocked("stop_event_already_attempted_reconciliation_required")
    if any(a["certainty"] in {"PREPARED", "UNKNOWN", "KNOWN_PENDING"} for a in state["attempts"].values()):
        return blocked("stop_prior_submission_unresolved")
    try:
        pending = observe_pending(ib, set(symbols), account_ids=account_ids)
    except RuntimeError:
        return blocked("stop_pending_orders_unavailable")
    if pending.symbols:
        return blocked("stop_pending_orders_detected")
    for row in rows:
        symbol, quantity = row["symbol"], quantities[row["symbol"]]
        if quantity == 0:
            row.update(remaining_quantity=0., final_fill_outcome="PROVEN")
            continue
        intent = order_intent_cls(symbol=symbol, side="sell", quantity=quantity,
            account_id=metadata["protective_exit"]["account_ids"][0], metadata={"stop_event_id": metadata["stop_event_id"],
                                                    "remaining_quantity": quantity})
        before = set(session.read_state()["attempts"])
        order_digest, _ = session._order(intent)
        error = False
        try:
            submit(ib, intent, reduction_position=quantity, reduction_pending_quantity=0.)
        except Exception:
            # The final wrapper persists transport failures as UNKNOWN. Do not
            # print broker exceptions or turn lack of a receipt into zero fill.
            error = True
        after = session.read_state()["attempts"]
        created = set(after) - before
        if len(created) != 1:
            return blocked("stop_submission_not_reserved")
        attempt_id = created.pop()
        attempt = after[attempt_id]
        if (attempt["intent_id"] != session.intent_id or attempt["order_sha256"] != order_digest
                or attempt["quantity"] != quantity or attempt["release_id"] != session.release_id):
            return blocked("stop_submission_observation_binding_mismatch")
        observation = attempt["observation"] or {}
        certainty = attempt["certainty"]
        filled = attempt["filled_quantity"]
        known = certainty in {"TERMINAL", "KNOWN_PENDING"}
        status = observation.get("status", "Unknown")
        row.update(attempt_id=attempt_id, quantity=quantity, status=status,
                   broker_order_id=observation.get("broker_order_id"),
                   confirmed_filled_quantity=filled if known else None,
                   average_fill_price=observation.get("average_fill_price") if known else None,
                   remaining_quantity=quantity - filled if known and (filled > 0 or certainty == "TERMINAL") else None,
                   remaining_quantity_upper_bound=quantity - filled if known else quantity,
                   final_fill_outcome="PROVEN" if certainty == "TERMINAL" else "UNKNOWN")
        summary["orders_submitted"].append(dict(row, side="sell"))
        if certainty == "TERMINAL" and filled == quantity:
            summary["orders_filled"].append(dict(row, side="sell"))
        elif certainty == "KNOWN_PENDING":
            key = "orders_partially_filled" if filled > 0 else "orders_pending"
            summary[key].append(dict(row, side="sell"))
        if error or certainty in {"UNKNOWN", "PREPARED"}:
            return blocked("stop_submission_outcome_unknown")
    summary["outcome_unknown"] = any(r["final_fill_outcome"] == "UNKNOWN" for r in rows)
    complete = all(r["remaining_quantity"] == 0 for r in rows)
    summary["execution_status"] = "executed" if complete else "blocked"
    summary["no_op_reason"] = None if complete else "stop_exit_not_complete"
    return ["protective_stop_observed"], summary
