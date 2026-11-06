"""Fake-only adverse contracts for the four-target disabled migration channel."""
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from scripts import short_name_migration as migration


def plan():
    return {
        'mapping_ref': migration.MAPPING_REF,
        'source_revision': migration.SOURCE_REVISION,
        'project': 'fake-project', 'region': 'asia-southeast1',
        'disabled_service_account': 'fake-disabled@fake-project.iam.gserviceaccount.com',
        'targets': [
            {'reference': f'S07-C0{i}', 'old': f'fake-old-{i}', 'new': f'fake-new-{i}',
             'image': 'asia-southeast1-docker.pkg.dev/fake-project/fake/image@sha256:' + 'a' * 64,
             'expected_uid': ''}
            for i in range(1, 5)
        ],
    }


def environment(candidate=None):
    candidate = candidate or plan()
    digest = hashlib.sha256(migration.canonical(candidate).encode()).hexdigest()
    env = {
        'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_REF': 'refs/heads/main',
        'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_RUN_ID': '123', 'GITHUB_SHA': 'b' * 40,
        'MIGRATION_ENVIRONMENT': 'fake-protected', 'MIGRATION_PLAN_JSON': json.dumps(candidate),
        'INPUT_PLAN_SHA256': digest, 'INPUT_SOURCE_REVISION': migration.SOURCE_REVISION,
        'INPUT_MAPPING_REF': migration.MAPPING_REF,
        'GCP_PROJECT_ID': 'fake-project',
    }
    card = {
        'action': 'initialize-disabled', 'run_id': '123', 'workflow_sha': 'b' * 40,
        'plan_sha256': digest, 'source_revision': migration.SOURCE_REVISION,
        'mapping_ref': migration.MAPPING_REF,
        'expires_at': (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat(),
        'authority_ref': 'fake-separate-owner-card',
    }
    env['MIGRATION_AUTHORIZATION_JSON'] = json.dumps(card)
    return env


def owned(candidate, target, uid='fake-uid'):
    body = migration.service_body(candidate, target)
    body.update(uid=uid, generation='1', observedGeneration='1', reconciling=False,
                terminalCondition={'state': 'CONDITION_SUCCEEDED'},
                latestCreatedRevision=body['name'] + '/revisions/fake-revision',
                latestReadyRevision=body['name'] + '/revisions/fake-revision',
                trafficStatuses=[{'revision': body['name'] + '/revisions/fake-revision',
                                  'percent': 100, 'type': 'TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION'}])
    return body


class Cloud:
    def __init__(self, candidate):
        self.plan = candidate
        self.services = {}
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.startswith('https://cloudresourcemanager.googleapis.com/'):
            return {'projectId': self.plan['project'], 'name': 'projects/123456789012', 'state': 'ACTIVE'}
        if '/operations/' in path:
            return {'done': True, 'response': self.created}
        if method == 'GET':
            return copy.deepcopy(self.services.get(path))
        if 'validateOnly=true' in path:
            return {'done': True}
        self.created = owned(self.plan, next(t for t in self.plan['targets'] if body['name'].endswith('/' + t['new'])))
        self.services[migration.RUN_API + '/' + body['name']] = self.created
        return {'name': migration.RUN_API + '/projects/fake-project/locations/asia-southeast1/operations/fake'}


@pytest.mark.parametrize('mutation', [
    lambda p: p['targets'].pop(),
    lambda p: p['targets'][0].update(reference='S07-C05'),
    lambda p: p['targets'][1].update(new=p['targets'][0]['new']),
    lambda p: p['targets'][0].update(new=p['targets'][1]['old']),
    lambda p: p.update(source_revision='c' * 40),
    lambda p: p['targets'][0].update(image='fake:latest'),
    lambda p: p.update(project='../fake-project'),
    lambda p: p['targets'][0].update(enable_live=True),
])
def test_invalid_scope_fails_before_cloud(mutation):
    p = plan()
    mutation(p)
    cloud = Mock()
    with pytest.raises(migration.Blocked):
        migration.run('observe', environment(p), cloud)
    cloud.assert_not_called()


@pytest.mark.parametrize('key,value', [('GITHUB_REF', 'refs/heads/fake'), ('GITHUB_RUN_ATTEMPT', '2'),
                                     ('MIGRATION_ENVIRONMENT', ''), ('INPUT_PLAN_SHA256', 'a' * 64),
                                     ('MIGRATION_AUTHORIZATION_JSON', '{}')])
def test_dispatch_or_authority_failure_blocks_before_cloud(key, value):
    env = environment()
    env[key] = value
    cloud = Mock()
    with pytest.raises(migration.Blocked):
        migration.run('initialize', env, cloud)
    cloud.assert_not_called()


def test_absent_destination_observation_needs_no_inventory_or_mutation():
    p = plan()
    cloud = Cloud(p)
    report = migration.run('observe', environment(p), cloud)
    assert [r['state'] for r in report['targets']] == ['ABSENT'] * 4
    assert all(c[0] == 'GET' for c in cloud.calls)
    assert 'fake-project' not in json.dumps(report)


def test_collision_never_writes_even_if_other_destinations_absent():
    p = plan()
    cloud = Cloud(p)
    cloud.services[migration.RUN_API + '/' + migration.service_body(p, p['targets'][3])['name']] = owned(p, p['targets'][3])
    with pytest.raises(migration.Blocked):
        migration.run('initialize', environment(p), cloud)
    assert all(c[0] == 'GET' for c in cloud.calls)


def test_initialize_creates_only_fixed_disabled_containers_and_reads_back():
    p = plan()
    cloud = Cloud(p)
    report = migration.run('initialize', environment(p), cloud)
    assert [r['state'] for r in report['targets']] == ['INITIALIZED_DISABLED'] * 4
    writes = [c for c in cloud.calls if c[0] == 'POST' and 'validateOnly=true' not in c[1]]
    assert len(writes) == 4
    for _, _, body in writes:
        assert body['template']['containers'][0]['command'] == ['python', '-c']
        assert body['template']['containers'][0]['args'] == [migration.DISABLED_SERVER]
        assert body['ingress'] == 'INGRESS_TRAFFIC_INTERNAL_ONLY'
        assert body['invokerIamDisabled'] is False
        assert body['template']['containers'][0]['env'] == []
    assert all(r['revision_verified'] and r['traffic_verified'] for r in report['targets'])


def test_concurrent_creator_is_detected_by_fresh_read_before_post():
    p = plan()
    cloud = Cloud(p)
    original = cloud.__call__
    reads = 0
    def race(method, path, body=None):
        nonlocal reads
        if method == 'GET' and '/services/' in path:
            reads += 1
            if reads == 5:
                return owned(p, p['targets'][0])
        return original(method, path, body)
    with pytest.raises(migration.Blocked):
        migration.run('initialize', environment(p), race)
    assert not any(c[0] == 'POST' and 'validateOnly' not in c[1] for c in cloud.calls)


def test_expiry_rechecked_before_each_mutation():
    env = environment()
    cloud = Cloud(plan())
    original = cloud.__call__
    def expire(method, path, body=None):
        result = original(method, path, body)
        if 'validateOnly' in path:
            card = json.loads(env['MIGRATION_AUTHORIZATION_JSON'])
            card['expires_at'] = '2000-01-01T00:00:00+00:00'
            env['MIGRATION_AUTHORIZATION_JSON'] = json.dumps(card)
        return result
    with pytest.raises(migration.Blocked):
        migration.run('initialize', env, expire)
    assert not any(c[0] == 'POST' and 'validateOnly' not in c[1] for c in cloud.calls)


@pytest.mark.parametrize('field,value', [('trafficStatuses', []), ('reconciling', True),
                                        ('latestReadyRevision', ''), ('uid', 'different')])
def test_bad_or_stale_readback_blocks(field, value):
    p = plan()
    target = p['targets'][0]
    service = owned(p, target)
    service[field] = value
    with pytest.raises(migration.Blocked):
        migration.verify_service(p, target, service, 'fake-uid')


def test_transport_and_provider_details_never_escape(capsys, monkeypatch):
    monkeypatch.setenv('MIGRATION_PLAN_JSON', '{invalid fake-sensitive-value')
    assert migration.main(['--mode', 'observe']) == 1
    assert 'fake-sensitive-value' not in capsys.readouterr().err


def test_validate_is_not_creation():
    cloud = Cloud(plan())
    report = migration.run('validate', environment(), cloud)
    assert report['mode'] == 'validate'
    posts = [c for c in cloud.calls if c[0] == 'POST']
    assert len(posts) == 4
    assert all('validateOnly=true' in c[1] for c in posts)
    assert not cloud.services


def test_existing_owned_destinations_are_not_changed():
    p = plan()
    for t in p['targets']:
        t['expected_uid'] = 'fake-uid'
    cloud = Cloud(p)
    for t in p['targets']:
        cloud.services[migration.RUN_API + '/' + migration.service_body(p, t)['name']] = owned(p, t)
    report = migration.run('initialize', environment(p), cloud)
    assert all(row['state'] == 'OWNED_DISABLED' for row in report['targets'])
    assert all(c[0] == 'GET' for c in cloud.calls)


@pytest.mark.parametrize('status,absent', [(404, True), (403, False), (429, False), (500, False)])
def test_only_service_not_found_means_absent(monkeypatch, status, absent):
    import urllib.error
    monkeypatch.setattr(migration.subprocess, 'run', lambda *a, **kw: Mock(stdout='fake-token'))
    def fail(*a, **kw):
        raise urllib.error.HTTPError('fake-private-url', status, 'fake-private-diagnostic', {}, None)
    monkeypatch.setattr(migration, 'open_request', fail)
    cloud = migration.Cloud(environment(), 'observe')
    if absent:
        assert cloud('GET', migration.RUN_API + '/projects/fake-project/locations/fake/services/fake') is None
    else:
        with pytest.raises(migration.Blocked) as caught:
            cloud('GET', migration.RUN_API + '/projects/fake-project/locations/fake/services/fake')
        assert 'fake-private' not in str(caught.value)


def test_token_acquisition_expiry_prevents_post(monkeypatch):
    env = environment()
    def token(*a, **kw):
        card = json.loads(env['MIGRATION_AUTHORIZATION_JSON'])
        card['expires_at'] = '2000-01-01T00:00:00+00:00'
        env['MIGRATION_AUTHORIZATION_JSON'] = json.dumps(card)
        return Mock(stdout='fake-token')
    monkeypatch.setattr(migration.subprocess, 'run', token)
    request = Mock()
    monkeypatch.setattr(migration, 'open_request', request)
    with pytest.raises(migration.Blocked):
        migration.Cloud(env, 'initialize')('POST', migration.RUN_API + '/projects/fake-project/locations/fake/services', {})
    request.assert_not_called()


@pytest.mark.parametrize('status', [409, 500])
def test_creation_error_is_never_retried_or_assumed_absent(monkeypatch, status):
    import urllib.error
    monkeypatch.setattr(migration.subprocess, 'run', lambda *a, **kw: Mock(stdout='fake-token'))
    request = Mock(side_effect=urllib.error.HTTPError('fake-url', status, 'fake-private-error', {}, None))
    monkeypatch.setattr(migration, 'open_request', request)
    with pytest.raises(migration.Blocked):
        migration.Cloud(environment(), 'initialize')('POST', migration.RUN_API + '/projects/fake-project/locations/fake/services', {})
    assert request.call_count == 1


def test_operation_timeout_does_not_repeat_mutation(monkeypatch):
    monkeypatch.setattr(migration.time, 'sleep', lambda _: None)
    cloud = Mock(return_value={'done': False})
    with pytest.raises(migration.Blocked):
        migration.wait_operation(cloud, {'name': 'projects/fake-project/locations/fake/operations/fake'},
                                 'projects/fake-project/locations/fake')
    assert all(call.args[0] == 'GET' for call in cloud.call_args_list)


def test_workflow_protection_contract():
    from pathlib import Path
    workflow = Path('.github/workflows/short-name-migration.yml').read_text()
    assert 'on:\n  workflow_dispatch:' in workflow
    assert 'workflow_call:' not in workflow and 'push:' not in workflow
    assert 'permissions:\n  contents: read\n' in workflow
    assert 'cancel-in-progress: false' in workflow
    assert 'environment: ${{ vars.IBKR_DEPLOY_ENVIRONMENT }}' in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert 'github.run_attempt == 1' in workflow
    assert workflow.index('admit exact scope') < workflow.index('google-github-actions/auth@v3')
    assert 'persist-credentials: false' in workflow
    assert 'ref: ${{ github.sha }}' in workflow
    assert 'path: ${{ runner.temp }}/short-name-migration.safe.json' in workflow


def test_provider_proto3_default_omission_preserves_disabled_semantics():
    p = plan()
    t = p['targets'][0]
    service = owned(p, t)
    del service['invokerIamDisabled']
    del service['reconciling']
    del service['template']['scaling']['minInstanceCount']
    del service['template']['containers'][0]['env']
    del service['template']['containers'][0]['resources']['startupCpuBoost']
    migration.verify_service(p, t, service, 'fake-uid')


@pytest.mark.parametrize('change', [
    lambda s: s.update(invokerIamDisabled=True),
    lambda s: s['template']['containers'][0].update(command=['python', 'main.py']),
    lambda s: s['template']['containers'][0].update(env=[{'name': 'FAKE_SECRET', 'value': 'fake'}]),
    lambda s: s['template'].update(volumes=[{'secret': {'secret': 'fake'}}]),
    lambda s: s['template']['scaling'].update(minInstanceCount=1),
    lambda s: s['trafficStatuses'][0].update(tag='fake-tag'),
])
def test_active_or_divergent_service_is_not_owned_disabled(change):
    p = plan()
    t = p['targets'][0]
    service = owned(p, t)
    change(service)
    with pytest.raises(migration.Blocked):
        migration.verify_service(p, t, service, 'fake-uid')


def test_redirect_does_not_forward_bearer_token():
    assert migration.NoRedirect().redirect_request(None, None, 302, 'fake', {}, 'https://fake.invalid') is None


def test_inert_container_command_is_valid_python():
    compile(migration.DISABLED_SERVER, '<disabled-server>', 'exec')


@pytest.mark.parametrize('mode', ['validate', 'initialize'])
@pytest.mark.parametrize('result', [{'done': True, 'error': {'message': 'fake-private-error'}},
                                   {'done': True, 'error': {}}, None, {}])
def test_failed_or_unknown_validation_blocks_all_creates(mode, result):
    cloud = Cloud(plan())
    original = cloud.__call__
    validations = 0
    def fail_last(method, path, body=None):
        nonlocal validations
        response = original(method, path, body)
        if 'validateOnly=true' in path:
            validations += 1
            if validations == 4:
                return result
        return response
    with pytest.raises(migration.Blocked):
        migration.run(mode, environment(), fail_last)
    assert validations == 4
    assert not cloud.services


def test_pending_validation_finishes_before_creation(monkeypatch):
    monkeypatch.setattr(migration.time, 'sleep', lambda _: None)
    cloud = Cloud(plan())
    original = cloud.__call__
    pending = polls = 0
    def pending_validation(method, path, body=None):
        nonlocal pending, polls
        if 'validateOnly=true' in path:
            original(method, path, body)
            pending += 1
            return {'name': 'projects/123456789012/locations/asia-southeast1/operations/validation'}
        if path.endswith('/operations/validation'):
            polls += 1
            return {'done': polls % 2 == 0}
        if method == 'POST':
            assert polls == 8 and pending == 4
        return original(method, path, body)
    report = migration.run('initialize', environment(), pending_validation)
    assert polls == 8
    assert all(r['state'] == 'INITIALIZED_DISABLED' for r in report['targets'])


@pytest.mark.parametrize('completion', [{'done': True, 'error': {'message': 'fake-private-error'}},
                                       {'done': False}])
def test_pending_validation_failure_or_timeout_never_creates(monkeypatch, completion):
    monkeypatch.setattr(migration.time, 'sleep', lambda _: None)
    cloud = Cloud(plan())
    original = cloud.__call__
    def pending(method, path, body=None):
        if '/operations/' in path:
            return completion
        response = original(method, path, body)
        if 'validateOnly=true' in path:
            return {'name': 'projects/fake-project/locations/asia-southeast1/operations/validation'}
        return response
    with pytest.raises(migration.Blocked):
        migration.run('initialize', environment(), pending)
    assert not cloud.services


def test_validation_poll_expiry_blocks_creation(monkeypatch):
    monkeypatch.setattr(migration.time, 'sleep', lambda _: None)
    cloud = Cloud(plan())
    original = cloud.__call__
    env = environment()
    def expire(method, path, body=None):
        if '/operations/' in path:
            card = json.loads(env['MIGRATION_AUTHORIZATION_JSON'])
            card['expires_at'] = '2000-01-01T00:00:00+00:00'
            env['MIGRATION_AUTHORIZATION_JSON'] = json.dumps(card)
            return {'done': True}
        response = original(method, path, body)
        if 'validateOnly=true' in path:
            return {'name': 'projects/fake-project/locations/asia-southeast1/operations/validation'}
        return response
    with pytest.raises(migration.Blocked):
        migration.run('initialize', env, expire)
    assert not cloud.services


@pytest.mark.parametrize('project', ['fake-project', '123456789012'])
def test_creation_accepts_only_authenticated_project_operation_names(project):
    cloud = Cloud(plan())
    original = cloud.__call__
    def numeric(method, path, body=None):
        response = original(method, path, body)
        if method == 'POST' and 'validateOnly' not in path:
            response['name'] = response['name'].replace('projects/fake-project/', 'projects/' + project + '/')
        return response
    report = migration.run('initialize', environment(), numeric)
    assert all(r['state'] == 'INITIALIZED_DISABLED' for r in report['targets'])


@pytest.mark.parametrize('prefix', ['projects/999999999999/locations/asia-southeast1',
                                    'projects/123456789012/locations/other-region1',
                                    'projects/fake-other/locations/asia-southeast1'])
def test_validation_operation_wrong_project_or_region_blocks(prefix):
    cloud = Cloud(plan())
    original = cloud.__call__
    def foreign(method, path, body=None):
        response = original(method, path, body)
        if 'validateOnly=true' in path:
            return {'name': prefix + '/operations/fake'}
        return response
    with pytest.raises(migration.Blocked):
        migration.run('initialize', environment(), foreign)
    assert not cloud.services


@pytest.mark.parametrize('minimum', [1, 4, '0', None])
def test_service_level_minimum_drift_blocks_before_any_post(minimum):
    p = plan()
    p['targets'][3]['expected_uid'] = 'fake-uid'
    cloud = Cloud(p)
    target = p['targets'][3]
    service = owned(p, target)
    service['scaling'] = {'minInstanceCount': minimum}
    cloud.services[migration.RUN_API + '/' + service['name']] = service
    with pytest.raises(migration.Blocked):
        migration.run('initialize', environment(p), cloud)
    assert all(c[0] == 'GET' for c in cloud.calls)


def test_validation_error_stays_redacted_at_cli(monkeypatch, capsys):
    cloud = Cloud(plan())
    original = cloud.__call__
    def failure(method, path, body=None):
        if 'validateOnly=true' in path:
            return {'done': True, 'error': {'message': 'fake-private-validation-detail'}}
        return original(method, path, body)
    monkeypatch.setattr(migration.os, 'environ', environment())
    monkeypatch.setattr(migration, 'Cloud', lambda *a: failure)
    assert migration.main(['--mode', 'initialize']) == 1
    captured = capsys.readouterr()
    assert 'fake-private' not in captured.err + captured.out
    assert 'BLOCKED_OR_OUTCOME_UNKNOWN' in captured.err
    assert not cloud.services


@pytest.mark.parametrize('project_name', [None, '', 'projects/fake-project',
                                         'projects/123/locations/fake'])
def test_missing_or_invalid_authenticated_project_number_blocks(project_name):
    cloud = Cloud(plan())
    original = cloud.__call__
    def invalid_project(method, path, body=None):
        response = original(method, path, body)
        if path.startswith(migration.PROJECT_API):
            response['name'] = project_name
        return response
    with pytest.raises(migration.Blocked):
        migration.run('initialize', environment(), invalid_project)
    assert len(cloud.calls) == 1


@pytest.mark.parametrize('scaling', [{}, {'minInstanceCount': 0}])
def test_explicit_default_service_minimum_remains_disabled(scaling):
    p = plan()
    t = p['targets'][0]
    service = owned(p, t)
    service['scaling'] = scaling
    migration.verify_service(p, t, service, 'fake-uid')
