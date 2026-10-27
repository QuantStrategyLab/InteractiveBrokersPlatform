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


def bundle(source):
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
    return make_bundle(git("archive", "--format=tar", "HEAD"))


def make_bundle(raw):
    output = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(raw)) as source_tar, tarfile.open(fileobj=output, mode="w") as target:
        for member in source_tar.getmembers():
            if member.isfile() and not member.name.startswith(("tests/", "docs/", ".github/", "scripts/", "external/")):
                target.addfile(member, source_tar.extractfile(member))
    data = gzip.compress(output.getvalue(), mtime=0)
    return data, hashlib.sha256(data).hexdigest()


def prepared(ctx, before, group, desired, source):
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
    kept = [row for row in kept if row["name"] not in overrides]
    kept.extend(dict(name=key, value=value) for key, value in overrides.items())
    data, digest = bundle(source)
    encoded = base64.b64encode(data).decode()
    task = desired["spec"]["template"]["spec"]["template"]["spec"]
    task["timeoutSeconds"] = "120"
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
