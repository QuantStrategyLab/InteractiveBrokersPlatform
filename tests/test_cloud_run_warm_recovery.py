"""Synthetic control-plane tests; no cloud, broker or business HTTP calls."""
import copy
from datetime import datetime, timezone
import json
from unittest import mock

import pytest

from scripts.cloud_run_candidate_warmup import MINIMUM
from scripts.cloud_run_warm_recovery import API, Recovery, RecoveryError, healthy

DIGEST = 'sha256:' + 'a' * 64
COMMIT = 'b' * 40


def revision(name, minimum):
    return {'metadata': {'name': name, 'uid': name + '-uid', 'generation': 1,
                         'labels': {'commit-sha': COMMIT}, 'annotations': {MINIMUM: str(minimum)}},
            'spec': {'containers': [{'image': 'registry.invalid/unit/image@' + DIGEST,
                'startupProbe': {'httpGet': {'path': '/health', 'port': 8080}},
                'env': [{'name': 'RUNTIME_TARGET_ENABLED', 'value': 'true'},
                        {'name': 'RUNTIME_TARGET_JSON', 'value': '{}'}]}]},
            'status': {'imageDigest': DIGEST, 'observedGeneration': 1, 'conditions': [
                {'type': kind, 'status': 'True'} for kind in ('Ready', 'Active', 'ContainerReady', 'ContainerHealthy')]}}


def card():
    warm = revision('unit-service-warm', 1)
    baseline = {'metadata': {'name': 'unit-service', 'uid': 'service-uid',
        'namespace': 'unit-project', 'resourceVersion': '1', 'generation': 1,
        'labels': {}, 'annotations': {'run.googleapis.com/operation-id': 'old'}},
        'spec': {'template': {'metadata': {'name': 'unit-service-old',
            'annotations': {MINIMUM: '0'}, 'labels': {'commit-sha': COMMIT}},
            'spec': copy.deepcopy(warm['spec'])},
            'traffic': [{'revisionName': 'unit-service-warm', 'percent': 100}]},
        'status': {'observedGeneration': 1, 'latestCreatedRevisionName': 'unit-service-old',
                   'traffic': [{'revisionName': 'unit-service-warm', 'percent': 100}]}}
    return {'scope': 's3-only', 'project': 'unit-project', 'region': 'unit-region',
        'service': 'unit-service', 'warm': warm, 'service_snapshot': baseline,
        'source_commit': COMMIT, 'operator_commit': 'c' * 40, 'image_digest': DIGEST,
        'request_id': 'attempt1', 'replacement': 'unit-service-rec-attempt1',
        'authority': 'unit-authority', 'protected_environment': 'unit-protected',
        'expires_at': '2099-01-01T00:00:00+00:00', 'restore_warm_on_failure': True}


class Fake:
    def __init__(self, approval, case='ok'):
        self.card = approval
        self.service = copy.deepcopy(approval['service_snapshot'])
        self.revisions = {approval['warm']['metadata']['name']: copy.deepcopy(approval['warm'])}
        self.writes, self.reads, self.sleeps = [], [], []
        self.case, self.seconds = case, 0

    def clock(self):
        return self.seconds

    def sleep(self, seconds):
        self.seconds += seconds
        self.sleeps.append(seconds)

    def __call__(self, kind, name='', body=None, timeout=30):
        assert 0 < timeout <= 30
        self.reads.append((kind, name))
        if kind == 'absent':
            return {'absent': name not in self.revisions}
        if kind == 'minimum':
            return {'name': 'projects/unit-project/locations/unit-region/services/unit-service',
                    'uid': 'service-uid', 'scaling': {'minInstanceCount': 1 if self.case == 'min' else 0}}
        if kind == 'revision':
            value = copy.deepcopy(self.revisions[name])
            split = len(self.writes) >= 2
            if name == self.card['replacement'] and split:
                if self.case in {'missing', 'timeout'}:
                    value['status']['conditions'] = [row for row in value['status']['conditions']
                                                      if row['type'] != 'ContainerHealthy']
                if self.case in {'false', 'unknown', 'duplicate'}:
                    value['status']['conditions'].append({'type': 'ContainerHealthy',
                        'status': 'False' if self.case == 'false' else 'Unknown'})
                if self.case == 'digest': value['status']['imageDigest'] = 'sha256:' + 'd' * 64
                if self.case == 'revision_uid': value['metadata']['uid'] = 'drift'
                if self.case == 'stale': value['status']['observedGeneration'] = 0
                if self.case == 'interrupt': raise KeyboardInterrupt()
            return value
        if body is not None:
            assert body['metadata']['resourceVersion'] == self.service['metadata']['resourceVersion']
            assert 'run.googleapis.com/operation-id' not in body['metadata']['annotations']
            self.writes.append(copy.deepcopy(body))
            index = len(self.writes)
            if self.case == 'reject_split' and index == 2: raise RecoveryError('rejected')
            if self.case == 'reject_promote' and index == 3: raise RecoveryError('rejected')
            if self.case == 'restore_unknown' and index == 4: raise RecoveryError('unknown')
            self.service['spec'] = copy.deepcopy(body['spec'])
            self.service['metadata']['generation'] += 1
            self.service['metadata']['resourceVersion'] = str(index + 1)
            self.service['metadata']['annotations']['run.googleapis.com/operation-id'] = str(index)
            self.service['status']['observedGeneration'] = self.service['metadata']['generation']
            self.service['status']['traffic'] = copy.deepcopy(body['spec']['traffic'])
            template = body['spec']['template']
            new = template['metadata']['name']
            self.service['status']['latestCreatedRevisionName'] = new
            if new not in self.revisions:
                value = revision(new, 0)
                value['spec'] = copy.deepcopy(template['spec'])
                self.revisions[new] = value
            if self.case == 'unknown_split' and index == 2: raise RecoveryError('unknown')
            if self.case in {'unknown_promote', 'restore_unknown'} and index == 3:
                raise RecoveryError('unknown')
            return copy.deepcopy(self.service)
        value = copy.deepcopy(self.service)
        if len(self.writes) >= 2:
            if self.case == 'service_uid': value['metadata']['uid'] = 'drift'
            if self.case == 'service_spec': value['spec']['template']['spec']['concurrency'] = 2
            if self.case == 'other_traffic':
                value['status']['traffic'] = [{'revisionName': 'unit-other', 'percent': 100}]
        return value


