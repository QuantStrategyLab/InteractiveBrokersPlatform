"""Offline bootstrap boundaries; all provider bindings are fake."""
import copy
from datetime import datetime, timezone
import hashlib
import io
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

import pytest

from scripts.recovery_evidence_bootstrap import Bootstrap, BootstrapError, main

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


def config():
    return dict(authority='fake-authority', protected_environment='fake-protected',
                project='fake-project', region='fake-region', service='fake-service',
                warm_revision='fake-service-warm', request_id='fake1', bucket_suffix='fakeunique1',
                source_commit='a' * 40, operator_commit='b' * 40,
                image_digest='sha256:' + 'c' * 64, restore_warm_on_failure=True,
                issued_at='2030-01-01T00:00:00+00:00', expires_at='2030-01-01T01:00:00+00:00')


class Fake:
    def __init__(self):
        self.service = {'metadata': {'name': 'fake-service', 'namespace': 'fake-project',
            'uid': 'fake-uid', 'resourceVersion': '1', 'generation': 1},
            'spec': {'traffic': [{'revisionName': 'fake-service-warm', 'percent': 100}]}}
        self.warm = {'metadata': {'name': 'fake-service-warm', 'uid': 'fake-warm-uid',
            'generation': 1, 'labels': {'commit-sha': 'a' * 40},
            'annotations': {'autoscaling.knative.dev/minScale': '1'}},
            'spec': {'containers': [{'resources': {'limits': {'cpu': '1', 'memory': '512Mi'}}}]},
            'status': {'imageDigest': 'sha256:' + 'c' * 64}}
        self.minimum = {'name': 'projects/fake-project/locations/fake-region/services/fake-service',
                        'uid': 'fake-uid', 'scaling': {'minInstanceCount': 0}}
        self.bucket = None
        self.objects = {}
        self.calls = []
        self.lock = Lock()
        self.iam = {'bindings': []}
        self.drift = False
        self.reads = 0

    def __call__(self, kind, **kwargs):
        with self.lock:
            self.calls.append(kind)
            if kind == 'service':
                self.reads += 1
                value = copy.deepcopy(self.service)
                if self.drift and self.reads > 1:
                    value['metadata']['resourceVersion'] = 'changed'
                return value
            if kind == 'warm': return copy.deepcopy(self.warm)
            if kind == 'minimum': return copy.deepcopy(self.minimum)
            if kind == 'create_bucket':
                if self.bucket is not None: raise BootstrapError('bootstrap failed')
                self.bucket = copy.deepcopy(kwargs['body'])
                return self.bucket
            if kind == 'bucket': return copy.deepcopy(self.bucket)
            if kind == 'iam': return self.iam
            if kind == 'upload':
                key = kwargs['name']
                if key in self.objects: raise BootstrapError('bootstrap failed')
                self.objects[key] = kwargs['data']
                return {}
            if kind == 'download': return self.objects[kwargs['name']]
            raise AssertionError(kind)


def run(cloud=None, value=None, now=lambda: NOW):
    cloud = cloud or Fake()
    return Bootstrap(value or config(), api=cloud, now=now,
                     operator='b' * 40, environment='fake-protected').execute(), cloud


def test_private_card_contract_and_exact_object_readbacks():
    _, cloud = run()
    card_bytes = cloud.objects['s3-recovery/fake1/recovery-card.json']
    card = json.loads(card_bytes)
    assert card['scope'] == 's3-only'
    assert card['warm'] == cloud.warm and card['service_snapshot'] == cloud.service
    assert card['replacement'] == 'fake-service-rec-fake1'
    receipt = json.loads(cloud.objects['s3-recovery/fake1/receipt.json'])
    assert receipt['card_sha256'] == hashlib.sha256(card_bytes).hexdigest()
    assert receipt['pricing_status'] == 'NOT_PROVEN'
    assert set(cloud.calls) <= {'service', 'warm', 'minimum', 'create_bucket', 'bucket',
                                'iam', 'upload', 'download'}


@pytest.mark.parametrize('field,value', [
    ('expires_at', '2029-01-01T00:00:00+00:00'),
    ('expires_at', '2030-01-01T01:01:00+00:00'),
    ('issued_at', '2030-01-01T00:01:00+00:00'),
    ('operator_commit', 'd' * 40), ('protected_environment', ''),
    ('restore_warm_on_failure', False), ('bucket_suffix', '../bad'),
    ('request_id', 'a/b'), ('source_commit', 'main'), ('image_digest', '')])
def test_invalid_authority_cannot_create_storage(field, value):
    cloud, approval = Fake(), config()
    approval[field] = value
    with pytest.raises((BootstrapError, ValueError)): run(cloud, approval)
    assert 'create_bucket' not in cloud.calls


