from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import patch
from types import SimpleNamespace

from notifications import unified_adapter as adapter
from notifications.events import RenderedNotification
from quant_platform_kit.notifications.unified_contract import NotificationEvent
from quant_platform_kit.notifications.digest_routing import immediate_plugin_signals
from scripts.project_digest_candidates import merge_routine_observation_counts, project_notification_events
from scripts import execution_report_heartbeat as heartbeat
from application import rebalance_service
from application.runtime_dependencies import IBKRRebalanceConfig
from application.runtime_reporting_adapters import IBKRRuntimeReportingAdapters, confirmed_gcs_report_uri
from quant_platform_kit.common.runtime_reports import RuntimeReportPersistResult


def pending_order():
    order = {"symbol": "SYN", "status": "Submitted", "broker_order_id": "synthetic-id"}
    return {"execution_status": "pending_reconciliation", "no_op_reason": "broker_order_pending_confirmation",
            "orders_submitted": [], "orders_filled": [], "orders_pending": [order],
            "orders_partially_filled": [], "option_orders_submitted": [], "option_orders_filled": [],
            "option_orders_pending": [], "option_orders_partially_filled": [], "skipped_reasons": []}


class Publisher:
    def __init__(self):
        self.messages = []

    def publish(self, notification):
        self.messages.append(notification.compact_text)
        return True