def run(tmp_path, case='ok', approval=None):
    approval = approval or card()
    cloud = Fake(approval, case)
    obj = Recovery(approval, tmp_path / 'attempt', api=cloud, clock=cloud.clock, sleep=cloud.sleep)
    return obj, cloud


@pytest.fixture(autouse=True)
def continuity():
    with mock.patch('scripts.runtime_target_continuity.verify_target_continuity'):
        yield


def test_success_uses_retained_digest_and_min_zero_only(tmp_path):
    obj, cloud = run(tmp_path)
    obj.execute()
    assert len(cloud.writes) == 3
    assert cloud.writes[0]['spec']['template']['metadata']['annotations'][MINIMUM] == '0'
    assert cloud.writes[0]['spec']['template']['spec'] == card()['service_snapshot']['spec']['template']['spec']
    assert cloud.writes[1]['spec']['traffic'] == [
        {'revisionName': 'unit-service-warm', 'percent': 99},
        {'revisionName': 'unit-service-rec-attempt1', 'percent': 1}]
    assert cloud.writes[2]['spec']['traffic'] == [{'revisionName': 'unit-service-rec-attempt1', 'percent': 100}]
    assert cloud.sleeps == [5, 5]
    assert cloud.reads.count(('minimum', '')) == 2
    assert json.loads(obj.attempt.read_text())['status'] == 'SUCCEEDED'
    with pytest.raises(FileExistsError): obj.execute()
    assert len(cloud.writes) == 3


@pytest.mark.parametrize('case', ['missing', 'timeout', 'false', 'unknown', 'duplicate', 'digest',
    'revision_uid', 'stale', 'interrupt', 'reject_split', 'unknown_split', 'reject_promote', 'unknown_promote'])
def test_failed_forward_path_restores_warm_once_and_never_replays(tmp_path, case):
    obj, cloud = run(tmp_path, case)
    with pytest.raises(RecoveryError): obj.execute()
    assert cloud.service['status']['traffic'] == [{'revisionName': 'unit-service-warm', 'percent': 100}]
    status = json.loads(obj.attempt.read_text())['status']
    assert status == 'FAILED_WARM_RESTORED'
    assert sum(write['spec']['traffic'][0].get('percent') == 99 for write in cloud.writes) == 1
    assert cloud.seconds <= 310
    with pytest.raises(FileExistsError): obj.execute()


@pytest.mark.parametrize('case', ['service_uid', 'service_spec', 'other_traffic', 'restore_unknown'])
def test_drift_or_failed_compensation_is_unconfirmed_and_never_blindly_retried(tmp_path, case):
    obj, cloud = run(tmp_path, case)
    with pytest.raises(RecoveryError): obj.execute()
    assert json.loads(obj.attempt.read_text())['status'] == 'FAILED_RESTORE_UNCONFIRMED'
    assert len(cloud.writes) == (4 if case == 'restore_unknown' else 2)


@pytest.mark.parametrize('field,value', [('image_digest', ''), ('scope', 'peer1'),
    ('source_commit', 'main'), ('restore_warm_on_failure', False),
    ('expires_at', '2000-01-01T00:00:00+00:00'), ('replacement', 'unit-other')])
def test_malformed_or_expired_card_cannot_write(tmp_path, field, value):
    approval = card()
    approval[field] = value
    with pytest.raises((RecoveryError, ValueError)):
        run(tmp_path, approval=approval)


