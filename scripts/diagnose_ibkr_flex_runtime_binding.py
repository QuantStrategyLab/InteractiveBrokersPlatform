"""Compare protected Flex slots with serving runtime identity; no report or broker I/O."""
from __future__ import annotations

from collections.abc import Mapping
import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import zipfile

from application.account_facts_target_config import (
    account_selector, parse_additional_target_configs, single_line_text,
)

PROFILES = ("global_etf_rotation", "russell_top50_leader_rotation")
IDENTITY_FIELDS = ("service_name", "account_scope", "account_selector", "deployment_selector")
SLOT_FIELDS = ("project_id", "service_name", "runtime_revision",
               "account_scope", "deployment_selector")


class BindingError(ValueError):
    pass


def require(value, reason):
    if not value:
        raise BindingError(reason)


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "configuration_invalid")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def configuration(env):
    primary = {field: single_line_text(env.get("IBKR_ACCOUNT_FACTS_" + field.upper())) for field in SLOT_FIELDS}
    primary["account_selector"] = account_selector(_json(env.get("IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON", "")))
    require(all(primary[field] for field in SLOT_FIELDS) and len(primary["account_selector"]) == 1,
            "configuration_invalid")
    additional = parse_additional_target_configs(env.get("IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON", ""))
    slots = {"primary": primary, **additional}
    selectors = [tuple(row["account_selector"]) for row in slots.values()]
    require(len(set(selectors)) == len(slots) and all(re.fullmatch(r"(?:U|DU)[0-9]+", row[0]) for row in selectors),
            "configuration_invalid")
    require(account_selector(_json(env.get("IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON", ""))) == selectors[0], "configuration_invalid")
    inventory = _json(env.get("CLOUD_RUN_SERVICE_TARGETS_JSON", ""))
    entries = inventory.get("targets") if isinstance(inventory, dict) else inventory
    require(isinstance(entries, list), "configuration_invalid")
    requested = env.get("REQUESTED_TARGET")
    require(requested in (*PROFILES, "all"), "target_not_supported")
    project = single_line_text(env.get("GCP_PROJECT_ID"))
    region = single_line_text(env.get("GCP_REGION"))
    require(project and region, "configuration_invalid")
    result = {}
    for profile in PROFILES if requested == "all" else (requested,):
        matches = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("include_reconciliation") is not True:
                continue
            target = entry.get("runtime_target")
            target = _json(target) if isinstance(target, str) else target
            if isinstance(target, dict) and target.get("strategy_profile") == profile:
                service = single_line_text(entry.get("service") or entry.get("service_name"))
                require(re.fullmatch(r"[a-z][a-z0-9-]{0,62}", service) and target.get("service_name") == service and target.get("execution_mode") == "live",
                        "configuration_invalid")
                require(len(account_selector(target.get("account_selector"))) == 1, "configuration_invalid")
                matches.append((service, target))
        require(len(matches) == 1, "target_not_unique")
        result[profile] = (matches[0][0], matches[0][1])
    return slots, result, project, region


def _environment(spec):
    containers = spec.get("containers")
    require(isinstance(containers, list) and len(containers) == 1, "runtime_identity_unavailable")
    rows = containers[0].get("env", [])
    require(isinstance(rows, list), "runtime_identity_unavailable")
    names = [row.get("name") for row in rows if isinstance(row, dict)]
    require(len(names) == len(rows) and len(names) == len(set(names)), "runtime_identity_unavailable")
    return {row["name"]: row.get("value") for row in rows}


def _serving(service):
    rows = service.get("status", {}).get("traffic")
    require(isinstance(rows, list) and rows and all(isinstance(row, dict) and type(row.get("percent")) is int
            and 0 <= row["percent"] <= 100 for row in rows), "serving_unresolved")
    positive = [row for row in rows if row["percent"] > 0]
    require(sum(row["percent"] for row in rows) == 100 and len(positive) == 1
            and single_line_text(positive[0].get("revisionName")), "serving_unresolved")
    return positive[0]["revisionName"]


def _read(kind, name, project, region):
    require(kind in {"services", "revisions"} and re.fullmatch(r"[a-z][a-z0-9-]{0,62}", name), "metadata_read_failed")
    result = subprocess.run(["gcloud", "run", kind, "describe", name, "--project", project,
                             "--region", region, "--format=json"], capture_output=True, timeout=30, check=False)
    require(result.returncode == 0, "metadata_read_failed")
    value = _json(result.stdout)
    require(isinstance(value, dict), "metadata_read_failed")
    return value


