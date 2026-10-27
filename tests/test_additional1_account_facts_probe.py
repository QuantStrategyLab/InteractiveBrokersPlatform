"""Offline admission and one-attempt transport regressions; no broker/cloud calls."""
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from scripts import invoke_additional1_account_facts_probe as probe

PRIVATE = 'PRIVATE-DO-NOT-OUTPUT'


@dataclass
class Group:
    execution_backend: str = 'gateway'
    ib_client_id: int = 7
    account_ids: tuple = ('U00000001',)


@pytest.fixture
def case(monkeypatch):
    monkeypatch.setenv('CLOUD_RUN_REGION', 'us-central1')
    expected = dict(expected_project_id='synthetic-project', expected_service_name='synthetic-service',
        expected_runtime_revision='synthetic-service-old', expected_account_scope='synthetic-scope',
        expected_deployment_selector='synthetic-deploy', expected_account_selector=('U00000001',))
    revision_name = 'synthetic-service-abc'
    target = dict(platform_id='ibkr', service_name=expected['expected_service_name'],
        account_scope=expected['expected_account_scope'], deployment_selector=expected['expected_deployment_selector'],
        account_selector=list(expected['expected_account_selector']))
    container = dict(image='us-docker.pkg.dev/synthetic-project/synthetic/image@sha256:' + 'f' * 64,
                     env=[dict(name='RUNTIME_TARGET_JSON', value=json.dumps(target))])
    template = dict(metadata=dict(annotations={'autoscaling.knative.dev/maxScale': '1'}),
                    spec=dict(containerConcurrency=1, containers=[container]))
    revision = deepcopy(template)
    revision['metadata'].update(name=revision_name, labels={'commit-sha': probe.REVIEWED_SOURCE})
    revision['status'] = dict(conditions=[dict(type='Ready', status='True')])
    service = dict(metadata=dict(name=expected['expected_service_name'], generation=9),
        spec=dict(template=deepcopy(template)), status=dict(observedGeneration=9,
            conditions=[dict(type='Ready', status='True')], latestReadyRevisionName=revision_name,
            latestCreatedRevisionName=revision_name, url='https://synthetic-service-abc-uc.a.run.app',
            traffic=[dict(revisionName=revision_name, percent=100)]))
    bridge = _scheduler_target()['bridge']
    bridge['httpTarget']['uri'] = service['status']['url'] + '/probe'
    bridge['httpTarget']['oidcToken']['audience'] = service['status']['url']
    c = dict(expected=expected, service=service, revision=revision, group=Group(), bridge=bridge)
    c['state'] = lambda e: probe._state(e, read_service=lambda _: c['service'],
        read_json=lambda *_: c['revision'], read_group=lambda *_: c['group'],
        read_bridge=lambda e, u: probe.existing_bridge(e, u, read_json=lambda *_: [deepcopy(c['bridge'])]))
    return c


def test_preflight_only_returns_safe_fingerprints_after_stable_verified_source(case):
    receipt = {}
    hashes, url = probe.preflight(case['expected'], receipt, state=case['state'],
        provenance=lambda _: dict(deployment_provenance_status='verified', serving_stable=True,
                                 selected_source_sha=probe.REVIEWED_SOURCE))
    assert all(receipt['checks'].values())
    assert set(hashes) == probe.FINGERPRINTS
    assert hashes['serving_revision_sha256'] == probe.sha('synthetic-service-abc')
    assert hashes['source_sha'] == probe.REVIEWED_SOURCE
    assert url['url'].endswith('.run.app')
    assert 'U00000001' not in json.dumps(receipt)
    assert 'https://' not in json.dumps(receipt)
    assert receipt['status'] == 'preflight_verified'


@pytest.mark.parametrize('change', ['scope', 'source', 'concurrency', 'maxscale', 'service_scale',
    'command', 'clientzero', 'clientbool', 'groupaccount', 'backend', 'split', 'url', 'generation', 'deploying', 'manualscale'])
def test_preflight_rejects_concrete_identity_or_serialization_risks(case, change):
    revision = case['revision']
    if change == 'scope':
        revision['spec']['containers'][0]['env'][0]['value'] = '{}'
    elif change == 'source':
        revision['metadata']['labels']['commit-sha'] = 'a' * 40
    elif change == 'concurrency':
        revision['spec']['containerConcurrency'] = 2
    elif change == 'maxscale':
        revision['metadata']['annotations']['autoscaling.knative.dev/maxScale'] = '2'
    elif change == 'service_scale':
        case['service']['spec']['template']['metadata']['annotations']['autoscaling.knative.dev/maxScale'] = '2'
    elif change == 'command':
        revision['spec']['containers'][0]['command'] = ['gunicorn', '--workers', '2', 'main:app']
    elif change == 'clientzero':
        case['group'].ib_client_id = 0
    elif change == 'clientbool':
        case['group'].ib_client_id = True
    elif change == 'groupaccount':
        case['group'].account_ids = ('U99999999',)
    elif change == 'backend':
        case['group'].execution_backend = 'quantconnect'
    elif change == 'split':
        case['service']['status']['traffic'] = [dict(revisionName='synthetic-service-abc', percent=50)]
    elif change == 'url':
        case['service']['status']['url'] = 'https://external.invalid/probe'
    elif change == 'generation':
        case['service']['status']['observedGeneration'] = 8
    elif change == 'manualscale':
        case['service']['metadata']['annotations'] = {'run.googleapis.com/manualInstanceCount': '2'}
    else:
        case['service']['status']['latestCreatedRevisionName'] = 'synthetic-service-new'
    with pytest.raises(ValueError):
        case['state'](case['expected'])


