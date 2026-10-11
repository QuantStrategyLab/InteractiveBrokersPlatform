import json
from pathlib import Path

import pytest

from scripts import verify_image_ues_release_binding as guard

OLD = "4a3943883cd6b5bbfe32a559e56a91b40a81b7ce"
NEW = "9d1544d1476dd19b1da61b16f37cee7f9126caf4"


def _target(service, *, release=None, binding=None):
    rt = {"service_name": service}
    if release:
        rt["strategy_release"] = {"release_id": f"{service}-rel", "strategy_revision": release}
    if binding:
        rt["runtime_risk_limits"] = {"binding": {"ues_revision": binding}}
    return rt


def _plan(*targets):
    return json.dumps({"targets": [{"service_name": t["service_name"], "env": {"RUNTIME_TARGET_JSON": json.dumps(t)}}
                                   for t in targets]})


def test_lockfile_pin_is_read_from_repo_lock():
    assert guard.SHA_RE.match(guard.revision_from_lockfile(Path("uv.lock")))


def test_matching_release_and_binding_pass():
    env = {"SYNC_PLAN_JSON": _plan(_target("soxl", release=OLD, binding=OLD), _target("tqqq", release=OLD))}
    assert guard.check({"image": OLD}, guard.runtime_targets_from_env(env)) == []


@pytest.mark.parametrize("release,binding", [(NEW, OLD), (OLD, NEW), (OLD, OLD)])
def test_any_mismatch_blocks(release, binding):
    env = {"SYNC_PLAN_JSON": _plan(_target("soxl", release=release, binding=binding))}
    installed = NEW if (release, binding) == (OLD, OLD) else OLD
    failures = guard.check({"image": installed}, guard.runtime_targets_from_env(env))
    assert failures and all("target-1" in line for line in failures)


def test_rebuild_from_newer_pin_without_release_update_is_blocked():
    env = {"SYNC_PLAN_JSON": _plan(_target("soxl", release=OLD, binding=OLD))}
    failures = guard.check({"lockfile": NEW, "image": NEW}, guard.runtime_targets_from_env(env))
    assert len(failures) == 2


def test_targets_without_release_are_not_blocked():
    env = {"SYNC_PLAN_JSON": _plan(_target("russell"))}
    assert guard.check({"image": NEW}, guard.runtime_targets_from_env(env)) == []


def test_installed_sources_must_agree():
    env = {"SYNC_PLAN_JSON": _plan(_target("soxl", release=OLD))}
    assert guard.check({"lockfile": NEW, "image": OLD}, guard.runtime_targets_from_env(env))


def test_no_target_fails_closed():
    assert guard.check({"image": OLD}, guard.runtime_targets_from_env({}))


def test_fallback_to_service_targets_json_and_single_runtime_target():
    targets = {"targets": [{"service_name": "soxl", "runtime_target": _target("soxl", release=OLD, binding=OLD)}]}
    resolved = guard.runtime_targets_from_env({"CLOUD_RUN_SERVICE_TARGETS_JSON": json.dumps(targets)})
    assert resolved[0][0] == "soxl"
    assert guard.check({"image": NEW}, resolved)
    single = guard.runtime_targets_from_env({"RUNTIME_TARGET_JSON": json.dumps(_target("svc", release=OLD))})
    assert guard.check({"image": OLD}, single) == []


def test_main_exit_codes(monkeypatch, capsys):
    monkeypatch.setenv("SYNC_PLAN_JSON", _plan(_target("soxl", release=OLD, binding=OLD)))
    monkeypatch.delenv("CLOUD_RUN_SERVICE_TARGETS_JSON", raising=False)
    assert guard.main(["--installed-revision", OLD]) == 0
    assert guard.main(["--installed-revision", NEW]) == 1
    assert guard.main([]) == 1
    assert "Deploy blocked" in capsys.readouterr().err


def test_deploy_workflow_runs_guard_before_push_and_deploy():
    workflow = Path(".github/workflows/sync-cloud-run-env-protected.yml").read_text(encoding="utf-8")
    guard_at = workflow.index("scripts/verify_image_ues_release_binding.py --image")
    assert workflow.index("docker build --pull") < guard_at < workflow.index('docker push "${image}"')
    assert guard_at < workflow.index("gcloud run deploy")


@pytest.mark.parametrize("stamp,direct,expected", [
    (OLD, OLD, OLD), (OLD, "", OLD), ("", OLD, OLD),
    (OLD, NEW, None), ("", "", None), ("private-account", "", None),
])
def test_actual_image_probe_branch(monkeypatch, stamp, direct, expected):
    import subprocess

    def run(command, **kwargs):
        assert command[:7] == ["docker", "run", "--rm", "--network", "none", "--entrypoint", "python"]
        assert command[7:] == ["synthetic-image", "-c", guard._IMAGE_PROBE]
        assert kwargs == {"capture_output": True, "text": True, "timeout": 300, "check": False}
        return subprocess.CompletedProcess(command, 0, f"UES_STAMP={stamp}\nUES_DIRECT_URL={direct}\n", "secret-stderr")

    monkeypatch.setattr(guard.subprocess, "run", run)
    if expected:
        assert guard.revision_from_image("synthetic-image") == expected
    else:
        with pytest.raises(guard.GuardError):
            guard.revision_from_image("synthetic-image")


