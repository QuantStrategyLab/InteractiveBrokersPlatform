"""Real IBKR archive export -> pinned, offline QRS intake/merge/aggregate."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import importlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

from notifications import unified_adapter as adapter
from notifications.archive_adapter import emit
from notifications.digest_snapshot.digest_candidates import cycle_evidence
from tests.test_unified_notification_adapter import (
    NOW, TARGET, CandidateStore, StateStore, event, healthy_receipt_report,
)

QRS_SHA = "bd3dc79f10b2c0b8048721695885c3f9333a53c6"
QRS_FIXTURE = Path(__file__).parent / "fixtures/qrs_notification_contract"


@pytest.fixture
def qrs(monkeypatch):
    monkeypatch.setenv("QRS_UNIFIED_NOTIFICATION_ENABLED", "true")
    monkeypatch.syspath_prepend(str(QRS_FIXTURE))
    with patch.dict(sys.modules):
        names = ("merge_digest_candidates", "notification_candidate_adapter", "notification_mode",
                 "notification_snapshot", "daily_digest_notify", "display_names", "unified_daily_digest")
        for name in list(sys.modules):
            if any(name == prefix or name.startswith(prefix + ".") for prefix in names):
                del sys.modules[name]
        yield {name: importlib.import_module(name) for name in names if name != "notification_snapshot"}


def archive_report(run, orders=()):
    from quant_platform_kit.common.execution_receipts import build_execution_receipt
    source = healthy_receipt_report()
    source["run_id"] = run
    summary = {"execution_status": "completed", "orders_submitted": list(orders),
               "broker_submission_done": bool(orders)}
    summary["notification_digest"] = cycle_evidence(summary, orders)
    source["summary"] = summary
    source["execution_receipt"] = build_execution_receipt(
        platform="ibkr", strategy_profile=TARGET["strategy_profile"], strategy_revision="a" * 40,
        execution_mode="paper", outcome="submitted" if orders else "no_action", observed_at=NOW)
    return source


def export_archive(monkeypatch, reports, *, seed_unknown=False):
    state, candidates = StateStore(), CandidateStore()
    monkeypatch.setattr(adapter, "_state_store", lambda env=None: state)
    monkeypatch.setattr(adapter, "GCSCandidateStore", lambda *args, **kwargs: candidates)
    env = {adapter.FLAG: "true", "RUNTIME_TARGET_ENABLED": "true", "RUNTIME_TARGET_JSON": json.dumps(TARGET),
           "DIGEST_STORE_TARGET": "synthetic-slot", "DIGEST_OPAQUE_ACCOUNT_UID": "synthetic-account",
           "DIGEST_TARGET_ID": "synthetic-target", "EXECUTION_REPORT_GCS_URI": "gs://synthetic/archive",
           "DIGEST_CANDIDATES_GCS_URI": "gs://synthetic/candidates"}
    if seed_unknown:
        assert adapter.persist_candidate(event(run=reports[0]["run_id"]).to_candidate(), env=env)
    def read(args):
        if args[0] == "ls":
            return [{"url": f"gs://synthetic/archive/ibkr/strategy/2026-10/{i}.json",
                     "metadata": {"updated": NOW.isoformat()}} for i in range(len(reports))]
        index = int(args[-1].rsplit("/", 1)[-1].split(".")[0])
        return deepcopy(reports[index])
    assert emit(env, now=NOW + timedelta(hours=2, minutes=20), read=read)["uploaded"]
    # Observe the first receipt again in the real monitor path. It belongs to
    # that business run and may not create a monitor/workflow run.
    monkeypatch.setenv(adapter.FLAG, "true")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    if reports[0]["summary"]["execution_status"] == "completed":
        assert adapter.publish_monitor(kind="healthy_heartbeat", report=reports[0],
            now=NOW + timedelta(hours=2, minutes=21), send=lambda: pytest.fail("healthy monitor must not send"))
    return next(iter(candidates.rows.values()))[1]


def intake(qrs, tmp_path, payload):
    source = tmp_path / "ibkr.json"
    source.write_text(json.dumps(payload))
    merger = qrs["merge_digest_candidates"]
    exported, stats = merger.merge(source, None)
    assert stats["base_rows"] == len(payload["runs"])
    receiver = qrs["notification_candidate_adapter"]
    current, stale = receiver.partition(merger._rows(exported), "2026-10-16")
    assert stale == []
    expected = [{"platform_id": "ibkr", "opaque_account_uid": "synthetic-account",
                 "target_id": "synthetic-target", "strategy_profile": TARGET["strategy_profile"]}]
    receiver.validate_expected(expected)
    return receiver.aggregate(current, expected)


def render(qrs, rows, summary, locale):
    digest = qrs["daily_digest_notify"]
    payload = digest.DailyDigestInput(business_day="2026-10-16", locale=locale,
        runs=digest.filter_runs_for_digest(rows), notification_summary=summary,
        evidence_coverage=digest.EvidenceCoverage(candidates_configured=True, candidates_loaded=True,
                                                 candidates_count=len(rows)))
    return qrs["unified_daily_digest"].render(payload)


def test_real_archive_two_healthy_runs_roundtrip_and_duplicate_observation(qrs, monkeypatch, tmp_path):
    source = [archive_report("zero-a"), archive_report("zero-b")]
    payload = export_archive(monkeypatch, [*source, deepcopy(source[0])], seed_unknown=True)
    assert len(payload["runs"]) == 2
    rows, summary = intake(qrs, tmp_path, payload)
    assert summary["source_inventory"]["ibkr"] == {"expected_targets": 1, "covered_targets": 1, "runs": 2}
    assert summary["counts_unknown"] is False and summary["run_count_unknown"] is False
    assert summary["event_count"] == 3 and summary["immediate_events"] == 0
    for row in rows:
        assert (row["cycle_count"], row["order_count"], row["fill_count"]) == (1, 0, 0)
        assert all(fact["source_run_id"] == row["source_run_id"] for fact in row["notification_events"])
    assert "订单：0" in render(qrs, rows, summary, "zh")
    assert "Orders: 0" in render(qrs, rows, summary, "en")
    assert "未知" not in render(qrs, rows, summary, "zh")
    # Duplicate whole producer observations are also merged by actual QRS code.
    twice, repeated = qrs["notification_candidate_adapter"].aggregate([*rows, *deepcopy(rows)],
        [{key: rows[0][key] for key in ("platform_id", "opaque_account_uid", "target_id", "strategy_profile")}])
    assert len(twice) == 2 and repeated == summary


def test_single_healthy_zero_order_run_is_known_and_renders_zero(qrs, monkeypatch, tmp_path):
    payload = export_archive(monkeypatch, [archive_report("single-zero")])
    rows, summary = intake(qrs, tmp_path, payload)
    assert summary["source_inventory"]["ibkr"]["runs"] == 1
    assert summary["counts_unknown"] is False and summary["event_count"] == 2
    assert rows[0]["order_count"] == 0 and rows[0]["field_status"]["order_count"] == "known"
    assert "本轮 0 单" in render(qrs, rows, summary, "zh")
    assert "0 orders this run" in render(qrs, rows, summary, "en")
    assert "未知" not in render(qrs, rows, summary, "zh")


def test_real_order_archive_counts_stay_on_their_own_run(qrs, monkeypatch, tmp_path):
    orders = [{"order_id": "synthetic-order", "symbol": "AAA", "status": "filled",
               "fills": [{"execution_id": "synthetic-fill-a"}, {"execution_id": "synthetic-fill-b"}]}]
    payload = export_archive(monkeypatch, [archive_report("zero"), archive_report("ordered", orders)])
    rows, summary = intake(qrs, tmp_path, payload)
    by_run = {row["source_run_id"]: row for row in rows}
    assert (by_run["zero"]["order_count"], by_run["zero"]["fill_count"]) == (0, 0)
    assert (by_run["ordered"]["order_count"], by_run["ordered"]["fill_count"]) == (1, 2)
    assert summary["source_inventory"]["ibkr"]["runs"] == 2
    assert summary["event_count"] == 3 and summary["immediate_events"] == 1
    assert summary["counts_unknown"] is False
    assert "订单：1" in render(qrs, rows, summary, "zh")


def test_blocked_zero_order_archive_renders_this_run_zero_not_unknown(qrs, monkeypatch, tmp_path):
    from tests.notification_blocked_fixture import report
    source = report()
    source.update(platform="ibkr", service_name=TARGET["service_name"],
                  runtime_target={**deepcopy(TARGET), "execution_mode": source["runtime_target"]["execution_mode"]})
    payload = export_archive(monkeypatch, [source])
    rows, summary = intake(qrs, tmp_path, payload)
    assert rows[0]["block_reason"]["order_submitted_this_run"] is False
    assert rows[0]["order_count"] == 0 and rows[0]["field_status"]["order_count"] == "known"
    assert "本轮 0 单" in render(qrs, rows, summary, "zh")
    assert "0 orders this run" in render(qrs, rows, summary, "en")
    assert "全窗口统计未知" not in render(qrs, rows, summary, "zh")


def test_qrs_test_only_source_provenance():
    provenance = json.loads((QRS_FIXTURE / "provenance.json").read_text())
    assert provenance["commit"] == QRS_SHA
    for name, source in provenance["modules"].items():
        assert hashlib.sha256((QRS_FIXTURE / name).read_bytes()).hexdigest() == source["sha256"]
