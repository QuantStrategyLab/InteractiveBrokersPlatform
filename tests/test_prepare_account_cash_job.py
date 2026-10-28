"""Exercise candidate construction through the existing entry using synthetic data."""
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from scripts import account_cash_readonly_entry as entry
from scripts import prepare_account_cash_job as candidate
from tests.test_account_cash_readonly_entry import setup_case


@pytest.mark.parametrize("profile", ["global_etf_rotation", "russell_top50_leader_rotation"])
def test_serving_target_to_cash_entry_and_unverified_candidate_rejected(profile, monkeypatch, capsys, tmp_path):
    runtime, env, broker, calls, _ = setup_case(profile)
    target = vars(runtime.RUNTIME_SETTINGS.runtime_target)
    base = "us-docker.pkg.dev/test/repo/image@sha256:" + "1" * 64
    image = "us-docker.pkg.dev/test/repo/image@sha256:" + "2" * 64
    rev = {"metadata": {"name": "revision", "annotations": {
        "run.googleapis.com/vpc-access-connector": "synthetic-vpc"}},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        "spec": {"serviceAccountName": "synthetic-sa", "containers": [{
            "image": base, "resources": {"limits": {"cpu": "1", "memory": "512Mi"}},
            "env": [{"name": "RUNTIME_TARGET_JSON", "value": json.dumps(target)},
                    {"name": "RUNTIME_TARGET_ENABLED", "value": "false"},
                    {"name": "EXECUTION_REPORT_GCS_URI", "value": env["EXECUTION_REPORT_GCS_URI"]},
                    {"name": "SECRET", "valueFrom": {"secretKeyRef": {"name": "secret", "key": "latest"}}}]}]}}
    service = {"metadata": {"name": "private-service", "resourceVersion": "1"},
               "status": {"traffic": [{"percent": 100, "revisionName": "revision"}]}}
    monkeypatch.setattr(candidate.preflight.binding, "configuration", lambda _: (
        {}, {profile: ("private-service", target)}, "project", "region"))
    monkeypatch.setattr(candidate.preflight.archive, "resolve_profile_slot", lambda _: "additional-1")
    expected = {"expected_project_id": "project", "expected_service_name": "private-service",
                "expected_runtime_revision": "revision"}
    input_env = {"REQUESTED_TARGET": profile}
    dockerfile, manifest, sources, job = candidate.prepare(
        input_env, expected, reader=lambda kind, *args: service if kind == "services" else rev)
    assert job is None
    assert manifest["base_image"] == base and dockerfile.startswith("FROM " + base)
    assert set(sources) == set(candidate.FILES)
    path = tmp_path / "image-input.json"
    path.write_text(json.dumps(manifest))
    second = {**input_env, "CASH_CANDIDATE_IMAGE": image, "CASH_CANDIDATE_JOB": "cash-read-123",
              "CASH_BUILD_INPUT": str(path)}
    monkeypatch.setattr(candidate.image_verifier, "verify", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("unverified")))
    with pytest.raises(ValueError, match="unverified"):
        candidate.prepare(second, expected, reader=lambda kind, *args: service if kind == "services" else rev)
    monkeypatch.setattr(candidate.image_verifier, "verify", lambda *args, **kwargs: None)
    _, _, _, document = candidate.prepare(second, expected, reader=lambda kind, *args: service if kind == "services" else rev)
    task = document["spec"]["template"]["spec"]["template"]["spec"]
    assert task["maxRetries"] == 0 and task["containers"][0]["image"] == image
    assert "COPY --chown=1000:1000 --chmod=0644" in dockerfile
    assert "USER root" in dockerfile and dockerfile.endswith("USER appuser\n")
    original_inspect = candidate.preflight.inspect
    count = 0
    def drift(*args, **kwargs):
        nonlocal count
        count += 1
        result = original_inspect(*args, **kwargs)
        if count == 2:
            result["network_declarations_sha256"] = "0" * 64
        return result
    monkeypatch.setattr(candidate.preflight, "inspect", drift)
    with pytest.raises(ValueError, match="serving_changed_after_image_verification"):
        candidate.prepare(second, expected, reader=lambda kind, *args: service if kind == "services" else rev)
    monkeypatch.setattr(candidate.preflight, "inspect", original_inspect)
    altered = dict(manifest, serving_revision_sha256="0" * 64)
    path.write_text(json.dumps(altered))
    with pytest.raises(ValueError, match="build_input_changed"):
        candidate.prepare(second, expected, reader=lambda kind, *args: service if kind == "services" else rev)
    try:
        actual_env = {**env, "CASH_READONLY_PROFILE": profile,
                      "CASH_READONLY_TARGET_SHA256": manifest["target_binding_sha256"],
                      "CASH_READONLY_REPORT_PREFIX_SHA256": manifest["report_prefix_sha256"]}
        assert entry.run(runtime, lambda _: {"account_ids": ["DU00000001"]}, actual_env) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "observation_saved"
        assert calls[0][0] == "connect" and broker.calls[0][2:] == ("All", "$LEDGER:USD")
        actual_env["CASH_READONLY_TARGET_SHA256"] = "0" * 64
        calls.clear()
        assert entry.run(runtime, lambda _: {}, actual_env) == 1 and calls == []
    finally:
        broker.loop.close()


