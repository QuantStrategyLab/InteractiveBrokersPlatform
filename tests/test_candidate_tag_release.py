"""Fake-only lifecycle regressions; no cloud or application requests."""
import copy
import importlib
import json
from pathlib import Path
from unittest import mock

import pytest

from tests.test_candidate_warmup import candidate

COMMIT = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64


def api():
    return importlib.import_module('scripts.cloud_run_candidate_warmup')


def plan(profile='russell_top50_leader_rotation'):
    return {'targets': [{'service_name': 'unit-service', 'strategy_profile': profile}]}


class Cloud:
    def __init__(self, failure=None):
        self.failure = failure
        self.commands = []
        self.revisions = {}
        self.service = {'metadata': {'uid': 'service-uid', 'resourceVersion': '1'},
                        'spec': {'template': {'metadata': {'annotations': {}}, 'spec': {}}},
                        'status': {'conditions': [{'type': 'Ready', 'status': 'True'}],
                                   'latestCreatedRevisionName': 'unit-service-old',
                                   'traffic': [{'revisionName': 'unit-service-old', 'percent': 100}]}}
        old = candidate()
        old['metadata']['name'] = 'unit-service-old'
        old['metadata']['annotations'] = {}
        self.revisions['unit-service-old'] = old

    def __call__(self, command):
        self.commands.append(command)
        assert all(value not in command for value in ('scheduler', 'secrets', 'get-iam-policy', 'set-iam-policy'))
        assert not any('env-vars' in value or value.startswith('--min=') for value in command)
        if 'logging' in command:
            return []
        if 'describe' in command:
            if 'services' in command:
                if self.failure == 'drift' and len(self.revisions) == 2:
                    self.service['metadata']['resourceVersion'] = 'external'
                return copy.deepcopy(self.service)
            return copy.deepcopy(self.revisions[command[4]])
        if 'deploy' in command or 'update' in command:
            warm = 'deploy' in command
            if not warm and self.failure == 'reset_command':
                raise RuntimeError('provider-private-detail')
            name = 'unit-service-warm' if warm else 'unit-service-reset'
            value = candidate()
            value['metadata'].update(name=name, uid=name, labels={'commit-sha': COMMIT, 'github-run-id': '123'})
            value['metadata']['annotations']['autoscaling.knative.dev/minScale'] = '1' if warm else '0'
            value['status']['imageDigest'] = DIGEST
            value['status']['conditions'].append({'type': 'ContainerHealthy', 'status': 'True'})
            self.revisions[name] = value
            self.service['status']['latestCreatedRevisionName'] = name
            self.service['spec']['template']['metadata']['annotations'] = copy.deepcopy(value['metadata']['annotations'])
            if warm:
                self.service['status']['traffic'].append({'revisionName': name, 'tag': 'cand-aaaaaaaa', 'percent': 0})
            self.service['metadata']['resourceVersion'] = str(len(self.commands))
            if warm and self.failure == 'deploy_abort':
                raise InterruptedError('interrupted deploy')
        elif 'update-traffic' in command:
            remove = next((v.split('=', 1)[1] for v in command if v.startswith('--remove-tags=')), None)
            if remove:
                if self.failure != 'tag_sticks':
                    self.service['status']['traffic'] = [r for r in self.service['status']['traffic'] if r.get('tag') != remove]
                    if not self.service['status']['traffic']:
                        self.service['status']['traffic'] = [{'revisionName': 'unit-service-warm', 'percent': 100}]
            else:
                target = next(v.split('=', 1)[1].rsplit('=', 1)[0] for v in command if v.startswith('--to-revisions='))
                tags = [dict(r, percent=0) for r in self.service['status']['traffic'] if r.get('tag')]
                self.service['status']['traffic'] = [{'revisionName': target, 'percent': 100}] + tags
            self.service['metadata']['resourceVersion'] = str(len(self.commands))
        else:
            pytest.fail('unexpected command')
        return {}


def release(tmp_path, cloud):
    return api().Release(project='<PROJECT_ID>', region='unit-region', service='unit-service',
                         commit=COMMIT, digest=DIGEST, run_id='123',
                         deploy_args=['--image=fake@' + DIGEST, '--no-traffic', '--deploy-health-check'],
                         state_path=tmp_path / 'state.json', cloud=cloud, no_traffic_verify=lambda: None)


