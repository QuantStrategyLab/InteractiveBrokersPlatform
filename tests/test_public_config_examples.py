from pathlib import Path
from urllib.parse import urlparse


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_env_example_uses_portable_execution_report_gcs_uri():
    env_example = (REPOSITORY_ROOT / ".env.example").read_text(encoding="utf-8")
    uri = next(
        line.partition("=")[2]
        for line in env_example.splitlines()
        if line.startswith("EXECUTION_REPORT_GCS_URI=")
    )
    parsed = urlparse(uri)

    assert parsed.scheme == "gs"
    assert parsed.netloc == "your-bucket"
    assert parsed.path not in {"", "/"}


def test_runtime_rollout_uses_portable_repository_path():
    rollout = (REPOSITORY_ROOT / "docs" / "ibkr_runtime_rollout.md").read_text(
        encoding="utf-8"
    )

    assert "/Users/" not in rollout
    assert "cd /path/to/InteractiveBrokersPlatform" in rollout
