"""Offline option-loop regressions; these do not grant order admission."""

from types import SimpleNamespace

import pytest

from application.execution_service import _execute_option_order_intents
from notifications.telegram import build_translator
from quant_platform_kit.common.models import OrderIntent


def run_option_loop(submit, *, summary=None, translator=None):
    if summary is None:
        summary = {key: [] for key in (
            "option_orders_submitted", "option_orders_pending", "option_orders_filled",
            "option_orders_partially_filled", "option_orders_skipped", "skipped_reasons",
        )}
    intents = tuple({
        "asset_class": "option", "intent_type": "single_leg_option",
        "action": "buy_to_open", "underlier": symbol, "right": "C",
        "expiration": "2028-01-21", "strike": 70.0, "quantity": 1,
        "limit_price": 1.0, "contract_multiplier": 100,
    } for symbol in ("TQQQ", "SOXL"))
    logs = []
    remaining = _execute_option_order_intents(
        object(), intents, submit_order_intent=submit, order_intent_cls=OrderIntent,
        translator=translator or build_translator("en"), execution_summary=summary,
        trade_logs=logs, dry_run_only=False, order_account_id="DU00000003",
        buying_power=10000.0,
    )
    return summary, logs, remaining


@pytest.mark.parametrize("report", [
    None, SimpleNamespace(), {"status": "Submitted", "broker_order_id": "untrusted"},
    SimpleNamespace(status="Submitted", broker_order_id=None),
    SimpleNamespace(status="Unknown", broker_order_id="original-option-order"),
    SimpleNamespace(status="PendingCancel", broker_order_id="original-option-order"),
])
def test_unknown_option_report_stops_before_second_intent_and_retains_first(report):
    sent = []

    def submit(_ib, intent):
        sent.append(intent)
        return report

    summary, _logs, remaining = run_option_loop(submit)
    assert len(sent) == 1
    assert summary["submission_halted"] is True
    assert summary["outcome_unknown"] is True
    assert summary["execution_status"] == "pending_reconciliation"
    assert summary["no_op_reason"] == "broker_order_outcome_unknown"
    pending = summary["option_orders_pending"]
    assert len(pending) == 1
    assert pending[0]["underlier"] == sent[0].symbol
    assert pending[0]["quantity"] == sent[0].quantity
    assert pending[0]["broker_order_id"] == getattr(report, "broker_order_id", None)
    assert summary["option_orders_skipped"] == []
    assert summary["option_orders_filled"] == []
    assert remaining == 10000.0  # No reconciled buying-power update is invented.


@pytest.mark.parametrize("flag", ["submission_halted", "outcome_unknown"])
def test_already_uncertain_cycle_cannot_start_option_loop(flag):
    sent = []
    summary = {flag: True}
    assert run_option_loop(lambda *args: sent.append(args), summary=summary)[2] == 10000.0
    assert not sent
    assert summary == {flag: True}


def test_uncertain_option_is_recorded_before_notification_formatting_can_fail():
    summary = {key: [] for key in (
        "option_orders_submitted", "option_orders_pending", "option_orders_filled",
        "option_orders_partially_filled", "option_orders_skipped", "skipped_reasons",
    )}
    sent = []

    def submit(_ib, intent):
        sent.append(intent)
        return SimpleNamespace(status="Unknown", broker_order_id="original-option-order")

    def failing_format(*_args, **_kwargs):
        raise RuntimeError("synthetic formatting failure")

    with pytest.raises(RuntimeError, match="formatting failure"):
        run_option_loop(submit, summary=summary, translator=failing_format)
    assert len(sent) == 1
    assert summary["outcome_unknown"] is True
    assert summary["option_orders_pending"][0]["broker_order_id"] == "original-option-order"


@pytest.mark.parametrize("status", ["Submitted", "Rejected", "Cancelled"])
def test_known_option_response_preserves_existing_continuation(status):
    sent = []

    def submit(_ib, intent):
        sent.append(intent)
        return SimpleNamespace(status=status, broker_order_id=f"synthetic-{len(sent)}")

    summary, _logs, remaining = run_option_loop(submit)
    assert len(sent) == 2
    assert not summary.get("submission_halted", False)
    assert not summary.get("outcome_unknown", False)
    assert remaining == (9800.0 if status == "Submitted" else 10000.0)


@pytest.mark.parametrize("outcome", ["unknown", "missing", "timeout"])
def test_option_loop_with_real_journal_retains_unresolved_attempt_across_restart(tmp_path, outcome):
    from quant_platform_kit.common.execution_state import (
        AccountSubmissionError, AccountSubmissionSession, ExecutionMarkerStore,
    )

    def session(intent_id):
        return AccountSubmissionSession(
            ExecutionMarkerStore(local_dir=tmp_path), broker="ibkr", account_id="DU00000003",
            owner_id="synthetic-owner", profile="synthetic-option-profile",
            intent_id=intent_id, release_id="c" * 64,
        )

    active = session("a" * 64)
    sent = []
    grants = []

    def submit(_ib, intent):
        grant = active.reserve(intent)
        grants.append(grant)

        def venue():
            sent.append(intent)
            if outcome == "timeout":
                raise TimeoutError("synthetic timeout")
            if outcome == "missing":
                return None
            return SimpleNamespace(status="Unknown", broker_order_id="original-option-order")

        return active.dispatch(grant, intent, venue)

    if outcome == "timeout":
        with pytest.raises(TimeoutError, match="synthetic timeout"):
            run_option_loop(submit)
    else:
        summary, _logs, _remaining = run_option_loop(submit)
        assert summary["outcome_unknown"] is True
    assert len(sent) == len(grants) == 1
    restarted = session("b" * 64)
    attempts = restarted.read_state()["attempts"]
    assert len(attempts) == 1
    retained = next(iter(attempts.values()))
    assert retained["certainty"] == "UNKNOWN"
    assert retained["order_sha256"] == grants[0].order_sha256
    assert retained["quantity"] == sent[0].quantity
    with pytest.raises(AccountSubmissionError, match="unresolved UNKNOWN"):
        restarted.reserve(sent[0])
