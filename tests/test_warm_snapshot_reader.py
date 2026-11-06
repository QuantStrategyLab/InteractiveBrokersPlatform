"""Offline read-only transport, drift and encrypted export boundaries."""
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from scripts import warm_snapshot_reader as module
from scripts.recovery_evidence_bootstrap import BootstrapError
from tests.test_recovery_evidence_bootstrap import Fake, config


def reader(cloud=None):
    cloud = cloud or Fake()
    cloud.warm['metadata']['namespace'] = 'fake-project'
    calls = []
    def get(url):
        calls.append(url)
        if '/revisions/' in url: value = cloud('warm')
        elif '/v2/' in url: value = cloud('minimum')
        elif '/services/' in url: value = cloud('service')
        elif 'cloudresourcemanager' in url: value = cloud('project')
        else:
            from urllib.parse import unquote
            return cloud.objects[unquote(url.split('/o/')[1].split('?')[0])]
        return json.dumps(value).encode()
    return module.Reader(config(), environment='fake-protected', get=get), cloud, calls


def test_missing_receipt_fails_instead_of_exporting_success():
    instance, _, _ = reader()
    with pytest.raises(KeyError): instance.collect('receipt')


def test_changed_snapshot_blocks_export():
    instance, cloud, _ = reader()
    cloud.drift = True
    with pytest.raises(BootstrapError): instance.collect('snapshot')


def test_readonly_export_has_all_live_bootstrap_fields():
    instance, _, calls = reader()
    value = instance.collect('snapshot')
    assert set(('project', 'region', 'service', 'warm_revision', 'source_commit', 'image_digest')) <= value['snapshot'].keys()
    assert value['receipt_status'] == 'NOT_REQUESTED'
    assert value['recovery_dispatched'] is False
    assert len(calls) == 8
    assert all(url.startswith('https://') for url in calls)


@pytest.mark.parametrize('case', ['namespace', 'digest', 'traffic', 'minimum', 'project'])
def test_invalid_live_state_is_rejected(case):
    instance, cloud, _ = reader()
    if case == 'namespace': cloud.warm['metadata']['namespace'] = 'fake-foreign'
    if case == 'digest': cloud.warm['status']['imageDigest'] = 'invalid'
    if case == 'traffic': cloud.service['spec']['traffic'].append({'latestRevision': True, 'percent': 0})
    if case == 'minimum': cloud.minimum['scaling']['minInstanceCount'] = 1
    if case == 'project': cloud.project['projectId'] = 'fake-foreign'
    with pytest.raises(BootstrapError): instance.collect('snapshot')


def test_transport_can_only_send_get_and_captures_token(monkeypatch):
    instance, _, _ = reader()
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = b'{}'
    open_request = Mock(return_value=response)
    monkeypatch.setattr(module, 'urlopen', open_request)
    monkeypatch.setattr(module.subprocess, 'run', Mock(return_value=Mock(stdout='fake-token')))
    assert instance.request('https://example.invalid') == b'{}'
    sent = open_request.call_args.args[0]
    assert sent.method == 'GET' and sent.data is None
    assert module.subprocess.run.call_args.kwargs['capture_output'] is True


def test_receipt_hash_and_operator_binding():
    from scripts.recovery_evidence_bootstrap import Bootstrap
    from tests.test_recovery_evidence_bootstrap import NOW
    instance, cloud, _ = reader()
    Bootstrap(config(), operator='b' * 40, environment='fake-protected',
              api=cloud, now=lambda: NOW).execute()
    assert instance.collect('receipt')['receipt_status'] == 'READ_VERIFIED'
    name = 's3-recovery/fake1/recovery-card.json'
    original = cloud.objects[name]
    cloud.objects[name] = original + b' '
    with pytest.raises(BootstrapError): instance.collect('receipt')
    cloud.objects[name] = original
    instance.config = {**config(), 'operator_commit': 'd' * 40}
    with pytest.raises(BootstrapError): instance.collect('receipt')


