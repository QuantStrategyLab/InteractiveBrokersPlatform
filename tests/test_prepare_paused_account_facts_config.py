from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from scripts.prepare_paused_account_facts_config import (
    ADOPT_SCHEMA,
    PROBE_SCHEMA,
    PreparationError,
    assemble_configs,
)

NOW = datetime(2026, 10, 2, 21, 40, tzinfo=timezone.utc)
OLD_SHA = "1" * 40
CANDIDATE_SHA = "2" * 40
IMAGE_DIGEST = "a" * 64
BASE = "https://service-abc-uc.a.run.app"
SERVICE = "qsl-runner"
SERVICE_RESOURCE = "projects/test-project-123/locations/us-central1/services/qsl-runner"
OLD_REV = "qsl-runner-old123"
CANDIDATE_REV = "qsl-runner-candidate456"


def fixture_data():
    jobs_spec = {
        "main": {"name": "runner-main", "schedule": "*/5 * * * *", "method": "POST", "path": "/run", "deadline": "900s"},
        "precheck": {"name": "runner-precheck", "schedule": "*/5 * * * *", "method": "POST", "path": "/dry-run", "deadline": "900s"},
        "warmup": {"name": "runner-warmup", "schedule": "0 8 * * 1-5", "method": "GET", "path": "/health", "deadline": "300s"},
    }
    context = {
        "project": "test-project-123", "region": "us-central1", "service": SERVICE,
        "service_resource": SERVICE_RESOURCE, "service_base": BASE,
        "production_revision": CANDIDATE_REV, "old_revision": OLD_REV,
        "old_source": OLD_SHA, "allowed_candidate_source": CANDIDATE_SHA,
        "oidc": "runner@test-project-123.iam.gserviceaccount.com",
        "report_prefix": "gs://private-reports/reports", "expected_job": jobs_spec,
    }
    binding = {
        "account_scope": "synthetic-scope", "account_selector": ["synthetic-account"],
        "deployment_selector": "synthetic-deployment", "project_id": "test-project-123",
        "runtime_revision": CANDIDATE_REV, "service_name": SERVICE,
    }
    binding["source_binding_id"] = hashlib.sha256(
        json.dumps(binding, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    anchor = {
        "request_started_at_utc": (NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "request_finished_at_utc": (NOW - timedelta(hours=1) + timedelta(seconds=5)).isoformat().replace("+00:00", "Z"),
        "request_url": BASE + "/run", "revision": OLD_REV,
        "report_identity_matches_binding": True, "orders_submitted_count": 0,
        "receipt_outcome": "no_action", "private_evidence_ref": "gs://private-reports/reports/old-report.json",
    }
    inputs = {
        "target_context": context, "image_digest": IMAGE_DIGEST, "account_binding": binding,
        "paused_prior_run": anchor, "selected_slot": "additional-1",
        "old_target": {key: binding[key] for key in (
            "project_id", "service_name", "account_scope", "account_selector", "deployment_selector",
        )},
        "live_jobs": {"placeholder": True},
    }
    service = {
        "name": SERVICE_RESOURCE,
        "terminalCondition": {"state": "CONDITION_SUCCEEDED"},
        "trafficStatuses": [
            {"revision": SERVICE_RESOURCE + "/revisions/" + OLD_REV, "percent": 100},
        ],
        "traffic": [
            {"revision": SERVICE_RESOURCE + "/revisions/" + OLD_REV, "percent": 100},
        ],
    }
    def revision(name, sha, image):
        return {
            "name": SERVICE_RESOURCE + "/revisions/" + name,
            "labels": {"commit-sha": sha},
            "conditions": [{"type": "Ready", "state": "CONDITION_SUCCEEDED"}],
            "containers": [{"image": image, "env": [
                {"name": "RUNTIME_TARGET_ENABLED", "value": "false"},
                {"name": "RUNTIME_TARGET_JSON", "value": json.dumps({
                    "account_selector": ["synthetic-account"], "account_scope": "synthetic-scope",
                    "deployment_selector": "synthetic-deployment",
                })},
            ]}],
        }
    revisions = [revision(OLD_REV, OLD_SHA, "registry.example/runner:old"), revision(CANDIDATE_REV, CANDIDATE_SHA, "registry.example/runner@sha256:" + IMAGE_DIGEST)]
    job_responses = {}
    for role, spec in jobs_spec.items():
        target = {
            "uri": BASE + spec["path"], "httpMethod": spec["method"],
            "oidcToken": {"serviceAccountEmail": context["oidc"], "audience": BASE},
        }
        job = {
            "name": f"projects/{context['project']}/locations/{context['region']}/jobs/{spec['name']}",
            "state": "PAUSED", "schedule": spec["schedule"], "timeZone": "America/New_York",
            "attemptDeadline": spec["deadline"], "httpTarget": target,
        }
        job_responses[role] = job
    mapping = {
        "status": "mapping_verified", "runtime_ordinal": 0, "gateway_inventory_index": 4,
        "native_account_matches_protected": True, "current_vm_host_matches_actual_runtime_config": True,
        "mode_matches": True, "vm_running": True,
        "observed_at_utc": (NOW - timedelta(seconds=15)).isoformat(), "mutations": 0,
    }
    passive = {
        "status": "observed", "container_running": True, "api_listener": True,
        "established_api_connections": 0, "observed_at_utc": (NOW - timedelta(seconds=10)).isoformat(),
        "account_authentication": "not_verified", "pending_orders": "not_verified",
        "broker_calls": 0, "production_mutations": 0,
    }
    args = [inputs, service, *revisions, job_responses, [], mapping, passive, "36927000001", 4, NOW, "b" * 64, "c" * 64, True]
    return args


def test_assembles_exact_existing_configs_from_complete_current_evidence():
    adopter, probe = assemble_configs(*fixture_data(), pagination_complete=True)
    assert set(adopter) == {
        "schema", "driver_sha256", "target_context", "source_sha", "image_digest",
        "account_binding", "confirm_no_other_writers", "window_proof",
    }
    assert adopter["schema"] == ADOPT_SCHEMA
    assert set(probe) == {
        "schema", "driver_sha256", "target_context", "image_digest",
        "account_binding", "window_proof", "confirm_no_other_writers",
    }
    assert probe["schema"] == PROBE_SCHEMA
    assert adopter["source_sha"] == CANDIDATE_SHA
    assert adopter["window_proof"]["paused_prior_run"] == fixture_data()[0]["paused_prior_run"]
    assert adopter["window_proof"]["evidence_id"] == "36927000001"
    assert adopter["window_proof"]["window_start_utc"] == NOW.isoformat().replace("+00:00", "Z")
    assert adopter["window_proof"]["window_end_utc"] == (NOW + timedelta(seconds=600)).isoformat().replace("+00:00", "Z")
    assert adopter["window_proof"]["gateway_observed_at_utc"].endswith("Z")
    assert adopter["confirm_no_other_writers"] is True
    assert "verified" not in json.dumps(probe["window_proof"])


def test_zero_candidate_traffic_may_omit_percent():
    args = fixture_data()
    zero_row = {"revision": SERVICE_RESOURCE + "/revisions/" + CANDIDATE_REV}
    args[1]["trafficStatuses"].append(copy.deepcopy(zero_row))
    args[1]["traffic"].append(copy.deepcopy(zero_row))
    assemble_configs(*args, pagination_complete=True)


@pytest.mark.parametrize(
    ("mutate", "category"),
    [
        (lambda a: a[6].clear(), "gateway_mapping_invalid"),
        (lambda a: a[6].update(native_account_matches_protected=False), "gateway_mapping_invalid"),
        (lambda a: a[6].update(current_vm_host_matches_actual_runtime_config=False), "gateway_mapping_invalid"),
        (lambda a: a[6].update(mode_matches=False), "gateway_mapping_invalid"),
        (lambda a: a[6].update(observed_at_utc=(NOW + timedelta(seconds=1)).isoformat()), "gateway_evidence_stale"),
        (lambda a: a.__setitem__(9, 3), "gateway_mapping_invalid"),
        (lambda a: a[0].__setitem__("selected_slot", "primary"), "target_input_invalid"),
        (lambda a: a[0]["account_binding"].__setitem__("account_selector", ["wrong-account"]), "account_binding_invalid"),
        (lambda a: a[0].__setitem__("image_digest", "f" * 64), "candidate_digest_invalid"),
        (lambda a: a[1]["traffic"].__setitem__(0, {"revision": SERVICE_RESOURCE + "/revisions/" + OLD_REV, "percent": 0}), "service_traffic_invalid"),
        (lambda a: a[1]["trafficStatuses"][0].__setitem__("percent", None), "service_traffic_invalid"),
        (lambda a: a[1]["trafficStatuses"][0].__setitem__("percent", True), "service_traffic_invalid"),
        (lambda a: a[1]["trafficStatuses"][0].__setitem__("percent", "100"), "service_traffic_invalid"),
        (lambda a: a[1]["trafficStatuses"].append({"revision": SERVICE_RESOURCE + "/revisions/" + CANDIDATE_REV, "percent": 100}), "service_traffic_invalid"),
        (lambda a: a[2]["labels"].__setitem__("commit-sha", "f" * 40), "revision_source_invalid"),
        (lambda a: a[3]["containers"][0]["env"][1].__setitem__("value", json.dumps({"account_selector": ["wrong"], "account_scope": "synthetic-scope", "deployment_selector": "synthetic-deployment"})), "runtime_target_identity_invalid"),
        (lambda a: a[3]["containers"][0].__setitem__("image", "registry.example/runner@sha256:" + "d" * 64), "candidate_digest_invalid"),
        (lambda a: a[4]["main"].__setitem__("state", "ENABLED"), "job_preflight_failed"),
        (lambda a: a[5].append({"request": "placeholder"}), "recent_requests_not_clear"),
        (lambda a: a[7].update(established_api_connections=1), "gateway_observation_invalid"),
        (lambda a: a[7].update(broker_calls=True), "gateway_observation_invalid"),
        (lambda a: a.__setitem__(13, False), "sole_operator_not_confirmed"),
        (lambda a: a.__setitem__(10, "not-a-datetime"), "time_invalid"),
        (lambda a: a[0]["paused_prior_run"].__setitem__("orders_submitted_count", True), "paused_anchor_invalid"),
    ],
)
def test_fails_closed_on_invalid_or_incomplete_evidence(mutate, category):
    args = fixture_data()
    mutate(args)
    with pytest.raises(PreparationError) as exc:
        assemble_configs(*args, pagination_complete=True)
    assert exc.value.category == category
    assert str(exc.value) == category


def test_mapping_index_and_actual_source_run_id_are_required():
    args = fixture_data()
    args[8] = ""
    with pytest.raises(PreparationError, match="gateway_evidence_invalid"):
        assemble_configs(*args, pagination_complete=True)
    args = fixture_data()
    args[9] = 6
    with pytest.raises(PreparationError, match="gateway_mapping_invalid"):
        assemble_configs(*args, pagination_complete=True)


def test_rejects_stale_gateway_evidence_and_nonempty_or_incomplete_pages():
    args = fixture_data()
    args[7]["observed_at_utc"] = (NOW - timedelta(seconds=121)).isoformat()
    with pytest.raises(PreparationError, match="gateway_evidence_stale"):
        assemble_configs(*args, pagination_complete=True)
    for requests, complete in [([{}], True), ([], False)]:
        args = fixture_data()
        args[5] = requests
        with pytest.raises(PreparationError, match="recent_requests_not_clear"):
            if complete:
                assemble_configs(*args, pagination_complete=complete)
            else:
                assemble_configs(*args)


def test_output_is_detached_from_inputs():
    args = fixture_data()
    original = copy.deepcopy(args[0])
    adopter, probe = assemble_configs(*args, pagination_complete=True)
    adopter["target_context"]["service"] = "changed"
    probe["window_proof"]["paused_prior_run"]["revision"] = "changed"
    assert args[0] == original


def test_mapping_can_be_observed_after_the_passive_observation():
    args = fixture_data()
    args[6]["observed_at_utc"] = (NOW - timedelta(seconds=5)).isoformat()
    adopter, _ = assemble_configs(*args, pagination_complete=True)
    assert adopter["window_proof"]["gateway_observed_at_utc"] == (NOW - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
