"""Retired profiles remain readable but cannot be selected by the platform."""

import pytest

from hk_equity_strategies import get_platform_runtime_adapter, get_strategy_catalog
from strategy_loader import load_strategy_definition
from strategy_registry import get_supported_profiles_for_platform, resolve_strategy_definition


RETIRED_PROFILES = ('hk_global_etf_tactical_rotation', 'hk_low_vol_dividend_quality_snapshot')


@pytest.mark.parametrize("profile", RETIRED_PROFILES)
def test_retired_profiles_are_not_selectable(profile):
    assert profile not in get_supported_profiles_for_platform("ibkr")
    with pytest.raises(ValueError, match="Unsupported STRATEGY_PROFILE"):
        resolve_strategy_definition(profile, platform_id="ibkr")
    with pytest.raises(ValueError, match="Unsupported STRATEGY_PROFILE"):
        load_strategy_definition(profile)


@pytest.mark.parametrize("profile", RETIRED_PROFILES)
def test_retired_profile_aliases_are_not_selectable(profile):
    catalog = get_strategy_catalog()
    for alias, canonical in catalog.profile_aliases.items():
        if canonical == profile:
            with pytest.raises(ValueError, match="Unsupported STRATEGY_PROFILE"):
                resolve_strategy_definition(alias, platform_id="ibkr")


@pytest.mark.parametrize("profile", RETIRED_PROFILES)
def test_retired_profile_history_and_adapters_remain_available(profile):
    catalog = get_strategy_catalog()
    assert profile in catalog.definitions
    assert profile in catalog.metadata
    assert get_platform_runtime_adapter(profile, platform_id="ibkr") is not None
