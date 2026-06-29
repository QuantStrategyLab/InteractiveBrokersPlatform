from __future__ import annotations

from quant_platform_kit.common.execution_capabilities import (
    FRACTIONAL_SHARE_EXECUTION_SKIP_REASON,
    definition_requires_fractional_share_execution,
    platform_supports_fractional_share_execution,
)
from quant_platform_kit.common.strategies import normalize_profile_name
from strategy_registry import PLATFORM_CAPABILITY_MATRIX, STRATEGY_CATALOG

# IBKR Campus documents that TWS / Client Portal APIs do not support fractional
# equity share trading (API errors 10242/10243). Desktop TWS may allow dollar
# amounts, but automated DCA must stay disabled on this platform until IBKR
# exposes a supported API path.
IBKR_FRACTIONAL_EQUITY_API_UNSUPPORTED_SKIP_REASON = "ibkr_fractional_equity_api_unsupported"


def dca_execution_unsupported_reason(strategy_profile: str) -> str | None:
    # IBKR TWS API *hard-rejects* notional/fractional equity orders at the
    # protocol level (error codes 10242/10243).  Unlike other platforms that
    # can fall back to whole-share compat mode, IBKR must keep DCA profiles
    # blocked regardless of the soft-capability change in QPK.
    normalized_profile = normalize_profile_name(strategy_profile)
    definition = STRATEGY_CATALOG.definitions.get(normalized_profile)
    if definition is None:
        return None
    if definition_requires_fractional_share_execution(definition):
        if not platform_supports_fractional_share_execution(
            capability_matrix=PLATFORM_CAPABILITY_MATRIX
        ):
            return IBKR_FRACTIONAL_EQUITY_API_UNSUPPORTED_SKIP_REASON
    return None


def notional_buy_execution_enabled(strategy_profile: str) -> bool:
    if dca_execution_unsupported_reason(strategy_profile) is not None:
        return False
    normalized_profile = normalize_profile_name(strategy_profile)
    definition = STRATEGY_CATALOG.definitions.get(normalized_profile)
    if definition is None:
        return False
    return definition_requires_fractional_share_execution(definition)


__all__ = (
    "FRACTIONAL_SHARE_EXECUTION_SKIP_REASON",
    "IBKR_FRACTIONAL_EQUITY_API_UNSUPPORTED_SKIP_REASON",
    "dca_execution_unsupported_reason",
    "notional_buy_execution_enabled",
)
