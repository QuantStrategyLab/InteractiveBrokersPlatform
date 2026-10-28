"""Assemble only the fixed public candidate; private configuration stays in runner."""
import base64
import copy
from dataclasses import asdict
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile

from scripts import deploy_pilot_no_traffic as pilot
from scripts import pilot_app_isolation_entry as entry

CANDIDATE = "15427acb0ecc5e4c2450adf5276f90625a32e01a"
ENTRY = Path(__file__).with_name("pilot_app_isolation_entry.py")
SENSITIVE = re.compile(r"TOKEN|PASSWORD|CREDENTIAL|SECRET|API_KEY|TELEGRAM|DISCORD|NOTIF|WEBHOOK|SLACK|SMTP")


def bundle(source, *, cycle=False):
    def git(*args):
        try:
            value = subprocess.run(["git", "-C", str(source), *args], capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise pilot.ReleaseError("candidate_source_unavailable") from None
        if value.returncode:
            raise pilot.ReleaseError("candidate_source_unavailable")
        return value.stdout
    if git("rev-parse", "HEAD").decode().strip() != CANDIDATE:
        raise pilot.ReleaseError("candidate_source_unverified")
    return make_bundle(git("archive", "--format=tar", "HEAD"), cycle=cycle)


def make_bundle(raw, *, cycle=False):
    output = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(raw)) as source_tar, tarfile.open(fileobj=output, mode="w") as target:
        for member in source_tar.getmembers():
            if member.isfile() and not member.name.startswith(("tests/", "docs/", ".github/", "scripts/", "external/")):
                target.addfile(member, source_tar.extractfile(member))
        if cycle:
            code = Path(__file__).with_name("pilot_cycle_entry.py").read_bytes()
            member = tarfile.TarInfo("_p4_operator_cycle.py")
            member.size = len(code)
            target.addfile(member, io.BytesIO(code))
    data = gzip.compress(output.getvalue(), mtime=0)
    return data, hashlib.sha256(data).hexdigest()


def prepared(ctx, before, group, desired, source, *, phase="isolation"):
    desired = copy.deepcopy(desired)
    source_env = before["serving_revision"]["spec"]["containers"][0].get("env", [])
    groups = [row.get("value") for row in source_env if row.get("name") == "ACCOUNT_GROUP"]
    if len(groups) != 1 or not isinstance(groups[0], str) or not groups[0]:
        raise pilot.ReleaseError("application_group_declaration_unverified")
    kept = []
    for row in source_env:
        name = row.get("name", "")
        if name.startswith(("K_", "CLOUD_RUN_", "PILOT_")) or SENSITIVE.search(name) or name == "IB_ACCOUNT_GROUP_CONFIG_JSON":
            continue
        if "valueFrom" in row and name not in {"RUNTIME_TARGET_JSON", "QSL_RUNTIME_TARGET_JSON"}:
            raise pilot.ReleaseError("application_secret_scope_unverified")
        kept.append(copy.deepcopy(row))
    overrides = dict(IB_ACCOUNT_GROUP_CONFIG_JSON=json.dumps({"groups": {groups[0]: asdict(group)}}),
        GCP_PROJECT_ID=ctx.project, GOOGLE_CLOUD_PROJECT=ctx.project,
        PILOT_EXPECTED_TARGET=json.dumps(dict(project=ctx.project, accounts=list(group.account_ids),
            host=group.ib_gateway_instance_name, port=group.ib_gateway_port, client_id=group.ib_client_id,
            target={key: ctx.target[key] for key in pilot.IDENTITY_FIELDS})))
    if phase == "cycle":
        stores = [row.get("value") for row in source_env if row.get("name") == "EXECUTION_REPORT_GCS_URI"]
        if len(stores) != 1 or not isinstance(stores[0], str) or not stores[0].startswith("gs://"):
            raise pilot.ReleaseError("private_report_binding_unverified")
        overrides.update(PILOT_JOB_PHASE="cycle", PILOT_REPORT_STORE=stores[0])
    elif phase != "isolation":
        raise pilot.ReleaseError("job_phase_not_admitted")
    kept = [row for row in kept if row["name"] not in overrides]
    kept.extend(dict(name=key, value=value) for key, value in overrides.items())
    data, digest = bundle(source, cycle=True) if phase == "cycle" else bundle(source)
    encoded = base64.b64encode(data).decode()
    task = desired["spec"]["template"]["spec"]["template"]["spec"]
    task["timeoutSeconds"] = "600" if phase == "cycle" else "120"
    container = task["containers"][0]
    container.update(name="isolation", command=["/app/.venv/bin/python"],
        args=["-I", "-B", "-c", ENTRY.read_text(), digest, *[encoded[i:i+60000] for i in range(0, len(encoded), 60000)]], env=kept)
    return desired


