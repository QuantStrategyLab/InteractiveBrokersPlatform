from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd


MODULE_PATH = Path(__file__).resolve().parents[1] / "research" / "backtest_tqqq_growth_indicator_variants.py"
spec = importlib.util.spec_from_file_location("tqqq_variants", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_confirmation_gate_respects_entry_and_exit_streaks():
    raw = pd.Series([False, True, True, True, False, False, True])
    active = module.apply_confirmation_gate(raw, entry_confirm_days=2, exit_confirm_days=2)
    assert active.tolist() == [False, False, True, True, True, False, False]


def test_build_target_weights_allocates_idle_to_selected_asset():
    weights = module.build_target_weights(
        current_equity=100.0,
        target_tqqq_value=30.0,
        idle_value=65.0,
        reserved_value=5.0,
        idle_asset="QQQ",
    )
    assert weights == {"TQQQ": 0.3, "QQQ": 0.65, "CASH": 0.05}

    cash_weights = module.build_target_weights(
        current_equity=100.0,
        target_tqqq_value=30.0,
        idle_value=65.0,
        reserved_value=5.0,
        idle_asset="CASH",
    )
    assert cash_weights == {"TQQQ": 0.3, "CASH": 0.7}


def test_result_files_are_written(tmp_path):
    comparison = pd.DataFrame([{"a": 1}])
    comparison_path = tmp_path / "comparison.csv"
    comparison.to_csv(comparison_path, index=False)
    recommendation_path = tmp_path / "recommendation.json"
    recommendation_path.write_text('{"ok": true}', encoding="utf-8")
    summary_path = tmp_path / "summary.md"
    summary_path.write_text("# ok\n", encoding="utf-8")

    assert comparison_path.read_text(encoding="utf-8").strip()
    assert recommendation_path.read_text(encoding="utf-8").strip()
    assert summary_path.read_text(encoding="utf-8").strip()
