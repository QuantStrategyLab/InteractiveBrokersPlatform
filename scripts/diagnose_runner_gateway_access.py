"""Read the original platform runner's declared Gateway binding and static IAP access.

Uses its existing identity only. No socket, broker call, image pull or resource write.
Private configuration and native-client output remain inside the protected runner.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace
import urllib.request

from scripts import deploy_pilot_no_traffic as pilot
from scripts import diagnose_pilot_serving_install as serving

PERMISSION = "iap.tunnelInstances.accessViaIAP"
REASONS = serving.REASONS | frozenset({"group_declaration_unavailable", "group_secret_unavailable",
    "group_configuration_invalid", "gateway_binding_invalid", "gateway_instance_unavailable",
    "gateway_instance_invalid", "project_number_unavailable", "iap_check_unavailable",
    "iap_response_invalid", "iap_permission_not_observed", "static_access_observed"})


def result():
    return dict(schema_version="ibkr.runner_gateway_access_preflight.v1", status="unavailable",
        reason="diagnostic_error", observed_at=datetime.now(timezone.utc).isoformat(),
        group_configuration_observed=False, gateway_binding_verified=False,
        positive_client_id_verified=False, compute_instance_read=False,
        iap_permission_observed=None, source_stable=False, network_path_verified=False,
        runtime_ready=False, broker_called=False, image_pull_attempted=False,
        resources_created=False, configuration_changed=False, identity_changed=False)


def command(args, reason):
    allowed = (("gcloud", "run", "services", "describe"), ("gcloud", "run", "revisions", "describe"),
        ("gcloud", "secrets", "versions", "access"), ("gcloud", "compute", "instances", "describe"),
        ("gcloud", "projects", "describe"), ("gcloud", "auth", "print-access-token"))
    if not any(tuple(args[:len(prefix)]) == prefix for prefix in allowed):
        raise pilot.ReleaseError("operation_not_readonly")
    completed = subprocess.run(args, capture_output=True, text=True, timeout=30, env={**os.environ,
        "CLOUDSDK_CORE_DISABLE_FILE_LOGGING": "true", "CLOUDSDK_CORE_LOG_HTTP": "false",
        "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"})
    if completed.returncode or len(completed.stdout.encode()) > 4_000_000:
        raise pilot.ReleaseError(reason)
    return completed.stdout


def read_group(ctx, rows, run):
    from runtime_config_support import load_account_group_config, resolve_account_group

    def secret(name, version="latest"):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", name) or not re.fullmatch(r"latest|[1-9][0-9]*", version):
            raise pilot.ReleaseError("group_configuration_invalid")
        value = run(("gcloud", "secrets", "versions", "access", version, f"--project={ctx.project}",
            f"--secret={name}"), "group_secret_unavailable")
        if len(value.encode()) > 65536:
            raise pilot.ReleaseError("group_configuration_invalid")
        return value

    def declaration(name):
        matches = [row for row in rows if row.get("name") == name]
        if not matches:
            return None
        if len(matches) != 1:
            raise pilot.ReleaseError("group_configuration_invalid")
        row = matches[0]
        if "value" in row and "valueFrom" not in row and isinstance(row["value"], str):
            return row["value"]
        ref = row.get("valueFrom", {}).get("secretKeyRef", {})
        if "value" not in row and isinstance(ref.get("name"), str) and isinstance(ref.get("key"), str):
            return secret(ref["name"], ref["key"])
        raise pilot.ReleaseError("group_declaration_unavailable")

    class NativeSecretClient:
        def access_secret_version(self, *, request):
            prefix = f"projects/{ctx.project}/secrets/"
            name = request["name"]
            if not name.startswith(prefix) or not name.endswith("/versions/latest"):
                raise pilot.ReleaseError("group_configuration_invalid")
            value = secret(name[len(prefix):-len("/versions/latest")])
            return SimpleNamespace(payload=SimpleNamespace(data=value.encode()))

    secret_name = declaration("IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME")
    return load_account_group_config(project_id=ctx.project,
        account_group=resolve_account_group(declaration("ACCOUNT_GROUP")),
        raw_json=None if secret_name else declaration("IB_ACCOUNT_GROUP_CONFIG_JSON"),
        secret_name=secret_name,
        secret_client_factory=NativeSecretClient)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check_iap(resource, token):
    # A read-only IAM test, with no redirects/proxy and no token in arguments/logs.
    request = urllib.request.Request("https://iap.googleapis.com/v1/" + resource + ":testIamPermissions",
        data=json.dumps({"permissions": [PERMISSION]}).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=20) as response:
            body = response.read(65537)
        if len(body) > 65536:
            raise ValueError()
        value = json.loads(body)
    except Exception:
        raise pilot.ReleaseError("iap_check_unavailable") from None
    permissions = value.get("permissions", []) if isinstance(value, dict) else None
    if not isinstance(permissions, list) or any(item != PERMISSION for item in permissions):
        raise pilot.ReleaseError("iap_response_invalid")
    return PERMISSION in permissions


def diagnose(ctx, *, run=command, read=serving._read, group_reader=read_group, iap=check_iap):
    out = result()
    before = None
    try:
        before = read(ctx, run, include_group_declarations=True)
        group = group_reader(ctx, before["group_declarations"], run)
        out["group_configuration_observed"] = True
        project, zone, instance = group.ib_gateway_project_id, group.ib_gateway_zone, group.ib_gateway_instance_name
        if (group.execution_backend not in (None, "gateway") or group.ib_gateway_mode != "live"
                or tuple(group.account_ids) != tuple(ctx.target["account_selector"])
                or type(group.ib_client_id) is not int or group.ib_client_id <= 0
                or not isinstance(project, str) or not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project)
                or not isinstance(zone, str) or not re.fullmatch(r"[a-z]+(?:-[a-z]+)*[0-9]+-[a-z]", zone)
                or not isinstance(instance, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", instance)):
            raise pilot.ReleaseError("gateway_binding_invalid")
        out.update(gateway_binding_verified=True, positive_client_id_verified=True)
        vm = pilot._json(run(("gcloud", "compute", "instances", "describe", instance,
            f"--project={project}", f"--zone={zone}", "--format=json"), "gateway_instance_unavailable"))
        vm_id = str(vm.get("id", ""))
        if vm.get("name") != instance or vm.get("status") != "RUNNING" or not re.fullmatch(r"[0-9]+", vm_id):
            raise pilot.ReleaseError("gateway_instance_invalid")
        out["compute_instance_read"] = True
        number = run(("gcloud", "projects", "describe", project, "--format=value(projectNumber)"),
            "project_number_unavailable").strip()
        if not re.fullmatch(r"[0-9]+", number):
            raise pilot.ReleaseError("project_number_unavailable")
        token = run(("gcloud", "auth", "print-access-token", "--quiet"), "iap_check_unavailable").strip()
        if not token or len(token) > 8192 or "\n" in token or "\r" in token:
            raise pilot.ReleaseError("iap_check_unavailable")
        permitted = iap(f"projects/{number}/iap_tunnel/zones/{zone}/instances/{vm_id}", token)
        if type(permitted) is not bool:
            raise pilot.ReleaseError("iap_response_invalid")
        out.update(status="observed", iap_permission_observed=permitted,
            reason="static_access_observed" if permitted else "iap_permission_not_observed")
    except pilot.ReleaseError as error:
        out["reason"] = str(error) if str(error) in REASONS else "diagnostic_error"
    except Exception:
        out["reason"] = "group_configuration_invalid" if before is not None else "diagnostic_error"
    if before is not None:
        try:
            out["source_stable"] = read(ctx, run, include_group_declarations=True) == before
            if not out["source_stable"]:
                out.update(status="unavailable", reason="serving_changed_during_probe")
        except Exception:
            out.update(status="unavailable", reason="source_read_failed")
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    out = result()
    try:
        out = diagnose(pilot.context(os.environ))
    except Exception:
        out["reason"] = "dispatch_not_admitted"
    args.receipt.write_text(json.dumps(out, sort_keys=True) + "\n")
    args.receipt.chmod(0o600)
    print(json.dumps(out, sort_keys=True))
    return 0 if out["status"] == "observed" and out["source_stable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
