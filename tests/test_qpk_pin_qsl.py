"""Exercise the local pin check without contacting GitHub."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_qpk_pin_consistency.py"
TARGET = "a" * 40
OLD = "b" * 40


class QslPinCheckTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "0"\n'
            f'dependencies = ["quant-platform-kit @ git+https://github.com/QuantStrategyLab/QuantPlatformKit.git@{TARGET}"]\n'
        )
        workflows = self.root / ".github" / "workflows"
        workflows.mkdir(parents=True)
        self.workflow = workflows / "historical.yml"
        self.workflow.write_text(f"env:\n  QPK_OFFLINE_SHA: {OLD}\n")

    def run_check(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args], cwd=self.root,
            env={**os.environ, "QPK_EXPECTED_PIN": TARGET},
            capture_output=True, text=True, check=False,
        )

    def write_qsl(self, pin):
        (self.root / "qsl.toml").write_text(
            f'[qsl.requires]\nquant_platform_kit = "{pin}"\n'
            f'us_equity_strategies = "{OLD}"\n'
        )

    def test_matching_pin_passes_and_preserves_historical_pin(self):
        self.write_qsl(TARGET)
        self.assertEqual(self.run_check().returncode, 0)
        self.assertIn(OLD, self.workflow.read_text())

    def test_mismatched_pin_fails_and_fix_does_not_change_history(self):
        self.write_qsl(OLD)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("qsl.toml", result.stdout)
        self.run_check("--fix")
        self.assertEqual(self.run_check().returncode, 0)
        self.assertIn(f'us_equity_strategies = "{OLD}"', (self.root / "qsl.toml").read_text())
        self.assertIn(OLD, self.workflow.read_text())

    def test_malformed_qsl_fails_closed(self):
        self.write_qsl("bad")
        self.assertEqual(self.run_check().returncode, 1)
