"""GET-only warm configuration and receipt reader; export ciphertext only."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import quote
from urllib.request import Request

from scripts.mask_sync_identifiers import mask_identifiers
from scripts.recovery_evidence_bootstrap import encode, require, urlopen


class Reader:
    def __init__(self, config, *, environment, get=None):
        self.config = config
        for key in ('project', 'region', 'service'):
            require(isinstance(config[key], str)
                    and re.fullmatch(r'[a-z][a-z0-9-]{0,62}', config[key]))
        require(environment and config['protected_environment'] == environment)
        require(re.fullmatch(r'[a-f0-9]{40}', config['operator_commit']))
        self.get = get or self.request

    def request(self, url):
        # Fixed, internally constructed endpoints only. No write method, body,
        # redirect, automatic retry or provider diagnostics can escape here.
        token = subprocess.run(['gcloud', 'auth', 'print-access-token'],
                               capture_output=True, check=True, text=True,
                               timeout=15).stdout.strip()
        require(token)
        request = Request(url, method='GET', headers={'Authorization': 'Bearer ' + token})
        with urlopen(request, timeout=30) as response:
            data = response.read(4 * 1024 * 1024 + 1)
        require(len(data) <= 4 * 1024 * 1024)
        return data

    def snapshot(self):
        c = self.config
        base = ('https://' + c['region'] + '-run.googleapis.com/apis/serving.knative.dev/v1/'
                'namespaces/' + c['project'])
        project = json.loads(self.get('https://cloudresourcemanager.googleapis.com/v1/projects/'
                                      + c['project']))
        require(project['projectId'] == c['project'])
        number = project['projectNumber']
        require(isinstance(number, str) and re.fullmatch(r'[1-9][0-9]*', number))
        service = json.loads(self.get(base + '/services/' + c['service']))
        require(service['metadata']['name'] == c['service'])
        require(service['metadata']['namespace'] in {c['project'], number})
        require(service['metadata'].get('uid') and service['metadata'].get('resourceVersion'))
        traffic = service['spec']['traffic']
        require(isinstance(traffic, list) and len(traffic) == 1)
        revision = traffic[0]['revisionName']
        require(isinstance(revision, str) and re.fullmatch(r'[a-z][a-z0-9-]{0,62}', revision))
        require(revision.startswith(c['service'] + '-'))
        require(traffic == [{'revisionName': revision, 'percent': 100}])
        warm = json.loads(self.get(base + '/revisions/' + revision))
        require(warm['metadata']['name'] == revision and warm['metadata'].get('uid'))
        require(warm['metadata']['namespace'] in {c['project'], number})
        source = warm['metadata']['labels']['commit-sha']
        digest = warm['status']['imageDigest'].split('@')[-1]
        require(re.fullmatch(r'[a-f0-9]{40}', source))
        require(re.fullmatch(r'sha256:[a-f0-9]{64}', digest))
        require(str(warm['metadata']['annotations']['autoscaling.knative.dev/minScale']) == '1')
        minimum = json.loads(self.get('https://run.googleapis.com/v2/projects/' + c['project']
                                     + '/locations/' + c['region'] + '/services/' + c['service']))
        require(minimum['name'] == 'projects/' + c['project'] + '/locations/' + c['region']
                + '/services/' + c['service'])
        require(minimum['uid'] == service['metadata']['uid'])
        require(minimum.get('scaling', {}).get('minInstanceCount', 0) == 0)
        require(minimum.get('template', {}).get('scaling', {}).get('minInstanceCount', 0) == 0)
        return dict(project=c['project'], region=c['region'], service=c['service'],
                    warm_revision=revision, source_commit=source, image_digest=digest,
                    service_snapshot=service, warm=warm, service_v2=minimum)

    def receipt(self, snapshot):
        c = self.config
        require(re.fullmatch(r'[a-z0-9]{8,32}', c['bucket_suffix']))
        require(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,30}', c['request_id']))
        bucket = 'ibkr-recovery-evidence-' + c['bucket_suffix']
        prefix = 's3-recovery/' + c['request_id'] + '/'
        def read(name):
            return self.get('https://storage.googleapis.com/storage/v1/b/' + bucket
                            + '/o/' + quote(prefix + name, safe='') + '?alt=media')
        receipt_bytes, card_bytes, before_bytes = (read(name) for name in
            ('receipt.json', 'recovery-card.json', 'pre-warm-snapshot.json'))
        receipt, card, before = map(json.loads, (receipt_bytes, card_bytes, before_bytes))
        require(receipt['card_uri'] == 'gs://' + bucket + '/' + prefix + 'recovery-card.json')
        require(receipt['card_sha256'] == hashlib.sha256(card_bytes).hexdigest())
        require(receipt['operator_commit'] == card['operator_commit'] == c['operator_commit'])
        require(receipt['snapshot_stable'] is True and receipt['recovery_dispatched'] is False)
        require(receipt['pricing_status'] == 'NOT_PROVEN')
        require(receipt['expires_at'] == card['expires_at'])
        require(card['scope'] == 's3-only' and card['restore_warm_on_failure'] is True)
        require(card['protected_environment'] == c['protected_environment'] and card['authority'])
        require(card['request_id'] == c['request_id'])
        require(card['replacement'] == c['service'] + '-rec-' + c['request_id'])
        for key in ('project', 'region', 'service', 'source_commit', 'image_digest',
                    'service_snapshot', 'warm'):
            require(card[key] == snapshot[key])
        for key in ('service_snapshot', 'warm', 'service_v2'):
            require(before[key] == snapshot[key])
        require(read('receipt.json') == receipt_bytes)
        require(read('recovery-card.json') == card_bytes)
        require(read('pre-warm-snapshot.json') == before_bytes)
        # Expiry is retained as evidence, never extended or interpreted as approval.
        return dict(receipt=receipt, card=card, pre_warm_snapshot=before)

    def collect(self, mode):
        require(mode in {'snapshot', 'receipt'})
        initial = self.snapshot()
        private = self.receipt(initial) if mode == 'receipt' else None
        require(self.snapshot() == initial)
        return dict(schema_version=1, captured_at=datetime.now(timezone.utc).isoformat(),
                    operator_commit=self.config['operator_commit'], snapshot=initial,
                    bootstrap_receipt=private,
                    receipt_status='READ_VERIFIED' if private else 'NOT_REQUESTED',
                    authority_status='NOT_EVALUATED', recovery_dispatched=False)


def encrypt(payload, certificate, root):
    # No plaintext output file. Authenticated CMS encryption uses a coordinator
    # certificate; the corresponding private key must never enter Actions.
    cert = root / 'coordinator.pem'
    output = root / 'warm-readback.cms'
    with cert.open('x', encoding='ascii') as file:
        file.write(certificate)
    try:
        result = subprocess.run(['openssl', 'cms', '-encrypt', '-aes-256-gcm',
                                 '-binary', '-outform', 'DER', str(cert)],
                                input=encode(payload), capture_output=True, check=True, timeout=30)
        require(result.stdout)
        with output.open('xb') as file:
            file.write(result.stdout)
    finally:
        cert.unlink()


def main():
    os.umask(0o077)
    try:
        raw = os.environ['IBKR_RECOVERY_READER_JSON']
        # Shared helper is the only masking implementation; no payload is printed.
        mask_identifiers({'IBKR_RECOVERY_READER_JSON': raw})
        config = json.loads(raw)
        certificate = os.environ['IBKR_RECOVERY_READER_CERT_PEM']
        require(certificate and 'PRIVATE KEY' not in certificate)
        root = Path(os.environ['RUNNER_TEMP']) / 'warm-reader'
        # Exclusive directory creation refuses a restarted/stale export directory.
        root.mkdir(mode=0o700)
        # Validate certificate/encryption before acquiring cloud credentials.
        encrypt({'probe': True}, certificate, root)
        (root / 'warm-readback.cms').unlink()
        result = Reader(config, environment=os.environ['RECOVERY_PROTECTED_ENVIRONMENT']).collect(
            os.environ['READER_MODE'])
        encrypt(result, certificate, root)
    except Exception:
        print('Private warm readback failed; no recovery is authorized.', file=sys.stderr)
        return 1
    print('Encrypted readback ready; recovery remains separately gated.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
