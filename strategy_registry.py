from __future__ import annotations

from us_equity_strategies import (
    get_platform_runtime_adapter as get_us_platform_runtime_adapter,
    get_runtime_enabled_profiles as get_us_runtime_enabled_profiles,
    get_strategy_catalog as get_us_strategy_catalog,
)
from us_equity_strategies.runtime_adapters import (
    describe_platform_runtime_requirements as describe_us_platform_runtime_requirements,
)
from hk_equity_strategies import (
    get_platform_runtime_adapter as get_hk_platform_runtime_adapter,
    get_runtime_enabled_profiles as get_hk_runtime_enabled_profiles,
    get_strategy_catalog as get_hk_strategy_catalog,
)
from hk_equity_strategies.runtime_adapters import (
    describe_platform_runtime_requirements as describe_hk_platform_runtime_requirements,
)

from quant_platform_kit.common.strategies import (
    PlatformCapabilityMatrix,
    PlatformStrategyPolicy,
    StrategyCatalog,
    StrategyDefinition,
    StrategyMetadata,
    US_EQUITY_DOMAIN,
    build_platform_profile_matrix,
    build_platform_profile_status_matrix,
    derive_enabled_profiles_for_platform,
    derive_eligible_profiles_for_platform,
    get_catalog_strategy_metadata,
    get_enabled_profiles_for_platform,
    resolve_platform_strategy_definition,
)

IBKR_PLATFORM = "ibkr"
HK_EQUITY_DOMAIN = "hk_equity"
TECH_COMMUNICATION_PULLBACK_PROFILE = "tech_communication_pullback_enhancement"
HK_DIVIDEND_GOLD_DEFENSIVE_ROTATION_PROFILE = "hk_dividend_gold_defensive_rotation"

PLATFORM_SUPPORTED_DOMAINS: dict[str, frozenset[str]] = {
    IBKR_PLATFORM: frozenset({US_EQUITY_DOMAIN, HK_EQUITY_DOMAIN}),
}


def _merge_strategy_catalogs(*catalogs: StrategyCatalog) -> StrategyCatalog:
    definitions: dict[str, StrategyDefinition] = {}
    metadata: dict[str, StrategyMetadata] = {}
    compatible_platforms: dict[str, frozenset[str]] = {}
    profile_aliases: dict[str, str] = {}
    for catalog in catalogs:
        for profile, definition in catalog.definitions.items():
            if profile in definitions and definitions[profile] != definition:
                raise ValueError(f"Duplicate strategy definition for profile {profile!r}")
            definitions[profile] = definition
        for profile, value in catalog.metadata.items():
            if profile in metadata and metadata[profile] != value:
                raise ValueError(f"Duplicate strategy metadata for profile {profile!r}")
            metadata[profile] = value
        for profile, platforms in catalog.compatible_platforms.items():
            if profile in compatible_platforms and compatible_platforms[profile] != platforms:
                raise ValueError(f"Duplicate strategy platform compatibility for profile {profile!r}")
            compatible_platforms[profile] = platforms
        for alias, profile in catalog.profile_aliases.items():
            if alias in profile_aliases and profile_aliases[alias] != profile:
                raise ValueError(f"Duplicate strategy alias {alias!r}")
            profile_aliases[alias] = profile
    return StrategyCatalog(
        definitions=definitions,
        metadata=metadata,
        compatible_platforms=compatible_platforms,
        profile_aliases=profile_aliases,
    )


def _canonical_profile(profile: str | None) -> str:
    normalized = str(profile or "").strip().lower()
    return STRATEGY_CATALOG.profile_aliases.get(normalized, normalized)


def get_platform_runtime_adapter(profile: str | None, *, platform_id: str):
    canonical_profile = _canonical_profile(profile)
    if canonical_profile in HK_STRATEGY_PROFILES:
        return get_hk_platform_runtime_adapter(canonical_profile, platform_id=platform_id)
    return get_us_platform_runtime_adapter(canonical_profile, platform_id=platform_id)


def describe_platform_runtime_requirements(profile: str | None, *, platform_id: str) -> dict[str, object]:
    canonical_profile = _canonical_profile(profile)
    if canonical_profile in HK_STRATEGY_PROFILES:
        return describe_hk_platform_runtime_requirements(canonical_profile, platform_id=platform_id)
    return describe_us_platform_runtime_requirements(canonical_profile, platform_id=platform_id)