def test_changed_config_or_unverified_receipt_never_completes_preflight(case):
    for verified in (False, True):
        calls = []
        def state(expected):
            hashes, url = case['state'](expected)
            calls.append(1)
            if len(calls) == 2:
                hashes['state_sha256'] = 'a' * 64
            return hashes, url
        with pytest.raises(probe.ProbeError):
            probe.preflight(case['expected'], {}, state=state,
                provenance=lambda _: dict(deployment_provenance_status='verified' if verified else 'unknown',
                    serving_stable=True, selected_source_sha=probe.REVIEWED_SOURCE))


def test_exact_reviewed_single_worker_override_is_allowed(case):
    for node in (case['revision'], case['service']['spec']['template']):
        node['spec']['containers'][0].update(command=probe.CMD[:1], args=probe.CMD[1:])
    case['state'](case['expected'])


@pytest.mark.parametrize('outcome', ['ok', 'redirect', 'timeout', 'error', 'forbidden'])
def test_scheduler_is_once_and_unknown_outcome_is_not_retried(outcome):
    calls, records, receipt = [], [], {}
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self, *_): pytest.fail('must not read Scheduler response body')
    class Client:
        def open(self, request, **kwargs):
            calls.append(request)
            assert receipt['scheduler_dispatch_attempted'] is True and records
            assert request.full_url.endswith('/jobs/synthetic-probe:run')
            assert request.method == 'POST' and request.data == b'{}'
            assert dict(request.header_items()) == {'Authorization': 'Bearer ' + PRIVATE,
                                                    'Content-type': 'application/json'}
            assert kwargs['timeout'] == 30
            if outcome == 'timeout': raise URLError(PRIVATE)
            if outcome in {'redirect', 'error', 'forbidden'}:
                code = {'redirect': 302, 'error': 500, 'forbidden': 403}[outcome]
                raise HTTPError(request.full_url, code, PRIVATE, {}, BytesIO(PRIVATE.encode()))
            return Response()
    probe.invoke_once(_scheduler_target(), receipt, token=PRIVATE, opener=Client(), verify_previous=lambda *_: True,
        record=lambda: records.append(deepcopy(receipt)),
        read_terminal=lambda *_: {'scheduler_terminal': True, 'scheduler_request_succeeded': True})
    assert len(calls) == 1 and receipt['scheduler_dispatch_attempts'] == 1
    assert PRIVATE not in json.dumps(receipt)
    assert receipt['status'] == ('scheduler_terminal_observed' if outcome == 'ok' else 'request_outcome_unknown')
    with pytest.raises(probe.ProbeError, match='previous_dispatch_inspect_only'):
        probe.invoke_once(_scheduler_target(), receipt, token=PRIVATE, opener=Client())
    assert len(calls) == 1


def test_no_redirect_handler_does_not_follow_location():
    assert publisher_no_redirect().redirect_request(None, None, 302, '', {}, 'https://other.invalid') is None


def publisher_no_redirect():
    return probe.publisher._NoRedirect()


def test_changed_reviewed_fingerprint_stops_before_token_or_http(monkeypatch, tmp_path, capsys):
    env = dict(GITHUB_REF='refs/heads/main', GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_RUN_ATTEMPT='1',
        GITHUB_REPOSITORY=probe.identity._SOURCE_REPOSITORY, IBKR_ACCOUNT_FACTS_TARGET='additional-1',
        RUNNER_TEMP=str(tmp_path), ACCOUNT_FACTS_PROBE_MODE='invoke')
    hashes = {k: 'b' * (40 if k == 'source_sha' else 64) for k in probe.FINGERPRINTS}
    hashes['source_evidence_sha256'] = probe.sha('deploy_receipt')
    for k, v in env.items(): monkeypatch.setenv(k, v)
    monkeypatch.setenv('ACCOUNT_FACTS_PROBE_REVIEWED_FINGERPRINTS', json.dumps(hashes))
    monkeypatch.setattr(probe.identity, 'resolve_expected', lambda: {})
    monkeypatch.setattr(probe, 'preflight', lambda *_args, **_kwargs: ({**hashes, 'state_sha256': 'f' * 64}, 'https://private.run.app'))
    monkeypatch.setattr(probe, 'invoke_once', lambda *_args, **_kwargs: pytest.fail('must not invoke'))
    assert probe.main([]) == 1
    receipt = json.loads((tmp_path / 'additional1-probe.safe.json').read_text())
    assert receipt['reason'] == 'reviewed_fingerprints_changed'
    assert receipt['scheduler_dispatch_attempted'] is False
    assert 'https://' not in capsys.readouterr().out


def test_workflow_is_manual_fixed_slot_original_identity_and_no_scheduler():
    text = Path('.github/workflows/additional1-account-facts-probe.yml').read_text()
    assert 'workflow_dispatch:' in text and 'schedule:' not in text
    assert 'IBKR_ACCOUNT_FACTS_TARGET: additional-1' in text
    assert 'github.run_attempt == 1' in text
    assert 'ACCOUNT_FACTS_RECOVERY_WIF_PROVIDER' in text
    assert 'vars.IBKR_DEPLOY_SERVICE_ACCOUNT' in text
    assert 'X-QSL-Maintenance' not in text
    assert 'gcloud scheduler' not in text and 'gcloud run deploy' not in text
    assert 'secrets: write' not in text


