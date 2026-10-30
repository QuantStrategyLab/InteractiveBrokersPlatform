"""Synthetic-only tests for fixed-output target observation."""
import unittest
from scripts import release_target_observation as observer


class ObservationTest(unittest.TestCase):
    def test_real_release_shape_and_comparisons(self):
        sha = "a" * 40
        release = {"release_id": "synthetic-release", "strategy_revision": sha,
                   "effective_session": "2026-10-30"}
        for name in ("manifest_sha256", "config_sha256", "risk_policy_sha256",
                     "evidence_sha256", "plugin_bundle_sha256"):
            release[name] = "b" * 64
        target = {"strategy_release": release, "runtime_risk_limits": {"binding": {"ues_revision": "c" * 40}}}
        result = observer.classify(target, sha)
        self.assertEqual(result, {
            "release_identity": "valid", "release_revision": "matches_source_lock",
            "risk_binding_revision": "differs_from_source_lock",
            "source_lock_revision": "observed", "artifact_qualification": "not_assessed",
        })
        del release["evidence_sha256"]
        self.assertEqual(observer.classify(target, sha)["release_identity"], "invalid")

    def test_missing_invalid_and_unavailable_comparison(self):
        private = "private-untrusted-value"
        result = observer.classify({"strategy_release": {"strategy_revision": private},
                                    "runtime_risk_limits": {"binding": private}}, None)
        self.assertEqual(result["release_identity"], "invalid")
        self.assertEqual(result["release_revision"], "invalid")
        self.assertEqual(result["risk_binding_revision"], "invalid")
        self.assertEqual(result["source_lock_revision"], "unavailable")
        self.assertNotIn(private, str(result))
        missing = observer.classify({}, None)
        self.assertEqual(missing["release_identity"], "absent")
        self.assertEqual(missing["risk_binding_revision"], "absent")
        result = observer.classify({"strategy_release": {"strategy_revision": "a" * 40}}, None)
        self.assertEqual(result["release_revision"], "comparison_unavailable")
        self.assertEqual(result["artifact_qualification"], "not_assessed")


if __name__ == "__main__":
    unittest.main()
