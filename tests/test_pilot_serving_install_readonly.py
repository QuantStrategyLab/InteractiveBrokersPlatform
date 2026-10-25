"""One scoped metadata read, using synthetic subprocess results only."""
import ast
import copy
import json
import subprocess
from pathlib import Path

import pytest

from scripts import diagnose_pilot_serving_install as reader
from scripts import deploy_pilot_no_traffic as pilot
from scripts import diagnose_serving_image_identity as native
from tests.test_pilot_no_traffic_release import COMMIT, SERVICE, SERVING, TARGET, environment

INSTALLED = "b" * 40
IMAGE = "us-central1-docker.pkg.dev/synthetic-project/runtime/app@sha256:" + "c" * 64
PRIVATE = "synthetic-private-value"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def chain(monkeypatch):
    env = environment()
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    ctx = pilot.context(env)
    service = {"metadata": {"name": SERVICE}, "status": {"traffic": [{"revisionName": SERVING, "percent": 100}],
               "latestReadyRevisionName": SERVICE + "-unused", "latestCreatedRevisionName": SERVICE + "-unused"}}
    target = copy.deepcopy(TARGET)
    target["strategy_release"]["strategy_revision"] = INSTALLED
    revision = {"metadata": {"name": SERVING}, "spec": {"containers": [{"image": IMAGE,
                "env": [{"name": "RUNTIME_TARGET_JSON", "value": json.dumps(target)}]}]},
                "status": {"imageDigest": IMAGE}}
    probe = {"schema_version": 1, "ues_stamp": INSTALLED, "packages": {
        "ues": {"present": True, "version": "1.0.0", "commit_id": INSTALLED},
        "qpk": {"present": True, "version": "1.0.0", "commit_id": "d" * 40},
        "plugin": {"present": True, "version": "1.0.0", "commit_id": "e" * 40}}}
    state = {"ctx": ctx, "services": [service, copy.deepcopy(service)],
             "revisions": [revision, copy.deepcopy(revision)], "probe": probe,
             "inspected": [{"RepoDigests": [IMAGE], "Config": {}}], "calls": [], "failure": None}
    service_reads, revision_reads = [], []
    def run(args, **kwargs):
        args = tuple(args)
        state["calls"].append((args, kwargs))
        assert kwargs["capture_output"] is True and kwargs["check"] is False
        if state["failure"] and args[:len(state["failure"])] == state["failure"]:
            return subprocess.CompletedProcess(args, 1, PRIVATE, PRIVATE)
        if args[0] == "git":
            payload = COMMIT if args[-1] == "HEAD" else ""
        elif args[:4] == ("gcloud", "run", "services", "describe"):
            assert args[4] == SERVICE
            payload = json.dumps(state["services"][len(service_reads)])
            service_reads.append(True)
        elif args[:4] == ("gcloud", "run", "revisions", "describe"):
            payload = json.dumps(state["revisions"][len(revision_reads)])
            revision_reads.append(True)
        elif args[:3] == ("gcloud", "auth", "configure-docker"):
            assert args[3] == "us-central1-docker.pkg.dev"
            assert kwargs["env"]["DOCKER_CONFIG"]
            payload = ""
        elif args[:2] == ("docker", "pull"):
            assert args[2] == IMAGE
            payload = ""
        elif args[:3] == ("docker", "image", "inspect"):
            assert args[3] == IMAGE
            payload = json.dumps(state["inspected"])
        else:
            assert args[:2] == ("docker", "run")
            payload = json.dumps(state["probe"])
        return subprocess.CompletedProcess(args, 0, payload, PRIVATE)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(reader, "revision_from_lockfile", lambda _: INSTALLED)
    state["execute"] = lambda: reader.diagnose(ctx, Path("synthetic-source"))
    return state