@pytest.mark.parametrize("failure", ["nonzero", "timeout", "missing_docker"])
def test_image_failures_keep_exit_code_and_redact(monkeypatch, tmp_path, capsys, failure):
    import subprocess

    secret = "DO-NOT-PRINT-account-service-runtime-stderr"

    def run(command, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 300, output=secret, stderr=secret)
        if failure == "missing_docker":
            raise OSError(secret)
        return subprocess.CompletedProcess(command, 1, secret, secret)

    monkeypatch.setattr(guard.subprocess, "run", run)
    path = tmp_path / "report.json"
    assert guard.main(["--image", secret, "--report-json", str(path)]) == 1
    report = json.loads(path.read_text())
    assert report["status"] == "error"
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err + path.read_text()


@pytest.mark.parametrize("targets,status,code,counts", [
    ([_target("secret-service", release=OLD)], "verified", 0, (1, 0, 0)),
    ([_target("secret-service")], "not_covered", 0, (0, 0, 1)),
    ([_target("secret-service", release=OLD), _target("secret-unbound")], "not_covered", 0, (1, 0, 1)),
    ([_target("secret-service", release=NEW)], "mismatch", 1, (0, 1, 0)),
])
def test_structured_coverage_is_sanitized(monkeypatch, tmp_path, capsys, targets, status, code, counts):
    monkeypatch.setenv("SYNC_PLAN_JSON", _plan(*targets))
    path = tmp_path / "report.json"
    assert guard.main(["--installed-revision", OLD, "--report-json", str(path)]) == code
    report = json.loads(path.read_text())
    assert report["status"] == status
    assert tuple(report["target_counts"][key] for key in ("verified", "mismatch", "not_covered")) == counts
    assert report["evidence_scope"] == "built_image_and_planned_targets"
    assert set(report) == {"schema_version", "evidence_scope", "status", "target_counts", "installed_revisions"}
    output = capsys.readouterr()
    assert "secret-" not in path.read_text() + output.out + output.err
    assert "guard: OK" not in output.out


def test_no_target_and_disagreeing_sources_reports():
    report = guard.new_report()
    assert guard.check({"image": OLD}, [], report=report)
    assert report["status"] == "uncomputable"
    report = guard.new_report()
    assert guard.check({"image": OLD, "lockfile": NEW}, [("private", {})], report=report)
    assert report["status"] == "mismatch"
    assert report["target_counts"]["uncomputable"] == 1


def test_untrusted_values_are_not_printed(monkeypatch, tmp_path, capsys):
    private = "account-secret-env-assets"
    monkeypatch.setenv("SYNC_PLAN_JSON", _plan(_target(private, release=private)))
    path = tmp_path / "report.json"
    assert guard.main(["--installed-revision", OLD, "--report-json", str(path)]) == 1
    output = capsys.readouterr()
    assert private not in output.out + output.err + path.read_text()
    monkeypatch.setenv("SYNC_PLAN_JSON", "{invalid-" + private)
    assert guard.main(["--installed-revision", OLD, "--report-json", str(path)]) == 1
    output = capsys.readouterr()
    assert private not in output.out + output.err + path.read_text()


def test_report_write_failure_does_not_change_validation(monkeypatch, tmp_path):
    monkeypatch.setenv("SYNC_PLAN_JSON", _plan(_target("private", release=OLD)))
    assert guard.main(["--installed-revision", OLD, "--report-json", str(tmp_path)]) == 0
    assert guard.main(["--installed-revision", NEW, "--report-json", str(tmp_path)]) == 1


def test_summary_reads_report_without_validation(tmp_path, monkeypatch, capsys):
    path = tmp_path / "report.json"
    report = guard.new_report()
    report["status"] = "not_covered"
    report["target_counts"]["verified"] = 1
    report["target_counts"]["not_covered"] = 1
    report["unexpected"] = "private-account"
    path.write_text(json.dumps(report))
    monkeypatch.setattr(guard, "check", lambda *a, **kw: pytest.fail("summary must not validate"))
    assert guard.main(["--summarize-report", str(path)]) == 0
    output = capsys.readouterr().out
    assert "not_covered=1" in output
    assert "does not verify a live deployed revision" in output
    assert "private-account" not in output
    report["target_counts"]["verified"] = "private-account"
    path.write_text(json.dumps(report))
    assert guard.main(["--summarize-report", str(path)]) == 0
    assert capsys.readouterr().out == "UES build-time binding coverage: report unavailable.\n"


def test_workflow_summary_only_consumes_report():
    workflow = Path(".github/workflows/sync-cloud-run-env-protected.yml").read_text()
    assert '--report-json "${RUNNER_TEMP}/ues-binding-coverage.json"' in workflow
    summary = workflow.split("- name: Summarize build-time UES binding coverage", 1)[1].split("- name:", 1)[0]
    assert "if: always() && steps.config.outputs.deploy_enabled == 'true'" in summary
    assert '--summarize-report "${RUNNER_TEMP}/ues-binding-coverage.json"' in summary
    assert "continue-on-error: true" in summary
    assert "GITHUB_STEP_SUMMARY" in summary
    assert "SYNC_PLAN_JSON" not in summary