@pytest.mark.parametrize('key,value', [
    ('GITHUB_REF', 'refs/heads/other'), ('GITHUB_EVENT_NAME', 'push'), ('GITHUB_RUN_ATTEMPT', '2'),
    ('GITHUB_REPOSITORY', 'other/repo'), ('IBKR_ACCOUNT_FACTS_TARGET', 'additional-2'),
])
def test_invalid_dispatch_stops_before_reading_private_configuration(monkeypatch, key, value):
    for k, v in dict(GITHUB_REF='refs/heads/main', GITHUB_EVENT_NAME='workflow_dispatch',
        GITHUB_RUN_ATTEMPT='1', GITHUB_REPOSITORY=probe.identity._SOURCE_REPOSITORY,
        IBKR_ACCOUNT_FACTS_TARGET='additional-1').items(): monkeypatch.setenv(k, v)
    monkeypatch.setenv(key, value)
    monkeypatch.setattr(probe.identity, 'resolve_expected', lambda: pytest.fail('must not read target'))
    with pytest.raises(probe.ProbeError, match='dispatch_not_admitted'):
        probe.context('preflight', {})


@pytest.mark.parametrize('token', [None, '', 'contains whitespace', 'newline\n', 'control\x00'])
def test_token_failure_stops_without_http_or_secret_error_output(token):
    receipt = {}
    with pytest.raises(probe.ProbeError, match='existing_invocation_identity_unavailable') as error:
        probe.invoke_once('https://synthetic.run.app', receipt, token=token,
                          opener=type('Forbidden', (), {'open': lambda *_a, **_k: pytest.fail('must not send HTTP')})())
    assert str(error.value) == 'existing_invocation_identity_unavailable'
    assert not receipt


def _static_evidence(expected):
    return dict(reason='static_files_match', matched_candidate_sha=probe.REVIEWED_SOURCE,
        immutable_image_status='verified', image_content_status='verified', image_command_matches_reviewed=True,
        selected_files_match_candidate=True, serving_stable=True,
        selected_files=dict.fromkeys(probe.static.SOURCE_FILES, True),
        scope_equalities=dict.fromkeys(probe.identity._source_scope({}, expected), True),
        dependencies={k: dict(status='observed', equals_candidate_lock=True) for k in probe.static.PACKAGES},
        loaded_source_status='unknown', independent_release_approval='unknown')


def test_explicit_static_evidence_is_bracketed_and_fingerprint_bound(case):
    order = []
    def state(expected):
        order.append('state')
        return case['state'](expected)
    def static(expected):
        order.append('static')
        return _static_evidence(expected)
    receipt = {}
    fingerprints, _ = probe.preflight(case['expected'], receipt, source_evidence='static_source', state=state,
        static_provenance=static, provenance=lambda _: pytest.fail('must not read deploy logs in explicit static mode'))
    assert order == ['state', 'static', 'state']
    assert receipt['source_evidence'] == 'static_source'
    assert fingerprints['source_evidence_sha256'] == probe.sha('static_source')
    assert all(receipt['checks'].values())


@pytest.mark.parametrize('change', ['reason', 'candidate', 'immutable', 'content', 'command', 'all_files',
    'old_eight_files', 'new_file', 'scope', 'stable', 'dep_status', 'dep_lock'])
def test_static_missing_any_required_evidence_cannot_admit_probe(case, change):
    evidence = _static_evidence(case['expected'])
    if change == 'reason': evidence['reason'] = 'diagnostic_error'
    elif change == 'candidate': evidence['matched_candidate_sha'] = 'a' * 40
    elif change == 'immutable': evidence['immutable_image_status'] = 'unknown'
    elif change == 'content': evidence['image_content_status'] = 'unknown'
    elif change == 'command': evidence.pop('image_command_matches_reviewed')
    elif change == 'all_files': evidence['selected_files_match_candidate'] = False
    elif change == 'old_eight_files':
        evidence['selected_files'] = {k: v for k, v in evidence['selected_files'].items()
            if k in {'entrypoint', 'strategy_adapter', 'composer', 'dependencies', 'broker_adapter',
                     'reporting_adapter', 'project', 'lock'}}
    elif change == 'new_file': evidence['selected_files']['probe_handler'] = None
    elif change == 'scope': evidence['scope_equalities']['account_selector'] = False
    elif change == 'stable': evidence['serving_stable'] = None
    elif change == 'dep_status': evidence['dependencies']['qpk']['status'] = 'unknown'
    else: evidence['dependencies']['qpk']['equals_candidate_lock'] = False
    with pytest.raises(probe.ProbeError, match='static_source_unverified'):
        probe.preflight(case['expected'], {}, source_evidence='static_source', state=case['state'],
                        static_provenance=lambda _: evidence)


def test_deployment_receipt_failure_never_automatically_falls_back_to_static(case):
    with pytest.raises(probe.ProbeError, match='source_receipt_unverified'):
        probe.preflight(case['expected'], {}, state=case['state'], provenance=lambda _: {},
                        static_provenance=lambda _: pytest.fail('must not fall back'))


def test_switching_evidence_cannot_reuse_reviewed_preflight(monkeypatch):
    for k, v in dict(GITHUB_REF='refs/heads/main', GITHUB_EVENT_NAME='workflow_dispatch',
        GITHUB_RUN_ATTEMPT='1', GITHUB_REPOSITORY=probe.identity._SOURCE_REPOSITORY,
        IBKR_ACCOUNT_FACTS_TARGET='additional-1', ACCOUNT_FACTS_PROBE_SOURCE_EVIDENCE='static_source').items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(probe.identity, 'resolve_expected', lambda: {})
    reviewed = {k: 'b' * (40 if k == 'source_sha' else 64) for k in probe.FINGERPRINTS}
    reviewed['source_evidence_sha256'] = probe.sha('deploy_receipt')
    with pytest.raises(probe.ProbeError, match='reviewed_source_evidence_changed'):
        probe.context('invoke', reviewed)


