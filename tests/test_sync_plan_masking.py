"""Run the real plan handoff with synthetic private bindings, without cloud calls."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/sync-cloud-run-env-protected.yml'


def _step(name):
    section = WORKFLOW.read_text().split(f'      - name: {name}\n', 1)[1]
    body = section.split('\n      - name:', 1)[0].split('        run: |\n', 1)[1]
    return '\n'.join(line[10:] for line in body.splitlines())


def _masks(stdout):
    prefix = '::add-mask::'
    return [line[len(prefix):].replace('%0A', '\n').replace('%0D', '\r').replace('%25', '%')
            for line in stdout.splitlines() if line.startswith(prefix)]


@pytest.mark.parametrize('invalid', [False, True])
def test_plan_masks_private_leaves_before_step_output(tmp_path, invalid):
    private = ['U00000042', 'fake-account-alias', 'fake-project-id', '123450000042',
               'fake-deployer@fake-project-id.iam.gserviceaccount.com', 'fake-pool', 'fake-provider']
    provider = f'projects/{private[3]}/locations/global/workloadIdentityPools/{private[5]}/providers/{private[6]}'
    target = {'service_name': 'fake-service', 'env': {'RUNTIME_TARGET_JSON': json.dumps({
        'account_id': private[0], 'account_alias': private[1], 'project_id': private[2],
        'project_number': int(private[3]), 'service_account': private[4], 'provider': provider})}}
    if invalid:
        target['env']['RUNTIME_TARGET_JSON'] = '{broken-private-binding'
    plan = {'mode': 'per_service', 'targets': [target]}
    (tmp_path / 'python').symlink_to(sys.executable)
    fake_uv = tmp_path / 'uv'
    fake_uv.write_text('#!/bin/sh\nprintf "%s" "$FAKE_PLAN"\n')
    fake_uv.chmod(0o700)
    output = tmp_path / 'output'
    envfile = tmp_path / 'env'
    result = subprocess.run(['bash', '-euo', 'pipefail', '-c', _step('Resolve admissible Cloud Run targets')],
                            cwd=ROOT, env={**os.environ, 'PATH': f'{tmp_path}:{os.environ["PATH"]}',
                            'FAKE_PLAN': json.dumps(plan), 'GITHUB_OUTPUT': str(output),
                            'GITHUB_ENV': str(envfile), 'INPUT_CONFIGURED_SERVICE': 'fake-service',
                            'INPUT_INITIAL_STATE_SOURCE_RUN_ID': '', 'QSL_DIGEST_ROUTINE_ORDERS': '',
                            'QSL_ROUTINE_NOTIFICATION_MODE': ''}, text=True, capture_output=True)
    if invalid:
        assert result.returncode != 0
        assert not output.exists()
        assert '{broken-private-binding' not in result.stdout + result.stderr
        return
    assert result.returncode == 0
    masks = _masks(result.stdout)
    rendered = output.read_text()
    for value in sorted(masks, key=len, reverse=True):
        rendered = rendered.replace(value, '***')
    assert all(value not in rendered for value in private)
    assert masks
    # The raw handoff remains machine-readable and unchanged for execution.
    assert json.loads(output.read_text().splitlines()[1]) == plan
    assert all(line.startswith('::add-mask::') for line in result.stdout.splitlines())


@pytest.mark.parametrize('value', ['broken-json', '{"account_id":'])
def test_invalid_identity_json_fails_closed_without_echo(value):
    result = subprocess.run([sys.executable, 'scripts/mask_sync_identifiers.py'], cwd=ROOT,
                            env={**os.environ, 'RUNTIME_TARGET_JSON': value}, text=True, capture_output=True)
    assert result.returncode != 0
    assert value not in result.stdout + result.stderr


def test_environment_masks_components_and_escapes_commands():
    from scripts.mask_sync_identifiers import mask_identifiers
    import io
    output = io.StringIO()
    mask_identifiers({'account_id': 'fake%account\n::warning::injected',
                      'provider': 'projects/123450000042/locations/global/workloadIdentityPools/fake-pool/providers/fake-provider'}, output)
    lines = output.getvalue().splitlines()
    assert all(line.startswith('::add-mask::') for line in lines)
    assert '::add-mask::fake%25account%0A::warning::injected' in lines
    assert {'123450000042', 'fake-pool', 'fake-provider'} <= set(_masks(output.getvalue()))


def test_cloud_masks_run_before_authentication_and_plan_output():
    text = WORKFLOW.read_text()
    assert text.index('python scripts/mask_sync_identifiers.py') < text.index('- name: Authenticate to Google Cloud')
    block = _step('Resolve admissible Cloud Run targets')
    assert block.index('mask_identifiers(plan)') < block.index('handle.write("sync_plan_json')
