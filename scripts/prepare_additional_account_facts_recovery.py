"""Seal a same-account serving-revision update; never write config or call a broker."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from datetime import datetime, timezone

from scripts import diagnose_account_facts_identity as diagnostic
from scripts import publish_account_facts_from_report as publisher
from scripts.backup_primary_account_facts_state import _seal_secret


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def sha(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def prepare(report, metadata, *, expected, raw_targets, slot, expected_revision_hash, uri, now):
    """Keep all identity fields and other slots intact; verify the real serving report."""
    if slot not in publisher.IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS:
        raise ValueError("slot_invalid")
    configs = publisher._additional_target_configs(raw_targets)
    # Reuse the duplicate-identity and immutable report projection contracts.
    publisher._reject_primary_target_duplicates(configs)
    rows = metadata.get("status", {}).get("traffic", [])
    serving = {row.get("revisionName") for row in rows if row.get("percent", 0) > 0}
    if (len(serving) != 1 or sum(row.get("percent", 0) for row in rows) != 100
            or metadata.get("metadata", {}).get("name") != expected["expected_service_name"]):
        raise ValueError("serving_revision_unresolved")
    revision = next(iter(serving))
    if not isinstance(revision, str) or sha(revision) != expected_revision_hash:
        raise ValueError("serving_revision_changed")
    selected = configs[slot]
    identity = {key: expected[f"expected_{key}"] for key in (
        "project_id", "service_name", "account_scope", "account_selector", "deployment_selector",
    )}
    if (any(selected[key] != identity[key] for key in identity)
            or selected["target_id"] != expected["target_id"]
            or selected["report_prefix"] != expected["expected_report_prefix"]
            or selected["runtime_revision"] != expected["expected_runtime_revision"]):
        raise ValueError("configured_identity_changed")
    if revision == selected["runtime_revision"]:
        raise ValueError("revision_update_not_required")
    candidate = {**expected, "expected_runtime_revision": revision}
    result = diagnostic.diagnose_identity(report, expected=candidate, source_report_uri=uri,
                                          now=now, cloud_run_metadata=metadata)
    if (result["projection_passed"] is not True or result["freshness_status"] != "fresh"
            or result["serving_traffic"]["report_is_only_serving_revision"] is not True
            or result["revision_equalities"]["receipt_equals_report"] is not True):
        raise ValueError("fresh_serving_report_not_verified")
    original = json.loads(raw_targets)
    updated = copy.deepcopy(original)
    updated[slot]["runtime_revision"] = revision
    publisher._additional_target_configs(json.dumps(updated))
    rotation = {
        "target_id": expected["target_id"],
        "previous_source_binding_id": publisher._bound_source_id(
            **identity, runtime_revision=selected["runtime_revision"]),
        "next_source_binding_id": publisher._bound_source_id(**identity, runtime_revision=revision),
    }
    return original, updated, rotation


def seal_values(original, updated, rotation, keys):
    # Keys come only from the existing repository's protected configuration.
    # Dispatch inputs cannot choose a recipient or a different cloud identity.
    if not isinstance(keys, dict) or set(keys) != {"publisher", "receiver"}:
        raise ValueError("recovery_keys_invalid")
    for key in keys.values():
        if (not isinstance(key, dict) or set(key) != {"key", "key_id"}
                or not isinstance(key["key_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key["key_id"])
                or len(base64.b64decode(key["key"], validate=True)) != 32):
            raise ValueError("recovery_keys_invalid")
    result = []
    for destination, purpose, name, value in (
        ("publisher", "restore", "IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON", original),
        ("publisher", "adopt", "IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON", updated),
        ("receiver", "rotate", "IBKR_ACCOUNT_FACTS_SOURCE_ROTATION_JSON", rotation),
    ):
        key = keys[destination]
        encrypted = _seal_secret(key["key"], canonical(value))
        result.append({"destination": destination, "purpose": purpose, "name": name, "key_id": key["key_id"],
                       "encrypted_value": base64.b64encode(encrypted).decode(),
                       "public_key_sha256": sha(base64.b64decode(key["key"]))})
    return result


def main():
    stage = "context"
    try:
        if os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch" or os.environ.get("GITHUB_REF") != "refs/heads/main":
            raise ValueError("manual_main_required")
        slot = os.environ.get("IBKR_ACCOUNT_FACTS_TARGET", "")
        expected = diagnostic.resolve_expected()
        now = datetime.now(timezone.utc)
        stage = "report_read"
        uri = publisher._latest_report_uri(prefix=expected["expected_report_prefix"],
                                           project_id=expected["expected_project_id"], now=now)
        report = publisher._load_gcs_report(uri, project_id=expected["expected_project_id"])
        metadata = diagnostic._read_cloud_run_metadata(expected)
        stage = "serving_report_validation"
        original, updated, rotation = prepare(report, metadata, expected=expected,
            raw_targets=os.environ[publisher.IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON_ENV], slot=slot,
            expected_revision_hash=os.environ["EXPECTED_SERVING_REVISION_SHA256"], uri=uri, now=now)
        stage = "sealing"
        keys = json.loads(os.environ["ACCOUNT_FACTS_RECOVERY_PUBLIC_KEYS_JSON"])
        values = seal_values(original, updated, rotation, keys)
        stage = "serving_readback"
        after = diagnostic._read_cloud_run_metadata(expected)
        if metadata["status"]["traffic"] != after["status"]["traffic"]:
            raise ValueError("serving_changed_during_read")
        payload = {"schema_version": "ibkr.additional_source_recovery.v1", "slot": slot,
            "target_id_sha256": sha(expected["target_id"]),
            "previous_source_binding_id_sha256": sha(rotation["previous_source_binding_id"]),
            "next_source_binding_id_sha256": sha(rotation["next_source_binding_id"]),
            "expected_runtime_revision_sha256": sha(updated[slot]["runtime_revision"]),
            "report_name": uri.rsplit("/", 1)[-1], "secrets": values}
        stage = "sealed_artifact"
        output = Path(os.environ["RUNNER_TEMP"]) / "additional-recovery-sealed.json"
        with output.open("xb") as handle:
            os.chmod(output, 0o600)
            handle.write(canonical(payload))
        print(json.dumps({"status": "additional_recovery_sealed", "slot": slot,
            "target_id_sha256": payload["target_id_sha256"], "expected_runtime_revision_sha256": payload["expected_runtime_revision_sha256"],
            "sealed_artifact_sha256": sha(canonical(payload)), "secret_count": len(values),
            "remote_configuration_writes": False, "plaintext_exported": False}))
        return 0
    except Exception:
        # Neither provider exceptions nor private config/report bodies leave the runner.
        print(f"status=blocked reason=additional_source_recovery_not_verified stage={stage}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