def test_one_serving_image_read_has_separate_nullable_bindings_and_full_isolation(chain, capsys):
    out = chain["execute"]()
    assert out["status"] == "observed" and out["ues_identity_verified"] is True
    assert out["source_stable"] is True and out["immutable_image_verified"] is True
    assert out["equalities"]["installed_ues_equals_actual_serving_release"] is True
    assert out["equalities"]["installed_ues_equals_deployment_inventory_release"] is False
    assert out["equalities"]["installed_ues_equals_source_lock"] is True
    assert out["equalities"]["installed_ues_equals_actual_risk_binding"] is None
    assert out["cloud_write_attempted"] is False and out["broker_called"] is False
    docker = next(args for args, _ in chain["calls"] if args[:2] == ("docker", "run"))
    assert docker[-1] == native.IMAGE_PROBE
    for flag in ("--network=none", "--read-only", "--user=1000:1000", "--cap-drop=ALL",
                 "--security-opt=no-new-privileges", "--entrypoint=/usr/local/bin/python", "-I", "-S"):
        assert flag in docker
    assert not any(arg in docker for arg in ("-e", "--env", "--mount", "-v", "--volume", "--env-file"))
    assert sum(args[:2] == ("docker", "pull") for args, _ in chain["calls"]) == 1
    assert all("list" not in args for args, _ in chain["calls"])
    assert capsys.readouterr() == ("", "")
    text = json.dumps(out)
    for value in (PRIVATE, IMAGE, SERVICE, SERVING, INSTALLED, COMMIT, "1.0.0", "DU00000001"):
        assert value not in text


def test_plugin_missing_remains_partial_without_hiding_verified_ues(chain):
    chain["probe"]["packages"]["plugin"] = {"present": False, "version": None, "commit_id": None}
    out = chain["execute"]()
    assert out["status"] == "partial" and out["dependencies"]["plugin"]["status"] == "missing"
    assert out["ues_identity_verified"] is True
    assert out["equalities"]["installed_ues_equals_actual_serving_release"] is True


def test_stamp_conflict_is_unavailable_even_when_package_commit_is_observed(chain):
    chain["probe"]["ues_stamp"] = "f" * 40
    out = chain["execute"]()
    assert out["status"] == "unavailable" and out["reason"] == "ues_stamp_conflict"
    assert out["ues_identity_verified"] is False
    assert out["equalities"]["ues_stamp_equals_installed_commit"] is False


@pytest.mark.parametrize("traffic", [[], [{"revisionName": SERVING, "percent": 0}],
    [{"revisionName": SERVING, "percent": 99}], [{"revisionName": SERVING, "percent": True}],
    [{"revisionName": SERVING, "percent": 50}, {"revisionName": SERVICE + "-other", "percent": 50}]])
def test_no_fallback_from_zero_or_ambiguous_traffic_to_ready_candidate(chain, traffic):
    chain["services"][0]["status"]["traffic"] = traffic
    with pytest.raises(pilot.ReleaseError):
        chain["execute"]()
    assert not any(args[0] == "docker" for args, _ in chain["calls"])


@pytest.mark.parametrize("field", pilot.IDENTITY_FIELDS)
def test_any_actual_account_or_deployment_identity_mismatch_blocks_pull(chain, field):
    target = json.loads(chain["revisions"][0]["spec"]["containers"][0]["env"][0]["value"])
    target[field] = ["DU00000002"] if field == "account_selector" else PRIVATE
    chain["revisions"][0]["spec"]["containers"][0]["env"][0]["value"] = json.dumps(target)
    with pytest.raises(pilot.ReleaseError, match="inventory_serving_mismatch"):
        chain["execute"]()
    assert not any(args[0] == "docker" for args, _ in chain["calls"])


@pytest.mark.parametrize("change", ["traffic", "account", "image", "release"])
def test_final_source_stability_is_required(chain, change):
    if change == "traffic":
        chain["services"][1]["status"]["traffic"] = [{"revisionName": SERVICE + "-other", "percent": 100}]
    elif change == "image":
        chain["revisions"][1]["spec"]["containers"][0]["image"] = IMAGE.replace("c" * 64, "f" * 64)
        chain["revisions"][1]["status"]["imageDigest"] = IMAGE.replace("c" * 64, "f" * 64)
    else:
        target = json.loads(chain["revisions"][1]["spec"]["containers"][0]["env"][0]["value"])
        if change == "account":
            target["account_selector"] = ["DU00000002"]
        else:
            target["strategy_release"]["strategy_revision"] = "f" * 40
        chain["revisions"][1]["spec"]["containers"][0]["env"][0]["value"] = json.dumps(target)
    with pytest.raises((pilot.ReleaseError, native._DiagnosticError)):
        chain["execute"]()


@pytest.mark.parametrize("args", [("gcloud", "run", "services", "update"), ("gcloud", "secrets", "versions", "access"),
    ("gcloud", "run", "services", "get-iam-policy"), ("gcloud", "scheduler", "jobs", "list"),
    ("docker", "build"), ("docker", "push")])
def test_write_and_unrelated_read_commands_are_rejected_before_execution(monkeypatch, args):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("forbidden command escaped allowlist"))
    with pytest.raises(pilot.ReleaseError, match="operation_not_readonly"):
        reader.command(args, "diagnostic_error")


