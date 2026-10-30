"""Offline producer/marker/entrypoint integration; every IO dependency is fake."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from notifications import unified_adapter as adapter
from quant_platform_kit.notifications.unified_contract import NotificationEvent, CandidatePersistenceError
from notifications.events import NotificationPublisher, RenderedNotification
from scripts.project_digest_candidates import project_notification_events

NOW = datetime(2026, 10, 16, 20, 0, tzinfo=timezone.utc)
TARGET = {"platform_id": "ibkr", "strategy_profile": "synthetic_strategy", "service_name": "synthetic-service",
          "account_scope": "synthetic-scope", "account_selector": ["synthetic-selector"],
          "deployment_selector": "synthetic-slot", "dry_run_only": False}


class StateStore:
    def __init__(self, backing=None):
        self.backing = {} if backing is None else backing
    def read(self, key):
        return deepcopy(self.backing.get(key, (None, None)))
    def compare_and_set(self, key, version, value):
        current, _ = self.read(key)
        if current != version:
            return False
        self.backing[key] = ((version or 0) + 1, deepcopy(value))
        return True


class CandidateStore:
    def __init__(self):
        self.rows = {}
        self.writes = []
        self.ack = True
    def publish(self, key, revision, payload):
        self.writes.append((key, revision, deepcopy(payload)))
        if self.ack:
            current = self.rows.get(key, (0, None))
            if revision >= current[0]:
                self.rows[key] = (revision, deepcopy(payload))
        return self.ack


@pytest.fixture
def routing(monkeypatch):
    monkeypatch.setenv(adapter.FLAG, "true")
    monkeypatch.setenv("QSL_ROUTINE_NOTIFICATION_MODE", "immediate")
    monkeypatch.delenv("QSL_DIGEST_ROUTINE_ORDERS", raising=False)
    monkeypatch.setenv("RUNTIME_TARGET_JSON", json.dumps(TARGET))
    monkeypatch.setenv("DIGEST_OPAQUE_ACCOUNT_UID", "synthetic-account")
    monkeypatch.setenv("DIGEST_TARGET_ID", "synthetic-target")
    monkeypatch.setenv("DIGEST_STORE_TARGET", "synthetic-slot")
    state, candidates = StateStore(), CandidateStore()
    monkeypatch.setattr(adapter, "_state_store", lambda env=None: StateStore(state.backing))
    monkeypatch.setattr(adapter, "GCSCandidateStore", lambda *a, **kw: candidates)
    return state, candidates


def report(run="source-run", **kwargs):
    return {"run_id": run, "started_at": NOW.isoformat(), "dry_run": False, "runtime_target": deepcopy(TARGET), **kwargs}


def event(kind="routine_cycle", run="source-run", **kwargs):
    return NotificationEvent(kind, NOW, platform_id="ibkr", opaque_account_uid="synthetic-account",
                             target_id="synthetic-target", strategy_profile="synthetic_strategy",
                             source_run_id=run, incident_reason=kind, **kwargs)


def cycle(execution, *, source=None):
    sends = []
    publisher = NotificationPublisher(log_message=lambda _: None, send_message=lambda text: sends.append(text) or True)
    result = adapter.with_run(source or report(), adapter.publish_cycle, publisher,
        notification=RenderedNotification("details", "compact"), execution=deepcopy(execution))
    return result, sends


@pytest.mark.parametrize("flags", [["risk_gate:passed"], ("risk_gate:passed",)])
def test_approved_gate_marker_is_routine_notification_evidence(routing, flags):
    execution = {"execution_status": "no_op", "no_op_reason": "target_diff_below_threshold",
                 "risk_gate": "APPROVE", "risk_flags": flags,
                 "orders_submitted": [], "orders_pending": [], "orders_filled": []}
    original = deepcopy(execution)
    assert adapter._kind(execution) == "routine_cycle"
    result, sends = cycle(execution)
    assert result and not sends
    assert execution == original
    payload = next(iter(routing[1].rows.values()))[1]
    assert {event["event_kind"] for row in payload["runs"] for event in row["notification_events"]} == {"routine_cycle"}


@pytest.mark.parametrize("extra", [
    {"risk_gate": "BLOCK"},
    {"risk_gate": None},
    {"risk_flags": "risk_gate:passed"},
    {"risk_flags": ["risk_gate:passed", "synthetic_warning"]},
    {"risk_flags": ["unknown_flag"]},
    {"account_new_risk_buy_blocked": True},
    {"small_account_buy_blocked": True},
    {"execution_blocked_reason": "synthetic_restriction"},
    {"error": "synthetic_failure"},
    {"orders_pending": [{"status": "PendingSubmit"}]},
])
def test_approval_marker_never_clears_other_attention(routing, extra):
    execution = {"execution_status": "no_op", "no_op_reason": "target_diff_below_threshold",
                 "risk_gate": "APPROVE", "risk_flags": ["risk_gate:passed"], **extra}
    original = deepcopy(execution)
    assert adapter._kind(execution) != "routine_cycle"
    assert execution == original


@pytest.mark.parametrize("flag", [None, "", "false", "1", "yes", "typo", False])
def test_unset_empty_invalid_flags_delegate_without_writes(monkeypatch, flag):
    env = {} if flag is None else {adapter.FLAG: flag}
    assert not adapter.enabled(env)
    assert adapter.effective_mode(env) == "immediate"
    assert adapter.effective_mode({**env, "QSL_ROUTINE_NOTIFICATION_MODE": "daily_digest"}) == "daily_digest"


def test_routine_persists_before_deferral_and_exports_unique_runs(routing):
    state, candidates = routing
    summary = {"execution_status": "completed", "orders_submitted": [], "orders_filled": [], "orders_pending": [],
               "orders_partially_filled": [], "option_orders_filled": [], "option_orders_pending": [],
               "option_orders_partially_filled": []}
    assert cycle(summary)[1] == []
    assert cycle(summary)[1] == []
    assert cycle(summary, source=report("second-run"))[1] == []
    rows = next(iter(candidates.rows.values()))[1]["runs"]
    assert {row["source_run_id"] for row in rows} == {"source-run", "second-run"}
    assert len(rows) == 2
    for row in rows:
        assert (row["cycle_count"], row["order_count"], row["fill_count"]) == (1, 0, 0)
        assert row["actually_ran"] is True
        assert {event["source_run_id"] for event in row["notification_events"]} == {row["source_run_id"]}
    assert len([v for _, v in state.backing.values() if "events" in v][0]["events"]) == 2


@pytest.mark.parametrize("locale,notice", [("zh", "汇总候选写入未确认"), ("en", "Digest candidate write unconfirmed")])
def test_failed_candidate_sends_only_persistence_error(routing, monkeypatch, locale, notice):
    routing[1].ack = False
    monkeypatch.setenv("NOTIFY_LANG", locale)
    result, sent = cycle({"execution_status": "no_op", "orders_submitted": []})
    assert result is False and notice in sent[0]
    assert "compact" not in sent[0]


@pytest.mark.parametrize("kind,summary", [
    ("auth_failure", {"auth_expired": True}),
    ("real_error", {"execution_status": "error", "errors": ["synthetic_failure"]}),
    ("order_status_unknown", {"execution_status": "pending_reconciliation", "orders_pending": [{"status": "pending"}]}),
    ("blocked_run", {"execution_status": "blocked", "no_op_reason": "strategy_risk_rejected"}),
])
def test_urgent_events_dedupe_across_runs_and_recovery_allows_new_alert(routing, kind, summary):
    assert adapter._kind(summary) == kind
    assert cycle(summary)[1] == ["compact"]
    assert cycle(summary, source=report("second-run"))[1] == []
    recovery = report("recovered", started_at=(NOW + timedelta(minutes=1)).isoformat())
    assert cycle({"execution_status": "completed", "orders_submitted": []}, source=recovery)[1] == []
    assert cycle(summary, source=report("worse-again", started_at=(NOW + timedelta(minutes=2)).isoformat()))[1] == ["compact"]


def test_real_order_and_fill_counts_are_deferred_and_unknown_is_preserved(routing):
    order = {"status": "filled", "broker_order_id": "synthetic-order", "symbol": "AAA"}
    summary = {"execution_status": "completed", "orders_submitted": [], "orders_filled": [order],
               "orders_pending": [], "orders_partially_filled": [], "option_orders_filled": [],
               "option_orders_pending": [], "option_orders_partially_filled": []}
    assert cycle(summary)[1] == []
    row = next(iter(routing[1].rows.values()))[1]["runs"][0]
    assert row["order_count"] == 1 and row["fill_count"] is None
    assert cycle(summary)[1] == []
    assert next(iter(routing[1].rows.values()))[1]["runs"][0]["order_count"] == 1


def test_dry_run_is_candidate_coverage_without_real_order_or_fill_counts(routing):
    summary = {"execution_status": "dry_run_completed", "orders_submitted": [{"status": "dry_run"}]}
    assert cycle(summary, source=report(dry_run=True))[1] == []
    row = next(iter(routing[1].rows.values()))[1]["runs"][0]
    assert row["execution_mode"] == "dry_run"
    assert row["order_count"] is None and row["fill_count"] is None


def test_marker_uncertain_ack_is_never_fabricated_or_retried(routing):
    state = routing[0]
    calls = []
    source = event("real_error")
    assert adapter.send_incident(source, lambda: calls.append(1) or True, family="cycle", store=state)
    marker = next(v for _, v in state.backing.values() if "delivery_status" in v)
    assert marker["delivery_status"] == "uncertain" and marker["receipt_id"] is None
    assert adapter.send_incident(replace(source, source_run_id="new-run"), lambda: calls.append(2) or True, family="cycle", store=StateStore(state.backing))
    assert calls == [1]


def test_unique_transport_receipt_is_the_only_delivered_marker(routing):
    state = routing[0]
    assert adapter.immediate_incident(event("real_error"), lambda: {"acknowledged": True, "receipt_id": "synthetic-receipt"}, store=state)
    marker = next(v for _, v in state.backing.values() if "delivery_status" in v)
    assert marker["delivery_status"] == "delivered" and marker["receipt_id"] == "synthetic-receipt"


def test_marker_read_error_never_falls_back_to_untracked_send(routing, monkeypatch):
    class Broken:
        def read(self, key):
            raise OSError("unavailable")
    monkeypatch.setattr(adapter, "_state_store", lambda env=None: Broken())
    assert cycle({"execution_status": "error"}) == (False, [])


def test_unknown_identity_keeps_routine_visible_without_zero_counts(routing, monkeypatch):
    monkeypatch.delenv("DIGEST_OPAQUE_ACCOUNT_UID")
    result, sends = cycle({"execution_status": "no_op"})
    assert result is False and len(sends) == 1
    assert "compact" not in sends[0]
    assert routing[1].rows == {}


def test_different_accounts_and_targets_never_share_incidents(routing):
    state = routing[0]
    calls = []
    source = event("auth_failure")
    for account, target in [("a", "one"), ("b", "one"), ("a", "two")]:
        adapter.send_incident(replace(source, opaque_account_uid=account, target_id=target), lambda: calls.append(1) or True,
                              family="cycle", store=state)
    assert len(calls) == 3


def test_stale_recovery_is_rejected_and_does_not_reopen_a_claim(routing):
    state = routing[0]
    source = event("real_error")
    adapter.send_incident(source, lambda: True, family="cycle", store=state)
    with pytest.raises(RuntimeError, match="observation_stale"):
        adapter.recover_incidents(replace(source, occurred_at=NOW - timedelta(seconds=1)), family="cycle", store=state)
    calls = []
    adapter.send_incident(source, lambda: calls.append(1) or True, family="cycle", store=state)
    assert calls == []


def test_same_run_monitor_and_cycle_are_one_run_conflicts_stay_unknown(routing):
    candidates = routing[1]
    original = event(order_count=1, fill_count=None).to_candidate()
    assert adapter.persist_candidate(original)
    monitored = event("healthy_heartbeat", order_count=1, fill_count=None).to_candidate()
    assert adapter.persist_candidate(monitored)
    row = next(iter(candidates.rows.values()))[1]["runs"][0]
    assert row["cycle_count"] == 1 and row["order_count"] == 1 and row["fill_count"] is None
    assert adapter.persist_candidate({**original, "order_count": 2})
    row = next(iter(candidates.rows.values()))[1]["runs"][0]
    assert row["order_count"] is None and row["status"] == "alert"


def test_new_risk_severity_and_proven_recovery_are_persistent(routing):
    calls = []
    def publish():
        calls.append(1)
        return {"sent": 1, "skipped": 0, "failed": 0}
    def observe(level, recovery=False):
        decision = SimpleNamespace(level=SimpleNamespace(value=level))
        return adapter.with_run(report(), adapter.publish_risk_attention, decision, publish,
                                profile=TARGET["strategy_profile"], reasons=("synthetic_guard",), recovery_proven=recovery)
    assert observe("action")["sent"] == 1
    assert observe("action")["skipped"] == 1
    assert observe("halt")["sent"] == 1
    observe("ok")
    assert observe("halt")["skipped"] == 1
    observe("ok", recovery=True)
    assert observe("action")["sent"] == 1
    assert calls == [1, 1, 1]


def test_half_open_sgt_window_and_same_run_deduplication(routing):
    source = event()
    before = replace(source, occurred_at=datetime.fromisoformat("2026-10-17T05:59:59+08:00"))
    cutoff = replace(source, occurred_at=datetime.fromisoformat("2026-10-17T06:00:00+08:00"), source_run_id="next-window")
    assert before.to_candidate()["business_day"] == "2026-10-16"
    assert cutoff.to_candidate()["business_day"] == "2026-10-17"
    for item in (before, cutoff):
        adapter.persist_candidate(item.to_candidate())
    assert len(routing[1].rows) == 2


@pytest.mark.parametrize("case", ["heartbeat", "no_order", "filled", "pending", "error"])
def test_opted_in_cycle_keeps_all_broker_and_persistence_calls(routing, monkeypatch, case):
    from tests.test_notif_consolidation_equivalence import capture_cycle
    baseline = json.loads((__import__("pathlib").Path(__file__).parent / "fixtures/notif_main_2f16cb9.json").read_text())
    observed = adapter.with_run(report(), capture_cycle, monkeypatch, case, "en")
    def keep(data):
        return [call for call in data["calls"] if call[0] != "send"]
    expected = baseline[f"cycle:{case}:en"]
    for call in expected["calls"]:
        if call[0] == "execute":
            call[-1]["account_execution_session"] = None
    assert keep(observed) == keep(expected)
    assert observed["result"] == baseline[f"cycle:{case}:en"]["result"]


def test_candidate_failure_is_one_persistent_incident_and_recovers(routing):
    summary = {"execution_status": "no_op", "orders_submitted": []}
    routing[1].ack = False
    assert len(cycle(summary)[1]) == 1
    assert cycle(summary, source=report("another-run"))[1] == []
    routing[1].ack = True
    assert cycle(summary, source=report("healthy", started_at=(NOW + timedelta(minutes=1)).isoformat()))[1] == []
    routing[1].ack = False
    assert len(cycle(summary, source=report("failed-again", started_at=(NOW + timedelta(minutes=2)).isoformat()))[1]) == 1


def test_binding_mismatch_does_not_fall_back_to_another_accounts_aliases(routing, monkeypatch):
    monkeypatch.setenv("IBKR_DIGEST_OPAQUE_ACCOUNT_UID", "other-account")
    monkeypatch.setenv("IBKR_DIGEST_TARGET_ID", "other-target")
    identity, _ = adapter._identity({**TARGET, "account_selector": ["different-selector"]})
    assert identity["opaque_account_uid"] is None and identity["target_id"] is None


def test_new_risk_caller_preserves_its_admission_and_uses_durable_dedupe(routing):
    from application.account_new_risk_gate_support import maybe_publish_attention_for_admission
    from quant_platform_kit.risk.account_new_risk_gate import NewRiskAdmissionResult, NewRiskDisposition
    admission = NewRiskAdmissionResult(NewRiskDisposition.NEW_RISK_PROHIBITED, ("EQUITY_UNKNOWN_FAIL_CLOSED",))
    original = deepcopy(admission)
    calls = []
    def observe(run):
        return adapter.with_run(report(run), maybe_publish_attention_for_admission, admission,
            portfolio={"strategy_profile": TARGET["strategy_profile"]},
            telegram_sender=lambda **kw: calls.append(kw) or True, log_message=lambda *a, **kw: None)
    assert observe("first")["sent"] == 1
    assert observe("second")["skipped"] == 1
    assert len(calls) == 1 and admission == original


class BlobFactory:
    def __init__(self):
        self.objects = {}
        self.uploads = []
        self.unknown = False
    def __call__(self, uri, *, client):
        factory = self
        from google.api_core.exceptions import NotFound, PreconditionFailed
        class Blob:
            generation = None
            metadata = None
            def reload(self, **kwargs):
                if uri not in factory.objects:
                    raise NotFound("missing")
                self.generation, self.metadata, _ = deepcopy(factory.objects[uri])
            def download_as_bytes(self, **kwargs):
                self.reload(**kwargs)
                return factory.objects[uri][2].encode()
            def upload_from_string(self, body, *, if_generation_match, **kwargs):
                factory.uploads.append((uri, if_generation_match, kwargs))
                current = factory.objects.get(uri, (0, None, None))[0]
                if if_generation_match != current:
                    raise PreconditionFailed("CAS rejected")
                factory.objects[uri] = (current + 1, self.metadata, body)
                if factory.unknown:
                    raise TimeoutError("write outcome unknown")
        return Blob()


def test_real_gcs_adapter_uses_atomic_generations_and_readback_only(monkeypatch):
    factory = BlobFactory()
    store = adapter.GCSMarkerStore("gs://synthetic/notification-state", client=object())
    store.blob_factory = factory
    assert store.read("key") == (None, None)
    assert store.compare_and_set("key", None, {"state": "pending"})
    assert store.read("key") == (1, {"state": "pending"})
    assert not store.compare_and_set("key", None, {"state": "duplicate"})
    cold = adapter.GCSMarkerStore("gs://synthetic/notification-state", client=object())
    cold.blob_factory = factory
    assert cold.read("key") == (1, {"state": "pending"})
    assert all(kwargs["retry"] is None for _, _, kwargs in factory.uploads)


def test_candidate_cas_refuses_cross_account_slot_and_unknown_write_is_not_retried():
    factory = BlobFactory()
    store = adapter.GCSCandidateStore("gs://synthetic/candidates", client=object())
    store.blob_factory = factory
    body = project_notification_events([{"event": event().to_candidate(), "actually_ran": True, "dry_run": False}])
    assert store.publish("ibkr/2026-10-16/slot", 1, body)
    assert store.publish("ibkr/2026-10-16/slot", 1, body)
    assert len(factory.uploads) == 1
    wrong = deepcopy(body)
    wrong["runs"][0]["target_id"] = "other-target"
    with pytest.raises(ValueError, match="identity_conflict"):
        store.publish("ibkr/2026-10-16/slot", 2, wrong)
    factory.unknown = True
    with pytest.raises(TimeoutError):
        store.publish("ibkr/2026-10-16/slot", 2, body)
    assert len(factory.uploads) == 2


def test_duplicate_observation_advances_cursor_before_recovery(routing):
    source = event("real_error")
    store = routing[0]
    adapter.send_incident(source, lambda: True, family="cycle", store=store)
    adapter.send_incident(replace(source, occurred_at=NOW + timedelta(minutes=2)), lambda: True, family="cycle", store=store)
    with pytest.raises(RuntimeError, match="observation_stale"):
        adapter.recover_incidents(replace(source, occurred_at=NOW + timedelta(minutes=1)), family="cycle", store=store)


def healthy_receipt_report():
    from quant_platform_kit.common.execution_receipts import build_execution_receipt
    payload = report(schema_version="runtime_report.v1", platform="ibkr", strategy_profile=TARGET["strategy_profile"],
                     service_name=TARGET["service_name"], status="ok", errors=[], diagnostics={},
                     finished_at=(NOW + timedelta(seconds=1)).isoformat(),
                     summary={"execution_status": "no_action", "orders_submitted_count": 0,
                              "notification_digest": {"schema_version": "qsl.notification_cycle.v1", "dry_run": False,
                                                      "routine": True, "order_count": 0, "fill_count": None}})
    payload["execution_receipt"] = build_execution_receipt(
        platform="ibkr", strategy_profile=TARGET["strategy_profile"], strategy_revision="a" * 40,
        execution_mode="paper", outcome="no_action", observed_at=NOW)
    return payload


def setup_heartbeat(monkeypatch, payload):
    from scripts import execution_report_heartbeat as heartbeat
    monkeypatch.setenv("RUNTIME_TARGET_ENABLED", "true")
    monkeypatch.setenv("RUNTIME_HEARTBEAT_NOTIFY_ON_SUCCESS", "true")
    monkeypatch.setenv("RUNTIME_HEARTBEAT_FAIL_WORKFLOW_ON_ALERT", "true")
    monkeypatch.setattr(heartbeat, "load_runtime_targets", lambda env: [])
    monkeypatch.setattr(heartbeat, "_hydrate_runtime_target_schedules", lambda targets, **kw: targets)
    monkeypatch.setattr(heartbeat, "_runtime_target_scheduler_skip_reason", lambda *a: None)
    monkeypatch.setattr(heartbeat, "_resolve_required_services", lambda **kw: ([], None, True))
    monkeypatch.setattr(heartbeat, "_report_globs", lambda *a: ["synthetic-reports"])
    monkeypatch.setattr(heartbeat, "_list_gcs_objects", lambda *a, **kw: [] if payload is None else [
        {"url": "gs://synthetic/reports/a.json", "metadata": {"updated": (NOW + timedelta(seconds=2)).isoformat()}},
    ])
    monkeypatch.setattr(heartbeat, "_cat_gcs_json", lambda *a, **kw: deepcopy(payload))
    sends = []
    monkeypatch.setattr(heartbeat, "_send_telegram", lambda message, **kw: sends.append(message) or True)
    return heartbeat, sends


def test_heartbeat_main_healthy_receipt_is_candidate_not_a_send(routing, monkeypatch):
    payload = healthy_receipt_report()
    heartbeat, sends = setup_heartbeat(monkeypatch, payload)
    assert heartbeat._is_accepted_report(payload)[0]
    assert heartbeat._is_quiet_report(payload, now=NOW + timedelta(minutes=1))
    assert heartbeat.main(now=NOW + timedelta(minutes=1)) == 0
    assert sends == []
    row = next(iter(routing[1].rows.values()))[1]["runs"][0]
    assert row["actually_ran"] and row["cycle_count"] == 1 and row["fill_count"] is None


@pytest.mark.parametrize("case", ["missing", "unacceptable", "pending", "auth"])
def test_heartbeat_alerts_are_immediate_and_persistent_without_changing_acceptance(routing, monkeypatch, case):
    payload = healthy_receipt_report()
    if case == "missing":
        payload.pop("execution_receipt")
    elif case == "unacceptable":
        payload["summary"].update(execution_status="blocked", no_op_reason="strategy_risk_rejected")
    elif case == "pending":
        payload["summary"].update(execution_status="pending", orders_pending_count=1)
    else:
        payload["summary"]["auth_expired"] = True
    heartbeat, sends = setup_heartbeat(monkeypatch, payload)
    acceptance = heartbeat._is_accepted_report(payload)
    exit_before = 0 if acceptance[0] else 1
    assert heartbeat.main(now=NOW + timedelta(minutes=1)) == exit_before
    assert len(sends) == 1
    assert heartbeat.main(now=NOW + timedelta(minutes=2)) == exit_before
    assert len(sends) == 1
    assert heartbeat._is_accepted_report(payload) == acceptance


def test_missing_report_is_immediate_recovery_then_new_failure_sends_again(routing, monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "synthetic-workflow")
    heartbeat, sends = setup_heartbeat(monkeypatch, None)
    assert heartbeat.main(now=NOW + timedelta(minutes=1)) == 1
    assert len(sends) == 1
    assert heartbeat.main(now=NOW + timedelta(minutes=2)) == 1
    assert len(sends) == 1
    heartbeat, healthy_sends = setup_heartbeat(monkeypatch, healthy_receipt_report())
    assert heartbeat.main(now=NOW + timedelta(minutes=3)) == 0
    assert healthy_sends == []
    heartbeat, next_sends = setup_heartbeat(monkeypatch, None)
    assert heartbeat.main(now=NOW + timedelta(minutes=4)) == 1
    assert len(next_sends) == 1
    rows = next(iter(routing[1].rows.values()))[1]["runs"]
    assert {row["source_run_id"] for row in rows} == {"source-run"}
    assert all(row["actually_ran"] is True for row in rows)


def test_daily_drill_alert_main_is_deduped_and_preserves_exit_one(routing, monkeypatch):
    from scripts import daily_dry_run_digest as drill
    monkeypatch.setenv("GITHUB_RUN_ID", "synthetic-workflow")
    monkeypatch.delenv("DRILL_DIGEST_PREVIEW", raising=False)
    target = drill.DrillTarget("Example", TARGET["strategy_profile"], TARGET["account_scope"], TARGET["service_name"], "0 1 * * *", "UTC")
    monkeypatch.setattr(drill, "_targets", lambda: [target])
    monkeypatch.setattr(drill, "_target_status", lambda *a: (NOW.date(), "synthetic failure", False))
    sends = []
    monkeypatch.setattr(drill, "_send_telegram", lambda text, **kw: sends.append(text) or True)
    assert drill.main(now=NOW) == 1
    assert drill.main(now=NOW + timedelta(minutes=1)) == 1
    assert len(sends) == 1


def test_daily_drill_real_checker_routes_healthy_dry_run_evidence(routing, monkeypatch):
    from scripts import daily_dry_run_digest as drill
    target = drill.DrillTarget("Example", TARGET["strategy_profile"], TARGET["account_scope"], TARGET["service_name"], "0 20 * * *", "UTC")
    payload = healthy_receipt_report()
    payload["dry_run"] = True
    payload["account_scope"] = target.scope
    payload["summary"]["notification_digest"]["dry_run"] = True
    monkeypatch.setattr(drill, "_targets", lambda: [target])
    monkeypatch.setattr(drill, "_service_is_drill_only", lambda _: (True, "synthetic-service-url"))
    monkeypatch.setattr(drill, "_today_reports", lambda *a: [deepcopy(payload)])
    monkeypatch.delenv("DRILL_DIGEST_PREVIEW", raising=False)
    def job(name):
        if name == target.live_job:
            return {"state": "PAUSED"}
        return {"state": "ENABLED", "schedule": target.schedule, "timeZone": target.timezone,
                "lastAttemptTime": NOW.isoformat(), "status": {},
                "httpTarget": {"uri": "synthetic-service-url/dry-run", "httpMethod": "POST",
                               "oidcToken": {"serviceAccountEmail": "synthetic-principal", "audience": "synthetic-service-url"}}}
    monkeypatch.setattr(drill, "_job", job)
    sends = []
    monkeypatch.setattr(drill, "_send_telegram", lambda text, **kw: sends.append(text) or True)
    assert drill.main(now=NOW + timedelta(minutes=1)) == 0
    assert sends == []
    row = next(iter(routing[1].rows.values()))[1]["runs"][0]
    assert row["actually_ran"] is True and row["execution_mode"] == "dry_run"
    assert row["order_count"] is None and row["fill_count"] is None


def test_archive_opt_in_bridges_existing_full_window_producer_without_double_count(routing, monkeypatch):
    import os
    from notifications.digest_snapshot.digest_archive import emit
    payload = healthy_receipt_report()
    env = {**os.environ, "RUNTIME_TARGET_ENABLED": "true", "EXECUTION_REPORT_GCS_URI": "gs://synthetic/archive",
           "DIGEST_CANDIDATES_GCS_URI": "gs://synthetic/candidates"}
    def read(args):
        if args[0] == "ls":
            return [{"url": "gs://synthetic/archive/ibkr/strategy/2026-10/run.json",
                     "metadata": {"updated": NOW.isoformat()}}]
        return deepcopy(payload)
    def no_legacy_upload(*args):
        raise AssertionError("opted-in producer must use the shared durable bridge")
    for _ in range(2):
        assert emit(env, now=NOW + timedelta(hours=2, minutes=20), read=read, upload=no_legacy_upload)["uploaded"]
    row = next(iter(routing[1].rows.values()))[1]["runs"][0]
    assert row["cycle_count"] == 1 and row["order_count"] == 0 and row["fill_count"] is None


def test_no_due_heartbeat_is_silent_and_does_not_invent_a_run(routing):
    from scripts import execution_report_heartbeat as heartbeat
    heartbeat._notify_normal_heartbeat("Example", "not due", quiet_eligible=True)
    assert routing[1].rows == {} and routing[0].backing == {}


def test_one_unhealthy_monitor_does_not_page_for_a_healthy_run(routing, monkeypatch):
    from scripts import execution_report_heartbeat as heartbeat
    healthy = healthy_receipt_report()
    failed = deepcopy(healthy)
    failed["run_id"] = "other-run"
    failed["summary"]["auth_expired"] = True
    sends = []
    monkeypatch.setattr(heartbeat, "_send_telegram", lambda text, **kw: sends.append(text) or True)
    heartbeat._notify_normal_heartbeat("Example", "details", quiet_eligible=False,
                                     reports=[(healthy, True), (failed, False)], now=NOW + timedelta(minutes=1))
    assert len(sends) == 1


def test_conflicting_known_counts_from_two_event_kinds_are_an_explicit_alert(routing):
    adapter.persist_candidate(event(order_count=1).to_candidate())
    adapter.persist_candidate(event("healthy_heartbeat", order_count=2).to_candidate())
    row = next(iter(routing[1].rows.values()))[1]["runs"][0]
    assert row["status"] == "alert" and row["order_count"] is None and row["cycle_count"] == 1


def test_primary_runtime_failure_sender_is_also_deduped(routing, monkeypatch, strategy_module):
    calls = []
    monkeypatch.setattr(strategy_module, "publish_notification", lambda **kwargs: calls.append(kwargs) or True)
    # The main fixture supplies an inert target; bind the notification scope explicitly.
    monkeypatch.setenv("RUNTIME_TARGET_JSON", json.dumps(TARGET))
    for _ in range(2):
        assert strategy_module._publish_runtime_failure_notification(detailed_text="details", compact_text="compact", exc=RuntimeError("synthetic"))
    assert len(calls) == 1


def test_archive_accepts_actual_report_platform_path_and_keeps_dry_runs_separate(routing, monkeypatch):
    import os
    from notifications.digest_snapshot.digest_archive import emit
    payload = healthy_receipt_report()
    payload.update(platform="interactive_brokers", dry_run=True)
    payload["summary"]["notification_digest"]["dry_run"] = True
    env = {**os.environ, "RUNTIME_TARGET_ENABLED": "true", "EXECUTION_REPORT_GCS_URI": "gs://synthetic/archive",
           "DIGEST_CANDIDATES_GCS_URI": "gs://synthetic/candidates", "RUNTIME_HEARTBEAT_REPORT_PLATFORM": "interactive_brokers"}
    def read(args):
        assert "/interactive_brokers/" in args[-1]
        if args[0] == "ls":
            return [{"url": "gs://synthetic/archive/interactive_brokers/strategy/2026-10/run.json",
                     "metadata": {"updated": NOW.isoformat()}}]
        return deepcopy(payload)
    assert emit(env, now=NOW + timedelta(hours=2, minutes=20), read=read)["uploaded"]
    row = next(iter(routing[1].rows.values()))[1]["runs"][0]
    assert row["platform_id"] == "ibkr" and row["execution_mode"] == "dry_run"
    assert row["order_count"] is None and row["fill_count"] is None


def test_cycle_preserves_signal_attention_when_config_codes_are_empty(routing, monkeypatch):
    from tests.test_notif_consolidation_equivalence import capture_cycle
    observed = adapter.with_run(report(), capture_cycle, monkeypatch, "no_order", "en",
        metadata_overrides={"notification_attention_reason_codes": ["synthetic_attention"]})
    assert len([call for call in observed["calls"] if call[0] == "send"]) == 1
    row = next(iter(routing[1].rows.values()))[1]["runs"][0]
    assert row["status"] == "alert"


@pytest.mark.parametrize("history", [{}, None, {"schema_version": "qsl.notification_incident.v1", "active": False}])
def test_versioned_incomplete_marker_history_refuses_to_send(history):
    class IncompleteStore:
        def read(self, key):
            return 7, deepcopy(history)
        def compare_and_set(self, *args):
            pytest.fail("incomplete marker history must never be overwritten")
    sends = []
    with pytest.raises(RuntimeError, match="notification_marker_history_incomplete"):
        adapter.immediate_incident(event("real_error"), lambda: sends.append("send"), store=IncompleteStore())
    assert sends == []


@pytest.mark.parametrize("outcome,status,receipt", [
    (True, "uncertain", None),
    ({"acknowledged": True}, "uncertain", None),
    ({"acknowledged": True, "receipt_id": "telegram:synthetic-message-42"}, "delivered", "telegram:synthetic-message-42"),
])
def test_delivery_requires_sender_receipt_and_uncertain_is_not_resent(outcome, status, receipt):
    store, sends = StateStore(), []
    source = event("real_error")
    def send():
        sends.append("send")
        return outcome
    assert adapter.immediate_incident(source, send, store=store)
    marker = next(value for _, value in store.backing.values())
    assert marker["delivery_status"] == status and marker["receipt_id"] == receipt
    assert adapter.immediate_incident(source, send, store=store)
    assert sends == ["send"]


def test_per_run_projection_never_copies_window_totals_or_monitor_events(routing):
    facts = [event(run="zero", order_count=0, fill_count=0),
             event("order_submitted", run="orders", order_count=2, fill_count=3,
                   order_execution_digest="a" * 64),
             event("healthy_heartbeat", run="zero", order_count=0, fill_count=0)]
    for fact in (*facts, facts[0]):
        assert adapter.persist_candidate(fact.to_candidate())
    rows = {row["source_run_id"]: row for row in next(iter(routing[1].rows.values()))[1]["runs"]}
    assert set(rows) == {"zero", "orders"}
    assert (rows["zero"]["order_count"], rows["zero"]["fill_count"]) == (0, 0)
    assert (rows["orders"]["order_count"], rows["orders"]["fill_count"]) == (2, 3)
    assert len(rows["zero"]["notification_events"]) == 2
    assert len(rows["orders"]["notification_events"]) == 1
    for run, row in rows.items():
        assert row["cycle_count"] == 1
        assert all(fact["source_run_id"] == run for fact in row["notification_events"])


def test_nonrunning_observation_is_rejected_outside_the_business_run_channel(routing):
    with pytest.raises(CandidatePersistenceError, match="business_run_required"):
        adapter.persist_candidate(event("receipt_missing", run="monitor-only").to_candidate(), actually_ran=False)
    assert routing[1].rows == {}


@pytest.mark.parametrize("field", ["order_count", "fill_count"])
def test_conflicting_same_event_counts_remain_unknown_after_later_duplicates(routing, field):
    first = event(**{field: 1}).to_candidate()
    conflicting = {**first, field: 2}
    for fact in (first, conflicting, first):
        assert adapter.persist_candidate(fact)
    row = next(iter(routing[1].rows.values()))[1]["runs"][0]
    assert row[field] is None and row["field_status"][field] == "unknown"
    assert row["status"] == "alert" and row["cycle_count"] == 1

