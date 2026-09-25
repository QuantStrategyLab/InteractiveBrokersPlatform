from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from application.runtime_target_manifest import (
    RuntimeTargetManifestError,
    build_github_actions_matrix,
    load_runtime_target_manifest,
    validate_runtime_target_manifest,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Synthetic public matrix examples; production bindings come from protected inventory.
EXPECTED_RECONCILIATION = [
    {
        "profile": "soxl_soxx_trend_income",
        "service": "interactive-brokers-quant-live-u00000001-service",
    },
    {
        "profile": "tqqq_growth_income",
        "service": "interactive-brokers-quant-live-u00000002-service",
    },
    {
        "profile": "global_etf_rotation",
        "service": "interactive-brokers-quant-live-u00000003-service",
    },
    {
        "profile": "russell_top50_leader_rotation",
        "service": "interactive-brokers-quant-live-u00000004-service",
    },
]


def _load_render_script():
    script_path = REPO_ROOT / "scripts" / "render_runtime_target_matrix.py"
    spec = importlib.util.spec_from_file_location("render_runtime_target_matrix", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_checked_in_manifest_renders_four_live_reconciliation_targets():
    manifest = load_runtime_target_manifest()
    matrix = build_github_actions_matrix(manifest, profile="reconciliation")
    assert matrix == {"include": EXPECTED_RECONCILIATION}
    for row in matrix["include"]:
        assert "enabled" not in row
        assert "us_combo_shadow" not in row.values()


def test_reconciliation_matrix_ignores_manifest_enabled_flag():
    payload = json.loads(
        (REPO_ROOT / "config" / "runtime_targets.manifest.json").read_text(encoding="utf-8")
    )
    for target in payload["targets"]:
        target["enabled"] = True
    manifest = validate_runtime_target_manifest(payload)
    matrix = build_github_actions_matrix(manifest, profile="reconciliation")
    assert matrix == {"include": EXPECTED_RECONCILIATION}
    assert all("enabled" not in row for row in matrix["include"])


def test_reconciliation_matrix_excludes_targets_without_include_flag():
    payload = json.loads(
        (REPO_ROOT / "config" / "runtime_targets.manifest.json").read_text(encoding="utf-8")
    )
    for target in payload["targets"]:
        if target["id"] == "soxl_soxx_trend_income":
            target["include_reconciliation"] = False
    manifest = validate_runtime_target_manifest(payload)
    matrix = build_github_actions_matrix(manifest, profile="reconciliation")
    assert matrix == {
        "include": [
            row
            for row in EXPECTED_RECONCILIATION
            if row["profile"] != "soxl_soxx_trend_income"
        ]
    }


def test_reconciliation_matrix_fails_closed_when_empty():
    payload = json.loads(
        (REPO_ROOT / "config" / "runtime_targets.manifest.json").read_text(encoding="utf-8")
    )
    for target in payload["targets"]:
        target["include_reconciliation"] = False
    # Shadow already false; live targets must also flip to keep schema valid.
    manifest = validate_runtime_target_manifest(payload)
    with pytest.raises(RuntimeTargetManifestError, match="no targets with include_reconciliation"):
        build_github_actions_matrix(manifest, profile="reconciliation")


def test_unknown_matrix_profile_rejected():
    manifest = load_runtime_target_manifest()
    with pytest.raises(RuntimeTargetManifestError, match="Unknown matrix profile"):
        build_github_actions_matrix(manifest, profile="guard")


def test_render_script_writes_github_output(tmp_path, monkeypatch, capsys):
    module = _load_render_script()
    output_path = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))
    exit_code = module.main(["--profile", "reconciliation", "--github-output"])
    assert exit_code == 0
    printed = capsys.readouterr().out.strip()
    payload = json.loads(printed)
    assert payload == {"include": EXPECTED_RECONCILIATION}
    written = output_path.read_text(encoding="utf-8").strip()
    assert written.startswith("matrix=")
    assert json.loads(written.removeprefix("matrix=")) == payload


def test_render_script_rejects_github_output_without_env(monkeypatch):
    module = _load_render_script()
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert module.main(["--profile", "reconciliation", "--github-output"]) == 1