def test_configuration_change_during_static_read_stops_before_invocation(case):
    calls = []
    def state(expected):
        hashes, url = case['state'](expected)
        if calls: hashes['state_sha256'] = 'f' * 64
        calls.append(1)
        return hashes, url
    with pytest.raises(probe.ProbeError, match='configuration_changed_during_preflight'):
        probe.preflight(case['expected'], {}, source_evidence='static_source', state=state,
                        static_provenance=lambda _: _static_evidence(case['expected']))


@pytest.mark.parametrize('fault,reason', [
    ('template_concurrency', 'template_concurrency_unverified'),
    ('revision_concurrency', 'revision_concurrency_unverified'),
    ('template_maxscale', 'template_max_scale_unverified'),
    ('revision_maxscale', 'revision_max_scale_unverified'),
    ('manual_scaling', 'manual_scaling_unverified'),
])
def test_real_serialization_failure_has_only_precise_safe_reason(case, monkeypatch, tmp_path, capsys, fault, reason):
    template, revision, service = case['service']['spec']['template'], case['revision'], case['service']
    if fault.endswith('concurrency'):
        node = template if fault.startswith('template') else revision
        node['spec']['containerConcurrency'] = PRIVATE
    elif fault.endswith('maxscale'):
        node = template if fault.startswith('template') else revision
        node['metadata']['annotations']['autoscaling.knative.dev/maxScale'] = PRIVATE
    else:
        service['metadata']['annotations'] = {'run.googleapis.com/scalingMode': PRIVATE}
    with pytest.raises(probe.ProbeError) as error:
        case['state'](case['expected'])
    assert str(error.value) == reason
    for key, value in dict(GITHUB_REF='refs/heads/main', GITHUB_EVENT_NAME='workflow_dispatch',
        GITHUB_RUN_ATTEMPT='1', GITHUB_REPOSITORY=probe.identity._SOURCE_REPOSITORY,
        IBKR_ACCOUNT_FACTS_TARGET='additional-1', RUNNER_TEMP=str(tmp_path),
        ACCOUNT_FACTS_PROBE_MODE='preflight', ACCOUNT_FACTS_PROBE_REVIEWED_FINGERPRINTS='{}',
        ACCOUNT_FACTS_PROBE_SOURCE_EVIDENCE='static_source').items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(probe.identity, 'resolve_expected', lambda: case['expected'])
    original_preflight = probe.preflight
    def preflight(expected, receipt, **kwargs):
        return original_preflight(expected, receipt, state=case['state'],
            static_provenance=lambda *_args, **_kwargs: pytest.fail('must stop before static source'), **kwargs)
    monkeypatch.setattr(probe, 'preflight', preflight)
    monkeypatch.setattr(probe, 'invoke_once', lambda *_args, **_kwargs: pytest.fail('must not invoke'))
    assert probe.main([]) == 1
    stdout = capsys.readouterr().out
    receipt = json.loads((tmp_path / 'additional1-probe.safe.json').read_text())
    assert receipt['status'] == 'blocked' and receipt['reason'] == reason
    assert receipt['scheduler_dispatch_attempted'] is False and receipt['scheduler_dispatch_attempts'] == 0
    for private in (PRIVATE, 'synthetic-service', 'U00000001', 'https://', 'containerConcurrency', 'maxScale'):
        assert private not in stdout
        assert private not in json.dumps(receipt)


@pytest.mark.parametrize('node_name', ['template', 'revision'])
@pytest.mark.parametrize('value', [2, '1', True, None])
def test_concurrency_non_one_or_non_integer_still_rejected(case, node_name, value):
    node = case['revision'] if node_name == 'revision' else case['service']['spec']['template']
    node['spec']['containerConcurrency'] = value
    with pytest.raises(probe.ProbeError) as error:
        case['state'](case['expected'])
    assert str(error.value) == node_name + '_concurrency_unverified'


@pytest.mark.parametrize('node_name', ['template', 'revision'])
@pytest.mark.parametrize('value', [None, 1, '2'])
def test_missing_or_non_string_one_maxscale_still_rejected(case, node_name, value):
    node = case['revision'] if node_name == 'revision' else case['service']['spec']['template']
    node['metadata']['annotations']['autoscaling.knative.dev/maxScale'] = value
    with pytest.raises(probe.ProbeError) as error:
        case['state'](case['expected'])
    assert str(error.value) == node_name + '_max_scale_unverified'


@pytest.mark.parametrize('cap', [None, '0', '1', '2', '100'])
def test_service_cap_does_not_expand_or_replace_revision_cap(case, cap):
    if cap is not None:
        case['service']['metadata']['annotations'] = {'run.googleapis.com/maxScale': cap}
    receipt = {}
    probe.preflight(case['expected'], receipt, source_evidence='static_source', state=case['state'],
                    static_provenance=lambda _: _static_evidence(case['expected']))
    assert receipt['status'] == 'preflight_verified'
    assert all(receipt['checks'].values())
    assert 'http_attempted' not in receipt


