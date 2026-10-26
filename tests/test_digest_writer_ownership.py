"""Serving metadata decides ownership; fixtures never contact a service."""
import copy
import io
import json
import subprocess
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import digest_writer_ownership as guard

ENV = {"STORE_TARGET": "primary-live", "GCP_PROJECT_ID": "synthetic-project",
       "IBKR_ACCOUNT_FACTS_SERVICE_NAME": "synthetic-service",
       "DIGEST_CANDIDATES_GCS_URI": "gs://synthetic/candidates"}
SERVICE = {"metadata": {"name": "synthetic-service", "labels": {"cloud.googleapis.com/location": "synthetic-region"}},
           "status": {"traffic": [{"revisionName": "serving-synthetic", "percent": 100}],
                      "latestCreatedRevisionName": "unserved-synthetic"}}
VALUES = {"IBKR_UNIFIED_NOTIFICATION_ENABLED": "true", "DIGEST_STORE_TARGET": "primary-live",
          "DIGEST_OPAQUE_ACCOUNT_UID": "synthetic-uid", "DIGEST_TARGET_ID": "synthetic/target",
          "DIGEST_CANDIDATES_GCS_URI": ENV["DIGEST_CANDIDATES_GCS_URI"]}


def runner(values=None, service=None):
    revision = {"spec": {"containers": [{"env": [{"name": k, "value": v}
                                                    for k, v in (VALUES if values is None else values).items()]}]}}
    return mock.Mock(side_effect=[subprocess.CompletedProcess([], 0, json.dumps([SERVICE if service is None else service])),
                                  subprocess.CompletedProcess([], 0, json.dumps(revision))])


class OwnershipTests(unittest.TestCase):
    def test_other_slots_never_read_cloud_metadata(self):
        read = mock.Mock(side_effect=AssertionError("no cloud read"))
        self.assertFalse(guard.runtime_owns_slot({**ENV, "STORE_TARGET": "additional-2"}, runner=read))
        read.assert_not_called()

    def test_serving_revision_owns_slot_when_an_unserved_candidate_exists(self):
        read = runner()
        self.assertTrue(guard.runtime_owns_slot(ENV, runner=read))
        command = read.call_args_list[1].args[0]
        self.assertIn("serving-synthetic", command)
        self.assertNotIn("unserved-synthetic", command)
        self.assertFalse(any(x in command for x in ("update", "replace", "execute")))

    def test_false_or_missing_flag_keeps_legacy_path(self):
        for values in ({}, {"IBKR_UNIFIED_NOTIFICATION_ENABLED": "false"}):
            self.assertFalse(guard.runtime_owns_slot(ENV, runner=runner(values)))

    def test_split_traffic_and_missing_service_are_unknown(self):
        service = copy.deepcopy(SERVICE)
        service["status"]["traffic"] = [{"revisionName": "one", "percent": 50}, {"revisionName": "two", "percent": 50}]
        for value in (service, {**SERVICE, "metadata": {"name": "other-service"}}):
            with self.assertRaises(ValueError):
                guard.runtime_owns_slot(ENV, runner=runner(service=value))

    def test_enabled_writer_requires_matching_slot_namespace_and_identity(self):
        for key in ("DIGEST_STORE_TARGET", "DIGEST_OPAQUE_ACCOUNT_UID", "DIGEST_TARGET_ID", "DIGEST_CANDIDATES_GCS_URI"):
            options = ("", "other-value") if key in {"DIGEST_STORE_TARGET", "DIGEST_CANDIDATES_GCS_URI"} else ("",)
            for value in options:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    guard.runtime_owns_slot(ENV, runner=runner({**VALUES, key: value}))

    def test_read_failure_stops_without_logging_private_client_error(self):
        read = mock.Mock(return_value=subprocess.CompletedProcess([], 1, "", "private-service-and-config"))
        with self.assertRaises(ValueError):
            guard.runtime_owns_slot(ENV, runner=read)
        self.assertEqual(read.call_count, 1)
        output = io.StringIO()
        with mock.patch.object(guard, "runtime_owns_slot", side_effect=ValueError("private-service-and-config")), redirect_stdout(output):
            self.assertEqual(guard.main(), 2)
        self.assertNotIn("private-service-and-config", output.getvalue())

    def test_workflow_checks_primary_ownership_before_private_store_write(self):
        body = (Path(__file__).resolve().parents[1] / ".github/workflows/digest-candidates-emit.yml").read_text()
        self.assertLess(body.index("python -m scripts.digest_writer_ownership"), body.index('gcloud storage cp "$CANDIDATES_PATH"'))
        self.assertIn('if [[ "$STORE_TARGET" == "primary-live" ]]', body)
        self.assertIn('exit "$ownership_status"', body)


if __name__ == "__main__":
    unittest.main()
