from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QPK_SRC = ROOT.parent / "QuantPlatformKit" / "src"
UES_SRC = ROOT.parent / "UsEquityStrategies" / "src"
HES_SRC = ROOT.parent / "HkEquityStrategies" / "src"


def _has_catalog_marker(candidate: Path, package_name: str, marker: str) -> bool:
    catalog_path = candidate / package_name / "catalog.py"
    if not catalog_path.exists():
        return False
    return marker in catalog_path.read_text(encoding="utf-8")


def _should_add_local_src(candidate: Path) -> bool:
    if candidate == QPK_SRC:
        return (candidate / "quant_platform_kit" / "common" / "runtime_target.py").exists()
    if candidate == UES_SRC:
        return _has_catalog_marker(
            candidate,
            "us_equity_strategies",
            "russell_top50_leader_rotation",
        )
    if candidate == HES_SRC:
        return _has_catalog_marker(
            candidate,
            "hk_equity_strategies",
            "hk_global_etf_tactical_rotation",
        )
    return True


for candidate in (ROOT, QPK_SRC, UES_SRC, HES_SRC):
    if not _should_add_local_src(candidate):
        continue
    candidate_str = str(candidate)
    if candidate_str not in sys.path:
        sys.path.insert(0, candidate_str)

from quant_platform_kit.common.runtime_target import build_runtime_target  # noqa: E402
from quant_platform_kit.common.strategies import derive_strategy_artifact_paths  # noqa: E402
from strategy_registry import (  # noqa: E402
    HK_EQUITY_DOMAIN,
    IBKR_PLATFORM,
    STRATEGY_CATALOG,
    describe_platform_runtime_requirements,
    get_platform_profile_status_matrix,
    resolve_strategy_definition,
    resolve_strategy_metadata,
)


IBKR_HK_MARKET_ENV: dict[str, str] = {
    "IBKR_MARKET": "HK",
    "IBKR_MARKET_CALENDAR": "XHKG",
    "IBKR_MARKET_CURRENCY": "HKD",
    "IBKR_MARKET_DATA_SYMBOL_SUFFIX": ".HK",
    "IBKR_MARKET_EXCHANGE": "SEHK",
    "IBKR_MARKET_TIMEZONE": "Asia/Hong_Kong",
}

HK_DRY_RUN_CHECKS = [
    "Confirm IBKR account group uses the intended HK/SEHK paper or verify-only service identity.",
    "Confirm HK/SEHK market-data and trading permissions before evaluating the strategy.",
    "Load market_history for all HK managed symbols with .HK/SEHK/HKD mapping.",
    "Preview orders only; keep IBKR_DRY_RUN_ONLY=true until operator approval.",
    "Verify integer-share sizing and broker lot-size behavior before any live order submission.",
    "Verify HKD cash, reserved-cash policy, fees, notifications, and runtime report output.",
]

HK_BLOCKED_DRY_RUN_ACTIONS = [
    "Do not submit live IBKR orders while dry_run_only=true.",
    "Do not remove HK market overrides when testing hk_equity profiles.",
]


def _feature_snapshot_filenames(profile: str, snapshot_contract_version: str | None) -> tuple[str, str]:
    suffix = "factor_snapshot" if ".factor_snapshot." in str(snapshot_contract_version or "") else "feature_snapshot"
    snapshot_filename = f"{profile}_{suffix}_latest.csv"
    return snapshot_filename, f"{snapshot_filename}.manifest.json"


