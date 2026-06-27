from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QPK_SRC = ROOT.parent / "QuantPlatformKit" / "src"
if str(QPK_SRC) not in sys.path:
    sys.path.insert(0, str(QPK_SRC))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quant_platform_kit.common.execution_capabilities import FRACTIONAL_SHARE_EXECUTION_CAPABILITY
from quant_platform_kit.common.strategies import PlatformCapabilityMatrix, StrategyCatalog, StrategyDefinition

_DCA_DEFINITION = StrategyDefinition(
    profile="nasdaq_sp500_smart_dca",
    domain="us_equity",
    supported_platforms=frozenset({"ibkr", "firstrade", "schwab", "longbridge"}),
    compatible_capabilities=frozenset({FRACTIONAL_SHARE_EXECUTION_CAPABILITY}),
)
_ROTATION_DEFINITION = StrategyDefinition(
    profile="global_etf_rotation",
    domain="us_equity",
    supported_platforms=frozenset({"ibkr"}),
    compatible_capabilities=frozenset(),
)
_FAKE_CATALOG = StrategyCatalog(
    definitions={
        "nasdaq_sp500_smart_dca": _DCA_DEFINITION,
        "ibit_smart_dca": StrategyDefinition(
            profile="ibit_smart_dca",
            domain="us_equity",
            supported_platforms=frozenset({"ibkr", "firstrade", "schwab", "longbridge"}),
            compatible_capabilities=frozenset({FRACTIONAL_SHARE_EXECUTION_CAPABILITY}),
        ),
        "global_etf_rotation": _ROTATION_DEFINITION,
    }
)
_FAKE_CAPABILITY_MATRIX = PlatformCapabilityMatrix(
    platform_id="ibkr",
    supported_domains=frozenset({"us_equity"}),
    supported_target_modes=frozenset({"weight", "value"}),
    supported_inputs=frozenset(),
    supported_capabilities=frozenset({"broker_client"}),
)

_fake_registry = types.ModuleType("strategy_registry")
_fake_registry.PLATFORM_CAPABILITY_MATRIX = _FAKE_CAPABILITY_MATRIX
_fake_registry.STRATEGY_CATALOG = _FAKE_CATALOG
sys.modules["strategy_registry"] = _fake_registry

runtime_execution_policy = importlib.import_module("runtime_execution_policy")
runtime_execution_policy = importlib.reload(runtime_execution_policy)

IBKR_FRACTIONAL_EQUITY_API_UNSUPPORTED_SKIP_REASON = (
    runtime_execution_policy.IBKR_FRACTIONAL_EQUITY_API_UNSUPPORTED_SKIP_REASON
)
dca_execution_unsupported_reason = runtime_execution_policy.dca_execution_unsupported_reason
notional_buy_execution_enabled = runtime_execution_policy.notional_buy_execution_enabled


class RuntimeExecutionPolicyTests(unittest.TestCase):
    def test_dca_profiles_are_blocked_on_ibkr(self) -> None:
        for profile in ("nasdaq_sp500_smart_dca", "ibit_smart_dca"):
            self.assertEqual(
                dca_execution_unsupported_reason(profile),
                IBKR_FRACTIONAL_EQUITY_API_UNSUPPORTED_SKIP_REASON,
            )
            self.assertFalse(notional_buy_execution_enabled(profile))

    def test_rotation_profile_does_not_enable_notional_buy(self) -> None:
        self.assertFalse(notional_buy_execution_enabled("global_etf_rotation"))


if __name__ == "__main__":
    unittest.main()
