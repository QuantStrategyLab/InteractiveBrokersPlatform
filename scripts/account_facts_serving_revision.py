"""Resolve a scheduled publisher revision from the exact serving account target."""
from __future__ import annotations

import json
import re
import subprocess


class ServingRevisionError(ValueError):
    """Fixed reason codes only; private metadata never leaves this module."""


def read_metadata(args, env):
    account = env.get("GCP_WORKLOAD_IDENTITY_SERVICE_ACCOUNT")
    region = env.get("CLOUD_RUN_REGION")
    if not account or not region:
        raise ServingRevisionError("serving_revision_reader_config_missing")
    result = subprocess.run(
        ["gcloud", *args, "--account=" + account, "--project=" + env["IBKR_ACCOUNT_FACTS_PROJECT_ID"],
         "--region=" + region, "--format=json", "--quiet"],
        capture_output=True, text=True, check=False, timeout=60,
    )
    if result.returncode or len(result.stdout) > 256 * 1024:
        raise ServingRevisionError("serving_revision_metadata_unavailable")
    return json.loads(result.stdout)


def resolve(expected, env, read=read_metadata):
    service = expected["expected_service_name"]
    metadata = read(["run", "services", "describe", service], env)
    if metadata.get("metadata", {}).get("name") != service:
        raise ServingRevisionError("serving_revision_identity_mismatch")
    traffic = [r for r in metadata.get("status", {}).get("traffic", []) if r.get("percent", 0) > 0]
    if len(traffic) != 1 or type(traffic[0].get("percent")) is not int or traffic[0]["percent"] != 100:
        raise ServingRevisionError("serving_revision_ambiguous")
    revision = traffic[0].get("revisionName", "")
    if not re.fullmatch(re.escape(service) + r"-[a-z0-9-]+", revision):
        raise ServingRevisionError("serving_revision_ambiguous")
    payload = read(["run", "revisions", "describe", revision], env)
    if payload.get("metadata", {}).get("name") != revision:
        raise ServingRevisionError("serving_revision_identity_mismatch")
    containers = payload.get("spec", {}).get("containers", [])
    if len(containers) != 1:
        raise ServingRevisionError("serving_revision_metadata_invalid")
    rows = [r for r in containers[0].get("env", []) if r.get("name") == "RUNTIME_TARGET_JSON"]
    if len(rows) != 1 or not isinstance(rows[0].get("value"), str):
        raise ServingRevisionError("serving_revision_metadata_invalid")
    target = json.loads(rows[0]["value"])
    identity = {"platform_id": "ibkr", "service_name": service,
                "account_scope": expected["expected_account_scope"],
                "account_selector": list(expected["expected_account_selector"]),
                "deployment_selector": expected["expected_deployment_selector"]}
    if not isinstance(target, dict) or any(target.get(k) != v for k, v in identity.items()):
        raise ServingRevisionError("serving_revision_identity_mismatch")
    return revision