@pytest.mark.parametrize("failure", [("gcloud", "run", "services", "describe"), ("gcloud", "run", "revisions", "describe"),
    ("docker", "pull"), ("docker", "run")])
def test_failed_real_source_operations_emit_only_fixed_safe_receipt(chain, failure, tmp_path, capsys):
    chain["failure"] = failure
    path = tmp_path / "safe.json"
    assert reader.main(["--source-dir", "synthetic-source", "--receipt", str(path)]) == 1
    output = capsys.readouterr()
    out = json.loads(output.out)
    assert out["status"] == "unavailable" and out["ues_identity_verified"] is False
    assert all(value is None for value in out["equalities"].values())
    assert path.stat().st_mode & 0o777 == 0o600 and output.err == ""
    for value in (PRIVATE, IMAGE, SERVICE, SERVING, INSTALLED, COMMIT, "DU00000001"):
        assert value not in output.out + path.read_text()


def test_partial_main_result_retains_metadata_and_failing_exit_status(chain, tmp_path, capsys):
    chain["probe"]["packages"]["plugin"] = {"present": True, "version": "1.0.0", "commit_id": None}
    path = tmp_path / "safe.json"
    assert reader.main(["--source-dir", "synthetic-source", "--receipt", str(path)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "partial" and out["dependencies"]["plugin"]["status"] == "partial"
    assert out["ues_identity_verified"] is True and out["source_stable"] is True


def test_missing_source_lock_stays_nullable_without_hiding_ues(chain, monkeypatch):
    monkeypatch.setattr(reader, "revision_from_lockfile", lambda _: (_ for _ in ()).throw(ValueError(PRIVATE)))
    out = chain["execute"]()
    assert out["source_lock_status"] == "unavailable"
    assert out["equalities"]["installed_ues_equals_source_lock"] is None
    assert out["ues_identity_verified"] is True and PRIVATE not in json.dumps(out)


def test_dirty_or_wrong_source_fails_before_cloud_reads(chain, monkeypatch):
    monkeypatch.setattr(pilot, "command", lambda *a, **k: "f" * 40)
    with pytest.raises(pilot.ReleaseError, match="source_commit_unverified"):
        chain["execute"]()
    assert chain["calls"] == []


def test_image_declared_volumes_cannot_make_probe_writable(chain):
    chain["inspected"][0]["Config"]["Volumes"] = {"/synthetic": {}}
    with pytest.raises(pilot.ReleaseError, match="image_volumes_not_supported"):
        chain["execute"]()
    assert not any(args[:2] == ("docker", "run") for args, _ in chain["calls"])


def test_preflight_and_bad_source_are_network_free(monkeypatch, tmp_path, capsys):
    for key, value in environment().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("preflight cannot call outside the process"))
    path = tmp_path / "safe.json"
    assert reader.main(["--preflight", "--receipt", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "preflight"
    monkeypatch.setenv("PILOT_SOURCE_COMMIT", "f" * 40)
    assert reader.main(["--preflight", "--receipt", str(path)]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "dispatch_not_admitted"


def test_probe_is_original_isolated_stdlib_metadata_code():
    tree = ast.parse(native.IMAGE_PROBE)
    names = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    names |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert names == {"json", "pathlib", "re", "importlib"}


def test_workflow_uses_single_protected_target_original_identity_and_deployment_lock():
    wrapper = (ROOT / ".github/workflows/diagnose-pilot-serving-install.yml").read_text()
    callee = (ROOT / ".github/workflows/diagnose-pilot-serving-install-protected.yml").read_text()
    assert "group: Deploy Cloud Run-${{ github.ref_name }}" in wrapper
    assert "matrix:" not in wrapper + callee and "vars." not in callee
    for text in (wrapper, callee):
        assert "github.ref == 'refs/heads/main' && github.run_attempt == 1" in text
        assert "github.repository == 'QuantStrategyLab/InteractiveBrokersPlatform'" in text
    assert "workload_identity_provider: ${{ env.PILOT_WIF_PROVIDER }}" in callee
    assert "projects/303" not in wrapper + callee
    assert callee.index("--preflight") < callee.index("google-github-actions/auth")
    assert "--source-dir source" in callee and "path: source" in callee
    assert callee.count("actions/upload-artifact") == 1
    assert "path: ${{ runner.temp }}/pilot-serving-install.safe.json" in callee
    for forbidden in ("docker build", "docker push", "refresh_artifact_registry_keep", "sync_env", "approve_traffic", "approve_scheduler", "secrets versions access"):
        assert forbidden not in callee
