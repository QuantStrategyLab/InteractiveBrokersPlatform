from copy import deepcopy
from datetime import datetime, timezone
import pytest
from quant_platform_kit.notifications.digest_candidates import cycle_evidence, project_reports

TARGET = dict(platform_id="synthetic", strategy_profile="core", service_name="test-service",
    account_scope="test-scope", account_selector=["test-selector"], deployment_selector="test-slot", dry_run_only=False)
NOW = datetime(2026, 10, 16, 2, tzinfo=timezone.utc)


def report(run_id="cycle-1", orders=()):
    return dict(schema_version="runtime_report.v1", platform="synthetic", strategy_profile="core",
        service_name="test-service", runtime_target=deepcopy(TARGET), dry_run=False, status="ok", run_id=run_id,
        started_at="2026-10-16T01:00:00Z", summary={"notification_digest": cycle_evidence(
            {"execution_status": "completed", "broker_submission_done": bool(orders)}, orders)})


def project(reports):
    return project_reports(reports, expected_target=TARGET, opaque_account_uid="synthetic-uid",
        target_id="synthetic-target", now=NOW)["runs"]


def test_all_cycles_aggregate_without_inventing_fills():
    source = [report(), report("cycle-2", [{"status": "filled"}])]
    original = deepcopy(source)
    row = project(source)[0]
    assert (row["order_count"], row["fill_count"], row["cycle_count"], row["status"]) == (1, None, 2, "ok")
    assert source == original
    assert "account_scope" not in row


def test_missing_evidence_is_unknown_not_zero():
    source = report()
    source["summary"] = {}
    row = project([source])[0]
    assert row["fill_count"] is None and row["order_count"] is None and row["status"] == "alert"


@pytest.mark.parametrize("mutation", ["identity", "future", "duplicate", "error"])
def test_bad_source_stays_visible_or_rejects(mutation):
    source = report()
    if mutation == "error":
        source["status"] = "error"
        assert project([source])[0]["status"] == "alert"
        return
    if mutation == "identity": source["runtime_target"]["account_scope"] = "other"
    if mutation == "future": source["started_at"] = "2026-10-17T00:00:00Z"
    with pytest.raises(ValueError):
        project([source, deepcopy(source)] if mutation == "duplicate" else [source])


@pytest.mark.parametrize(("error", "reason"), [
    (ValueError("candidate_identity_missing"), "candidate_identity_missing"),
    (ValueError("private account detail"), "candidate_projection_failed"),
    (RuntimeError("private transport detail"), "candidate_projection_failed"),
])
def test_main_reports_allowlisted_reason_and_keeps_private_errors_generic(monkeypatch, capsys, error, reason):
    import json
    import notifications.digest_snapshot.digest_archive as archive
    def failed(_env):
        raise error
    monkeypatch.setattr(archive, "emit", failed)
    assert archive.main() == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "blocked", "uploaded": False, "reason": reason,
    }


def test_archive_default_off_and_disabled_target_never_read_or_upload():
    from notifications.digest_snapshot.digest_archive import emit
    def forbidden(*args): raise AssertionError("unexpected I/O")
    assert emit({}, read=forbidden, upload=forbidden)["status"] == "disabled"
    assert emit({"QSL_ROUTINE_NOTIFICATION_MODE": "daily_digest", "RUNTIME_TARGET_ENABLED": "false"},
        read=forbidden, upload=forbidden)["status"] == "target_disabled"


def test_archive_aggregates_window_and_uploads_once_without_private_output():
    import json
    from notifications.digest_snapshot.digest_archive import emit
    env = dict(QSL_ROUTINE_NOTIFICATION_MODE="daily_digest", RUNTIME_TARGET_ENABLED="true",
        RUNTIME_TARGET_JSON=json.dumps(TARGET), DIGEST_STORE_TARGET="synthetic-slot",
        DIGEST_OPAQUE_ACCOUNT_UID="synthetic-uid", DIGEST_TARGET_ID="synthetic-target",
        EXECUTION_REPORT_GCS_URI="gs://synthetic-reports/archive",
        DIGEST_CANDIDATES_GCS_URI="gs://synthetic-reports/candidates")
    reports = [report("first", [{"status": "filled"}]), report("second")]
    for source in reports: source["started_at"] = "2026-10-15T20:00:00Z"
    def read(args):
        if args[0] == "ls":
            return [{"url": f"gs://synthetic-reports/archive/synthetic/core/2026-10/{i}.json",
                     "metadata": {"updated": "2026-10-15T20:01:00Z"}} for i in range(2)]
        return reports[int(args[1].rsplit("/", 1)[-1].split(".")[0])]
    uploads = []
    result = emit(env, now=NOW, read=read, upload=lambda *args: uploads.append(args))
    assert result["runs"] == 2 and len(uploads) == 1
    assert "/2026-10-15/" in uploads[0][0]
    row = json.loads(uploads[0][1])["runs"][0]
    assert (row["order_count"], row["fill_count"], row["cycle_count"]) == (1, None, 2)
    calls = []
    def failed_upload(*args):
        calls.append(args)
        raise RuntimeError("unknown write outcome")
    with pytest.raises(RuntimeError): emit(env, now=NOW, read=read, upload=failed_upload)
    assert len(calls) == 1


