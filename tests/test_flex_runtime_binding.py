"""Synthetic configuration/metadata only; real cloud I/O is forbidden."""
import copy
import json
from pathlib import Path
import subprocess

import pytest

from scripts import diagnose_ibkr_flex_runtime_binding as diagnostic


@pytest.fixture(autouse=True)
def no_transport(monkeypatch):
    monkeypatch.setattr(diagnostic.subprocess, 'run', lambda *a, **k: pytest.fail('no real cloud process'))


@pytest.fixture
def materials():
    slots = {}
    for index, slot in enumerate(('primary', 'additional-1', 'additional-2', 'additional-3')):
        slots[slot] = {
            'project_id': 'private-project', 'report_prefix': f'gs://private-bucket/source-{index}',
            'target_id': f'private-target-{index}', 'service_name': f'private-service-{index}',
            'runtime_revision': f'private-revision-{index}', 'account_scope': f'private-scope-{index}',
            'account_selector': [f'DU000000{index}'], 'deployment_selector': f'private-deployment-{index}',
        }
    primary = slots['primary']
    env = {'GCP_PROJECT_ID': 'private-project', 'GCP_REGION': 'us-central1', 'REQUESTED_TARGET': 'all',
           'IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON': json.dumps(primary['account_selector']),
           'IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON': json.dumps(primary['account_selector']),
           'IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON': json.dumps({k: v for k, v in slots.items() if k != 'primary'})}
    env.update({'IBKR_ACCOUNT_FACTS_' + key.upper(): primary[key] for key in diagnostic.SLOT_FIELDS})
    entries, services, revisions = [], {}, {}
    for profile, slot in zip(diagnostic.PROFILES, ('additional-1', 'additional-2'), strict=True):
        row = slots[slot]
        target = {key: row[key] for key in diagnostic.IDENTITY_FIELDS}
        target.update(strategy_profile=profile, execution_mode='live')
        entries.append({'service': row['service_name'], 'include_reconciliation': True, 'runtime_target': target})
        services[row['service_name']] = {'metadata': {'name': row['service_name'], 'resourceVersion': '1'},
                                        'status': {'traffic': [{'revisionName': row['runtime_revision'], 'percent': 100}]}}
        revisions[row['runtime_revision']] = {
            'metadata': {'name': row['runtime_revision']}, 'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]},
            'spec': {'containers': [{'env': [{'name': 'RUNTIME_TARGET_JSON', 'value': json.dumps(target)}]}]},
        }
    env['CLOUD_RUN_SERVICE_TARGETS_JSON'] = json.dumps({'targets': entries})
    calls = []

    def reader(kind, name, project, region):
        calls.append((kind, name, project, region))
        assert project == 'private-project' and region == 'us-central1'
        return copy.deepcopy((services if kind == 'services' else revisions)[name])
    return env, slots, services, revisions, reader, calls


def test_two_targets_require_unique_slots_and_exact_serving_identity(materials):
    env, _, _, _, reader, calls = materials
    result = diagnostic.diagnose(env, reader=reader)
    assert result['status'] == 'matched' and len(calls) == 6
    assert set(result['targets']) == set(diagnostic.PROFILES)
    for row in result['targets'].values():
        assert row == {'status': 'matched', 'reason': 'binding_verified', 'unique_slot_match': True,
                       'serving_revision_match': True, 'runtime_scope_match': True}
    assert all(value not in json.dumps(result) for value in ('private-', 'DU000000', 'additional-1', 'additional-2', 'gs://'))


def test_preflight_never_reads_cloud_and_target_selection_does_not_expand(materials):
    env, _, _, _, reader, calls = materials
    env['REQUESTED_TARGET'] = diagnostic.PROFILES[0]
    assert diagnostic.diagnose(env, preflight=True, reader=reader)['status'] == 'configuration_shape_valid'
    assert calls == []
    assert set(diagnostic.diagnose(env, reader=reader)['targets']) == {diagnostic.PROFILES[0]}
    assert len(calls) == 3


@pytest.mark.parametrize('field', ['project_id', *diagnostic.IDENTITY_FIELDS])
def test_slot_scope_difference_never_guesses_an_account(materials, field):
    env, slots, _, _, reader, calls = materials
    slots['additional-1'][field] = ['DU0000099'] if field == 'account_selector' else 'different-private-value'
    env['IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON'] = json.dumps({k: v for k, v in slots.items() if k != 'primary'})
    env['REQUESTED_TARGET'] = diagnostic.PROFILES[0]
    row = diagnostic.diagnose(env, reader=reader)['targets'][diagnostic.PROFILES[0]]
    assert row['status'] == 'unknown' and row['unique_slot_match'] is False and calls == []


def test_stale_configured_revision_is_unknown(materials):
    env, _, services, _, reader, _ = materials
    services['private-service-1']['status']['traffic'][0]['revisionName'] = 'new-private-revision'
    row = diagnostic.diagnose(env, reader=reader)['targets'][diagnostic.PROFILES[0]]
    assert row['status'] == 'unknown' and row['reason'] == 'configured_revision_mismatch'
    assert row['unique_slot_match'] is True and row['serving_revision_match'] is False
    assert row['runtime_scope_match'] is None


