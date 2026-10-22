"""Offline routing tests: no broker, storage, or Telegram calls."""

import copy

import pytest

from notifications.digest_snapshot.digest_routing import defer_routine_cycle_to_digest


def test_default_off_and_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("QSL_ROUTINE_NOTIFICATION_MODE", raising=False)
    evidence = {"execution_status": "no_action", "orders_pending_count": 0}
    original = copy.deepcopy(evidence)
    assert not defer_routine_cycle_to_digest(evidence, submitted_order_count=0)
    monkeypatch.setenv("QSL_ROUTINE_NOTIFICATION_MODE", "daily_digest")
    assert defer_routine_cycle_to_digest(evidence, submitted_order_count=0)
    assert evidence == original


@pytest.mark.parametrize("status", ["blocked", "unknown", "pending", "pending_reconciliation", "rejected", "", None])
def test_urgent_or_unknown_results_are_never_deferred(status):
    assert not defer_routine_cycle_to_digest(
        {"execution_status": status}, submitted_order_count=0, mode="daily_digest",
    )


@pytest.mark.parametrize("field,value", [
    ("notification_attention_reason_codes", ["DAILY_LOSS_LIMIT_EXCEEDED"]),
    ("errors", ["storage_failed"]), ("orders_pending_count", 1),
    ("reconciliation_required", True), ("broker_submission_done", True),
    ("no_op_reason", "RECONCILE_ONLY"), ("notification_error", "unknown_result"),
])
def test_healthy_label_cannot_hide_guard_or_operational_evidence(field, value):
    assert not defer_routine_cycle_to_digest(
        {"execution_status": "no_action", field: value},
        submitted_order_count=0, mode="daily_digest",
    )


@pytest.mark.parametrize("count", [1, None, False, -1])
def test_any_order_or_unknown_order_count_remains_immediate(count):
    assert not defer_routine_cycle_to_digest(
        {"execution_status": "completed"}, submitted_order_count=count, mode="daily_digest",
    )


def test_invalid_mode_preserves_immediate_delivery():
    assert not defer_routine_cycle_to_digest(
        {"execution_status": "no_action"}, submitted_order_count=0, mode="typo",
    )


def test_below_threshold_is_routine_but_an_unclassified_reason_stays_immediate():
    evidence = {"execution_status": "no_op", "no_op_reason": "rebalance_diff_below_threshold"}
    assert defer_routine_cycle_to_digest(evidence, submitted_order_count=0, mode="daily_digest")
    evidence["no_op_reason"] = "unknown_order_result"
    assert not defer_routine_cycle_to_digest(evidence, submitted_order_count=0, mode="daily_digest")


@pytest.mark.parametrize("reason", ["target_diff_below_threshold", "min_notional:TQQQ", "quantity_zero:TQQQ"])
def test_final_normal_no_order_reasons_join_digest(reason):
    evidence = {"execution_status": "no_op", "no_op_reason": reason, "orders_skipped": [{"reason": reason.split(":")[0]}]}
    assert defer_routine_cycle_to_digest(evidence, submitted_order_count=0, mode="daily_digest")
    for change in ({"orders_pending_count": 1}, {"skipped_reasons": ["submit_failed:TQQQ"]},
                   {"error": "synthetic_failure"}, {"execution_status": "unknown"}):
        assert not defer_routine_cycle_to_digest({**evidence, **change}, submitted_order_count=0, mode="daily_digest")
    assert not defer_routine_cycle_to_digest({**evidence, "no_op_reason": "min_notional:unknown_order_result"},
                                              submitted_order_count=0, mode="daily_digest")


def test_real_orders_remain_immediate_even_if_old_digest_order_flag_is_enabled(monkeypatch):
    evidence = {"execution_status": "completed", "broker_submission_done": True}
    orders = [{"status": "filled"}]
    original = copy.deepcopy((evidence, orders))
    assert not defer_routine_cycle_to_digest(evidence, submitted_order_count=1,
        submitted_orders=orders, mode="daily_digest")
    monkeypatch.setenv("QSL_DIGEST_ROUTINE_ORDERS", "false")
    assert not defer_routine_cycle_to_digest(evidence, submitted_order_count=1,
        submitted_orders=orders, mode="daily_digest")
    assert (evidence, orders) == original


@pytest.mark.parametrize("field", ["orders_submitted", "orders_filled", "orders_partially_filled",
                                   "option_orders_submitted", "option_orders_filled"])
def test_order_arrays_cannot_be_hidden_by_zero_submitted_count(field):
    assert not defer_routine_cycle_to_digest(
        {"execution_status": "completed", field: [{"status": "filled"}]},
        submitted_order_count=0, mode="daily_digest")


def test_pending_order_cannot_qualify_as_healthy_archived_fill():
    from notifications.digest_snapshot.digest_routing import healthy_cycle_evidence
    assert not healthy_cycle_evidence(
        {"execution_status": "completed", "orders_pending": [{"status": "pending"}]},
        submitted_order_count=1, submitted_orders=[{"status": "filled"}], mode="daily_digest")
    assert not healthy_cycle_evidence(
        {"execution_status": "completed", "orders_submitted": [{"status": "rejected"}]},
        submitted_order_count=1, submitted_orders=[{"status": "filled"}], mode="daily_digest")


@pytest.mark.parametrize("status", ["pending", "submitted", "rejected", "unknown", "partially_filled", ""])
def test_unresolved_orders_never_become_routine(status):
    assert not defer_routine_cycle_to_digest({"execution_status": "completed"},
        submitted_order_count=1, submitted_orders=[{"status": status}], mode="daily_digest")