def test_configured_closed_session_proves_idle_without_inventing_runs(monkeypatch):
    import json
    import quant_platform_kit.common.runtime_heartbeat_policy as policy
    from notifications.digest_snapshot.digest_archive import emit
    env = dict(QSL_ROUTINE_NOTIFICATION_MODE="daily_digest", RUNTIME_TARGET_ENABLED="true",
        RUNTIME_TARGET_JSON=json.dumps(TARGET), DIGEST_STORE_TARGET="synthetic-slot",
        DIGEST_OPAQUE_ACCOUNT_UID="synthetic-uid", DIGEST_TARGET_ID="synthetic-target",
        EXECUTION_REPORT_GCS_URI="gs://synthetic-reports/archive",
        DIGEST_CANDIDATES_GCS_URI="gs://synthetic-reports/candidates",
        DIGEST_NOTIFICATION_SCHEDULE_JSON=json.dumps({"scheduler": {"main_time": "0 20 * * 1-5", "timezone": "UTC"}}))
    monkeypatch.setattr(policy, "filter_due_targets", lambda *args, **kwargs: ([], True))
    uploads = []
    emit(env, now=NOW, read=lambda args: [], upload=lambda *args: uploads.append(args))
    row = json.loads(uploads[0][1])["runs"][0]
    assert row["actually_ran"] is False and "order_count" not in row and "fill_count" not in row
    monkeypatch.setattr(policy, "filter_due_targets", lambda *args, **kwargs: ([{}], True))
    with pytest.raises(ValueError, match="candidate_due_source_absent"):
        emit(env, now=NOW, read=lambda args: [], upload=lambda *args: uploads.append(args))
    assert len(uploads) == 1


def test_multi_target_bindings_use_each_explicit_enablement_without_a_global_target():
    import json
    from notifications.digest_snapshot.digest_archive import emit
    result = emit({"QSL_ROUTINE_NOTIFICATION_MODE": "daily_digest",
        "DIGEST_TARGET_BINDINGS_JSON": json.dumps([{"RUNTIME_TARGET_ENABLED": "false"}])},
        read=lambda args: pytest.fail("disabled target must not be read"),
        upload=lambda *args: pytest.fail("disabled target must not be uploaded"))
    assert result == {"status": "projected", "uploaded": False, "targets": 1}


def test_completed_order_count_is_not_used_as_execution_fill_count():
    source = report(orders=[{"status": "filled"}])
    row = project([source])[0]
    assert row["order_count"] == 1 and row["fill_count"] is None and row["status"] == "ok"
    fills = [{"execution_id": f"synthetic-fill-{i}"} for i in range(3)]
    row = project([report(orders=[{"status": "filled", "fills": fills}])])[0]
    assert row["order_count"] == 1 and row["fill_count"] == 3


def _probe_report(*, started_at, observed_at, finished_at, account_id="U00000001", equity="1234.50"):
    target = {**TARGET, "platform_id": "ibkr", "account_selector": [account_id], "dry_run_only": False}
    return {
        "schema_version": "runtime_report.v1",
        "platform": "ibkr",
        "strategy_profile": "core",
        "service_name": "test-service",
        "runtime_target": target,
        "dry_run": True,
        "status": "ok",
        "run_id": f"probe-{started_at}",
        "started_at": started_at,
        "finished_at": finished_at,
        "summary": {
            "buying_power": 500.0,
            "total_equity": float(equity),
            "positions_count": 1,
            "account_facts": {
                "schema_version": "ibkr_account_snapshot.v1",
                "account_ids": [account_id],
                "observed_at": observed_at,
                "currency": "USD",
                "net_assets": equity,
                "broker_reported_positions_scope": "stocks_only",
                "broker_reported_positions_complete": True,
                "broker_reported_positions": [{"symbol": "SPY", "quantity": "2", "market_value": "1000.00", "currency": "USD"}],
            },
        },
        "diagnostics": {},
    }