def build_switch_plan(
    profile: str,
    *,
    dry_run_only: bool = False,
    deployment_selector: str | None = None,
    account_scope: str | None = None,
    account_group: str | None = None,
    service_name: str | None = "interactive-brokers-quant-service",
) -> dict[str, object]:
    definition = resolve_strategy_definition(profile, platform_id=IBKR_PLATFORM)
    metadata = resolve_strategy_metadata(definition.profile, platform_id=IBKR_PLATFORM)
    status_row = next(
        row for row in get_platform_profile_status_matrix() if row["canonical_profile"] == definition.profile
    )
    artifact_paths = derive_strategy_artifact_paths(
        STRATEGY_CATALOG,
        definition.profile,
        repo_root=ROOT,
    )
    runtime_requirements = describe_platform_runtime_requirements(
        definition.profile,
        platform_id=IBKR_PLATFORM,
    )
    requires_feature_snapshot = bool(runtime_requirements["requires_snapshot_artifacts"])
    requires_snapshot_manifest_path = bool(
        runtime_requirements["requires_snapshot_manifest_path"]
    )
    requires_strategy_config_path = bool(runtime_requirements["requires_strategy_config_path"])
    config_source_policy = str(runtime_requirements.get("config_source_policy") or "none")
    reconciliation_output_policy = str(
        runtime_requirements.get("reconciliation_output_policy") or "none"
    )
    runtime_target = build_runtime_target(
        platform_id=IBKR_PLATFORM,
        strategy_profile=definition.profile,
        dry_run_only=dry_run_only,
        deployment_selector=deployment_selector,
        account_scope=account_scope,
        service_name=service_name,
    )

    set_env: dict[str, str] = {
        "RUNTIME_TARGET_JSON": json.dumps(runtime_target.to_dict(), separators=(",", ":"))
    }
    if account_group:
        set_env["ACCOUNT_GROUP"] = account_group
    keep_env = [
        "ACCOUNT_GROUP",
        "IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME",
    ]
    optional_env = [
        "IBKR_EXECUTION_BACKEND",
        "IBKR_DRY_RUN_ONLY",
        "IBKR_MARKET",
        "IBKR_MARKET_CALENDAR",
        "IBKR_MARKET_CURRENCY",
        "IBKR_MARKET_DATA_SYMBOL_SUFFIX",
        "IBKR_MARKET_EXCHANGE",
        "IBKR_MARKET_TIMEZONE",
        "IBKR_MIN_RESERVED_CASH_USD",
        "IBKR_RESERVED_CASH_RATIO",
        "IBKR_SAFE_HAVEN_CASH_SUBSTITUTE_THRESHOLD_USD",
        "INCOME_LAYER_ENABLED",
        "INCOME_LAYER_START_USD",
        "INCOME_LAYER_MAX_RATIO",
        "DCA_MODE",
        "DCA_BASE_INVESTMENT_USD",
        "IBIT_ZSCORE_EXIT_ENABLED",
        "IBIT_ZSCORE_EXIT_MODE",
        "IBIT_ZSCORE_EXIT_PARKING_SYMBOL",
        "IBIT_ZSCORE_EXIT_RISK_REDUCED_EXPOSURE",
        "IBIT_ZSCORE_EXIT_RISK_OFF_EXPOSURE",
        "IBIT_ZSCORE_EXIT_ALLOW_OUTSIDE_EXECUTION_WINDOW",
        "IBKR_MARKET_SIGNAL_HANDOFF_INDEX_URI",
        "IBKR_MARKET_SIGNAL_HANDOFF_MANIFEST_URI",
        "IBKR_MARKET_SIGNAL_CONSUMPTION_AUDIT_URI",
        "IBKR_MARKET_SIGNAL_CACHE_DIR",
        "IBKR_MARKET_SIGNAL_REQUIRED",
        "IBKR_MARKET_SIGNAL_FALLBACK_MODE",
        "IBKR_MARKET_SIGNAL_MAX_STALE_DAYS",
        "CASH_ONLY_EXECUTION",
        "IBKR_FEATURE_SNAPSHOT_FALLBACK_MODE",
        "IBKR_FEATURE_SNAPSHOT_FALLBACK_CACHE_DIR",
        "IBKR_FEATURE_SNAPSHOT_MAX_STALE_DAYS",
    ]
    remove_if_present: list[str] = []
    notes = [
        "Keep ACCOUNT_GROUP and IB account-group config aligned with the current service identity.",
        "For HK-equity deployments set IBKR_MARKET=HK, or use an ACCOUNT_GROUP containing hk to derive SEHK/HKD/XHKG defaults.",
    ]
    dry_run_plan: dict[str, object] = {}
    if dry_run_only:
        set_env["IBKR_DRY_RUN_ONLY"] = "true"
        dry_run_plan = {
            "dry_run_only": True,
            "verify_only": True,
            "checks": [
                "Confirm runtime_target.execution_mode is paper before applying any env plan.",
                "Review broker order preview and notifications; do not submit live orders.",
            ],
            "blocked_actions": [
                "Do not submit live orders while dry_run_only=true.",
            ],
        }

    if definition.domain == HK_EQUITY_DOMAIN:
        set_env.update(IBKR_HK_MARKET_ENV)
        set_env["IBKR_DRY_RUN_ONLY"] = "true" if dry_run_only else "false"
        dry_run_plan = {
            "dry_run_only": dry_run_only,
            "verify_only": dry_run_only,
            "market": "HK",
            "checks": HK_DRY_RUN_CHECKS,
            "blocked_actions": HK_BLOCKED_DRY_RUN_ACTIONS if dry_run_only else [],
        }
        if dry_run_only:
            dry_run_plan["workflow_dispatch"] = {
                "workflow": "sync-cloud-run-env.yml",
                "target": "hk-verify",
                "cloud_run_service": service_name or "interactive-brokers-hk-verify-service",
                "deploy_image": True,
                "sync_env": True,
            }
        notes.append(
            "HK-equity switch plans describe environment values; apply them through Cloud Run env sync or deployment."
        )
        if not dry_run_only:
            notes.append("Use --dry-run-only for first HK runtime validation; live mode requires separate operator approval.")

    if requires_feature_snapshot:
        set_env["IBKR_FEATURE_SNAPSHOT_PATH"] = "<required>"
        if requires_snapshot_manifest_path:
            set_env["IBKR_FEATURE_SNAPSHOT_MANIFEST_PATH"] = "<required>"
        else:
            remove_if_present.append("IBKR_FEATURE_SNAPSHOT_MANIFEST_PATH")
        if requires_strategy_config_path and config_source_policy == "env_only":
            set_env["IBKR_STRATEGY_CONFIG_PATH"] = "<required>"
        elif requires_strategy_config_path and config_source_policy == "bundled_or_env":
            remove_if_present.append("IBKR_STRATEGY_CONFIG_PATH")
            notes.append(
                "IBKR_STRATEGY_CONFIG_PATH is optional for bundled_or_env profiles; leave it unset to use the packaged canonical config."
            )
        elif requires_strategy_config_path:
            set_env["IBKR_STRATEGY_CONFIG_PATH"] = "<required>"
        else:
            remove_if_present.append("IBKR_STRATEGY_CONFIG_PATH")
        if reconciliation_output_policy == "required":
            set_env["IBKR_RECONCILIATION_OUTPUT_PATH"] = "<required>"
        elif reconciliation_output_policy == "optional":
            optional_env.append("IBKR_RECONCILIATION_OUTPUT_PATH")
        else:
            remove_if_present.append("IBKR_RECONCILIATION_OUTPUT_PATH")
    else:
        remove_if_present.extend(
            [
                "IBKR_FEATURE_SNAPSHOT_PATH",
                "IBKR_FEATURE_SNAPSHOT_MANIFEST_PATH",
                "IBKR_STRATEGY_CONFIG_PATH",
                "IBKR_RECONCILIATION_OUTPUT_PATH",
            ]
        )

    hints: dict[str, str] = {}
    if requires_feature_snapshot:
        snapshot_filename, manifest_filename = _feature_snapshot_filenames(
            definition.profile,
            runtime_requirements.get("snapshot_contract_version"),
        )
        hints["feature_snapshot_filename"] = snapshot_filename
        hints["feature_snapshot_manifest_filename"] = manifest_filename
    if artifact_paths.bundled_config_path is not None:
        hints["bundled_strategy_config_path"] = str(artifact_paths.bundled_config_path)

    return {
        "platform": IBKR_PLATFORM,
        "canonical_profile": definition.profile,
        "display_name": metadata.display_name,
        "domain": definition.domain,
        "eligible": status_row["eligible"],
        "enabled": status_row["enabled"],
        **runtime_requirements,
        "required_inputs": sorted(definition.required_inputs),
        "target_mode": definition.target_mode,
        "runtime_target": runtime_target.to_dict(),
        "set_env": set_env,
        "keep_env": keep_env,
        "optional_env": sorted(optional_env),
        "remove_if_present": sorted(set(remove_if_present)),
        "hints": hints,
        "dry_run_plan": dry_run_plan,
        "notes": notes,
    }


