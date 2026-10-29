"""Offline structural checks; never load runtime configuration or execute workflows."""
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1] / ".github/workflows"
PROTECTED = ("CLOUD_RUN_SERVICE_TARGETS_JSON", "RUNTIME_TARGET_JSON")
WORKFLOWS = (
    "runtime-guard", "execution-report-heartbeat", "sync-cloud-run-env",
    "env-sync-drift-check", "notify-route-check", "collect-reconciliation-evidence",
)

# The route observer forwards these existing same-value settings as reusable
# secrets so runner headers mask them; it does not create repository secrets.
# This explicit contract is limited to this caller's six new fields.
NOTIFY_ROUTE_ADDITIONAL_SECRET_CONTRACTS = {
    "GCP_PROJECT_ID": (True, "vars.IBKR_GCP_PROJECT_ID"),
    "GCP_WORKLOAD_IDENTITY_PROVIDER": (
        True, "'projects/303168642265/locations/global/workloadIdentityPools/github-actions/providers/github-main'",
    ),
    "GCP_WORKLOAD_IDENTITY_SERVICE_ACCOUNT": (
        True, "vars.IBKR_DEPLOY_SERVICE_ACCOUNT",
    ),
    "RUNTIME_GUARD_CLOUD_RUN_SERVICES": (False, "vars.RUNTIME_GUARD_CLOUD_RUN_SERVICES"),
    "CLOUD_RUN_REGION": (False, "vars.CLOUD_RUN_REGION"),
    "TELEGRAM_TOKEN_SECRET_NAME": (False, "vars.TELEGRAM_TOKEN_SECRET_NAME"),
}


RECONCILIATION_BINDING_SECRETS = frozenset({
    "IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON", "IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON",
    "IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON", "IBKR_ACCOUNT_FACTS_PROJECT_ID",
    "IBKR_ACCOUNT_FACTS_SERVICE_NAME", "IBKR_ACCOUNT_FACTS_RUNTIME_REVISION",
    "IBKR_ACCOUNT_FACTS_ACCOUNT_SCOPE", "IBKR_ACCOUNT_FACTS_DEPLOYMENT_SELECTOR",
})


def _assert_reconciliation_binding_secret_contract(key: str, declared: str, passed: str) -> None:
    assert f"      {key}:\n        required: false" in declared
    expression = f"(inputs.inspect_flex_runtime_binding_only || inputs.inspect_recovery_receipt_only) && secrets.{key} || ''"
    assert f"      {key}: ${{{{ {expression} }}}}" in passed


def _assert_notify_route_additional_secret_contract(key: str, declared: str, passed: str) -> None:
    required, expression = NOTIFY_ROUTE_ADDITIONAL_SECRET_CONTRACTS[key]
    assert f"      {key}:\n        required: {str(required).lower()}" in declared
    assert f"      {key}: ${{{{ {expression} }}}}" in passed


def _block(text: str, key: str, indent: int) -> str:
    match = re.search(rf"(?m)^{' ' * indent}{re.escape(key)}:\s*\n", text)
    assert match is not None, key
    tail = text[match.end():]
    end = re.search(rf"(?m)^\S|^ {{1,{indent}}}\S" if indent else r"(?m)^\S", tail)
    return tail[:end.start()] if end else tail


def _jobs(text: str) -> dict[str, str]:
    jobs = _block(text, "jobs", 0)
    return {key: _block(jobs, key, 2) for key in re.findall(r"(?m)^  ([\w-]+):\s*$", jobs)}


def _unsafe_json_env(text: str) -> list[str]:
    """Inspect actual env mappings, excluding run blocks and reusable secrets maps."""
    unsafe = []
    env_indent = None
    literal_indent = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if literal_indent is not None:
            if indent > literal_indent:
                continue
            literal_indent = None
        if env_indent is not None and indent <= env_indent:
            env_indent = None
        if re.search(r":\s*[|>][-+]?\s*$", line):
            literal_indent = indent
        if line.strip() == "env:":
            env_indent = indent
        elif env_indent is not None and any(f"vars.{key}" in line for key in PROTECTED):
            unsafe.append(line.strip())
    return unsafe