def test_probe_report_carries_snapshot_onto_scheduled_idle_and_is_excluded_from_cycles(monkeypatch):
    import json
    import quant_platform_kit.common.runtime_heartbeat_policy as policy
    from notifications.digest_snapshot.digest_archive import emit

    target = {**TARGET, "platform_id": "ibkr", "account_selector": ["U00000001"], "dry_run_only": False}
    env = dict(
        QSL_ROUTINE_NOTIFICATION_MODE="daily_digest",
        RUNTIME_TARGET_ENABLED="true",
        RUNTIME_TARGET_JSON=json.dumps(target),
        DIGEST_STORE_TARGET="synthetic-slot",
        DIGEST_OPAQUE_ACCOUNT_UID="synthetic-uid",
        DIGEST_TARGET_ID="synthetic-target",
        EXECUTION_REPORT_GCS_URI="gs://synthetic-reports/archive",
        DIGEST_CANDIDATES_GCS_URI="gs://synthetic-reports/candidates",
        DIGEST_NOTIFICATION_SCHEDULE_JSON=json.dumps({"scheduler": {"main_time": "0 20 * * 1-5", "timezone": "UTC"}}),
    )
    probe = _probe_report(
        started_at="2026-10-15T20:00:00Z",
        observed_at="2026-10-15T20:00:10Z",
        finished_at="2026-10-15T20:01:00Z",
    )
    monkeypatch.setattr(policy, "filter_due_targets", lambda *args, **kwargs: ([], True))

    def read(args):
        if args[0] == "ls":
            return [{"url": "gs://synthetic-reports/archive/ibkr/core/2026-10/probe.json",
                     "metadata": {"updated": "2026-10-15T20:01:30Z"}}]
        return probe

    uploads = []
    emit(env, now=NOW, read=read, upload=lambda *args: uploads.append(args))
    row = json.loads(uploads[0][1])["runs"][0]
    assert row["actually_ran"] is False
    assert row["scheduled_idle"] is True
    assert row["execution_status"] == "not_run"
    assert row["reason_code"] == "not_due"
    snap = row["account_daily_facts"]["snapshots"][0]
    assert snap["equity"] == 1234.5
    assert snap["holdings"] == [{"symbol": "SPY", "quantity": 2.0, "asset_type": "stock"}]


def test_off_schedule_dry_run_suppressed_on_live_target_and_uses_7day_carryover(monkeypatch):
    import json
    import quant_platform_kit.common.runtime_heartbeat_policy as policy
    from notifications.digest_snapshot.digest_archive import emit

    target = {**TARGET, "platform_id": "ibkr", "account_selector": ["U00000001"], "dry_run_only": False}
    env = dict(
        QSL_ROUTINE_NOTIFICATION_MODE="daily_digest",
        RUNTIME_TARGET_ENABLED="true",
        RUNTIME_TARGET_JSON=json.dumps(target),
        DIGEST_STORE_TARGET="synthetic-slot",
        DIGEST_OPAQUE_ACCOUNT_UID="synthetic-uid",
        DIGEST_TARGET_ID="synthetic-target",
        EXECUTION_REPORT_GCS_URI="gs://synthetic-reports/archive",
        DIGEST_CANDIDATES_GCS_URI="gs://synthetic-reports/candidates",
        DIGEST_NOTIFICATION_SCHEDULE_JSON=json.dumps({"scheduler": {"main_time": "0 20 * * 1-5", "timezone": "UTC"}}),
    )
    weekend_dry_run = {
        "schema_version": "runtime_report.v1",
        "platform": "ibkr",
        "strategy_profile": "core",
        "service_name": "test-service",
        "runtime_target": target,
        "dry_run": True,
        "status": "ok",
        "run_id": "dry-weekend",
        "started_at": "2026-10-15T20:00:00Z",
        "finished_at": "2026-10-15T20:01:00Z",
        "summary": {"execution_status": "dry_run_completed", "notification_digest": cycle_evidence({"execution_status": "dry_run_completed"}, [], dry_run=True)},
    }
    prior_probe = _probe_report(
        started_at="2026-10-13T18:00:00Z",
        observed_at="2026-10-13T18:00:05Z",
        finished_at="2026-10-13T18:01:00Z",
        equity="999.25",
    )
    monkeypatch.setattr(policy, "filter_due_targets", lambda *args, **kwargs: ([], True))

    def read(args):
        if args[0] == "ls":
            return [
                {"url": "gs://synthetic-reports/archive/ibkr/core/2026-10/weekend.json",
                 "metadata": {"updated": "2026-10-15T20:01:10Z"}},
                {"url": "gs://synthetic-reports/archive/ibkr/core/2026-10/prior.json",
                 "metadata": {"updated": "2026-10-13T18:01:10Z"}},
            ]
        return weekend_dry_run if args[1].endswith("weekend.json") else prior_probe

    uploads = []
    emit(env, now=NOW, read=read, upload=lambda *args: uploads.append(args))
    row = json.loads(uploads[0][1])["runs"][0]
    assert row["actually_ran"] is False
    assert row["scheduled_idle"] is True
    assert row["account_daily_facts"]["snapshots"][0]["equity"] == 999.25


