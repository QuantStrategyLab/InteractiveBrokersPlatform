from copy import deepcopy
import json
from unittest.mock import Mock

import pytest

from scripts import account_facts_serving_revision as serving

EXPECTED = dict(expected_service_name="synthetic-service", expected_account_scope="synthetic-scope",
                expected_account_selector=("U00000001",), expected_deployment_selector="synthetic-deployment")
REVISION = "synthetic-service-00001-test"


def metadata():
    target = dict(platform_id="interactive_brokers", service_name="synthetic-service",
                  account_scope="synthetic-scope", account_selector=["U00000001"],
                  deployment_selector="synthetic-deployment")
    service = {"metadata": {"name": "synthetic-service"}, "status": {
        "latestReadyRevisionName": "synthetic-service-00002-candidate",
        "traffic": [{"revisionName": REVISION, "percent": 100}]}}
    revision = {"metadata": {"name": REVISION}, "spec": {"containers": [{"env": [
        {"name": "RUNTIME_TARGET_JSON", "value": json.dumps(target)}]}]}}
    return service, revision


def test_uses_actual_serving_revision_and_preserves_input(capsys):
    service, revision = metadata()
    original = deepcopy(EXPECTED)
    read = Mock(side_effect=[service, revision])
    assert serving.resolve(EXPECTED, {}, read) == REVISION
    assert read.call_args_list[1].args[0] == ["run", "revisions", "describe", REVISION]
    assert EXPECTED == original and capsys.readouterr().out == ""


@pytest.mark.parametrize("field,value", [("account_selector", ["U00000002"]),
    ("account_scope", "other-scope"), ("deployment_selector", "other-deployment"),
    ("service_name", "other-service"), ("platform_id", "other-platform")])
def test_other_account_identity_is_never_adopted(field, value):
    service, revision = metadata()
    row = revision["spec"]["containers"][0]["env"][0]
    target = json.loads(row["value"])
    target[field] = value
    row["value"] = json.dumps(target)
    with pytest.raises(serving.ServingRevisionError, match="serving_revision_identity_mismatch"):
        serving.resolve(EXPECTED, {}, Mock(side_effect=[service, revision]))


def test_split_traffic_and_duplicate_target_metadata_fail_closed():
    service, revision = metadata()
    service["status"]["traffic"].append({"revisionName": "synthetic-service-00002-candidate", "percent": 1})
    with pytest.raises(serving.ServingRevisionError, match="serving_revision_ambiguous"):
        serving.resolve(EXPECTED, {}, Mock(return_value=service))
    service, revision = metadata()
    rows = revision["spec"]["containers"][0]["env"]
    rows.append(rows[0])
    with pytest.raises(serving.ServingRevisionError, match="serving_revision_metadata_invalid"):
        serving.resolve(EXPECTED, {}, Mock(side_effect=[service, revision]))


def test_reader_uses_explicit_account_without_disclosing_provider_output(monkeypatch):
    run = Mock(return_value=Mock(returncode=1, stdout="private-metadata", stderr="private-error"))
    monkeypatch.setattr(serving.subprocess, "run", run)
    with pytest.raises(serving.ServingRevisionError, match="^serving_revision_metadata_unavailable$"):
        serving.read_metadata(["run", "services", "describe", "synthetic-service"], {
            "GCP_WORKLOAD_IDENTITY_SERVICE_ACCOUNT": "synthetic@example.invalid",
            "CLOUD_RUN_REGION": "synthetic-region", "IBKR_ACCOUNT_FACTS_PROJECT_ID": "synthetic-project"})
    assert "--account=synthetic@example.invalid" in run.call_args.args[0]
