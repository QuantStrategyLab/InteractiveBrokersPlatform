"""Synthetic runtime reports; signal labels alone never establish zero fills."""

import copy

import pytest

from scripts.project_digest_candidates import project_digest_candidates


def report():
    return {
        "schema_version": "runtime_report.v1", "platform": "ibkr",
        "strategy_profile": "demo_strategy", "status": "ok", "errors": [],
        "started_at": "2026-10-14T20:01:00Z", "finished_at": "2026-10-14T20:02:00Z",
        "execution_receipt": {"outcome": "no_action", "broker_confirmation": "not_applicable"},
        "summary": {"execution_status": "no_op", "orders_submitted_count": 0},
    }


def project(value):
    return project_digest_candidates(runtime_reports=value, business_day="2026-10-14",
                                     opaque_account_uid="synthetic-account", target_id="demo/target")["runs"][0]


def test_verified_no_submission_projects_explicit_zero_counts():
    row = project(report())
    assert (row["fill_count"], row["order_count"]) == (0, 0)
    assert row["field_status"]["fill_count"] == "known"


def test_explicit_counts_preserve_distinct_fills_and_orders():
    value = report()
    value["execution_receipt"] = {"outcome": "filled", "broker_confirmation": "filled"}
    value["summary"] = {"execution_status": "completed", "orders_submitted_count": 2, "fill_count": 3}
    row = project(value)
    assert (row["fill_count"], row["order_count"]) == (3, 2)


@pytest.mark.parametrize("mutation", [
    lambda v: v.pop("execution_receipt"),
    lambda v: v["execution_receipt"].pop("broker_confirmation"),
    lambda v: v["summary"].pop("orders_submitted_count"),
    lambda v: v["summary"].update(orders_submitted_count=False),
    lambda v: v["summary"].update(orders_pending_count=1),
    lambda v: v["summary"].update(orders_filled_count=1),
    lambda v: v["summary"].update(option_orders_filled=[{"id": "synthetic"}]),
    lambda v: v["summary"].update(broker_submission_done=True),
    lambda v: v["summary"].update(execution_status="pending_reconciliation"),
    lambda v: v.update(errors=["synthetic failure"]),
    lambda v: v["summary"].update(fill_count="unreadable"),
])
def test_missing_or_contradictory_evidence_never_infers_zero_fills(mutation):
    value = report()
    mutation(value)
    assert project(value)["fill_count"] is None


def test_one_unknown_cycle_keeps_aggregate_unknown():
    first = report()
    second = copy.deepcopy(first)
    second["summary"] = {}
    row = project([first, second])
    assert row["fill_count"] is None
    assert row["order_count"] is None


def test_stale_reports_are_not_relabelled_as_current_zero():
    value = report()
    value["started_at"] = "2026-10-13T20:01:00Z"
    payload = project_digest_candidates(runtime_reports=value, business_day="2026-10-14")
    assert payload["runs"] == []
