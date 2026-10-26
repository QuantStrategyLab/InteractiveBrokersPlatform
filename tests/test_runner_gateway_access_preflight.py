"""Synthetic configuration and IAM responses only; no cloud or broker calls."""
from dataclasses import replace
import json
import hashlib
import subprocess
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
        service_name="synthetic-service", account_ids=("U1",))


def inspect(*, configured=None, permission=True, failure=None, drift=False):
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
        group_reader=lambda *args: configured or group(), iap=iap)
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


@pytest.mark.parametrize("changes", [dict(ib_client_id=0), dict(ib_client_id=True),
    dict(account_ids=("U2",)), dict(ib_gateway_mode="paper"), dict(ib_gateway_project_id="bad;project")])
def test_wrong_account_or_invalid_binding_stops_before_vm_or_iam(changes):
    out, calls, _ = inspect(configured=replace(group(), **changes))
    assert out["reason"] == "gateway_binding_invalid"
    assert not calls


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
        "ib_gateway_instance_name": "synthetic-vm", "account_ids": ["U1"]}}})
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
