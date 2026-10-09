"""N13: IBKR adapter around QPK execution_kernel T1 (claim consult-only)."""

from __future__ import annotations

import unittest

from quant_platform_kit.execution_kernel import ExecutionMode

from application.execution_kernel_adapter import consult_t1_live_submit


class ExecutionKernelAdapterTests(unittest.TestCase):
    def test_live_without_identity_denies_missing_durable_identity(self):
        decision = consult_t1_live_submit(identity_held=False)
        self.assertEqual(decision.decision, "deny")
        self.assertEqual(decision.reason_code, "missing_durable_identity")
        self.assertFalse(decision.allowed)

    def test_live_with_identity_allows(self):
        decision = consult_t1_live_submit(identity_held=True)
        self.assertEqual(decision.decision, "allow")
        self.assertEqual(decision.reason_code, "ok")
        self.assertTrue(decision.allowed)

    def test_dry_run_bypass_allows_without_identity(self):
        decision = consult_t1_live_submit(
            identity_held=False,
            dry_run_bypass=True,
            mode=ExecutionMode.LIVE,
        )
        self.assertEqual(decision.decision, "allow")
        self.assertTrue(decision.allowed)

    def test_paper_mode_allows_without_identity(self):
        decision = consult_t1_live_submit(
            identity_held=False,
            mode=ExecutionMode.PAPER,
        )
        self.assertEqual(decision.decision, "allow")
        self.assertTrue(decision.allowed)


if __name__ == "__main__":
    unittest.main()
