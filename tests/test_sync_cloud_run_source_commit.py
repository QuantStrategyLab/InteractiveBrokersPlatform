"""Exercise the trusted workflow's exact source-SHA checkout guard."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "sync-cloud-run-env.yml"


def _source_validation_script() -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "      - name: Verify selected image source\n"
    section = text.split(marker, 1)[1].split("\n      - name:", 1)[0]
    run_marker = "        run: |\n"
    body = section.split(run_marker, 1)[1]
    return "\n".join(line[10:] if line.startswith("          ") else line for line in body.splitlines())


def _config_script() -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "      - name: Check whether Cloud Run automation is enabled\n"
    section = text.split(marker, 1)[1].split("\n      - name:", 1)[0]
    body = section.split("        run: |\n", 1)[1]
    return "\n".join(line[10:] if line.startswith("          ") else line for line in body.splitlines())


def _make_source_repo(path: Path) -> str:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "marker").write_text("source\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "marker"], check=True)
    env = dict(os.environ, GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid",
               GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid")
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "source"], check=True, env=env)
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def _run_validation(tmp_path: Path, selected_sha: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", _source_validation_script()],
        cwd=tmp_path,
        env=dict(os.environ, SOURCE_COMMIT=selected_sha),
        text=True,
        capture_output=True,
    )


def test_selected_source_verifier_accepts_exact_checkout_and_rejects_mismatch(tmp_path: Path) -> None:
    selected_sha = _make_source_repo(tmp_path / "source")
    assert _run_validation(tmp_path, selected_sha).returncode == 0
    assert _run_validation(tmp_path, "a" * 40).returncode != 0
    assert _run_validation(tmp_path, "not-a-full-sha").returncode != 0


def test_workflow_uses_main_admission_and_selected_sha_consistently() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert 'GITHUB_REF:-}" != "refs/heads/main"' in workflow
    assert "source_commit" in workflow
    assert "repository: ${{ github.repository }}" in workflow
    assert "ref: ${{ env.SOURCE_COMMIT }}" in workflow
    assert "docker build --pull -t \"${image}\" source" in workflow
    assert '--labels="managed-by=github-actions,commit-sha=${SOURCE_COMMIT},github-run-id=${GITHUB_RUN_ID}"' in workflow
    assert '--expected-sha="${SOURCE_COMMIT}"' in workflow
    assert 'target_sha="${SOURCE_COMMIT}"' in workflow
    assert "--no-traffic" in workflow


def test_dispatch_config_requires_main_and_keeps_deploy_only_gates(tmp_path: Path) -> None:
    output = tmp_path / "github-output"
    common = dict(
        os.environ,
        GITHUB_EVENT_NAME="workflow_dispatch",
        GITHUB_REF="refs/heads/main",
        GITHUB_OUTPUT=str(output),
        ENABLE_GITHUB_CLOUD_RUN_DEPLOY="true",
        ENABLE_GITHUB_ENV_SYNC="true",
        ENABLE_MAIN_PUSH_CLOUD_RUN_AUTOMATION="false",
        INPUT_CONFIGURED_SERVICE="interactive-brokers-quant-live-u16608560-service",
        INPUT_SOURCE_COMMIT="a" * 40,
        INPUT_DEPLOY_IMAGE="true",
        INPUT_SYNC_ENV="false",
        INPUT_APPROVE_TRAFFIC_SHIFT="false",
        INPUT_APPROVE_SCHEDULER_SYNC="false",
    )
    script = _config_script()
    main_result = subprocess.run(["bash", "-euo", "pipefail", "-c", script], env=common, text=True, capture_output=True)
    assert main_result.returncode == 0, main_result.stderr
    values = output.read_text(encoding="utf-8")
    assert "deploy_enabled=true" in values
    assert "env_sync_enabled=false" in values
    assert "traffic_shift_enabled=false" in values
    assert "scheduler_sync_enabled=false" in values

    missing_source = dict(common, INPUT_SOURCE_COMMIT="", GITHUB_OUTPUT=str(tmp_path / "default-output"))
    default_result = subprocess.run(["bash", "-euo", "pipefail", "-c", script], env=missing_source, text=True, capture_output=True)
    assert default_result.returncode == 0, default_result.stderr

    branch_output = tmp_path / "branch-output"
    branch_env = dict(common, GITHUB_REF="refs/heads/candidate", GITHUB_OUTPUT=str(branch_output))
    branch_result = subprocess.run(["bash", "-euo", "pipefail", "-c", script], env=branch_env, text=True, capture_output=True)
    assert branch_result.returncode != 0
    assert "trusted main workflow ref" in branch_result.stderr

    malformed_env = dict(common, INPUT_SOURCE_COMMIT="short", GITHUB_OUTPUT=str(tmp_path / "bad-output"))
    malformed = subprocess.run(["bash", "-euo", "pipefail", "-c", script], env=malformed_env, text=True, capture_output=True)
    assert malformed.returncode != 0
    assert "Explicit source_commit must be a full 40-character SHA" in malformed.stderr