@pytest.mark.parametrize('fault,reason', [
    ('revision_cap', 'revision_max_scale_unverified'),
    ('template_cap', 'template_max_scale_unverified'),
    ('manual', 'manual_scaling_unverified'),
    ('unsupported', 'manual_scaling_unverified'),
])
def test_larger_service_cap_keeps_revision_and_manual_guards(case, fault, reason):
    case['service']['metadata']['annotations'] = {'run.googleapis.com/maxScale': '100'}
    if fault == 'revision_cap':
        case['revision']['metadata']['annotations']['autoscaling.knative.dev/maxScale'] = '2'
    elif fault == 'template_cap':
        case['service']['spec']['template']['metadata']['annotations']['autoscaling.knative.dev/maxScale'] = '2'
    elif fault == 'manual':
        case['service']['metadata']['annotations']['run.googleapis.com/scalingMode'] = 'manual'
    else:
        case['service']['scaling'] = {'maxInstanceCount': 100}
    with pytest.raises(probe.ProbeError) as error:
        case['state'](case['expected'])
    assert str(error.value) == reason


@pytest.fixture
def scheduler_invocation(case, monkeypatch, tmp_path):
    hashes, _ = case['state'](case['expected'])
    hashes['source_evidence_sha256'] = probe.sha('static_source')
    for key, value in dict(GITHUB_REF='refs/heads/main', GITHUB_EVENT_NAME='workflow_dispatch',
        GITHUB_RUN_ATTEMPT='1', GITHUB_REPOSITORY=probe.identity._SOURCE_REPOSITORY,
        IBKR_ACCOUNT_FACTS_TARGET='additional-1', RUNNER_TEMP=str(tmp_path),
        ACCOUNT_FACTS_PROBE_MODE='invoke', ACCOUNT_FACTS_PROBE_SOURCE_EVIDENCE='static_source',
        ACCOUNT_FACTS_PROBE_REVIEWED_FINGERPRINTS=json.dumps(hashes)).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(probe.identity, 'resolve_expected', lambda: case['expected'])
    original_preflight = probe.preflight
    calls = []
    def preflight(expected, receipt, **kwargs):
        calls.append('full_preflight')
        return original_preflight(expected, receipt, state=case['state'],
            static_provenance=lambda _: _static_evidence(expected), **kwargs)
    monkeypatch.setattr(probe, 'preflight', preflight)
    case.update(hashes=hashes, receipt=tmp_path / 'additional1-probe.safe.json', preflight_calls=calls)
    return case


def test_changed_fingerprints_stop_before_access_token_or_scheduler(scheduler_invocation, monkeypatch, capsys):
    case = scheduler_invocation
    case['service']['metadata']['annotations'] = {'run.googleapis.com/maxScale': '10'}
    monkeypatch.setattr(probe.commands, 'command', lambda *_a, **_k: pytest.fail('must not mint token'))
    monkeypatch.setattr(probe, 'invoke_once', lambda *_a, **_k: pytest.fail('must not invoke'))
    assert probe.main([]) == 1
    assert json.loads(capsys.readouterr().out)['reason'] == 'reviewed_fingerprints_changed'
    assert json.loads(case['receipt'].read_text())['scheduler_dispatch_attempted'] is False


