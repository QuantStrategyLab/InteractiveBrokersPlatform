"""Synthetic cloud commands and reports; no credentials, SDK or network."""
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import observe_maintenance_cash as observer
from scripts import deploy_pilot_no_traffic as pilot
from tests.test_pilot_no_traffic_release import baseline, COMMIT, CANDIDATE, SERVICE, SERVING, environment
from tests.test_publish_ibkr_account_facts import _report

NOW = datetime(2026, 9, 30, 1, 0, tzinfo=timezone.utc)
URI = "gs://example-bucket/tqqq_growth_income/2026-09/20260930T010000Z.json"


def inputs():
    state = baseline()
    target = pilot._runtime_target(state["serving_revision"])
    revision = copy.deepcopy(state["serving_revision"])
    revision["metadata"].update(name=CANDIDATE)
    revision["metadata"]["labels"]["commit-sha"] = COMMIT
    state["service"]["status"].update(latestCreatedRevisionName=CANDIDATE, latestReadyRevisionName=CANDIDATE)
    state["service"]["spec"]["template"] = {key: copy.deepcopy(revision[key]) for key in ("metadata", "spec")}
    state["scheduler"].append(dict(name="synthetic-warmup", state="ENABLED", httpTarget=dict(
        uri="https://synthetic.run.app/health", httpMethod="GET",
        oidcToken=dict(serviceAccountEmail="synthetic-caller", audience="https://synthetic.run.app"))))
    expected = dict(target_id="synthetic-primary", expected_report_prefix="gs://example-bucket/tqqq_growth_income",
        expected_project_id="synthetic-project", expected_service_name=SERVICE, expected_runtime_revision=SERVING,
        expected_account_scope=target["account_scope"], expected_account_selector=tuple(target["account_selector"]),
        expected_deployment_selector=target["deployment_selector"])
    report = _report()
    report.update(project_id=expected["expected_project_id"], service_name=SERVICE,
        account_scope=expected["expected_account_scope"], status="ok")
    report["runtime_target"].update(account_selector=list(expected["expected_account_selector"]),
        deployment_selector=expected["expected_deployment_selector"])
    report["diagnostics"]["runtime_revision"] = CANDIDATE
    facts = report["summary"]["account_facts"]
    facts["account_ids"] = list(expected["expected_account_selector"])
    facts["broker_cash_observations"] = dict(schema_version="ibkr.cash_observations.v1", observed_at=facts["observed_at"],
        values=[dict(currency="USD", source_tag="SettledCash", value="0")])
    return state, revision, expected, report


class Cloud:
    def __init__(self, state, revision, *, failure=None):
        self.state, self.revision = copy.deepcopy(state), revision
        self.calls, self.jobs, self.failure = [], [], failure

    def snapshot(self, *_):
        state = copy.deepcopy(self.state)
        state["traffic"], state["serving"] = pilot._traffic(state["service"])
        state["scheduler"] += copy.deepcopy(self.jobs)
        return state

    def run(self, args, **_):
        self.calls.append(args)
        if args[1:4] == ["run", "revisions", "describe"]:
            return json.dumps(self.revision)
        if args[1:4] == ["run", "services", "describe"]:
            return json.dumps(self.state["service"])
        if args[1:4] == ["run", "services", "update-traffic"]:
            rows = self.state["service"]["status"]["traffic"]
            if any(arg.startswith("--update-tags=") for arg in args):
                rows.append(dict(revisionName=CANDIDATE, percent=0, tag="c3f", url="https://c3f---synthetic.run.app"))
            else:
                rows[:] = [row for row in rows if row.get("tag") != "c3f"]
            return "{}"
        if args[1:4] == ["scheduler", "jobs", "list"]:
            return json.dumps(self.jobs)
        if args[1:4] == ["scheduler", "jobs", "describe"]:
            return json.dumps(self.jobs[0])
        if args[1:4] == ["scheduler", "jobs", "create"]:
            self.jobs.append(dict(name="ibkr-cash-read-123", state="ENABLED", schedule="03 01 30 09 *",
                timeZone="Etc/UTC", attemptDeadline="300s", retryConfig={}, httpTarget=dict(
                uri="https://c3f---synthetic.run.app/probe", httpMethod="POST",
                oidcToken=dict(serviceAccountEmail="synthetic-caller", audience="https://synthetic.run.app"))))
            if self.failure == "create_unknown":
                raise pilot.ReleaseError("scheduler_create_unknown")
            return "{}"
        if args[1:4] == ["scheduler", "jobs", "delete"]:
            if self.failure == "delete_unknown":
                raise pilot.ReleaseError("job_delete_unknown")
            self.jobs.clear()
            return "{}"
        if args[1:3] == ["logging", "read"]:
            if self.failure == "log_denied":
                raise pilot.ReleaseError("report_log_read_failed")
            if self.failure == "ambiguous":
                return json.dumps([dict(textPayload="execution_report " + uri) for uri in (URI, URI.replace("010000", "010001"))])
            return json.dumps([dict(textPayload="execution_report " + URI)])
        raise AssertionError("unexpected external command")


