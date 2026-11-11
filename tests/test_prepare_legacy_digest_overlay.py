from collections.abc import Mapping
from types import SimpleNamespace

import pytest

from scripts.prepare_legacy_digest_overlay import OLD_ROUTE, ORIGINAL_ARCHIVE, patch_archive, patch_rebalance


@pytest.mark.parametrize(("mode", "summary", "expected"), [
    ("daily_digest", {"status": "filled", "order_id": 12}, False),
    ("immediate", {"status": "filled", "order_id": 12}, True),
    ("daily_digest", {"status": "filled"}, True),
    ("daily_digest", {"status": "submitted", "order_id": 12}, True),
    ("daily_digest", {"status": "partially_filled", "order_id": 12}, True),
])
def test_only_broker_confirmed_final_orders_are_deferred(monkeypatch, mode, summary, expected):
    base = (
        "def _should_publish_execution_notification(evidence, has_orders=True):\n"
        + OLD_ROUTE + "    return True\n"
    )
    env = {"Mapping": Mapping, "defer_routine_cycle_to_digest": lambda _evidence, **_kwargs: False}
    exec(patch_rebalance(base), env)
    monkeypatch.setenv("QSL_ROUTINE_NOTIFICATION_MODE", mode)
    monkeypatch.setenv("QSL_DIGEST_ROUTINE_ORDERS", "true")
    evidence = {"execution_status": "completed", "orders_submitted": [summary]}
    assert env["_should_publish_execution_notification"](evidence) is expected
    evidence["orders_pending"] = [{"status": "pending"}]
    assert env["_should_publish_execution_notification"](evidence) is True


@pytest.mark.parametrize(("mode", "cloud_uri", "throws", "expected"), [
    ("daily_digest", "gs://example/archive.json", False, 0),
    ("daily_digest", None, False, 1),
    ("daily_digest", None, True, 1),
    ("immediate", None, True, 0),
])
def test_legacy_archive_failures_become_real_alerts_without_changing_run_result(mode, cloud_uri, throws, expected):
    alerts = []
    source = ("def run(report, persist, dry_run_only_override=None):\n"
              "    try:\n        return 200\n    finally:\n        if True:\n            try:\n"
              "                report_path = persist()\n" + ORIGINAL_ARCHIVE)

    def notify(**data):
        alerts.append(data)
        raise RuntimeError("synthetic transport failure")

    def persist():
        if throws:
            raise RuntimeError("archive failure")
        return "/tmp/legacy.json"

    env = {"os": SimpleNamespace(getenv=lambda k, default=None: mode if k == "QSL_ROUTINE_NOTIFICATION_MODE" else default),
           "_publish_runtime_failure_notification": notify, "t": str}
    exec(patch_archive(source), env)
    assert env["run"]({"artifacts": {"runtime_report_cloud_uri": cloud_uri}}, persist) == 200
    assert len(alerts) == expected


def test_legacy_patch_refuses_source_drift():
    with pytest.raises(ValueError, match="legacy_archive_source_changed"):
        patch_archive("pass\n")
