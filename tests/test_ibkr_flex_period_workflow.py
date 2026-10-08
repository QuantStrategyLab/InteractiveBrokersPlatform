from pathlib import Path


def test_manual_flex_publisher_cannot_run_from_schedule_branch_or_rerun():
    workflow = Path(".github/workflows/publish-ibkr-flex-period-return.yml").read_text()
    assert "workflow_dispatch:" in workflow and "default: false" in workflow
    assert "inputs.publish_once" in workflow
    assert "vars.IBKR_PERIOD_RETURN_PUBLISH_ENABLED == 'true'" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow and "github.run_attempt == 1" in workflow
    assert "github.repository == 'QuantStrategyLab/InteractiveBrokersPlatform'" in workflow
    assert "cancel-in-progress: false" in workflow
    for unsafe in ("schedule:", "workflow_run:", "id-token:", "google-github-actions/auth", "gcloud", "upload-artifact", "TELEGRAM", "heartbeat", "Gateway", "scheduler"):
        assert unsafe not in workflow


def test_cloud_caller_uses_private_target_inputs_and_existing_locked_ci():
    workflow = Path(".github/workflows/publish-ibkr-flex-period-return.yml").read_text()
    assert "uv sync --frozen --no-dev" in workflow
    assert "persist-credentials: false" in workflow
    assert workflow.count("python -m scripts.publish_ibkr_flex_period_return") == 1
    for key in ("IBKR_PERIOD_RETURN_TARGET_ID", "IBKR_PERIOD_RETURN_SOURCE_BINDING_ID",
                "IBKR_PERIOD_RETURN_ACCOUNT_SCOPE", "IBKR_PERIOD_RETURN_ACCOUNT_KEY",
                "IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON", "IBKR_FLEX_TOKEN", "IBKR_FLEX_QUERY_ID",
                "IBKR_ACCOUNT_FACTS_SYNC_TOKEN"):
        assert f"{key}: ${{{{ secrets.{key} }}}}" in workflow
        assert f"vars.{key}" not in workflow
    assert "REFERENCE" not in workflow and "SYNC_URL" not in workflow
    ci = Path(".github/workflows/ci.yml").read_text()
    assert "python -m pytest -q tests" in ci and "ruff check --exclude external ." in ci