def test_persist_archived_reports_carries_probe_snapshot_to_exec_run(monkeypatch):
    import json
    import notifications.unified_adapter as adapter

    target = {**TARGET, "platform_id": "ibkr", "account_selector": ["U00000001"], "dry_run_only": False}
    env = dict(
        RUNTIME_TARGET_JSON=json.dumps(target),
        DIGEST_OPAQUE_ACCOUNT_UID="synthetic-uid",
        DIGEST_TARGET_ID="synthetic-target",
        DIGEST_CANDIDATES_GCS_URI="gs://synthetic-reports/candidates",
        DIGEST_STORE_TARGET="synthetic-slot",
    )
    probe = _probe_report(
        started_at="2026-10-16T00:00:00Z",
        observed_at="2026-10-16T00:00:05Z",
        finished_at="2026-10-16T00:01:00Z",
    )
    exec_run = {
        "schema_version": "runtime_report.v1",
        "platform": "ibkr",
        "strategy_profile": "core",
        "service_name": "test-service",
        "runtime_target": target,
        "dry_run": False,
        "status": "ok",
        "run_id": "live-exec-1",
        "started_at": "2026-10-16T01:00:00Z",
        "finished_at": "2026-10-16T01:01:00Z",
        "summary": {
            "execution_status": "no_action",
            "notification_digest": cycle_evidence({"execution_status": "no_action", "broker_submission_done": False}, []),
        },
    }
    captured = []
    monkeypatch.setattr(adapter, "_checked_candidate", lambda candidate, **kwargs: captured.append((candidate, kwargs)) or True)
    res = adapter.persist_archived_reports([probe, exec_run], env, now=NOW)
    assert res == {"status": "projected", "uploaded": True, "runs": 1}
    assert len(captured) == 1
    assert captured[0][1]["dry_run"] is False
    assert captured[0][1]["account_snapshot"]["equity"] == 1234.5


def test_notify_account_facts_failure_redacts_private_details():
    from scripts.notify_account_facts_failure import build_failure_message

    msg = build_failure_message(
        "additional-1",
        "https://github.com/org/repo/actions/runs/123",
        reason="failed reading gs://private-bucket/path for U99999999 bot123456:SECRET_TOK Bearer xyz",
    )
    assert "gs://private-bucket" not in msg
    assert "U99999999" not in msg
    assert "SECRET_TOK" not in msg
    assert "Bearer xyz" not in msg
    assert "additional-1" in msg