def _actual_group_match(env, expected):
    # load_account_group_config gives Secret precedence over inline JSON.
    # A current latest-version read cannot prove the worker's startup version.
    secret = env.get("IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME")
    if secret:
        return None, "worker_secret_version_unverified"
    if any(key in env and env[key] is None for key in (
        "IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME", "IB_ACCOUNT_GROUP_CONFIG_JSON", "ACCOUNT_GROUP",
    )):
        return None, "group_config_reference_unverified"
    raw, group = env.get("IB_ACCOUNT_GROUP_CONFIG_JSON"), env.get("ACCOUNT_GROUP")
    if not isinstance(raw, str) or not raw or not isinstance(group, str) or not group.strip():
        return None, "group_config_unavailable"
    try:
        payload = _json(raw)
        groups = payload.get("groups", payload) if isinstance(payload, dict) else None
        selected = groups.get(group.strip()) if isinstance(groups, dict) else None
        if not isinstance(selected, dict):
            return None, "group_config_unavailable"
        values = selected.get("account_ids")
        if values is None:
            values = []
        if not isinstance(values, list):
            return None, "group_config_invalid"
        # Match runtime_config_support.parse_account_ids normalization exactly.
        accounts = tuple(str(value).strip() for value in values if value is not None and str(value).strip())
        match = accounts == tuple(expected)
        return match, "binding_verified" if match else "group_account_mismatch"
    except Exception:
        return None, "group_config_invalid"


REPOSITORY = "QuantStrategyLab/InteractiveBrokersPlatform"
WORKFLOW = ".github/workflows/collect-reconciliation-evidence.yml"


