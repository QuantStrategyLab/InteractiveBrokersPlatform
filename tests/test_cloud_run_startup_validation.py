from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_production_startup_validation_is_a_ci_and_image_build_gate():
    command = "python scripts/validate_cloud_run_startup.py"
    dockerfile = (ROOT / "Dockerfile").read_text()
    ci_workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert command in dockerfile
    assert "Validate production Cloud Run startup" in ci_workflow
    assert f"uv run --no-sync {command}" in ci_workflow
