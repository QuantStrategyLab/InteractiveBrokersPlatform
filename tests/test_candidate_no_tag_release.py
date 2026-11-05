"""Fake-only compact-tag lifecycle regressions; no cloud requests."""
import json
from unittest import mock

import pytest

from scripts.cloud_run_candidate_warmup import Release
from tests.test_candidate_tag_release import Cloud, COMMIT, DIGEST

SERVICE = 'unit-' + 'x' * 30 + '62'


def release(tmp_path, cloud):
    return Release(project='<PROJECT_ID>', region='unit-region', service=SERVICE,
                   commit=COMMIT, digest=DIGEST, run_id='123',
                   deploy_args=['--image=fake@' + DIGEST, '--no-traffic', '--deploy-health-check'],
                   state_path=tmp_path / 'state.json', cloud=cloud, no_traffic_verify=lambda: None)


def assert_compact_tag(cloud):
    assert any('--tag=caaaaaaaa' in command for command in cloud.commands)
    assert any('--remove-tags=caaaaaaaa' in command for command in cloud.commands)
    assert sum('deploy' in command for command in cloud.commands) == 1


def test_long_service_qualifies_both_revisions_and_promotes_by_name(tmp_path):
    cloud = Cloud(service_name=SERVICE)
    observed = []
    def health(value, **kwargs):
        observed.append(value['metadata']['name'])
    with mock.patch('scripts.reconcile_cloud_runtime._verify_deploy_candidate_with_wait', side_effect=health):
        result = release(tmp_path, cloud).execute()
    assert len(SERVICE) == 37
    assert_compact_tag(cloud)
    assert result == SERVICE + '-reset'
    assert observed == [SERVICE + '-warm', SERVICE + '-reset']
    assert cloud.service['status']['traffic'] == [{'revisionName': result, 'percent': 100}]
    assert json.loads((tmp_path / 'state.json').read_text())['status'] == 'restored'


@pytest.mark.parametrize('failure', ['health', 'baseline', 'deploy_abort'])
def test_long_service_failure_preserves_old_traffic_without_retry(tmp_path, failure):
    cloud = Cloud('deploy_abort' if failure == 'deploy_abort' else None, service_name=SERVICE)
    obj = release(tmp_path, cloud)
    if failure == 'baseline':
        obj.no_traffic_verify = mock.Mock(side_effect=RuntimeError('baseline unavailable'))
    health_error = ValueError('health unavailable') if failure == 'health' else None
    with mock.patch('scripts.reconcile_cloud_runtime._verify_deploy_candidate_with_wait', side_effect=health_error):
        with pytest.raises((RuntimeError, ValueError, InterruptedError)):
            obj.execute()
    assert_compact_tag(cloud)
    assert cloud.service['status']['traffic'] == [{'revisionName': SERVICE + '-old', 'percent': 100}]
    assert any('--min-instances=0' in command for command in cloud.commands)
    assert json.loads((tmp_path / 'state.json').read_text())['status'] == 'restored'


def test_long_service_restart_blocks_replay(tmp_path):
    cloud = Cloud(service_name=SERVICE)
    with mock.patch('scripts.reconcile_cloud_runtime._verify_deploy_candidate_with_wait'):
        release(tmp_path, cloud).execute()
    commands = len(cloud.commands)
    with pytest.raises(RuntimeError, match='restart'):
        release(tmp_path, cloud).execute()
    assert len(cloud.commands) == commands
    assert_compact_tag(cloud)


def test_long_service_stale_snapshot_blocks_mutation(tmp_path):
    cloud = Cloud(service_name=SERVICE)
    def drift(*args, **kwargs):
        cloud.service['metadata']['resourceVersion'] = 'external'
    with mock.patch('scripts.reconcile_cloud_runtime._verify_deploy_candidate_with_wait', side_effect=drift):
        with pytest.raises(RuntimeError):
            release(tmp_path, cloud).execute()
    assert not any('update-traffic' in command or 'update' in command for command in cloud.commands)
    assert any('--tag=caaaaaaaa' in command for command in cloud.commands)


def test_long_service_readback_keeps_shared_redaction(tmp_path, capsys):
    cloud = Cloud(service_name=SERVICE)
    cloud.service['status']['conditions'] = [{'type': 'Ready', 'status': 'True',
        'message': 'account=U12345678 host=private.example.invalid project=private-project'}]
    with mock.patch('scripts.reconcile_cloud_runtime._verify_deploy_candidate_with_wait'):
        release(tmp_path, cloud).execute()
    output = capsys.readouterr().out
    assert 'U12345678' not in output and 'private.example.invalid' not in output
    assert 'private-project' not in output
    assert_compact_tag(cloud)


@pytest.mark.parametrize('length', [33, 34, 37])
def test_combined_tag_length_preserves_supported_short_services(tmp_path, length):
    obj = Release(project='<PROJECT_ID>', region='unit-region', service='x' * length,
                  commit=COMMIT, digest=DIGEST, run_id='123', deploy_args=[],
                  state_path=tmp_path / 'state.json')
    assert obj.tag and length + len(obj.tag) <= 46
