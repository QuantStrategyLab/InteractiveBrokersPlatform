"""Synthetic maintenance failures never authorize a policy write."""
import copy
import json
import io
import os
import tempfile
from contextlib import redirect_stdout
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import artifact_registry_maintenance as maintenance
from scripts.refresh_artifact_registry_keep import KEEP_ID
from tests.test_refresh_artifact_registry_keep import FakeGcloud, PROJECT, REGION, REPOSITORY, IMAGE_REPO, image

POLICY = Path(__file__).resolve().parents[1] / "config/artifact-registry-cleanup-policies.json"

class MaintenanceCloud(FakeGcloud):
    def __init__(self):
        super().__init__()
        self.repository["cleanupPolicies"] = {}
        self.corrupt_after = False

    def __call__(self, args):
        if args[:3] == ["artifacts", "repositories", "set-cleanup-policies"]:
            self.calls.append(args)
            path = next(a.split("=", 1)[1] for a in args if a.startswith("--policy="))
            self.pending = json.loads(Path(path).read_text())
            self.repository["cleanupPolicies"] = maintenance.api_policies(self.pending)
            if self.corrupt_after:
                self.repository["cleanupPolicyDryRun"] = False
            return ""
        return super().__call__(args)


def execute(fake, **kwargs):
    return maintenance.maintain(fake, project=PROJECT, region=REGION,
        repository_id=REPOSITORY, image_repo=IMAGE_REPO, candidate=image("c"),
        policy_file=POLICY, mode=kwargs.pop("mode", "repair"),
        dry_run=kwargs.pop("dry_run", True), **kwargs)

