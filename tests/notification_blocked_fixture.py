from unittest import mock
from application import execution_receipt_adapter as adapter

def report(*, run_id="synthetic-run-1", minute=0, counts=True):
    source = {
        "schema_version": "runtime_report.v1", "platform": "interactive_brokers",
        "strategy_profile": "synthetic_strategy", "dry_run": False,
        "status": "ok", "errors": [], "run_id": run_id,
        "started_at": f"2026-10-16T12:{minute:02d}:00Z",
        "finished_at": f"2026-10-16T12:{minute:02d}:59Z",
        "runtime_target": {"execution_mode": "live", "account_selector": ["synthetic"]},
        "runtime_release_receipt": {"attestation_state": "self_attested",
            "strategy_release": {"strategy_revision": "a" * 40}},
        "summary": {"execution_status": "blocked"},
    }
    real_attach = adapter.attach_runtime_execution_receipt
    with mock.patch.object(adapter, "attach_runtime_execution_receipt",
            side_effect=lambda body, **kw: real_attach(body,
                observed_at=f"2026-10-16T12:{minute:02d}:30Z", **kw)):
        adapter.attach_cycle_execution_receipt(source, {"execution_status": "blocked"}, {},
            execution_failed=False)
    if counts:
        source["summary"]["notification_digest"] = {
            "schema_version": "qsl.notification_cycle.v1", "dry_run": False,
            "order_count": 0, "fill_count": 0,
        }
    return source