@pytest.mark.parametrize("failure", [None, "create_unknown", "delete_unknown", "log_denied", "ambiguous"])
def test_one_probe_path_and_owned_cleanup_without_strategy_or_retry(tmp_path, monkeypatch, failure):
    state, revision, expected, report = inputs()
    cloud = Cloud(state, revision, failure=failure)
    monkeypatch.setattr(observer.publisher, "_load_gcs_report", lambda *_, **__: report)
    receipt = tmp_path / "safe.json"
    result = observer.observe(expected, COMMIT, {**environment(), "CLOUD_RUN_REGION": "us-central1"}, receipt,
        run=cloud.run, snapshot=cloud.snapshot, now=lambda: NOW, sleep=lambda _: pytest.fail("unexpected wait"))
    assert result["status"] == ("OBSERVED_CANDIDATE_ONLY" if failure is None else "STOPPED")
    assert result["tag_cleanup_verified"] is True
    assert result["runtime_adopted"] is False and result["broker_write_requested"] is False
    assert result["baseline_restored"] is (failure != "delete_unknown")
    creates = [args for args in cloud.calls if args[1:4] == ["scheduler", "jobs", "create"]]
    assert len(creates) == 1 and "--max-retry-attempts=0" in creates[0]
    assert all("/run" not in arg and "/dry-run" not in arg and "/reconcile" not in arg
        for args in cloud.calls for arg in args)
    assert not any("--to-latest" in args or "set-iam-policy" in args for args in cloud.calls)
    assert "12345.6700" not in receipt.read_text() and "U00000001" not in receipt.read_text()
    if failure is None:
        assert result["cash_observations"]["fields"]["SettledCash"] == "OBSERVED"
        assert result["spendable_cash_verified"] is False and result["publisher_binding_changed"] is False


@pytest.mark.parametrize("change", ["identity", "env", "secret", "source", "serving", "not_ready"])
def test_unverified_candidate_stops_before_any_mutation(tmp_path, change):
    state, revision, expected, _ = inputs()
    if change == "identity":
        expected["expected_account_scope"] = "other"
    elif change == "env":
        revision["spec"]["containers"][0]["env"][1]["value"] = "other"
    elif change == "secret":
        revision["spec"]["containers"][0]["env"][2]["valueFrom"]["secretKeyRef"]["key"] = "2"
    elif change == "source":
        revision["metadata"]["labels"]["commit-sha"] = "f" * 40
    elif change == "serving":
        expected["expected_runtime_revision"] = CANDIDATE
    else:
        revision["status"]["conditions"] = []
    cloud = Cloud(state, revision)
    with pytest.raises(pilot.ReleaseError):
        observer.observe(expected, COMMIT, {**environment(), "CLOUD_RUN_REGION": "us-central1"}, tmp_path / "safe.json",
            run=cloud.run, snapshot=cloud.snapshot, now=lambda: NOW)
    assert len(cloud.calls) == 1  # Candidate metadata only; no scheduler or tags.


@pytest.mark.parametrize("change", ["account", "revision", "stale", "wrong_prefix"])
def test_projection_cannot_rebind_wrong_account_old_snapshot_or_other_prefix(change):
    _, _, expected, report = inputs()
    uri = URI
    if change == "account":
        report["summary"]["account_facts"]["account_ids"] = ["DU00000002"]
    elif change == "revision":
        report["diagnostics"]["runtime_revision"] = SERVING
    elif change == "stale":
        report["summary"]["account_facts"]["observed_at"] = (NOW - timedelta(days=2)).isoformat()
    else:
        uri = "gs://other-bucket/private.json"
    with pytest.raises(pilot.ReleaseError):
        observer.project(report, uri, expected, candidate=CANDIDATE, started=NOW, now=NOW + timedelta(seconds=2))


def test_candidate_workflow_preserves_existing_reader_and_safe_artifact_only():
    text = (Path(__file__).resolve().parents[1] / ".github/workflows/primary-account-facts-readonly.yml").read_text()
    assert "if: inputs.maintenance_source_commit == ''" in text
    assert "python3 -m scripts.diagnose_account_facts_identity --broker-contract-source-evidence" in text
    assert "path: ${{ runner.temp }}/maintenance-cash.safe.json" in text


def test_zero_traffic_retired_candidate_does_not_replace_service_ready_identity():
    state, revision, expected, _ = inputs()
    state["service"]["status"]["latestReadyRevisionName"] = SERVING
    revision["status"]["conditions"] = [{"type": "Ready", "status": "True", "reason": "Retired"},
        {"type": "Active", "status": "False", "reason": "Retired"}]
    assert observer.admit(expected, state, revision, COMMIT) == CANDIDATE


def test_realistic_run_identifier_fits_combined_tag_limit_without_collision():
    run_id = "40218930896"
    tag = observer.traffic_tag(run_id, "s" * 38)
    assert len(tag) == 8 and int(tag[1:], 36) == int(run_id)
    assert len(tag) + 38 == 46
    with pytest.raises(pilot.ReleaseError, match="candidate_tag_length_unsupported"):
        observer.traffic_tag(run_id, "s" * 39)
    assert tag != observer.traffic_tag(str(int(run_id) + 1), "s" * 38)


@pytest.mark.parametrize("key,value", [("GITHUB_RUN_ATTEMPT", "2"), ("GITHUB_REF", "refs/heads/other"),
    ("GITHUB_EVENT_NAME", "schedule"), ("GITHUB_REPOSITORY", "synthetic-fork/repo")])
def test_repeated_or_non_main_dispatch_stops_before_cloud_reads(tmp_path, key, value):
    state, revision, expected, _ = inputs()
    cloud = Cloud(state, revision)
    env = {**environment(), "CLOUD_RUN_REGION": "us-central1", key: value}
    with pytest.raises(pilot.ReleaseError, match="dispatch_not_admitted"):
        observer.observe(expected, COMMIT, env, tmp_path / "safe.json", run=cloud.run,
            snapshot=lambda *_: pytest.fail("external read before admission"))
    assert not cloud.calls