class DigestOnlyDispatch(TestCase):
    def test_real_reporting_adapter_result_requires_confirmed_gcs_uri(self):
        results = [(None, False),
                   (RuntimeReportPersistResult(local_path="/tmp/report.json"), False),
                   (RuntimeReportPersistResult(local_path="/tmp/report.json", cloud_uri="gs://bucket/report.json"), True),
                   ("gs://bucket/report.json", True),
                   ({"gcs_uri": "gs://bucket/report.json"}, False)]
        for result, expected in results:
            with self.subTest(result=result):
                adapter_stub = SimpleNamespace(report_persister=lambda *a, **kw: result, report_base_dir=None,
                                               report_gcs_prefix_uri="gs://bucket", runtime_assembly=SimpleNamespace(project_id="synthetic"))
                path = IBKRRuntimeReportingAdapters.persist_execution_report(adapter_stub, {})
                self.assertEqual(confirmed_gcs_report_uri(path), expected)

    def test_observation_conflicts_are_per_category_and_sticky(self):
        merged = merge_routine_observation_counts({"watch": 1, "taco_rebound": 2},
                                                   {"watch": 1, "taco_rebound": 3})
        self.assertEqual(merged, {"watch": 1, "taco_rebound": None})
        self.assertEqual(merge_routine_observation_counts(merged, {"watch": 1, "taco_rebound": 2}), merged)
        self.assertEqual(merge_routine_observation_counts({}, {"watch": 1}), {"watch": None})
        with self.assertRaises(ValueError):
            merge_routine_observation_counts({"private": 1}, {})

    def test_actual_pending_order_rebalance_reaches_candidate_without_success_send(self):
        class Broker:
            def isConnected(self):
                return True
            def disconnect(self):
                pass
        report = {"run_id": "run-one", "started_at": datetime.now(timezone.utc).isoformat(), "dry_run": False}
        env = {"DIGEST_OPAQUE_ACCOUNT_UID": "synthetic-account", "DIGEST_TARGET_ID": "synthetic-target",
               "STRATEGY_PROFILE": "synthetic-profile"}
        signals = {"strategy_profile": "synthetic-profile", "allocation": {"target_mode": "weight",
                   "targets": {"SYN": .5}, "strategy_symbols": ["SYN"]}}
        candidates, sends = [], []
        with patch.dict(adapter.os.environ, env, clear=True), \
             patch.object(adapter, "persist_candidate", side_effect=lambda fact, **kw: candidates.append(fact) or True), \
             patch.object(adapter, "recover_incidents"), \
             patch.object(rebalance_service, "write_reconciliation_record", return_value="synthetic"), \
             patch.object(rebalance_service, "_record_platform_execution_telemetry"):
            result = adapter.with_run(report, rebalance_service.run_strategy_core,
                connect_ib=lambda: Broker(), get_current_portfolio=lambda _: ({}, {"equity": 1000.0, "buying_power": 500.0}),
                compute_signals=lambda *_: ({"SYN": .5}, "", False, "", signals),
                execute_rebalance=lambda *args, **kw: ([], pending_order()),
                send_tg_message=lambda text: sends.append(text) or True,
                config=IBKRRebalanceConfig(translator=lambda key, **kwargs: key, separator="---",
                                           strategy_display_name="Example", notify_no_trade_cycles=False))
        self.assertEqual(sends, [])
        self.assertEqual(result.result, "OK - executed")
        self.assertEqual(len(candidates), 1)
        self.assertEqual((candidates[0]["event_kind"], candidates[0]["order_count"], candidates[0]["fill_count"]),
                         ("order_submitted", 1, None))

    def test_actual_unknown_submission_remains_immediate(self):
        class Broker:
            def isConnected(self):
                return True
            def disconnect(self):
                pass
        report = {"run_id": "run-two", "started_at": datetime.now(timezone.utc).isoformat(), "dry_run": False}
        env = {"DIGEST_OPAQUE_ACCOUNT_UID": "synthetic-account", "DIGEST_TARGET_ID": "synthetic-target",
               "STRATEGY_PROFILE": "synthetic-profile"}
        signals = {"strategy_profile": "synthetic-profile", "allocation": {"target_mode": "weight",
                   "targets": {"SYN": .5}, "strategy_symbols": ["SYN"]}}
        unknown = {**pending_order(), "outcome_unknown": True, "submission_halted": True}
        sends = []
        with patch.dict(adapter.os.environ, env, clear=True), \
             patch.object(rebalance_service, "write_reconciliation_record", return_value="synthetic"), \
             patch.object(rebalance_service, "_record_platform_execution_telemetry"):
            adapter.with_run(report, rebalance_service.run_strategy_core,
                connect_ib=lambda: Broker(), get_current_portfolio=lambda _: ({}, {"equity": 1000.0, "buying_power": 500.0}),
                compute_signals=lambda *_: ({"SYN": .5}, "", False, "", signals),
                execute_rebalance=lambda *args, **kw: ([], unknown),
                send_tg_message=lambda text: sends.append(text) or True,
                config=IBKRRebalanceConfig(translator=lambda key, **kwargs: key, separator="---",
                                           strategy_display_name="Example", notify_no_trade_cycles=False))
        self.assertEqual(len(sends), 1)

    def test_candidate_store_and_projector_preserve_one_run_and_one_order(self):
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
        now = datetime.now(timezone.utc)
        facts = adapter.digest_evidence(pending_order())
        identity = dict(platform_id="ibkr", opaque_account_uid="synthetic-account", target_id="synthetic-target",
                        strategy_profile="synthetic-profile", source_run_id="run-one", order_count=1, fill_count=None)
        order = NotificationEvent("order_submitted", now, order_execution_digest=facts["order_execution_digest"], **identity)
        heartbeat_event = NotificationEvent("healthy_heartbeat", now, **identity)
        state, store = State(), Candidates()
        for event in (order, heartbeat_event, order):
            fact = {**event.to_candidate(), "routine_observation_counts": {"watch": 1}}
            self.assertTrue(adapter.persist_candidate(fact, state_store=state, candidate_store=store,
                                                      env={"DIGEST_STORE_TARGET": "synthetic-slot"}))
        row = store.payload["runs"][0]
        self.assertEqual((row["cycle_count"], row["order_count"], row["fill_count"]), (1, 1, None))
        self.assertEqual(row["routine_observation_counts"], {"watch": 1})
        self.assertEqual(row["execution_status"], "order_submitted")
        conflicting = {**order.to_candidate(), "routine_observation_counts": {}}
        self.assertTrue(adapter.persist_candidate(conflicting, state_store=state, candidate_store=store,
                                                  env={"DIGEST_STORE_TARGET": "synthetic-slot"}))
        row = store.payload["runs"][0]
        self.assertEqual(row["routine_observation_counts"], {"watch": None})
        self.assertEqual(row["field_status"]["routine_observation_counts"], "unknown")
        self.assertEqual((row["status"], row["cycle_count"], row["order_count"]), ("ok", 1, 1))
        again = {**order.to_candidate(), "routine_observation_counts": {"watch": 1}}
        self.assertTrue(adapter.persist_candidate(again, state_store=state, candidate_store=store,
                                                  env={"DIGEST_STORE_TARGET": "synthetic-slot"}))
        self.assertEqual(store.payload["runs"][0]["routine_observation_counts"], {"watch": None})

    def test_plugin_counts_are_bounded_and_derive_from_original_signal_errors(self):
        signals = [SimpleNamespace(canonical_route="watch", suggested_action="observe"),
                   SimpleNamespace(canonical_route="watch", suggested_action="observe", error="failure"),
                   SimpleNamespace(canonical_route="unrecognized", suggested_action="observe")]
        counts = adapter.routine_observation_counts_from_signals(signals)
        self.assertEqual(counts, {"watch": 1})
        self.assertEqual(adapter.routine_observation_counts({"summary": {"routine_observation_counts": counts}}), counts)
        self.assertIsNone(adapter.routine_observation_counts({"summary": {"strategy_plugins": []}}))
        self.assertEqual(immediate_plugin_signals(signals, mode="daily_digest"), tuple(signals[1:]))

    def test_actual_noop_rebalance_reaches_persisted_candidate_with_flag_off(self):
        class Broker:
            def isConnected(self):
                return True
            def disconnect(self):
                pass
        report = {"run_id": "run-one", "started_at": datetime.now(timezone.utc).isoformat(), "dry_run": False}
        env = {"DIGEST_OPAQUE_ACCOUNT_UID": "synthetic-account", "DIGEST_TARGET_ID": "synthetic-target",
               "STRATEGY_PROFILE": "synthetic-profile"}
        candidates, sends = [], []
        with patch.dict(adapter.os.environ, env, clear=True), \
             patch.object(adapter, "persist_candidate", side_effect=lambda fact, **kw: candidates.append(fact) or True), \
             patch.object(adapter, "recover_incidents"), \
             patch.object(rebalance_service, "write_reconciliation_record", return_value="synthetic"), \
             patch.object(rebalance_service, "_record_platform_execution_telemetry"):
            result = adapter.with_run(report, rebalance_service.run_strategy_core,
                connect_ib=lambda: Broker(), get_current_portfolio=lambda _: ({}, {"equity": 1000.0, "buying_power": 500.0}),
                compute_signals=lambda *_: (None, "", False, "", {"strategy_profile": "synthetic-profile"}),
                execute_rebalance=lambda *args, **kw: ([], {}), send_tg_message=lambda text: sends.append(text) or True,
                config=IBKRRebalanceConfig(translator=lambda key, **kwargs: key, separator="---",
                                           strategy_display_name="Example", notify_no_trade_cycles=False))
        self.assertEqual(sends, [])
        self.assertEqual(result.result, "OK - no-op")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["event_kind"], "routine_cycle")

    def test_known_live_order_is_persisted_once_and_never_sent_for_any_flags(self):
        report = {"run_id": "run-one", "started_at": datetime.now(timezone.utc).isoformat(), "dry_run": False}
        env = {"DIGEST_OPAQUE_ACCOUNT_UID": "synthetic-account", "DIGEST_TARGET_ID": "synthetic-target",
               "STRATEGY_PROFILE": "synthetic-profile"}
        for flag in ("", "false", "true"):
            with self.subTest(flag=flag), patch.dict(adapter.os.environ, {**env, adapter.FLAG: flag,
                    "QSL_ROUTINE_NOTIFICATION_MODE": "immediate", "QSL_DIGEST_ROUTINE_ORDERS": "false"}, clear=True):
                publisher, candidates = Publisher(), []
                with patch.object(adapter, "persist_candidate", side_effect=lambda fact, **kw: candidates.append(fact) or True), \
                     patch.object(adapter, "recover_incidents"):
                    result = adapter.with_run(report, adapter.publish_cycle, publisher,
                        notification=RenderedNotification("success", "success"), execution=pending_order())
                self.assertTrue(result)
                self.assertEqual(publisher.messages, [])
                self.assertEqual(len(candidates), 1)
                self.assertEqual(candidates[0]["event_kind"], "order_submitted")
                self.assertEqual(candidates[0]["order_count"], 1)
                self.assertIsNone(candidates[0]["fill_count"])

    def test_unknown_or_duplicate_broker_id_never_becomes_routine(self):
        execution = pending_order()
        execution["outcome_unknown"] = True
        self.assertEqual(adapter._kind(execution), "order_status_unknown")
        execution = pending_order()
        execution["orders_pending"].append(dict(execution["orders_pending"][0]))
        self.assertEqual(adapter._kind(execution), "order_status_unknown")
        self.assertIsNone(adapter.digest_evidence(execution)["order_count"])

    def test_explicit_fill_has_order_count_without_inventing_fill_count(self):
        execution = pending_order()
        execution.update(execution_status="executed", no_op_reason=None, orders_pending=[],
                         orders_filled=[{"status": "Filled", "broker_order_id": "synthetic-id"}])
        facts = adapter.digest_evidence(execution)
        self.assertEqual(adapter._kind(execution), "fill")
        self.assertEqual(facts["order_count"], 1)
        self.assertIsNone(facts["fill_count"])
        archived = {"dry_run": False, "errors": [], "summary": {"execution_status": "executed",
                    "orders_submitted_count": 1, "notification_digest": facts}}
        self.assertTrue(adapter.verified_order_summary(archived))
        archived["summary"]["orders_submitted_count"] = 0
        self.assertFalse(adapter.verified_order_summary(archived))

    def test_no_prior_proof_or_conflicting_count_does_not_quiet_pending(self):
        facts = adapter.digest_evidence(pending_order())
        report = {"dry_run": False, "summary": {"execution_status": "pending_reconciliation",
                  "no_op_reason": "broker_order_pending_confirmation", "orders_submitted_count": 1,
                  "notification_digest": facts}, "errors": []}
        self.assertTrue(adapter.verified_pending_summary(report))
        report["summary"]["orders_submitted_count"] = 0
        self.assertFalse(adapter.verified_pending_summary(report))

    def test_missing_auth_and_contradictory_pending_stay_urgent(self):
        base = {"dry_run": False, "diagnostics": {}, "summary": {"execution_status": "no_action"}}
        with patch.dict(adapter.os.environ, {adapter.FLAG: "true"}, clear=True):
            self.assertEqual(adapter.receipt_notification_kind(base, quiet_eligible=True), "receipt_missing")
            receipt = {"outcome": "no_action"}
            auth = {**base, "execution_receipt": receipt, "summary": {"execution_status": "no_action", "auth_expired": True}}
            pending = {**base, "execution_receipt": receipt, "summary": {"execution_status": "pending", "orders_pending_count": 1}}
            self.assertEqual(adapter.receipt_notification_kind(auth, quiet_eligible=True), "auth_failure")
            self.assertEqual(adapter.receipt_notification_kind(pending, quiet_eligible=True), "order_status_unknown")
        with patch.dict(adapter.os.environ, {}, clear=True):
            self.assertEqual(adapter.receipt_notification_kind(base, quiet_eligible=True), "healthy_heartbeat")

    def test_monitor_marker_failure_keeps_urgent_alert_without_duplicate(self):
        report = {"run_id": "synthetic", "started_at": datetime.now(timezone.utc).isoformat(), "dry_run": False}
        env = {adapter.FLAG: "true", "DIGEST_OPAQUE_ACCOUNT_UID": "synthetic-account",
               "DIGEST_TARGET_ID": "synthetic-target", "STRATEGY_PROFILE": "synthetic-profile"}
        sent = []
        with patch.dict(adapter.os.environ, env, clear=True), \
             patch.object(adapter, "_checked_candidate", return_value=False), \
             patch.object(adapter, "_state_store", side_effect=OSError("synthetic")):
            self.assertTrue(adapter.publish_monitor(kind="receipt_missing", report=report,
                send=lambda: sent.append("urgent") or True))
        self.assertEqual(sent, ["urgent"])

    def test_unbound_healthy_monitor_reports_coverage_once(self):
        notices = []
        with patch.object(adapter, "event_for", side_effect=ValueError("synthetic")):
            self.assertFalse(adapter.publish_monitor(kind="healthy_heartbeat", report={}, send=lambda: None,
                send_error=lambda: notices.append("coverage")))
        self.assertEqual(notices, ["coverage"])

    def test_heartbeat_routes_proven_pending_to_candidate_even_with_legacy_flag(self):
        now = datetime.now(timezone.utc)
        report = {"run_id": "run-one", "started_at": now.isoformat(), "finished_at": now.isoformat(),
                  "platform": "ibkr", "strategy_profile": "synthetic-profile", "status": "ok", "stage": "",
                  "dry_run": False, "errors": [], "diagnostics": {}, "execution_receipt": {"synthetic": True},
                  "summary": {"execution_status": "pending_reconciliation", "no_op_reason": "broker_order_pending_confirmation",
                              "orders_submitted_count": 1, "notification_digest": adapter.digest_evidence(pending_order())}}
        env = {"DIGEST_OPAQUE_ACCOUNT_UID": "synthetic-account", "DIGEST_TARGET_ID": "synthetic-target",
               "STRATEGY_PROFILE": "synthetic-profile", "RUNTIME_HEARTBEAT_NOTIFY_ON_SUCCESS": "true"}
        calls, sends = [], []
        with patch.dict(adapter.os.environ, env, clear=True), \
             patch.object(heartbeat, "validate_execution_receipt", return_value={"outcome": "reconciliation_required", "platform": "ibkr", "strategy_profile": "synthetic-profile"}), \
             patch.object(heartbeat, "_send_telegram", side_effect=lambda msg, **kw: sends.append(msg) or True), \
             patch.object(adapter, "persist_candidate", side_effect=lambda fact, **kw: calls.append(fact) or True), \
             patch.object(adapter, "recover_incidents"), patch.object(adapter, "_state_store", return_value=object()):
            self.assertTrue(heartbeat._is_quiet_report(report, now=now))
            heartbeat._notify_normal_heartbeat("synthetic", "healthy", reports=[(report, True)], now=now)
        self.assertEqual(sends, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["event_kind"], "healthy_heartbeat")
        report["summary"].pop("notification_digest")
        self.assertFalse(adapter.verified_pending_summary(report))

    def test_no_due_heartbeat_does_not_invent_a_run_or_send_with_success_flag(self):
        with patch.dict(adapter.os.environ, {"RUNTIME_HEARTBEAT_NOTIFY_ON_SUCCESS": "true"}, clear=True), \
             patch.object(heartbeat, "_send_telegram") as send, \
             patch.object(adapter, "persist_candidate") as persist:
            heartbeat._notify_normal_heartbeat("synthetic", "not due")
        send.assert_not_called()
        persist.assert_not_called()

    def test_persistence_failure_sends_only_error_and_returns_failure(self):
        report = {"run_id": "run-one", "started_at": datetime.now(timezone.utc).isoformat(), "dry_run": False}
        env = {"DIGEST_OPAQUE_ACCOUNT_UID": "synthetic-account", "DIGEST_TARGET_ID": "synthetic-target",
               "STRATEGY_PROFILE": "synthetic-profile", "NOTIFY_LANG": "ZH-hans"}
        publisher = Publisher()
        with patch.dict(adapter.os.environ, env, clear=True), \
             patch.object(adapter, "persist_candidate", return_value=False), \
             patch.object(adapter, "send_incident", side_effect=OSError("synthetic")):
            result = adapter.with_run(report, adapter.publish_cycle, publisher,
                notification=RenderedNotification("trade details", "trade details"), execution=pending_order())
        self.assertFalse(result)
        self.assertEqual(len(publisher.messages), 1)
        self.assertIn("汇总候选写入未确认", publisher.messages[0])
        self.assertNotIn("trade details", publisher.messages[0])

    def test_order_and_heartbeat_for_same_run_project_once_without_claiming_fill(self):
        facts = adapter.digest_evidence(pending_order())
        self.assertTrue(facts["routine"])
        self.assertEqual(facts["order_count"], 1)
        now = datetime.now(timezone.utc)
        identity = dict(platform_id="ibkr", opaque_account_uid="synthetic-account", target_id="synthetic-target",
                        strategy_profile="synthetic-profile", source_run_id="run-one", order_count=1, fill_count=None)
        order = NotificationEvent("order_submitted", now, order_execution_digest=facts["order_execution_digest"], **identity)
        heartbeat = NotificationEvent("healthy_heartbeat", now, **identity)
        records = [{"event": {**event.to_candidate(), "routine_observation_counts": {"watch": 2}}, "actually_ran": True, "dry_run": False}
                   for event in (order, heartbeat)]
        rows = project_notification_events(records)["runs"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cycle_count"], 1)
        self.assertEqual(rows[0]["order_count"], 1)
        self.assertIsNone(rows[0]["fill_count"])
        self.assertEqual(rows[0]["execution_status"], "order_submitted")
        self.assertEqual(rows[0]["routine_observation_counts"], {"watch": 2})
        records[1]["event"]["routine_observation_counts"] = {"watch": 3}
        conflict = project_notification_events(records)["runs"][0]
        self.assertEqual(conflict["routine_observation_counts"], {"watch": None})
        self.assertEqual(conflict["field_status"]["routine_observation_counts"], "unknown")
        self.assertEqual((conflict["cycle_count"], conflict["order_count"]), (1, 1))