@pytest.mark.parametrize('change', ['split', 'invalid_traffic', 'missing_revision', 'wrong_account', 'not_ready', 'secret_env'])
def test_serving_metadata_failures_stay_unknown(materials, change):
    env, _, services, revisions, reader, _ = materials
    service, revision = services['private-service-1'], revisions['private-revision-1']
    if change == 'split':
        service['status']['traffic'] = [{'revisionName': 'private-revision-1', 'percent': 50}, {'revisionName': 'other', 'percent': 50}]
    elif change == 'invalid_traffic': service['status']['traffic'][0]['percent'] = True
    elif change == 'missing_revision': del revision['metadata']['name']
    elif change == 'not_ready': revision['status']['conditions'][0]['status'] = 'False'
    elif change == 'secret_env':
        revision['spec']['containers'][0]['env'][0] = {'name': 'RUNTIME_TARGET_JSON', 'valueFrom': {'secretKeyRef': {'name': 'private-secret'}}}
    else:
        value = json.loads(revision['spec']['containers'][0]['env'][0]['value'])
        value['account_selector'] = ['DU0000099']
        revision['spec']['containers'][0]['env'][0]['value'] = json.dumps(value)
    row = diagnostic.diagnose(env, reader=reader)['targets'][diagnostic.PROFILES[0]]
    assert row['status'] == 'unknown'
    assert 'private-' not in json.dumps(row) and 'DU000' not in json.dumps(row)


def test_revision_drift_during_read_is_rejected(materials):
    env, _, _, _, original, _ = materials
    count = 0

    def reader(kind, *args):
        nonlocal count
        result = original(kind, *args)
        if kind == 'services':
            count += 1
            if count == 2: result['metadata']['resourceVersion'] = '2'
        return result
    assert diagnostic.diagnose(env, reader=reader)['targets'][diagnostic.PROFILES[0]]['reason'] == 'serving_changed_during_read'


@pytest.mark.parametrize('change', ['duplicate_target', 'duplicate_slot', 'missing_secret', 'bad_json', 'bad_target'])
def test_invalid_configuration_stops_before_metadata_reads(materials, change):
    env, slots, _, _, reader, calls = materials
    if change == 'duplicate_target':
        entries = json.loads(env['CLOUD_RUN_SERVICE_TARGETS_JSON'])
        entries['targets'].append(entries['targets'][0])
        env['CLOUD_RUN_SERVICE_TARGETS_JSON'] = json.dumps(entries)
    elif change == 'duplicate_slot':
        slots['additional-1']['account_selector'] = slots['primary']['account_selector']
        env['IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON'] = json.dumps({k: v for k, v in slots.items() if k != 'primary'})
    elif change == 'missing_secret': del env['IBKR_ACCOUNT_FACTS_ACCOUNT_SCOPE']
    elif change == 'bad_target': env['REQUESTED_TARGET'] = 'unapproved'
    else: env['CLOUD_RUN_SERVICE_TARGETS_JSON'] = 'PRIVATE_CANARY'
    with pytest.raises((ValueError, TypeError)):
        diagnostic.diagnose(env, reader=reader)
    assert calls == []


def test_cli_suppresses_raw_errors_and_child_output(monkeypatch, capsys, materials):
    env, _, _, _, _, _ = materials
    for key, value in env.items(): monkeypatch.setenv(key, value)

    def failed(*args):
        print('PRIVATE_CANARY')
        raise RuntimeError('private-service-DU0000001')
    monkeypatch.setattr(diagnostic, '_read', failed)
    assert diagnostic.main([]) == 1
    text = capsys.readouterr().out
    assert 'PRIVATE_CANARY' not in text and 'private-service' not in text and 'DU000' not in text


def test_metadata_command_is_bounded_and_only_describes(monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, b'{}', b'private-error')
    monkeypatch.setattr(diagnostic.subprocess, 'run', run)
    diagnostic._read('services', 'synthetic', 'project', 'region')
    assert calls[0][0] == ['gcloud', 'run', 'services', 'describe', 'synthetic', '--project', 'project', '--region', 'region', '--format=json']
    assert calls[0][1]['capture_output'] is True and calls[0][1]['timeout'] == 30


def test_workflow_mode_is_default_off_and_excludes_broker_job():
    root = Path(__file__).resolve().parents[1]
    caller = (root / '.github/workflows/collect-reconciliation-evidence.yml').read_text()
    protected = (root / '.github/workflows/collect-reconciliation-evidence-protected.yml').read_text()
    assert 'default: false' in caller.split('inspect_flex_runtime_binding_only:', 1)[1].split('inspect_execution_ledger:', 1)[0]
    assert '  collect:\n    if: ${{ !inputs.inspect_flex_runtime_binding_only }}' in protected
    job = protected.split('  flex-runtime-binding:', 1)[1].split('  collect:', 1)[0]
    assert 'inputs.inspect_flex_runtime_binding_only' in job and 'github.run_attempt == 1' in job
    assert 'GCP_WORKLOAD_IDENTITY_PROVIDER' in job and 'GCP_WORKLOAD_IDENTITY_SERVICE_ACCOUNT' in job
    assert 'diagnose_ibkr_flex_runtime_binding --preflight' in job
    for unsafe in ('IBKR_FLEX_TOKEN', 'IBKR_FLEX_QUERY_ID', 'storage ', 'scheduler ', 'curl ', 'upload-artifact'):
        assert unsafe not in job
