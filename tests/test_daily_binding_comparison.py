import unittest
from unittest.mock import patch

from scripts.daily_binding_protocol import digest
from scripts import compare_website_daily_binding as adapter


class DailyBindingComparisonTests(unittest.TestCase):
    def setUp(self):
        self.binding = dict(account_key="synthetic-key", account_scope="live", account_selector="U1234567",
                            deployment_selector="synthetic-deployment", platform="ibkr", service_name="synthetic-service",
                            source_binding_id="b" * 64, strategy_profile="tqqq_growth_income", target_id="synthetic-target")
        self.nonce = "a" * 64

    def test_canonical_matches_node_fixture_and_detects_changes(self):
        value = digest(self.binding, self.nonce)
        self.assertEqual(value, "22022fdc1b35acdf54a50fce9b8860ec1b22912edb0fc09d74242f2b0fb7b442")
        self.assertEqual(value, digest(dict(reversed(list(self.binding.items()))), self.nonce))
        for name in ("account_key", "account_scope", "account_selector", "deployment_selector", "service_name", "target_id"):
            self.assertNotEqual(value, digest({**self.binding, name: self.binding[name] + "x"}, self.nonce))
        self.assertNotEqual(value, digest({**self.binding, "source_binding_id": "c" * 64}, self.nonce))
        self.assertNotEqual(value, digest(self.binding, "c" * 64))

    def test_invalid_inputs_rejected(self):
        for binding, nonce in (({k: v for k, v in self.binding.items() if k != "target_id"}, self.nonce),
                               ({**self.binding, "platform": "other"}, self.nonce),
                               ({**self.binding, "account_key": "中文"}, self.nonce),
                               ({**self.binding, "source_binding_id": "c" * 63}, self.nonce),
                               (self.binding, "A" * 64)):
            with self.assertRaises(ValueError):
                digest(binding, nonce)

    def test_adapter_rejects_mismatched_period_source(self):
        expected = {"target_id": "synthetic-target", "expected_report_prefix": "gs://synthetic/tqqq_growth_income/",
                    "expected_project_id": "synthetic-project", "expected_service_name": "synthetic-service",
                    "expected_runtime_revision": "revision", "expected_account_scope": "live",
                    "expected_account_selector": ("U1234567",), "expected_deployment_selector": "synthetic-deployment"}
        env = {"DAILY_COMPARISON_NONCE": self.nonce, "IBKR_PERIOD_RETURN_ACCOUNT_KEY": "synthetic-key",
               "IBKR_PERIOD_RETURN_ACCOUNT_SCOPE": "live", "IBKR_PERIOD_RETURN_SOURCE_BINDING_ID": "b" * 64,
               "IBKR_PERIOD_RETURN_TARGET_ID": "synthetic-target"}
        with patch.object(adapter.identity, "resolve_expected", return_value=expected), \
             patch.object(adapter.publisher, "_bound_source_id", return_value="b" * 64), \
             patch.dict(adapter.os.environ, env, clear=True):
            self.assertEqual(adapter.main(), digest(self.binding, self.nonce))
            with patch.dict(adapter.os.environ, {"IBKR_PERIOD_RETURN_SOURCE_BINDING_ID": "c" * 64}):
                with self.assertRaises(ValueError):
                    adapter.main()
            with patch.object(adapter.identity, "resolve_expected", return_value={**expected, "expected_account_selector": ("U1234567", "U7654321")}):
                with self.assertRaises(ValueError):
                    adapter.main()


if __name__ == "__main__":
    unittest.main()
