"""Synthetic configuration and IAM responses only; no cloud or broker calls."""
from dataclasses import replace
import json
import hashlib
import subprocess
import socket
from types import SimpleNamespace

import pytest

from runtime_config_support import AccountGroupConfig
from scripts import deploy_pilot_no_traffic as pilot
from scripts import diagnose_runner_gateway_access as probe


def context():
    return SimpleNamespace(project="synthetic-project", service="synthetic-service",
        target={"account_selector": ["U1"]})


def group():
    return AccountGroupConfig(ib_gateway_project_id="synthetic-gateway", ib_gateway_zone="us-central1-a",
        ib_gateway_instance_name="synthetic-vm", ib_gateway_mode="live", ib_client_id=7,
        ib_gateway_port=4001, service_name="synthetic-service", account_ids=("U1",))


def inspect(*, configured=None, permission=True, failure=None, drift=False, direct_tcp=False,
            tcp=lambda *a: pytest.fail("TCP requires explicit opt-in")):
    calls = []
    reads = []

    def read(ctx, run, **kwargs):
        reads.append(kwargs)
        return {"group_declarations": [], "observed_version": 2 if drift and len(reads) == 2 else 1}

    def run(args, reason):
        calls.append(tuple(args))
        if failure:
            raise pilot.ReleaseError(failure)
        if args[:4] == ("gcloud", "compute", "instances", "describe"):
            return json.dumps(dict(name="synthetic-vm", status="RUNNING", id="1234"))
        if args[:3] == ("gcloud", "projects", "describe"):
            return "5678"
        if args[:3] == ("gcloud", "auth", "print-access-token"):
            return "private-synthetic-token"
        raise AssertionError("unexpected command")

    def iap(resource, token):
        assert resource == "projects/5678/iap_tunnel/zones/us-central1-a/instances/1234"
        assert token == "private-synthetic-token"
        return permission

    out = probe.diagnose(context(), read=read, run=run,
        group_reader=lambda *args: configured or group(), iap=iap, direct_tcp=direct_tcp, tcp=tcp)
    return out, calls, reads


def test_static_permission_never_claims_network_or_runtime_ready():
    out, calls, reads = inspect()
    assert out["status"] == "observed" and out["iap_permission_observed"] is True
    assert out["source_stable"] and out["gateway_binding_verified"]
    for key in ("network_path_verified", "runtime_ready", "broker_called", "image_pull_attempted",
                "configuration_changed", "identity_changed", "resources_created"):
        assert out[key] is False
    assert len(calls) == 3 and len(reads) == 2
    assert "private-synthetic-token" not in json.dumps(out)
    assert "synthetic-vm" not in json.dumps(out)


def test_missing_static_permission_stays_distinct_from_a_network_failure():
    out, _, _ = inspect(permission=False)
    assert out["status"] == "observed"
    assert out["reason"] == "iap_permission_not_observed"
    assert out["iap_permission_observed"] is False
    assert not out["network_path_verified"]


@pytest.mark.parametrize("changes", [dict(ib_client_id=True),
    dict(account_ids=("U2",)), dict(ib_gateway_mode="paper"), dict(ib_gateway_project_id="bad;project")])
def test_wrong_account_or_invalid_binding_stops_before_vm_or_iam(changes):
    out, calls, _ = inspect(configured=replace(group(), **changes))
    assert out["reason"] == "gateway_binding_invalid"
    assert not calls


@pytest.mark.parametrize("host", ["192.0.2.1", "synthetic.example.invalid"])
def test_original_direct_host_skips_vm_and_iap_without_claiming_connectivity(host):
    out, calls, _ = inspect(configured=replace(group(), ib_gateway_project_id=None,
        ib_gateway_zone=None, ib_gateway_instance_name=host))
    assert out["status"] == "observed" and out["gateway_binding_verified"]
    assert out["reason"] == "direct_gateway_declaration_observed"
    assert out["gateway_address_route"] == "direct" and not calls
    assert out["iap_permission_observed"] is None
    assert not out["network_path_verified"] and not out["runtime_ready"]
    assert host not in json.dumps(out)


def test_original_default_project_is_used_for_zoned_instance():
    out, calls, _ = inspect(configured=replace(group(), ib_gateway_project_id=None))
    assert out["status"] == "observed"
    assert "--project=synthetic-project" in calls[0]
    assert calls[1][3] == "synthetic-project"


def test_explicit_project_without_zone_matches_original_source_rejection():
    out, calls, _ = inspect(configured=replace(group(), ib_gateway_zone=None))
    assert out["reason"] == "gateway_binding_invalid" and not calls
    assert out["binding_checks"]["project_zone_consistent"] is False


def test_zero_client_id_does_not_block_readonly_metadata_but_is_not_ready_for_isolation():
    out, calls, _ = inspect(configured=replace(group(), ib_client_id=0))
    assert out["status"] == "observed" and len(calls) == 3
    assert out["binding_checks"]["client_id_valid"]
    assert not out["positive_client_id_verified"] and not out["runtime_ready"]


