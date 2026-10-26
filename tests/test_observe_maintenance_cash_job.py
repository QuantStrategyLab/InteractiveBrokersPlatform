"""Synthetic Job lifecycle, privacy and unknown-outcome regression checks."""
import copy
from datetime import timedelta
import json

import pytest

from scripts import observe_maintenance_cash_job as jobs
from scripts import deploy_pilot_no_traffic as pilot
from tests.test_observe_maintenance_cash import inputs, NOW, URI
from tests.test_pilot_no_traffic_release import environment

JOB = "ibkr-cash-read-123"
EXECUTION = JOB + "-abc"


def setup():
    state, revision, expected, report = inputs()
    revision["metadata"]["labels"]["commit-sha"] = jobs.SOURCE
    revision["metadata"]["annotations"]["run.googleapis.com/network-interfaces"] = '[{"network":"synthetic-vpc","subnetwork":"synthetic-subnet"}]'
    revision["spec"]["containers"][0]["image"] = "synthetic/image@" + jobs.IMAGE_DIGEST
    revision["spec"]["containers"][0]["env"].append(dict(name="EXECUTION_REPORT_GCS_URI", value="gs://example-bucket/tqqq_growth_income"))
    state["serving_revision"]["spec"]["containers"][0]["env"] = copy.deepcopy(revision["spec"]["containers"][0]["env"])
    # Admission compares the full non-image runtime configuration.
    state["serving_revision"]["metadata"]["annotations"].update(revision["metadata"]["annotations"])
    state["service"]["spec"]["template"] = {key: copy.deepcopy(revision[key]) for key in ("metadata", "spec")}
    diag = report["diagnostics"]
    diag.update(runtime_revision=None, execution_kind="cloud_run_job", cloud_run_job=JOB,
                cloud_run_execution=EXECUTION, task_index=0, task_attempt=0, task_count=1)
    return state, revision, expected, report


