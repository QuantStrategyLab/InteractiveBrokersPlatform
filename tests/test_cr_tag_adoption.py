"""Shared policy adoption regressions; fake targets and offline drivers only."""
from dataclasses import replace
import importlib
from unittest import mock

import pytest

from scripts import cloud_run_candidate_warmup as warmup
from scripts import deploy_pilot_no_traffic as pilot
from scripts import observe_maintenance_cash as cash
from scripts import run_pilot_cloud_tcp as tcp
from tests.test_candidate_tag_release import Cloud, COMMIT, DIGEST
from tests.test_pilot_cloud_tcp import lifecycle  # noqa: F401


def policy():
    return importlib.import_module('scripts.cloud_run_tag_policy')


def release(tmp_path, service, **kwargs):
    cloud = Cloud(service_name=service)
    obj = warmup.Release(project='fake-project', region='fake-region', service=service,
        commit=COMMIT, digest=DIGEST, run_id='123', state_path=tmp_path / 'state.json',
        deploy_args=['--no-traffic', '--deploy-health-check'], cloud=cloud,
        no_traffic_verify=lambda: None, **kwargs)
    return obj, cloud


@pytest.mark.parametrize('length', [34, 38, 44, 45])
def test_shortened_tag_activates_only_the_cleanup_replacement(tmp_path, length):
    obj, cloud = release(tmp_path, 's' * length,
                         warmup_traffic_approved=True, approved_service='s' * length)
    assert obj.tag and len(obj.tag) + length <= 46
    with mock.patch('scripts.reconcile_cloud_runtime._verify_deploy_candidate_with_wait'):
        obj.execute()
    splits = [arg for command in cloud.commands for arg in command if '=99,' in arg]
    assert splits == ['--to-revisions=' + cloud.service_name + '-warm=99,' + cloud.service_name + '-reset=1']


def test_impossible_warmup_without_exact_approval_refuses_before_mutation(tmp_path):
    obj, cloud = release(tmp_path, 's' * 48)
    with pytest.raises((ValueError, RuntimeError), match='approv|TAG_IMPOSSIBLE'):
        obj.execute()
    assert not any('deploy' in command or 'update-traffic' in command for command in cloud.commands)


def test_tcp_shortens_and_refuses_impossible_without_micro_traffic(lifecycle):  # noqa: F811
    for length in (38, 45):
        ctx = replace(lifecycle.ctx, service='s' * length)
        desired, _, _ = tcp.prepared(ctx, lifecycle.before, lifecycle.group, '123')
        tag = desired['spec']['traffic'][-1]['tag']
        assert len(tag) + length <= 46
        assert desired['spec']['traffic'][-1]['percent'] == 0
    ctx = replace(lifecycle.ctx, service='s' * 48)
    with pytest.raises(pilot.ReleaseError, match='TAG_IMPOSSIBLE'):
        tcp.prepared(ctx, lifecycle.before, lifecycle.group, '123')


def test_cash_supports_one_character_budget_with_shared_policy():
    assert len(cash.traffic_tag('123', 's' * 45)) == 1
    with pytest.raises(pilot.ReleaseError, match='TAG_IMPOSSIBLE'):
        cash.traffic_tag('123', 's' * 48)


def test_refusal_diagnostics_do_not_expose_supplied_identity():
    private = 'invalid-UPPERCASE-private-name'
    with pytest.raises(ValueError) as error:
        policy().headroom(private)
    assert private not in str(error.value)


def test_foreign_tag_and_unknown_snapshot_refuse():
    module = policy()
    tag = module.allocate_tag('s' * 45, 'cand-aaaaaaaa', existing_tags=[], operation_id='fake-op')
    assert tag.status == 'ALLOCATED'
    conflict = module.allocate_tag('s' * 45, 'cand-aaaaaaaa', existing_tags=[tag.tag], operation_id='fake-op')
    assert conflict.status == 'REFUSE'
    with pytest.raises(ValueError):
        module.allocate_tag('fake-service', 'cand-aaaaaaaa', existing_tags=None, operation_id='fake-op')