def test_legacy_job_constructor_keeps_original_digest_default():
    from scripts import observe_maintenance_cash_job as maintenance
    from tests.test_observe_maintenance_cash_job import setup
    _, revision, _, _ = setup()
    document = maintenance.job_document(revision, "cash-read-123", "pass")
    image = document["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["image"]
    assert image.endswith("@" + maintenance.IMAGE_DIGEST)


def test_image_patch_requires_exact_old_functions_and_limits_changes():
    from scripts import patch_cash_candidate_image as patcher
    source = "def connect():\n" + patcher.OLD_CONNECT + "\n\ndef persist():\n" + patcher.OLD_PERSIST + "\n"
    patched = patcher.patch(source)
    compile(patched, "synthetic_main.py", "exec")
    assert "connect_attempts=1" in patched and ' + "/maintenance-cash"' in patched
    with pytest.raises(ValueError, match="base_image_source_not_supported"):
        patcher.patch(source.replace(patcher.OLD_CONNECT, "    pass"))


def test_patched_report_writer_preserves_other_reports_and_isolates_maintenance():
    from scripts import patch_cash_candidate_image as patcher

    @dataclass(frozen=True)
    class Reporting:
        report_base_dir: str = "/tmp/synthetic"
        report_gcs_prefix_uri: str = "gs://synthetic/reports"

        def persist_execution_report(self, report):
            return self.report_base_dir, self.report_gcs_prefix_uri

    original = Reporting()
    scope = {"Mapping": Mapping, "os": os, "build_composer": lambda **_: SimpleNamespace(
        build_reporting_adapters=lambda: original)}
    exec("def persist(report, dry_run_only_override=None):\n" + patcher.NEW_PERSIST, scope)
    assert scope["persist"]({"diagnostics": None}) == (original.report_base_dir, original.report_gcs_prefix_uri)
    assert scope["persist"]({"diagnostics": {"maintenance_cash_observation": True}}) == (
        "/tmp/synthetic/maintenance-cash", "gs://synthetic/reports/maintenance-cash")
    assert original.report_base_dir == "/tmp/synthetic"


def test_output_is_private_and_existing_directory_is_never_overwritten(monkeypatch, tmp_path, capsys):
    destination = tmp_path / "candidate"
    monkeypatch.setenv("CASH_CANDIDATE_DIR", str(destination))
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_TARGET", "previous")
    monkeypatch.setattr(candidate.archive, "resolve_profile_slot", lambda _: "additional-1")
    monkeypatch.setattr(candidate.identity, "resolve_expected", lambda: {})
    monkeypatch.setattr(candidate, "prepare", lambda *args: (
        "FROM synthetic\n", {"base_image": "synthetic"}, {"scripts/one.py": b"pass\n"}, {"kind": "Job"}))
    assert candidate.main() == 0
    assert json.loads(capsys.readouterr().out)["job_definition_prepared"] is True
    assert destination.stat().st_mode & 0o077 == 0
    for path in (destination / "Dockerfile", destination / "image-input.json", destination / "job.private.json",
                 destination / "scripts/one.py"):
        assert path.stat().st_mode & 0o077 == 0
    assert candidate.main() == 1
    assert (destination / "scripts/one.py").read_bytes() == b"pass\n"