def _gh(path):
    try:
        result = subprocess.run(["gh", "api", f"repos/{REPOSITORY}/{path}"],
                                capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BindingError("receipt_read_failed") from exc
    require(result.returncode == 0 and len(result.stdout) <= 2_000_000, "receipt_read_failed")
    return result.stdout


def _artifact_json(raw):
    require(len(raw) <= 2_000_000, "receipt_invalid")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            require(len(entries) == 1 and not entries[0].is_dir() and entries[0].file_size <= 1_000_000,
                    "receipt_invalid")
            return _json(archive.read(entries[0]))
    except (zipfile.BadZipFile, UnicodeError, ValueError) as exc:
        raise BindingError("receipt_invalid") from exc


def _receipt_account_match(run_id, profile, service, revision, revision_labels, expected, reader=_gh, *, private_receipt=None):
    """Compare only a verified past receipt; never infer the worker's current Secret."""
    require(isinstance(run_id, str) and re.fullmatch(r"[0-9]{1,20}", run_id), "receipt_run_invalid")
    run = _json(reader(f"actions/runs/{run_id}"))
    require(run.get("id") == int(run_id) and run.get("status") == "completed"
            and run.get("conclusion") == "success" and run.get("head_branch") == "main"
            and run.get("event") == "workflow_dispatch" and run.get("path") == WORKFLOW
            and run.get("repository", {}).get("full_name") == REPOSITORY, "receipt_run_unverified")
    listing = _json(reader(f"actions/runs/{run_id}/artifacts?per_page=100"))
    items = listing.get("artifacts")
    require(isinstance(items, list) and listing.get("total_count") == len(items), "receipt_artifacts_unavailable")
    def select(name):
        matches = [item for item in items if item.get("name") == name and item.get("expired") is False]
        require(len(matches) == 1 and type(matches[0].get("id")) is int, "receipt_artifacts_unavailable")
        return matches[0]
    name = f"ibkr-reconciliation-{profile}-{run_id}"
    artifact = select(name)
    source = select(f"ibkr-reconciliation-source-record-{profile}-{run_id}")
    raw = reader(f"actions/artifacts/{artifact['id']}/zip")
    receipt = _artifact_json(raw)
    record = _artifact_json(reader(f"actions/artifacts/{source['id']}/zip"))
    evidence = receipt.get("reconciliation", {}).get("evidence", {})
    require(record.get("schema_version") == "ibkr_reconciliation_source_receipt_record.v1"
            and record.get("repository") == REPOSITORY and record.get("workflow_path") == WORKFLOW
            and record.get("workflow_run_id") == run_id and record.get("workflow_run_attempt") == str(run.get("run_attempt"))
            and record.get("workflow_head_sha") == run.get("head_sha")
            and record.get("artifact_id") == str(artifact["id"]) and record.get("artifact_name") == name
            and record.get("artifact_sha256") == hashlib.sha256(raw).hexdigest()
            and record.get("service_name") == service and record.get("service_revision") == revision
            and record.get("service_revision_commit_sha") == revision_labels.get("commit-sha")
            and re.fullmatch(r"[0-9a-f]{40}", str(record.get("service_revision_commit_sha", "")))
            and record.get("service_deploy_run_id") == revision_labels.get("github-run-id")
            and re.fullmatch(r"[0-9]+", str(record.get("service_deploy_run_id", "")))
            and record.get("evidence_sha256") == evidence.get("evidence_sha256")
            and record.get("scheduler_job_sha256") == receipt.get("reconciliation_scheduler_job_sha256")
            and re.fullmatch(r"[0-9a-f]{64}", str(record.get("scheduler_job_sha256", "")))
            and receipt.get("strategy_profile") == profile and receipt.get("report_status") == "ok"
            and evidence.get("platform_id") == "ibkr" and evidence.get("strategy_profile") == profile
            and evidence.get("broker_connected") is True and evidence.get("account_identity_match") is True,
            "receipt_provenance_unverified")
    actual = evidence.get("account_scope_sha256")
    require(isinstance(actual, str) and re.fullmatch(r"[0-9a-f]{64}", actual), "receipt_invalid")
    encoded = json.dumps({"account_ids": sorted(set(expected))}, ensure_ascii=True,
                         sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    matched = actual == hashlib.sha256(encoded).hexdigest()
    if matched and private_receipt is not None:
        private_receipt.append((record, raw))
    return matched


def observe(profile, service_name, target, slots, project, region, reader=_read, run_id=None, receipt_reader=_gh, *, private_output=None):
    candidate = [(slot, row) for slot, row in slots.items()
                 if row["project_id"] == project and all(
                     tuple(row[field]) == account_selector(target.get(field)) if field == "account_selector"
                     else row[field] == target.get(field) for field in IDENTITY_FIELDS)]
    result = {"status": "unknown", "unique_slot_match": len(candidate) == 1,
              "serving_revision_match": None, "runtime_scope_match": None, "actual_group_account_match": None}
    if len(candidate) != 1:
        return {**result, "reason": "slot_not_unique"}
    _, configured = candidate[0]
    try:
        service = reader("services", service_name, project, region)
        require(service.get("metadata", {}).get("name") == service_name, "runtime_identity_unavailable")
        resource_version = service.get("metadata", {}).get("resourceVersion")
        require(single_line_text(resource_version), "runtime_identity_unavailable")
        revision_name = _serving(service)
        result["serving_revision_match"] = revision_name == configured["runtime_revision"]
        require(result["serving_revision_match"], "configured_revision_mismatch")
        revision = reader("revisions", revision_name, project, region)
        require(revision.get("metadata", {}).get("name") == revision_name and
                any(row.get("type") == "Ready" and row.get("status") == "True"
                    for row in revision.get("status", {}).get("conditions", []) if isinstance(row, dict)),
                "runtime_identity_unavailable")
        env = _environment(revision.get("spec", {}))
        runtime = _json(env.get("RUNTIME_TARGET_JSON", ""))
        require(isinstance(runtime, dict), "runtime_identity_unavailable")
        equal = runtime.get("strategy_profile") == profile and runtime.get("execution_mode") == "live" and all(
            account_selector(runtime.get(field)) == account_selector(target.get(field)) if field == "account_selector"
            else runtime.get(field) == target.get(field) for field in IDENTITY_FIELDS)
        result["runtime_scope_match"] = equal
        require(equal, "runtime_scope_mismatch")
        result["actual_group_account_match"], group_reason = _actual_group_match(env, configured["account_selector"])
        captured_receipt = []
        if run_id:
            result["observed_group_account_match"] = _receipt_account_match(
                run_id, profile, service_name, revision_name, revision.get("metadata", {}).get("labels", {}),
                configured["account_selector"], reader=receipt_reader, private_receipt=captured_receipt)
        after = reader("services", service_name, project, region)
        require(after.get("metadata", {}).get("resourceVersion") == resource_version
                and _serving(after) == revision_name, "serving_changed_during_read")
        if run_id:
            matched = result["observed_group_account_match"]
            if matched and private_output is not None:
                private_output.update(record=captured_receipt[0][0], archive=captured_receipt[0][1], runtime_target=runtime)
            return {**result, "status": "matched_at_observation" if matched else "mismatched",
                    "reason": "receipt_account_binding_verified" if matched else "receipt_account_mismatch"}
        if result["actual_group_account_match"] is not True:
            return {**result, "reason": group_reason}
        return {**result, "status": "matched", "reason": "binding_verified"}
    except BindingError as exc:
        return {**result, "reason": str(exc)}
    except Exception:
        return {**result, "reason": "metadata_read_failed"}


def diagnose(env, *, preflight=False, reader=_read, receipt_reader=_gh):
    slots, targets, project, region = configuration(env)
    run_id = env.get("RECONCILIATION_RUN_ID", "").strip()
    require(not run_id or (len(targets) == 1 and re.fullmatch(r"[0-9]{1,20}", run_id)), "receipt_run_invalid")
    if preflight:
        return {"status": "configuration_shape_valid", "targets": {profile: {"status": "not_read"} for profile in targets}}
    results = {profile: observe(profile, service, target, slots, project, region, reader=reader,
                                run_id=run_id or None, receipt_reader=receipt_reader)
               for profile, (service, target) in targets.items()}
    statuses = {row["status"] for row in results.values()}
    status = "matched_at_observation" if statuses == {"matched_at_observation"} else (
        "matched" if statuses == {"matched"} else "unknown")
    return {"status": status,
            "targets": results}


def main(argv=None):
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        require(args in ([], ["--preflight"]), "arguments_invalid")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = diagnose(os.environ, preflight=bool(args), reader=_read)
    except BindingError as exc:
        result = {"status": "unknown", "reason": str(exc)}
    except Exception:
        result = {"status": "unknown", "reason": "configuration_invalid"}
    print(json.dumps({"schema_version": "ibkr.flex_runtime_binding.v1", **result}, sort_keys=True))
    return 0 if result["status"] in {"matched", "matched_at_observation", "configuration_shape_valid"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