def test_failed_off_schedule_dry_run_is_preserved_not_suppressed_as_idle(monkeypatch):
    import json
    import notifications.unified_adapter as adapter
    import quant_platform_kit.common.runtime_heartbeat_policy as policy
    from notifications.digest_snapshot.digest_archive import emit

    class State:
        def __init__(self):
            self.data = {}

        def read(self, key):
            return self.data.get(key, (None, None))

        def compare_and_set(self, key, version, value):
            if self.read(key)[0] != version:
                return False
            self.data[key] = ((version or 0) + 1, value)
            return True

    class Candidates:
        def __init__(self):
            self.payload = None

        def publish(self, key, revision, payload):
            self.payload = payload
            return True

    state, store = State(), Candidates()
    monkeypatch.setattr(adapter, "_state_store", lambda env=None: state)
    monkeypatch.setattr(adapter, "GCSCandidateStore", lambda *args, **kwargs: store)

    target = {**TARGET, "platform_id": "ibkr", "account_selector": ["U00000001"], "dry_run_only": False}
    env = dict(
        IBKR_UNIFIED_NOTIFICATION_ENABLED="true",
        QSL_ROUTINE_NOTIFICATION_MODE="daily_digest",
        RUNTIME_TARGET_ENABLED="true",
        RUNTIME_TARGET_JSON=json.dumps(target),
        DIGEST_STORE_TARGET="synthetic-slot",
        DIGEST_OPAQUE_ACCOUNT_UID="synthetic-uid",
        DIGEST_TARGET_ID="synthetic-target",
        EXECUTION_REPORT_GCS_URI="gs://synthetic-reports/archive",
        DIGEST_CANDIDATES_GCS_URI="gs://synthetic-reports/candidates",
        DIGEST_NOTIFICATION_SCHEDULE_JSON=json.dumps({"scheduler": {"main_time": "0 20 * * 1-5", "timezone": "UTC"}}),
    )
    failed_dry_run = {
        "schema_version": "runtime_report.v1",
        "platform": "ibkr",
        "strategy_profile": "core",
        "service_name": "test-service",
        "runtime_target": target,
        "dry_run": True,
        "status": "error",
        "run_id": "dry-weekend-failed",
        "started_at": "2026-10-15T20:00:00Z",
        "finished_at": "2026-10-15T20:01:00Z",
        "summary": {
            "execution_status": "error",
            "notification_digest": cycle_evidence({"execution_status": "error"}, [], dry_run=True),
        },
    }
    monkeypatch.setattr(policy, "filter_due_targets", lambda *args, **kwargs: ([], True))

    def read(args):
        if args[0] == "ls":
            return [{"url": "gs://synthetic-reports/archive/ibkr/core/2026-10/weekend-failed.json",
                     "metadata": {"updated": "2026-10-15T20:01:10Z"}}]
        return failed_dry_run

    result = emit(env, now=NOW, read=read)
    assert result == {"status": "projected", "uploaded": True, "runs": 1}
    row = store.payload["runs"][0]
    assert row["actually_ran"] is True
    assert row["status"] == "alert"
    assert row["source_run_id"] == "dry-weekend-failed"


def test_persist_candidate_updates_borrowed_probe_without_dropping_snapshot():
    from quant_platform_kit.notifications.unified_contract import NotificationEvent
    import notifications.unified_adapter as adapter

    class State:
        def __init__(self):
            self.data = {}
        def read(self, key):
            return self.data.get(key, (None, None))
        def compare_and_set(self, key, version, value):
            if self.read(key)[0] != version:
                return False
            self.data[key] = ((version or 0) + 1, value)
            return True

    class Candidates:
        def __init__(self):
            self.payload = None
        def publish(self, key, revision, payload):
            self.payload = payload
            return True

    state, store = State(), Candidates()
    env = {"DIGEST_STORE_TARGET": "synthetic-slot"}
    event = NotificationEvent(
        "routine_cycle",
        datetime(2026, 10, 16, 1, tzinfo=timezone.utc),
        platform_id="ibkr",
        opaque_account_uid="synthetic-uid",
        target_id="synthetic-target",
        strategy_profile="core",
        source_run_id="live-exec-1",
    )
    snap1 = {"as_of": "2026-10-16T00:00:05+00:00", "equity": 100.0, "equity_currency": "USD"}
    snap2 = {"as_of": "2026-10-16T00:05:05+00:00", "equity": 105.0, "equity_currency": "USD"}
    own = {"as_of": "2026-10-16T01:00:05+00:00", "equity": 110.0, "equity_currency": "USD"}

    assert adapter.persist_candidate(
        event.to_candidate(), state_store=state, candidate_store=store, env=env,
        account_snapshot=snap1, account_snapshot_borrowed=True,
    ) is True
    assert store.payload["runs"][0]["account_daily_facts"]["snapshots"] == [snap1]

    # A newer borrowed probe snapshot replaces the older borrowed one rather than blanking to None.
    assert adapter.persist_candidate(
        event.to_candidate(), state_store=state, candidate_store=store, env=env,
        account_snapshot=snap2, account_snapshot_borrowed=True,
    ) is True
    assert store.payload["runs"][0]["account_daily_facts"]["snapshots"] == [snap2]

    # An authoritative own snapshot supersedes any borrowed one, and later borrowed probes do not overwrite it.
    assert adapter.persist_candidate(
        event.to_candidate(), state_store=state, candidate_store=store, env=env,
        account_snapshot=own, account_snapshot_borrowed=False,
    ) is True
    assert adapter.persist_candidate(
        event.to_candidate(), state_store=state, candidate_store=store, env=env,
        account_snapshot={"as_of": "2026-10-16T01:30:05+00:00", "equity": 120.0, "equity_currency": "USD"},
        account_snapshot_borrowed=True,
    ) is True
    assert store.payload["runs"][0]["account_daily_facts"]["snapshots"] == [own]
