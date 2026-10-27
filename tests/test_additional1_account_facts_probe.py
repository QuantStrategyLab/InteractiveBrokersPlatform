"""Offline admission and one-attempt transport regressions; no broker/cloud calls."""
from copy import deepcopy
from dataclasses import dataclass
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
    c = dict(expected=expected, service=service, revision=revision, group=Group())
    c['state'] = lambda e: probe._state(e, read_service=lambda _: c['service'],
        read_json=lambda *_: c['revision'], read_group=lambda *_: c['group'])
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
    assert url.endswith('.run.app')
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


@pytest.mark.parametrize('outcome', ['ok', 'redirect', 'timeout', 'error'])
def test_http_is_once_no_maintenance_header_and_unknown_outcome_is_not_retried(outcome):
    calls, records, receipt = [], [], {}
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self, _): return b'Probe OK'
    class Client:
        def open(self, request, **kwargs):
            calls.append(request)
            assert receipt['http_attempted'] is True and records
            assert request.full_url == 'https://synthetic.run.app/probe'
            assert request.method == 'POST'
            assert dict(request.header_items()) == {'Authorization': 'Bearer ' + PRIVATE}
            assert kwargs['timeout'] == 120
            if outcome == 'timeout': raise URLError(PRIVATE)
            if outcome in {'redirect', 'error'}:
                raise HTTPError(request.full_url, 302 if outcome == 'redirect' else 500, PRIVATE, {}, BytesIO(PRIVATE.encode()))
            return Response()
    probe.invoke_once('https://synthetic.run.app', receipt, token=PRIVATE,
        opener=Client(), record=lambda: records.append(deepcopy(receipt)))
    assert len(calls) == 1
    assert receipt['http_attempts'] == 1
    assert PRIVATE not in json.dumps(receipt)
    assert receipt['status'] == ('response_received' if outcome == 'ok' else 'request_outcome_unknown'
                                if outcome == 'timeout' else 'http_failure')


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
    assert receipt['http_attempted'] is False
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
    assert receipt['http_attempted'] is False and receipt['http_attempts'] == 0
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
def native_invocation(case, monkeypatch, tmp_path):
    hashes, _ = case['state'](case['expected'])
    hashes['source_evidence_sha256'] = probe.sha('static_source')
    for key, value in dict(GITHUB_REF='refs/heads/main', GITHUB_EVENT_NAME='workflow_dispatch',
        GITHUB_RUN_ATTEMPT='1', GITHUB_REPOSITORY=probe.identity._SOURCE_REPOSITORY,
        IBKR_ACCOUNT_FACTS_TARGET='additional-1', RUNNER_TEMP=str(tmp_path),
        GITHUB_OUTPUT=str(tmp_path / 'step-output'), ACCOUNT_FACTS_PROBE_MODE='invoke',
        ACCOUNT_FACTS_PROBE_SOURCE_EVIDENCE='static_source',
        ACCOUNT_FACTS_PROBE_REVIEWED_FINGERPRINTS=json.dumps(hashes)).items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('ACCOUNT_FACTS_PROBE_ID_TOKEN', raising=False)
    monkeypatch.setattr(probe.identity, 'resolve_expected', lambda: case['expected'])
    original_preflight = probe.preflight
    calls = []
    def preflight(expected, receipt, **kwargs):
        calls.append('full_preflight')
        return original_preflight(expected, receipt, state=case['state'],
            static_provenance=lambda _: _static_evidence(expected), **kwargs)
    monkeypatch.setattr(probe, 'preflight', preflight)
    case.update(hashes=hashes, output=tmp_path / 'step-output',
                receipt=tmp_path / 'additional1-probe.safe.json', preflight_calls=calls)
    return case


def test_prepare_auth_exports_masked_audience_only_after_full_exact_preflight(native_invocation, monkeypatch, capsys):
    case = native_invocation
    monkeypatch.setattr(probe, 'invoke_once', lambda *_a, **_k: pytest.fail('prepare cannot invoke'))
    original_get = probe.os.environ.get
    def get(key, *args):
        assert key != 'ACCOUNT_FACTS_PROBE_ID_TOKEN', 'prepare must not read an invocation token'
        return original_get(key, *args)
    monkeypatch.setattr(probe.os.environ, 'get', get)
    assert probe.main(['--prepare-invocation-auth']) == 0
    stdout = capsys.readouterr().out.splitlines()
    audience = 'https://synthetic-service-abc-uc.a.run.app'
    assert stdout[0] == '::add-mask::' + audience
    assert case['output'].read_text() == 'invocation_audience=' + audience + '\n'
    receipt = json.loads(case['receipt'].read_text())
    assert receipt['status'] == 'invocation_auth_prepared'
    assert receipt['fingerprints'] == case['hashes'] and all(receipt['checks'].values())
    assert receipt['http_attempted'] is False and receipt['http_attempts'] == 0
    assert audience not in json.dumps(receipt)
    assert case['preflight_calls'] == ['full_preflight']


