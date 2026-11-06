"""Protected four-target destination observation and inert initialization.

Never changes an existing service, traffic, inventory, Scheduler, IAM or secrets.
Provider responses and resource bindings remain in memory; reports use refs only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SOURCE_REVISION = '097c16d9a5dc703f682658b189d5df80bef6670b'
MAPPING_REF = 'authority.values.s07-ibkr-rename-draft-r1-mapping'
REFERENCES = [f'S07-C0{i}' for i in range(1, 5)]
RUN_API = 'https://run.googleapis.com/v2'
PROJECT_API = 'https://cloudresourcemanager.googleapis.com/v3/projects/'
NAME = re.compile(r'[a-z][a-z0-9-]{0,47}[a-z0-9]')
DISABLED_SERVER = (
    'from http.server import BaseHTTPRequestHandler, HTTPServer\n'
    'class Disabled(BaseHTTPRequestHandler):\n'
    ' def do_GET(self):\n'
    '  self.send_response(503); self.end_headers(); self.wfile.write(b"disabled")\n'
    ' do_POST = do_GET\n'
    ' def log_message(self, *args): pass\n'
    'HTTPServer(("0.0.0.0", 8080), Disabled).serve_forever()'
)


class Blocked(Exception):
    """No provider or private input detail may be included in this exception."""


def require(condition):
    if not condition:
        raise Blocked('Migration contract not satisfied')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def admit(mode, env):
    require(mode in {'observe', 'validate', 'initialize'})
    require(env.get('GITHUB_EVENT_NAME') == 'workflow_dispatch')
    require(env.get('GITHUB_REF') == 'refs/heads/main' and env.get('GITHUB_RUN_ATTEMPT') == '1')
    require(bool(env.get('MIGRATION_ENVIRONMENT')))
    p = json.loads(env['MIGRATION_PLAN_JSON'])
    require(set(p) == {'mapping_ref', 'source_revision', 'project', 'region',
                       'disabled_service_account', 'targets'})
    require(p['source_revision'] == env.get('INPUT_SOURCE_REVISION') == SOURCE_REVISION)
    require(p['mapping_ref'] == env.get('INPUT_MAPPING_REF') == MAPPING_REF)
    require(hashlib.sha256(canonical(p).encode()).hexdigest() == env.get('INPUT_PLAN_SHA256'))
    require(isinstance(p['project'], str) and re.fullmatch(r'[a-z][a-z0-9-]{4,61}[a-z0-9]', p['project']))
    require(p['project'] == env.get('GCP_PROJECT_ID'))
    require(isinstance(p['region'], str) and re.fullmatch(r'[a-z]+-[a-z]+[0-9]', p['region']))
    require(re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]@' + re.escape(p['project']) +
                         r'\.iam\.gserviceaccount\.com', p['disabled_service_account']))
    require(isinstance(p['targets'], list) and len(p['targets']) == 4)
    require([t['reference'] for t in p['targets']] == REFERENCES)
    for t in p['targets']:
        require(set(t) == {'reference', 'old', 'new', 'image', 'expected_uid'})
        require(all(isinstance(t[k], str) for k in t))
        require(NAME.fullmatch(t['old']) and NAME.fullmatch(t['new']))
        require(len(t['new']) + 13 <= 46)
        require(re.fullmatch(re.escape(p['region'] + '-docker.pkg.dev/' + p['project']) +
                             r'/[a-z0-9-]+/[a-z0-9/-]+@sha256:[0-9a-f]{64}', t['image']))
        require(not t['expected_uid'] or re.fullmatch(r'[a-zA-Z0-9-]{1,128}', t['expected_uid']))
    require(len({t['old'] for t in p['targets']}) == 4)
    require(len({t['new'] for t in p['targets']}) == 4)
    require(not {t['old'] for t in p['targets']} & {t['new'] for t in p['targets']})
    if mode != 'observe':
        authorize(env)
    return p


def authorize(env):
    """A future card, bound to this run and tree, must be installed in Environment."""
    card = json.loads(env.get('MIGRATION_AUTHORIZATION_JSON', '{}'))
    for key, expected in {
        'action': 'initialize-disabled', 'run_id': env.get('GITHUB_RUN_ID'),
        'workflow_sha': env.get('GITHUB_SHA'), 'plan_sha256': env.get('INPUT_PLAN_SHA256'),
        'source_revision': SOURCE_REVISION, 'mapping_ref': MAPPING_REF,
    }.items():
        require(bool(expected) and card.get(key) == expected)
    require(re.fullmatch(r'[0-9a-f]{40}', card['workflow_sha']))
    require(re.fullmatch(r'[0-9a-f]{64}', card['plan_sha256']))
    require(isinstance(card.get('authority_ref'), str) and bool(card['authority_ref'].strip()))
    # The draft card and retained historical deployment scope are not a new run card.
    require(card['authority_ref'] not in {'t1320s7739', 't1320s7726'})
    expiry = datetime.fromisoformat(card['expires_at'])
    require(expiry.tzinfo is not None)
    seconds = (expiry - datetime.now(timezone.utc)).total_seconds()
    require(0 < seconds <= 3600)


def service_body(p, t):
    return {
        'name': f"projects/{p['project']}/locations/{p['region']}/services/{t['new']}",
        'description': 'S07 inert destination ' + t['reference'],
        'labels': {'s07-reference': t['reference'].lower(), 's07-source': SOURCE_REVISION},
        'ingress': 'INGRESS_TRAFFIC_INTERNAL_ONLY', 'invokerIamDisabled': False,
        'template': {
            'serviceAccount': p['disabled_service_account'],
            'scaling': {'minInstanceCount': 0, 'maxInstanceCount': 1},
            'timeout': '30s', 'maxInstanceRequestConcurrency': 1,
            'containers': [{
                'image': t['image'], 'command': ['python', '-c'],
                'args': [DISABLED_SERVER], 'env': [],
                'ports': [{'name': 'http1', 'containerPort': 8080}],
                'resources': {'limits': {'cpu': '1', 'memory': '128Mi'},
                              'cpuIdle': True, 'startupCpuBoost': False},
            }],
        },
        'traffic': [{'type': 'TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST', 'percent': 100}],
    }


def verify_service(p, t, service, uid):
    """Require exact inert configuration and explicit ready revision/traffic."""
    require(bool(uid) and service.get('uid') == uid)
    body = service_body(p, t)
    for key in ('name', 'description', 'labels', 'ingress'):
        require(service.get(key) == body[key])
    # Proto3 JSON omits default false/zero fields; these are defined API values.
    require(service.get('invokerIamDisabled', False) is False)
    require(service.get('scaling', {}).get('minInstanceCount', 0) == 0)
    template = service.get('template', {})
    expected = body['template']
    for key in ('serviceAccount', 'timeout', 'maxInstanceRequestConcurrency'):
        require(template.get(key) == expected[key])
    scaling = template.get('scaling', {})
    require(scaling.get('minInstanceCount', 0) == 0 and scaling.get('maxInstanceCount') == 1)
    containers = template.get('containers')
    require(isinstance(containers, list) and len(containers) == 1)
    for key, value in expected['containers'][0].items():
        if key == 'resources':
            resources = containers[0].get('resources', {})
            require(resources.get('limits') == value['limits'])
            require(resources.get('cpuIdle') is True)
            require(resources.get('startupCpuBoost', False) is False)
        else:
            require(containers[0].get(key, [] if key == 'env' else None) == value)
    # Never accept volumes, sidecars, secret env or additional container inputs.
    require(not template.get('volumes'))
    require(set(containers[0]) <= set(expected['containers'][0]) | {'name', 'startupProbe', 'livenessProbe'})
    require(service.get('reconciling', False) is False)
    require(str(service.get('generation')) == str(service.get('observedGeneration')) != 'None')
    require(service.get('terminalCondition', {}).get('state') == 'CONDITION_SUCCEEDED')
    revision = service.get('latestCreatedRevision')
    require(isinstance(revision, str) and revision.startswith(body['name'] + '/revisions/'))
    require(service.get('latestReadyRevision') == revision)
    traffic = service.get('trafficStatuses')
    require(isinstance(traffic, list) and len(traffic) == 1)
    require(traffic[0].get('revision') == revision and traffic[0].get('percent') == 100)
    require(not traffic[0].get('tag'))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_request(request, timeout):
    return urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout)


class Cloud:
    """Bounded REST client. No diagnostic body, token or raw exception escapes."""
    def __init__(self, env, mode):
        self.env, self.mode = env, mode

    def __call__(self, method, url, body=None):
        require(url.startswith(RUN_API + '/projects/') or
                method == 'GET' and url.startswith(PROJECT_API))
        require(method in {'GET', 'POST'})
        try:
            token = subprocess.run(['gcloud', 'auth', 'print-access-token'], capture_output=True,
                                   text=True, check=True, timeout=30).stdout.strip()
            require(bool(token))
            if method == 'POST':
                authorize(self.env)  # Token acquisition can outlive the approval.
            request = urllib.request.Request(
                url, data=canonical(body).encode() if body is not None else None,
                method=method, headers={'Authorization': 'Bearer ' + token,
                                        'Content-Type': 'application/json'})
            with open_request(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if method == 'GET' and '/services/' in url and exc.code == 404:
                return None
            raise Blocked('Cloud request failed; no automatic retry') from None
        except Blocked:
            raise
        except Exception:
            raise Blocked('Cloud transport failed; outcome may be unknown') from None


def wait_operation(cloud, operation, parents, *, validation=False):
    """Confirm completion; validate-only success need not contain a service."""
    require(isinstance(operation, dict) and 'error' not in operation)
    if validation and operation.get('done') is True and 'name' not in operation:
        return None
    name = operation.get('name', '')
    require(isinstance(name, str))
    if name.startswith(RUN_API + '/'):
        name = name[len(RUN_API) + 1:]
    parents = (parents,) if isinstance(parents, str) else parents
    require(any(name.startswith(parent + '/operations/') for parent in parents) and
            re.fullmatch(r'[A-Za-z0-9-]+', name.split('/')[-1]))
    result = operation
    for attempt in range(31):
        require(isinstance(result, dict) and 'error' not in result)
        require(isinstance(result.get('done', False), bool))
        if result.get('done') is True:
            if validation:
                return None
            require(isinstance(result.get('response'), dict))
            return result['response']
        if attempt == 30:
            break
        if attempt:
            time.sleep(2)
        result = cloud('GET', RUN_API + '/' + name)
    raise Blocked('Operation pending; inspect before any separately authorized continuation')


def run(mode, env, cloud):
    p = admit(mode, env)
    project = cloud('GET', PROJECT_API + p['project'])
    require(isinstance(project, dict) and project.get('projectId') == p['project'] and project.get('state') == 'ACTIVE')
    parent = f"projects/{p['project']}/locations/{p['region']}"
    project_name = project.get('name', '')
    require(isinstance(project_name, str) and re.fullmatch(r'projects/[0-9]+', project_name))
    operation_parents = (parent, project_name + '/locations/' + p['region'])
    rows = []
    # Complete all four read-only checks before the first validation or creation.
    for t in p['targets']:
        url = RUN_API + '/' + service_body(p, t)['name']
        current = cloud('GET', url)
        if current is not None:
            verify_service(p, t, current, t['expected_uid'])
        else:
            require(not t['expected_uid'])  # A formerly known service disappearing is drift.
        rows.append({'reference': t['reference'], 'state': 'ABSENT' if current is None else 'OWNED_DISABLED',
                     'revision_verified': current is not None, 'traffic_verified': current is not None})
    if mode == 'observe':
        return {'mode': mode, 'targets': rows}
    for t, row in zip(p['targets'], rows):
        if row['state'] == 'ABSENT':
            authorize(env)
            operation = cloud('POST', RUN_API + '/' + parent + '/services?serviceId=' +
                              urllib.parse.quote(t['new'], safe='') + '&validateOnly=true', service_body(p, t))
            wait_operation(cloud, operation, operation_parents, validation=True)
    if mode == 'validate':
        return {'mode': mode, 'targets': rows}
    for t, row in zip(p['targets'], rows):
        url = RUN_API + '/' + service_body(p, t)['name']
        current = cloud('GET', url)  # Never use the earlier preflight snapshot to create.
        if row['state'] == 'OWNED_DISABLED':
            require(current is not None)
            verify_service(p, t, current, t['expected_uid'])
            continue
        require(current is None)
        authorize(env)
        operation = cloud('POST', RUN_API + '/' + parent + '/services?serviceId=' +
                          urllib.parse.quote(t['new'], safe=''), service_body(p, t))
        created = wait_operation(cloud, operation, operation_parents)
        verify_service(p, t, created, created.get('uid'))
        readback = cloud('GET', url)
        require(isinstance(readback, dict))
        verify_service(p, t, readback, created['uid'])
        row.update(state='INITIALIZED_DISABLED', revision_verified=True, traffic_verified=True)
    return {'mode': mode, 'targets': rows}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['observe', 'validate', 'initialize'], required=True)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    try:
        result = run(args.mode, os.environ, Cloud(os.environ, args.mode))
    except Exception:
        # Fixed diagnostic includes no environment, provider response or resource identity.
        result = {'mode': args.mode, 'state': 'BLOCKED_OR_OUTCOME_UNKNOWN',
                  'automatic_retry': False, 'revision_and_traffic': 'NOT_PROVEN'}
        status = 1
    else:
        status = 0
    safe = canonical(result)
    if args.report:
        args.report.write_text(safe + '\n', encoding='utf-8')
    print(safe, file=sys.stderr if status else sys.stdout)
    return status


if __name__ == '__main__':
    raise SystemExit(main())
