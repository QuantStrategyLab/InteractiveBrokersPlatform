from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from strategy_registry import (
    DEFAULT_STRATEGY_PROFILE as PLATFORM_DEFAULT_STRATEGY_PROFILE,
    IBKR_PLATFORM,
    resolve_strategy_definition,
)

DEFAULT_ACCOUNT_GROUP = "default"
DEFAULT_STRATEGY_PROFILE = PLATFORM_DEFAULT_STRATEGY_PROFILE


@dataclass(frozen=True)
class AccountGroupConfig:
    ib_gateway_instance_name: str | None = None
    ib_gateway_zone: str | None = None
    ib_gateway_mode: str | None = None
    ib_gateway_ip_mode: str | None = None
    ib_client_id: int | None = None
    service_name: str | None = None
    account_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlatformRuntimeSettings:
    project_id: str | None
    ib_gateway_instance_name: str
    ib_gateway_zone: str
    ib_gateway_mode: str
    ib_gateway_ip_mode: str
    ib_client_id: int
    strategy_profile: str
    strategy_domain: str
    feature_snapshot_path: str | None
    feature_snapshot_manifest_path: str | None
    strategy_config_path: str | None
    strategy_config_source: str | None
    reconciliation_output_path: str | None
    dry_run_only: bool
    account_group: str
    service_name: str | None
    account_ids: tuple[str, ...]
    tg_token: str | None
    tg_chat_id: str | None
    notify_lang: str


def load_platform_runtime_settings(
    *,
    project_id_resolver: Callable[[], str | None],
    logger: Callable[[str], None] = print,
    secret_client_factory: Callable[[], Any] | None = None,
) -> PlatformRuntimeSettings:
    project_id = project_id_resolver()
    strategy_definition = resolve_strategy_definition(
        os.getenv("STRATEGY_PROFILE"),
        platform_id=IBKR_PLATFORM,
    )
    strategy_config_path, strategy_config_source = resolve_strategy_config_path(
        strategy_definition.profile,
        explicit_path=first_non_empty(
            os.getenv("IBKR_STRATEGY_CONFIG_PATH"),
            os.getenv("STRATEGY_CONFIG_PATH"),
        ),
    )
    account_group = resolve_account_group(os.getenv("ACCOUNT_GROUP"))
    group_config = load_account_group_config(
        project_id=project_id,
        account_group=account_group,
        raw_json=os.getenv("IB_ACCOUNT_GROUP_CONFIG_JSON"),
        secret_name=os.getenv("IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME"),
        secret_client_factory=secret_client_factory,
    )

    instance_name = require_group_string(
        group_config.ib_gateway_instance_name,
        field_name="ib_gateway_instance_name",
        account_group=account_group,
    )

    return PlatformRuntimeSettings(
        project_id=project_id,
        ib_gateway_instance_name=instance_name,
        ib_gateway_zone=first_non_empty(
            group_config.ib_gateway_zone,
            os.getenv("IB_GATEWAY_ZONE", "").strip(),
        )
        or "",
        ib_gateway_mode=resolve_ib_gateway_mode(
            require_group_string(
                group_config.ib_gateway_mode,
                field_name="ib_gateway_mode",
                account_group=account_group,
            )
        ),
        ib_gateway_ip_mode=resolve_ib_gateway_ip_mode(
            first_non_empty(group_config.ib_gateway_ip_mode, os.getenv("IB_GATEWAY_IP_MODE")),
            logger=logger,
        ),
        ib_client_id=require_group_int(
            group_config.ib_client_id,
            field_name="ib_client_id",
            account_group=account_group,
        ),
        strategy_profile=strategy_definition.profile,
        strategy_domain=strategy_definition.domain,
        feature_snapshot_path=first_non_empty(
            os.getenv("IBKR_FEATURE_SNAPSHOT_PATH"),
            os.getenv("FEATURE_SNAPSHOT_PATH"),
        ),
        feature_snapshot_manifest_path=first_non_empty(
            os.getenv("IBKR_FEATURE_SNAPSHOT_MANIFEST_PATH"),
            os.getenv("FEATURE_SNAPSHOT_MANIFEST_PATH"),
        ),
        strategy_config_path=strategy_config_path,
        strategy_config_source=strategy_config_source,
        reconciliation_output_path=first_non_empty(
            os.getenv("IBKR_RECONCILIATION_OUTPUT_PATH"),
            os.getenv("RECONCILIATION_OUTPUT_PATH"),
        ),
        dry_run_only=resolve_bool_env(os.getenv("IBKR_DRY_RUN_ONLY")),
        account_group=account_group,
        service_name=group_config.service_name,
        account_ids=group_config.account_ids,
        tg_token=os.getenv("TELEGRAM_TOKEN"),
        tg_chat_id=os.getenv("GLOBAL_TELEGRAM_CHAT_ID"),
        notify_lang=os.getenv("NOTIFY_LANG", "en"),
    )


def resolve_strategy_profile(raw_value: str | None) -> str:
    return resolve_strategy_definition(
        raw_value,
        platform_id=IBKR_PLATFORM,
    ).profile


def resolve_account_group(raw_value: str | None) -> str:
    value = (raw_value or "").strip()
    if not value:
        raise EnvironmentError("ACCOUNT_GROUP is required")
    return value


