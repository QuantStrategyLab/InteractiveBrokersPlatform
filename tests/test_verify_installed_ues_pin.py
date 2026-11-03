"""Exercise the actual Docker build guard with synthetic installed metadata."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

PIN = "a" * 40
OTHER = "b" * 40
REPOSITORY = "https://github.com/QuantStrategyLab/UsEquityStrategies.git"
ROOT = Path(__file__).resolve().parents[1]


def build_guard(tmp_path, *, project=PIN, lock=PIN, installed=PIN, metadata=True,
                stamp=None, malformed=False):
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\ndependencies = ["us-equity-strategies @ git+{REPOSITORY}@{project}"]\n'
    )
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "us-equity-strategies"\n'
        f'source = {{ git = "{REPOSITORY}?rev={project}#{lock}" }}\n'
    )
    dist = tmp_path / "us_equity_strategies-0.0.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Name: us-equity-strategies\nVersion: 0.0.0\n")
    if metadata:
        (dist / "direct_url.json").write_text("{bad-private-value" if malformed else json.dumps({
            "url": REPOSITORY, "vcs_info": {"vcs": "git", "commit_id": installed},
        }))
    if stamp is not None:
        (tmp_path / "UES_REVISION").write_text(stamp)
    source = ROOT / "scripts/verify_installed_ues_pin.py"
    if source.exists():
        (tmp_path / "scripts").mkdir()
        shutil.copyfile(source, tmp_path / "scripts" / source.name)
    line = next(line for line in (ROOT / "Dockerfile").read_text().splitlines()
                if 'direct_url.json' in line or 'python scripts/verify_installed_ues_pin.py' in line)
    command = line.strip().removeprefix("&& ").removesuffix(" \\")
    command = command.replace("/app/.venv/bin/python", shlex.quote(sys.executable))
    command = command.replace("/app/UES_REVISION", str(tmp_path / "UES_REVISION"))
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    return subprocess.run(shlex.split(command), cwd=tmp_path, env=env, capture_output=True, text=True)


@pytest.mark.parametrize("changes", [
    {"installed": OTHER}, {"lock": OTHER}, {"metadata": False},
    {"installed": "not-a-commit"}, {"stamp": OTHER}, {"malformed": True},
])
def test_docker_guard_rejects_unknown_or_conflicting_revision(tmp_path, changes):
    result = build_guard(tmp_path, **changes)
    assert result.returncode != 0
    assert "bad-private-value" not in result.stdout + result.stderr


def test_docker_guard_writes_only_verified_stamp(tmp_path):
    result = build_guard(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "UES_REVISION").read_text() == PIN + "\n"
