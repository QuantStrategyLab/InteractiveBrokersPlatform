"""Pure validation of the existing protected account-facts slot configuration.

No environment, publisher, endpoint, token, broker or storage is accessed here.
Configuration shape never attests the account's financial facts or authority.
"""
from collections.abc import Sequence
import json
import re
from typing import Any
from urllib.parse import urlsplit


ADDITIONAL_TARGETS = frozenset({"additional-1", "additional-2", "additional-3"})
ADDITIONAL_TARGET_CONFIG_KEYS = frozenset({
    "project_id", "report_prefix", "target_id", "service_name", "runtime_revision",
    "account_scope", "account_selector", "deployment_selector",
})
_TARGET_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


class AccountFactsTargetConfigError(ValueError):
    """The existing slot configuration is invalid; private values are omitted."""


def account_selector(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    if any(not isinstance(item, str) or not item or item != item.strip() for item in value):
        return ()
    return tuple(value)


def single_line_text(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\n" in value or "\r" in value:
        return ""
    return value


def parse_additional_target_configs(raw: str) -> dict[str, dict[str, Any]]:
    def invalid():
        return AccountFactsTargetConfigError("target_config_invalid")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise invalid()
            result[key] = value
        return result

    try:
        parsed = json.loads(raw, object_pairs_hook=unique_object)
    except (json.JSONDecodeError, TypeError, AccountFactsTargetConfigError):
        raise invalid() from None
    if not isinstance(parsed, dict) or set(parsed) != ADDITIONAL_TARGETS:
        raise invalid()

    configs: dict[str, dict[str, Any]] = {}
    seen_identities: set[tuple[str, ...]] = set()
    seen_target_ids: set[str] = set()
    seen_prefixes: set[tuple[str, str, str]] = set()
    for slot in sorted(ADDITIONAL_TARGETS):
        record = parsed[slot]
        if not isinstance(record, dict) or set(record) != ADDITIONAL_TARGET_CONFIG_KEYS:
            raise invalid()
        normalized = {key: single_line_text(record[key]) for key in (
            "project_id", "report_prefix", "target_id", "service_name", "runtime_revision",
            "account_scope", "deployment_selector",
        )}
        selector = account_selector(record["account_selector"])
        try:
            parsed_prefix = urlsplit(normalized["report_prefix"])
        except ValueError:
            raise invalid() from None
        if (
            any(not value for value in normalized.values())
            or not _TARGET_ID.fullmatch(normalized["target_id"])
            or len(selector) != 1
            or selector[0].lower() == "default"
            or "\n" in selector[0] or "\r" in selector[0]
            or parsed_prefix.scheme != "gs" or not parsed_prefix.netloc
            or not parsed_prefix.path.strip("/") or parsed_prefix.query or parsed_prefix.fragment
        ):
            raise invalid()
        prefix_identity = (parsed_prefix.scheme, parsed_prefix.netloc, parsed_prefix.path.rstrip("/"))
        if (selector in seen_identities or normalized["target_id"] in seen_target_ids
                or prefix_identity in seen_prefixes):
            raise invalid()
        seen_identities.add(selector)
        seen_target_ids.add(normalized["target_id"])
        seen_prefixes.add(prefix_identity)
        normalized["account_selector"] = selector
        configs[slot] = normalized
    return configs