def measurement(rows):
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise pilot.ReleaseError("measurement_not_unique")
    value = rows[0].get("jsonPayload")
    bools = {"isolation_passed", "candidate_source_verified", "dependencies_verified", "application_imported",
        "binding_verified", "outputs_isolated", "report_saved_readback", "sdk_guards_installed", "notification_guards_installed"}
    fixed = {"schema_version", "stage", "external_effect_attempts", "broker_called", "strategy_computed", "production_state_read"}
    stages = {"bundle_preflight", "application_import", "configuration_readback", "isolated_save_readback", "isolation_observed",
        "candidate_bundle_unverified", "candidate_lock_unverified", "original_dependencies_unverified", "external_effect_forbidden",
        "broker_or_notification_forbidden", "candidate_import_unverified", "application_binding_unverified", "application_outputs_unverified",
        "isolated_report_readback_unverified", "isolated_paths_unverified", "unexpected_external_effect_attempted"}
    if (not isinstance(value, dict) or not bools | fixed <= set(value) or set(value) - (bools | fixed | {"failure_class"})
            or value.get("schema_version") != "ibkr.application_isolation.v1" or value.get("stage") not in stages
            or any(type(value[key]) is not bool for key in bools)
            or any(value[key] is not False for key in ("broker_called", "strategy_computed", "production_state_read"))
            or value["external_effect_attempts"] not in (None, 0)
            or value.get("failure_class") not in (None, "ImportError", "ModuleNotFoundError", "AttributeError", "TypeError", "ValueError", "PermissionError", "FileNotFoundError", "unavailable")
            or (value["isolation_passed"] and (not all(value[key] for key in bools) or value["external_effect_attempts"] != 0))):
        raise pilot.ReleaseError("application_response_invalid")
    return value


def cycle_measurement(rows):
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise pilot.ReleaseError("measurement_not_unique")
    value = rows[0].get("jsonPayload")
    if not isinstance(value, dict) or set(value) != {"schema_version", "isolation", "cycle"} or value["schema_version"] != "ibkr.guarded_cycle.v1":
        raise pilot.ReleaseError("cycle_response_invalid")
    measurement([{"jsonPayload": value["isolation"]}])
    from scripts.pilot_cycle_entry import CLOUD_DENIAL_KINDS, ISOLATION_DENIAL_KINDS, result
    cycle = value["cycle"]
    def valid_counts(counts, kinds, total):
        return (isinstance(counts, dict) and set(counts) == set(kinds)
                and all(type(count) is int and 0 <= count <= 1_000_000 for count in counts.values())
                and sum(counts.values()) == total)
    if (not isinstance(cycle, dict) or set(cycle) != set(result())
            or cycle["status"] not in {"unavailable", "waiting_market", "cycle_observed", "stopped"}
            or cycle["stage"] not in {"calendar", "market_closed", "original_dry_run", "private_report_save", "cycle_observed",
                "private_report_binding_unverified", "private_report_readback_unverified", "cycle_report_unverified", "broker_write_forbidden", "cloud_write_forbidden"}
            or any(type(cycle[key]) is not bool for key in ("cycle_completed", "broker_called", "signals_completed", "rebalance_completed", "saved_readback_verified"))
            or (cycle["market_open"] is not None and type(cycle["market_open"]) is not bool)
            or any(type(cycle[key]) is not int or not 0 <= cycle[key] <= 1_000_000 for key in ("broker_write_attempts", "cloud_write_attempts", "isolated_effect_attempts"))
            or not valid_counts(cycle["cloud_denials"], CLOUD_DENIAL_KINDS, cycle["cloud_write_attempts"])
            or not valid_counts(cycle["isolation_denials"], ISOLATION_DENIAL_KINDS, cycle["isolated_effect_attempts"])
            or any(cycle[key] is not None and (type(cycle[key]) is not int or not 0 <= cycle[key] <= 1_000_000)
                for key in ("http_status", "orders_previewed_count", "orders_skipped_count"))
            or cycle["report_status"] not in {None, "ok", "error", "skipped", "started", "unavailable"}
            or (cycle["cycle_completed"] and (not value["isolation"]["isolation_passed"] or cycle["market_open"] is not True
                or not all(cycle[key] for key in ("broker_called", "signals_completed", "rebalance_completed", "saved_readback_verified"))
                or cycle["broker_write_attempts"] or cycle["cloud_write_attempts"] or cycle["isolated_effect_attempts"]
                or cycle["report_status"] != "ok" or cycle["http_status"] != 200))):
        raise pilot.ReleaseError("cycle_response_invalid")
    return value