def _print_plan(plan: dict[str, object]) -> None:
    print(f"platform: {plan['platform']}")
    print(f"profile: {plan['canonical_profile']} ({plan['display_name']})")
    print(f"eligible: {plan['eligible']}  enabled: {plan['enabled']}")
    print(f"profile_group: {plan['profile_group']}")
    print(f"required_inputs: {', '.join(plan['required_inputs'])}")
    print(f"input_mode: {plan['input_mode']}")
    print(f"requires_snapshot_artifacts: {plan['requires_snapshot_artifacts']}")
    print(f"requires_strategy_config_path: {plan['requires_strategy_config_path']}")
    print(f"target_mode: {plan['target_mode']}")
    print(f"runtime_target: {json.dumps(plan['runtime_target'], sort_keys=True)}")
    print("\nset_env:")
    for key, value in plan["set_env"].items():
        print(f"  {key}={value}")
    print("\nkeep_env:")
    for key in plan["keep_env"]:
        print(f"  {key}")
    print("\noptional_env:")
    for key in plan["optional_env"]:
        print(f"  {key}")
    print("\nremove_if_present:")
    for key in plan["remove_if_present"]:
        print(f"  {key}")
    if plan["hints"]:
        print("\nhints:")
        for key, value in plan["hints"].items():
            print(f"  {key}: {value}")
    if plan["dry_run_plan"]:
        print("\ndry_run_plan:")
        print(json.dumps(plan["dry_run_plan"], indent=2, sort_keys=True))
    if plan["notes"]:
        print("\nnotes:")
        for note in plan["notes"]:
            print(f"  - {note}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--dry-run-only", action="store_true")
    parser.add_argument("--deployment-selector")
    parser.add_argument("--account-scope")
    parser.add_argument("--account-group")
    parser.add_argument("--service-name", default="interactive-brokers-quant-service")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    plan = build_switch_plan(
        args.profile,
        dry_run_only=args.dry_run_only,
        deployment_selector=args.deployment_selector,
        account_scope=args.account_scope,
        account_group=args.account_group,
        service_name=args.service_name,
    )
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    _print_plan(plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
