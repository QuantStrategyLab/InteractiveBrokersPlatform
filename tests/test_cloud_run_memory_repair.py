import copy
import json

import pytest

from scripts.repair_cloud_run_memory import PROFILE, repair

PATH = "projects/example/locations/example/services/example-service"
OLD = "example-service-old"
NEW = "example-service-new"


def simulator(*, drift=False):
    target = {"execution_mode": "live", "dry_run_only": False, "strategy_profile": PROFILE,
              "service_name": "example-service"}
    container = {"image": "example.invalid/image@sha256:" + "1" * 64,
                 "env": [{"name": "RUNTIME_TARGET_ENABLED", "value": "true"},
                         {"name": "RUNTIME_TARGET_JSON", "value": json.dumps(target)}],
                 "resources": {"limits": {"memory": "512Mi", "cpu": "1"}}}
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
