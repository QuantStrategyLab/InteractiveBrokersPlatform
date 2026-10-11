from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "digest_candidates_store.py"
spec = importlib.util.spec_from_file_location("digest_candidates_store_for_test", SCRIPT)
store = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = store
assert spec.loader is not None
spec.loader.exec_module(store)


def _write(tmp_path, rows):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"schema_version": "qsl.digest_candidates.v1", "runs": rows}), encoding="utf-8")
    return path


def test_summary_is_redacted(tmp_path, capsys):
    path = _write(tmp_path, [{"business_day": "2026-10-12", "equity": 987.65, "opaque_account_uid": "secret-uid", "fill_count": None}])
    store.main(["summarize", "--path", str(path)])
    out = capsys.readouterr().out
    assert "987" not in out and "secret-uid" not in out
    assert json.loads(out) == {"business_days": ["2026-10-12"], "equity_present": True, "fill_count_null": True, "runs": 1, "undated_rows": 0}


def test_object_uri_single_day(tmp_path):
    path = _write(tmp_path, [{"business_day": "2026-10-12"}])
    assert store.object_uri(path, platform="schwab", target="live", uri="gs://b/p/") == "gs://b/p/schwab/2026-10-12/live.json"


def test_object_uri_empty_when_undated_or_mixed(tmp_path):
    assert store.object_uri(_write(tmp_path, [{}]), platform="schwab", target="live", uri="gs://b") == ""
    mixed = _write(tmp_path, [{"business_day": "2026-10-09"}, {"business_day": "2026-10-12"}])
    assert store.object_uri(mixed, platform="schwab", target="live", uri="gs://b") == ""


def test_object_uri_rejects_unsafe_input(tmp_path):
    path = _write(tmp_path, [{"business_day": "2026-10-12"}])
    with pytest.raises(ValueError):
        store.object_uri(path, platform="../x", target="live", uri="gs://b")
    with pytest.raises(ValueError):
        store.object_uri(path, platform="schwab", target="live", uri="https://b")


def test_workflow_is_dry_run_without_store_variable():
    wf = (Path(__file__).resolve().parents[1] / ".github/workflows/digest-candidates-emit.yml").read_text(encoding="utf-8")
    assert "DIGEST_CANDIDATES_GCS_URI: ${{ vars.DIGEST_CANDIDATES_GCS_URI || '' }}" in wf
    assert "dry_run: vars.DIGEST_CANDIDATES_GCS_URI is unset" in wf
    # Artifact capture exists only as the manual opt-in; the scheduled caller never enables it.
    assert "if: ${{ inputs.capture_artifact }}" in wf
    daily = (Path(__file__).resolve().parents[1] / ".github/workflows/digest-candidates-daily.yml").read_text(encoding="utf-8")
    assert "capture_artifact" not in daily
    assert "SYNC_TOKEN" not in wf


def test_target_json_variables_only_reach_jobs_as_masked_secrets():
    root = Path(__file__).resolve().parents[1] / ".github/workflows"
    emit = (root / "digest-candidates-emit.yml").read_text(encoding="utf-8")
    assert "vars.CLOUD_RUN_SERVICE_TARGETS_JSON" not in emit
    assert "vars.RUNTIME_TARGET_JSON" not in emit
    assert "CLOUD_RUN_SERVICE_TARGETS_JSON: ${{ secrets.CLOUD_RUN_SERVICE_TARGETS_JSON }}" in emit
    for caller in ("digest-candidates-daily.yml", "execution-report-heartbeat.yml"):
        text = (root / caller).read_text(encoding="utf-8")
        assert "uses: ./.github/workflows/digest-candidates-emit.yml" in text
    daily = (root / "digest-candidates-daily.yml").read_text(encoding="utf-8")
    assert "steps:" not in daily  # no step of the caller can echo the variable