def test_binding_failures_are_reported_independently_without_private_values():
    out, _, _ = inspect(configured=replace(group(), account_ids=("U2",), ib_client_id=0))
    assert out["binding_checks"]["account_scope_exact"] is False
    assert out["binding_checks"]["client_id_valid"] is True
    assert not out["positive_client_id_verified"]
    assert "U2" not in json.dumps(out)


@pytest.mark.parametrize("connected,failure", [(True, None), (False, "timeout")])
def test_opt_in_tcp_checks_only_original_direct_address_once(connected, failure):
    calls = []
    def tcp(host, port):
        calls.append((host, port))
        return connected, failure
    configured = replace(group(), ib_gateway_project_id=None, ib_gateway_zone=None,
        ib_gateway_instance_name="192.0.2.1")
    out, native_calls, _ = inspect(configured=configured, direct_tcp=True, tcp=tcp)
    assert calls == [("192.0.2.1", 4001)] and not native_calls
    assert out["status"] == "observed" and out["gateway_tcp_probe_attempted"]
    assert out["gateway_tcp_connected"] is connected and out["tcp_failure_class"] == failure
    assert not out["broker_protocol_sent"] and not out["broker_called"]
    assert not out["runtime_ready"] and not out["network_path_verified"]
    assert "192.0.2.1" not in json.dumps(out)


@pytest.mark.parametrize("changes", [dict(ib_client_id=0), dict(account_ids=("U2",)),
    dict(ib_gateway_port=0), dict(ib_gateway_port=65536), dict(ib_gateway_port=True)])
def test_tcp_rejects_invalid_isolation_binding_before_socket(changes):
    configured = replace(group(), ib_gateway_project_id=None, ib_gateway_zone=None, **changes)
    out, native_calls, _ = inspect(configured=configured, direct_tcp=True)
    assert out["status"] == "unavailable" and out["reason"] == "gateway_binding_invalid"
    assert not out["gateway_tcp_probe_attempted"] and not native_calls


def test_tcp_never_falls_back_to_a_vm_or_iap_route():
    out, native_calls, _ = inspect(direct_tcp=True)
    assert out["status"] == "unavailable" and out["reason"] == "tcp_check_requires_direct_route"
    assert not native_calls and not out["gateway_tcp_probe_attempted"]


def test_tcp_probe_closes_socket_without_sending_or_receiving_protocol(monkeypatch):
    events = []
    class Connection:
        def __enter__(self):
            events.append("opened")
            return self
        def __exit__(self, *args):
            events.append("closed")
        def settimeout(self, timeout):
            assert timeout == 5
        def connect(self, address):
            assert address == ("192.0.2.1", 4001)
            events.append("connected")
    def resolve(host, port, *, type):
        assert host == "synthetic.example.invalid" and port == 4001 and type == socket.SOCK_STREAM
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 4001)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.2", 4001))]
    def connection(family, kind, protocol):
        assert (family, kind, protocol) == (socket.AF_INET, socket.SOCK_STREAM, 6)
        return Connection()
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket, "socket", connection)
    assert probe.check_direct_tcp("synthetic.example.invalid", 4001) == (True, None)
    assert events == ["opened", "connected", "closed"]


@pytest.mark.parametrize("error,category", [(socket.gaierror("private-host"), "name_resolution_failed"),
    (TimeoutError("private-host"), "timeout"), (ConnectionRefusedError("private-host"), "connection_refused"),
    (OSError("private-host"), "network_error")])
def test_tcp_failure_is_fixed_class_without_details_or_retry(monkeypatch, error, category):
    calls = []
    def connect(*args, **kwargs):
        calls.append(True)
        raise error
    monkeypatch.setattr(socket, "getaddrinfo", connect)
    assert probe.check_direct_tcp("synthetic.example.invalid", 4001) == (False, category)
    assert calls == [True]


def test_missing_compute_permission_stops_without_token_or_iap_retry():
    out, calls, reads = inspect(failure="gateway_instance_unavailable")
    assert out["reason"] == "gateway_instance_unavailable"
    assert len(calls) == 1 and len(reads) == 2
    assert out["iap_permission_observed"] is None


def test_source_drift_overrides_a_successful_static_check():
    out, _, _ = inspect(drift=True)
    assert out["status"] == "unavailable" and out["reason"] == "serving_changed_during_probe"


def test_secret_priority_uses_only_original_selected_source():
    payload = json.dumps({"groups": {"synthetic": {
        "ib_gateway_instance_name": "synthetic-vm", "ib_gateway_mode": "live", "account_ids": ["U1"]}}})
    rows = [dict(name="ACCOUNT_GROUP", value="synthetic"),
        dict(name="IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME", value="selected-config"),
        dict(name="IB_ACCOUNT_GROUP_CONFIG_JSON", valueFrom={"secretKeyRef": {"name": "unused-config", "key": "latest"}})]
    calls = []

    def run(args, reason):
        calls.append(tuple(args))
        assert args[-1] == "--secret=selected-config"
        return payload

    selected = probe.read_group(context(), rows, run)
    assert selected.account_ids == ("U1",) and len(calls) == 1


