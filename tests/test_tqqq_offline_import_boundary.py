"""Default installs neither load the research stack nor activate its adapter."""
import ast
from pathlib import Path
import subprocess
import sys


def test_offline_module_import_does_not_load_research_or_require_its_overlay():
    code = """
import sys
from application.tqqq_offline_risk import run_tqqq_offline_risk
assert not any(k.startswith('us_equity_strategies.research') for k in sys.modules)
out=run_tqqq_offline_risk(None,runtime=None,portfolio_snapshot=None,packet=None,
    event_context=None,journal=None,binding=None,action='read')
assert out['new_risk_prohibited'] and out['draft_settings'] is None
assert not out['settings_applied'] and not out['execution_authorized']
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_production_composer_and_dispatch_do_not_import_offline_adapter():
    root = Path(__file__).resolve().parents[1]
    for filename in ("main.py", "strategy_runtime.py", "application/runtime_composer.py"):
        tree = ast.parse((root / filename).read_text())
        imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
        assert all("tqqq_offline_risk" not in ast.unparse(node) for node in imports)
