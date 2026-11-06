"""Private storage/snapshot bootstrap. Never writes Cloud Run or invokes recovery."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import re
import subprocess
import sys
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from scripts.mask_sync_identifiers import mask_identifiers


class BootstrapError(RuntimeError):
    """Fixed diagnostic; provider data stays private."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BootstrapError('bootstrap provider redirect refused')


urlopen = build_opener(NoRedirect()).open


def require(value):
    if not value:
        raise BootstrapError('bootstrap binding or readback failed')


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


LIFECYCLE = {'rule': [{'action': {'type': 'Delete'}, 'condition': {'age': 7}}]}


class API:
    """Single attempts with captured credentials/diagnostics; no SDK retries."""
    def __init__(self, config, *, now=lambda: datetime.now(timezone.utc)):
        self.config, self.now = config, now
        self.bucket = 'ibkr-recovery-evidence-' + config['bucket_suffix']

    def __call__(self, kind, *, body=None, name=None, data=None):
        c = self.config
        base = ('https://' + c['region'] + '-run.googleapis.com/apis/serving.knative.dev/v1/'
                'namespaces/' + c['project'])
        bucket = 'https://storage.googleapis.com/storage/v1/b/' + self.bucket
        method = 'GET'
        if kind == 'service': url = base + '/services/' + c['service']
        elif kind == 'project':
            url = 'https://cloudresourcemanager.googleapis.com/v1/projects/' + c['project']
        elif kind == 'warm': url = base + '/revisions/' + c['warm_revision']
        elif kind == 'minimum':
            url = ('https://run.googleapis.com/v2/projects/' + c['project']
                   + '/locations/' + c['region'] + '/services/' + c['service'])
        elif kind == 'create_bucket':
            url = 'https://storage.googleapis.com/storage/v1/b?project=' + c['project']
            method, data = 'POST', encode(body)
        elif kind == 'bucket': url = bucket
        elif kind == 'iam': url = bucket + '/iam'
        elif kind == 'upload':
            url = ('https://storage.googleapis.com/upload/storage/v1/b/' + self.bucket
                   + '/o?uploadType=media&ifGenerationMatch=0&name=' + quote(name, safe=''))
            method = 'POST'
        elif kind == 'download': url = bucket + '/o/' + quote(name, safe='') + '?alt=media'
        else: raise BootstrapError('unsupported bootstrap operation')
        try:
            token = subprocess.run(['gcloud', 'auth', 'print-access-token'], capture_output=True,
                                   text=True, check=True, timeout=15).stdout.strip()
            require(token)
            request = Request(url, data=data, method=method,
                              headers={'Authorization': 'Bearer ' + token,
                                       'Content-Type': 'application/json'})
            # Authentication can consume the remaining approval window. Recheck
            # at the transport boundary, after token acquisition, for every POST.
            if method == 'POST':
                require(datetime.fromisoformat(c['issued_at']) <= self.now()
                        < datetime.fromisoformat(c['expires_at']))
            with urlopen(request, timeout=30) as response:
                result = response.read()
            return result if kind == 'download' else json.loads(result)
        except Exception:
            raise BootstrapError('bootstrap provider operation failed or outcome unknown') from None


