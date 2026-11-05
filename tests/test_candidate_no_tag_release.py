"""Fake-only long-service lifecycle regressions; no cloud requests."""
import json
from unittest import mock

import pytest

from scripts.cloud_run_candidate_warmup import Release
from tests.test_candidate_tag_release import Cloud, COMMIT, DIGEST

SERVICE = 'interactive-brokers-quant-live-fake12345-service'


def release(tmp_path, cloud):
    return Release(project='<PROJECT_ID>', region='unit-region', service=SERVICE,
                   commit=COMMIT, digest=DIGEST, run_id='123',
                   deploy_args=['--image=fake@' + DIGEST, '--no-traffic', '--deploy-health-check'],
                   state_path=tmp_path / 'state.json', cloud=cloud, no_traffic_verify=lambda: None)


def assert_no_tags(cloud):
    assert not any(value.startswith(('--tag', '--remove-tags', '--set-tags', '--update-tags'))
                   for command in cloud.commands for value in command)
    assert sum('deploy' in command for command in cloud.commands) == 1


def test_long_service_qualifies_both_revisions_and_promotes_by_name(tmp_path):
    cloud = Cloud(service_name=SERVICE)
    observed = []
    def health(value, **kwargs):
        observed.append(value['metadata']['name'])
        if value['metadata']['name'].endswith('-warm'):
            assert kwargs['service']['status']['traffic'] == [
                {'revisionName': SERVICE + '-old', 'percent': 99},
                {'revisionName': SERVICE + '-warm', 'percent': 1}]
    with mock.patch('scripts.reconcile_cloud_runtime._verify_deploy_candidate_with_wait', side_effect=health):
        result = release(tmp_path, cloud).execute()
    assert len(SERVICE) == 48
    assert_no_tags(cloud)
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
    assert_no_tags(cloud)
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
    assert_no_tags(cloud)


def test_long_service_stale_snapshot_blocks_mutation(tmp_path):
    cloud = Cloud(service_name=SERVICE)
    def drift(*args, **kwargs):
        cloud.service['metadata']['resourceVersion'] = 'external'
    with mock.patch('scripts.reconcile_cloud_runtime._verify_deploy_candidate_with_wait', side_effect=drift):
        with pytest.raises(RuntimeError):
            release(tmp_path, cloud).execute()
    shifts = [command for command in cloud.commands if 'update-traffic' in command]
    assert len(shifts) == 1  # Only the approved split, before the external change.
    assert not any('update' in command for command in cloud.commands)
    assert_no_tags(cloud)


def test_long_service_readback_keeps_shared_redaction(tmp_path, capsys):
    cloud = Cloud(service_name=SERVICE)
    cloud.service['status']['conditions'] = [{'type': 'Ready', 'status': 'True',
        'message': 'account=U12345678 host=private.example.invalid project=private-project'}]
    with mock.patch('scripts.reconcile_cloud_runtime._verify_deploy_candidate_with_wait'):
        release(tmp_path, cloud).execute()
    output = capsys.readouterr().out
    assert 'U12345678' not in output and 'private.example.invalid' not in output
    assert 'private-project' not in output
    assert_no_tags(cloud)


@pytest.mark.parametrize('length,tagged', [(33, True), (34, False), (48, False)])
def test_combined_tag_length_preserves_supported_short_services(tmp_path, length, tagged):
    obj = release(tmp_path, Cloud())
    obj = Release(project=obj.project, region=obj.region, service='x' * length,
                  commit=COMMIT, digest=DIGEST, run_id='123', deploy_args=obj.deploy_args,
                  state_path=obj.state_path, cloud=obj.cloud)
    assert bool(obj.tag) is tagged


