"""Compare the primary website binding without reading reports."""
import json
import os
from pathlib import PurePosixPath
from urllib.parse import urlsplit
from scripts import diagnose_account_facts_identity as identity
from scripts import publish_account_facts_from_report as publisher
from scripts.daily_binding_protocol import additional_digest, digest


def compare_additional(env):
    slot = env.get("IBKR_ACCOUNT_FACTS_TARGET", "")
    if slot not in publisher.IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS:
        raise ValueError("slot_invalid")
    expected = identity.resolve_expected()
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result
    keys = json.loads(env.get("IBKR_PERIOD_RETURN_ADDITIONAL_ACCOUNT_KEYS_JSON") or "{}", object_pairs_hook=unique)
    one = env.get("IBKR_PERIOD_RETURN_ADDITIONAL1_ACCOUNT_KEY", "")
    if not isinstance(keys, dict) or set(keys) - set(publisher.IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS):
        raise ValueError("keys_invalid")
    if one:
        if "additional-1" in keys and keys["additional-1"] != one:
            raise ValueError("keys_conflict")
        keys["additional-1"] = one
    key = keys.get(slot)
    if not isinstance(key, str) or not key:
        raise ValueError("key_missing")
    inventory = json.loads(env.get("CLOUD_RUN_SERVICE_TARGETS_JSON", ""), object_pairs_hook=unique)
    entries = inventory.get("targets") if isinstance(inventory, dict) else inventory
    if not isinstance(entries, list):
        raise ValueError("inventory_invalid")
    matches = []
    for row in entries:
        if not isinstance(row, dict):
            continue
        target = row.get("runtime_target")
        if isinstance(target, str):
            target = json.loads(target, object_pairs_hook=unique)
        if not isinstance(target, dict):
            continue
        if (target.get("service_name") == expected["expected_service_name"]
                and target.get("account_scope") == expected["expected_account_scope"]
                and target.get("deployment_selector") == expected["expected_deployment_selector"]
                and publisher._selector(target.get("account_selector")) == expected["expected_account_selector"]
                and (row.get("service") or row.get("service_name")) == expected["expected_service_name"]):
            matches.append(target)
    if len(matches) != 1 or not isinstance(matches[0].get("strategy_profile"), str):
        raise ValueError("target_not_unique")
    source = publisher._bound_source_id(**{k: expected["expected_" + k] for k in (
        "project_id", "service_name", "runtime_revision", "account_scope", "account_selector", "deployment_selector")})
    binding = dict(account_key=key, account_scope=expected["expected_account_scope"],
        account_selector=expected["expected_account_selector"][0], deployment_selector=expected["expected_deployment_selector"],
        platform="ibkr", service_name=expected["expected_service_name"], source_binding_id=source,
        strategy_profile=matches[0]["strategy_profile"], target_id=expected["target_id"])
    return additional_digest(binding, env.get("DAILY_COMPARISON_NONCE", ""))


def main():
    if os.environ.get("IBKR_ACCOUNT_FACTS_TARGET", "primary-live") != "primary-live":
        return compare_additional(os.environ)
    expected = identity.resolve_expected()
    if len(expected["expected_account_selector"]) != 1:
        raise ValueError("selector_mismatch")
    if "tqqq_growth_income" not in PurePosixPath(urlsplit(expected["expected_report_prefix"]).path).parts:
        raise ValueError("scope_mismatch")
    source = publisher._bound_source_id(**{k: expected["expected_" + k] for k in (
        "project_id", "service_name", "runtime_revision", "account_scope", "account_selector", "deployment_selector")})
    period = {name: os.environ.get("IBKR_PERIOD_RETURN_" + name, "") for name in (
        "ACCOUNT_KEY", "ACCOUNT_SCOPE", "SOURCE_BINDING_ID", "TARGET_ID")}
    if (period["ACCOUNT_SCOPE"] != expected["expected_account_scope"]
            or period["SOURCE_BINDING_ID"] != source or period["TARGET_ID"] != expected["target_id"]):
        raise ValueError("configuration_mismatch")
    binding = dict(account_key=period["ACCOUNT_KEY"], account_scope=expected["expected_account_scope"],
                   account_selector=expected["expected_account_selector"][0],
                   deployment_selector=expected["expected_deployment_selector"], platform="ibkr",
                   service_name=expected["expected_service_name"], source_binding_id=source,
                   strategy_profile="tqqq_growth_income", target_id=expected["target_id"])
    return digest(binding, os.environ.get("DAILY_COMPARISON_NONCE", ""))


if __name__ == "__main__":
    try:
        value = main()
        slot = os.environ.get("IBKR_ACCOUNT_FACTS_TARGET", "primary-live")
        if slot == "primary-live":
            print("status=ok comparison=" + value)
        else:
            print("status=ok slot=" + slot + " comparison=" + value)
    except Exception:
        print("status=blocked reason=comparison_unavailable")
        raise SystemExit(1) from None