def test_invalid_private_configuration_never_leaks(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv('IBKR_RECOVERY_READER_JSON', 'fake-private-malformed')
    monkeypatch.setenv('RUNNER_TEMP', str(tmp_path))
    assert module.main() == 1
    output = capsys.readouterr()
    assert 'fake-private-malformed' not in output.out + output.err
    assert not list(tmp_path.rglob('*.cms'))


def test_cms_roundtrip_and_exclusive_export(tmp_path):
    key, cert = tmp_path / 'key.pem', tmp_path / 'cert.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:3072', '-nodes',
                    '-subj', '/CN=fake-coordinator', '-keyout', str(key), '-out', str(cert),
                    '-days', '1'], check=True, capture_output=True)
    certificate = cert.read_text()
    cert.unlink()
    payload = {'private': 'fake-private-readback'}
    previous = os.umask(0o077)
    try:
        module.encrypt(payload, certificate, tmp_path)
    finally:
        os.umask(previous)
    encrypted = tmp_path / 'warm-readback.cms'
    assert b'fake-private-readback' not in encrypted.read_bytes()
    assert encrypted.stat().st_mode & 0o777 == 0o600
    decrypted = subprocess.run(['openssl', 'cms', '-decrypt', '-inform', 'DER',
                                '-inkey', str(key), '-in', str(encrypted)],
                               capture_output=True, check=True).stdout
    assert json.loads(decrypted) == payload
    with pytest.raises(FileExistsError): module.encrypt(payload, certificate, tmp_path)
    assert not (tmp_path / 'coordinator.pem').exists()
    damaged = bytearray(encrypted.read_bytes())
    damaged[-1] ^= 1
    encrypted.write_bytes(damaged)
    assert subprocess.run(['openssl', 'cms', '-decrypt', '-inform', 'DER',
                           '-inkey', str(key), '-in', str(encrypted)], capture_output=True).returncode != 0


def test_workflow_only_exports_ciphertext_after_success():
    text = Path('.github/workflows/read-warm-recovery-snapshot.yml').read_text()
    assert 'refs/heads/main' in text and 'run_attempt == 1' in text
    assert 'environment: ${{ vars.IBKR_DEPLOY_ENVIRONMENT }}' in text
    assert 'group: ibkr-retained-warm-recovery' in text
    assert 'cancel-in-progress: false' in text
    assert 'warm-reader/warm-readback.cms' in text
    assert 'if-no-files-found: error' in text
    assert 'always()' not in text
    assert 'pull_request:' not in text and 'schedule:' not in text
    assert 'IBKR_RECOVERY_BOOTSTRAP_JSON' not in text
    assert 'cloud_run_warm_recovery' not in text


def test_restarted_export_directory_fails_closed(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv('IBKR_RECOVERY_READER_JSON', json.dumps(config()))
    monkeypatch.setenv('IBKR_RECOVERY_READER_CERT_PEM', 'fake-certificate')
    monkeypatch.setenv('RECOVERY_PROTECTED_ENVIRONMENT', 'fake-protected')
    monkeypatch.setenv('READER_MODE', 'snapshot')
    monkeypatch.setenv('RUNNER_TEMP', str(tmp_path))
    (tmp_path / 'warm-reader').mkdir()
    transport = Mock()
    monkeypatch.setattr(module.Reader, 'request', transport)
    assert module.main() == 1
    transport.assert_not_called()
    assert not list(tmp_path.rglob('*.cms'))
    assert 'fake-certificate' not in capsys.readouterr().err


def test_unknown_provider_failure_has_no_retry_or_private_diagnostics(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv('IBKR_RECOVERY_READER_JSON', json.dumps(config()))
    monkeypatch.setenv('IBKR_RECOVERY_READER_CERT_PEM', 'fake-certificate')
    monkeypatch.setenv('RECOVERY_PROTECTED_ENVIRONMENT', 'fake-protected')
    monkeypatch.setenv('READER_MODE', 'snapshot')
    monkeypatch.setenv('RUNNER_TEMP', str(tmp_path))
    def encrypt(payload, cert, root):
        (root / 'warm-readback.cms').write_bytes(b'fake-encrypted-probe')
    monkeypatch.setattr(module, 'encrypt', encrypt)
    transport = Mock(side_effect=TimeoutError('fake-private-provider-detail'))
    monkeypatch.setattr(module.Reader, 'request', transport)
    assert module.main() == 1
    assert transport.call_count == 1
    assert 'fake-private-provider-detail' not in capsys.readouterr().err
    assert not list(tmp_path.rglob('*.cms'))


def test_receipt_changing_between_reads_blocks_export():
    from scripts.recovery_evidence_bootstrap import Bootstrap
    from tests.test_recovery_evidence_bootstrap import NOW
    instance, cloud, _ = reader()
    Bootstrap(config(), operator='b' * 40, environment='fake-protected',
              api=cloud, now=lambda: NOW).execute()
    original = instance.get
    count = [0]
    def get(url):
        result = original(url)
        if 'receipt.json' in url:
            count[0] += 1
            if count[0] > 1: return result + b' '
        return result
    instance.get = get
    with pytest.raises(BootstrapError): instance.collect('receipt')
