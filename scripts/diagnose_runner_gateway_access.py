"""Read the original platform runner's declared Gateway binding and static IAP access.

Uses its existing identity only. An optional direct TCP check sends no protocol data.
No broker call, image pull or resource write.
Private configuration and native-client output remain inside the protected runner.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
from types import SimpleNamespace
import urllib.error
import urllib.request

from scripts import deploy_pilot_no_traffic as pilot
from scripts import diagnose_pilot_serving_install as serving

PERMISSION = "iap.tunnelInstances.accessViaIAP"
REASONS = serving.REASONS | frozenset({"group_declaration_unavailable", "group_secret_unavailable",
    "group_configuration_invalid", "gateway_binding_invalid", "gateway_instance_unavailable",
    "gateway_instance_invalid", "project_number_unavailable", "iap_check_unavailable",
    "iap_response_invalid", "iap_permission_not_observed", "static_access_observed",
    "direct_gateway_declaration_observed", "tcp_check_requires_direct_route",
    "gateway_tcp_connected", "gateway_tcp_not_connected"})


def result():
    return dict(schema_version="ibkr.runner_gateway_access_preflight.v1", status="unavailable",
        reason="diagnostic_error", observed_at=datetime.now(timezone.utc).isoformat(),
        group_configuration_observed=False, gateway_binding_verified=False,
        binding_checks={}, gateway_address_route=None,
        gateway_tcp_probe_attempted=False, gateway_tcp_connected=None, tcp_failure_class=None,
        broker_protocol_sent=False,
        positive_client_id_verified=False, compute_instance_read=False,
        iap_permission_observed=None, iap_http_status=None, cloud_read_failure_class=None,
        source_stable=False, network_path_verified=False,
        runtime_ready=False, broker_called=False, image_pull_attempted=False,
        resources_created=False, configuration_changed=False, identity_changed=False)


class ReadFailure(pilot.ReleaseError):
    def __init__(self, reason, failure_class, http_status=None):
        super().__init__(reason)
        self.failure_class = failure_class
        self.http_status = http_status


def command(args, reason):
    allowed = (("gcloud", "run", "services", "describe"), ("gcloud", "run", "revisions", "describe"),
        ("gcloud", "secrets", "versions", "access"), ("gcloud", "compute", "instances", "describe"),
        ("gcloud", "projects", "describe"), ("gcloud", "auth", "print-access-token"))
    if not any(tuple(args[:len(prefix)]) == prefix for prefix in allowed):
        raise pilot.ReleaseError("operation_not_readonly")
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=30, env={**os.environ,
            "CLOUDSDK_CORE_DISABLE_FILE_LOGGING": "true", "CLOUDSDK_CORE_LOG_HTTP": "false",
            "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"})
    except subprocess.TimeoutExpired:
        raise ReadFailure(reason, "timeout") from None
    if completed.returncode or len(completed.stdout.encode()) > 4_000_000:
        detail = completed.stderr.lower()
        category = ("api_disabled" if "service_disabled" in detail or "has not been used" in detail else
            "permission_denied" if "permission_denied" in detail or "does not have" in detail or "required '" in detail else
            "not_found" if "not_found" in detail or "was not found" in detail else "unclassified")
        raise ReadFailure(reason, category)
    return completed.stdout


def read_group(ctx, rows, run):
    from runtime_config_support import (first_non_empty, load_account_group_config,
        resolve_account_group, resolve_execution_backend, resolve_ib_gateway_mode, resolve_ib_gateway_port)

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
    group = load_account_group_config(project_id=ctx.project,
        account_group=resolve_account_group(declaration("ACCOUNT_GROUP")),
        raw_json=None if secret_name else declaration("IB_ACCOUNT_GROUP_CONFIG_JSON"),
        secret_name=secret_name,
        secret_client_factory=NativeSecretClient)
    return replace(group,
        execution_backend=resolve_execution_backend(
            group.execution_backend or declaration("IBKR_EXECUTION_BACKEND")),
        ib_gateway_mode=resolve_ib_gateway_mode(group.ib_gateway_mode),
        ib_gateway_port=resolve_ib_gateway_port(group.ib_gateway_port if group.ib_gateway_port is not None
            else declaration("IB_GATEWAY_PORT"), gateway_mode=resolve_ib_gateway_mode(group.ib_gateway_mode)),
        ib_gateway_zone=first_non_empty(group.ib_gateway_zone,
            declaration("IB_GATEWAY_ZONE") if not group.ib_gateway_zone else None) or "")


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
    except urllib.error.HTTPError as error:
        raise ReadFailure("iap_check_unavailable", "http_rejected", error.code) from None
    except TimeoutError:
        raise ReadFailure("iap_check_unavailable", "timeout") from None
    except Exception:
        raise pilot.ReleaseError("iap_check_unavailable") from None
    permissions = value.get("permissions", []) if isinstance(value, dict) else None
    if not isinstance(permissions, list) or any(item != PERMISSION for item in permissions):
        raise pilot.ReleaseError("iap_response_invalid")
    return PERMISSION in permissions


def check_direct_tcp(host, port):
    try:
        # No send/recv, API handshake, authentication, retry, or alternative route.
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not addresses:
            return False, "name_resolution_failed"
        family, kind, protocol, _, address = addresses[0]
        with socket.socket(family, kind, protocol) as connection:
            connection.settimeout(5)
            connection.connect(address)
            return True, None
    except socket.gaierror:
        return False, "name_resolution_failed"
    except TimeoutError:
        return False, "timeout"
    except ConnectionRefusedError:
        return False, "connection_refused"
    except OSError:
        return False, "network_error"


def diagnose(ctx, *, run=command, read=serving._read, group_reader=read_group, iap=check_iap,
             direct_tcp=False, tcp=check_direct_tcp):
    out = result()
    before = None
    try:
        before = read(ctx, run, include_group_declarations=True)
        group = group_reader(ctx, before["group_declarations"], run)
        out["group_configuration_observed"] = True
        project = group.ib_gateway_project_id or ctx.project
        zone, instance = group.ib_gateway_zone or "", group.ib_gateway_instance_name
        checks = dict(backend_gateway=group.execution_backend in (None, "gateway"),
            mode_live=group.ib_gateway_mode == "live",
            account_scope_exact=tuple(group.account_ids) == tuple(ctx.target["account_selector"]),
            client_id_valid=type(group.ib_client_id) is int and group.ib_client_id >= 0,
            project_valid=isinstance(project, str) and bool(re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project)),
            project_zone_consistent=not group.ib_gateway_project_id or bool(zone),
            zone_valid=isinstance(zone, str) and (not zone or bool(re.fullmatch(r"[a-z]+(?:-[a-z]+)*[0-9]+-[a-z]", zone))),
            host_valid=isinstance(instance, str) and bool(instance.strip()) and (
                not zone or bool(re.fullmatch(r"[a-z][a-z0-9-]{0,62}", instance))))
        out.update(binding_checks=checks, gateway_address_route="gce" if zone else "direct",
            positive_client_id_verified=type(group.ib_client_id) is int and group.ib_client_id > 0)
        if not all(checks.values()):
            raise pilot.ReleaseError("gateway_binding_invalid")
        out["gateway_binding_verified"] = True
        if not zone:
            out.update(status="observed", reason="direct_gateway_declaration_observed")
            if direct_tcp:
                if not out["positive_client_id_verified"] or type(group.ib_gateway_port) is not int or not 1 <= group.ib_gateway_port <= 65535:
                    raise pilot.ReleaseError("gateway_binding_invalid")
                out["gateway_tcp_probe_attempted"] = True
                connected, failure = tcp(instance, group.ib_gateway_port)
                if type(connected) is not bool or failure not in (None, "name_resolution_failed", "timeout", "connection_refused", "network_error") or connected != (failure is None):
                    raise pilot.ReleaseError("diagnostic_error")
                out.update(gateway_tcp_connected=connected, tcp_failure_class=failure,
                    reason="gateway_tcp_connected" if connected else "gateway_tcp_not_connected")
        elif direct_tcp:
            raise pilot.ReleaseError("tcp_check_requires_direct_route")
        else:
            observe_vm_iap(out, project, zone, instance, run, iap)
    except pilot.ReleaseError as error:
        out["status"] = "unavailable"
        out["reason"] = str(error) if str(error) in REASONS else "diagnostic_error"
        if isinstance(error, ReadFailure):
            out.update(cloud_read_failure_class=error.failure_class, iap_http_status=error.http_status)
    except Exception:
        out["status"] = "unavailable"
        out["reason"] = "group_configuration_invalid" if before is not None else "diagnostic_error"
    if before is not None:
        try:
            out["source_stable"] = read(ctx, run, include_group_declarations=True) == before
            if not out["source_stable"]:
                out.update(status="unavailable", reason="serving_changed_during_probe")
        except Exception:
            out.update(status="unavailable", reason="source_read_failed")
    return out


def observe_vm_iap(out, project, zone, instance, run, iap):
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
    out.update(status="observed", iap_permission_observed=permitted, iap_http_status=200,
        reason="static_access_observed" if permitted else "iap_permission_not_observed")


def configuration_reference(ctx, *, run=command, read=serving._read):
    """Attest the selected literal reference without retrying any payload access."""
    out = dict(schema_version="ibkr.runner_config_reference.v1", status="unavailable",
        reason="configuration_reference_unavailable", reference_sha256=None, source_stable=False,
        secret_payload_read=False, iam_changed=False, broker_called=False)
    try:
        before = read(ctx, run, include_group_declarations=True)
        rows = before["group_declarations"]
        def literal(name):
            found = [row for row in rows if row.get("name") == name]
            if len(found) != 1 or "valueFrom" in found[0] or not isinstance(found[0].get("value"), str):
                raise ValueError()
            return found[0]["value"]
        name = literal("IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME")
        if not literal("ACCOUNT_GROUP").strip() or not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", name):
            raise ValueError()
        reference = f"projects/{ctx.project}/secrets/{name}"
        out["reference_sha256"] = hashlib.sha256(reference.encode()).hexdigest()
        out["source_stable"] = read(ctx, run, include_group_declarations=True) == before
        if out["source_stable"]:
            out.update(status="observed", reason="literal_configuration_reference_observed")
        else:
            out["reason"] = "serving_changed_during_probe"
    except Exception:
        pass
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--configuration-reference-only", action="store_true")
    parser.add_argument("--direct-tcp", action="store_true")
    args = parser.parse_args(argv)
    out = result()
    try:
        ctx = pilot.context(os.environ)
        if args.configuration_reference_only and args.direct_tcp:
            raise pilot.ReleaseError("dispatch_not_admitted")
        out = configuration_reference(ctx) if args.configuration_reference_only else diagnose(ctx, direct_tcp=args.direct_tcp)
    except Exception:
        out["reason"] = "dispatch_not_admitted"
    args.receipt.write_text(json.dumps(out, sort_keys=True) + "\n")
    args.receipt.chmod(0o600)
    print(json.dumps(out, sort_keys=True))
    return 0 if out["status"] == "observed" and out["source_stable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
