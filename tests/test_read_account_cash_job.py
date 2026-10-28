"""Synthetic readback; no cloud or account access."""
from datetime import timedelta
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import observe_maintenance_cash_job as jobs
from scripts import read_account_cash_job as reader
from tests.test_observe_maintenance_cash_job import setup, NOW, JOB, EXECUTION


def fixture(monkeypatch):
    _, revision, expected, _ = setup()
    revision["spec"]["containers"][0]["env"].append({"name": "CASH_READONLY_PROFILE", "value": "global_etf_rotation"})
    document = jobs.job_document(revision, JOB, "pass")
    execution = {"metadata": {"name": EXECUTION, "labels": {"run.googleapis.com/job": JOB}},
                 "spec": document["spec"]["template"]["spec"],
                 "status": {"startTime": NOW.isoformat(), "completionTime": (NOW + timedelta(seconds=3)).isoformat(),
                            "succeededCount": 1}}
    namespace = "gs://synthetic/reports/maintenance-cash"
    uri = namespace + "/file.json"
    digest = hashlib.sha256(uri.encode()).hexdigest()
    logs = [{"resource": {"type": "cloud_run_job", "labels": {"job_name": JOB,
             "project_id": expected["expected_project_id"], "location": "synthetic-region"}},
             "labels": {"run.googleapis.com/execution_name": EXECUTION}, "timestamp": NOW.isoformat(),
             "textPayload": json.dumps({"status": "observation_saved", "report_uri_sha256": digest})}]
    raw = json.dumps({"strategy_profile": "global_etf_rotation", "runtime_target": {
        "strategy_profile": "global_etf_rotation"}, "started_at": NOW.isoformat(),
        "finished_at": (NOW + timedelta(seconds=1)).isoformat()}).encode()
    blob = SimpleNamespace(name="reports/maintenance-cash/file.json", generation=123, size=len(raw))
    bucket = Mock()
    bucket.blob.return_value.download_as_bytes.return_value = raw
    page = Mock()
    page.pages = iter([[blob]])
    page.next_page_token = None
    client = Mock()
    client.bucket.return_value = bucket
    client.list_blobs.return_value = page
    inspect = Mock(return_value={"status": "OBSERVED_JOB_ONLY", "source_identity_verified": True,
                   "saved_readback_verified": True, "report_sha256": "a" * 64,
                   "cash_observations": {"private": "not-output"}})
    monkeypatch.setattr(jobs, "inspect_report", inspect)
    return dict(expected=expected, actual_job=document, document=document, execution=execution, logs=logs,
                namespace=namespace, region="synthetic-region", client=client,
                now=NOW + timedelta(seconds=4)), bucket, inspect, blob


def test_exact_execution_finds_one_generation_and_redacts_output(monkeypatch):
    args, bucket, inspect, _ = fixture(monkeypatch)
    result = reader.readback(**args)
    assert result["status"] == "OBSERVED_JOB_ONLY" and "private" not in json.dumps(result)
    bucket.blob.assert_called_once_with("reports/maintenance-cash/file.json", generation=123)
    bucket.blob.return_value.download_as_bytes.assert_called_once_with(if_generation_match=123, timeout=60, retry=None)
    assert inspect.call_args.kwargs["execution"] == EXECUTION


def test_readback_uses_original_report_identity_checks(monkeypatch):
    actual_inspect = jobs.inspect_report
    args, bucket, _, _ = fixture(monkeypatch)
    monkeypatch.setattr(jobs, "inspect_report", actual_inspect)
    _, _, expected, report = setup()
    report["strategy_profile"] = "global_etf_rotation"
    report["runtime_target"]["strategy_profile"] = "global_etf_rotation"
    expected["expected_report_prefix"] = "gs://example-bucket/reports/interactive_brokers/tqqq_growth_income/synthetic-service"
    args["expected"] = expected
    args["namespace"] = "gs://example-bucket/reports/maintenance-cash"
    path = "reports/maintenance-cash/interactive_brokers/tqqq_growth_income/synthetic-service/2026-09/report.json"
    uri = "gs://example-bucket/" + path
    args["logs"][0]["textPayload"] = json.dumps({"status": "observation_saved", "report_uri_sha256":
                                                   hashlib.sha256(uri.encode()).hexdigest()})
    raw = json.dumps(report).encode()
    blob = SimpleNamespace(name=path, generation=123, size=len(raw))
    page = Mock()
    page.pages = iter([[blob]])
    page.next_page_token = None
    args["client"].list_blobs.return_value = page
    bucket.blob.return_value.download_as_bytes.return_value = raw
    assert reader.readback(**args)["source_identity_verified"] is True


@pytest.mark.parametrize("change", ["profile", "target_profile", "after_completion"])
def test_report_must_belong_to_selected_profile_and_execution_interval(monkeypatch, change):
    args, bucket, _, blob = fixture(monkeypatch)
    report = {"strategy_profile": "global_etf_rotation", "runtime_target": {
        "strategy_profile": "global_etf_rotation"}, "started_at": NOW.isoformat(),
        "finished_at": (NOW + timedelta(seconds=1)).isoformat()}
    if change == "profile":
        report["strategy_profile"] = "russell_top50_leader_rotation"
    elif change == "target_profile":
        report["runtime_target"]["strategy_profile"] = "russell_top50_leader_rotation"
    else:
        report["finished_at"] = (NOW + timedelta(seconds=4)).isoformat()
    raw = json.dumps(report).encode()
    blob.size = len(raw)
    bucket.blob.return_value.download_as_bytes.return_value = raw
    with pytest.raises(ValueError):
        reader.readback(**args)


@pytest.mark.parametrize("case", ["zero", "duplicate", "wrong_execution", "wrong_region", "upload_failed", "generation_changed"])
def test_missing_ambiguous_or_untrusted_report_fails(monkeypatch, case):
    args, bucket, _, blob = fixture(monkeypatch)
    if case in {"zero", "duplicate"}:
        page = Mock()
        page.pages = iter([[] if case == "zero" else [blob, blob]])
        page.next_page_token = None
        args["client"].list_blobs.return_value = page
    elif case == "wrong_execution":
        args["logs"][0]["labels"]["run.googleapis.com/execution_name"] = "other"
    elif case == "wrong_region":
        args["logs"][0]["resource"]["labels"]["location"] = "other"
    elif case == "upload_failed":
        args["logs"][0]["textPayload"] = '{"status":"failed"}'
    else:
        bucket.blob.return_value.download_as_bytes.side_effect = RuntimeError("generation changed")
    with pytest.raises((ValueError, RuntimeError)):
        reader.readback(**args)


def test_cloud_run_structured_json_payload_is_read(monkeypatch):
    args, _, _, _ = fixture(monkeypatch)
    args["logs"][0]["jsonPayload"] = json.loads(args["logs"][0].pop("textPayload"))
    assert reader.readback(**args)["saved_readback_verified"] is True
