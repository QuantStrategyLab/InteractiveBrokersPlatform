import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

path = Path(__file__).parents[1] / "scripts" / "verify_image_ues_release_binding.py"
spec = importlib.util.spec_from_file_location("guard", path)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

REV = "a" * 40
OTHER = "b" * 40


def test_partial_bindings_are_not_complete_coverage():
    for target in (
        {"strategy_release": {"strategy_revision": REV}},
        {"runtime_risk_limits": {"binding": {"ues_revision": REV}}},
    ):
        report = guard.new_report()
        assert guard.check({"image": REV}, [("synthetic", target)], report=report) == []
        assert report["status"] == "not_covered"
        assert report["target_counts"]["not_covered"] == 1


def test_complete_bindings_and_partial_mismatch():
    report = guard.new_report()
    target = {"strategy_release": {"strategy_revision": REV},
              "runtime_risk_limits": {"binding": {"ues_revision": REV}}}
    assert guard.check({"image": REV}, [("synthetic", target)], report=report) == []
    assert report["status"] == "verified"
    target["runtime_risk_limits"]["binding"]["ues_revision"] = OTHER
    report = guard.new_report()
    assert guard.check({"image": REV}, [("synthetic", target)], report=report)
    assert report["status"] == "mismatch"
    report = guard.new_report()
    assert guard.check({"image": REV}, [("synthetic", {"strategy_release": {"strategy_revision": OTHER}})], report=report)
    assert report["status"] == "mismatch"


def test_missing_fields_and_each_mismatch():
    report = guard.new_report()
    assert guard.check({"image": REV}, [("synthetic", {})], report=report) == []
    assert report["status"] == "not_covered"
    for release, binding in ((OTHER, REV), (REV, OTHER), (OTHER, OTHER)):
        target = {"strategy_release": {"strategy_revision": release},
                  "runtime_risk_limits": {"binding": {"ues_revision": binding}}}
        report = guard.new_report()
        assert guard.check({"image": REV}, [("synthetic", target)], report=report)
        assert report["status"] == "mismatch"


def test_cli_rejects_invalid_installed_revision(monkeypatch, tmp_path):
    for value in (" ", "invalid"):
        target = {"strategy_release": {"strategy_revision": value},
                  "runtime_risk_limits": {"binding": {"ues_revision": value}}}
        monkeypatch.setenv("RUNTIME_TARGET_JSON", json.dumps(target))
        monkeypatch.delenv("SYNC_PLAN_JSON", raising=False)
        monkeypatch.delenv("CLOUD_RUN_SERVICE_TARGETS_JSON", raising=False)
        report_path = tmp_path / "report.json"
        assert guard.main(["--installed-revision", value, "--report-json", str(report_path)]) == 1
        report = json.loads(report_path.read_text())
        assert report["status"] == "error"
        assert report["target_counts"]["verified"] == 0
        assert report["target_counts"]["uncomputable"] == 1


def test_cli_accepts_valid_complete_bindings(monkeypatch, tmp_path):
    target = {"strategy_release": {"strategy_revision": REV},
              "runtime_risk_limits": {"binding": {"ues_revision": REV}}}
    monkeypatch.setenv("RUNTIME_TARGET_JSON", json.dumps(target))
    monkeypatch.delenv("SYNC_PLAN_JSON", raising=False)
    monkeypatch.delenv("CLOUD_RUN_SERVICE_TARGETS_JSON", raising=False)
    report_path = tmp_path / "report.json"
    assert guard.main(["--installed-revision", REV, "--report-json", str(report_path)]) == 0
    assert json.loads(report_path.read_text())["status"] == "verified"


def test_cli_process_rejects_invalid_revisions(tmp_path):
    for value in (" ", "invalid"):
        target = {"strategy_release": {"strategy_revision": value},
                  "runtime_risk_limits": {"binding": {"ues_revision": value}}}
        env = dict(os.environ)
        env.pop("SYNC_PLAN_JSON", None)
        env.pop("CLOUD_RUN_SERVICE_TARGETS_JSON", None)
        env["RUNTIME_TARGET_JSON"] = json.dumps(target)
        report_path = tmp_path / "report.json"
        result = subprocess.run(
            [sys.executable, str(path), "--installed-revision", value, "--report-json", str(report_path)],
            env=env, capture_output=True, text=True, check=False,
        )
        assert result.returncode == 1
        assert json.loads(report_path.read_text())["status"] == "error"