def test_stale_service_does_not_publish_card():
    cloud = Fake()
    cloud.drift = True
    with pytest.raises(BootstrapError): run(cloud)
    assert not cloud.objects


def test_concurrent_or_restarted_request_cannot_adopt_existing_bucket():
    cloud = Fake()
    def attempt():
        try:
            run(cloud)
            return True
        except BootstrapError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: attempt(), range(2))) == [False, True]
    assert attempt() is False
    assert cloud.calls.count('upload') == 3


def test_unknown_bucket_create_outcome_is_not_retried():
    cloud = Fake()
    def api(kind, **kwargs):
        result = cloud(kind, **kwargs)
        if kind == 'create_bucket': raise TimeoutError('fake private provider detail')
        return result
    with pytest.raises(TimeoutError):
        Bootstrap(config(), api=api, now=lambda: NOW, operator='b' * 40,
                  environment='fake-protected').execute()
    assert cloud.calls.count('create_bucket') == 1
    assert not cloud.objects


@pytest.mark.parametrize('case', ['public', 'lifecycle', 'uniform', 'readback', 'expiry'])
def test_integrity_or_expiry_failure_blocks_card(case):
    cloud = Fake()
    def api(kind, **kwargs):
        result = cloud(kind, **kwargs)
        if case == 'public' and kind == 'iam':
            return {'bindings': [{'members': ['allUsers']}]}
        if kind == 'bucket':
            if case == 'lifecycle': result['lifecycle'] = {}
            if case == 'uniform': result['iamConfiguration']['uniformBucketLevelAccess']['enabled'] = False
        if case == 'readback' and kind == 'download': return b'corrupt'
        return result
    def now():
        return datetime(2031, 1, 1, tzinfo=timezone.utc) if case == 'expiry' and cloud.bucket else NOW
    with pytest.raises(BootstrapError):
        Bootstrap(config(), api=api, now=now, operator='b' * 40,
                  environment='fake-protected').execute()
    assert 's3-recovery/fake1/recovery-card.json' not in cloud.objects


def test_cli_suppresses_private_provider_exception(monkeypatch, capsys):
    monkeypatch.setenv('IBKR_RECOVERY_BOOTSTRAP_JSON', 'not json fake-private-detail')
    assert main() == 1
    output = capsys.readouterr()
    assert output.out == '' and 'private-detail' not in output.err


def test_shared_masker_handles_private_configuration():
    from scripts.mask_sync_identifiers import mask_identifiers
    output = io.StringIO()
    mask_identifiers({'IBKR_RECOVERY_BOOTSTRAP_JSON': json.dumps(config())}, output)
    assert 'fake-project' in output.getvalue()


def test_transport_uses_create_only_preconditions_and_captures_diagnostics(monkeypatch):
    from scripts import recovery_evidence_bootstrap as module
    from unittest.mock import Mock
    requests = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{}'
    def open_request(request, timeout):
        requests.append(request)
        assert timeout == 30
        return Response()
    monkeypatch.setattr(module.subprocess, 'run', Mock(return_value=Mock(stdout='fake-token')))
    monkeypatch.setattr(module, 'urlopen', open_request)
    api = module.API(config())
    api('upload', name='s3-recovery/fake1/recovery-card.json', data=b'{}')
    assert requests[0].method == 'POST'
    assert 'ifGenerationMatch=0' in requests[0].full_url
    monkeypatch.setattr(module, 'urlopen', Mock(side_effect=RuntimeError('fake-private-detail')))
    with pytest.raises(BootstrapError) as error: api('bucket')
    assert 'private-detail' not in str(error.value)
    assert module.urlopen.call_count == 1


def test_manual_protected_workflow_never_dispatches_recovery():
    from pathlib import Path
    text = Path('.github/workflows/bootstrap-recovery-evidence.yml').read_text()
    assert '  workflow_dispatch:\n' in text
    assert 'run_attempt == 1' in text and "refs/heads/main" in text
    assert 'environment: ${{ vars.IBKR_DEPLOY_ENVIRONMENT }}' in text
    assert 'group: ibkr-retained-warm-recovery' in text
    assert 'cancel-in-progress: false' in text
    assert '  contents: read\n  id-token: write' in text
    assert text.count('run:') == 1
    assert 'run: python -m scripts.recovery_evidence_bootstrap' in text
    assert 'upload-artifact' not in text
    assert 'pull_request' not in text and 'schedule:' not in text


def test_transport_refuses_redirect_with_credentials():
    from scripts.recovery_evidence_bootstrap import NoRedirect
    from urllib.request import Request
    with pytest.raises(BootstrapError):
        NoRedirect().redirect_request(Request('https://example.invalid'), None, 302,
                                      'redirect', {}, 'https://other.invalid')
