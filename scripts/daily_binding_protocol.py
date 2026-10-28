"""Canonical, nonce-bound comparison of the IBKR daily binding."""
import hashlib
import json
import re

FIELDS = ("account_key", "account_scope", "account_selector", "deployment_selector",
          "platform", "service_name", "source_binding_id", "strategy_profile", "target_id")
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._|:-]{0,119}\Z", re.ASCII)
HEX = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


def digest(binding, nonce):
    if not isinstance(binding, dict) or set(binding) != set(FIELDS) or not HEX.fullmatch(nonce or ""):
        raise ValueError("invalid_input")
    if any(not isinstance(binding[k], str) or not TOKEN.fullmatch(binding[k]) for k in FIELDS):
        raise ValueError("invalid_input")
    if (binding["platform"] != "ibkr" or binding["strategy_profile"] != "tqqq_growth_income"
            or not HEX.fullmatch(binding["source_binding_id"])):
        raise ValueError("invalid_input")
    body = {"binding": binding, "domain": "qsl.ibkr-daily-binding-comparison.v1", "nonce": nonce}
    canonical = json.dumps(body, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()