@pytest.mark.parametrize('phase', ['prepare', 'invoke'])
def test_changed_fingerprints_stop_before_audience_or_token_read(native_invocation, monkeypatch, capsys, phase):
    case = native_invocation
    case['service']['metadata']['annotations'] = {'run.googleapis.com/maxScale': '10'}
    monkeypatch.setattr(probe, 'invoke_once', lambda *_a, **_k: pytest.fail('must not invoke'))
    original_get = probe.os.environ.get
    def get(key, *args):
        assert key != 'ACCOUNT_FACTS_PROBE_ID_TOKEN', 'changed fingerprints must stop before token read'
        return original_get(key, *args)
    monkeypatch.setattr(probe.os.environ, 'get', get)
    assert probe.main(['--prepare-invocation-auth'] if phase == 'prepare' else []) == 1
    assert json.loads(capsys.readouterr().out)['reason'] == 'reviewed_fingerprints_changed'
    assert not case['output'].exists()
    assert json.loads(case['receipt'].read_text())['http_attempted'] is False


def test_normal_preflight_does_not_prepare_audience_or_read_native_token(native_invocation, monkeypatch, capsys):
    case = native_invocation
    monkeypatch.setenv('ACCOUNT_FACTS_PROBE_MODE', 'preflight')
    monkeypatch.setenv('ACCOUNT_FACTS_PROBE_REVIEWED_FINGERPRINTS', '{}')
    original_get = probe.os.environ.get
    def get(key, *args):
        assert key != 'ACCOUNT_FACTS_PROBE_ID_TOKEN'
        return original_get(key, *args)
    monkeypatch.setattr(probe.os.environ, 'get', get)
    assert probe.main([]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'preflight_verified'
    assert not case['output'].exists()
    assert probe.main(['--prepare-invocation-auth']) == 1
    assert json.loads(capsys.readouterr().out)['reason'] == 'invocation_auth_requires_invoke'


def test_native_auth_missing_stops_at_zero_http_after_reverification(native_invocation, capsys):
    case = native_invocation
    assert probe.main([]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output['reason'] == 'existing_invocation_identity_unavailable'
    assert output['http_attempted'] is False and output['http_attempts'] == 0
    assert case['preflight_calls'] == ['full_preflight']


def test_native_id_token_is_only_in_one_http_header_after_reverification(native_invocation, monkeypatch, capsys):
    case, requests = native_invocation, []
    assert probe.main(['--prepare-invocation-auth']) == 0
    capsys.readouterr()
    monkeypatch.setenv('ACCOUNT_FACTS_PROBE_ID_TOKEN', PRIVATE)
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self, _): return b'Probe OK'
    class Client:
        def open(self, request, **kwargs):
            requests.append(request)
            assert request.get_header('Authorization') == 'Bearer ' + PRIVATE
            assert json.loads(case['receipt'].read_text())['http_attempted'] is True
            return Response()
    original_invoke = probe.invoke_once
    monkeypatch.setattr(probe, 'invoke_once', lambda *args, **kwargs:
                        original_invoke(*args, **kwargs, opener=Client()))
    assert probe.main([]) == 0
    stdout = capsys.readouterr().out
    assert len(requests) == 1 and case['preflight_calls'] == ['full_preflight', 'full_preflight']
    assert PRIVATE not in stdout + case['receipt'].read_text() + case['output'].read_text()
    receipt = json.loads(case['receipt'].read_text())
    assert receipt['http_attempted'] is True and receipt['http_attempts'] == 1
    assert receipt['facts_verified'] is False and receipt['published'] is False


@pytest.mark.parametrize('url', ['https://synthetic.run.app\nINJECT=value', 'https://synthetic.run.app\r',
    'https://synthetic.run.app\x00'])
def test_prepare_cannot_inject_private_workflow_command_or_output(tmp_path, monkeypatch, capsys, url):
    output = tmp_path / 'step-output'
    monkeypatch.setenv('GITHUB_OUTPUT', str(output))
    with pytest.raises(probe.ProbeError, match='canonical_url_invalid'):
        probe.prepare_invocation_auth(url, {})
    assert not output.exists() and capsys.readouterr().out == ''


def test_workflow_native_auth_is_same_identity_and_only_after_prepare():
    text = Path('.github/workflows/additional1-account-facts-probe.yml').read_text()
    prepared = text.index('id: prepare_invocation_auth')
    native = text.index('id: invocation_auth')
    final = text.index('Reverify reviewed fingerprints and invoke once')
    assert prepared < native < final
    native_step = text[native:final]
    assert "if: ${{ inputs.mode == 'invoke' }}" in text[prepared:native]
    assert 'uses: google-github-actions/auth@v3' in native_step
    assert 'workload_identity_provider: ${{ secrets.ACCOUNT_FACTS_RECOVERY_WIF_PROVIDER }}' in native_step
    assert 'service_account: ${{ vars.IBKR_DEPLOY_SERVICE_ACCOUNT }}' in native_step
    assert 'token_format: id_token' in native_step
    assert 'id_token_audience: ${{ steps.prepare_invocation_auth.outputs.invocation_audience }}' in native_step
    assert 'create_credentials_file: false' in native_step and 'export_environment_variables: false' in native_step
    assert 'ACCOUNT_FACTS_PROBE_ID_TOKEN: ${{ steps.invocation_auth.outputs.id_token }}' in text[final:]
    assert 'continue-on-error' not in text
    assert 'print-identity-token' not in Path('scripts/invoke_additional1_account_facts_probe.py').read_text()
