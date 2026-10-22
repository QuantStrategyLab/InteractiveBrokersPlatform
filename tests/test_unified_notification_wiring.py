from types import SimpleNamespace
from copy import deepcopy

from application.rebalance_service import _should_publish_execution_notification
from notifications.unified_adapter import _kind


def test_confirmed_orders_defer_but_pending_failed_and_opt_out_stay_immediate(monkeypatch):
    monkeypatch.setenv("QSL_ROUTINE_NOTIFICATION_MODE", "daily_digest")
    config = SimpleNamespace(dry_run_only=False, notify_no_trade_cycles=True)
    summary = {"execution_status": "completed", "orders_submitted": [{"status": "filled"}], "orders_filled": [{"status": "filled"}]}
    original = deepcopy(summary)
    def publish(value):
        return _should_publish_execution_notification(config=config, signal_metadata={}, execution_summary=value, trade_logs=["trade"])
    assert not publish(summary)
    assert summary == original
    assert publish({**summary, "orders_pending": [{"status": "pending"}], "execution_status": "pending_reconciliation"})
    assert publish({**summary, "error": "synthetic_failure"})
    monkeypatch.setenv("QSL_DIGEST_ROUTINE_ORDERS", "false")
    assert publish(summary)


def test_final_no_order_is_not_a_standalone_execution_result(monkeypatch):
    monkeypatch.setenv("QSL_ROUTINE_NOTIFICATION_MODE", "daily_digest")
    config = SimpleNamespace(dry_run_only=False, notify_no_trade_cycles=False,
                             notification_attention_reason_codes=())
    for reason in ("target_diff_below_threshold", "min_notional:TQQQ", "quantity_zero:TQQQ"):
        result = {"execution_status": "no_op", "no_op_reason": reason,
                  "orders_skipped": [{"symbol": "TQQQ", "reason": reason.split(":")[0]}],
                  "skipped_reasons": [reason] if reason != "target_diff_below_threshold" else []}
        assert _kind(result) == "routine_cycle"
        assert not _should_publish_execution_notification(
            config=config, signal_metadata={}, execution_summary=result, trade_logs=[])
    for reason in ("outside_execution_window", "outside_monthly_execution_window"):
        assert _kind({"execution_status": "no_action", "no_op_reason": reason}) == "routine_cycle"
    assert _kind({"execution_status": "no_op", "no_op_reason": "min_notional:unknown_order_result"}) == "real_error"
    assert _kind({"execution_status": "no_op", "no_op_reason": "min_notional:TQQQ",
                  "skipped_reasons": ["submit_failed:TQQQ"]}) == "real_error"
    for result in ({"execution_status": "unknown", "no_op_reason": "broker_order_outcome_unknown"},
                   {"execution_status": "blocked", "no_op_reason": "submit_failed:TQQQ"},
                   {"execution_status": "no_op", "no_op_reason": "min_notional:TQQQ",
                    "orders_pending": [{"status": "unknown"}]}):
        assert _kind(result) != "routine_cycle"
        assert _should_publish_execution_notification(
            config=config, signal_metadata={}, execution_summary=result, trade_logs=[])
    assert _kind({"execution_status": "completed", "orders_filled": [{"status": "filled"}]}) == "fill"
    assert _kind({"execution_status": "completed", "orders_submitted": [{"status": "rejected"}]}) == "order_status_unknown"
    assert _kind({"execution_status": "completed", "orders_submitted": [{"status": "filled"}]}) == "order_submitted"
