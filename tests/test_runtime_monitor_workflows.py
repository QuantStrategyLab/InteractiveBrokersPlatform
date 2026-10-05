import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _job_block(workflow: str, name: str) -> str:
    start = workflow.index(f"  {name}:")
    tail = workflow[start:]
    next_job = re.search(r"(?m)^  [A-Za-z0-9_-]+:\s*$", tail[len(f"  {name}:"):])
    if next_job is None:
        return tail
    return tail[:len(f"  {name}:") + next_job.start()]


def test_execution_report_heartbeat_has_market_neutral_daily_schedule() -> None:
    workflow = (ROOT / ".github/workflows/execution-report-heartbeat.yml").read_text()

    assert 'cron: "20 22 * * *"' in workflow
    assert 'cron: "20 22 * * 1-5"' not in workflow
    assert "RUNTIME_HEARTBEAT_MARKET_AWARE:" in workflow
    assert "RUNTIME_HEARTBEAT_PUBLICATION_GRACE_MINUTES:" in workflow
    assert "RUNTIME_HEARTBEAT_SCHEDULER_LOCATION:" in workflow
    assert "CLOUD_SCHEDULER_MAIN_TIME:" in workflow
    assert "astral-sh/setup-uv@37802adc94f370d6bfd71619e3f0bf239e1f3b78" in workflow
    assert "uv sync --frozen --no-dev" in workflow
    assert "uv run --no-sync python scripts/execution_report_heartbeat.py" in workflow
    assert "python -m pip install" not in workflow


def test_runtime_monitor_workflows_retry_gcp_authentication() -> None:
    workflows = {
        name: (ROOT / ".github/workflows" / name).read_text()
        for name in ("execution-report-heartbeat.yml", "runtime-guard.yml")
    }
    heartbeat_job = _job_block(workflows["execution-report-heartbeat.yml"], "heartbeat")
    runtime_guard = workflows["runtime-guard.yml"]

    for workflow in (heartbeat_job, runtime_guard):
        assert workflow.count("google-github-actions/auth@v3") == 2
        assert "id: gcp_auth_primary" in workflow
        assert "continue-on-error: true" in workflow
        assert "steps.gcp_auth_primary.outcome == 'failure'" in workflow


def _manual_input_block(workflow: str, name: str) -> str:
    import re
    match = re.search(rf"(?ms)^      {name}:\n(.*?)(?=^      [a-z_]+:|^  [a-z_]+:)", workflow)
    assert match is not None, f"missing workflow_dispatch input {name}"
    return match.group(1)


def _evaluate_success_notify_expression(workflow: str, event: str, explicit_input, repository_value) -> str:
    """Evaluate this bounded Actions boolean/string expression without running a workflow."""
    import ast
    import re
    expression = re.search(r"RUNTIME_HEARTBEAT_NOTIFY_ON_SUCCESS: \$\{\{ (.*?) \}\}", workflow).group(1)
    expression = expression.replace("github.event_name", repr(event))
    expression = expression.replace("inputs.notify_on_success", repr(False if explicit_input is None else explicit_input))
    expression = expression.replace("vars.RUNTIME_HEARTBEAT_NOTIFY_ON_SUCCESS", repr(repository_value or ""))
    expression = expression.replace("&&", " and ").replace("||", " or ")
    parsed = ast.parse(expression, mode="eval")
    assert all(isinstance(node, (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.Compare, ast.Eq, ast.NotEq, ast.Constant)) for node in ast.walk(parsed))
    result = eval(compile(parsed, "<offline Actions expression>", "eval"), {"__builtins__": {}}, {})
    return str(result).lower() if isinstance(result, bool) else str(result)


def test_manual_healthy_notify_is_typed_and_explicitly_opt_in() -> None:
    workflow = (ROOT / ".github/workflows/execution-report-heartbeat.yml").read_text()
    block = _manual_input_block(workflow, "notify_on_success")
    assert "type: boolean" in block
    assert "default: false" in block
    assert "required: false" in block
    line = next(line for line in workflow.splitlines() if "RUNTIME_HEARTBEAT_NOTIFY_ON_SUCCESS:" in line)
    assert "inputs.notify_on_success" in line
    assert "github.event.inputs" not in line
    assert "vars.RUNTIME_HEARTBEAT_NOTIFY_ON_SUCCESS" not in line
    assert "github.event_name == 'workflow_dispatch'" in line


def test_schedule_and_manual_default_never_inherit_legacy_success_variable() -> None:
    workflow = (ROOT / ".github/workflows/execution-report-heartbeat.yml").read_text()
    for event in ("schedule", "workflow_dispatch", "repository_dispatch"):
        for explicit_input in (None, False, True):
            for repository_value in (None, "false", "true"):
                actual = _evaluate_success_notify_expression(workflow, event, explicit_input, repository_value)
                expected = "true" if event == "workflow_dispatch" and explicit_input is True else "false"
                assert actual == expected, (event, explicit_input, repository_value, actual)


def test_manual_quiet_control_preserves_existing_alert_and_report_steps() -> None:
    workflow = (ROOT / ".github/workflows/execution-report-heartbeat.yml").read_text()
    alert_block = _manual_input_block(workflow, "fail_workflow_on_alert")
    assert 'default: "true"' in alert_block
    assert "RUNTIME_HEARTBEAT_FAIL_WORKFLOW_ON_ALERT: ${{ inputs.fail_workflow_on_alert || vars.RUNTIME_HEARTBEAT_FAIL_WORKFLOW_ON_ALERT || 'true' }}" in workflow
    assert "uv run --no-sync python scripts/execution_report_heartbeat.py" in workflow
    assert "Publish read-only runtime execution evidence" in workflow
    assert 'cron: "20 22 * * *"' in workflow


def test_ibkr_manual_validation_does_not_opt_into_drills_or_account_facts() -> None:
    workflow = (ROOT / ".github/workflows/execution-report-heartbeat.yml").read_text()
    digest = _manual_input_block(workflow, "send_daily_dry_run_digest")
    account_facts = _manual_input_block(workflow, "account_facts_target")
    assert "type: boolean" in digest and "default: false" in digest
    assert "default: disabled" in account_facts
    assert "always() && (github.event_name == 'schedule' || inputs.send_daily_dry_run_digest)" in workflow
    assert "inputs.account_facts_target == 'primary-live'" in workflow
    assert "inputs.account_facts_target == 'additional-1'" in workflow
