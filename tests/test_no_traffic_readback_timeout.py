"""Bound baseline reads and exercise timeout cleanup without cloud access."""
from __future__ import annotations

import argparse
import json
import subprocess
from unittest import mock

import pytest

from scripts import verify_cloud_run_no_traffic_deploy as readback
from tests.test_candidate_tag_release import COMMIT, DIGEST, Cloud, release


STAGES = ("service", "iam", "scheduler", "created_name", "revision")


def stage(command):
    if "get-iam-policy" in command:
        return "iam"
    if "scheduler" in command:
        return "scheduler"
    if "revisions" in command:
        return "revision"
    if command[-1].startswith("--format=value("):
        return "created_name"
    return "service"


def response(command):
    values = {
        "service": {"status": {"traffic": [{"revisionName": "unit-old", "percent": 100}]}, "spec": {}},
        "iam": {"bindings": []},
        "scheduler": [],
        "revision": {"metadata": {"labels": {"commit-sha": COMMIT}},
                     "spec": {"containers": [{"image": "fake@" + DIGEST}]}},
    }
    value = "unit-warm" if stage(command) == "created_name" else json.dumps(values[stage(command)])
    return subprocess.CompletedProcess(command, 0, stdout=value, stderr="")


def baseline(monkeypatch, tmp_path):
    args = argparse.Namespace(project="fake-project", region="unit-region", service="unit-service",
                              scheduler_location="unit-region", before=tmp_path / "baseline.json",
                              expected_sha=COMMIT, expected_image_digest=DIGEST)
    monkeypatch.setattr(readback.subprocess, "run", lambda command, **kwargs: response(command))
    args.before.write_text(json.dumps(readback._snapshot(args)))
    return args


def test_every_baseline_read_has_a_finite_timeout(monkeypatch, tmp_path):
    args = baseline(monkeypatch, tmp_path)
    calls = []

    def run(command, **kwargs):
        calls.append(stage(command))
        assert kwargs.get("timeout") == 30
        return response(command)

    monkeypatch.setattr(readback.subprocess, "run", run)
    readback._verify(args)
    assert calls == list(STAGES)


@pytest.mark.parametrize("failing_stage", STAGES)
def test_verification_timeout_blocks_shift_and_runs_cleanup(monkeypatch, tmp_path, capsys, failing_stage):
    args = baseline(monkeypatch, tmp_path)
    sentinel = "private-diagnostic-sentinel"

    def run(command, **kwargs):
        if stage(command) == failing_stage:
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout"), output=sentinel, stderr=sentinel)
        return response(command)

    monkeypatch.setattr(readback.subprocess, "run", run)
    cloud = Cloud()
    obj = release(tmp_path, cloud)
    obj.no_traffic_verify = lambda: readback._verify(args)
    with mock.patch("scripts.reconcile_cloud_runtime._verify_deploy_candidate_with_wait"):
        with pytest.raises(RuntimeError, match="readback timed out") as caught:
            obj.execute()
    assert caught.value.__suppress_context__
    assert sentinel not in str(caught.value)
    assert not any(any(value.startswith("--to-revisions=") for value in command) for command in cloud.commands)
    assert any("--remove-tags=cand-aaaaaaaa" in command for command in cloud.commands)
    assert any("--min-instances=0" in command for command in cloud.commands)
    assert json.loads(obj.state_path.read_text())["status"] == "restored"
    assert cloud.service["status"]["traffic"] == [{"revisionName": "unit-service-old", "percent": 100}]
    output = capsys.readouterr()
    assert sentinel not in output.out + output.err


@pytest.mark.parametrize("command", ["capture", "verify"])
def test_timeout_cli_fails_with_fixed_diagnostic(monkeypatch, tmp_path, capsys, command):
    args = baseline(monkeypatch, tmp_path)
    output = tmp_path / "capture.json"
    argv = ["readback", command, "--project=fake-project", "--region=unit-region",
            "--service=unit-service", "--scheduler-location=unit-region"]
    argv += (["--output", str(output)] if command == "capture" else
             ["--before", str(args.before), "--expected-sha", COMMIT, "--expected-image-digest", DIGEST])
    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.setattr(readback.subprocess, "run", mock.Mock(side_effect=subprocess.TimeoutExpired(
        ["gcloud", "private-diagnostic-sentinel"], 30, output="private-output", stderr="private-error")))
    assert readback.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "No-traffic deployment readback failed: read-only gcloud readback timed out after 30 seconds\n"
    assert not output.exists()
