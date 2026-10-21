"""Offline safety cases for the one approved IBKR notification overlay."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from scripts import deploy_notification_only as release


ROOT = Path(__file__).resolve().parents[1]
IMAGE = release.REPOSITORY + "@sha256:" + "a" * 64


def admitted_env() -> dict[str, str]:
    return {
        "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
        "GITHUB_RUN_ATTEMPT": "1", "GITHUB_RUN_ID": "12345",
        "ENABLE_GITHUB_CLOUD_RUN_DEPLOY": "true",
        "INPUT_TARGET": "configured", "INPUT_CONFIGURED_SERVICE": "",
        "INPUT_NOTIFICATION_READBACK_ONLY": "false",
        "INPUT_SOURCE_COMMIT": release.SOURCE, "INPUT_DEPLOY_IMAGE": "false",
        "INPUT_SYNC_ENV": "false", "INPUT_APPROVE_TRAFFIC_SHIFT": "false",
        "INPUT_APPROVE_SCHEDULER_SYNC": "false", "GCP_PROJECT_ID": release.PROJECT,
        "GCP_DEPLOY_SERVICE_ACCOUNT": release.DEPLOYER,
        "GCP_RUNTIME_SERVICE_ACCOUNT": release.RUNTIME,
    }


@pytest.mark.parametrize("key,value", [
    ("GITHUB_REF", "refs/heads/candidate"), ("GITHUB_EVENT_NAME", "push"),
    ("GITHUB_RUN_ATTEMPT", "2"), ("ENABLE_GITHUB_CLOUD_RUN_DEPLOY", "false"),
    ("INPUT_SOURCE_COMMIT", "b" * 40), ("INPUT_TARGET", "hk-verify"),
    ("INPUT_CONFIGURED_SERVICE", "another-service"), ("INPUT_DEPLOY_IMAGE", "true"),
    ("INPUT_SYNC_ENV", "true"), ("INPUT_APPROVE_TRAFFIC_SHIFT", "true"),
    ("INPUT_APPROVE_SCHEDULER_SYNC", "true"), ("GCP_PROJECT_ID", "different"),
    ("GCP_DEPLOY_SERVICE_ACCOUNT", "another"), ("GCP_RUNTIME_SERVICE_ACCOUNT", "another"),
    ("GITHUB_RUN_ID", "not-numeric"),
])
def test_broader_dispatch_rejected_before_cloud(key, value):
    env = admitted_env()
    assert release.validate_dispatch(env) == "12345"
    env[key] = value
    with pytest.raises(release.ReleaseError):
        release.validate_dispatch(env)


@pytest.fixture
def original(monkeypatch):
    target = {"strategy_profile": "tqqq_growth_income", "execution_mode": "live", "dry_run_only": False,
              "strategy_release": {"strategy_revision": release.PACKAGES["us_equity_strategies-0.7.60"]},
              "live_continuity": {"opaque_fixture": "retain-without-reset"}}
    fingerprint = {key: value for key, value in target.items() if key != "live_continuity"}
    monkeypatch.setattr(release, "TARGET_HASH", release.sha(json.dumps(fingerprint, sort_keys=True, separators=(",", ":"))))
    return {"metadata": {"name": "fixture-serving", "labels": {"user-label": "keep", "commit-sha": "old"},
                         "annotations": {"autoscaling.knative.dev/minScale": "0", "run.googleapis.com/cpu-throttling": "true",
                                         "run.googleapis.com/ingress": "internal"}},
            "spec": {"serviceAccountName": release.RUNTIME, "containerConcurrency": 1, "timeoutSeconds": 60,
                     "volumes": [{"name": "data", "emptyDir": {"medium": "Memory"}}],
                     "containers": [{"image": release.BASE_IMAGE, "command": ["fixture-entrypoint"],
                                     "resources": {"limits": {"cpu": "1", "memory": "512Mi"}},
                                     "env": [{"name": "RUNTIME_TARGET_JSON", "value": json.dumps(target)},
                                             {"name": "ORDINARY_SETTING", "value": "fixture-preserved"},
                                             {"name": "CREDENTIAL_REFERENCE", "valueFrom": {"secretKeyRef": {"name": "fixture-secret", "key": "7"}}}]}]}}


@pytest.fixture
def service(original):
    desired = copy.deepcopy(original)
    desired["spec"]["containers"][0]["env"][1]["value"] = "different-latest-desired"
    return {"metadata": {"name": "fixture-service", "namespace": "fixture-project", "resourceVersion": "opaque-token",
                         "labels": {"user-service-label": "keep"}, "annotations": {"run.googleapis.com/ingress": "internal"}},
            "spec": {"template": desired, "traffic": [{"latestRevision": True, "percent": 100}]},
            "status": {"traffic": [{"revisionName": "fixture-serving", "percent": 100, "latestRevision": True},
                                   {"revisionName": "fixture-tagged", "percent": 0, "tag": "keep-tag"}]}}


def test_candidate_copies_serving_template_and_keeps_concrete_traffic(service, original):
    frozen = copy.deepcopy(original)
    body = release.candidate(service, original, IMAGE, "12345")
    revision = body["spec"]["template"]
    assert original == frozen
    assert body["metadata"]["resourceVersion"] == "opaque-token"
    assert release.env_map(revision)["ORDINARY_SETTING"]["value"] == "fixture-preserved"
    assert release.nonnotification(revision) == release.nonnotification(original)
    assert release.runtime_target(revision) == release.runtime_target(original)
    assert body["spec"]["traffic"] == release.routes(service)
    assert all("latestRevision" not in route for route in body["spec"]["traffic"])
    assert {key: release.env_map(revision)[key]["value"] for key in release.ROUTING} == release.ROUTING
    after = copy.deepcopy(service)
    after["spec"] = body["spec"]
    release.verify_readback(service, original, after, revision, body)


@pytest.fixture
def notification_inputs(original, monkeypatch):
    original["spec"]["containers"][0]["env"].extend({"name": k, "value": v} for k, v in release.ROUTING.items())
    binding = {"RUNTIME_TARGET_JSON": release.runtime_target(original), "DIGEST_OPAQUE_ACCOUNT_UID": "synthetic-uid",
               "DIGEST_TARGET_ID": "synthetic-canonical-target", "DIGEST_STORE_TARGET": release.NOTIFICATION_SLOT}
    row = {"platform_id": "ibkr", "opaque_account_uid": "synthetic-uid", "target_id": "synthetic-canonical-target",
           "strategy_profile": "tqqq_growth_income"}
    monkeypatch.setattr(release, "NOTIFICATION_IDENTITY_SHA", release._digest(row))
    stores = {"DIGEST_CANDIDATES_GCS_URI": "gs://synthetic-bucket/candidates", "IBKR_NOTIFICATION_MARKER_GCS_URI": "gs://synthetic-bucket/markers"}
    return binding, stores, {k: release.sha(v) for k, v in stores.items()}


def test_notification_binding_preparation_preserves_every_other_field_and_private_rollback(original, notification_inputs):
    frozen = copy.deepcopy(original)
    updated, rollback, safe = release.prepare_notification_binding(original, *notification_inputs)
    assert original == frozen
    assert set(rollback) == {*release.NOTIFICATION_IDENTITY_KEYS, *release.NOTIFICATION_STORE_KEYS}
    assert all(v is None for v in rollback.values())
    assert release.env_map(updated)["CREDENTIAL_REFERENCE"] == release.env_map(original)["CREDENTIAL_REFERENCE"]
    assert updated["metadata"] == original["metadata"]
    assert updated["spec"]["containers"][0]["image"] == original["spec"]["containers"][0]["image"]
    assert safe["identity_row_sha256"] == release.NOTIFICATION_IDENTITY_SHA
    assert safe["expected_single_member_sha256"] == release._digest([{"platform_id": "ibkr", "opaque_account_uid": "synthetic-uid",
        "target_id": "synthetic-canonical-target", "strategy_profile": "tqqq_growth_income"}])
    assert "synthetic-uid" not in json.dumps(safe) and "synthetic-canonical-target" not in json.dumps(safe)
    assert "gs://" not in json.dumps(safe) and "fixture-secret" not in json.dumps(safe)


def test_notification_binding_equal_effective_target_and_idempotent_input_reuses_existing_values(original, notification_inputs):
    original["spec"]["containers"][0]["env"].append({"name": "QSL_RUNTIME_TARGET_JSON", "value": json.dumps(release.runtime_target(original))})
    updated, _, _ = release.prepare_notification_binding(original, *notification_inputs)
    again, rollback, safe = release.prepare_notification_binding(updated, *notification_inputs)
    assert again == updated and not safe["changed_fields"]
    assert all(v is not None for v in rollback.values())


def test_notification_binding_unrecognized_effective_target_shape_is_not_absence(original, notification_inputs):
    original["spec"]["containers"][0]["env"].append({"name": "QSL_RUNTIME_TARGET_JSON", "valueSource": {"secretKeyRef": {"secret": "synthetic"}}})
    with pytest.raises(release.ReleaseError, match="shape_unknown"):
        release.prepare_notification_binding(original, *notification_inputs)


@pytest.mark.parametrize("source", ["binding", "qsl"])
def test_notification_binding_risk_boolean_and_integer_are_not_equal(original, notification_inputs, source):
    binding, stores, hashes = notification_inputs
    target = release.runtime_target(original)
    target["live_continuity"]["typed_guard"] = True
    release.env_map(original)["RUNTIME_TARGET_JSON"]["value"] = json.dumps(target)
    binding["RUNTIME_TARGET_JSON"] = copy.deepcopy(target)
    changed = copy.deepcopy(target)
    changed["live_continuity"]["typed_guard"] = 1
    if source == "binding":
        binding["RUNTIME_TARGET_JSON"] = changed
    else:
        original["spec"]["containers"][0]["env"].append({"name": "QSL_RUNTIME_TARGET_JSON", "value": json.dumps(changed)})
    with pytest.raises(release.ReleaseError):
        release.prepare_notification_binding(original, binding, stores, hashes)


@pytest.mark.parametrize("reference", [{}, None])
def test_notification_binding_empty_reference_is_not_absence(original, notification_inputs, reference):
    original["spec"]["containers"][0]["env"].append({"name": "QSL_RUNTIME_TARGET_JSON", "valueFrom": reference})
    with pytest.raises(release.ReleaseError, match="reference_unresolved"):
        release.prepare_notification_binding(original, *notification_inputs)


def test_notification_binding_full_template_verification_preserves_json_types(original, notification_inputs):
    binding, stores, _ = notification_inputs
    updated, _, _ = release.prepare_notification_binding(original, *notification_inputs)
    updated["spec"]["containerConcurrency"] = True
    values = {k: binding[k] for k in release.NOTIFICATION_IDENTITY_KEYS} | stores
    with pytest.raises(release.ReleaseError, match="other_template_field"):
        release.verify_notification_binding_update(original, updated, values)


@pytest.mark.parametrize("field", ["DIGEST_OPAQUE_ACCOUNT_UID", "DIGEST_TARGET_ID", "DIGEST_STORE_TARGET"])
def test_notification_binding_rejects_wrong_identity_without_mutating(original, notification_inputs, field):
    binding, stores, hashes = notification_inputs
    frozen = copy.deepcopy(original)
    binding[field] = "different"
    with pytest.raises(release.ReleaseError):
        release.prepare_notification_binding(original, binding, stores, hashes)
    assert original == frozen


@pytest.mark.parametrize("conflict", ["literal", "reference", "qsl-target", "qsl-reference", "risk", "store", "routing", "extra-key"])
def test_notification_binding_rejects_conflicts_and_unapproved_inputs(original, notification_inputs, conflict):
    binding, stores, hashes = notification_inputs
    entries = original["spec"]["containers"][0]["env"]
    if conflict == "literal":
        entries.append({"name": "DIGEST_TARGET_ID", "value": "existing-other-account"})
    elif conflict == "reference":
        entries.append({"name": "DIGEST_TARGET_ID", "valueFrom": {"secretKeyRef": {"name": "fixture-private", "key": "latest"}}})
    elif conflict == "qsl-target":
        entries.append({"name": "QSL_RUNTIME_TARGET_JSON", "value": json.dumps({"different": True})})
    elif conflict == "qsl-reference":
        entries.append({"name": "QSL_RUNTIME_TARGET_JSON", "valueFrom": {"secretKeyRef": {"name": "fixture-private", "key": "latest"}}})
    elif conflict == "risk":
        binding["RUNTIME_TARGET_JSON"] = {**binding["RUNTIME_TARGET_JSON"], "live_continuity": {"different": True}}
    elif conflict == "store":
        stores["DIGEST_CANDIDATES_GCS_URI"] = "gs://synthetic-unapproved/other"
    elif conflict == "routing":
        next(x for x in entries if x["name"] == "IBKR_UNIFIED_NOTIFICATION_ENABLED")["value"] = "true"
    else:
        binding["TELEGRAM_TOKEN"] = "must-never-be-consumed"
    frozen = copy.deepcopy(original)
    with pytest.raises(release.ReleaseError):
        release.prepare_notification_binding(original, binding, stores, hashes)
    assert original == frozen


@pytest.mark.parametrize("mutation", ["image", "risk", "ordinary", "reference", "metadata", "binding"])
def test_notification_binding_verifier_rejects_unrelated_or_identity_drift(original, notification_inputs, mutation):
    binding, stores, _ = notification_inputs
    updated, _, _ = release.prepare_notification_binding(original, *notification_inputs)
    values = {k: binding[k] for k in release.NOTIFICATION_IDENTITY_KEYS} | stores
    if mutation == "image":
        updated["spec"]["containers"][0]["image"] = IMAGE
    elif mutation == "risk":
        release.env_map(updated)["RUNTIME_TARGET_JSON"]["value"] = "{}"
    elif mutation == "ordinary":
        release.env_map(updated)["ORDINARY_SETTING"]["value"] = "changed"
    elif mutation == "reference":
        release.env_map(updated)["CREDENTIAL_REFERENCE"]["valueFrom"]["secretKeyRef"]["key"] = "new"
    elif mutation == "metadata":
        updated["metadata"]["annotations"]["run.googleapis.com/ingress"] = "all"
    else:
        release.env_map(updated)["DIGEST_TARGET_ID"]["value"] = "changed"
    with pytest.raises(release.ReleaseError):
        release.verify_notification_binding_update(original, updated, values)


def binding_prepare_env() -> dict[str, str]:
    return {**admitted_env(), "INPUT_NOTIFICATION_MARKER_MODE": "binding-prepare",
            "INPUT_NOTIFICATION_ONLY": "false", "INPUT_APPROVE_MARKER_PROBE": "false",
            "INPUT_SOURCE_COMMIT": release.COMBINED_SOURCE,
            "INPUT_EXPECTED_SERVING_REVISION_SHA256": "a" * 64,
            "INPUT_EXPECTED_FULL_TARGET_SHA256": "b" * 64,
            "INPUT_EXPECTED_SERVICE_SPEC_SHA256": "c" * 64}


@pytest.mark.parametrize("key,value", [
    ("INPUT_NOTIFICATION_MARKER_MODE", "disabled"), ("INPUT_NOTIFICATION_ONLY", "true"),
    ("INPUT_NOTIFICATION_READBACK_ONLY", "true"), ("INPUT_APPROVE_MARKER_PROBE", "true"),
    ("INPUT_DEPLOY_IMAGE", "true"), ("INPUT_SYNC_ENV", "true"),
    ("INPUT_APPROVE_TRAFFIC_SHIFT", "true"), ("INPUT_APPROVE_SCHEDULER_SYNC", "true"),
    ("INPUT_SOURCE_COMMIT", release.SOURCE),
    ("INPUT_EXPECTED_SERVING_REVISION_SHA256", ""), ("INPUT_EXPECTED_FULL_TARGET_SHA256", "x" * 64),
    ("INPUT_EXPECTED_SERVICE_SPEC_SHA256", ""),
    ("GITHUB_RUN_ATTEMPT", "2"), ("GCP_DEPLOY_SERVICE_ACCOUNT", "different"),
])
def test_binding_prepare_rejects_broader_or_stale_dispatch(key, value):
    env = binding_prepare_env()
    assert release.validate_binding_prepare_dispatch(env) == ("a" * 64, "b" * 64, "c" * 64)
    env[key] = value
    with pytest.raises(release.ReleaseError):
        release.validate_binding_prepare_dispatch(env)


def test_binding_prepare_selects_one_exact_target_and_no_global_fallback(original, notification_inputs):
    binding, _, _ = notification_inputs
    other = {**binding, "RUNTIME_TARGET_JSON": {"different": True}}
    assert release.selected_notification_binding(json.dumps([other, binding])) == binding
    with pytest.raises(release.ReleaseError, match="not_unique"):
        release.selected_notification_binding(json.dumps([binding, binding]))
    with pytest.raises(release.ReleaseError, match="not_unique"):
        release.selected_notification_binding(json.dumps([other]))


@pytest.fixture
def binding_serving_snapshot(service, original, notification_inputs, monkeypatch):
    binding, stores, hashes = notification_inputs
    monkeypatch.setattr(release, "NOTIFICATION_STORE_HASHES", hashes)
    monkeypatch.setattr(release, "SERVICE_HASH", release.sha(service["metadata"]["name"]))
    service["metadata"]["generation"] = 9
    service["status"] = {"observedGeneration": 9,
                         "traffic": [{"revisionName": original["metadata"]["name"], "percent": 100}]}
    service["spec"]["template"] = copy.deepcopy(original)
    for item in (original, service["spec"]["template"]):
        item["spec"]["containers"][0]["image"] = release.NOTIFICATION_SERVING_IMAGE
        item["metadata"]["labels"]["commit-sha"] = release.COMBINED_SOURCE
    original["status"] = {"conditions": [{"type": "Ready", "status": "True"}]}
    revision_sha = release.sha(original["metadata"]["name"])
    target_sha = release._digest(release.runtime_target(original))
    service_spec_sha = release._digest(service["spec"])
    return service, original, binding, stores, revision_sha, target_sha, service_spec_sha


def test_binding_prepare_read_only_snapshot_exact_diff_and_idempotence(binding_serving_snapshot):
    service, revision, binding, stores, revision_sha, target_sha, service_spec_sha = binding_serving_snapshot
    frozen_service, frozen_revision = copy.deepcopy(service), copy.deepcopy(revision)
    safe = release.prepare_binding_snapshot(service, revision, binding, stores, revision_sha, target_sha, service_spec_sha)
    assert safe["status"] == "PREPARED_READ_ONLY" and safe["write_operations"] == 0
    assert safe["service_reads"] == safe["revision_reads"] == 1 and "cloud_operations" not in safe
    assert len(safe["changed_fields"]) == 5
    assert safe["latest_follow_preserved"] and safe["current_traffic_percent"] == 100
    assert service == frozen_service and revision == frozen_revision
    assert "synthetic-uid" not in json.dumps(safe) and "gs://" not in json.dumps(safe)
    installed, rollback, _ = release.prepare_notification_binding(revision, binding, stores, release.NOTIFICATION_STORE_HASHES)
    service["spec"]["template"] = copy.deepcopy(installed)
    safe_again = release.prepare_binding_snapshot(service, installed, binding, stores, revision_sha, target_sha,
                                                  release._digest(service["spec"]))
    assert safe_again["changed_fields"] == []
    assert all(rollback[name] is None for name in release.NOTIFICATION_IDENTITY_KEYS + release.NOTIFICATION_STORE_KEYS)


@pytest.mark.parametrize("change", ["generation", "revision", "image", "source", "traffic", "target", "qsl", "service"])
def test_binding_prepare_rejects_changed_serving_baseline(binding_serving_snapshot, change):
    service, revision, binding, stores, revision_sha, target_sha, service_spec_sha = binding_serving_snapshot
    if change == "generation":
        service["status"]["observedGeneration"] = 8
    elif change == "revision":
        revision_sha = "f" * 64
    elif change == "image":
        service["spec"]["template"]["spec"]["containers"][0]["image"] = release.BASE_IMAGE
    elif change == "source":
        revision["metadata"]["labels"]["commit-sha"] = "f" * 40
    elif change == "traffic":
        service["spec"]["traffic"] = [{"revisionName": revision["metadata"]["name"], "percent": 100}]
    elif change == "target":
        target_sha = "f" * 64
    elif change == "service":
        service["spec"]["template"]["spec"]["timeoutSeconds"] = 999
    else:
        service["spec"]["template"]["spec"]["containers"][0]["env"].append(
            {"name": "QSL_RUNTIME_TARGET_JSON", "value": "{}"})
    with pytest.raises(release.ReleaseError):
        release.prepare_binding_snapshot(service, revision, binding, stores, revision_sha, target_sha,
                                         service_spec_sha)


@pytest.mark.parametrize("template_omits_ingress", [False, True])
def test_ingress_service_scope_accepts_equal_revision_and_unchanged_service(service, original, template_omits_ingress):
    expected = release.candidate(service, original, IMAGE, "12345")
    revision = copy.deepcopy(expected["spec"]["template"])
    after = copy.deepcopy(service)
    after["spec"] = copy.deepcopy(expected["spec"])
    if template_omits_ingress:
        after["spec"]["template"]["metadata"]["annotations"].pop("run.googleapis.com/ingress")
    release.verify_readback(service, original, after, revision, expected)
    assert release.nonnotification(revision) == release.nonnotification(original)
    assert after["metadata"]["annotations"]["run.googleapis.com/ingress"] == "internal"


@pytest.mark.parametrize("mutation", ["service-value", "service-missing", "both-service-values", "template-value", "revision-missing",
                                     "revision-value", "other-template-annotation", "unknown-original", "missing-original", "null-service"])
def test_ingress_scope_never_hides_real_or_unknown_changes(service, original, mutation):
    if mutation in {"unknown-original", "missing-original"}:
        annotations = original["metadata"]["annotations"]
        if mutation == "unknown-original":
            annotations["run.googleapis.com/ingress"] = "unknown-private-value"
            service["metadata"]["annotations"]["run.googleapis.com/ingress"] = "unknown-private-value"
        else:
            annotations.pop("run.googleapis.com/ingress")
    expected = release.candidate(service, original, IMAGE, "12345")
    revision = copy.deepcopy(expected["spec"]["template"])
    after = copy.deepcopy(service)
    after["spec"] = copy.deepcopy(expected["spec"])
    template = after["spec"]["template"]
    if mutation not in {"unknown-original", "missing-original", "null-service", "both-service-values"}:
        template["metadata"]["annotations"].pop("run.googleapis.com/ingress", None)
    if mutation == "service-value":
        after["metadata"]["annotations"]["run.googleapis.com/ingress"] = "all"
    elif mutation == "service-missing":
        after["metadata"]["annotations"].pop("run.googleapis.com/ingress")
    elif mutation == "both-service-values":
        service["metadata"]["annotations"]["run.googleapis.com/ingress"] = "all"
        after["metadata"]["annotations"]["run.googleapis.com/ingress"] = "all"
    elif mutation == "template-value":
        template["metadata"]["annotations"]["run.googleapis.com/ingress"] = "all"
    elif mutation == "revision-missing":
        revision["metadata"]["annotations"].pop("run.googleapis.com/ingress")
    elif mutation == "revision-value":
        revision["metadata"]["annotations"]["run.googleapis.com/ingress"] = "all"
    elif mutation == "other-template-annotation":
        template["metadata"]["annotations"].pop("run.googleapis.com/cpu-throttling")
    elif mutation == "null-service":
        service["metadata"]["annotations"]["run.googleapis.com/ingress"] = None
        after["metadata"]["annotations"]["run.googleapis.com/ingress"] = None
    with pytest.raises(release.ReleaseError):
        release.verify_readback(service, original, after, revision, expected)


@pytest.mark.parametrize("mutation", ["literal", "secret-version", "resources", "target", "image", "label", "traffic", "tag", "service-spec"])
def test_readback_rejects_nonnotification_or_routing_changes(service, original, mutation):
    expected = release.candidate(service, original, IMAGE, "12345")
    revision = copy.deepcopy(expected["spec"]["template"])
    after = copy.deepcopy(service)
    after["spec"] = copy.deepcopy(expected["spec"])
    container = revision["spec"]["containers"][0]
    if mutation == "literal":
        release.env_map(revision)["ORDINARY_SETTING"]["value"] = "changed"
    elif mutation == "secret-version":
        release.env_map(revision)["CREDENTIAL_REFERENCE"]["valueFrom"]["secretKeyRef"]["key"] = "8"
    elif mutation == "resources":
        container["resources"]["limits"]["cpu"] = "2"
    elif mutation == "target":
        release.env_map(revision)["RUNTIME_TARGET_JSON"]["value"] = "{}"
    elif mutation == "image":
        container["image"] = release.BASE_IMAGE
    elif mutation == "label":
        revision["metadata"]["labels"]["user-label"] = "changed"
    elif mutation == "traffic":
        after["status"]["traffic"][0]["revisionName"] = "another"
    elif mutation == "tag":
        after["status"]["traffic"][1]["tag"] = "changed"
    else:
        after["spec"]["unexpected"] = True
    with pytest.raises(release.ReleaseError):
        release.verify_readback(service, original, after, revision, expected)


@pytest.mark.parametrize("mutation", ["literal", "image", "labels", "routing", "latest-route"])
def test_readback_rejects_concurrent_desired_template_or_route_changes(service, original, mutation):
    expected = release.candidate(service, original, IMAGE, "12345")
    revision = copy.deepcopy(expected["spec"]["template"])
    after = copy.deepcopy(service)
    after["spec"] = copy.deepcopy(expected["spec"])
    desired = after["spec"]["template"]
    if mutation == "literal":
        release.env_map(desired)["ORDINARY_SETTING"]["value"] = "concurrent-change"
    elif mutation == "image":
        desired["spec"]["containers"][0]["image"] = release.BASE_IMAGE
    elif mutation == "labels":
        desired["metadata"]["labels"]["user-label"] = "concurrent-change"
    elif mutation == "routing":
        release.env_map(desired)["QSL_ROUTINE_NOTIFICATION_MODE"]["value"] = "digest"
    else:
        after["spec"]["traffic"] = [{"latestRevision": True, "percent": 100}]
    with pytest.raises(release.ReleaseError):
        release.verify_readback(service, original, after, revision, expected)


def test_zero_candidate_does_not_shift_routes_but_tags_stay_guarded(service):
    before = release.routes(service)
    service["status"]["traffic"].append({"revisionName": "fixture-candidate", "percent": 0})
    assert release.routes(service) == before
    assert any(route.get("tag") == "keep-tag" for route in before)


@pytest.mark.parametrize("mutation", ["service-min", "revision-min", "cpu", "memory", "always-cpu", "extra-container"])
def test_cost_guard_refuses_uncovered_runtime_shape(service, original, mutation):
    release.budget_guard(service, original)
    if mutation in {"service-min", "revision-min"}:
        selected = service if mutation == "service-min" else original
        selected["metadata"]["annotations"]["autoscaling.knative.dev/minScale"] = "1"
    elif mutation == "always-cpu":
        original["metadata"]["annotations"]["run.googleapis.com/cpu-throttling"] = "false"
    elif mutation == "extra-container":
        original["spec"]["containers"].append({})
    else:
        original["spec"]["containers"][0]["resources"]["limits"][mutation] = "2"
    with pytest.raises(release.ReleaseError):
        release.budget_guard(service, original)


@pytest.mark.parametrize("state", ["SCANNING_ACTIVE", "ENABLEMENT_STATE_UNSPECIFIED", "", "SCANNING_UNSUPPORTED"])
def test_unknown_or_extra_scan_cost_stops_before_build(monkeypatch, state):
    monkeypatch.setattr(release, "run", lambda *args, **kwargs: state.encode())
    with pytest.raises(release.ReleaseError, match="automatic_scan_cost_not_covered_or_unknown"):
        release.registry_cost_guard()


def test_disabled_scanning_cost_guard_is_metadata_only(monkeypatch):
    seen = []
    monkeypatch.setattr(release, "run", lambda args: seen.append(args) or b"SCANNING_DISABLED\n")
    release.registry_cost_guard()
    assert seen[0][1:4] == ["artifacts", "repositories", "describe"]
    assert "--format=value(vulnerabilityScanningConfig.enablementState)" in seen[0]






def test_workflow_bash_gate_matches_helper_and_excludes_normal_job(tmp_path):
    workflow = (ROOT / ".github/workflows/sync-cloud-run-env-protected.yml").read_text()
    block = workflow.split("      - name: Admit fixed notification dispatch\n", 1)[1].split("\n      - name:", 1)[0]
    script = "\n".join(line[10:] for line in block.split("        run: |\n", 1)[1].splitlines())
    env = dict(os.environ, **admitted_env())
    assert subprocess.run(["bash", "-c", script], env=env, cwd=tmp_path).returncode == 0
    for key in ("INPUT_SYNC_ENV", "INPUT_APPROVE_TRAFFIC_SHIFT", "INPUT_APPROVE_SCHEDULER_SYNC"):
        assert subprocess.run(["bash", "-c", script], env=dict(env, **{key: "true"}), cwd=tmp_path).returncode != 0
    assert "if: github.event_name == 'workflow_dispatch' && inputs.notification_only != true" in workflow
    assert "if: github.event_name == 'workflow_dispatch' && inputs.notification_only == true" in workflow
    job = workflow.split("  notification-only:\n", 1)[1].split("  sync-cloud-run-env:\n", 1)[0]
    assert job.index("Admit fixed notification dispatch") < job.index("google-github-actions/auth@")
    assert "CLOUD_RUN_SERVICE_TARGETS_JSON: ${{ secrets.CLOUD_RUN_SERVICE_TARGETS_JSON }}" in job
    assert "TELEGRAM_TOKEN" not in job
    assert "persist-credentials: false" in job






@pytest.mark.parametrize("status", [200, 403, 409, 500])
def test_direct_replace_keeps_original_cas_and_never_retries(monkeypatch, service, original, status):
    monkeypatch.setattr(release, "SERVICE_HASH", release.sha("fixture-service"))
    body = release.candidate(service, original, IMAGE, "12345")
    calls = []

    def put(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=status)

    session = SimpleNamespace(put=put)
    if status == 200:
        release.replace_service(body, session=session)
    else:
        with pytest.raises(release.ReleaseError, match=f"cloud_run_replace_http_{status}_no_retry"):
            release.replace_service(body, session=session)
    assert len(calls) == 1
    assert calls[0][0] == "https://us-central1-run.googleapis.com/apis/serving.knative.dev/v1/namespaces/interactivebrokersquant/services/fixture-service"
    assert calls[0][1]["json"]["metadata"]["resourceVersion"] == "opaque-token"
    assert calls[0][1]["allow_redirects"] is False
    assert calls[0][1]["timeout"] == 60


def response(status, payload):
    return SimpleNamespace(status_code=status, json=lambda: payload)


@pytest.mark.parametrize("http,sdk,code,allowed", [
    (404, "NOT_FOUND", 404, True),
    (403, "PERMISSION_DENIED", 403, False),
    (403, "NOT_FOUND", 404, False),
    (404, "PERMISSION_DENIED", 403, False),
    (404, "NOT_FOUND", 403, False),
    (404, None, 404, False),
    (404, ["NOT_FOUND"], 404, False),
    (500, "INTERNAL", 500, False),
    (401, "UNAUTHENTICATED", 401, False),
    (302, None, None, False),
    (None, None, None, False),
    ("404", "NOT_FOUND", 404, False),
])
def test_only_structured_NOT_FOUND_establishes_absence(http, sdk, code, allowed):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return response(http, {"error": {"status": sdk, "code": code, "message": "Cannot find revision [fixture-candidate]; NOT_FOUND fixture-private-text"}})

    report = {}
    if allowed:
        assert release.read_revision("fixture-candidate", report, phase="candidate_absence", session=SimpleNamespace(get=get), allow_missing=True) == {}
        assert report["revision_queries"][0]["decision"] == "not_found"
    else:
        with pytest.raises(release.ReleaseError):
            release.read_revision("fixture-candidate", report, phase="candidate_absence", session=SimpleNamespace(get=get), allow_missing=True)
        assert report["revision_queries"][0]["decision"] == "stop"
    assert len(calls) == 1
    assert calls[0][0] == "https://us-central1-run.googleapis.com/apis/serving.knative.dev/v1/namespaces/interactivebrokersquant/revisions/fixture-candidate"
    assert calls[0][1] == {"timeout": 60, "allow_redirects": False}
    assert "fixture-private-text" not in json.dumps(report)
    assert "fixture-candidate" not in json.dumps(report)


def test_original_revision_NOT_FOUND_cannot_be_accepted():
    report = {}
    session = SimpleNamespace(get=lambda *a, **k: response(404, {"error": {"code": 404, "status": "NOT_FOUND"}}))
    with pytest.raises(release.ReleaseError, match="revision_get_http_404_NOT_FOUND_no_retry"):
        release.read_revision("fixture-original", report, phase="original", session=session)
    assert report["revision_queries"][0]["http_status"] == 404
    assert report["revision_queries"][0]["sdk_status"] == "NOT_FOUND"


@pytest.mark.parametrize("failure", ["transport", "invalid-json", "message-only", "wrong-success-name"])
def test_revision_unknowns_stop_without_message_guess_or_retry(failure):
    calls = []

    def get(*args, **kwargs):
        calls.append(1)
        if failure == "transport":
            raise TimeoutError("fixture-private-text HTTP404 NOT_FOUND")
        if failure == "invalid-json":
            def bad_json():
                raise ValueError("Cannot find revision [fixture-candidate]")
            return SimpleNamespace(status_code=404, json=bad_json)
        if failure == "message-only":
            return response(404, {"message": "Cannot find revision [fixture-candidate] NOT_FOUND"})
        return response(200, {"metadata": {"name": "another-revision"}, "spec": {}})

    report = {}
    with pytest.raises(release.ReleaseError) as error:
        release.read_revision("fixture-candidate", report, phase="candidate_absence", session=SimpleNamespace(get=get), allow_missing=True)
    assert len(calls) == 1
    assert report["revision_queries"][0]["decision"] == "stop"
    assert "fixture-private-text" not in str(error.value) + json.dumps(report)


@pytest.mark.parametrize("stderr", [
    b"ERROR: (gcloud.run.revisions.describe) Cannot find revision [fixture-candidate]",
    b"NOT_FOUND: fixture-private-text", b"HTTP 404", b"403: NOT_FOUND",
])
def test_official_CLI_message_is_never_HTTP_or_permission_evidence(monkeypatch, stderr):
    calls = []

    def command(*args, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, 1, b"fixture-private-stdout", stderr)

    monkeypatch.setattr(release.subprocess, "run", command)
    with pytest.raises(release.ReleaseError, match="^native_command_exit_1_no_retry$"):
        release.run(["gcloud", "run", "revisions", "describe", "fixture-candidate"])
    assert len(calls) == 1
    assert calls[0]["env"]["CLOUDSDK_CORE_DISABLE_FILE_LOGGING"] == "true"


def test_source_still_requires_frozen_overlay_and_all_pins_without_copy(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "safe.py").write_bytes(b"fixture\n")
    lock = ('[[package]]\nname = "us-equity-strategies"\nsource = { git = "https://example.invalid/repo#' + release.PACKAGES["us_equity_strategies-0.7.60"] + '" }\n').encode()
    (source / "uv.lock").write_bytes(lock)
    monkeypatch.setattr(release, "SOURCE_FILES", {"safe.py": release.sha(b"fixture\n")})
    monkeypatch.setattr(release, "SOURCE_LOCK_HASH", release.sha(lock))
    monkeypatch.setattr(release, "run", lambda *a, **k: release.SOURCE.encode())
    release.verify_source(source)
    assert sorted(path.name for path in source.iterdir()) == ["safe.py", "uv.lock"]
    (source / "safe.py").write_bytes(b"tampered")
    with pytest.raises(release.ReleaseError, match="source_file_mismatch"):
        release.verify_source(source)
    (source / "safe.py").write_bytes(b"fixture\n")
    (source / "uv.lock").write_bytes(lock + b"# changed\n")
    with pytest.raises(release.ReleaseError, match="source_lock_changed"):
        release.verify_source(source)


@pytest.mark.parametrize("failure", [None, "registry-mismatch", "permission", "unknown", "original-permission", "candidate-exists"])
def test_reuse_only_release_never_builds_or_pushes_and_denial_never_writes(monkeypatch, tmp_path, service, original, failure):
    for key, value in admitted_env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("CLOUD_RUN_SERVICE_TARGETS_JSON", json.dumps([{"service_name": "fixture-service"}]))
    monkeypatch.setattr(release, "SERVICE_HASH", release.sha("fixture-service"))
    monkeypatch.setattr(release, "REVISION_HASH", release.sha("fixture-serving"))
    monkeypatch.setattr(release, "QUALIFIED_BASELINE", release._digest(release.nonnotification(original)))
    monkeypatch.setattr(release, "verify_source", lambda *a: None)
    monkeypatch.setattr(release, "revision_from_lockfile", lambda *a: release.PACKAGES["us_equity_strategies-0.7.60"])
    commands, gets, puts, closed = [], [], [], []

    def command(args, **kwargs):
        commands.append(args)
        assert args[0] != "docker"
        if args[1:4] == ["artifacts", "repositories", "describe"]:
            return b"SCANNING_DISABLED"
        assert args[1:5] == ["artifacts", "docker", "images", "describe"]
        assert args[5] == release.QUALIFIED_IMAGE
        return (("sha256:" + "b" * 64) if failure == "registry-mismatch" else release.QUALIFIED_DIGEST).encode()

    def get(url, **kwargs):
        gets.append(url)
        name = url.rsplit("/", 1)[1]
        if name == "fixture-serving":
            if failure == "original-permission":
                return response(403, {"error": {"code": 403, "status": "PERMISSION_DENIED"}})
            return response(200, copy.deepcopy(original))
        if not puts:
            if failure == "permission":
                return response(403, {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "Cannot find revision [fixture]"}})
            if failure == "unknown":
                return response(404, {"message": "Cannot find revision [fixture]"})
            if failure == "candidate-exists":
                return response(200, {"metadata": {"name": name}, "spec": {}})
            return response(404, {"error": {"code": 404, "status": "NOT_FOUND"}})
        result = copy.deepcopy(puts[0]["spec"]["template"])
        result["status"] = {"conditions": [{"type": "Ready", "status": "True"}]}
        return response(200, result)

    def put(url, **kwargs):
        puts.append(copy.deepcopy(kwargs["json"]))
        return response(200, {})

    def cloud(args):
        assert args[:3] == ["run", "services", "describe"]
        result = copy.deepcopy(service)
        if puts:
            result["spec"] = copy.deepcopy(puts[0]["spec"])
        return result

    session = SimpleNamespace(get=get, put=put, close=lambda: closed.append(1))
    monkeypatch.setattr(release, "authorized_session", lambda: session)
    monkeypatch.setattr(release, "run", command)
    monkeypatch.setattr(release, "cloud", cloud)
    report = {}
    if failure:
        with pytest.raises(release.ReleaseError):
            release.release(tmp_path / "source", report)
        assert not puts
        assert len(gets) == (1 if failure in {"registry-mismatch", "original-permission"} else 2)
    else:
        release.release(tmp_path / "source", report)
        assert report["status"] == "PASS_NO_TRAFFIC"
        assert len(puts) == 1
        assert puts[0]["spec"]["traffic"] == release.routes(service)
        assert puts[0]["spec"]["template"]["spec"]["containers"][0]["image"] == release.QUALIFIED_IMAGE
        assert [item["http_status"] for item in report["revision_queries"]] == [200, 404, 200]
        assert report["image_qualification_run"] == 39512119214
        assert report["build_commands"] == report["push_commands"] == 0
    assert closed == [1]
    assert all(args[0] == "gcloud" for args in commands)
    assert "fixture-secret" not in json.dumps(report)


def test_HTTP_diagnostics_are_available_in_safe_log_without_summary_UI(monkeypatch, tmp_path, capsys):
    report_file = tmp_path / "result.json"
    monkeypatch.setattr(release.sys, "argv", ["deploy", "--source", str(tmp_path), "--report", str(report_file), "--execute"])

    def stop(source, report):
        report["revision_queries"].append({"phase": "candidate_absence", "http_status": 403, "sdk_code": 403,
                                            "sdk_status": "PERMISSION_DENIED", "decision": "stop", "revision_sha256": "a" * 64})
        raise release.ReleaseError("revision_get_http_403_PERMISSION_DENIED_no_retry")

    monkeypatch.setattr(release, "release", stop)
    assert release.main() == 1
    output = capsys.readouterr().out
    line = next(line for line in output.splitlines() if line.startswith("NOTIFICATION_RESULT_JSON="))
    emitted = json.loads(line.split("=", 1)[1])
    assert emitted == json.loads(report_file.read_text())
    assert emitted["revision_queries"][0]["http_status"] == 403
    assert emitted["revision_queries"][0]["sdk_status"] == "PERMISSION_DENIED"
    assert emitted["revision_create_attempts"] == 0


def test_projection_keeps_runtime_and_secret_material_private():
    before = {"spec": {"containers": [{"env": [{"name": "PRIVATE_ACCOUNT_MARKER", "value": "PRIVATE_VALUE_MARKER"}],
                                        "resources": {"limits": {"cpu": "1", "memory": "512Mi"}}}]},
              "metadata": {"labels": {"PRIVATE_KEY_MARKER": "PRIVATE_LABEL_MARKER"}}}
    after = copy.deepcopy(before)
    after["spec"]["containers"][0]["env"][0]["value"] = "NEW_PRIVATE_VALUE_MARKER"
    after["metadata"]["labels"]["PRIVATE_KEY_MARKER"] = "NEW_PRIVATE_LABEL_MARKER"
    after["spec"]["containers"][0]["resources"]["limits"]["cpu"] = "1000m"
    projection = release.difference_projection(before, after)
    output = json.dumps(projection)
    assert "PRIVATE_" not in output
    assert any(d.get("approved_resource_alias_equivalent") is True for d in projection)
    assert "resource_literal" not in output and '"1000m"' not in output
    assert any(d["path"].endswith("/env/0/value") for d in projection)
    assert release.difference_projection({"spec": {"timeoutSeconds": 1}}, {"spec": {"timeoutSeconds": True}}) == [
        {"path": "/spec/timeoutSeconds", "change": "changed", "before_type": "int", "after_type": "bool"}]


@pytest.mark.parametrize("namespace", ["run.googleapis.com", "autoscaling.knative.dev", "client.knative.dev"])
def test_projection_never_discloses_unknown_provider_keys(namespace):
    before = {"metadata": {"annotations": {namespace + "/PRIVATE_ACCOUNT_KEY": "PRIVATE_VALUE"}}}
    after = {"metadata": {"annotations": {namespace + "/PRIVATE_ACCOUNT_KEY": "PRIVATE_NEW_VALUE"}}}
    result = json.dumps(release.difference_projection(before, after))
    assert "PRIVATE" not in result
    assert "unlisted-key" in result


@pytest.mark.parametrize("failure", [None, "permission", "missing", "unknown", "transport", "not-ready", "resource-change", "ingress-change"])
def test_fixed_readback_is_three_gets_only_and_never_writes(monkeypatch, tmp_path, service, original, failure):
    for key, value in admitted_env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("INPUT_NOTIFICATION_READBACK_ONLY", "true")
    monkeypatch.setenv("CLOUD_RUN_SERVICE_TARGETS_JSON", json.dumps([{"service_name": "fixture-service"}]))
    monkeypatch.setattr(release, "SERVICE_HASH", release.sha("fixture-service"))
    monkeypatch.setattr(release, "REVISION_HASH", release.sha("fixture-serving"))
    monkeypatch.setattr(release, "QUALIFIED_BASELINE", release._digest(release.nonnotification(original)))
    monkeypatch.setattr(release, "verify_source", lambda *a: None)
    candidate = release.candidate(service, original, release.QUALIFIED_IMAGE, release.READBACK_RUN)["spec"]["template"]
    monkeypatch.setattr(release, "READBACK_REVISION_HASH", release.sha(candidate["metadata"]["name"]))
    if failure == "resource-change":
        candidate["spec"]["containers"][0]["resources"]["limits"]["cpu"] = "1000m"
    candidate["status"] = {"conditions": [{"type": "Ready", "status": "Unknown" if failure == "not-ready" else "True"}]}
    service["spec"]["template"] = copy.deepcopy(candidate)
    service["spec"]["template"]["metadata"]["annotations"].pop("run.googleapis.com/ingress")
    service["spec"]["traffic"] = release.routes(service)
    if failure == "ingress-change":
        service["metadata"]["annotations"]["run.googleapis.com/ingress"] = "all"
    service_reads, gets, closed = [], [], []

    def cloud(args):
        assert args == ["run", "services", "describe", "fixture-service"]
        service_reads.append(args)
        return copy.deepcopy(service)

    def get(url, **kwargs):
        assert kwargs == {"timeout": 60, "allow_redirects": False}
        gets.append(url)
        if len(gets) == 1:
            body, status = original, 200
            assert url == release.RUN_API + "/revisions/fixture-serving"
        else:
            assert url == release.RUN_API + "/revisions/" + candidate["metadata"]["name"]
            if failure == "transport":
                raise OSError("PRIVATE_MESSAGE")
            if failure == "permission":
                body, status = {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "PRIVATE_MESSAGE"}}, 403
            elif failure == "missing":
                body, status = {"error": {"code": 404, "status": "NOT_FOUND", "message": "PRIVATE_MESSAGE"}}, 404
            elif failure == "unknown":
                body, status = {}, None
            else:
                body, status = candidate, 200
        return SimpleNamespace(status_code=status, json=lambda: copy.deepcopy(body))

    def forbidden(*args, **kwargs):
        raise AssertionError("readback must never execute writes, registry, image or Secret Manager commands")

    monkeypatch.setattr(release, "cloud", cloud)
    monkeypatch.setattr(release, "authorized_session", lambda: SimpleNamespace(get=get, close=lambda: closed.append(True)))
    for name in ["run", "replace_service", "registry_cost_guard", "release"]:
        monkeypatch.setattr(release, name, forbidden)
    report = {"revision_queries": [], "revision_create_attempts": 0}
    if failure is not None:
        with pytest.raises(release.ReleaseError):
            release.readback_only(tmp_path, report)
        if failure in {"permission", "missing", "unknown", "transport"}:
            assert "projection" not in report
        assert report.get("status") != "READBACK_ONLY_VERIFIED_NO_TRAFFIC"
    else:
        release.readback_only(tmp_path, report)
        assert report["status"] == "READBACK_ONLY_VERIFIED_NO_TRAFFIC"
        assert report["write_operations"] == 0
        assert report["candidate_ready"] and report["original_still_receives_100_percent"]
        assert report["projection"]["candidate_revision"]["difference_count"] == 0
        assert report["projection"]["service_template"]["difference_count"] == 1
        assert report["service_ingress_matches_qualified_baseline"]
        assert report["revision_nonnotification_unchanged"] and report["service_template_scope_unchanged"]
        assert report["routing_literals"] == release.ROUTING
        assert report["projection"]["candidate_revision"]["target_entry_unchanged"]
        assert report["projection"]["candidate_revision"]["routing_literals_match"]
    assert len(service_reads) == 1 and len(gets) == 2 and closed == [True]
    assert report["revision_create_attempts"] == 0
    assert all(s not in json.dumps(report) for s in ["fixture-secret", "PRIVATE_MESSAGE", "fixture-preserved", "retain-without-reset"])


def test_readback_selector_cannot_enter_release(monkeypatch, tmp_path):
    monkeypatch.setenv("INPUT_NOTIFICATION_READBACK_ONLY", "true")
    monkeypatch.setattr(release, "cloud", lambda *a: pytest.fail("must stop before cloud"))
    with pytest.raises(release.ReleaseError, match="readback_cannot_execute_release"):
        release.release(tmp_path, {})


def test_readback_input_cannot_fall_into_broad_deploy_job():
    protected = (ROOT / ".github/workflows/sync-cloud-run-env-protected.yml").read_text()
    wrapper = (ROOT / ".github/workflows/sync-cloud-run-env.yml").read_text()
    assert "inputs.notification_only != true && inputs.notification_readback_only != true" in protected
    assert "notification_readback_only: ${{ inputs.notification_readback_only }}" in wrapper
    assert "INPUT_NOTIFICATION_READBACK_ONLY: ${{ inputs.notification_readback_only }}" in protected
    assert 'mode=--readback-only; else mode=--execute' in protected


@pytest.mark.parametrize("readback", ["false", "true"])
def test_combined_build_dispatch_is_exact_and_cannot_be_readback(monkeypatch, readback):
    monkeypatch.setattr(release, "COMBINED_SOURCE", "c" * 40)
    env = dict(admitted_env(), INPUT_SOURCE_COMMIT="c" * 40,
               INPUT_DEPLOY_IMAGE="true", INPUT_NOTIFICATION_READBACK_ONLY=readback)
    if readback == "true":
        with pytest.raises(release.ReleaseError, match="combined_build_cannot_readback"):
            release.validate_dispatch(env)
    else:
        assert release.validate_dispatch(env) == "12345"
        for key, value in (("INPUT_SOURCE_COMMIT", release.SOURCE),
                           ("GITHUB_RUN_ATTEMPT", "2"),
                           ("INPUT_APPROVE_TRAFFIC_SHIFT", "true")):
            with pytest.raises(release.ReleaseError):
                release.validate_dispatch(dict(env, **{key: value}))


def test_combined_context_copies_only_reviewed_files_and_never_executes(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "safe.py").write_bytes(b"reviewed\n")
    (source / "not_in_overlay.py").write_text("raise RuntimeError('not for image')")
    monkeypatch.setattr(release, "COMBINED_FILES", {"safe.py": release.sha(b"reviewed\n")})
    context = tmp_path / "context"
    release.build_context(source, context)
    assert sorted(str(p.relative_to(context)) for p in context.rglob("*") if p.is_file()) == ["Dockerfile", "overlay/safe.py"]
    assert (context / "Dockerfile").read_text() == f"FROM {release.BASE_IMAGE}\nCOPY --chown=1000:1000 overlay/ /app/\n"
    with pytest.raises(release.ReleaseError, match="context_already_exists"):
        release.build_context(source, context)


@pytest.mark.parametrize("failure", [None, "pull", "build", "pins", "push", "digest"])
def test_combined_build_one_attempt_and_no_application_entrypoint(tmp_path, monkeypatch, failure):
    commands = []
    digest = "sha256:" + "d" * 64
    tag = release.REPOSITORY + ":notification-c" + "c" * 39 + "-12345"
    monkeypatch.setattr(release, "build_context", lambda *_: None)
    monkeypatch.setattr(release, "COMBINED_SOURCE", "c" * 40)

    def command(args, **kwargs):
        commands.append(args)
        assert args[:2] not in (["docker", "run"], ["docker", "start"], ["docker", "exec"])
        phase = args[1] if args[0] == "docker" else None
        if failure and phase == failure:
            raise release.ReleaseError("fixture_failure")
        if phase == "push":
            return (tag.rsplit(":", 1)[1] + ": digest: " + digest + " size: 1234\n").encode()
        if args[:5] == ["gcloud", "artifacts", "docker", "images", "describe"]:
            return ("sha256:" + "e" * 64 if failure == "digest" else digest).encode()
        return b""

    def verify(*args):
        if failure == "pins":
            raise release.ReleaseError("installed_package_commit_changed")
        return dict(release.PACKAGES)

    monkeypatch.setattr(release, "run", command)
    monkeypatch.setattr(release, "verify_built_image", verify)
    report = {"build_commands": 0, "push_commands": 0, "image_pull_commands": 0}
    if failure:
        with pytest.raises(release.ReleaseError):
            release.build_image(tmp_path, "12345", report)
    else:
        assert release.build_image(tmp_path, "12345", report) == release.REPOSITORY + "@" + digest
        assert report["image_digest"] == digest
        assert report["installed_commits"] == release.PACKAGES
        assert report["build_commands"] == report["push_commands"] == report["image_pull_commands"] == 1
    for phase in ("pull", "build", "push"):
        assert sum(command[:2] == ["docker", phase] for command in commands) <= 1
    assert all("--network=none" in command for command in commands if command[:2] == ["docker", "build"])
    if failure in {"pull", "build", "pins"}:
        assert not any(command[:2] == ["docker", "push"] for command in commands)


@pytest.mark.parametrize("failure", [None, "permission", "unknown", "original-permission", "candidate-exists", "concurrent-service", "risk-binding"])
def test_combined_release_verifies_before_build_and_keeps_original_routes(monkeypatch, tmp_path, service, original, failure):
    env = dict(admitted_env(), INPUT_DEPLOY_IMAGE="true", INPUT_SOURCE_COMMIT="c" * 40)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("CLOUD_RUN_SERVICE_TARGETS_JSON", json.dumps([{"service_name": "fixture-service"}]))
    monkeypatch.setattr(release, "COMBINED_SOURCE", "c" * 40)
    monkeypatch.setattr(release, "COMBINED_TREE", "b" * 40)
    monkeypatch.setattr(release, "SERVICE_HASH", release.sha("fixture-service"))
    monkeypatch.setattr(release, "REVISION_HASH", release.sha("fixture-serving"))
    monkeypatch.setattr(release, "QUALIFIED_BASELINE", release._digest(release.nonnotification(original)))
    monkeypatch.setattr(release, "verify_source", lambda *a, **kw: None)
    monkeypatch.setattr(release, "revision_from_lockfile", lambda *_: release.PACKAGES["us_equity_strategies-0.7.60"])
    if failure == "risk-binding":
        monkeypatch.setattr(release, "check", lambda *a, **kw: True)
    gets, puts, builds, closes = [], [], [], []

    def get(url, **kwargs):
        gets.append(url)
        name = url.rsplit("/", 1)[1]
        if name == "fixture-serving":
            if failure == "original-permission":
                return response(403, {"error": {"code": 403, "status": "PERMISSION_DENIED"}})
            return response(200, copy.deepcopy(original))
        if not puts:
            if failure == "permission":
                return response(403, {"error": {"code": 403, "status": "PERMISSION_DENIED"}})
            if failure == "unknown":
                return response(404, {"message": "Cannot find revision [fixture]"})
            if failure == "candidate-exists":
                return response(200, {"metadata": {"name": name}, "spec": {}})
            return response(404, {"error": {"code": 404, "status": "NOT_FOUND"}})
        result = copy.deepcopy(puts[0]["spec"]["template"])
        result["status"] = {"conditions": [{"type": "Ready", "status": "True"}]}
        return response(200, result)

    def put(url, **kwargs):
        puts.append(copy.deepcopy(kwargs["json"]))
        return response(200, {})

    def cloud(args):
        result = copy.deepcopy(service)
        if puts:
            result["spec"] = copy.deepcopy(puts[0]["spec"])
        elif builds and failure == "concurrent-service":
            result["metadata"]["resourceVersion"] = "new-token"
        return result

    def build(source, run_id, report):
        builds.append(run_id)
        report.update(build_commands=1, push_commands=1, image_pull_commands=1, image_digest=IMAGE.rsplit("@", 1)[1])
        return IMAGE

    monkeypatch.setattr(release, "authorized_session", lambda: SimpleNamespace(get=get, put=put, close=lambda: closes.append(1)))
    monkeypatch.setattr(release, "cloud", cloud)
    monkeypatch.setattr(release, "registry_cost_guard", lambda: None)
    monkeypatch.setattr(release, "build_image", build)
    monkeypatch.setattr(release, "run", lambda *a, **kw: pytest.fail("unexpected native command"))
    report = {}
    if failure:
        with pytest.raises(release.ReleaseError):
            release.release(tmp_path, report)
        assert not puts
        assert builds == (["12345"] if failure == "concurrent-service" else [])
    else:
        release.release(tmp_path, report)
        assert builds == ["12345"] and len(puts) == 1
        assert report["status"] == "PASS_NO_TRAFFIC"
        assert report["source"] == "c" * 40 and report["source_tree"] == "b" * 40
        candidate = puts[0]["spec"]["template"]
        assert candidate["metadata"]["labels"]["commit-sha"] == "c" * 40
        assert candidate["spec"]["containers"][0]["image"] == IMAGE
        assert release.nonnotification(candidate) == release.nonnotification(original)
        assert puts[0]["spec"]["traffic"] == release.routes(service)
        assert [q["http_status"] for q in report["revision_queries"]] == [200, 404, 404, 200]
    assert closes == [1]
    assert "fixture-secret" not in json.dumps(report)


@pytest.mark.parametrize("failure", [None, "sha", "tree", "content", "lock", "unfrozen"])
def test_combined_source_is_full_sha_tree_twenty_hashes_and_original_lock(tmp_path, monkeypatch, failure):
    source = tmp_path / "source"
    source.mkdir()
    contents = {f"module_{index}.py": f"# fixture{index}\n".encode() for index in range(20)}
    for relative, content in contents.items():
        (source / relative).write_bytes(content)
    lock = ('[[package]]\nname = "us-equity-strategies"\nsource = { git = "https://example.invalid/repo#' + release.PACKAGES["us_equity_strategies-0.7.60"] + '" }\n').encode()
    (source / "uv.lock").write_bytes(lock)
    monkeypatch.setattr(release, "COMBINED_SOURCE", "c" * 40)
    monkeypatch.setattr(release, "COMBINED_TREE", "b" * 40 if failure != "unfrozen" else "")
    monkeypatch.setattr(release, "COMBINED_FILES", {p: release.sha(v) for p, v in contents.items()})
    monkeypatch.setattr(release, "SOURCE_LOCK_HASH", release.sha(lock))
    commands = []

    def command(args, **kwargs):
        commands.append(args)
        assert args[:4] == ["git", "-C", str(source), "rev-parse"]
        assert args[-1] in {"HEAD", "HEAD^{tree}"}
        return (("d" * 40 if failure == "sha" else "c" * 40) if args[-1] == "HEAD" else ("e" * 40 if failure == "tree" else "b" * 40)).encode()

    monkeypatch.setattr(release, "run", command)
    if failure == "content":
        (source / "module_19.py").write_text("# not reviewed")
    if failure == "lock":
        (source / "uv.lock").write_bytes(lock + b"# changed")
    if failure:
        with pytest.raises(release.ReleaseError):
            release.verify_source(source, combined=True)
    else:
        release.verify_source(source, combined=True)
        assert len(commands) == 2
    assert not (tmp_path / "notification-context-12345").exists()


@pytest.mark.parametrize("mutation", [None, "old-source", "readback", "traffic"])
def test_reviewed_combined_workflow_gate_matches_exact_helper(tmp_path, mutation):
    workflow = (ROOT / ".github/workflows/sync-cloud-run-env-protected.yml").read_text()
    block = workflow.split("      - name: Admit fixed notification dispatch\n", 1)[1].split("\n      - name:", 1)[0]
    script = "\n".join(line[10:] for line in block.split("        run: |\n", 1)[1].splitlines())
    env = dict(os.environ, **admitted_env())
    env.update(INPUT_DEPLOY_IMAGE="true", INPUT_SOURCE_COMMIT=release.COMBINED_SOURCE)
    if mutation == "old-source":
        env["INPUT_SOURCE_COMMIT"] = release.SOURCE
    if mutation == "readback":
        env["INPUT_NOTIFICATION_READBACK_ONLY"] = "true"
    if mutation == "traffic":
        env["INPUT_APPROVE_TRAFFIC_SHIFT"] = "true"
    code = subprocess.run(["bash", "-c", script], env=env, cwd=tmp_path).returncode
    if mutation:
        assert code != 0
        with pytest.raises(release.ReleaseError):
            release.validate_dispatch(env)
    else:
        assert code == 0
        assert release.validate_dispatch(env) == "12345"