@pytest.mark.parametrize("name", WORKFLOWS)
def test_json_crosses_secret_boundary_before_any_execution(name: str) -> None:
    caller = (ROOT / f"{name}.yml").read_text()
    callee = (ROOT / f"{name}-protected.yml").read_text()
    calls = [job for job in _jobs(caller).values()
             if f"uses: ./.github/workflows/{name}-protected.yml" in job]
    assert len(calls) == 1
    call = calls[0]
    assert "steps:" not in call and "runs-on:" not in call and "    env:" not in call
    assert "workflow_call:" in callee
    assert "workflow_dispatch:" not in callee and "schedule:" not in callee
    # Only the original entry point owns run concurrency; no nested self-cancellation.
    assert "concurrency:" not in callee
    declared = _block(callee, "secrets", 4)
    passed = _block(call, "secrets", 4)
    referenced = set(re.findall(r"secrets\.([A-Z][A-Z0-9_]*)", _block(callee, "jobs", 0)))
    assert referenced
    for key in referenced:
        if name == "notify-route-check" and key in NOTIFY_ROUTE_ADDITIONAL_SECRET_CONTRACTS:
            _assert_notify_route_additional_secret_contract(key, declared, passed)
        elif name == "collect-reconciliation-evidence" and key in RECONCILIATION_BINDING_SECRETS:
            _assert_reconciliation_binding_secret_contract(key, declared, passed)
        else:
            assert f"      {key}:\n        required: false" in declared
            value = f"vars.{key} || secrets.{key}" if key in PROTECTED else f"secrets.{key}"
            assert f"      {key}: ${{{{ {value} }}}}" in passed
    for key in PROTECTED:
        assert f"vars.{key}" not in callee
        if key in referenced:
            assert f"{key}: ${{{{ secrets.{key} }}}}" in callee
    assert not _unsafe_json_env(caller)
    assert not _unsafe_json_env(callee)


@pytest.mark.parametrize("key", NOTIFY_ROUTE_ADDITIONAL_SECRET_CONTRACTS)
@pytest.mark.parametrize("mutation", ("required", "source"))
def test_route_additional_contract_rejects_changed_required_or_source(key: str, mutation: str) -> None:
    required, expression = NOTIFY_ROUTE_ADDITIONAL_SECRET_CONTRACTS[key]
    declared = f"      {key}:\n        required: {str(required).lower()}\n"
    passed = f"      {key}: ${{{{ {expression} }}}}\n"
    _assert_notify_route_additional_secret_contract(key, declared, passed)
    if mutation == "required":
        declared = f"      {key}:\n        required: {str(not required).lower()}\n"
    else:
        passed = f"      {key}: ${{{{ vars.UNRELATED_PRIVATE_SETTING }}}}\n"
    with pytest.raises(AssertionError):
        _assert_notify_route_additional_secret_contract(key, declared, passed)


def test_all_workflows_reject_variable_backed_json_in_env() -> None:
    for path in ROOT.glob("*.yml"):
        assert not _unsafe_json_env(path.read_text()), path.name


@pytest.mark.parametrize("indent", (0, 4, 8))
def test_detector_catches_workflow_job_and_step_env_regressions(indent: int) -> None:
    fixture = " " * indent + "env:\n" + " " * (indent + 2)
    fixture += "RUNTIME_TARGET_JSON: ${{ vars.RUNTIME_TARGET_JSON || secrets.RUNTIME_TARGET_JSON }}\n"
    assert len(_unsafe_json_env(fixture)) == 1


def test_detector_accepts_secret_boundary_and_ignores_literal_script_text() -> None:
    fixture = """jobs:
  caller:
    uses: ./.github/workflows/synthetic.yml
    secrets:
      RUNTIME_TARGET_JSON: ${{ vars.RUNTIME_TARGET_JSON || secrets.RUNTIME_TARGET_JSON }}
  executor:
    env:
      RUNTIME_TARGET_JSON: ${{ secrets.RUNTIME_TARGET_JSON }}
    steps:
      - run: |
          env:
            RUNTIME_TARGET_JSON: vars.RUNTIME_TARGET_JSON
"""
    assert not _unsafe_json_env(fixture)


