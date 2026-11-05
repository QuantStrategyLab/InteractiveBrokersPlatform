"""Public-entry regressions for candidate health and sanitized initial reads."""
import copy
from unittest import mock

import pytest

from scripts import reconcile_cloud_runtime as rcr
from tests.test_candidate_warmup import candidate, service


def healthy():
    value = candidate()
    value['metadata']['generation'] = 2
    value['status']['observedGeneration'] = 2
    value['status']['conditions'].append({'type': 'ContainerHealthy', 'status': 'True'})
    return value


@pytest.mark.parametrize('health_only', [False, True])
@pytest.mark.parametrize('case', ['flap', 'stale', 'missing', 'malformed', 'generation_drift'])
def test_public_candidate_rejects_flapping_or_stale_generation(health_only, case):
    first = healthy()
    cold = copy.deepcopy(first)
    cold['status']['conditions'].pop()
    if case == 'stale':
        cold['status']['observedGeneration'] = 1
    if case == 'missing':
        cold['metadata'].pop('generation')
    if case == 'malformed':
        cold['metadata']['generation'] = True
    writes, revision_reads = [], []

    def run(args, **kwargs):
        if 'update-traffic' in args:
            writes.append(args)
            return ''
        if 'services' in args:
            current = service()
            if writes:
                current['status']['traffic'] = [{'revisionName': 'unit-service-new', 'percent': 100}]
            return current
        if 'list' in args:
            return [cold]
        revision_reads.append(args)
        if len(revision_reads) == 1:
            return cold
        value = healthy()
        if case == 'generation_drift':
            value['metadata']['generation'] = 3
        if case == 'flap' and len(revision_reads) >= 3:
            value['status']['conditions'][-1]['status'] = 'False'
        return value

    with mock.patch.object(rcr, '_run', side_effect=run), \
         mock.patch.object(rcr.time, 'sleep'), \
         mock.patch('scripts.runtime_target_continuity.verify_target_continuity'):
        with pytest.raises((rcr.ReconcileError, ValueError)):
            rcr.ensure_latest_traffic(project='<PROJECT_ID>', region='unit-region',
                targets=[rcr.RuntimeTarget('unit-service')], expected_commit='abc',
                dry_run=False, health_only=health_only)
    assert not writes


@pytest.mark.parametrize('failing_read', ['services', 'revisions'])
@pytest.mark.parametrize('error', ['provider', 'json', 'shape'])
def test_initial_health_reads_suppress_diagnostics(failing_read, error):
    sentinel = 'private-diagnostic-sentinel'
    timeouts = []

    def run(args, **kwargs):
        timeouts.append(kwargs.get('timeout'))
        if failing_read in args:
            if error == 'provider':
                raise rcr.ReconcileError(sentinel)
            if error == 'json':
                raise ValueError(sentinel)
            return {'status': [sentinel]}
        return service() if 'services' in args else healthy()

    with mock.patch.object(rcr, '_run', side_effect=run):
        with pytest.raises(rcr.ReconcileError) as caught:
            rcr.ensure_latest_traffic(project='<PROJECT_ID>', region='unit-region',
                targets=[rcr.RuntimeTarget('unit-service')], expected_commit='abc',
                dry_run=False, health_only=True)
    assert sentinel not in str(caught.value)
    assert caught.value.__suppress_context__
    assert all(value is not None and 0 < value <= 30 for value in timeouts)



@pytest.mark.parametrize('failing_read', ['services', 'revisions'])
def test_health_cli_stderr_never_contains_initial_provider_diagnostics(tmp_path, failing_read):
    import json
    from pathlib import Path
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    fake = tmp_path / 'gcloud'
    fake.write_text(f"#!{sys.executable}\nimport json,sys\n"
                    f"if {failing_read!r} in sys.argv:\n"
                    " print('private-diagnostic-sentinel',file=sys.stderr)\n sys.exit(1)\n"
                    f"print(json.dumps({service()!r}))\n")
    fake.chmod(0o700)
    plan = json.dumps({'targets': [{'service_name': 'unit-service'}]})
    result = subprocess.run([sys.executable, str(root / 'scripts/reconcile_cloud_runtime.py'),
                             '--platform=ibkr', '--project=<PROJECT_ID>', '--region=unit-region',
                             '--expected-commit=abc', '--verify-candidate-health-only'],
                            env={'PATH': str(tmp_path), 'PYTHONPATH': str(root),
                                 'SYNC_PLAN_JSON': plan}, capture_output=True, text=True,
                            timeout=10)
    assert result.returncode == 1
    assert result.stderr.strip() == 'error: candidate warmup cloud read failed: COMMAND_FAILED'
    assert 'private-diagnostic-sentinel' not in result.stdout + result.stderr