class MaintenanceTests(unittest.TestCase):
    def test_dry_run_and_inspect_never_write(self):
        for mode in ("inspect", "repair"):
            fake = MaintenanceCloud()
            result = execute(fake, mode=mode)
            self.assertIsNone(fake.pending)
            self.assertFalse(result["written"])
            self.assertEqual(result["protected_digest_count"], 3)

    def test_missing_policy_set_is_repaired_without_enabling_cleanup(self):
        fake = MaintenanceCloud()
        result = execute(fake, dry_run=False)
        self.assertTrue(result["written"])
        self.assertEqual(result["before_policy_count"], 0)
        self.assertEqual(result["after_policy_count"], 6)
        self.assertTrue(fake.repository["cleanupPolicyDryRun"])
        self.assertEqual(len(fake.pending), 6)
        self.assertEqual(fake.repository["cleanupPolicies"][KEEP_ID]["condition"]["versionNamePrefixes"], ["sha256:"])

    def test_missing_invalid_mode_or_failed_inventory_blocks(self):
        for scenario in ("missing", "string", "inventory", "transport", "identity"):
            fake = MaintenanceCloud()
            if scenario == "missing": del fake.repository["cleanupPolicyDryRun"]
            if scenario == "string": fake.repository["cleanupPolicyDryRun"] = "false"
            if scenario == "inventory": fake.service["status"]["traffic"] = []
            if scenario == "transport": fake.fail_on = ("run", "jobs", "list")
            if scenario == "identity": fake.repository["name"] += "-other"
            if scenario == "inventory": fake.service["status"].pop("latestReadyRevisionName", None)
            with self.subTest(scenario=scenario), self.assertRaises((ValueError, RuntimeError)):
                execute(fake, dry_run=False)
            self.assertIsNone(fake.pending)

    def test_stale_repository_blocks_concurrent_or_restarted_writer(self):
        fake = MaintenanceCloud()
        fake.mode_changes[2] = False
        with self.assertRaisesRegex(ValueError, "changed"):
            execute(fake, dry_run=False)
        self.assertIsNone(fake.pending)

    def test_failed_readback_or_mode_drift_is_not_success(self):
        for failure in ("transport", "mode"):
            fake = MaintenanceCloud()
            if failure == "transport": fake.fail_read_at = 3
            else: fake.corrupt_after = True
            with self.assertRaises((ValueError, RuntimeError)):
                execute(fake, dry_run=False)
            self.assertIsNotNone(fake.pending)

    def test_unbounded_delete_or_narrowed_keep_is_rejected(self):
        rows = json.loads(POLICY.read_text())
        for change in ("keep", "delete", "count", "extra", "recent-delete"):
            altered = copy.deepcopy(rows)
            if change == "keep": altered[0]["condition"]["versionNamePrefixes"] = ["sha256:a"]
            if change == "delete": altered[3]["condition"].pop("olderThan")
            if change == "count": altered.pop()
            if change == "extra": altered[0]["condition"]["tagPrefixes"] = ["narrow"]
            if change == "recent-delete":
                altered[1] = {"name": "keep-newest-ten-per-package", "action": {"type": "Delete"},
                              "condition": {"tagState": "untagged", "olderThan": None}}
            with self.subTest(change=change), self.assertRaises(ValueError):
                maintenance.api_policies(altered)

    def test_cloud_diagnostics_cannot_leak(self):
        import subprocess
        private = "synthetic-sensitive-cloud-diagnostic"
        with patch.object(maintenance.subprocess, "run", side_effect=subprocess.CalledProcessError(1, [private], stderr=private)):
            with self.assertRaisesRegex(RuntimeError, "Cloud command failed") as caught:
                maintenance.safe_gcloud([private])
        self.assertNotIn(private, str(caught.exception))

    def test_denied_or_unknown_permissions_stop_before_maintenance(self):
        for response in ({}, {"permissions": ["artifactregistry.repositories.get"]}):
            stream = io.BytesIO(json.dumps(response).encode())
            with patch.object(maintenance, "safe_gcloud", return_value="synthetic-token"), patch.object(maintenance.urllib.request, "urlopen", return_value=stream):
                with self.assertRaisesRegex(RuntimeError, "roles/artifactregistry.repoAdmin"):
                    maintenance.require_update_permission(PROJECT, REGION, REPOSITORY)

    def test_restart_rechecks_and_does_not_repeat_successful_write(self):
        fake = MaintenanceCloud()
        execute(fake, dry_run=False)
        fake.calls.clear()
        result = execute(fake, dry_run=False)
        self.assertFalse(result["written"])
        self.assertFalse(any(c[:3] == ["artifacts", "repositories", "set-cleanup-policies"] for c in fake.calls))

    def test_protected_configuration_missing_or_malformed_stops_before_auth(self):
        text = (POLICY.parents[1] / ".github/workflows/artifact-registry-maintenance.yml").read_text()
        code = text.split("python3 - <<'PYCONFIG'\n", 1)[1].split("          PYCONFIG", 1)[0]
        import textwrap
        code = textwrap.dedent(code)
        values = {"IBKR_GCP_PROJECT_ID": "synthetic-maintenance-project",
                  "IBKR_DEPLOY_SERVICE_ACCOUNT": "synthetic-deploy@synthetic-maintenance-project.iam.gserviceaccount.com",
                  "GCP_WORKLOAD_IDENTITY_PROVIDER": "projects/123456789012/locations/global/workloadIdentityPools/synthetic-pool/providers/synthetic-main",
                  "CLOUD_RUN_REGION": "us-central1", "IBKR_AR_CANDIDATE_DIGEST": "sha256:" + "c" * 64}
        for name in values:
            for bad in ("", "broken\nconfig"):
                with self.subTest(name=name, bad=bool(bad)), tempfile.TemporaryDirectory() as directory:
                    envfile = Path(directory) / "env"
                    altered = {**values, name: bad, "GITHUB_ENV": str(envfile)}
                    with patch.dict(os.environ, altered), redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                        exec(code, {})
                    self.assertFalse(envfile.exists())
        with tempfile.TemporaryDirectory() as directory:
            envfile = Path(directory) / "env"
            with patch.dict(os.environ, {**values, "GITHUB_ENV": str(envfile)}), redirect_stdout(io.StringIO()) as output:
                exec(code, {})
            self.assertEqual(len(envfile.read_text().splitlines()), 5)
            self.assertTrue(all(line.startswith("::add-mask::") for line in output.getvalue().splitlines()))

    def test_workflow_protects_auth_and_serializes_attempts(self):
        text = (POLICY.parents[1] / ".github/workflows/artifact-registry-maintenance.yml").read_text()
        self.assertIn("environment: ibkr-live", text)
        self.assertIn("cancel-in-progress: false", text)
        self.assertIn("github.ref == 'refs/heads/main'", text)
        self.assertNotIn("continue-on-error", text)
        self.assertIn("default: true", text)
        self.assertLess(text.index("Validate and mask protected configuration"), text.index("google-github-actions/auth@"))
        self.assertNotIn("${{ vars.", text)
        self.assertIn("::add-mask::", text)

if __name__ == "__main__":
    unittest.main()