def test_split_failure_restores_prior_traffic_before_minimum_reset(tmp_path):
    cloud = Cloud(service_name=SERVICE)
    obj = release(tmp_path, cloud)
    def baseline():
        assert cloud.service['status']['traffic'] == [{'revisionName': SERVICE + '-old', 'percent': 100}]
        assert not any('update-traffic' in c for c in cloud.commands)
    obj.no_traffic_verify = baseline
    with mock.patch('scripts.reconcile_cloud_runtime._verify_deploy_candidate_with_wait',
                    side_effect=ValueError('startup failed')):
        with pytest.raises(ValueError, match='startup failed'):
            obj.execute()
    split = next(i for i, c in enumerate(cloud.commands) if '--to-revisions=' + SERVICE + '-old=99,' + SERVICE + '-warm=1' in c)
    rollback = next(i for i, c in enumerate(cloud.commands) if '--to-revisions=' + SERVICE + '-old=100' in c)
    reset = next(i for i, c in enumerate(cloud.commands) if '--min-instances=0' in c)
    assert split < rollback < reset


@pytest.mark.parametrize('mismatch', ['split', 'rollback'])
def test_micro_split_readback_mismatch_fails_closed(tmp_path, mismatch):
    cloud = Cloud(service_name=SERVICE)
    def driver(command):
        result = cloud(command)
        if 'update-traffic' in command and any(v.startswith('--to-revisions=') for v in command):
            is_split = any('=99,' in v for v in command)
            if is_split == (mismatch == 'split'):
                cloud.service['status']['traffic'] = [
                    {'revisionName': SERVICE + '-old', 'percent': 98},
                    {'revisionName': SERVICE + '-warm', 'percent': 2}]
        return result
    obj = release(tmp_path, driver)
    with mock.patch('scripts.reconcile_cloud_runtime._verify_deploy_candidate_with_wait',
                    side_effect=ValueError('startup failed')) as health:
        with pytest.raises(RuntimeError, match='cleanup'):
            obj.execute()
    if mismatch == 'split':
        health.assert_not_called()
    assert not any('--min-instances=0' in c for c in cloud.commands)
    assert not any('--to-revisions=' + SERVICE + '-warm=100' in c for c in cloud.commands)


def test_untagged_retired_candidate_starts_only_after_split(tmp_path):
    cloud = Cloud(service_name=SERVICE)
    def driver(command, **kwargs):
        result = cloud(command)
        if 'deploy' in command:
            cold = cloud.revisions[SERVICE + '-warm']
            cold['status']['conditions'] = [
                {'type': 'Ready', 'status': 'True', 'reason': 'Retired'},
                {'type': 'Active', 'status': 'False', 'reason': 'Retired'},
                {'type': 'ContainerReady', 'status': 'True'},
                {'type': 'MinInstancesProvisioned', 'status': 'False', 'reason': 'NotProvisioned'}]
        if any('=99,' in v for v in command):
            cloud.revisions[SERVICE + '-warm']['status']['conditions'] = [
                {'type': kind, 'status': 'True'} for kind in
                ('Ready', 'Active', 'ContainerReady', 'MinInstancesProvisioned', 'ContainerHealthy')]
        return result
    # Use the real strict waiter and health verifier, with only external
    # continuity reads stubbed. Retired + NotProvisioned cannot pass it.
    with mock.patch('scripts.runtime_target_continuity.verify_target_continuity'), \
         mock.patch('scripts.reconcile_cloud_runtime._read_deploy_cloud', side_effect=driver), \
         mock.patch('scripts.reconcile_cloud_runtime.time.sleep'):
        assert release(tmp_path, driver).execute() == SERVICE + '-reset'


@pytest.mark.parametrize('failure', ['split_timeout', 'ownership_loss'])
def test_ambiguous_split_and_ownership_loss_do_not_replay(tmp_path, failure):
    cloud = Cloud(service_name=SERVICE)
    def driver(command):
        result = cloud(command)
        if any('=99,' in v for v in command):
            if failure == 'ownership_loss':
                cloud.service['metadata']['uid'] = 'other-owner'
            raise RuntimeError('ambiguous split')
        return result
    with pytest.raises(RuntimeError):
        release(tmp_path, driver).execute()
    shifts = [c for c in cloud.commands if 'update-traffic' in c]
    assert len(shifts) == (2 if failure == 'split_timeout' else 1)
    if failure == 'split_timeout':
        assert cloud.service['status']['traffic'] == [{'revisionName': SERVICE + '-old', 'percent': 100}]
        assert any('--min-instances=0' in c for c in cloud.commands)
    else:
        assert not any('--min-instances=0' in c for c in cloud.commands)