def resolve_strategy_config_path(
    strategy_profile: str,
    *,
    explicit_path: str | None,
) -> tuple[str | None, str | None]:
    path = first_non_empty(explicit_path)
    if path is not None:
        return path, "env"

    if str(strategy_profile).strip().lower() == "tech_pullback_cash_buffer":
        bundled = (
            Path(__file__).resolve().parent
            / "research"
            / "configs"
            / "growth_pullback_tech_pullback_cash_buffer.json"
        )
        if bundled.exists():
            return str(bundled), "bundled_canonical_default"
    return None, None


def load_account_group_config(
    *,
    project_id: str | None,
    account_group: str,
    raw_json: str | None,
    secret_name: str | None,
    secret_client_factory: Callable[[], Any] | None = None,
) -> AccountGroupConfig:
    payload = None
    if secret_name:
        if not project_id:
            raise EnvironmentError(
                "GOOGLE_CLOUD_PROJECT is required when IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME is set"
            )
        payload = load_secret_payload(
            project_id,
            secret_name,
            secret_client_factory=secret_client_factory,
        )
    elif raw_json:
        payload = raw_json

    if not payload:
        raise EnvironmentError(
            "IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME or IB_ACCOUNT_GROUP_CONFIG_JSON is required"
        )

    configs = parse_account_group_configs(payload)
    if account_group not in configs:
        available = ", ".join(sorted(configs))
        raise ValueError(
            f"ACCOUNT_GROUP={account_group!r} not found in account-group config; available groups: {available}"
        )
    return configs[account_group]


def parse_account_group_configs(payload: str) -> dict[str, AccountGroupConfig]:
    raw_data = json.loads(payload)
    groups = raw_data.get("groups", raw_data) if isinstance(raw_data, dict) else None
    if not isinstance(groups, dict):
        raise ValueError("IB account-group config must be a JSON object or {\"groups\": {...}}")

    parsed: dict[str, AccountGroupConfig] = {}
    for group_name, group_payload in groups.items():
        if not isinstance(group_payload, dict):
            raise ValueError(f"Account group {group_name!r} must be a JSON object")
        parsed[str(group_name)] = AccountGroupConfig(
            ib_gateway_instance_name=normalize_optional_string(group_payload.get("ib_gateway_instance_name")),
            ib_gateway_zone=normalize_optional_string(group_payload.get("ib_gateway_zone")),
            ib_gateway_mode=normalize_optional_string(group_payload.get("ib_gateway_mode")),
            ib_gateway_ip_mode=normalize_optional_string(group_payload.get("ib_gateway_ip_mode")),
            ib_client_id=parse_optional_int(group_payload.get("ib_client_id")),
            service_name=normalize_optional_string(group_payload.get("service_name")),
            account_ids=parse_account_ids(group_payload.get("account_ids")),
        )
    return parsed


def load_secret_payload(
    project_id: str,
    secret_name: str,
    *,
    secret_client_factory: Callable[[], Any] | None = None,
) -> str:
    if secret_client_factory is None:
        try:
            import google.cloud.secretmanager_v1 as secret_manager
        except ImportError:
            from google.cloud import secret_manager

        secret_client_factory = secret_manager.SecretManagerServiceClient

    client = secret_client_factory()
    resource_name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
    response = client.access_secret_version(request={"name": resource_name})
    return response.payload.data.decode("UTF-8")


def parse_account_ids(raw_value: Any) -> tuple[str, ...]:
    if raw_value is None:
        return ()
    if not isinstance(raw_value, (list, tuple)):
        raise ValueError("account_ids must be a JSON array of strings")
    parsed = []
    for item in raw_value:
        value = normalize_optional_string(item)
        if value is None:
            continue
        parsed.append(value)
    return tuple(parsed)


def parse_optional_int(raw_value: Any) -> int | None:
    if raw_value is None or raw_value == "":
        return None
    return int(raw_value)


def normalize_optional_string(raw_value: Any) -> str | None:
    if raw_value is None:
        return None
    value = str(raw_value).strip()
    return value or None


def first_non_empty(*values: str | None) -> str | None:
    for value in values:
        normalized = normalize_optional_string(value)
        if normalized is not None:
            return normalized
    return None


def resolve_bool_env(raw_value: str | None) -> bool:
    value = str(raw_value or "").strip().lower()
    return value in {"1", "true", "yes", "y", "on"}


def require_group_string(
    raw_value: str | None,
    *,
    field_name: str,
    account_group: str,
) -> str:
    value = normalize_optional_string(raw_value)
    if value is None:
        raise EnvironmentError(
            f"Account group {account_group!r} requires {field_name}"
        )
    return value


def require_group_int(
    raw_value: int | None,
    *,
    field_name: str,
    account_group: str,
) -> int:
    if raw_value is None:
        raise EnvironmentError(
            f"Account group {account_group!r} requires {field_name}"
        )
    return int(raw_value)


def resolve_ib_gateway_mode(raw_value: str | None) -> str:
    mode = (raw_value or "").strip().lower()
    if not mode:
        raise EnvironmentError("IB_GATEWAY_MODE is required and must be either 'live' or 'paper'")
    if mode in {"live", "paper"}:
        return mode
    raise EnvironmentError("IB_GATEWAY_MODE must be either 'live' or 'paper'")


def resolve_ib_gateway_ip_mode(
    raw_value: str | None,
    *,
    logger: Callable[[str], None] = print,
) -> str:
    mode = (raw_value or "internal").strip().lower()
    if mode in {"internal", "external"}:
        return mode
    logger(f"Invalid IB_GATEWAY_IP_MODE={mode!r}, defaulting to internal")
    return "internal"
