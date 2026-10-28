"""Verify one cash image against its base, source bytes and registry OCI bytes.

Adapted from the SOXL filesystem and OCI verification procedure. Run only on
its protected Docker host; exported files and registry credentials stay there.
"""
import hashlib
import json
import re
import subprocess
import tarfile
import urllib.request

from scripts.patch_cash_candidate_image import patch


def _run(args):
    result = subprocess.run(args, capture_output=True, timeout=120, check=False)
    if result.returncode:
        raise ValueError('image_command_failed')
    return result.stdout


def _inspect(image):
    rows = json.loads(_run(['docker', 'image', 'inspect', image]))
    if len(rows) != 1 or image not in (rows[0].get('RepoDigests') or []):
        raise ValueError('image_digest_not_local')
    return rows[0]


def _tree(image):
    container = _run(['docker', 'create', '--network=none', image]).decode().strip()
    if not re.fullmatch(r'[0-9a-f]{64}', container):
        raise ValueError('container_identity_invalid')
    process = None
    try:
        process = subprocess.Popen(['docker', 'export', container], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        entries, selected = {}, {}
        with tarfile.open(fileobj=process.stdout, mode='r|') as archive:
            for member in archive:
                name = member.name.removeprefix('./').rstrip('/')
                if name in entries:
                    raise ValueError('duplicate_filesystem_entry')
                digest, content = None, []
                if member.isfile():
                    stream = archive.extractfile(member)
                    hashed = hashlib.sha256()
                    while chunk := stream.read(1024 * 1024):
                        hashed.update(chunk)
                        if name in {'app/main.py', 'etc/passwd'}:
                            content.append(chunk)
                    digest = hashed.hexdigest()
                    if content:
                        selected[name] = b''.join(content)
                entries[name] = (member.type, member.mode, member.uid, member.gid, member.linkname, digest)
        if process.wait(timeout=120):
            raise ValueError('image_export_failed')
        return entries, selected
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        _run(['docker', 'rm', container])


def _blob(image, digest):
    path = 'blobs/sha256/' + digest.removeprefix('sha256:')
    process = subprocess.Popen(['docker', 'image', 'save', image], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        found = []
        with tarfile.open(fileobj=process.stdout, mode='r|') as archive:
            for member in archive:
                if member.name == path:
                    if not member.isfile() or member.size > 1_000_000:
                        raise ValueError('local_oci_invalid')
                    found.append(archive.extractfile(member).read())
        if process.wait(timeout=120) or len(found) != 1 or 'sha256:' + hashlib.sha256(found[0]).hexdigest() != digest:
            raise ValueError('local_oci_mismatch')
        return found[0]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def _remote_manifest(repo, digest, account):
    host, path = repo.split('/', 1)
    token = _run(['gcloud', 'auth', 'print-access-token', '--account=' + account]).decode().strip()
    request = urllib.request.Request('https://' + host + '/v2/' + path + '/manifests/' + digest,
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.oci.image.manifest.v1+json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read(1_000_001)


def verify(base, candidate, sources, *, account):
    """Reject any mismatch; no returned assertion can substitute for this check."""
    base_repo, base_digest = base.rsplit('@', 1)
    repo, digest = candidate.rsplit('@', 1)
    if (repo != base_repo or not re.fullmatch(r'[a-z0-9-]+-docker\.pkg\.dev/.+', repo)
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest) or digest == base_digest
            or not isinstance(account, str) or not account.strip()):
        raise ValueError('candidate_binding_invalid')
    original, built = _inspect(base), _inspect(candidate)
    if (original.get('Config') != built.get('Config') or original.get('Os') != 'linux'
            or original.get('Architecture') != 'amd64' or built.get('Os') != 'linux'
            or built.get('Architecture') != 'amd64' or original.get('Config', {}).get('Volumes')):
        raise ValueError('candidate_config_changed')
    old_layers, new_layers = original.get('RootFS', {}).get('Layers', []), built.get('RootFS', {}).get('Layers', [])
    if not old_layers or new_layers[:len(old_layers)] != old_layers or len(new_layers) <= len(old_layers):
        raise ValueError('candidate_base_layers_changed')
    old, old_selected = _tree(base)
    new, new_selected = _tree(candidate)
    user = original.get('Config', {}).get('User')
    passwd = old_selected.get('etc/passwd', b'').decode('utf-8').splitlines()
    rows = [line.split(':') for line in passwd if line.split(':', 1)[0] == user]
    if user != 'appuser' or len(rows) != 1 or rows[0][2:4] != ['1000', '1000']:
        raise ValueError('candidate_user_unverified')
    for name in ('app', 'app/scripts', 'app/application'):
        row = old.get(name)
        if row is None or row[0] != tarfile.DIRTYPE:
            raise ValueError('candidate_directory_unverified')
        mode = row[1] >> (6 if row[2] == 1000 else 3 if row[3] == 1000 else 0)
        if not mode & 1:
            raise ValueError('candidate_directory_unverified')
    allowed = {'app/main.py', *('app/' + name for name in sources)}
    if {name for name in old.keys() | new.keys() if old.get(name) != new.get(name)} - allowed:
        raise ValueError('candidate_filesystem_scope_changed')
    if (not old.get('app/main.py') or not new.get('app/main.py')
            or old['app/main.py'][:-1] != new['app/main.py'][:-1]
            or new_selected.get('app/main.py') != patch(old_selected['app/main.py'].decode()).encode()):
        raise ValueError('candidate_main_mismatch')
    for name, data in sources.items():
        row = new.get('app/' + name)
        if row is None or row[0] != tarfile.REGTYPE or row[-1] != hashlib.sha256(data).hexdigest() or row[2:4] != (1000, 1000) or row[1] != 0o644:
            raise ValueError('candidate_source_mismatch')
    local = _blob(candidate, digest)
    manifest = json.loads(local)
    config_digest = manifest.get('config', {}).get('digest', '')
    if (manifest.get('mediaType') != 'application/vnd.oci.image.manifest.v1+json'
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', config_digest)):
        raise ValueError('candidate_manifest_invalid')
    config = json.loads(_blob(candidate, config_digest))
    if (config.get('config') != built.get('Config') or config.get('rootfs', {}).get('diff_ids') != new_layers
            or config.get('os') != 'linux' or config.get('architecture') != 'amd64'
            or config.get('variant') != built.get('Variant')):
        raise ValueError('candidate_config_binding_mismatch')
    if _remote_manifest(repo, digest, account) != local:
        raise ValueError('candidate_registry_manifest_mismatch')