def test_normal_preflight_does_not_read_access_token_or_dispatch(scheduler_invocation, monkeypatch, capsys):
    monkeypatch.setenv('ACCOUNT_FACTS_PROBE_MODE', 'preflight')
    monkeypatch.setenv('ACCOUNT_FACTS_PROBE_REVIEWED_FINGERPRINTS', '{}')
    monkeypatch.setattr(probe.commands, 'command', lambda *_a, **_k: pytest.fail('preflight must not mint token'))
    monkeypatch.setattr(probe, 'invoke_once', lambda *_a, **_k: pytest.fail('preflight must not dispatch'))
    assert probe.main([]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'preflight_verified'


def test_access_token_failure_stops_at_zero_dispatch_after_reverification(scheduler_invocation, monkeypatch, capsys):
    def fail(*_a, **_k): raise RuntimeError(PRIVATE)
    monkeypatch.setattr(probe.commands, 'command', fail)
    assert probe.main([]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output['reason'] == 'protected_preflight_read_failed'
    assert output['scheduler_dispatch_attempted'] is False and output['scheduler_dispatch_attempts'] == 0
    assert PRIVATE not in json.dumps(output)
    assert scheduler_invocation['preflight_calls'] == ['full_preflight']


def test_access_token_only_in_one_control_header_after_reverification(scheduler_invocation, monkeypatch, capsys):
    case, requests = scheduler_invocation, []
    def token(args, **kwargs):
        assert args == ['gcloud', 'auth', 'print-access-token']
        return PRIVATE
    monkeypatch.setattr(probe.commands, 'command', token)
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self, *_): pytest.fail('must not read Scheduler response body')
    class Client:
        def open(self, request, **kwargs):
            requests.append(request)
            assert request.get_header('Authorization') == 'Bearer ' + PRIVATE
            assert json.loads(case['receipt'].read_text())['scheduler_dispatch_attempted'] is True
            return Response()
    original_invoke = probe.invoke_once
    monkeypatch.setattr(probe, 'invoke_once', lambda *args, **kwargs:
        original_invoke(*args, **kwargs, opener=Client(), verify_previous=lambda *_: True,
            read_terminal=lambda *_: {'scheduler_terminal': True, 'scheduler_request_succeeded': True}))
    assert probe.main([]) == 0
    stdout = capsys.readouterr().out
    assert len(requests) == 1 and case['preflight_calls'] == ['full_preflight']
    assert PRIVATE not in stdout + case['receipt'].read_text()
    receipt = json.loads(case['receipt'].read_text())
    assert receipt['scheduler_dispatch_attempts'] == 1
    assert receipt['facts_verified'] is False and receipt['published'] is False
    before = case['receipt'].read_bytes()
    monkeypatch.setattr(probe, 'preflight', lambda *_a, **_k: pytest.fail('journal forbids replay'))
    monkeypatch.setattr(probe.commands, 'command', lambda *_a, **_k: pytest.fail('no second token'))
    assert probe.main([]) == 1
    assert case['receipt'].read_bytes() == before and len(requests) == 1


def test_workflow_has_only_existing_wif_scheduler_path():
    text = Path('.github/workflows/additional1-account-facts-probe.yml').read_text()
    source = Path('scripts/invoke_additional1_account_facts_probe.py').read_text()
    assert text.count('uses: google-github-actions/auth@v3') == 1
    assert 'Reverify reviewed fingerprints and run the existing Scheduler once' in text
    assert 'token_format: id_token' not in text and 'id_token_audience:' not in text
    assert 'ACCOUNT_FACTS_PROBE_ID_TOKEN' not in source + text
    assert 'prepare-invocation-auth' not in source + text
    assert 'print-identity-token' not in source
    assert 'continue-on-error' not in text
    assert 'cloudscheduler.googleapis.com/v1/' in source
    assert 'd723ce5d0a7c4b6af81863c842823ed248b6d0eb5329f76dd905beaa14e29c86' not in source


@pytest.mark.parametrize('where', ['revision', 'static_evidence'])
def test_previous_reviewed_source_is_not_a_second_allowed_source(case, where):
    previous = '1595957e388a1d29914a13075d210016c8848404'
    if where == 'revision':
        case['revision']['metadata']['labels']['commit-sha'] = previous
        with pytest.raises(probe.ProbeError, match='source_not_reviewed'):
            case['state'](case['expected'])
    else:
        evidence = _static_evidence(case['expected'])
        evidence['matched_candidate_sha'] = previous
        with pytest.raises(probe.ProbeError, match='static_source_unverified'):
            probe.preflight(case['expected'], {}, source_evidence='static_source', state=case['state'],
                            static_provenance=lambda _: evidence)


def test_previous_source_fingerprints_cannot_dispatch(scheduler_invocation, monkeypatch, capsys):
    case = scheduler_invocation
    reviewed = {**case['hashes'], 'source_sha': '1595957e388a1d29914a13075d210016c8848404'}
    monkeypatch.setenv('ACCOUNT_FACTS_PROBE_REVIEWED_FINGERPRINTS', json.dumps(reviewed))
    monkeypatch.setattr(probe.commands, 'command', lambda *_a, **_k: pytest.fail('old source cannot mint token'))
    assert probe.main([]) == 1
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['reason'] == 'reviewed_fingerprints_changed'
    assert receipt['scheduler_dispatch_attempted'] is False and receipt['scheduler_dispatch_attempts'] == 0


def _scheduler_target():
    return {'url': 'https://synthetic.run.app', 'bridge': {
        'name': 'projects/synthetic-project/locations/us-central1/jobs/synthetic-probe',
        'state': 'ENABLED', 'schedule': '10 22 * * *', 'timeZone': 'UTC',
        'attemptDeadline': '180s', 'retryConfig': {'retryCount': 0, 'maxRetryDuration': '0s'},
        'lastAttemptTime': '2020-01-01T00:00:00Z', 'scheduleTime': '2099-01-01T00:00:00Z',
        'httpTarget': {'uri': 'https://synthetic.run.app/probe', 'httpMethod': 'POST',
                       'oidcToken': {'audience': 'https://synthetic.run.app',
                                     'serviceAccountEmail': 'synthetic@synthetic-project.iam.gserviceaccount.com'}}}}


def test_scheduler_dispatch_records_unknown_before_one_control_post_without_reading_body():
    records, calls, receipt = [], [], {}
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self, *_): pytest.fail('Scheduler response body must not be read')
    class Client:
        def open(self, request, **kwargs):
            assert records and records[0]['scheduler_dispatch_attempted'] is True
            calls.append(request)
            assert request.full_url == ('https://cloudscheduler.googleapis.com/v1/'
                'projects/synthetic-project/locations/us-central1/jobs/synthetic-probe:run')
            assert request.method == 'POST' and request.data == b'{}'
            assert request.get_header('Authorization') == 'Bearer ' + PRIVATE
            return Response()
    probe.invoke_once(_scheduler_target(), receipt, token=PRIVATE, opener=Client(), verify_previous=lambda *_: True,
        record=lambda: records.append(deepcopy(receipt)),
        read_terminal=lambda *_: {'scheduler_terminal': True, 'scheduler_request_succeeded': True})
    assert len(calls) == 1 and receipt['scheduler_dispatch_attempts'] == 1
    assert receipt['status'] == 'scheduler_terminal_observed'
    assert PRIVATE not in json.dumps(receipt)


@pytest.mark.parametrize('fault', ['absent', 'duplicate', 'foreign_name', 'path_injection', 'paused',
    'method', 'audience', 'identity', 'body', 'header', 'retry', 'bool_retry', 'duration', 'deadline'])