def test_baseline_drift_stops_before_creation(tmp_path):
    obj, cloud = run(tmp_path)
    cloud.service['metadata']['resourceVersion'] = 'changed'
    with pytest.raises(RecoveryError): obj.execute()
    assert cloud.writes == []


def test_service_minimum_one_is_not_revision_minimum_one_exception(tmp_path):
    obj, cloud = run(tmp_path, 'min')
    with pytest.raises(RecoveryError): obj.execute()
    assert cloud.writes == []


def test_false_duplicate_or_unknown_health_never_counts_as_healthy():
    value = card()['warm']
    value['status']['conditions'].append({'type': 'Ready', 'status': 'False'})
    with pytest.raises(ValueError): healthy(value, COMMIT)


def test_approval_expiry_after_activation_restores_without_forward_write(tmp_path):
    obj, cloud = run(tmp_path)
    obj.expires = datetime(2030, 1, 1, tzinfo=timezone.utc)
    obj.now = lambda: datetime(2029 if len(cloud.writes) < 2 else 2031, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(RecoveryError): obj.execute()
    assert len(cloud.writes) == 3
    assert json.loads(obj.attempt.read_text())['status'] == 'FAILED_WARM_RESTORED'


def test_provider_failure_is_redacted_and_never_retried():
    api = API('unit-project', 'unit-region', 'unit-service')
    with mock.patch('scripts.cloud_run_warm_recovery.subprocess.run',
                    side_effect=RuntimeError('private-token-and-host')) as run:
        with pytest.raises(RecoveryError, match='cloud operation') as error:
            api('service')
    assert 'private' not in str(error.value)
    assert run.call_count == 1


def test_request_identity_cannot_adopt_an_existing_revision(tmp_path):
    obj, cloud = run(tmp_path)
    cloud.revisions[obj.card['replacement']] = revision(obj.card['replacement'], 0)
    with pytest.raises(RecoveryError): obj.execute()
    assert cloud.writes == []


def test_baseline_warm_health_loss_stops_before_create(tmp_path):
    obj, cloud = run(tmp_path)
    cloud.revisions[obj.warm]['status']['conditions'] = []
    with pytest.raises(RecoveryError): obj.execute()
    assert cloud.writes == []


def test_post_promotion_failure_can_restore_temporarily_retired_warm(tmp_path):
    obj, cloud = run(tmp_path, 'unknown_promote')
    original = cloud.__call__
    def api(kind, name='', body=None, timeout=30):
        value = original(kind, name, body, timeout)
        if kind == 'revision' and name == obj.warm and len(cloud.writes) == 3:
            value['status']['conditions'] = []
        return value
    obj.api = api
    with pytest.raises(RecoveryError): obj.execute()
    assert json.loads(obj.attempt.read_text())['status'] == 'FAILED_WARM_RESTORED'


def test_cli_checks_pin_and_environment_before_cloud(tmp_path):
    import sys
    from scripts.cloud_run_warm_recovery import main
    path = tmp_path / 'card.json'
    path.write_text(json.dumps(card()))
    path.chmod(0o600)
    with mock.patch.object(sys, 'argv', ['recovery', '--card', str(path),
            '--attempt', str(tmp_path / 'result')]), \
         mock.patch('scripts.cloud_run_warm_recovery.subprocess.run',
                    return_value=mock.Mock(stdout='wrong-head')), \
         mock.patch('scripts.cloud_run_warm_recovery.API') as api:
        with pytest.raises(SystemExit) as error: main()
        assert error.value.code == 1
        api.assert_not_called()


def test_template_must_preserve_the_verified_warm_configuration(tmp_path):
    approval = card()
    approval['service_snapshot']['spec']['template']['spec']['containerConcurrency'] = 2
    obj, cloud = run(tmp_path, approval=approval)
    with pytest.raises(RecoveryError): obj.execute()
    assert cloud.writes == []


def test_control_plane_convergence_never_accepts_a_late_read(tmp_path):
    obj, cloud = run(tmp_path)
    original = cloud.__call__
    def api(kind, name='', body=None, timeout=30):
        value = original(kind, name, body, timeout)
        if kind == 'service' and body is None and len(cloud.writes) == 1:
            cloud.seconds += 61
        return value
    obj.api = api
    with pytest.raises(RecoveryError): obj.execute()
    assert len(cloud.writes) == 1
    assert cloud.service['status']['traffic'] == [{'revisionName': 'unit-service-warm', 'percent': 100}]


def test_unexpected_provider_provisioning_value_is_not_published(tmp_path):
    obj, cloud = run(tmp_path)
    cloud.revisions[obj.warm]['status']['conditions'].append(
        {'type': 'MinInstancesProvisioned', 'status': 'private-provider-value'})
    obj.execute()
    assert 'private-provider-value' not in obj.attempt.read_text()
    assert all(row['warm_minimum_one_provisioned'] == 'Unknown' for row in obj.minima)
