import copy
import json

import pytest

from scripts.maintain_live_cloud_run import PRIMARY_PROFILE, PROFILE, repair

PATH = "projects/example/locations/example/services/example-service"
OLD = "example-service-old"
NEW = "example-service-new"


def simulator(*, drift=False, order_digest=False):
    target = {"execution_mode": "live", "dry_run_only": False, "strategy_profile": PRIMARY_PROFILE if order_digest else PROFILE,
              "service_name": "example-service"}
    container = {"image": "example.invalid/image@sha256:" + "1" * 64,
                 "env": [{"name": "RUNTIME_TARGET_ENABLED", "value": "true"},
                         {"name": "RUNTIME_TARGET_JSON", "value": json.dumps(target)}],
                 "resources": {"limits": {"memory": "512Mi", "cpu": "1"}}}
    if order_digest:
        route_env = {"IBKR_UNIFIED_NOTIFICATION_ENABLED": "true", "QSL_ROUTINE_NOTIFICATION_MODE": "daily_digest",
                     "QSL_DIGEST_ROUTINE_ORDERS": "false", "DIGEST_CANDIDATES_GCS_URI": "gs://test/digest",
                     "DIGEST_STORE_TARGET": "test-slot", "DIGEST_TARGET_ID": "test-target",
                     "DIGEST_OPAQUE_ACCOUNT_UID": "test-account", "IBKR_NOTIFICATION_MARKER_GCS_URI": "gs://test/marker"}
        container["env"].extend({"name": key, "value": value} for key, value in route_env.items())
    template = {"containers": [container], "serviceAccount": "example"}
    route = {"type": "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION", "percent": 100, "revision": OLD}
    service = {"template": template, "traffic": [route], "trafficStatuses": [{"revision": OLD, "percent": 100}],
               "etag": "initial", "ingress": "INGRESS_TRAFFIC_INTERNAL_ONLY"}
    old = copy.deepcopy(template)
    old["containers"][0]["name"] = "generated-container-name"
    writes = []

    def request(path, method="GET", payload=None):
        if method == "PATCH":
            writes.append((path, copy.deepcopy(payload)))
            assert payload["etag"] == service["etag"]
            if path.endswith("updateMask=template"):
                service.update(template=payload["template"], etag="staged", latestCreatedRevision=NEW)
            if path.endswith("updateMask=traffic"):
                service.update(traffic=payload["traffic"], trafficStatuses=[{"revision": NEW, "percent": 100}])
            return {"done": True}
        if path == PATH:
            return copy.deepcopy(service)
        if path.endswith("/" + OLD):
            return copy.deepcopy(old)
        assert path.endswith("/" + NEW)
        revision = copy.deepcopy(service["template"])
        revision["conditions"] = [{"type": "Ready", "state": "CONDITION_SUCCEEDED"}]
        if drift:
            revision["containers"][0]["env"][0]["value"] = "false"
        return revision

    return request, writes


def test_memory_repair_changes_only_the_verified_memory_limit():
    request, writes = simulator()
    repair(request, PATH, OLD, apply=True)
    assert len(writes) == 3
    old_container = request(PATH + "/revisions/" + OLD)["containers"][0]
    old_container.pop("name")
    old_container["resources"]["limits"]["memory"] = "1Gi"
    assert writes[1][1]["template"]["containers"] == [old_container]
    assert writes[2][1]["traffic"][0]["revision"] == NEW


def test_dry_run_only_validates_the_cloud_run_request():
    request, writes = simulator()
    repair(request, PATH, OLD)
    assert len(writes) == 1
    assert "validateOnly=true" in writes[0][0]


def test_another_serving_revision_cannot_be_repaired_as_the_incident():
    request, writes = simulator()
    with pytest.raises(ValueError, match="incident_revision_no_longer_serving"):
        repair(request, PATH, NEW, apply=True)
    assert writes == []


def test_candidate_configuration_drift_stops_before_changing_traffic():
    request, writes = simulator(drift=True)
    with pytest.raises(ValueError, match="revision_configuration_mismatch"):
        repair(request, PATH, OLD, apply=True)
    assert len(writes) == 2


def test_primary_order_routing_changes_only_its_existing_digest_switch():
    request, writes = simulator(order_digest=True)
    repair(request, PATH, order_digest=True, apply=True)
    before = request(PATH + "/revisions/" + OLD)["containers"][0]
    before.pop("name")
    entry = next(row for row in before["env"] if row["name"] == "QSL_DIGEST_ROUTINE_ORDERS")
    assert entry["value"] == "false"
    entry["value"] = "true"
    assert writes[1][1]["template"]["containers"] == [before]


