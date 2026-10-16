from types import SimpleNamespace
from copy import deepcopy

from application.rebalance_service import _should_publish_execution_notification


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
