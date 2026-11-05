"""Fake-only tag bounds and fixed warmup failure diagnostics."""
import json
import subprocess
from unittest import mock

import pytest

from scripts import reconcile_cloud_runtime as rcr
from scripts.cloud_run_candidate_warmup import Release
from tests.test_candidate_tag_release import Cloud, COMMIT, DIGEST


@pytest.mark.parametrize("length", [1, 33, 34, 35, 36, 37])
def test_candidate_tag_fits_and_remains_source_bound(tmp_path, length):
    obj = Release(project="<PROJECT_ID>", region="unit-region", service="x" * length,
                  commit=COMMIT, digest=DIGEST, run_id="123", deploy_args=[],
                  state_path=tmp_path / "state.json")
    assert obj.tag and length + len(obj.tag) <= 46
    assert COMMIT[:8] in obj.tag


@pytest.mark.parametrize("length", [38, 45, 46, 48, 63])
def test_impossible_tag_fails_before_cloud_or_state_creation(tmp_path, length, capsys):
    cloud = Cloud()
    with pytest.raises(ValueError, match="CANDIDATE_TAG_LENGTH_UNSUPPORTED"):
        obj = Release(project="<PROJECT_ID>", region="unit-region", service="x" * length,
                      commit=COMMIT, digest=DIGEST, run_id="123", deploy_args=[],
                      state_path=tmp_path / "state.json", cloud=cloud)
        obj.execute()
    assert cloud.commands == []
    assert not (tmp_path / "state.json").exists()
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("error,code", [
    (subprocess.TimeoutExpired("fake-private-command", 1), "TIMEOUT"),
    (OSError("fake-private-path"), "EXECUTION_UNAVAILABLE"),
    (rcr.ReconcileError("fake-private-provider-detail"), "COMMAND_FAILED"),
    (ValueError("fake-private-readback"), "INVALID_READBACK"),
])
def test_cloud_read_failure_has_fixed_reason_and_no_private_detail(error, code, capsys):
    with mock.patch.object(rcr, "_run", side_effect=error):
        with pytest.raises(rcr.ReconcileError) as result:
            rcr._read_deploy_cloud(["fake"])
    assert str(result.value) == "candidate warmup cloud read failed: " + code
    assert result.value.__suppress_context__
    assert capsys.readouterr() == ("", "")


def test_invalid_json_is_classified_without_provider_text():
    error = rcr.ReconcileError("fake-private-command")
    error.__cause__ = json.JSONDecodeError("bad", "fake-private-json", 0)
    with mock.patch.object(rcr, "_run", side_effect=error):
        with pytest.raises(rcr.ReconcileError, match="INVALID_READBACK"):
            rcr._read_deploy_cloud(["fake"])