class Cloud:
    def __init__(self, state, revision, failure=None):
        self.state, self.revision, self.failure = state, revision, failure
        self.created, self.executed, self.calls = None, [], []

    def snapshot(self, *_):
        return copy.deepcopy(self.state)

    def run(self, args, **kwargs):
        self.calls.append(args)
        key = args[1:4]
        if key == ["run", "revisions", "describe"]:
            return json.dumps(self.revision)
        if key == ["run", "jobs", "list"]:
            return json.dumps([self.created] if self.created else [])
        if key == ["run", "jobs", "replace"]:
            from pathlib import Path
            self.created = json.loads(Path(args[4]).read_text())
            self.created["metadata"]["uid"] = "synthetic-uid"
            if self.failure == "create_unknown":
                raise pilot.ReleaseError("job_create_unknown")
            return "{}"
        if key == ["run", "jobs", "describe"]:
            value = copy.deepcopy(self.created)
            if self.failure == "readback_changed" and not self.executed:
                value["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["env"].append(dict(name="UNREVIEWED", value="private"))
            return json.dumps(value)
        if key == ["run", "jobs", "execute"]:
            self.executed = [dict(metadata=dict(name=EXECUTION, labels={"run.googleapis.com/job": JOB}),
                spec=copy.deepcopy(self.created["spec"]["template"]["spec"]), status=dict(completionTime=NOW.isoformat(), succeededCount=1))]
            if self.failure == "execute_unknown":
                raise pilot.ReleaseError("job_execute_unknown")
            if self.failure == "task_failed":
                self.executed[0]["status"].update(succeededCount=0, failedCount=1)
            return json.dumps(self.executed[0])
        if args[1:5] == ["run", "jobs", "executions", "list"]:
            return json.dumps(self.executed)
        if args[1:5] == ["run", "jobs", "executions", "describe"]:
            return json.dumps(self.executed[0])
        if key == ["run", "jobs", "delete"]:
            self.created = None
            return "{}"
        if args[1:3] == ["logging", "read"]:
            if self.failure == "log_denied":
                raise pilot.ReleaseError("job_read_failed")
            return json.dumps([dict(textPayload="execution_report " + URI)])
        raise AssertionError("unreviewed command " + str(args[:5]))


@pytest.mark.parametrize("failure", [None, "create_unknown", "execute_unknown", "task_failed", "log_denied", "readback_changed"])
def test_single_execution_and_cleanup(tmp_path, failure):
    state, revision, expected, report = setup()
    cloud = Cloud(state, revision, failure)
    env = {**environment(), "MAINTENANCE_SOURCE_COMMIT": jobs.SOURCE, "MAINTENANCE_JOB": "true"}
    result = jobs.observe(expected, env, tmp_path / "safe.json", run=cloud.run, snapshot=cloud.snapshot,
        sleep=lambda _: None, now=lambda: NOW, load_report=lambda *_, **__: report)
    assert result["status"] == ("OBSERVED_JOB_ONLY" if failure is None else "STOPPED")
    assert result["baseline_unchanged"] is True
    assert result["job_cleanup_verified"] is (failure != "readback_changed")
    executions = [row for row in cloud.calls if row[1:4] == ["run", "jobs", "execute"]]
    assert len(executions) == (0 if failure in {"create_unknown", "readback_changed"} else 1)
    assert not (tmp_path / "maintenance-cash-job.private.json").exists()
    assert "12345.67" not in json.dumps(result)
    assert not any("update-traffic" in row or "scheduler" in row for row in cloud.calls)
    assert result["runtime_adopted"] is False


@pytest.mark.parametrize("change", ["other_account", "service_revision", "other_execution", "outside_namespace", "stale", "native_invalid"])
def test_report_identity_and_native_fields(change):
    _, _, expected, report = setup()
    uri, started = URI, NOW
    if change == "other_account":
        report["summary"]["account_facts"]["account_ids"] = ["DU00000002"]
    elif change == "service_revision":
        report["diagnostics"]["runtime_revision"] = "forged-service"
    elif change == "other_execution":
        report["diagnostics"]["cloud_run_execution"] += "wrong"
    elif change == "outside_namespace":
        uri = uri.replace("maintenance-cash/", "")
    elif change == "stale":
        started = NOW + timedelta(minutes=1)
    else:
        report["summary"]["account_facts"]["broker_cash_observations"]["values"][0]["value"] = "NaN"
    with pytest.raises((ValueError, pilot.ReleaseError)):
        jobs.inspect_report(report, uri, expected, job=JOB, execution=EXECUTION, started=started, now=NOW)


def test_job_translation_preserves_env_secrets_resources_and_network():
    _, revision, _, _ = setup()
    document = jobs.job_document(revision, JOB, "reviewed-code")
    spec = document["spec"]["template"]["spec"]
    task = spec["template"]["spec"]
    before, after = revision["spec"]["containers"][0], task["containers"][0]
    assert before["env"] == after["env"] and before["resources"] == after["resources"]
    assert task["maxRetries"] == 0 and task["timeoutSeconds"] == "300"
    assert spec["taskCount"] == spec["parallelism"] == 1
    assert "startupProbe" not in after and "ports" not in after
    assert after["args"] == ["-c", "reviewed-code"]
    changed = copy.deepcopy(document)
    changed["spec"]["template"]["spec"]["template"]["spec"]["maxRetries"] = 1
    with pytest.raises(pilot.ReleaseError, match="template_readback"):
        jobs.verify_job(changed, document)


def test_writer_child_precedes_profile_and_service_segments():
    _, revision, expected, report = setup()
    rows = revision["spec"]["containers"][0]["env"]
    rows[-1]["value"] = "gs://example-bucket/reports"
    expected["expected_report_prefix"] = "gs://example-bucket/reports/interactive_brokers/tqqq_growth_income/synthetic-service"
    uri = "gs://example-bucket/reports/maintenance-cash/interactive_brokers/tqqq_growth_income/synthetic-service/2026-09/report.json"
    namespace = jobs.report_namespace(revision, expected)
    assert namespace == "gs://example-bucket/reports/maintenance-cash"
    with pytest.raises(ValueError):
        jobs.publisher._source_report_uri(uri, expected["expected_report_prefix"] + "/maintenance-cash")
    result = jobs.inspect_report(report, uri, expected, job=JOB, execution=EXECUTION, started=NOW, now=NOW, namespace=namespace)
    assert result["status"] == "OBSERVED_JOB_ONLY"
    with pytest.raises(ValueError):
        jobs.inspect_report(report, uri.replace("synthetic-service/", "other-service/"), expected,
            job=JOB, execution=EXECUTION, started=NOW, now=NOW, namespace=namespace)


def test_completed_readback_never_creates_or_executes_job(monkeypatch):
    from scripts import read_completed_maintenance_cash_job as reader
    state, revision, expected, report = setup()
    cloud = Cloud(state, revision)
    monkeypatch.setattr(reader, "RUN", "123")
    monkeypatch.setattr(reader, "STARTED", NOW)
    monkeypatch.setattr(reader, "EXECUTION_SHA256", jobs.observer.digest(EXECUTION))
    def run(args, **kwargs):
        if args[1:3] == ["logging", "read"]:
            return json.dumps([dict(textPayload="execution_report " + URI,
                labels={"run.googleapis.com/execution_name": EXECUTION})])
        return cloud.run(args, **kwargs)
    result = reader.readback(expected, {**environment(), "MAINTENANCE_SOURCE_COMMIT": jobs.SOURCE,
        "MAINTENANCE_JOB": "false", "MAINTENANCE_JOB_READBACK": "true"}, run=run,
        snapshot=cloud.snapshot, load_report=lambda *_, **__: report, now=lambda: NOW)
    assert result["readback_only"] is True and result["broker_called"] is False
    assert result["source_workflow_run"] == "123"
    assert not any(row[1:3] == ["run", "jobs"] for row in cloud.calls)


@pytest.mark.parametrize("failure", [None, "identity", "method", "incomplete", "before_snapshot", "after_report", "invalid_fields"])
def test_summary_source_and_interval_are_independent_of_snapshot(failure):
    _, _, expected, report = setup()
    start, finish = NOW + timedelta(seconds=1), NOW + timedelta(seconds=1.5)
    summary = dict(source_method="ibkr.reqAccountSummary", account_ids=list(expected["expected_account_selector"]),
        query_completed=True, snapshot_atomic=False, observed_started_at=start.isoformat(), observed_finished_at=finish.isoformat(),
        broker_cash_observations=dict(schema_version="ibkr.cash_observations.v1", observed_at=finish.isoformat(),
            values=[dict(currency="USD", source_tag="SettledCash", value="5.0123")]))
    report["summary"]["broker_account_summary"] = summary
    if failure == "identity":
        summary["account_ids"] = ["DU00000099"]
    elif failure == "method":
        summary["source_method"] = "accountValues"
    elif failure == "incomplete":
        summary["query_completed"] = False
    elif failure == "before_snapshot":
        summary["observed_started_at"] = NOW.isoformat()
    elif failure == "after_report":
        summary["observed_finished_at"] = (NOW + timedelta(seconds=3)).isoformat()
    elif failure == "invalid_fields":
        summary["broker_cash_observations"]["values"][0]["value"] = "NaN"
    if failure is not None:
        with pytest.raises(pilot.ReleaseError, match="account_summary"):
            jobs.inspect_report(report, URI, expected, job=JOB, execution=EXECUTION, started=NOW, now=NOW)
    else:
        result = jobs.inspect_report(report, URI, expected, job=JOB, execution=EXECUTION, started=NOW, now=NOW)
        assert result["account_summary_cash_observations"]["fields"]["SettledCash"] == "OBSERVED"
        assert result["cash_observations"]["fields"]["SettledCash"] == "OBSERVED"
        assert report["summary"]["account_facts"]["broker_cash_observations"]["values"][0]["value"] == "0"
        assert summary["broker_cash_observations"]["values"][0]["value"] == "5.0123"
        assert result["spendable_cash_verified"] is result["complete_obligations_verified"] is False
