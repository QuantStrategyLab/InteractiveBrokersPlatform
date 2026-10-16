"""Offline notification facts using the installed, pinned receipt contract."""

from copy import deepcopy
import json
from unittest import mock

import pytest
from quant_platform_kit.common import execution_receipts as receipts

from application import execution_receipt_adapter as adapter
from scripts import emit_digest_candidates as emitter
from scripts.project_digest_candidates import project_digest_candidates


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


def validate(source):
    return receipts.attach_execution_receipt(dict(source), source.get("execution_receipt"))["execution_receipt"]


def project(sources, validator=validate):
    return project_digest_candidates(runtime_reports=sources, validate_receipt=validator,
        opaque_account_uid="synthetic-uid", target_id="ibkr/synthetic-target")


@pytest.mark.parametrize("counts", [False, True])
def test_verified_reason_requires_explicit_counts_for_no_order_claim(counts):
    source = report(counts=counts)
    original = deepcopy(source)
    row = project([source])["runs"][0]
    assert row["block_reason"] == {
        "reason_code": "strategy_risk_rejected", "observed_value": None, "limit": None,
        "order_submitted_this_run": False if counts else None,
    }
    assert row["order_count"] is None and row["fill_count"] is None
    assert source == original


@pytest.mark.parametrize("mutation", ["pending", "error", "prior_receipt", "revision",
    "submission", "bad_counter", "missing_receipt", "unknown_outcome"])
def test_contradictory_or_unbound_evidence_omits_annotation(mutation):
    source = report()
    if mutation == "pending": source["summary"]["orders_pending_count"] = 1
    elif mutation == "error": source["status"] = "error"
    elif mutation == "prior_receipt": source["execution_receipt"] = report(minute=1)["execution_receipt"]
    elif mutation == "revision": source["runtime_release_receipt"]["strategy_release"]["strategy_revision"] = "b" * 40
    elif mutation == "submission": source["summary"]["option_orders_submitted"] = [{"symbol": "SYNTHETIC"}]
    elif mutation == "bad_counter": source["summary"]["orders_submitted_count"] = False
    elif mutation == "missing_receipt": del source["execution_receipt"]
    elif mutation == "unknown_outcome": source["execution_receipt"]["outcome"] = "unknown"
    assert "block_reason" not in project([source])["runs"][0]


@pytest.mark.parametrize("mutation", ["incomplete", "tie", "duplicate", "target"])
def test_ambiguous_or_incomplete_latest_run_suppresses_old_annotation(mutation):
    first, latest = report(), report(run_id="synthetic-run-2", minute=1)
    if mutation == "incomplete":
        for key in ("execution_receipt", "status", "summary"): latest.pop(key)
    elif mutation == "tie": latest = report(run_id="synthetic-run-2")
    elif mutation == "duplicate": latest["run_id"] = first["run_id"]
    elif mutation == "target": latest["runtime_target"]["account_selector"] = ["synthetic-other"]
    assert "block_reason" not in project([first, latest])["runs"][0]


def test_optional_annotation_preserves_daily_counts_and_omits_private_prose():
    earlier, latest = report(), report(run_id="synthetic-run-2", minute=1)
    earlier["summary"].update(order_count=2, fill_count=3)
    latest["summary"].update(order_count=0, fill_count=0, no_op_reason="synthetic-private-text")
    baseline = project([earlier, latest], validator=None)
    actual = project([earlier, latest])
    assert actual["runs"][0].pop("block_reason")["order_submitted_this_run"] is False
    assert actual == baseline
    assert (actual["runs"][0]["order_count"], actual["runs"][0]["fill_count"]) == (2, 3)
    assert "synthetic-private-text" not in json.dumps(actual)


@pytest.mark.parametrize("qpk_available", [False, True])
def test_emitter_uses_optional_qpk_validator_without_upload(tmp_path, qpk_available):
    output = tmp_path / "candidates.json"
    env = {"IBKR_DIGEST_CANDIDATES_OUTPUT_PATH": str(output),
        "IBKR_DIGEST_OPAQUE_ACCOUNT_UID": "synthetic-uid", "IBKR_DIGEST_TARGET_ID": "synthetic-target"}
    replacement = {"quant_platform_kit.common.execution_receipts": receipts if qpk_available else None}
    with mock.patch.dict("sys.modules", replacement):
        result = emitter.emit_digest_candidates(env, report_loader=lambda _env: report())
    assert result["status"] == "candidates_written"
    row = json.loads(output.read_text())["runs"][0]
    assert ("block_reason" in row) is qpk_available
    assert row["order_count"] is None and row["fill_count"] is None
