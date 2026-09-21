"""Public IBKR runtime-target manifest contract (non-sensitive).

This module loads and strictly validates the checked-in runtime-target
manifest. It does not read GitHub Environment variables, Secret Manager
values, or Cloud Run state, and it never enables trading.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = 1
PLATFORM_ID = "ibkr"
ALLOWED_EXECUTION_MODES = frozenset({"live", "paper", "shadow"})
ALLOWED_LIFECYCLE_ROLES = frozenset({"live", "paper", "shadow"})

REQUIRED_TARGET_FIELDS = frozenset(
    {
        "id",
        "label",
        "service",
        "region",
        "account_group",
        "strategy_profile",
        "execution_mode",
        "lifecycle_role",
        "include_lifecycle",
        "include_reconciliation",
    }
)

OPTIONAL_TARGET_FIELDS = frozenset({"enabled", "description"})

# Public identifiers only: group / service / profile names, never secret values.
_TARGET_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SERVICE_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_REGION_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_ACCOUNT_GROUP_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_STRATEGY_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LABEL_RE = re.compile(r"^[\w ./\-+]{1,64}$", re.UNICODE)

_FORBIDDEN_VALUE_KEYS = frozenset(
    {
        "account_ids",
        "account_id",
        "token",
        "password",
        "secret",
        "api_key",
        "api_secret",
        "app_key",
        "app_secret",
        "private_key",
        "access_token",
        "refresh_token",
        "credentials",
        "telegram_token",
        "client_id",
        "ib_client_id",
        "ib_gateway_host",
        "ib_gateway_port",
        "ib_gateway_instance_name",
        "ib_gateway_zone",
        "ib_gateway_ip",
        "ib_gateway_ip_mode",
        "gateway_host",
        "gateway_port",
        "continuity_fingerprint",
        "continuity_hash",
        "fingerprint",
    }
)

_FORBIDDEN_KEY_SUFFIXES = (
    "_token",
    "_password",
    "_secret",
    "_api_key",
    "_private_key",
    "_fingerprint",
)

_SECRET_VALUE_HINT_RE = re.compile(
    r"(?i)(-----BEGIN |Bearer\s+[A-Za-z0-9._\-]{20,}|sk-[A-Za-z0-9]{20,})"
)

DEFAULT_MANIFEST_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "runtime_targets.manifest.json"
)


class RuntimeTargetManifestError(ValueError):
    """Raised when a runtime-target manifest fails contract validation."""


@dataclass(frozen=True)
class RuntimeTargetEntry:
    id: str
    label: str
    service: str
    region: str
    account_group: str
    strategy_profile: str
    execution_mode: str
    lifecycle_role: str
    include_lifecycle: bool
    include_reconciliation: bool
    enabled: bool = False


@dataclass(frozen=True)
class RuntimeTargetManifest:
    schema_version: int
    platform_id: str
    targets: tuple[RuntimeTargetEntry, ...]
    source_path: Path | None = None


def default_manifest_path() -> Path:
    return DEFAULT_MANIFEST_PATH


def load_runtime_target_manifest(path: Path | str | None = None) -> RuntimeTargetManifest:
    """Load and validate a runtime-target manifest from disk."""
    manifest_path = Path(path) if path is not None else default_manifest_path()
    try:
        raw_text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeTargetManifestError(
            f"Unable to read runtime-target manifest: {manifest_path}"
        ) from exc
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise RuntimeTargetManifestError(
            f"Runtime-target manifest is not valid JSON: {manifest_path}"
        ) from exc
    return validate_runtime_target_manifest(payload, source_path=manifest_path)


def validate_runtime_target_manifest(
    payload: Any,
    *,
    source_path: Path | None = None,
) -> RuntimeTargetManifest:
    """Strictly validate a runtime-target manifest payload."""
    if not isinstance(payload, Mapping):
        raise RuntimeTargetManifestError("Manifest root must be a JSON object")

    _reject_forbidden_value_keys(payload, path="$")
    _reject_secret_value_strings(payload, path="$")

    schema_version = payload.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        raise RuntimeTargetManifestError(
            f"Unsupported schema_version {schema_version!r}; expected {SCHEMA_VERSION}"
        )

    platform_id = payload.get("platform_id")
    if platform_id != PLATFORM_ID:
        raise RuntimeTargetManifestError(
            f"platform_id must be {PLATFORM_ID!r}, got {platform_id!r}"
        )

    unknown_root = sorted(
        set(payload) - {"schema_version", "platform_id", "targets", "description"}
    )
    if unknown_root:
        raise RuntimeTargetManifestError(
            f"Unknown top-level fields: {', '.join(unknown_root)}"
        )

    raw_targets = payload.get("targets")
    if not isinstance(raw_targets, list):
        raise RuntimeTargetManifestError("targets must be a JSON array")
    if not raw_targets:
        raise RuntimeTargetManifestError("targets must contain at least one entry")

    entries: list[RuntimeTargetEntry] = []
    seen_ids: set[str] = set()
    seen_services: set[str] = set()
    seen_account_groups: set[str] = set()

    for index, raw_target in enumerate(raw_targets):
        path = f"$.targets[{index}]"
        entry = _validate_target(raw_target, path=path)
        if entry.id in seen_ids:
            raise RuntimeTargetManifestError(f"Duplicate target id: {entry.id!r}")
        if entry.service in seen_services:
            raise RuntimeTargetManifestError(
                f"Duplicate Cloud Run service: {entry.service!r}"
            )
        if entry.account_group in seen_account_groups:
            raise RuntimeTargetManifestError(
                f"Duplicate account_group: {entry.account_group!r}"
            )
        seen_ids.add(entry.id)
        seen_services.add(entry.service)
        seen_account_groups.add(entry.account_group)
        entries.append(entry)

    return RuntimeTargetManifest(
        schema_version=SCHEMA_VERSION,
        platform_id=PLATFORM_ID,
        targets=tuple(entries),
        source_path=source_path,
    )


def iter_enabled_targets(
    manifest: RuntimeTargetManifest,
) -> Iterable[RuntimeTargetEntry]:
    """Yield targets marked enabled. Callers must not treat this as production authority."""
    for target in manifest.targets:
        if target.enabled:
            yield target


def _validate_target(raw_target: Any, *, path: str) -> RuntimeTargetEntry:
    if not isinstance(raw_target, Mapping):
        raise RuntimeTargetManifestError(f"{path} must be a JSON object")

    _reject_forbidden_value_keys(raw_target, path=path)
    _reject_secret_value_strings(raw_target, path=path)

    missing = sorted(REQUIRED_TARGET_FIELDS - set(raw_target))
    if missing:
        raise RuntimeTargetManifestError(
            f"{path} missing required fields: {', '.join(missing)}"
        )

    unknown = sorted(set(raw_target) - REQUIRED_TARGET_FIELDS - OPTIONAL_TARGET_FIELDS)
    if unknown:
        raise RuntimeTargetManifestError(
            f"{path} has unknown fields: {', '.join(unknown)}"
        )

    target_id = _require_match(
        raw_target.get("id"),
        field="id",
        path=path,
        pattern=_TARGET_ID_RE,
    )
    label = _require_match(
        raw_target.get("label"),
        field="label",
        path=path,
        pattern=_LABEL_RE,
    )
    service = _require_match(
        raw_target.get("service"),
        field="service",
        path=path,
        pattern=_SERVICE_RE,
    )
    region = _require_match(
        raw_target.get("region"),
        field="region",
        path=path,
        pattern=_REGION_RE,
    )
    account_group = _require_match(
        raw_target.get("account_group"),
        field="account_group",
        path=path,
        pattern=_ACCOUNT_GROUP_RE,
    )
    strategy_profile = _require_match(
        raw_target.get("strategy_profile"),
        field="strategy_profile",
        path=path,
        pattern=_STRATEGY_PROFILE_RE,
    )

    execution_mode = str(raw_target.get("execution_mode") or "").strip().lower()
    if execution_mode not in ALLOWED_EXECUTION_MODES:
        raise RuntimeTargetManifestError(
            f"{path}.execution_mode must be one of {sorted(ALLOWED_EXECUTION_MODES)}, "
            f"got {raw_target.get('execution_mode')!r}"
        )

    lifecycle_role = str(raw_target.get("lifecycle_role") or "").strip().lower()
    if lifecycle_role not in ALLOWED_LIFECYCLE_ROLES:
        raise RuntimeTargetManifestError(
            f"{path}.lifecycle_role must be one of {sorted(ALLOWED_LIFECYCLE_ROLES)}, "
            f"got {raw_target.get('lifecycle_role')!r}"
        )

    if lifecycle_role != execution_mode:
        raise RuntimeTargetManifestError(
            f"{path}.lifecycle_role {lifecycle_role!r} must match "
            f"execution_mode {execution_mode!r} for live/shadow semantics"
        )

    include_lifecycle = _require_bool(
        raw_target.get("include_lifecycle"),
        field="include_lifecycle",
        path=path,
    )
    include_reconciliation = _require_bool(
        raw_target.get("include_reconciliation"),
        field="include_reconciliation",
        path=path,
    )

    if include_reconciliation and execution_mode != "live":
        raise RuntimeTargetManifestError(
            f"{path}.include_reconciliation may only be true when execution_mode is 'live'"
        )

    if "enabled" not in raw_target:
        enabled = False
    else:
        enabled = _require_bool(raw_target.get("enabled"), field="enabled", path=path)

    return RuntimeTargetEntry(
        id=target_id,
        label=label,
        service=service,
        region=region,
        account_group=account_group,
        strategy_profile=strategy_profile,
        execution_mode=execution_mode,
        lifecycle_role=lifecycle_role,
        include_lifecycle=include_lifecycle,
        include_reconciliation=include_reconciliation,
        enabled=enabled,
    )


def _require_bool(value: Any, *, field: str, path: str) -> bool:
    if not isinstance(value, bool):
        raise RuntimeTargetManifestError(f"{path}.{field} must be a boolean")
    return value


def _require_match(
    value: Any,
    *,
    field: str,
    path: str,
    pattern: re.Pattern[str],
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeTargetManifestError(f"{path}.{field} must be a non-empty string")
    text = value.strip()
    if not pattern.fullmatch(text):
        raise RuntimeTargetManifestError(f"{path}.{field} has an invalid format: {text!r}")
    return text


def _reject_forbidden_value_keys(payload: Mapping[str, Any], *, path: str) -> None:
    for key in payload:
        key_text = str(key)
        lowered = key_text.lower()
        if lowered in _FORBIDDEN_VALUE_KEYS or lowered.endswith(_FORBIDDEN_KEY_SUFFIXES):
            raise RuntimeTargetManifestError(
                f"{path} must not contain sensitive field {key_text!r}; "
                "keep account_ids, gateway host/port, client id, tokens, "
                "continuity fingerprints, and credentials out of the public manifest"
            )


def _reject_secret_value_strings(payload: Any, *, path: str) -> None:
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            child = f"{path}.{key}"
            _reject_secret_value_strings(value, path=child)
        return
    if isinstance(payload, list):
        for index, value in enumerate(payload):
            _reject_secret_value_strings(value, path=f"{path}[{index}]")
        return
    if isinstance(payload, str) and _looks_like_secret_value(payload):
        raise RuntimeTargetManifestError(
            f"{path} appears to embed a secret value; remove it from the public manifest"
        )


def _looks_like_secret_value(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if _SECRET_VALUE_HINT_RE.search(stripped):
        return True
    # Long opaque tokens are values, not public inventory identifiers.
    # Human-readable description prose may contain spaces and is allowed.
    if " " not in stripped and len(stripped) >= 64 and re.fullmatch(
        r"[A-Za-z0-9+/=_\-]+", stripped
    ):
        return True
    return False