def test_compatible_account_copies_only_its_existing_digest_binding():
    from scripts.maintain_live_cloud_run import DIGEST_BINDINGS, DIGEST_ROUTE, activate_compatible_digest

    target = {"strategy_profile": "global_etf_rotation", "account_scope": "example"}
    bindings = {key: "test-" + key.lower() for key in DIGEST_BINDINGS}
    env = {"RUNTIME_TARGET_ENABLED": {"name": "RUNTIME_TARGET_ENABLED", "value": "true"}}
    container = {"env": list(env.values())}

    def job(account, values):
        job_env = {**values, "RUNTIME_TARGET_JSON": json.dumps(account), "QSL_ROUTINE_NOTIFICATION_MODE": "daily_digest"}
        return {"template": {"template": {"containers": [{"env": [
                    {"name": key, "value": value} for key, value in job_env.items()
                ]}]}}, "terminalCondition": {"state": "CONDITION_SUCCEEDED"}}

    jobs = [job({**target, "account_scope": "other"}, bindings), job(target, bindings)]

    def request(path):
        assert path.endswith("/jobs?pageSize=100")
        return {"jobs": jobs}

    activate_compatible_digest(request, PATH, target, container, env)
    assert {row["name"]: row["value"] for row in container["env"]} == {
        "RUNTIME_TARGET_ENABLED": "true", **bindings, **DIGEST_ROUTE,
    }

    with pytest.raises(ValueError, match="unique_same_account_digest_job_missing"):
        activate_compatible_digest(lambda _: {"jobs": [jobs[1], jobs[1]]}, PATH, target, {"env": []}, {})


def test_legacy_route_requires_the_same_archive_and_changes_no_trading_fields(monkeypatch):
    from scripts import maintain_live_cloud_run as release

    archive = "gs://example/execution"
    env = {
        "EXECUTION_REPORT_GCS_URI": {"name": "EXECUTION_REPORT_GCS_URI", "value": archive},
        "RUNTIME_TARGET_ENABLED": {"name": "RUNTIME_TARGET_ENABLED", "value": "true"},
    }
    container = {"image": "old-image", "env": list(env.values())}
    target = {"account_scope": "example"}
    checked = []

    def check(_request, path, actual, *, archive_prefix):
        checked.append((path, actual, archive_prefix))
        return {}

    monkeypatch.setattr(release, "notification_bindings", check)
    monkeypatch.setattr(release, "build_verified_image", lambda value: "new-image" if value == "old-image" else None)
    release.activate_legacy_digest(None, PATH, target, container, env, apply=True)
    assert checked == [(PATH, target, archive)]
    assert container["image"] == "new-image"
    assert {entry["name"]: entry["value"] for entry in container["env"]} == {
        "EXECUTION_REPORT_GCS_URI": archive, "RUNTIME_TARGET_ENABLED": "true",
        "QSL_ROUTINE_NOTIFICATION_MODE": "daily_digest", "QSL_DIGEST_ROUTINE_ORDERS": "true",
    }


def test_russell_reuses_serving_settings_and_keeps_the_prior_image_revision():
    import copy
    from scripts.maintain_live_cloud_run import LEGACY_DIGEST_PROFILES, release_template

    active = {"containers": [{"image": "old-image", "env": [{"name": "RUNTIME_TARGET_ENABLED", "value": "true"}]}]}
    staged = copy.deepcopy(active)
    staged["containers"][0]["image"] = "other-stage-image"
    staged["labels"] = {"stage": "other"}
    service = {"template": staged, "latestCreatedRevision": NEW}
    prior_path = PATH + "/revisions/" + NEW

    def read(path):
        assert path == prior_path
        return {**staged, "uid": "prior-stage"}

    template, retained = release_template(
        read, PATH, service, active, PATH + "/revisions/" + OLD, LEGACY_DIGEST_PROFILES["russell"],
    )
    assert template == active
    assert retained == (prior_path, "prior-stage")
    assert service["template"]["containers"][0]["image"] == "other-stage-image"

    staged["containers"][0]["env"][0]["value"] = "false"
    with pytest.raises(ValueError, match="revision_configuration_mismatch"):
        release_template(read, PATH, service, active, PATH + "/revisions/" + OLD, LEGACY_DIGEST_PROFILES["russell"])
