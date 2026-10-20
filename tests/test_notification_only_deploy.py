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
        "INPUT_SOURCE_COMMIT": release.SOURCE, "INPUT_DEPLOY_IMAGE": "true",
        "INPUT_SYNC_ENV": "false", "INPUT_APPROVE_TRAFFIC_SHIFT": "false",
        "INPUT_APPROVE_SCHEDULER_SYNC": "false", "GCP_PROJECT_ID": release.PROJECT,
        "GCP_DEPLOY_SERVICE_ACCOUNT": release.DEPLOYER,
        "GCP_RUNTIME_SERVICE_ACCOUNT": release.RUNTIME,
    }


@pytest.mark.parametrize("key,value", [
    ("GITHUB_REF", "refs/heads/candidate"), ("GITHUB_EVENT_NAME", "push"),
    ("GITHUB_RUN_ATTEMPT", "2"), ("ENABLE_GITHUB_CLOUD_RUN_DEPLOY", "false"),
    ("INPUT_SOURCE_COMMIT", "b" * 40), ("INPUT_TARGET", "hk-verify"),
    ("INPUT_CONFIGURED_SERVICE", "another-service"), ("INPUT_DEPLOY_IMAGE", "false"),
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
                         "annotations": {"autoscaling.knative.dev/minScale": "0", "run.googleapis.com/cpu-throttling": "true"}},
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


def test_context_contains_only_fixed_overlay_and_rejects_tamper(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "safe.py").write_bytes(b"fixture\n")
    (source / "ignored.py").write_bytes(b"not-in-overlay\n")
    monkeypatch.setattr(release, "SOURCE_FILES", {"safe.py": release.sha(b"fixture\n")})
    monkeypatch.setattr(release, "run", lambda *args, **kwargs: release.SOURCE.encode())
    destination = tmp_path / "context"
    release.context(source, destination)
    assert sorted(str(path.relative_to(destination)) for path in destination.rglob("*") if path.is_file()) == ["Dockerfile", "overlay/safe.py"]
    assert (destination / "Dockerfile").read_text() == f"FROM {release.BASE_IMAGE}\nCOPY --chown=1000:1000 overlay/ /app/\n"
    (source / "safe.py").write_bytes(b"tampered")
    with pytest.raises(release.ReleaseError, match="source_file_mismatch"):
        release.context(source, tmp_path / "tampered-context")


@pytest.mark.parametrize("stderr,missing", [
    (b"NOT_FOUND: fixture", True), (b"HTTP 404", True),
    (b"PERMISSION_DENIED storage.objects.get resource404 fixture", False),
    (b"403: not found", False), (b"error resource140412", False),
])
def test_missing_is_not_permission_bypass_and_native_output_is_private(monkeypatch, stderr, missing):
    seen = []

    def command(*args, **kwargs):
        seen.append(kwargs)
        return subprocess.CompletedProcess(args, 1, b"fixture-private-stdout", stderr)

    monkeypatch.setattr(release.subprocess, "run", command)
    if missing:
        assert release.run(["fixture"], allow_missing=True) == b""
    else:
        with pytest.raises(release.ReleaseError) as error:
            release.run(["fixture"], allow_missing=True)
        assert "fixture" not in str(error.value)
    assert seen[0]["capture_output"] is True
    assert seen[0]["env"]["CLOUDSDK_CORE_DISABLE_FILE_LOGGING"] == "true"


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


def test_overlay_never_executes_image_entrypoint(monkeypatch, tmp_path):
    commands = []

    def command(args, **kwargs):
        commands.append(args)
        if args[1:3] == ["image", "inspect"]:
            return json.dumps(["base"] if args[3] == release.BASE_IMAGE else ["base", "overlay"]).encode()
        if args[1] == "create":
            return b"a" * 64
        return b""

    monkeypatch.setattr(release, "run", command)
    monkeypatch.setattr(release, "SOURCE_FILES", {"safe.py": release.sha(b"fixture")})
    monkeypatch.setattr(release, "PACKAGES", {})
    monkeypatch.setattr(release, "copy_file", lambda *args: b"wrong")
    with pytest.raises(release.ReleaseError, match="image_source_file_mismatch"):
        release.verify_image(tmp_path)
    assert any(args[1] == "rm" for args in commands)
    assert all(args[1] not in {"run", "start", "exec"} for args in commands)
    assert "--entrypoint=/bin/true" in next(args for args in commands if args[1] == "create")


@pytest.mark.parametrize("registry_mismatch", [False, True])
def test_one_release_builds_once_and_only_replaces_zero_traffic_template(monkeypatch, tmp_path, service, original, registry_mismatch):
    for key, value in admitted_env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("CLOUD_RUN_SERVICE_TARGETS_JSON", json.dumps([{"service_name": "fixture-service"}]))
    monkeypatch.setattr(release, "SERVICE_HASH", release.sha("fixture-service"))
    monkeypatch.setattr(release, "REVISION_HASH", release.sha("fixture-serving"))
    monkeypatch.setattr(release, "context", lambda *args: None)
    monkeypatch.setattr(release, "verify_image", lambda *args: release.PACKAGES.copy())
    monkeypatch.setattr(release, "revision_from_lockfile", lambda *args: release.PACKAGES["us_equity_strategies-0.7.60"])
    commands, cloud_calls, replaces = [], [], []

    def command(args, **kwargs):
        commands.append(args)
        if args[1:4] == ["artifacts", "repositories", "describe"]:
            return b"SCANNING_DISABLED"
        if args[1:5] == ["artifacts", "docker", "images", "describe"]:
            if any(item[1] == "push" for item in commands):
                assert args[5] == IMAGE  # Never re-resolve a mutable tag after push.
                return ("sha256:" + ("b" if registry_mismatch else "a") * 64).encode()
            return b""
        if args[1] == "push":
            return (release.IMAGE_TAG.rsplit(":", 1)[1] + ": digest: sha256:" + "a" * 64 + " size: 1234\n").encode()
        return b""

    def cloud(args, *, data=None, allow_missing=False):
        cloud_calls.append(args)
        if args[:3] == ["run", "services", "describe"]:
            result = copy.deepcopy(service)
            if replaces:
                result["spec"] = replaces[0]["spec"]
            return result
        if args[3] == "fixture-serving":
            return copy.deepcopy(original)
        if not replaces:
            assert allow_missing
            return {}
        result = copy.deepcopy(replaces[0]["spec"]["template"])
        result["status"] = {"conditions": [{"type": "Ready", "status": "True"}]}
        return result

    monkeypatch.setattr(release, "run", command)
    monkeypatch.setattr(release, "cloud", cloud)
    monkeypatch.setattr(release, "replace_service", lambda body: replaces.append(body))
    report = {}
    if registry_mismatch:
        with pytest.raises(release.ReleaseError, match="pushed_registry_digest_mismatch"):
            release.release(tmp_path / "source", tmp_path / "context", report)
        assert not replaces
        return
    release.release(tmp_path / "source", tmp_path / "context", report)
    assert report["status"] == "PASS_NO_TRAFFIC"
    assert len(replaces) == 1
    assert replaces[0]["spec"]["traffic"] == release.routes(service)
    assert sum(args[1] == "build" for args in commands) == 1
    assert sum(args[1] == "push" for args in commands) == 1
    assert all(args[:3] in (["run", "services", "describe"], ["run", "revisions", "describe"]) for args in cloud_calls)
    assert "fixture-preserved" not in json.dumps(report)
    assert "fixture-secret" not in json.dumps(report)
    assert "opaque_fixture" not in json.dumps(report)


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
    assert calls[0][1]["json"]["metadata"]["resourceVersion"] == "opaque-token"
    assert calls[0][1]["allow_redirects"] is False
    assert calls[0][1]["timeout"] == 60


@pytest.mark.parametrize("output", [b"", b"digest: sha256:untrusted", b"other-tag: digest: sha256:" + b"a" * 64 + b" size: 1"])
def test_pushed_manifest_digest_must_come_from_exact_push_receipt(output):
    with pytest.raises(release.ReleaseError, match="pushed_manifest_digest_unavailable"):
        release.pushed_digest(output)