def test_existing_bridge_rejects_changed_or_unbound_transport(case, fault):
    job = deepcopy(case['bridge'])
    jobs = [job]
    if fault == 'absent': jobs = []
    elif fault == 'duplicate': jobs.append(deepcopy(job))
    elif fault == 'foreign_name': job['name'] = job['name'].replace('synthetic-project', 'other-project')
    elif fault == 'path_injection': job['name'] += '/other:run'
    elif fault == 'paused': job['state'] = 'PAUSED'
    elif fault == 'method': job['httpTarget']['httpMethod'] = 'GET'
    elif fault == 'audience': job['httpTarget']['oidcToken']['audience'] += '/probe'
    elif fault == 'identity': job['httpTarget']['oidcToken']['serviceAccountEmail'] = ''
    elif fault == 'body': job['httpTarget']['body'] = PRIVATE
    elif fault == 'header': job['httpTarget']['headers'] = {'X-QSL-Maintenance': PRIVATE}
    elif fault == 'retry': job['retryConfig']['retryCount'] = 1
    elif fault == 'bool_retry': job['retryConfig']['retryCount'] = False
    elif fault == 'duration': job['retryConfig']['maxRetryDuration'] = '1s'
    elif fault == 'deadline': job['attemptDeadline'] = '600s'
    with pytest.raises(probe.ProbeError) as error:
        probe.existing_bridge(case['expected'], case['service']['status']['url'], read_json=lambda *_: jobs)
    assert PRIVATE not in str(error.value)


def test_bridge_config_is_fingerprinted_and_attempt_during_static_check_is_rejected(case):
    before, _ = case['state'](case['expected'])
    case['bridge']['schedule'] = '11 22 * * *'
    after, _ = case['state'](case['expected'])
    assert before['state_sha256'] != after['state_sha256']
    def static(expected):
        case['bridge']['lastAttemptTime'] = '2026-10-28T00:00:00Z'
        return _static_evidence(expected)
    with pytest.raises(probe.ProbeError, match='configuration_changed_during_preflight'):
        probe.preflight(case['expected'], {}, source_evidence='static_source',
                        state=case['state'], static_provenance=static)


@pytest.mark.parametrize('field,value,reason', [
    ('lastAttemptTime', '2026-10-28T00:00:00Z', 'recent_or_in_flight'),
    ('lastAttemptTime', '2099-01-01T00:00:00Z', 'recent_or_in_flight'),
    ('scheduleTime', '2026-10-28T00:03:15Z', 'natural_probe_window_conflict'),
])
def test_same_service_recent_attempt_and_natural_window_stop_before_journal_or_post(field, value, reason):
    target, receipt = _scheduler_target(), {}
    target['bridge'][field] = value
    with pytest.raises(probe.ProbeError, match=reason):
        probe.invoke_once(target, receipt, token=PRIVATE, now=datetime(2026, 10, 28, tzinfo=timezone.utc),
            record=lambda: pytest.fail('must not journal a dispatch'),
            opener=type('Forbidden', (), {'open': lambda *_a, **_k: pytest.fail('must not dispatch')})())
    assert not receipt


def _terminal_reader(*, http=200, fault=None, code=None):
    job = _scheduler_target()['bridge']
    current = deepcopy(job)
    current['lastAttemptTime'] = '2026-10-28T00:00:00.010Z'
    current['status'] = {} if code is None else {'code': code}
    started = [{'timestamp': '2026-10-28T00:00:00.100Z'}]
    finished = [{'timestamp': '2026-10-28T00:00:05Z', 'httpRequest': {}, 'jsonPayload': {}}]
    if http is not None: finished[0]['httpRequest']['status'] = http
    if fault == 'duplicate_finished': finished.append(deepcopy(finished[0]))
    elif fault == 'duplicate_started': started.append(deepcopy(started[0]))
    elif fault == 'no_started': started = []
    elif fault == 'old_attempt': current['lastAttemptTime'] = job['lastAttemptTime']
    elif fault == 'config_drift': current['schedule'] = '11 22 * * *'
    elif fault == 'bad_time': started[0]['timestamp'] = PRIVATE
    elif fault == 'log_order':
        finished[0]['timestamp'] = '2026-10-28T00:00:00.100Z'
        started[0]['timestamp'] = '2026-10-28T00:00:00.321Z'
    elif fault == 'late_lastAttemptTime': current['lastAttemptTime'] = '2026-10-28T00:03:16Z'
    elif fault == 'late_started': started[0]['timestamp'] = '2026-10-28T00:03:16Z'
    elif fault == 'late_finished': finished[0]['timestamp'] = '2026-10-28T00:03:16Z'
    calls = []
    def read(args, stage):
        calls.append(args)
        assert PRIVATE not in repr(args)
        if args[:3] == ('gcloud', 'logging', 'read'):
            assert '--limit=2' in args
            return started if 'AttemptStarted' in args[3] else finished
        assert args[:4] == ('gcloud', 'scheduler', 'jobs', 'describe')
        return current
    return job, read, calls


@pytest.mark.parametrize('http,code,success', [(200, None, True), (204, 0, True),
    (500, None, False), (403, 7, False), (None, None, None), (200, 7, None)])
def test_scheduler_terminal_uses_explicit_http_and_optional_empty_job_status(http, code, success):
    job, read, calls = _terminal_reader(http=http, code=code)
    result = probe.terminal_readback(job, '2026-10-28T00:00:00Z', read_json=read,
                                   pause=lambda _: pytest.fail('terminal already present'))
    assert result['scheduler_terminal'] is True
    assert result['scheduler_request_succeeded'] is success
    assert result['scheduler_job_status_code'] == code
    assert len(calls) == 3
    assert PRIVATE not in json.dumps(result)


