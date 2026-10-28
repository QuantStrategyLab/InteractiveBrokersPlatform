"""Compare the primary website binding without reading reports."""
import os
from pathlib import PurePosixPath
from urllib.parse import urlsplit
from scripts import diagnose_account_facts_identity as identity
from scripts import publish_account_facts_from_report as publisher
from scripts.daily_binding_protocol import digest


def main():
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
        print("status=ok comparison=" + main())
    except Exception:
        print("status=blocked reason=comparison_unavailable")
        raise SystemExit(1) from None
