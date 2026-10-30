import hashlib
import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from scripts import diagnose_account_facts_identity as diagnostic
from scripts import prepare_additional_account_facts_recovery as recovery


class ServingRevisionEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
        self.expected = {
            "target_id": "sample-target", "expected_report_prefix": "gs://sample/reports",
            "expected_project_id": "project", "expected_service_name": "service",
            "expected_runtime_revision": "old-revision", "expected_account_scope": "live",
            "expected_account_selector": ("U123456",), "expected_deployment_selector": "deployment",
        }
        self.revision = "service-new-revision"
        self.report = {
            "schema_version": "runtime_report.v1", "platform": "interactive_brokers",
            "deploy_target": "cloud_run", "project_id": "project", "service_name": "service",
            "account_scope": "live", "runtime_target": {"account_selector": ["U123456"],
                "deployment_selector": "deployment"},
            "diagnostics": {"runtime_revision": self.revision},
            "started_at": (self.now - timedelta(seconds=2)).isoformat(),
            "finished_at": self.now.isoformat(),
            "summary": {"account_facts": {"schema_version": "ibkr_account_snapshot.v1",
                "account_ids": ["U123456"], "currency": "USD",
                "observed_at": (self.now - timedelta(seconds=1)).isoformat(),
                "net_assets": "12", "cash": [{"currency": "USD", "cash_balance": "1", "source_tag": "CashBalance"}]}},
        }
        self.metadata = {"metadata": {"name": "service"}, "status": {
            "latestReadyRevisionName": self.revision, "latestCreatedRevisionName": self.revision,
            "traffic": [{"revisionName": self.revision, "percent": 100}]}}

    def diagnose(self):
        return diagnostic.diagnose_identity(self.report, expected=self.expected,
            source_report_uri="gs://sample/reports/2026-10/20261030T115958Z.json",
            now=self.now, cloud_run_metadata=self.metadata)

    def test_only_observes_fresh_report_and_single_serving_revision(self):
        result = self.diagnose()
        self.assertFalse(result["projection_passed"])
        evidence = result["serving_revision_evidence"]
        self.assertEqual(evidence, {"status": "observed", "serving_revision_sha256": recovery.sha(self.revision)})
        self.assertEqual(evidence["serving_revision_sha256"], hashlib.sha256(self.revision.encode("utf-8")).hexdigest())
        self.assertNotIn(self.revision, str(result))

    def test_missing_failed_stale_and_split_are_never_evidence(self):
        for case in ("no_metadata", "wrong_service", "no_traffic", "split", "invalid_percent", "report_differs", "stale", "wrong_account", "bad_receipt"):
            with self.subTest(case=case):
                self.setUp()
                if case == "no_metadata": self.metadata = None
                elif case == "wrong_service": self.metadata["metadata"]["name"] = "other"
                elif case == "no_traffic": self.metadata["status"].pop("traffic")
                elif case == "split": self.metadata["status"]["traffic"] = [
                    {"revisionName": self.revision, "percent": 50}, {"revisionName": "other", "percent": 50}]
                elif case == "invalid_percent": self.metadata["status"]["traffic"][0]["percent"] = 99
                elif case == "report_differs": self.report["diagnostics"]["runtime_revision"] = "other"
                elif case == "stale": self.report["summary"]["account_facts"]["observed_at"] = (self.now - timedelta(hours=37)).isoformat()
                elif case == "wrong_account": self.report["summary"]["account_facts"]["account_ids"] = ["U999999"]
                else: self.report["runtime_release_receipt"] = {"runtime_revision": "other"}
                self.assertNotIn("serving_revision_evidence", self.diagnose())

    def test_cli_emits_evidence_but_still_rejects_drift(self):
        uri = "gs://sample/reports/2026-10/20261030T115958Z.json"
        output = io.StringIO()
        with (patch.object(diagnostic, "resolve_expected", return_value=self.expected),
              patch.object(diagnostic.publisher, "select_account_facts_report", return_value=(uri, self.report)),
              patch.object(diagnostic, "_read_cloud_run_metadata", return_value=self.metadata),
              patch.dict(diagnostic.os.environ, {"IBKR_ACCOUNT_FACTS_TARGET": "additional-1"}),
              redirect_stdout(output)):
            code = diagnostic.main([], now=self.now)
        result = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(result["reason"], "runtime_target_mismatch")
        self.assertEqual(result["serving_revision_evidence"]["serving_revision_sha256"], recovery.sha(self.revision))
        self.assertNotIn(self.revision, output.getvalue())


if __name__ == "__main__":
    unittest.main()