def test_tag_minimum_order_and_replacement_restore(tmp_path):
    cloud = Cloud()
    events = []
    def health(value, **kwargs):
        events.append(value['metadata']['name'])
        assert not any('--to-revisions=' + value['metadata']['name'] + '=100' in c for c in cloud.commands)
    with mock.patch('scripts.reconcile_cloud_runtime._verify_deploy_candidate_with_wait', side_effect=health):
        result = release(tmp_path, cloud).execute()
    assert result == 'unit-service-reset'
    deploy = next(c for c in cloud.commands if 'deploy' in c)
    assert '--tag=cand-aaaaaaaa' in deploy and '--min-instances=1' in deploy and '--no-traffic' in deploy
    shift = next(i for i,c in enumerate(cloud.commands) if '--to-revisions=unit-service-warm=100' in c)
    remove = next(i for i,c in enumerate(cloud.commands) if '--remove-tags=cand-aaaaaaaa' in c)
    reset = next(i for i,c in enumerate(cloud.commands) if '--min-instances=0' in c)
    final = next(i for i,c in enumerate(cloud.commands) if '--to-revisions=unit-service-reset=100' in c)
    assert shift < remove < reset < final
    assert events == ['unit-service-warm', 'unit-service-reset']
    assert cloud.revisions['unit-service-warm']['metadata']['annotations']['autoscaling.knative.dev/minScale'] == '1'
    assert json.loads((tmp_path / 'state.json').read_text())['status'] == 'restored'


@pytest.mark.parametrize('failure', ['tag_sticks', 'reset_command'])
def test_cleanup_assertions_fail_closed_and_print_readback(tmp_path, failure, capsys):
    cloud = Cloud(failure)
    with mock.patch('scripts.reconcile_cloud_runtime._verify_deploy_candidate_with_wait'):
        with pytest.raises(RuntimeError, match='cleanup'):
            release(tmp_path, cloud).execute()
    output = capsys.readouterr().out
    assert 'restore' in output and 'conditions' in output
    assert 'provider-private-detail' not in output
    assert json.loads((tmp_path / 'state.json').read_text())['status'] == 'cleanup_failed'
    assert any('--min-instances=0' in c for c in cloud.commands)


@pytest.mark.parametrize('failure', ['deploy_abort', 'health'])
def test_abort_or_health_failure_still_removes_tag_and_resets_without_shift(tmp_path, failure):
    cloud = Cloud('deploy_abort' if failure == 'deploy_abort' else None)
    with mock.patch('scripts.reconcile_cloud_runtime._verify_deploy_candidate_with_wait', side_effect=ValueError('health missing')):
        with pytest.raises((ValueError, InterruptedError)):
            release(tmp_path, cloud).execute()
    assert any('--remove-tags=cand-aaaaaaaa' in c for c in cloud.commands)
    assert any('--min-instances=0' in c for c in cloud.commands)
    assert not any(any(v.startswith('--to-revisions=') for v in c) for c in cloud.commands)
    assert cloud.service['status']['traffic'] == [{'revisionName': 'unit-service-old', 'percent': 100}]


def test_reset_health_failure_never_shifts_unhealthy_replacement(tmp_path):
    cloud = Cloud()
    def health(value, **kwargs):
        if value['metadata']['name'].endswith('reset'):
            raise ValueError('probe failed')
    with mock.patch('scripts.reconcile_cloud_runtime._verify_deploy_candidate_with_wait', side_effect=health):
        with pytest.raises(RuntimeError, match='cleanup'):
            release(tmp_path, cloud).execute()
    assert not any('--to-revisions=unit-service-reset=100' in c for c in cloud.commands)
    assert json.loads((tmp_path / 'state.json').read_text())['status'] == 'cleanup_failed'


def test_restart_refuses_existing_state_without_cloud_calls(tmp_path):
    (tmp_path / 'state.json').write_text('{}')
    cloud = Cloud()
    with pytest.raises(RuntimeError, match='restart'):
        release(tmp_path, cloud).execute()
    assert not cloud.commands


def test_stale_service_refuses_promotion_and_cleanup_mutation(tmp_path):
    cloud = Cloud()
    def drift(*args, **kwargs):
        cloud.service['metadata']['resourceVersion'] = 'external'
    with mock.patch('scripts.reconcile_cloud_runtime._verify_deploy_candidate_with_wait', side_effect=drift):
        with pytest.raises(RuntimeError):
            release(tmp_path, cloud).execute()
    assert not any('update-traffic' in c for c in cloud.commands)


@pytest.mark.parametrize('profile,workflow,selector,expected', [
    ('russell_top50_leader_rotation', 'configured', 'unit-service', True),
    ('tqqq_growth_income', 'configured', 'unit-service', False),
    ('russell_top50_leader_rotation', 'hk-verify', 'unit-service', False)])
def test_only_selected_russell_target_gets_lifecycle(profile, workflow, selector, expected):
    assert api().eligible(plan(profile), workflow, selector) is expected


@pytest.mark.parametrize('case', ['selector', 'multiple'])
def test_target_identity_mismatch_blocks(case):
    value = plan()
    selector = 'wrong' if case == 'selector' else 'unit-service'
    if case == 'multiple':
        value['targets'].append(value['targets'][0])
    with pytest.raises(ValueError):
        api().eligible(value, 'configured', selector)


