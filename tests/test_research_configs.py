import importlib.util
import sys
from pathlib import Path


def load_research_module():
    path = Path(__file__).resolve().parents[1] / "research" / "backtest_qqq_variants.py"
    spec = importlib.util.spec_from_file_location("backtest_qqq_variants_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_build_configs_includes_voo_xlk_smh_variants():
    module = load_research_module()

    configs = module.build_configs([0.2, 0.3, 0.4])
    configs_by_name = {config.name: config for config in configs}

    assert "baseline_non_tech" in configs_by_name
    assert "current_default_qqq" in configs_by_name
    assert "proposed_voo_xlk_smh" in configs_by_name
    assert "replace_qqq_with_voo" in configs_by_name
    assert "voo_plus_xlk" in configs_by_name
    assert "voo_plus_xlk_plus_smh" in configs_by_name

    assert "VOO" in configs_by_name["replace_qqq_with_voo"].ranking_pool
    assert "XLK" in configs_by_name["voo_plus_xlk"].ranking_pool
    assert "SMH" in configs_by_name["voo_plus_xlk_plus_smh"].ranking_pool