def test_warmup_fresh_foreign_tag_blocks_mutation(tmp_path):
    obj, cloud = release(tmp_path, 's' * 45)
    assert obj.tag
    original = obj.cloud
    reads = 0
    def driver(command):
        nonlocal reads
        if 'services' in command and 'describe' in command:
            reads += 1
            if reads == 2:
                cloud.service['status']['traffic'].append(
                    {'revisionName': cloud.service_name + '-old', 'percent': 0, 'tag': obj.tag})
        return original(command)
    obj.cloud = driver
    with pytest.raises(RuntimeError):
        obj.execute()
    assert not any('deploy' in command for command in cloud.commands)


def test_shortened_warmup_restart_does_not_reallocate_or_replay(tmp_path):
    obj, cloud = release(tmp_path, 's' * 45,
                         warmup_traffic_approved=True, approved_service='s' * 45)
    assert obj.tag
    with mock.patch('scripts.reconcile_cloud_runtime._verify_deploy_candidate_with_wait'):
        obj.execute()
    count = len(cloud.commands)
    with pytest.raises(RuntimeError, match='restart'):
        obj.execute()
    assert len(cloud.commands) == count


def test_no_traffic_policy_never_acquires_traffic_authority():
    module = policy()
    assert module.lifecycle_for_impossible_tag(service_name='s' * 48,
        approved_service='s' * 48, warmup_traffic_approved=True, no_traffic=True) == 'STOP'
    assert module.lifecycle_for_impossible_tag(service_name='s' * 48,
        approved_service='other-service', warmup_traffic_approved=True) == 'STOP'


@pytest.mark.parametrize('service,valid', [('fake-service', True), ('s' * 48, True),
    ('Invalid-name', False), ('bad-name-', False), ('s' * 50, False)])
def test_planner_validates_exact_name_without_renaming(service, valid):
    from scripts import build_cloud_run_env_sync_plan as planner
    runtime = {'service_name': service, 'strategy_profile': 'fake-profile'}
    with mock.patch.object(planner, '_resolve_runtime_target', return_value=runtime.copy()):
        arguments = dict(target={'service': service}, defaults={},
            env={'INPUT_RUNTIME_SWITCH_ONLY': 'true', 'RUNTIME_TARGET_ENABLED': 'false'},
            status_rows={}, per_service_mode=False, platform_config={})
        if not valid:
            with pytest.raises(ValueError, match='INVALID_SERVICE_NAME'):
                planner._build_target_plan(**arguments)
        else:
            plan = planner._build_target_plan(**arguments)
            assert plan['service_name'] == service
            assert plan['tag_preflight']['headroom'] == 46 - len(service)
            if len(service) >= 46:
                assert plan['tag_preflight']['tag_status'] == 'TAG_IMPOSSIBLE'


def test_tcp_restart_claim_blocks_second_mutation(lifecycle):  # noqa: F811
    lifecycle.execute()
    count = len(lifecycle.calls)
    with pytest.raises(pilot.ReleaseError, match='restart'):
        lifecycle.execute()
    assert len(lifecycle.calls) == count


def test_exclusive_claim_is_shared_across_processes(tmp_path):
    import concurrent.futures
    import multiprocessing
    path = tmp_path / 'shared.claim'
    # fork workers import the same real policy and race O_EXCL on one inode.
    with concurrent.futures.ProcessPoolExecutor(max_workers=2,
            mp_context=multiprocessing.get_context('fork')) as executor:
        outcomes = list(executor.map(_try_claim, [str(path), str(path)]))
    assert sorted(outcomes) == ['claimed', 'refused']


def _try_claim(path):
    from pathlib import Path
    try:
        policy().claim_operation(Path(path), 'fake-operation')
        return 'claimed'
    except ValueError:
        return 'refused'


def test_cash_fresh_foreign_tag_blocks_mutation(tmp_path):
    from tests.test_observe_maintenance_cash import Cloud as CashCloud, inputs, NOW
    from tests.test_pilot_no_traffic_release import environment
    state, revision, expected, _ = inputs()
    driver = CashCloud(state, revision)
    reads = 0
    def snapshot(*args):
        nonlocal reads
        reads += 1
        result = driver.snapshot(*args)
        if reads == 2:
            result['traffic'].append({'revision': 'fake-foreign', 'percent': 0, 'tag': 'foreign'})
        return result
    result = cash.observe(expected, COMMIT, environment(), tmp_path / 'safe.json',
        run=driver.run, snapshot=snapshot, now=lambda: NOW)
    assert result['status'] == 'STOPPED' and result['reason'] == 'observation_baseline_changed'
    assert not any(args[1:4] == ['run', 'services', 'update-traffic'] for args in driver.calls)