@pytest.mark.parametrize('fault', ['duplicate_finished', 'duplicate_started', 'no_started', 'old_attempt', 'config_drift'])
def test_ambiguous_or_changed_terminal_is_unknown_without_replay(fault):
    job, read, _ = _terminal_reader(fault=fault)
    result = probe.terminal_readback(job, '2026-10-28T00:00:00Z', read_json=read, pause=lambda _: None)
    assert result == {'scheduler_terminal': False, 'scheduler_request_succeeded': None}


def test_missing_terminal_polling_is_bounded_and_does_not_dispatch():
    reads, pauses = [], []
    result = probe.terminal_readback(_scheduler_target()['bridge'], '2026-10-28T00:00:00Z',
        read_json=lambda *args: reads.append(args) or [], pause=pauses.append)
    assert len(reads) == 40 and pauses == [5] * 39
    assert all(args[0][:3] == ('gcloud', 'logging', 'read') for args in reads)
    assert result == {'scheduler_terminal': False, 'scheduler_request_succeeded': None}


def test_readback_parse_error_after_dispatch_stays_unknown(scheduler_invocation, monkeypatch, capsys):
    monkeypatch.setattr(probe.commands, 'command', lambda *_a, **_k: PRIVATE)
    def after_dispatch(_invocation, receipt, **kwargs):
        receipt.update(scheduler_dispatch_attempted=True, scheduler_dispatch_attempts=1)
        kwargs['record']()
        raise probe.ProbeError('scheduler_time_unverified')
    monkeypatch.setattr(probe, 'invoke_once', after_dispatch)
    assert probe.main([]) == 1
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()][-1]
    assert output['scheduler_dispatch_attempted'] is True
    assert output['status'] == 'request_outcome_unknown'
    assert PRIVATE not in json.dumps(output)


def test_finished_log_can_precede_started_by_221ms():
    job, read, _ = _terminal_reader(fault='log_order')
    result = probe.terminal_readback(job, '2026-10-28T00:00:00Z', read_json=read,
                                   pause=lambda _: pytest.fail('must accept unique terminal'))
    assert result['scheduler_terminal'] is True and result['scheduler_request_succeeded'] is True


@pytest.mark.parametrize('field', ['lastAttemptTime', 'started', 'finished'])
def test_terminal_outside_dispatch_window_is_unknown(field):
    job, read, _ = _terminal_reader(fault='late_' + field)
    assert probe.terminal_readback(job, '2026-10-28T00:00:00Z', read_json=read,
        pause=lambda _: None) == {'scheduler_terminal': False, 'scheduler_request_succeeded': None}


def test_slow_control_reads_stop_at_monotonic_total_deadline():
    elapsed, reads = [0.0], []
    def read(*args):
        reads.append(args)
        elapsed[0] += 100
        return []
    def pause(seconds): elapsed[0] += seconds
    result = probe.terminal_readback(_scheduler_target()['bridge'], '2026-10-28T00:00:00Z',
        read_json=read, pause=pause, clock=lambda: elapsed[0])
    assert len(reads) == 2 and elapsed[0] == 205
    assert result['scheduler_terminal'] is False


@pytest.mark.parametrize('content_type', ['application/json', 'application/octet-stream'])
def test_existing_standard_headers_are_preserved_and_fingerprinted(case, content_type):
    case['bridge']['httpTarget']['headers'] = {'Content-Type': content_type, 'User-Agent': 'Google-Cloud-Scheduler'}
    before, _ = case['state'](case['expected'])
    assert case['bridge']['httpTarget']['headers']['Content-Type'] == content_type
    case['bridge']['httpTarget']['headers']['Content-Type'] = ('application/json'
        if content_type == 'application/octet-stream' else 'application/octet-stream')
    after, _ = case['state'](case['expected'])
    assert before['state_sha256'] != after['state_sha256']


@pytest.mark.parametrize('fault,qualified', [(None, True), ('missing_finished', False),
    ('duplicate_started', False), ('last_attempt_changed', False), ('config_changed', False),
    ('pre_attempt_timestamp', False)])
def test_previous_attempt_requires_its_exact_terminal_pair(fault, qualified):
    job = _scheduler_target()['bridge']
    current = deepcopy(job)
    started = [{'timestamp': '2020-01-01T00:00:00.321Z'}]
    finished = [{'timestamp': '2020-01-01T00:00:00.100Z'}]
    if fault == 'missing_finished': finished = []
    elif fault == 'duplicate_started': started.append(deepcopy(started[0]))
    elif fault == 'last_attempt_changed': current['lastAttemptTime'] = '2020-01-02T00:00:00Z'
    elif fault == 'config_changed': current['schedule'] = '11 22 * * *'
    elif fault == 'pre_attempt_timestamp': finished[0]['timestamp'] = '2019-12-31T23:59:59Z'
    def read(args, stage):
        if args[:3] == ('gcloud', 'logging', 'read'):
            assert 'timestamp>="2020-01-01T00:00:00Z"' in args[3]
            assert 'resource.labels.job_id="synthetic-probe"' in args[3]
            return started if 'AttemptStarted' in args[3] else finished
        return current
    assert probe.previous_terminal(job, datetime(2026, 10, 28, tzinfo=timezone.utc),
                                   read_json=read) is qualified


def test_unverified_previous_terminal_stops_before_journal_or_dispatch():
    receipt = {}
    with pytest.raises(probe.ProbeError, match='previous_scheduler_attempt_terminal_unverified'):
        probe.invoke_once(_scheduler_target(), receipt, token=PRIVATE, verify_previous=lambda *_: False,
            record=lambda: pytest.fail('no attempted dispatch'),
            opener=type('Forbidden', (), {'open': lambda *_a, **_k: pytest.fail('must not dispatch')})())
    assert not receipt