def test_reconciliation_keeps_both_inventory_consumers_behind_one_boundary() -> None:
    callee = (ROOT / "collect-reconciliation-evidence-protected.yml").read_text()
    jobs = _jobs(callee)
    assert set(jobs) == {"resolve-matrix", "flex-runtime-binding", "collect"}
    assert "needs: resolve-matrix" in jobs["collect"]
    assert "matrix: ${{ fromJSON(needs.resolve-matrix.outputs.matrix) }}" in jobs["collect"]
    for job in jobs.values():
        assert "CLOUD_RUN_SERVICE_TARGETS_JSON: ${{ secrets.CLOUD_RUN_SERVICE_TARGETS_JSON }}" in job
    assert "if: ${{ !inputs.inspect_flex_runtime_binding_only && !inputs.inspect_recovery_receipt_only }}" in jobs["collect"]
    metadata = jobs["flex-runtime-binding"]
    assert "inputs.inspect_flex_runtime_binding_only" in metadata
    assert "diagnose_ibkr_flex_runtime_binding" in metadata
    for unsafe in ("IBKR_FLEX_TOKEN", "IBKR_FLEX_QUERY_ID", "scheduler jobs", "storage cat"):
        assert unsafe not in metadata
    # The event identity remains the original caller, including provenance validation.
    assert '.github/workflows/collect-reconciliation-evidence.yml@refs/heads/main' in jobs['collect']


def test_existing_digest_secret_boundary_is_preserved() -> None:
    emit = (ROOT / "digest-candidates-emit.yml").read_text()
    for key in PROTECTED:
        assert f"vars.{key}" not in emit
        assert f"{key}: ${{{{ secrets.{key} }}}}" in emit
    for name in ("digest-candidates-daily", "execution-report-heartbeat"):
        caller = (ROOT / f"{name}.yml").read_text()
        assert "uses: ./.github/workflows/digest-candidates-emit.yml" in caller


@pytest.mark.parametrize("name", WORKFLOWS)
def test_reusable_inputs_are_forwarded_without_changing_types_or_values(name: str) -> None:
    caller = (ROOT / f"{name}.yml").read_text()
    callee = (ROOT / f"{name}-protected.yml").read_text()
    call = next(job for job in _jobs(caller).values()
                if f"uses: ./.github/workflows/{name}-protected.yml" in job)
    referenced = set(re.findall(r"inputs\.([a-z_]+)", _block(callee, "jobs", 0)))
    if not referenced:
        assert "    with:" not in call
        return
    manual = _block(_block(caller, "workflow_dispatch", 2), "inputs", 4)
    declared = _block(_block(callee, "workflow_call", 2), "inputs", 4)
    for key in referenced:
        boolean_schedule_inputs = {"notify_on_success", "send_daily_dry_run_digest"}
        expression = f"inputs.{key} == true" if name == "execution-report-heartbeat" and key in boolean_schedule_inputs else f"inputs.{key}"
        assert f"      {key}: ${{{{ {expression} }}}}" in _block(call, "with", 4)
        source_type = re.search(r"type: (\w+)", _block(manual, key, 6)).group(1)
        expected_type = "string" if source_type == "choice" else source_type
        assert f"type: {expected_type}" in _block(declared, key, 6)
        # Dispatch defaults are applied by the original entry point, not imposed on schedules.
        assert "default:" not in _block(declared, key, 6)


@pytest.mark.parametrize("key", RECONCILIATION_BINDING_SECRETS)
@pytest.mark.parametrize("mutation", ("default_on", "source"))
def test_reconciliation_binding_secret_rejects_default_exposure_or_source_change(key, mutation):
    declared = f"      {key}:\n        required: false\n"
    expression = f"secrets.{key}" if mutation == "default_on" else f"inputs.inspect_flex_runtime_binding_only && vars.{key} || ''"
    passed = f"      {key}: ${{{{ {expression} }}}}\n"
    with pytest.raises(AssertionError):
        _assert_reconciliation_binding_secret_contract(key, declared, passed)
