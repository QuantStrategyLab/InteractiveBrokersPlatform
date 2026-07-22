from __future__ import annotations

import json
import unittest
from unittest import mock

from scripts import reconcile_cloud_runtime as rcr


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

    def test_ensure_latest_traffic_updates_then_verifies_latest_commit(self) -> None:
        target = rcr.RuntimeTarget(
            service_name="interactive-brokers-quant-live-u1234-service",
            region="us-central1",
        )
        service_before = {
            "status": {
                "latestReadyRevisionName": "interactive-brokers-quant-live-u1234-service-00001-abc",
                "traffic": [
                    {"percent": 100, "revisionName": "interactive-brokers-quant-live-u1234-service-00000-old"}
                ],
            }
        }
        service_after = {
            "status": {
                "latestReadyRevisionName": "interactive-brokers-quant-live-u1234-service-00001-abc",
                "traffic": [
                    {
                        "percent": 100,
                        "latestRevision": True,
                        "revisionName": "interactive-brokers-quant-live-u1234-service-00001-abc",
                    }
                ],
            }
        }
        revision = {"metadata": {"labels": {"commit-sha": "abc123"}}}
        calls: list[list[str]] = []
        service_describe_count = 0

        def fake_run(args, *, json_output=False, dry_run=False):
            nonlocal service_describe_count
            calls.append(list(args))
            self.assertFalse(dry_run)
            if args[:4] == ["gcloud", "run", "services", "describe"]:
                service_describe_count += 1
                return service_before if service_describe_count == 1 else service_after
            if args[:4] == ["gcloud", "run", "revisions", "describe"]:
                return revision
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
        self.assertTrue(any(cmd[:4] == ["gcloud", "run", "services", "update-traffic"] for cmd in calls))
        self.assertTrue(any(cmd[:4] == ["gcloud", "run", "revisions", "describe"] for cmd in calls))

    def test_ensure_latest_traffic_requires_latest_ready_revision(self) -> None:
        target = rcr.RuntimeTarget(service_name="interactive-brokers-quant-live-u1234-service", region="us-central1")

        def fake_run(args, *, json_output=False, dry_run=False):
            if args[:4] == ["gcloud", "run", "services", "describe"]:
                return {
                    "status": {
                        "latestCreatedRevisionName": "interactive-brokers-quant-live-u1234-service-00001-abc",
                        "traffic": [],
                    }
                }
            self.fail(f"unexpected command: {args}")

        with mock.patch.object(rcr, "_run", side_effect=fake_run):
            with self.assertRaises(rcr.ReconcileError):
                rcr.ensure_latest_traffic(
                    project="interactivebrokersquant",
                    region="us-central1",
                    targets=[target],
                    expected_commit="abc123",
                    dry_run=False,
                )

    def test_ensure_latest_traffic_rejects_commit_mismatch(self) -> None:
        target = rcr.RuntimeTarget(service_name="interactive-brokers-quant-live-u1234-service", region="us-central1")

        def fake_run(args, *, json_output=False, dry_run=False):
            if args[:4] == ["gcloud", "run", "services", "describe"]:
                return {
                    "status": {
                        "latestReadyRevisionName": "interactive-brokers-quant-live-u1234-service-00001-abc",
                        "traffic": [
                            {
                                "percent": 100,
                                "latestRevision": True,
                                "revisionName": "interactive-brokers-quant-live-u1234-service-00001-abc",
                            }
                        ],
                    }
                }
            if args[:4] == ["gcloud", "run", "revisions", "describe"]:
                return {"metadata": {"labels": {"commit-sha": "wrong-sha"}}}
            self.fail(f"unexpected command: {args}")

        with mock.patch.object(rcr, "_run", side_effect=fake_run):
            with self.assertRaises(rcr.ReconcileError):
                rcr.ensure_latest_traffic(
                    project="interactivebrokersquant",
                    region="us-central1",
                    targets=[target],
                    expected_commit="abc123",
                    dry_run=False,
                )

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