@pytest.mark.parametrize("declared_backend", [None, "gateway"])
def test_group_reader_applies_original_zone_and_backend_fallbacks(declared_backend):
    payload = {"groups": {"synthetic": {"ib_gateway_instance_name": "synthetic-vm",
        "ib_gateway_mode": "live", "execution_backend": declared_backend, "account_ids": ["U1"]}}}
    rows = [dict(name="ACCOUNT_GROUP", value="synthetic"),
        dict(name="IB_ACCOUNT_GROUP_CONFIG_JSON", value=json.dumps(payload)),
        dict(name="IB_GATEWAY_ZONE", value="us-central1-a"),
        dict(name="IB_GATEWAY_PORT", value="4003"),
        dict(name="IBKR_EXECUTION_BACKEND", value="gateway")]
    selected = probe.read_group(context(), rows, lambda *a: pytest.fail("no secret read"))
    assert selected.ib_gateway_zone == "us-central1-a"
    assert selected.ib_gateway_project_id is None
    assert selected.execution_backend == "gateway"
    assert selected.ib_gateway_port == 4003


def test_group_values_take_priority_without_reading_unused_secret_fallbacks():
    payload = {"groups": {"synthetic": {"ib_gateway_instance_name": "synthetic-vm",
        "ib_gateway_mode": "live", "ib_gateway_zone": "us-central1-a", "execution_backend": "gateway"}}}
    rows = [dict(name="ACCOUNT_GROUP", value="synthetic"),
        dict(name="IB_ACCOUNT_GROUP_CONFIG_JSON", value=json.dumps(payload))]
    rows += [dict(name=name, valueFrom={"secretKeyRef": {"name": "unused", "key": "latest"}})
        for name in ("IB_GATEWAY_ZONE", "IBKR_EXECUTION_BACKEND")]
    selected = probe.read_group(context(), rows, lambda *a: pytest.fail("unused secret fallback"))
    assert selected.ib_gateway_zone == "us-central1-a" and selected.execution_backend == "gateway"


def test_readonly_command_allowlist_rejects_mutations_before_subprocess():
    with pytest.raises(pilot.ReleaseError, match="operation_not_readonly"):
        probe.command(("gcloud", "compute", "instances", "set-service-account"), "diagnostic_error")


def test_native_permission_failure_is_classified_without_exposing_stderr(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "",
        "PERMISSION_DENIED private-identity private-project"))
    with pytest.raises(probe.ReadFailure) as caught:
        probe.command(("gcloud", "compute", "instances", "describe", "synthetic"), "gateway_instance_unavailable")
    assert caught.value.failure_class == "permission_denied"
    assert str(caught.value) == "gateway_instance_unavailable"


def test_reference_only_attests_exact_source_without_secret_or_other_cloud_reads():
    rows = [dict(name="ACCOUNT_GROUP", value="synthetic"),
        dict(name="IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME", value="private-configuration")]
    reads = []
    def read(ctx, run, **kwargs):
        reads.append(True)
        return {"group_declarations": rows}
    out = probe.configuration_reference(context(), read=read,
        run=lambda *a: pytest.fail("no payload, token, VM, IAM or other native command"))
    assert out["status"] == "observed" and out["source_stable"]
    assert out["reference_sha256"] == hashlib.sha256(
        b"projects/synthetic-project/secrets/private-configuration").hexdigest()
    assert "private-configuration" not in json.dumps(out)
    assert out["secret_payload_read"] is False and out["iam_changed"] is False
    assert len(reads) == 2


def test_reference_only_refuses_unresolved_secret_declarations():
    rows = [dict(name="ACCOUNT_GROUP", value="synthetic"),
        dict(name="IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME", valueFrom={"secretKeyRef": {"name": "private", "key": "latest"}})]
    out = probe.configuration_reference(context(), read=lambda *a, **k: {"group_declarations": rows},
        run=lambda *a: pytest.fail("must not fetch an unresolved declaration"))
    assert out["status"] == "unavailable" and out["reference_sha256"] is None


def test_reference_only_and_tcp_modes_reject_before_configuration_read(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(pilot, "context", lambda *a: context())
    monkeypatch.setattr(probe, "configuration_reference", lambda *a: pytest.fail("no reference read"))
    monkeypatch.setattr(probe, "diagnose", lambda *a, **k: pytest.fail("no configuration or TCP read"))
    path = tmp_path / "safe.json"
    assert probe.main(["--configuration-reference-only", "--direct-tcp", "--receipt", str(path)]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "dispatch_not_admitted"
