"""Synthetic metadata only: no cloud, broker or protected values."""
import pytest
from types import SimpleNamespace

from scripts import preflight_cash_readonly_job as preflight


def test_preflight_requires_one_disabled_unchanged_serving_target(monkeypatch):
    profile = 'global_etf_rotation'
    target = dict(platform_id='ibkr', strategy_profile=profile, execution_mode='live', service_name='test-service',
                  account_scope='test', account_selector=['DU123'], deployment_selector='test')
    env = {'REQUESTED_TARGET': profile}
    expected = dict(expected_project_id='test-project', expected_service_name='test-service',
                    expected_runtime_revision='test-revision')
    service = {'metadata': {'name': 'test-service', 'resourceVersion': '1'},
               'status': {'traffic': [{'percent': 100, 'revisionName': 'test-revision'}]}}
    revision = {'metadata': {'name': 'test-revision', 'annotations': {}},
                'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]},
                'spec': {'serviceAccountName': 'private-account', 'containers': [{'env': [
                    {'name': 'RUNTIME_TARGET_JSON', 'value': __import__('json').dumps(target)},
                    {'name': 'RUNTIME_TARGET_ENABLED', 'value': 'false'},
                    {'name': 'EXECUTION_REPORT_GCS_URI', 'value': 'gs://synthetic/private'},
                    {'name': 'PRIVATE', 'valueFrom': {'secretKeyRef': {'name': 'private-secret'}}}]}]}}
    monkeypatch.setattr(preflight.binding, 'configuration', lambda _: ({}, {profile: ('test-service', target)},
                                                                       'test-project', 'test-region'))
    monkeypatch.setattr(preflight.archive, 'resolve_profile_slot', lambda _: 'additional-1')
    monkeypatch.setattr(preflight.image_identity, 'immutable_image', lambda rev: ('private-image', rev['spec']['containers'][0]))
    calls = []
    def read(kind, *args):
        calls.append(kind)
        return service if kind == 'services' else revision
    result = preflight.inspect(env, expected=expected, reader=read)
    assert result['status'] == 'metadata_verified'
    assert result['target_binding_sha256'] == preflight.target_binding_sha256(target)
    assert result['target_binding_sha256'] == preflight.target_binding_sha256(SimpleNamespace(**target))
    assert result['report_prefix_sha256'] == preflight.report_prefix_sha256('gs://synthetic/private')
    assert result['worker_account_binding_verified'] is None
    assert result['job_permissions'] == 'not_checked'
    assert result['worker_storage_permissions'] == result['network_reachability'] == 'not_checked'
    assert result['concurrent_execution'] == result['execution_path'] == 'not_checked'
    assert calls == ['services', 'revisions', 'services']
    assert all(secret not in str(result) for secret in ('DU123', 'test-service', 'private-secret', 'private-account', 'private-image'))
    revision['spec']['containers'][0]['env'][1]['value'] = 'true'
    with pytest.raises(preflight.binding.BindingError, match='runtime_not_explicitly_disabled'):
        preflight.inspect(env, expected=expected, reader=read)
    revision['spec']['containers'][0]['env'][1]['value'] = 'false'
    revision['spec']['containers'][0]['env'].append({'name': 'QSL_RUNTIME_TARGET_JSON', 'valueFrom': {'secretKeyRef': {'name': 'hidden'}}})
    with pytest.raises(preflight.binding.BindingError, match='runtime_scope_unavailable'):
        preflight.inspect(env, expected=expected, reader=read)
    revision['spec']['containers'][0]['env'].pop()
    service2 = {**service, 'metadata': {**service['metadata'], 'resourceVersion': '2'}}
    count = 0
    def changed(kind, *args):
        nonlocal count
        if kind == 'revisions':
            return revision
        count += 1
        return service if count == 1 else service2
    with pytest.raises(preflight.binding.BindingError, match='serving_changed_during_read'):
        preflight.inspect(env, expected=expected, reader=changed)


def test_preflight_rejects_all_before_reading():
    with pytest.raises(preflight.binding.BindingError, match='target_not_supported'):
        preflight.inspect({'REQUESTED_TARGET': 'all'}, expected={}, reader=lambda *args: pytest.fail('cloud read'))
