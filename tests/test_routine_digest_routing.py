from types import SimpleNamespace

import pytest

from application.rebalance_service import _should_publish_execution_notification, _should_suppress_noop_notification


def _publish(summary):
    return _should_publish_execution_notification(
        config=SimpleNamespace(dry_run_only=False, notify_no_trade_cycles=True),
        signal_metadata={}, execution_summary=summary, trade_logs=(),
    )


def test_opt_in_consolidates_both_noop_paths(monkeypatch):
    summary = {"execution_status": "no_op", "no_op_reason": "rebalance_diff_below_threshold"}
    monkeypatch.delenv("QSL_ROUTINE_NOTIFICATION_MODE", raising=False)
    assert _publish(summary)
    assert not _should_suppress_noop_notification(summary, order_count=0)
    monkeypatch.setenv("QSL_ROUTINE_NOTIFICATION_MODE", "daily_digest")
    assert not _publish(summary)
    assert _should_suppress_noop_notification(summary, order_count=0)


@pytest.mark.parametrize("summary", [
    {"execution_status": "blocked"}, {"execution_status": "unknown"},
    {"execution_status": "pending_reconciliation"},
    {"execution_status": "no_action", "notification_attention_reason_codes": ["DAILY_LOSS_LIMIT_EXCEEDED"]},
    {"execution_status": "completed", "orders_filled": [{"status": "filled"}]},
    {"execution_status": "no_action", "report_persistence_error": "failed"},
])
def test_urgent_and_order_activity_stays_immediate(monkeypatch, summary):
    monkeypatch.setenv("QSL_ROUTINE_NOTIFICATION_MODE", "daily_digest")
    assert _publish(summary)
