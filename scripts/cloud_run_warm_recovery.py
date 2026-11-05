"""One-shot retained-image recovery; an owner-approved card is required.

No build, environment sync, health HTTP invocation, or mutation outside one
service. Private cards and attempt files must remain outside the repository.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from scripts.cloud_run_candidate_warmup import MINIMUM, active, revision_metadata
from scripts.runtime_target_continuity import StartupHealthPending, verify_traffic_revision


OUTPUT_ANNOTATIONS = {'run.googleapis.com/operation-id', 'run.googleapis.com/urls',
                      'run.googleapis.com/ingress-status', 'serving.knative.dev/creator',
                      'serving.knative.dev/lastModifier'}


def service_meta(value: dict, key: str):
    result = value['metadata'].get(key)
    if key == 'annotations':
        return {name: item for name, item in (result or {}).items() if name not in OUTPUT_ANNOTATIONS}
    return result


class RecoveryError(RuntimeError):
    """A fixed diagnostic with no private provider details."""


def require(test: bool) -> None:
    if not test:
        raise RecoveryError('recovery binding, continuity or readback mismatch')


def fingerprint(value: dict) -> dict:
    return {'metadata': revision_metadata(value), 'spec': value['spec'],
            'imageDigest': value.get('status', {}).get('imageDigest')}


def healthy(value: dict, commit: str) -> None:
    # The common verifier prints provider conditions; keep those private here.
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        verify_traffic_revision(value, expected_revision=value['metadata']['name'],
                                expected_commit=commit, require_startup_health=True)
    meta, status = value['metadata'], value['status']
    require(bool(meta.get('uid')) and type(meta.get('generation')) is int
            and meta['generation'] > 0 and status.get('observedGeneration') == meta['generation'])
    for kind in ('Ready', 'Active', 'ContainerReady', 'ContainerHealthy'):
        rows = [row for row in status['conditions'] if row.get('type') == kind]
        require(len(rows) == 1 and rows[0].get('status') == 'True')


class API:
    """Existing client credentials stay in memory; no retries or diagnostic body."""
    def __init__(self, project: str, region: str, service: str):
        self.base = ('https://' + region + '-run.googleapis.com/apis/serving.knative.dev/v1/'
                     + 'namespaces/' + project)
        self.service_url = self.base + '/services/' + service
        self.v2_url = ('https://run.googleapis.com/v2/projects/' + project
                       + '/locations/' + region + '/services/' + service)

    def __call__(self, kind: str, name: str = '', body: dict | None = None,
                 timeout: float = 30) -> dict:
        try:
            token = subprocess.run(['gcloud', 'auth', 'print-access-token'],
                capture_output=True, text=True, check=True, timeout=min(10, timeout / 2)).stdout.strip()
            require(bool(token))
            url = self.v2_url if kind == 'minimum' else (
                self.base + '/revisions/' + name if kind in {'revision', 'absent'} else self.service_url)
            request = Request(url, data=json.dumps(body).encode() if body is not None else None,
                method='PUT' if body is not None else 'GET',
                headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
            # Token acquisition plus HTTP fits inside the caller's remaining time.
            with urlopen(request, timeout=timeout / 2) as response:
                result = json.load(response)
            require(isinstance(result, dict))
            return {'absent': False} if kind == 'absent' else result
        except HTTPError as error:
            if kind == 'absent' and error.code == 404:
                return {'absent': True}
            raise RecoveryError('recovery cloud operation failed or outcome unknown') from None
        except Exception:
            raise RecoveryError('recovery cloud operation failed or outcome unknown') from None


class Recovery:
    def __init__(self, card: dict, attempt: Path, *, api=None,
                 clock=time.monotonic, sleep=time.sleep, now=lambda: datetime.now(timezone.utc)):
        self.card, self.attempt = card, attempt
        self.clock, self.sleep, self.now = clock, sleep, now
        self.project, self.region, self.service = (card[key] for key in ('project', 'region', 'service'))
        for value in (self.project, self.region, self.service, card['replacement']):
            require(isinstance(value, str) and re.fullmatch(r'[a-z0-9][a-z0-9-]*', value) is not None)
        require(card['scope'] == 's3-only' and card['restore_warm_on_failure'] is True)
        require(re.fullmatch(r'[0-9a-f]{40}', card['source_commit']) is not None)
        # Missing digest stops. It never silently builds or substitutes an image.
        require(re.fullmatch(r'sha256:[0-9a-f]{64}', card['image_digest']) is not None)
        require(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,30}', card['request_id']) is not None)
        require(card['replacement'] == self.service + '-rec-' + card['request_id']
                and len(card['replacement']) <= 63)
        self.expires = datetime.fromisoformat(card['expires_at'])
        require(self.expires.tzinfo is not None and self.expires > self.now())
        require(card['protected_environment'] and card['authority'])
        self.api = api or API(self.project, self.region, self.service)
        self.warm = card['warm']['metadata']['name']
        require(self.warm.startswith(self.service + '-') and self.warm != card['replacement'])
        self.expected = None
        self.replacement = None
        self.activated = False
        self.minima = []

    def valid(self):
        require(self.now() < self.expires)

    def record(self, status: str):
        # Fixed states only, never raw identities/configuration/conditions.
        self.attempt.write_text(json.dumps({'status': status, 'minimum_readbacks': self.minima}) + '\n')

    def failure_record(self, status: str):
        # Keep the existing exclusive attempt claim even when its volume fails.
        # Persistence must never suppress the independently authorized restore.
        with contextlib.suppress(BaseException):
            self.record(status)

    def minimum(self):
        value = self.api('minimum')
        require(value.get('name') == 'projects/' + self.project + '/locations/' + self.region
                + '/services/' + self.service)
        require(value.get('uid') == self.card['service_snapshot']['metadata']['uid'])
        require(value.get('scaling', {}).get('minInstanceCount', 0) == 0
                and value.get('template', {}).get('scaling', {}).get('minInstanceCount', 0) == 0)
        warm = self.api('revision', self.warm)
        require(fingerprint(warm) == fingerprint(self.card['warm']))
        rows = [row.get('status') for row in warm.get('status', {}).get('conditions', [])
                if row.get('type') == 'MinInstancesProvisioned']
        self.minima.append({'service_minimum': 0, 'template_minimum': 0,
                            'warm_revision_minimum': 1,
                            'warm_minimum_one_provisioned': (rows[0] if len(rows) == 1
                                and rows[0] in {'True', 'False', 'Unknown'} else 'Unknown'),
                            'billing_total': 'NOT_QUERIED'})

    def stable(self, distribution: list, timeout=30) -> dict:
        value = self.api('service', timeout=timeout)
        expected = self.expected
        require(value['metadata']['uid'] == self.card['service_snapshot']['metadata']['uid'])
        require(value['spec'] == expected['spec'] and active(value) == sorted(distribution))
        for key in ('name', 'namespace', 'generation', 'labels', 'annotations'):
            require(service_meta(value, key) == service_meta(expected, key))
        require(value['metadata'].get('resourceVersion'))
        require(value.get('status', {}).get('observedGeneration') == value['metadata']['generation'])
        return value

    def check_warm(self, *, require_health=True):
        value = self.api('revision', self.warm)
        require(fingerprint(value) == fingerprint(self.card['warm']))
        require(str(value['metadata'].get('annotations', {}).get(MINIMUM, '0')) == '1')
        require(value['status']['imageDigest'].split('@')[-1] == self.card['image_digest'])
        if require_health:
            healthy(value, self.card['source_commit'])

    def check_replacement(self, timeout=30):
        value = self.api('revision', self.card['replacement'], timeout=timeout)
        require(value['metadata'].get('name') == self.card['replacement'])
        require(str(value['metadata'].get('annotations', {}).get(MINIMUM, '0')) == '0')
        template = self.expected['spec']['template']
        require(value['spec'] == template['spec'])
        for key in ('labels', 'annotations'):
            for name, item in template['metadata'].get(key, {}).items():
                require(value['metadata'].get(key, {}).get(name) == item)
        require(value['status']['imageDigest'].split('@')[-1] == self.card['image_digest'])
        require(value['metadata'].get('uid') and value['metadata'].get('generation'))
        if self.replacement is not None:
            require(fingerprint(value) == fingerprint(self.replacement))
        return value

    def put(self, current: dict, spec: dict):
        # Cloud Run v1 resourceVersion is an optimistic-concurrency precondition.
        # Keep every setting; change only the explicitly constructed template/traffic.
        meta = {key: current['metadata'][key] for key in
                ('name', 'namespace', 'resourceVersion', 'labels', 'annotations')
                if key in current['metadata']}
        require(meta.get('resourceVersion'))
        meta['annotations'] = service_meta(current, 'annotations')
        self.api('service', body={'apiVersion': 'serving.knative.dev/v1', 'kind': 'Service',
                                 'metadata': meta, 'spec': spec})

    def settle(self, spec: dict, distribution: list):
        deadline = self.clock() + 60
        while True:
            remaining = deadline - self.clock()
            require(remaining > 0)
            value = self.api('service', timeout=min(30, remaining))
            require(self.clock() < deadline)
            require(value['metadata']['uid'] == self.card['service_snapshot']['metadata']['uid'])
            require(value['spec'] == spec)
            require(value['metadata']['generation'] == self.expected['metadata']['generation'] + 1)
            for key in ('name', 'namespace', 'labels', 'annotations'):
                require(service_meta(value, key) == service_meta(self.expected, key))
            if (active(value) == sorted(distribution)
                    and value['status'].get('observedGeneration') == value['metadata']['generation']):
                self.expected = value
                return
            require(self.clock() < deadline)
            self.sleep(1)

    def shift(self, distribution: list, *, deadline=None):
        self.valid()
        previous = active(self.expected)
        current = self.stable(previous)
        self.check_warm()
        if deadline is not None:
            require(self.clock() < deadline)
            healthy(self.check_replacement(timeout=min(30, deadline - self.clock())),
                    self.card['source_commit'])
            require(self.clock() < deadline)
        self.valid()
        spec = copy.deepcopy(current['spec'])
        spec['traffic'] = [{'revisionName': name, 'percent': percent} for name, percent in distribution]
        self.put(current, spec)
        self.settle(spec, distribution)

    def rollback(self):
        # Expiry stops forward work, but not the expressly approved failure restore.
        # A failed/unknown write is never repeated; fresh readback chooses the one
        # distinct compensation. Never overwrite another actor's configuration.
        self.check_warm(require_health=False)
        current = self.api('service')
        require(current['metadata']['uid'] == self.card['service_snapshot']['metadata']['uid'])
        require(current['spec']['template'] == self.expected['spec']['template'])
        require(current['metadata']['generation'] in {self.expected['metadata']['generation'],
                self.expected['metadata']['generation'] + 1})
        for key in ('name', 'namespace', 'labels', 'annotations'):
            require(service_meta(current, key) == service_meta(self.expected, key))
        left, right = copy.deepcopy(current['spec']), copy.deepcopy(self.expected['spec'])
        left.pop('traffic', None)
        right.pop('traffic', None)
        require(left == right)
        allowed = [[(self.warm, 100)], sorted([(self.warm, 99), (self.card['replacement'], 1)]),
                   [(self.card['replacement'], 100)]]
        require(active(current) in allowed)
        desired = [{'revisionName': self.warm, 'percent': 100}]
        if active(current) == [(self.warm, 100)] and current['spec'].get('traffic') == desired:
            self.expected = current
            return
        spec = copy.deepcopy(current['spec'])
        spec['traffic'] = desired
        self.expected = current
        self.put(current, spec)
        self.settle(spec, [(self.warm, 100)])

    def execute(self):
        # Exclusive creation persists across failure/restart, including reruns on
        # the same host. The owner must never reuse the attempt/request identity.
        with self.attempt.open('x'):
            os.chmod(self.attempt, 0o600)
        self.record('STARTED')
        try:
            self.valid()
            before = self.api('service')
            require(before == self.card['service_snapshot'])
            require(before['metadata'].get('resourceVersion') and before['metadata'].get('uid'))
            require(active(before) == [(self.warm, 100)])
            require(before['spec']['traffic'] == [{'revisionName': self.warm, 'percent': 100}])
            require(str(before['spec']['template']['metadata'].get('annotations', {}).get(MINIMUM, '0')) == '0')
            self.expected = before
            self.minimum()
            self.check_warm()
            self.sleep(5)
            self.check_warm()
            current = self.stable([(self.warm, 100)])
            self.valid()
            spec = copy.deepcopy(current['spec'])
            require(spec['template']['spec'] == self.card['warm']['spec'])
            template = spec['template']
            template['metadata']['name'] = self.card['replacement']
            template['metadata'].setdefault('annotations', {})[MINIMUM] = '0'
            containers = template['spec']['containers']
            require(len(containers) == 1)
            image = containers[0]['image'].split('@')[0]
            # Reject tags; only the existing repository plus retained digest.
            require(':' not in image.rsplit('/', 1)[-1] and '/' in image)
            containers[0]['image'] = image + '@' + self.card['image_digest']
            require(template['metadata'].get('labels', {}).get('commit-sha') == self.card['source_commit'])
            require(self.api('absent', self.card['replacement']) == {'absent': True})
            self.valid()
            # Persist intent before PUT; no restart can replay an ambiguous write.
            self.record('CREATE_INTENT')
            self.put(current, spec)
            self.settle(spec, [(self.warm, 100)])
            require(self.expected['status'].get('latestCreatedRevisionName') == self.card['replacement'])
            self.replacement = self.check_replacement()
            self.record('ACTIVATE_INTENT')
            self.activated = True
            self.shift([(self.warm, 99), (self.card['replacement'], 1)])
            deadline = self.clock() + 300
            count = 0
            while True:
                self.valid()
                require(self.clock() < deadline)
                self.stable([(self.warm, 99), (self.card['replacement'], 1)],
                            timeout=min(30, deadline - self.clock()))
                require(self.clock() < deadline)
                value = self.check_replacement(timeout=min(30, deadline - self.clock()))
                try:
                    healthy(value, self.card['source_commit'])
                except StartupHealthPending:
                    count = 0
                else:
                    count += 1
                require(self.clock() < deadline)
                if count == 2:
                    break
                self.sleep(min(5, deadline - self.clock()))
            self.record('PROMOTE_INTENT')
            self.shift([(self.card['replacement'], 100)], deadline=deadline)
            healthy(self.check_replacement(), self.card['source_commit'])
            self.minimum()
            self.record('SUCCEEDED')
        except BaseException:
            self.failure_record('FAILED')
            if self.activated:
                try:
                    self.rollback()
                    self.minimum()
                    outcome = 'FAILED_WARM_RESTORED'
                except BaseException:
                    outcome = 'FAILED_RESTORE_UNCONFIRMED'
                self.failure_record(outcome)
            if not self.activated and self.expected is not None:
                try:
                    self.minimum()
                    self.failure_record('FAILED')
                except Exception:
                    self.failure_record('FAILED_MINIMUM_READBACK_UNAVAILABLE')
            raise RecoveryError('recovery stopped; inspect private attempt state; do not replay') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--card', type=Path, required=True)
    parser.add_argument('--attempt', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    def stop(*_):
        raise RecoveryError('recovery interrupted')
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        require(args.card.stat().st_mode & 0o077 == 0)
        card = json.loads(args.card.read_text())
        head = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True,
                              text=True, check=True, timeout=10).stdout.strip()
        require(head == card['operator_commit'])
        dirty = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=no'],
                               capture_output=True, text=True, check=True, timeout=10).stdout
        require(not dirty)
        require(os.environ.get('RECOVERY_PROTECTED_ENVIRONMENT') == card['protected_environment'])
        Recovery(card, args.attempt).execute()
    except Exception:
        parser.exit(1, 'Recovery stopped. Read the private attempt state; never replay.\n')
    print('Recovery succeeded; minimum-zero readback verified.')


if __name__ == '__main__':
    main()
