"""Exercise actual verification logic with synthetic OCI and Docker evidence."""
import hashlib
import json
import tarfile

import pytest

from scripts import patch_cash_candidate_image as patcher
from scripts import verify_account_cash_image as verifier


def test_image_verification_and_rejections(monkeypatch):
    def sha(data):
        return 'sha256:' + hashlib.sha256(data).hexdigest()
    source = ('def connect():\n' + patcher.OLD_CONNECT + '\n\ndef persist():\n' + patcher.OLD_PERSIST + '\n').encode()
    files = {'scripts/entry.py': b'pass\n'}
    def row(data):
        return (tarfile.REGTYPE, 0o644, 1000, 1000, '', hashlib.sha256(data).hexdigest())
    directory = (tarfile.DIRTYPE, 0o755, 0, 0, '', None)
    old = {name: directory for name in ('app', 'app/scripts', 'app/application')}
    old['app/main.py'] = row(source)
    new = dict(old, **{'app/main.py': row(patcher.patch(source.decode()).encode()), 'app/scripts/entry.py': row(files['scripts/entry.py'])})
    config = {'config': {'User': 'appuser'}, 'rootfs': {'diff_ids': ['old', 'new']}, 'os': 'linux', 'architecture': 'amd64'}
    config_raw = json.dumps(config).encode()
    manifest_raw = json.dumps({'mediaType': 'application/vnd.oci.image.manifest.v1+json', 'config': {'digest': sha(config_raw)}}).encode()
    repo = 'us-docker.pkg.dev/test/repo/image'
    base, candidate = repo + '@sha256:' + '0' * 64, repo + '@' + sha(manifest_raw)
    original = {'Config': config['config'], 'RootFS': {'Layers': ['old']}, 'Os': 'linux', 'Architecture': 'amd64'}
    built = {**original, 'RootFS': {'Layers': ['old', 'new']}}
    monkeypatch.setattr(verifier, '_inspect', lambda image: original if image == base else built)
    monkeypatch.setattr(verifier, '_tree', lambda image: (old, {'app/main.py': source, 'etc/passwd': b'appuser:x:1000:1000::/:/bin/sh\n'}) if image == base else (new, {'app/main.py': patcher.patch(source.decode()).encode()}))
    monkeypatch.setattr(verifier, '_blob', lambda image, digest: manifest_raw if digest == sha(manifest_raw) else config_raw)
    monkeypatch.setattr(verifier, '_remote_manifest', lambda *args: manifest_raw)
    verifier.verify(base, candidate, files, account='test')
    original_main = new['app/main.py']
    new['app/main.py'] = (tarfile.REGTYPE, 0o600, 1000, 1000, '', original_main[-1])
    with pytest.raises(ValueError, match='main_mismatch'):
        verifier.verify(base, candidate, files, account='test')
    new['app/main.py'] = original_main
    original['Config']['Volumes'] = {'/app': {}}
    with pytest.raises(ValueError, match='config_changed'):
        verifier.verify(base, candidate, files, account='test')
    del original['Config']['Volumes']
    new['app/other'] = row(b'unexpected')
    with pytest.raises(ValueError, match='filesystem_scope'):
        verifier.verify(base, candidate, files, account='test')
    del new['app/other']
    new['app/scripts/entry.py'] = row(b'wrong')
    with pytest.raises(ValueError, match='source_mismatch'):
        verifier.verify(base, candidate, files, account='test')
    new['app/scripts/entry.py'] = row(files['scripts/entry.py'])
    old['app/scripts'] = (tarfile.DIRTYPE, 0o700, 0, 0, '', None)
    with pytest.raises(ValueError, match='directory_unverified'):
        verifier.verify(base, candidate, files, account='test')
    old['app/scripts'] = directory
    new['app/scripts/entry.py'] = (tarfile.REGTYPE, 0o600, 0, 0, '', hashlib.sha256(files['scripts/entry.py']).hexdigest())
    with pytest.raises(ValueError, match='source_mismatch'):
        verifier.verify(base, candidate, files, account='test')
    new['app/scripts/entry.py'] = row(files['scripts/entry.py'])
    monkeypatch.setattr(verifier, '_remote_manifest', lambda *args: b'wrong')
    with pytest.raises(ValueError, match='registry_manifest'):
        verifier.verify(base, candidate, files, account='test')
    built['RootFS'] = {'Layers': ['different', 'new']}
    with pytest.raises(ValueError, match='base_layers'):
        verifier.verify(base, candidate, files, account='test')


def test_local_oci_blob_is_checked_from_archive(monkeypatch):
    import io
    from types import SimpleNamespace

    payload = b'{"synthetic":true}'
    digest = 'sha256:' + hashlib.sha256(payload).hexdigest()
    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode='w') as archive:
        member = tarfile.TarInfo('blobs/sha256/' + digest.split(':')[1])
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    def popen(*args, **kwargs):
        return SimpleNamespace(stdout=io.BytesIO(archive_bytes.getvalue()), wait=lambda timeout: 0,
                               poll=lambda: 0, kill=lambda: None)
    monkeypatch.setattr(verifier.subprocess, 'Popen', popen)
    assert verifier._blob('synthetic', digest) == payload
    with pytest.raises(ValueError, match='local_oci_mismatch'):
        verifier._blob('synthetic', 'sha256:' + '0' * 64)
