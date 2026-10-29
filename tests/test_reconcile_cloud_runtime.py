from __future__ import annotations

import json
import unittest
from unittest import mock

from scripts import reconcile_cloud_runtime as rcr


def _http_candidate(name, commit):
    return {
        "metadata": {"name": name, "labels": {"commit-sha": commit}},
        "spec": {"containers": [{
            "startupProbe": {"httpGet": {"path": "/health", "port": 8080}},
            "env": [{"name": "RUNTIME_TARGET_JSON", "value": "{}"}],
        }]},
        "status": {"conditions": [
            {"type": "Ready", "status": "True"},
            {"type": "ContainerHealthy", "status": "True"},
        ]},
    }


class ReconcileCloudRuntimeTests(unittest.TestCase):
    def test_load_targets_reads_all_supported_env_shapes(self) -> None:
        env = {
            "SYNC_PLAN_JSON": json.dumps(
                {
                    "targets": [
                        {
                            "runtime_target_json": json.dumps(
                                {
                                    "service_name": "interactive-brokers-quant-live-u1234-service",
                                    "account_scope": "u1234",
                                }
                            ),
                            "region": "us-central1",
                        }
                    ]
                }
            ),
            "CLOUD_RUN_SERVICE_TARGETS_JSON": json.dumps(
                {
                    "targets": [
                        {"service_name": "interactive-brokers-quant-live-u1234-service"},
                        {"service_name": "interactive-brokers-quant-live-u5678-service", "region": "asia-east1"},
                    ]
                }
            ),
            "CLOUD_RUN_SERVICES": "interactive-brokers-quant-live-u9999-service, extra-service;interactive-brokers-quant-live-u1234-service",
        }

        targets = rcr.load_targets(env=env)

        self.assertEqual(
            [target.service_name for target in targets],
            [
                "interactive-brokers-quant-live-u1234-service",
                "interactive-brokers-quant-live-u5678-service",
                "interactive-brokers-quant-live-u9999-service",
                "extra-service",
            ],
        )
        self.assertEqual(targets[0].region, "us-central1")
        self.assertEqual(targets[0].account_scope, "u1234")
        self.assertEqual(targets[1].region, "asia-east1")

    def test_legacy_jobs_for_ibkr_service_include_only_explicit_candidates(self) -> None:
        target = rcr.RuntimeTarget(service_name="interactive-brokers-quant-live-u1234-service")

        self.assertEqual(
            set(rcr._legacy_jobs_for_target("ibkr", target)),
            {
                "interactive-brokers-quant-live-u1234-service-probe-scheduler",
                "interactive-brokers-quant-live-u1234-service-precheck-scheduler",
                "interactive-brokers-quant-live-u1234-probe-scheduler",
                "ibkr-u1234-backup-execution",
                "ibkr-u1234-pre-market-dry-run",
                "interactive-brokers-monitor-dispatcher-scheduler",
            },
        )

    def test_ensure_latest_traffic_updates_to_commit_revision(self) -> None:
        target = rcr.RuntimeTarget(
            service_name="interactive-brokers-quant-live-u1234-service",
            region="us-central1",
        )
        rev_name = "interactive-brokers-quant-live-u1234-service-00001-abc"
        stale = "interactive-brokers-quant-live-u1234-service-00000-old"
        calls: list[list[str]] = []
        service_describe_count = 0

        def fake_run(args, *, json_output=False, dry_run=False):
            nonlocal service_describe_count
            calls.append(list(args))
            self.assertFalse(dry_run)
            if args[:4] == ["gcloud", "run", "services", "describe"]:
                service_describe_count += 1
                traffic_rev = stale if service_describe_count == 1 else rev_name
                return {
                    "status": {
                        "latestReadyRevisionName": stale,
                        "traffic": [{"percent": 100, "revisionName": traffic_rev}],
                    }
                }
            if args[:4] == ["gcloud", "run", "revisions", "list"]:
                return [
                    {
                        "metadata": {
                            "name": rev_name,
                            "labels": {"commit-sha": "abc123"},
                        },
                        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                    }
                ]
            if args[:4] == ["gcloud", "run", "revisions", "describe"]:
                return _http_candidate(rev_name, "abc123")
            if args[:4] == ["gcloud", "run", "services", "update-traffic"]:
                return ""
            self.fail(f"unexpected command: {args}")

        with mock.patch.object(rcr, "_run", side_effect=fake_run):
            rcr.ensure_latest_traffic(
                project="interactivebrokersquant",
                region="us-central1",
                targets=[target],
                expected_commit="abc123",
                dry_run=False,
            )

        self.assertEqual(service_describe_count, 2)
        traffic_cmds = [cmd for cmd in calls if cmd[:4] == ["gcloud", "run", "services", "update-traffic"]]
        self.assertEqual(len(traffic_cmds), 1)
        self.assertIn(f"--to-revisions={rev_name}=100", traffic_cmds[0])
        self.assertNotIn("--to-latest", traffic_cmds[0])

    def test_ensure_latest_traffic_requires_matching_commit_revision(self) -> None:
        target = rcr.RuntimeTarget(service_name="interactive-brokers-quant-live-u1234-service", region="us-central1")

        def fake_run(args, *, json_output=False, dry_run=False):
            if args[:4] == ["gcloud", "run", "services", "describe"]:
                return {
                    "status": {
                        "latestCreatedRevisionName": "interactive-brokers-quant-live-u1234-service-00001-abc",
                        "latestReadyRevisionName": "interactive-brokers-quant-live-u1234-service-00000-old",
                        "traffic": [],
                    }
                }
            if args[:4] == ["gcloud", "run", "revisions", "list"]:
                return []
            self.fail(f"unexpected command: {args}")

        with mock.patch.object(rcr, "_run", side_effect=fake_run):
            with self.assertRaisesRegex(rcr.ReconcileError, "No Ready revision"):
                rcr.ensure_latest_traffic(
                    project="interactivebrokersquant",
                    region="us-central1",
                    targets=[target],
                    expected_commit="abc123",
                    dry_run=False,
                )

    def test_env_only_uses_exact_created_revision_and_preserves_source(self) -> None:
        target = rcr.RuntimeTarget(service_name="unit-service", region="us-central1")
        old = _http_candidate("unit-service-old", "old-commit")
        old["spec"]["containers"][0]["image"] = "synthetic-image@sha256:abc"
        old["spec"]["containers"][0]["env"].append({"name": "RUNTIME_TARGET_ENABLED", "value": "false"})
        old["spec"]["containers"][0]["env"].append({"name": "SECRET", "valueFrom": {"secretKeyRef": {"name": "retained", "key": "1"}}})
        candidate = _http_candidate("unit-service-eabc-1", "old-commit")
        candidate["spec"]["containers"][0]["image"] = "synthetic-image@sha256:abc"
        candidate["spec"]["containers"][0]["env"].append({"name": "RUNTIME_TARGET_ENABLED", "value": "true"})
        candidate["spec"]["containers"][0]["env"].append({"name": "SECRET", "valueFrom": {"secretKeyRef": {"name": "retained", "key": "1"}}})
        expected = {"RUNTIME_TARGET_JSON": "{}", "RUNTIME_TARGET_ENABLED": "true"}
        cases = ["pass", "wrong_image", "wrong_commit", "wrong_switch", "wrong_target", "wrong_latest",
                 "traffic_changed", "wrong_name", "unhealthy", "changed_secret", "changed_network"]
        for case in cases:
            with self.subTest(case=case):
                current = json.loads(json.dumps(candidate))
                if case == "wrong_image": current["spec"]["containers"][0]["image"] = "other"
                if case == "wrong_commit": current["metadata"]["labels"]["commit-sha"] = "other"
                if case == "wrong_switch": current["spec"]["containers"][0]["env"][1]["value"] = "false"
                if case == "wrong_target": current["spec"]["containers"][0]["env"][0]["value"] = '{"other":true}'
                if case == "wrong_name": current["metadata"]["name"] = "unit-service-other"
                if case == "unhealthy": current["status"]["conditions"][1]["status"] = "False"
                if case == "changed_secret": current["spec"]["containers"][0]["env"][2]["valueFrom"]["secretKeyRef"]["key"] = "2"
                if case == "changed_network": current["metadata"]["annotations"] = {"run.googleapis.com/network-interfaces": "different"}
                calls = []
                def fake_run(args, *, json_output=False, dry_run=False):
                    calls.append(list(args))
                    if args[:4] == ["gcloud", "run", "services", "describe"]:
                        adopted = any(row[:4] == ["gcloud", "run", "services", "update-traffic"] for row in calls)
                        revision = "unit-service-eabc-1" if adopted else ("unit-service-other" if case == "traffic_changed" else "unit-service-old")
                        return {"status": {"traffic": [{"percent": 100, "revisionName": revision}],
                                           "latestCreatedRevisionName": "unit-service-other" if case == "wrong_latest" else "unit-service-eabc-1"}}
                    if args[:4] == ["gcloud", "run", "revisions", "describe"]: return current
                    if args[:4] == ["gcloud", "run", "services", "update-traffic"]: return ""
                    self.fail(f"unexpected command: {args}")
                with mock.patch.object(rcr, "_run", side_effect=fake_run):
                    kwargs = dict(project="synthetic", region="us-central1", targets=[target],
                                  expected_commit="current-workflow-commit", dry_run=False,
                                  env_sync_baseline=old, env_sync_revision="unit-service-eabc-1", env_sync_expected=expected,
                                  switch_only=True, env_sync_service={"spec": {"template": {"spec": {"containers": [{}]}}}})
                    if case == "pass": rcr.ensure_latest_traffic(**kwargs)
                    else:
                        with self.assertRaises((rcr.ReconcileError, ValueError)): rcr.ensure_latest_traffic(**kwargs)
                updates = [row for row in calls if row[:4] == ["gcloud", "run", "services", "update-traffic"]]
                self.assertEqual(len(updates), 1 if case == "pass" else 0)
                if updates: self.assertIn("--to-revisions=unit-service-eabc-1=100", updates[0])
                self.assertFalse(any(row[:4] == ["gcloud", "run", "revisions", "list"] for row in calls))

    def test_switch_baseline_reuses_existing_target_and_rejects_reconcile_only_enable(self) -> None:
        baseline = _http_candidate("unit-service-old", "old-commit")
        baseline["spec"]["containers"][0]["image"] = "synthetic-image@sha256:abc"
        baseline["spec"]["containers"][0]["env"].append({"name": "RUNTIME_TARGET_ENABLED", "value": "true"})
        plan = {"env": {"RUNTIME_TARGET_JSON": "{ }", "RUNTIME_TARGET_ENABLED": "true"},
                "standard_execution_enabled": True}
        self.assertEqual(rcr.validate_switch_baseline(baseline, plan), ("true", True))
        plan["env"]["RUNTIME_TARGET_ENABLED"] = "false"
        self.assertEqual(rcr.validate_switch_baseline(baseline, plan), ("false", False))
        plan["env"]["RUNTIME_TARGET_ENABLED"] = "true"
        plan["standard_execution_enabled"] = False
        with self.assertRaisesRegex(rcr.ReconcileError, "does not permit"):
            rcr.validate_switch_baseline(baseline, plan)
        plan["standard_execution_enabled"] = True
        plan["env"]["RUNTIME_TARGET_JSON"] = '{"other": true}'
        with self.assertRaisesRegex(rcr.ReconcileError, "does not match"):
            rcr.validate_switch_baseline(baseline, plan)

    def test_stop_accepts_unhealthy_or_stale_baseline_but_enable_does_not(self) -> None:
        baseline = _http_candidate("unit-service-old", "old-commit")
        baseline["spec"]["containers"][0]["image"] = "synthetic-image@sha256:abc"
        target = {"live_continuity": {"state": "ACTIVE_LKG", "baseline_target_sha256": "stale"}}
        baseline["spec"]["containers"][0]["env"][0]["value"] = json.dumps(target)
        baseline["spec"]["containers"][0]["env"].append({"name": "RUNTIME_TARGET_ENABLED", "value": "true"})
        baseline["status"]["conditions"][1]["status"] = "False"
        plan = {"env": {"RUNTIME_TARGET_JSON": json.dumps(target), "RUNTIME_TARGET_ENABLED": "false"},
                "standard_execution_enabled": False}
        self.assertEqual(rcr.validate_switch_baseline(baseline, plan), ("false", False))
        plan["env"]["RUNTIME_TARGET_ENABLED"] = "true"
        plan["standard_execution_enabled"] = True
        with self.assertRaises(ValueError):
            rcr.validate_switch_baseline(baseline, plan)

    def test_switch_spec_comparison_normalizes_only_provider_fields_and_env_order(self) -> None:
        before = {"servingState": "active", "containers": [{"name": "generated-a", "image": "image", "env": [
            {"name": "SECRET", "valueFrom": {"secretKeyRef": {"name": "one"}}},
            {"name": "RUNTIME_TARGET_ENABLED", "value": "true"}, {"name": "A", "value": "a"}]}]}
        after = json.loads(json.dumps(before))
        after["servingState"] = "retired"
        after["containers"][0]["name"] = "generated-b"
        after["containers"][0]["env"].reverse()
        assert rcr._without_runtime_switch(before, unnamed_template=True) == rcr._without_runtime_switch(after, unnamed_template=True)
        assert rcr._without_runtime_switch(before) != rcr._without_runtime_switch(after)
        after["containers"][0]["env"][-1]["valueFrom"]["secretKeyRef"]["name"] = "other"
        assert rcr._without_runtime_switch(before, unnamed_template=True) != rcr._without_runtime_switch(after, unnamed_template=True)

    def test_ensure_latest_traffic_routes_when_latest_ready_is_stale(self) -> None:
        target = rcr.RuntimeTarget(service_name="interactive-brokers-quant-live-u1234-service", region="us-central1")
        stale = "interactive-brokers-quant-live-u1234-service-00001"
        fresh = "interactive-brokers-quant-live-u1234-service-00003"
        calls: list[list[str]] = []

        def fake_run(args, *, json_output=False, dry_run=False):
            calls.append(list(args))
            if args[:4] == ["gcloud", "run", "services", "describe"]:
                traffic_rev = stale
                if any(cmd[:4] == ["gcloud", "run", "services", "update-traffic"] for cmd in calls[:-1]):
                    traffic_rev = fresh
                return {
                    "status": {
                        "latestReadyRevisionName": stale,
                        "traffic": [{"percent": 100, "revisionName": traffic_rev}],
                    }
                }
            if args[:4] == ["gcloud", "run", "revisions", "list"]:
                return [
                    {
                        "metadata": {"name": fresh, "labels": {"commit-sha": "abc123"}},
                        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                    },
                    {
                        "metadata": {"name": stale, "labels": {"commit-sha": "old999"}},
                        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                    },
                ]
            if args[:4] == ["gcloud", "run", "revisions", "describe"]:
                return _http_candidate(fresh, "abc123")
            if args[:4] == ["gcloud", "run", "services", "update-traffic"]:
                return ""
            self.fail(f"unexpected command: {args}")

        with mock.patch.object(rcr, "_run", side_effect=fake_run):
            rcr.ensure_latest_traffic(
                project="interactivebrokersquant",
                region="us-central1",
                targets=[target],
                expected_commit="abc123",
                dry_run=False,
            )

        traffic_cmds = [cmd for cmd in calls if cmd[:4] == ["gcloud", "run", "services", "update-traffic"]]
        self.assertEqual(len(traffic_cmds), 1)
        self.assertIn(f"--to-revisions={fresh}=100", traffic_cmds[0])

    def test_delete_legacy_schedulers_deletes_only_known_jobs(self) -> None:
        target = rcr.RuntimeTarget(service_name="interactive-brokers-quant-live-u1234-service")
        expected_jobs = {
            "interactive-brokers-quant-live-u1234-service-probe-scheduler",
            "interactive-brokers-quant-live-u1234-service-precheck-scheduler",
            "interactive-brokers-quant-live-u1234-probe-scheduler",
            "ibkr-u1234-backup-execution",
            "ibkr-u1234-pre-market-dry-run",
            "interactive-brokers-monitor-dispatcher-scheduler",
        }
        describe_calls: list[list[str]] = []
        delete_calls: list[list[str]] = []

        def fake_run_optional(args, *, dry_run=False):
            self.assertFalse(dry_run)
            describe_calls.append(list(args))
            self.assertEqual(args[:4], ["gcloud", "scheduler", "jobs", "describe"])
            self.assertIn(args[4], expected_jobs)
            self.assertIn("--location=us-central1", args)
            return True

        def fake_run(args, *, json_output=False, dry_run=False):
            self.assertFalse(json_output)
            self.assertFalse(dry_run)
            delete_calls.append(list(args))
            self.assertEqual(args[:4], ["gcloud", "scheduler", "jobs", "delete"])
            self.assertIn(args[4], expected_jobs)
            self.assertIn("--location=us-central1", args)
            return ""

        with mock.patch.object(rcr, "_run_optional", side_effect=fake_run_optional), mock.patch.object(
            rcr, "_run", side_effect=fake_run
        ):
            rcr.delete_legacy_schedulers(
                platform="ibkr",
                project="interactivebrokersquant",
                region="us-central1",
                scheduler_location="us-central1",
                targets=[target],
                env={},
                dry_run=False,
            )

        self.assertEqual({call[4] for call in describe_calls}, expected_jobs)
        self.assertEqual({call[4] for call in delete_calls}, expected_jobs)

if __name__ == "__main__":
    unittest.main()
