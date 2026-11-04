"""Regression checks for runner-visible headers and encoded plan handoffs."""
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/sync-cloud-run-env-protected.yml'


def test_infrastructure_headers_use_premasked_secret_bindings():
    text = WORKFLOW.read_text()
    names = ('GCP_PROJECT_ID', 'GCP_WORKLOAD_IDENTITY_PROVIDER',
             'GCP_WORKLOAD_IDENTITY_SERVICE_ACCOUNT', 'GCP_RUNTIME_SERVICE_ACCOUNT',
             'GCP_SCHEDULER_SERVICE_ACCOUNT')
    for name in names:
        line = next(line for line in text.splitlines() if line.startswith(f'  {name}:'))
        assert 'secrets.' in line
        assert 'vars.' not in line
    for line in text.splitlines():
        if 'SERVICE_ACCOUNT:' in line or 'GCP_PROJECT_ID:' in line:
            assert 'vars.' not in line


def test_raw_plan_never_crosses_public_step_headers_or_outputs():
    text = WORKFLOW.read_text()
    assert 'steps.strategy_requirements.outputs.sync_plan_json' not in text
    assert 'sync_plan_json<<' not in text
    assert 'IBKR_INITIAL_SYNC_PLAN_PATH=' in text
    assert 'path.chmod(0o600)' in text


def test_required_secret_name_manifest_contains_no_values():
    manifest = json.loads((ROOT / 'config/cloud_run_sync_secret_names.json').read_text())
    assert set(manifest) == {'required_secret_names'}
    assert {'GCP_PROJECT_ID', 'GCP_WORKLOAD_IDENTITY_PROVIDER',
            'GCP_WORKLOAD_IDENTITY_SERVICE_ACCOUNT'} <= set(manifest['required_secret_names'])