def test_raw_readback_uses_shared_redaction(tmp_path, capsys):
    cloud = Cloud()
    cloud.service['status']['conditions'] = [{'type': 'Ready', 'message': 'account=U12345678 host=private.example.invalid project=private-project'}]
    with mock.patch('scripts.reconcile_cloud_runtime._verify_deploy_candidate_with_wait'):
        release(tmp_path, cloud).execute()
    output = capsys.readouterr().out
    assert 'U12345678' not in output and 'private.example.invalid' not in output and 'private-project' not in output
    assert 'conditions' in output and 'minScale' in output and 'restore' in output and 'set' in output


def test_workflow_keeps_lifecycle_in_one_script_and_skips_duplicate_shift():
    text = Path('.github/workflows/sync-cloud-run-env-protected.yml').read_text()
    assert 'python -m scripts.cloud_run_candidate_warmup' in text
    assert "steps.deploy.outputs.warmup_managed != 'true'" in text
    assert text.count('candidate_warmup_args=(--min-instances=1)') == 0


def test_exclusive_state_prevents_concurrent_writer(tmp_path):
    import subprocess
    import sys
    script = """
import json,sys
from pathlib import Path
from unittest import mock
from tests.test_candidate_tag_release import Cloud,release
cloud = Cloud()
try:
    with mock.patch('scripts.reconcile_cloud_runtime._verify_deploy_candidate_with_wait'):
        release(Path(sys.argv[1]), cloud).execute()
    status = 'success'
except RuntimeError:
    status = 'blocked'
print(json.dumps({'status':status,'deploys':sum('deploy' in c for c in cloud.commands)}))
"""
    processes = [subprocess.Popen([sys.executable, '-c', script, str(tmp_path)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    results = []
    for process in processes:
        output, error = process.communicate(timeout=15)
        assert process.returncode == 0, error
        results.append(json.loads(output.splitlines()[-1]))
    assert sorted(row['status'] for row in results) == ['blocked', 'success']
    assert sum(row['deploys'] for row in results) == 1


def test_restore_raw_readback_failure_is_not_success(tmp_path):
    cloud = Cloud()
    obj = release(tmp_path, cloud)
    readback = obj.readback
    def fail_restore(stage):
        if stage.startswith('restore'):
            raise RuntimeError('readback unavailable')
        return readback(stage)
    with mock.patch.object(obj, 'readback', side_effect=fail_restore), \
         mock.patch('scripts.reconcile_cloud_runtime._verify_deploy_candidate_with_wait'):
        with pytest.raises(RuntimeError, match='cleanup'):
            obj.execute()
    assert json.loads((tmp_path / 'state.json').read_text())['status'] == 'cleanup_failed'


def test_original_configuration_iam_scheduler_readback_failure_blocks_shift(tmp_path):
    cloud = Cloud()
    obj = release(tmp_path, cloud)
    obj.no_traffic_verify = mock.Mock(side_effect=RuntimeError('baseline mismatch'))
    with mock.patch('scripts.reconcile_cloud_runtime._verify_deploy_candidate_with_wait'):
        with pytest.raises(RuntimeError, match='baseline mismatch'):
            obj.execute()
    assert not any(any(v.startswith('--to-revisions=') for v in c) for c in cloud.commands)
    assert any('--min-instances=0' in c for c in cloud.commands)


@pytest.mark.parametrize('logs,expected', [([], 'absent'), ([{'logName': 'fake-log'}], 'present')])
def test_missing_health_readback_checks_actual_instance_log_presence(tmp_path, logs, expected, capsys):
    driver = Cloud()
    driver(['gcloud', 'run', 'deploy', 'unit-service'])
    driver.revisions['unit-service-warm']['status']['conditions'].pop()
    def cloud(command):
        if 'logging' in command:
            return logs
        return driver(command)
    obj = release(tmp_path, cloud)
    obj.warm = 'unit-service-warm'
    obj.readback('set')
    output = capsys.readouterr().out
    assert 'instance_logs=' + expected in output
    assert 'Retired' in output and 'conditions' in output
    assert 'startup_probe_failed' not in output


def test_empty_raw_describe_never_satisfies_cleanup_assertion(tmp_path):
    driver = Cloud()
    def cloud(command):
        if any(value.startswith('--format=json(') for value in command):
            return {}
        return driver(command)
    with mock.patch('scripts.reconcile_cloud_runtime._verify_deploy_candidate_with_wait'):
        with pytest.raises(RuntimeError, match='cleanup'):
            release(tmp_path, cloud).execute()
    assert json.loads((tmp_path / 'state.json').read_text())['status'] == 'cleanup_failed'
    assert not any('--to-revisions=unit-service-warm=100' in c for c in driver.commands)
