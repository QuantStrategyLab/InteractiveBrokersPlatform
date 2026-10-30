"""Offline account observation regressions; no broker calls."""

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from application.account_facts import build_ibkr_account_facts
from application.ibkr_cash_observations import capture_cash_observation_rows


class AccountRiskFactReadinessTest(unittest.TestCase):
    def facts(self, extra=()):
        cash = SimpleNamespace(account="TEST", currency="USD", tag="CashBalance", value="100", modelCode="")
        rows = capture_cash_observation_rows((cash, *extra), account_ids=("TEST",), source_method="account_values_cache")
        snapshot = SimpleNamespace(as_of=datetime(2026, 1, 1, tzinfo=timezone.utc), positions=(), metadata={
            "account_ids": ("TEST",),
            "cash_balances": ({"account_id": "TEST", "currency": "USD", "CashBalance": 100},),
            "cash_observation_rows": rows,
        })
        return build_ibkr_account_facts(snapshot)

    def settled(self, value="42", model=""):
        return SimpleNamespace(account="TEST", currency="USD", tag="SettledCash", value=value, modelCode=model)

    def test_cash_balance_is_not_settled_cash_and_missing_facts_block_readiness(self):
        facts = self.facts()
        receipt = facts["account_risk_fact_readiness"]
        self.assertEqual(facts["cash"][0]["source_tag"], "CashBalance")
        self.assertEqual(receipt["settled_cash"]["status"], "MISSING")
        self.assertEqual(receipt["status"], "NOT_READY")
        self.assertEqual(receipt["order_observation_watermark"], "MISSING")
        self.assertEqual(receipt["broker_freshness"], "UNVERIFIED")

    def test_native_account_scoped_settled_cash_keeps_provenance(self):
        facts = self.facts((self.settled(),))
        settled = facts["account_risk_fact_readiness"]["settled_cash"]
        self.assertEqual(settled, {"status": "OBSERVED", "currency": "USD", "source_tag": "SettledCash",
            "value": "42", "source_method": "account_values_cache", "model_scope": "no_model_code",
            "observed_at": facts["observed_at"]})
        self.assertEqual(facts["account_risk_fact_readiness"]["status"], "NOT_READY")

    def test_invalid_duplicate_and_model_scoped_values_do_not_certify_account_cash(self):
        for rows, status in [((self.settled("NaN"),), "INVALID"),
                             ((self.settled(), self.settled()), "AMBIGUOUS"),
                             ((self.settled(model="MODEL"),), "UNVERIFIED_SCOPE"),
                             ((self.settled(model=None),), "UNVERIFIED_SCOPE")]:
            with self.subTest(status=status):
                settled = self.facts(rows)["account_risk_fact_readiness"]["settled_cash"]
                self.assertEqual(settled["status"], status)
                self.assertNotIn("value", settled)

    def test_missing_invalid_or_wrong_account_cash_does_not_create_account_facts(self):
        for rows in ((),
                     ({"account_id": "TEST", "currency": "USD", "CashBalance": "invalid"},),
                     ({"account_id": "OTHER", "currency": "USD", "CashBalance": "100"},)):
            with self.subTest(rows=rows):
                snapshot = SimpleNamespace(as_of=datetime(2026, 1, 1, tzinfo=timezone.utc), positions=(),
                    metadata={"account_ids": ("TEST",), "cash_balances": rows})
                self.assertEqual(build_ibkr_account_facts(snapshot), {})

def test_receipt_survives_account_bound_report_serialization(strategy_module_factory, monkeypatch, tmp_path):
    import json
    import sys
    from application.cycle_result import StrategyCycleResult
    from quant_platform_kit.common.runtime_reports import finalize_runtime_report, write_runtime_report_json

    # The existing factory replaces sys.modules["main"]. Restore its prior
    # state as well as the factory's monkeypatched environment after the test.
    monkeypatch.setitem(sys.modules, "main", sys.modules.get("main"))
    main = strategy_module_factory()
    cases = AccountRiskFactReadinessTest()
    facts = cases.facts((cases.settled(),))
    cycle = StrategyCycleResult(result="ok", account_facts=facts)
    target = SimpleNamespace(account_selector=("TEST",))
    with patch.object(main, "RUNTIME_SETTINGS", SimpleNamespace(runtime_target=target)):
        summary = main._build_cycle_report_summary(cycle, {}, {}, dry_run=False)
    report = finalize_runtime_report({}, status="ok", summary=summary)
    path = tmp_path / "report.json"
    write_runtime_report_json(report, output_path=path)
    restored = json.loads(path.read_text())
    assert restored["summary"]["account_facts"]["account_risk_fact_readiness"] == facts["account_risk_fact_readiness"]
    with patch.object(main, "RUNTIME_SETTINGS", SimpleNamespace(runtime_target=SimpleNamespace(account_selector=("OTHER",)))):
        assert "account_facts" not in main._build_cycle_report_summary(cycle, {}, {}, dry_run=False)


if __name__ == "__main__":
    unittest.main()
