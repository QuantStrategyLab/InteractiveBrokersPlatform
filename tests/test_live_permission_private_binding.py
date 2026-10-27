from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import textwrap

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/probe-live-permission-protected.yml'


def _selection_script() -> str:
    selection = WORKFLOW.read_text().split('      - name: Select validated target\n', 1)[1]
    block = selection.split('        run: |\n', 1)[1].split('\n      - uses:', 1)[0]
    return textwrap.dedent(block)


def _target(service='synthetic-private-service'):
    return dict(service=service, include_reconciliation=True,
                runtime_target=dict(service_name=service, strategy_profile='soxl_soxx_trend_income',
                                    execution_mode='live'))


def _select(tmp_path, entries):
    output = tmp_path/'output'
    result = subprocess.run(['bash', '-c', _selection_script()], cwd=ROOT,
        env={**os.environ, 'CLOUD_RUN_SERVICE_TARGETS_JSON': json.dumps({'targets': entries}),
             'REQUESTED_TARGET': 'soxl_soxx_trend_income', 'GITHUB_OUTPUT': str(output),
             'PYTHONPATH': str(ROOT)}, capture_output=True, text=True, check=False)
    return result, output.read_text() if output.exists() else ''


def test_actual_workflow_selects_and_masks_private_service(tmp_path):
    result, output = _select(tmp_path, [_target()])
    assert result.returncode == 0, result.stderr
    assert 'service=synthetic-private-service\n' in output
    assert '::add-mask::synthetic-private-service' in result.stdout
    assert 'region=us-central1\n' in output
    assert 'jq -r .service' not in _selection_script()


@pytest.mark.parametrize('entries', [[], [_target(), _target()], [dict(_target(), service='different-service')]])
def test_missing_ambiguous_or_mismatched_private_binding_stops_selection(tmp_path, entries):
    result, output = _select(tmp_path, entries)
    assert result.returncode != 0
    assert 'service=' not in output
    assert 'synthetic-private-service' not in result.stdout + result.stderr


def test_wrapper_registers_inventory_and_preserves_permission_probe_contract():
    wrapper = (ROOT/'.github/workflows/probe-live-permission.yml').read_text()
    protected = WORKFLOW.read_text()
    assert 'uses: ./.github/workflows/probe-live-permission-protected.yml' in wrapper
    assert 'CLOUD_RUN_SERVICE_TARGETS_JSON: ${{ vars.CLOUD_RUN_SERVICE_TARGETS_JSON || secrets.CLOUD_RUN_SERVICE_TARGETS_JSON }}' in wrapper
    assert 'CLOUD_RUN_SERVICE_TARGETS_JSON: ${{ secrets.CLOUD_RUN_SERVICE_TARGETS_JSON }}' in protected
    assert 'workflow_call:' in protected and 'workflow_dispatch:' not in protected
    assert 'ibkr-live-permission-${{ inputs.target }}' in wrapper
    assert protected.index('name: Select validated target') < protected.index('uses: google-github-actions/auth')
    for contract in ('$env.RUNTIME_TARGET_ENABLED == "false"', '$target.live_continuity.state == "ACTIVE_LKG"',
                     'X-QSL-Permission-Probe-Expires-At', '--max-retry-attempts=0', '--max-retry-duration=0s',
                     'select(.scheduler_job_sha256 == $sha)', 'select(.resource.labels.revision_name == $revision)',
                     'name: Delete temporary Scheduler job'):
        assert contract in protected


def test_malformed_private_runtime_json_fails_without_echoing_its_value(tmp_path):
    workflow = WORKFLOW.read_text().split('      - name: Verify disabled live target and resolve internal URL\n', 1)[1]
    script = textwrap.dedent(workflow.split('        run: |\n', 1)[1].split('\n      - name:', 1)[0])
    native = tmp_path/'gcloud'
    service = dict(status=dict(latestReadyRevisionName='synthetic-revision',
        traffic=[dict(percent=100, revisionName='synthetic-revision')], url='https://synthetic.invalid'))
    revision = dict(spec=dict(containers=[dict(env=[dict(name='RUNTIME_TARGET_JSON', value='SYNTHETIC_PRIVATE_VALUE{')])]))
    native.write_text('#!/usr/bin/env python3\nimport json, sys\nprint(json.dumps(' +
        repr(service) + ' if sys.argv[2] == "services" else ' + repr(revision) + '))\n')
    native.chmod(0o700)
    result = subprocess.run(['bash', '-c', script], cwd=ROOT,
        env={**os.environ, 'PATH': str(tmp_path)+os.pathsep+os.environ['PATH'],
             'GCP_PROJECT_ID': 'synthetic-project', 'SERVICE': 'synthetic-service',
             'REGION': 'us-central1', 'PROFILE': 'soxl_soxx_trend_income',
             'GITHUB_OUTPUT': str(tmp_path/'output')}, capture_output=True, text=True)
    assert result.returncode != 0
    assert result.stderr.strip() == 'Disabled live target configuration could not be verified.'
    assert 'SYNTHETIC_PRIVATE_VALUE' not in result.stdout + result.stderr