US_STRATEGY_CATALOG = get_us_strategy_catalog()
HK_STRATEGY_CATALOG = get_hk_strategy_catalog()
STRATEGY_CATALOG = _merge_strategy_catalogs(US_STRATEGY_CATALOG, HK_STRATEGY_CATALOG)
US_STRATEGY_PROFILES = frozenset(US_STRATEGY_CATALOG.definitions)
HK_STRATEGY_PROFILES = frozenset(HK_STRATEGY_CATALOG.definitions)
IBKR_EXCLUDED_LIVE_PROFILES = frozenset(
    {
        HK_DIVIDEND_GOLD_DEFENSIVE_ROTATION_PROFILE,
        TECH_COMMUNICATION_PULLBACK_PROFILE,
    }
)
IBKR_ROLLOUT_ALLOWLIST = (
    get_us_runtime_enabled_profiles() | get_hk_runtime_enabled_profiles()
) - IBKR_EXCLUDED_LIVE_PROFILES
PLATFORM_CAPABILITY_MATRIX = PlatformCapabilityMatrix(
    platform_id=IBKR_PLATFORM,
    supported_domains=PLATFORM_SUPPORTED_DOMAINS[IBKR_PLATFORM],
    supported_target_modes=frozenset({"weight", "value"}),
    supported_inputs=frozenset(
        {"market_history", "benchmark_history", "feature_snapshot", "derived_indicators", "portfolio_snapshot"}
    ),
    supported_capabilities=frozenset({"broker_client"}),
)
_STRUCTURALLY_ELIGIBLE_STRATEGY_PROFILES = derive_eligible_profiles_for_platform(
    STRATEGY_CATALOG,
    capability_matrix=PLATFORM_CAPABILITY_MATRIX,
    runtime_adapter_loader=lambda profile: get_platform_runtime_adapter(
        profile,
        platform_id=IBKR_PLATFORM,
    ),
)
# Keep research-only and snapshot-scaffold HK profiles out of platform switch/status output.
ELIGIBLE_STRATEGY_PROFILES = _STRUCTURALLY_ELIGIBLE_STRATEGY_PROFILES & IBKR_ROLLOUT_ALLOWLIST
IBKR_ENABLED_PROFILES = derive_enabled_profiles_for_platform(
    STRATEGY_CATALOG,
    capability_matrix=PLATFORM_CAPABILITY_MATRIX,
    runtime_adapter_loader=lambda profile: get_platform_runtime_adapter(
        profile,
        platform_id=IBKR_PLATFORM,
    ),
    rollout_allowlist=IBKR_ROLLOUT_ALLOWLIST,
)
PLATFORM_POLICY = PlatformStrategyPolicy(
    platform_id=IBKR_PLATFORM,
    supported_domains=PLATFORM_SUPPORTED_DOMAINS[IBKR_PLATFORM],
    enabled_profiles=IBKR_ENABLED_PROFILES,
    default_profile="",
    rollback_profile="",
    require_explicit_profile=True,
)

SUPPORTED_STRATEGY_PROFILES = IBKR_ENABLED_PROFILES
_SELECTION_ROLE_FIELDS = frozenset({"is_default", "is_rollback"})


def _without_selection_role_fields(row: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in row.items() if key not in _SELECTION_ROLE_FIELDS}


def get_eligible_profiles_for_platform(platform_id: str) -> frozenset[str]:
    if platform_id != IBKR_PLATFORM:
        return frozenset()
    return ELIGIBLE_STRATEGY_PROFILES


def get_supported_profiles_for_platform(platform_id: str) -> frozenset[str]:
    return get_enabled_profiles_for_platform(platform_id, policy=PLATFORM_POLICY)


def get_platform_profile_matrix() -> list[dict[str, object]]:
    return [
        _without_selection_role_fields(row)
        for row in build_platform_profile_matrix(STRATEGY_CATALOG, policy=PLATFORM_POLICY)
    ]


def get_platform_profile_status_matrix() -> list[dict[str, object]]:
    return [
        _without_selection_role_fields(row)
        for row in build_platform_profile_status_matrix(
            STRATEGY_CATALOG,
            policy=PLATFORM_POLICY,
            eligible_profiles=ELIGIBLE_STRATEGY_PROFILES,
        )
    ]


def resolve_strategy_definition(
    raw_value: str | None,
    *,
    platform_id: str,
) -> StrategyDefinition:
    return resolve_platform_strategy_definition(
        raw_value,
        platform_id=platform_id,
        strategy_catalog=STRATEGY_CATALOG,
        policy=PLATFORM_POLICY,
    )


def resolve_strategy_metadata(
    raw_value: str | None,
    *,
    platform_id: str,
):
    definition = resolve_strategy_definition(raw_value, platform_id=platform_id)
    return get_catalog_strategy_metadata(STRATEGY_CATALOG, definition.profile)