class Bootstrap:
    def __init__(self, config, *, operator, environment, api=None,
                 now=lambda: datetime.now(timezone.utc)):
        self.config, self.now = config, now
        c = config
        for key in ('project', 'region', 'service', 'warm_revision'):
            require(isinstance(c[key], str) and re.fullmatch(r'[a-z][a-z0-9-]{0,62}', c[key]))
        require(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,30}', c['request_id']))
        require(re.fullmatch(r'[a-z0-9]{8,32}', c['bucket_suffix']))
        require(c['warm_revision'].startswith(c['service'] + '-'))
        require(c['operator_commit'] == operator and re.fullmatch(r'[a-f0-9]{40}', operator))
        require(re.fullmatch(r'[a-f0-9]{40}', c['source_commit']))
        require(re.fullmatch(r'sha256:[a-f0-9]{64}', c['image_digest']))
        require(environment and c['protected_environment'] == environment and c['authority'])
        require(c['restore_warm_on_failure'] is True)
        self.issued = datetime.fromisoformat(c['issued_at'])
        self.expires = datetime.fromisoformat(c['expires_at'])
        require(self.issued.tzinfo is not None and self.expires.tzinfo is not None)
        require(0 < (self.expires - self.issued).total_seconds() <= 3600)
        require(len(c['service'] + '-rec-' + c['request_id']) <= 63)
        self.valid()
        self.bucket = 'ibkr-recovery-evidence-' + c['bucket_suffix']
        self.prefix = 's3-recovery/' + c['request_id'] + '/'
        self.api = api or API(c, now=now)

    def valid(self):
        require(self.issued <= self.now() < self.expires)

    def snapshot(self):
        c = self.config
        project = self.api('project')
        require(project['projectId'] == c['project'])
        number = project['projectNumber']
        require(isinstance(number, str) and re.fullmatch(r'[1-9][0-9]*', number))
        service, warm, minimum = (self.api(kind) for kind in ('service', 'warm', 'minimum'))
        require(service['metadata']['name'] == c['service'])
        require(service['metadata']['namespace'] in {c['project'], number})
        require(service['metadata'].get('uid') and service['metadata'].get('resourceVersion'))
        require(service['spec']['traffic'] == [{'revisionName': c['warm_revision'], 'percent': 100}])
        require(warm['metadata']['name'] == c['warm_revision'] and warm['metadata'].get('uid'))
        require(warm['metadata']['labels']['commit-sha'] == c['source_commit'])
        require(str(warm['metadata']['annotations']['autoscaling.knative.dev/minScale']) == '1')
        require(warm['status']['imageDigest'].split('@')[-1] == c['image_digest'])
        require(minimum['name'] == 'projects/' + c['project'] + '/locations/' + c['region']
                + '/services/' + c['service'])
        require(minimum['uid'] == service['metadata']['uid'])
        require(minimum.get('scaling', {}).get('minInstanceCount', 0) == 0)
        require(minimum.get('template', {}).get('scaling', {}).get('minInstanceCount', 0) == 0)
        self.valid()
        return dict(service_snapshot=service, warm=warm, service_v2=minimum)

    def verify_bucket(self):
        value = self.api('bucket')
        require(value['name'] == self.bucket and value['storageClass'] == 'STANDARD')
        require(value['location'].lower() == self.config['region'])
        require(value['iamConfiguration']['uniformBucketLevelAccess']['enabled'] is True)
        require(value['iamConfiguration']['publicAccessPrevention'] == 'enforced')
        require(value['lifecycle'] == LIFECYCLE)
        require(not value.get('versioning', {}).get('enabled', False))
        require(not value.get('retentionPolicy') and not value.get('defaultEventBasedHold', False))
        require(int(value['softDeletePolicy']['retentionDurationSeconds']) == 0)
        bindings = self.api('iam')['bindings']
        require(isinstance(bindings, list))
        require(not any(member in {'allUsers', 'allAuthenticatedUsers'}
                        for binding in bindings for member in binding['members']))
        self.valid()

    def upload(self, name, data):
        self.valid()
        name = self.prefix + name
        self.api('upload', name=name, data=data)
        require(self.api('download', name=name) == data)
        self.valid()

    def execute(self):
        c = self.config
        initial = self.snapshot()
        self.valid()
        # Bucket creation is the distributed claim. A conflict/timeout is terminal;
        # never adopt an existing bucket, repeat POST, delete, or repair its policy.
        self.api('create_bucket', body={
            'name': self.bucket, 'location': c['region'], 'storageClass': 'STANDARD',
            'iamConfiguration': {'uniformBucketLevelAccess': {'enabled': True},
                                 'publicAccessPrevention': 'enforced'},
            'lifecycle': LIFECYCLE, 'versioning': {'enabled': False},
            'softDeletePolicy': {'retentionDurationSeconds': '0'}})
        self.verify_bucket()
        require(self.snapshot() == initial)
        self.upload('pre-warm-snapshot.json', encode({**initial, 'captured_at': self.now().isoformat()}))
        require(self.snapshot() == initial)
        card = {key: c[key] for key in ('authority', 'protected_environment', 'project', 'region',
                'service', 'source_commit', 'operator_commit', 'image_digest', 'request_id',
                'expires_at', 'restore_warm_on_failure')}
        card.update(scope='s3-only', replacement=c['service'] + '-rec-' + c['request_id'],
                    service_snapshot=initial['service_snapshot'], warm=initial['warm'])
        card_bytes = encode(card)
        self.verify_bucket()
        self.upload('recovery-card.json', card_bytes)
        require(self.snapshot() == initial)
        # Private receipt is not approval or a recover trigger. Public logs never
        # contain locators, checksums, snapshots, configuration or provider errors.
        self.upload('receipt.json', encode({
            'card_uri': 'gs://' + self.bucket + '/' + self.prefix + 'recovery-card.json',
            'card_sha256': hashlib.sha256(card_bytes).hexdigest(),
            'operator_commit': c['operator_commit'], 'expires_at': c['expires_at'],
            'snapshot_stable': True, 'pricing_status': 'NOT_PROVEN',
            'recovery_dispatched': False}))


def main():
    try:
        # Reuse the shared helper; register nested identity fields before work.
        mask_identifiers({'IBKR_RECOVERY_BOOTSTRAP_JSON': os.environ['IBKR_RECOVERY_BOOTSTRAP_JSON']})
        config = json.loads(os.environ['IBKR_RECOVERY_BOOTSTRAP_JSON'])
        Bootstrap(config, operator=os.environ['GITHUB_SHA'],
                  environment=os.environ['RECOVERY_PROTECTED_ENVIRONMENT']).execute()
    except Exception:
        print('Recovery evidence bootstrap failed; reconcile privately before any new request.', file=sys.stderr)
        return 1
    print('Recovery evidence bootstrap completed; recovery remains separately gated.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
