"""Synthetic controls: no cloud, secret values, broker or production calls."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import notification_marker_check as control
from scripts import notification_marker_probe as probe

ROOT = Path(__file__).resolve().parents[1]


def admitted(mode='binding-read'):
    return {'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_REF': 'refs/heads/main', 'GITHUB_RUN_ATTEMPT': '1',
            'GITHUB_SHA': 'a' * 40, 'INPUT_SOURCE_COMMIT': 'a' * 40, 'INPUT_NOTIFICATION_MARKER_MODE': mode,
            'INPUT_TARGET': 'configured', 'INPUT_CONFIGURED_SERVICE': '', 'INPUT_NOTIFICATION_ONLY': 'false',
            'INPUT_NOTIFICATION_READBACK_ONLY': 'false', 'INPUT_DEPLOY_IMAGE': 'false', 'INPUT_SYNC_ENV': 'false',
            'INPUT_APPROVE_TRAFFIC_SHIFT': 'false', 'INPUT_APPROVE_SCHEDULER_SYNC': 'false',
            'INPUT_APPROVE_MARKER_PROBE': 'true' if mode == 'execute-probe' else 'false',
            'ENABLE_GITHUB_CLOUD_RUN_DEPLOY': 'true', 'GCP_PROJECT_ID': control.PROJECT,
            'GCP_DEPLOY_SERVICE_ACCOUNT': control.DEPLOYER, 'GCP_RUNTIME_SERVICE_ACCOUNT': control.RUNTIME}


@pytest.mark.parametrize('key,value', [('GITHUB_REF', 'refs/heads/branch'), ('GITHUB_EVENT_NAME', 'push'),
    ('GITHUB_RUN_ATTEMPT', '2'), ('INPUT_SOURCE_COMMIT', 'b' * 40), ('INPUT_DEPLOY_IMAGE', 'true'),
    ('INPUT_SYNC_ENV', 'true'), ('INPUT_APPROVE_TRAFFIC_SHIFT', 'true'), ('INPUT_APPROVE_SCHEDULER_SYNC', 'true'),
    ('INPUT_CONFIGURED_SERVICE', 'other'), ('INPUT_NOTIFICATION_ONLY', 'true'), ('INPUT_APPROVE_MARKER_PROBE', 'false'),
    ('GCP_PROJECT_ID', 'other'), ('GCP_DEPLOY_SERVICE_ACCOUNT', 'other')])
def test_boundary_rejects_before_cloud(key, value):
    env = admitted('execute-probe')
    assert control.validate_dispatch('execute-probe', env) == 'a' * 40
    env[key] = value
    with pytest.raises(control.Stop):
        control.validate_dispatch('execute-probe', env)


@pytest.fixture
def binding(monkeypatch):
    target = {'service_name': 'synthetic-service', 'account_selector': 'synthetic-private-selector',
              'strategy_profile': 'tqqq_growth_income', 'execution_mode': 'live', 'dry_run_only': False}
    fp = hashlib.sha256(json.dumps(target, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    monkeypatch.setattr(control, 'TARGET_HASH', fp)
    return {'RUNTIME_TARGET_JSON': target, 'DIGEST_OPAQUE_ACCOUNT_UID': 'synthetic-uid',
            'DIGEST_TARGET_ID': 'synthetic-target', 'DIGEST_STORE_TARGET': 'synthetic-slot'}


def test_binding_projects_only_real_existing_four_fields(binding):
    row, receipt = control.binding_projection({'DIGEST_TARGET_BINDINGS_JSON': json.dumps([binding])})
    assert row == {'platform_id': 'ibkr', 'opaque_account_uid': 'synthetic-uid',
                   'target_id': 'synthetic-target', 'strategy_profile': 'tqqq_growth_income'}
    assert receipt['exact_binding_count'] == 1
    assert receipt['four_field_projection_sha256'] == control.digest(row)
    assert 'synthetic-private-selector' not in json.dumps(row) + json.dumps(receipt)
    assert 'synthetic-uid' not in json.dumps(receipt)


@pytest.mark.parametrize('credential_key', ['TELEGRAM_TOKEN', 'client_secret', 'ACCESS_TOKEN', 'password'])
def test_sensitive_binding_never_extracted(binding, credential_key):
    binding[credential_key] = 'synthetic-private-credential'
    with pytest.raises(control.Stop, match='sensitive'):
        control.binding_projection({'DIGEST_TARGET_BINDINGS_JSON': json.dumps([binding])})


def test_matched_binding_cannot_borrow_global_uid(binding):
    binding.pop('DIGEST_OPAQUE_ACCOUNT_UID')
    with pytest.raises(control.Stop):
        control.binding_projection({'DIGEST_TARGET_BINDINGS_JSON': json.dumps([binding]),
                                    'DIGEST_OPAQUE_ACCOUNT_UID': 'unrelated-global-uid'})


def test_duplicate_or_wrong_target_is_not_unique(binding):
    with pytest.raises(control.Stop):
        control.binding_projection({'DIGEST_TARGET_BINDINGS_JSON': json.dumps([binding, binding])})
    binding['RUNTIME_TARGET_JSON']['service_name'] = 'different'
    with pytest.raises(control.Stop):
        control.binding_projection({'DIGEST_TARGET_BINDINGS_JSON': json.dumps([binding])})


def test_exact_global_target_supports_empty_binding_list(binding):
    env = {**binding, 'RUNTIME_TARGET_JSON': json.dumps(binding['RUNTIME_TARGET_JSON']), 'DIGEST_TARGET_BINDINGS_JSON': '[]'}
    row, receipt = control.binding_projection(env)
    assert row['opaque_account_uid'] == 'synthetic-uid'
    assert receipt['source'] == 'exact_global_target'


def test_global_defaults_require_complete_same_target(binding):
    configured = json.dumps(binding['RUNTIME_TARGET_JSON'])
    binding.pop('DIGEST_STORE_TARGET')
    row, receipt = control.binding_projection({'DIGEST_TARGET_BINDINGS_JSON': json.dumps([binding]),
        'RUNTIME_TARGET_JSON': configured, 'DIGEST_STORE_TARGET': 'synthetic-exact-slot'})
    assert row['opaque_account_uid'] == 'synthetic-uid'
    assert receipt['source'] == 'matching_binding_with_exact_global_defaults'
    with pytest.raises(control.Stop):
        control.binding_projection({'DIGEST_TARGET_BINDINGS_JSON': json.dumps([binding]),
            'RUNTIME_TARGET_JSON': configured.replace('synthetic-service', 'unrelated-service'), 'DIGEST_STORE_TARGET': 'other-slot'})


def test_private_projection_file_matches_safe_hash_without_selector(binding, monkeypatch, tmp_path, capsys):
    env = {**admitted(), 'DIGEST_TARGET_BINDINGS_JSON': json.dumps([binding])}
    monkeypatch.setattr(control.os, 'environ', env)
    private, report = tmp_path / 'binding.private.json', tmp_path / 'binding.safe.json'
    monkeypatch.setattr('sys.argv', ['check', '--mode', 'binding-read', '--report', str(report), '--private-out', str(private)])
    assert control.main() == 0
    safe = json.loads(report.read_text())
    assert hashlib.sha256(private.read_bytes()).hexdigest() == safe['four_field_projection_sha256']
    assert private.stat().st_mode & 0o777 == 0o600
    assert 'synthetic-private-selector' not in private.read_text() + report.read_text() + capsys.readouterr().out


class Store:
    def __init__(self, existing=False, deny=False):
        self.body = {'other': 'untouched'} if existing else None
        self.live = '9' if existing else None
        self.next = 10
        self.history = {}
        self.calls = []
        self.deny = deny
    def request(self, method, path, body=None, query=None):
        assert method in {'GET', 'POST'}
        self.calls.append(method)
        if self.deny and method == 'POST':
            raise probe.Stop('permission_denied', 403)
        if method == 'GET':
            if self.live is None:
                raise probe.Stop('not_found', 404)
            return copy.deepcopy(self.body) if query.get('alt') == 'media' else {'generation': self.live}
        condition = str(query['ifGenerationMatch'])
        if condition != (self.live or '0'):
            raise probe.Stop('precondition_failed', 412)
        self.next += 1
        self.live = str(self.next)
        self.body = json.loads(body)
        self.history[self.live] = self.body
        return {'generation': self.live}


def test_probe_keeps_object_and_records_generations():
    store = Store()
    report = probe.qualify(store)
    assert report['status'] == 'pass_retained'
    assert report['cleanup_attempted'] is False
    assert report['observed_generations'] == ['11', '12']
    assert report['retained_live_generation'] == store.live == '12'
    assert store.body == probe.phase(2)
    assert 'DELETE' not in store.calls


def test_probe_existing_or_denied_stops_without_deletion():
    for store in (Store(existing=True), Store(deny=True)):
        report = probe.qualify(store)
        assert report['status'] == 'stop'
        assert report['probe_id'] == probe.NONCE
        assert store.calls in (['GET'], ['GET', 'POST'])


class Session:
    def __init__(self, *, status=404, sdk='NOT_FOUND', stale=False, error_code=None,
                 structured=False, failed=False, terminal_error=False, control_denied=False):
        self.status, self.sdk, self.stale = status, sdk, stale
        self.error_code = status if error_code is None else error_code
        self.structured, self.failed = structured, failed
        self.terminal_error, self.control_denied = terminal_error, control_denied
        self.calls = []
        self.body = None
    def request(self, method, url, json=None, **kwargs):
        self.calls.append((method, url, copy.deepcopy(json)))
        if len(self.calls) == 1:
            return self.response(self.status, {'error': {'code': self.error_code, 'status': self.sdk}})
        if url.endswith('/jobs?jobId=' + control.JOB_ID):
            self.body = copy.deepcopy(json)
            return self.response(200, self.op({'name': control.JOB}))
        if method == 'GET':
            return self.response(200, {**self.body, 'name': control.JOB, 'etag': 'synthetic-etag'})
        if url.endswith(':run'):
            assert json == {'etag': 'synthetic-etag'}
            if self.stale:
                return self.response(409, {'error': {'code': 409, 'status': 'ABORTED'}})
            if self.control_denied:
                return self.response(403, {'error': {'code': 403, 'status': 'PERMISSION_DENIED'}})
            execution = {'name': control.JOB + '/executions/synthetic-execution', 'job': control.JOB,
                'taskCount': 1, 'succeededCount': 0 if self.failed else 1, 'failedCount': 1 if self.failed else 0,
                'conditions': [{'type': 'Completed', 'state': 'CONDITION_FAILED' if self.failed else 'CONDITION_SUCCEEDED'}]}
            if self.terminal_error:
                return self.response(200, {'name': control.PARENT + '/operations/synthetic-op', 'done': True,
                    'metadata': execution, 'error': {'code': 9, 'message': 'private error must not be copied'}})
            return self.response(200, self.op(execution))
        assert url == 'https://logging.googleapis.com/v2/entries:list'
        proof = {'schema_version': 1, 'probe_id': control.NONCE, 'status': 'pass_retained', 'CAS_checks_passed': True,
                 'runtime_principal_matches': True, 'cleanup_attempted': False, 'write_outcome_unknown': False,
                 'production_marker_touched': False, 'observed_generations': ['11', '12'], 'retained_live_generation': '12'}
        if self.failed:
            proof.update(status='stop', reason='transport_unknown', CAS_checks_passed=False,
                         residual_generations=['11'], write_outcome_unknown=True,
                         private_field='synthetic-private-value')
            proof.pop('retained_live_generation')
        payload = {'jsonPayload': proof} if self.structured else {'textPayload': __import__('json').dumps(proof)}
        return self.response(200, {'entries': [payload]})
    @staticmethod
    def response(status, body):
        return SimpleNamespace(status_code=status, json=lambda: body)
    @staticmethod
    def op(response):
        return {'name': control.PARENT + '/operations/synthetic-op', 'done': True, 'response': response}


def test_exact_once_control_and_retained_receipt():
    session, report = Session(), {}
    control.execute_probe(session, report, sleep=lambda _: None)
    assert report['status'] == 'pass_retained'
    assert report['job_create_attempts'] == report['execution_submit_attempts'] == 1
    assert report['retained_live_generation'] == '12'
    assert not any(method == 'DELETE' for method, _, _ in session.calls)
    assert 'name' not in session.calls[1][2]
    assert session.calls[1][1].endswith('/jobs?jobId=' + control.JOB_ID)
    assert control.job_body()['template']['template']['containers'][0].get('env') is None


def test_structured_receipt_is_accepted_without_retry():
    session, report = Session(structured=True), {}
    control.execute_probe(session, report, sleep=lambda _: None)
    assert report['status'] == 'pass_retained'
    assert len([x for x in session.calls if x[1].endswith('entries:list')]) == 1


@pytest.mark.parametrize('terminal_error', [False, True])
def test_failed_bound_execution_preserves_only_safe_stop_certificate(terminal_error):
    session, report = Session(failed=True, structured=True, terminal_error=terminal_error), {}
    with pytest.raises(control.Stop, match='execution_not_successful'):
        control.execute_probe(session, report, sleep=lambda _: None)
    assert report['probe_receipt']['probe_id'] == control.NONCE
    assert report['probe_receipt']['residual_generations'] == ['11']
    assert report['probe_receipt']['write_outcome_unknown'] is True
    assert 'synthetic-private-value' not in json.dumps(report)
    assert 'private error' not in json.dumps(report)
    assert len([x for x in session.calls if x[1].endswith('entries:list')]) == 1


def test_control_denial_stops_without_log_read():
    session = Session(control_denied=True)
    with pytest.raises(control.Stop):
        control.execute_probe(session, {}, sleep=lambda _: None)
    assert not any(x[1].endswith('entries:list') for x in session.calls)


def test_float_404_is_not_proof_of_absence():
    session = Session(error_code=404.0)
    with pytest.raises(control.Stop):
        control.execute_probe(session, {}, sleep=lambda _: None)
    assert len(session.calls) == 1


class Replies:
    def __init__(self, *values):
        self.values, self.calls = list(values), []
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return Session.response(*value)


def test_control_error_retains_only_status_and_approved_field_paths():
    response = {'error': {'code': 400, 'status': 'INVALID_ARGUMENT', 'message': 'synthetic-private-token',
        'details': [{'@type': 'type.googleapis.com/google.rpc.BadRequest', 'fieldViolations': [
            {'field': 'job.template.template.containers[0].args[3]', 'description': 'synthetic-private-value'},
            {'field': 'job.template.template.containers[0].env[0].synthetic-private-selector'}]},
            {'@type': 'type.googleapis.com/google.rpc.ErrorInfo', 'metadata': {'private': 'synthetic-private-value'}}]}}
    report, session = {}, Replies((400, response))
    with pytest.raises(control.Stop, match='control_request_failed_no_retry'):
        control.control_request(session, report, 'POST', control.API + control.PARENT + '/jobs', phase='job_create')
    entry = report['requests'][0]
    assert entry['sdk_status'] == 'INVALID_ARGUMENT' and entry['sdk_code'] == 400
    assert entry['bad_request_fields'] == ['job.template.template.containers[0].args[3]']
    assert entry['unlisted_field_violations'] == 1
    assert 'synthetic-private' not in json.dumps(report)
    assert len(session.calls) == 1


@pytest.mark.parametrize('field,canonical', [
    ('job.template.annotations', 'job.template.annotations'),
    ('job.template.template.containers[0].args[123]', 'job.template.template.containers[0].args[123]'),
    ('template.template.containers[0].resources.limits', 'job.template.template.containers[0].resources.limits'),
    ('job.template.template.containers[0].resources.limits[cpu]', 'job.template.template.containers[0].resources.limits.cpu'),
    ('job.template.template.containers[0].resources.limits["cpu"]', 'job.template.template.containers[0].resources.limits.cpu'),
    ("job.template.template.containers[0].resources.limits['memory']", 'job.template.template.containers[0].resources.limits.memory'),
    ('job.template.template.service_account', 'job.template.template.serviceAccount'),
    ('job.template.task_count', 'job.template.taskCount'),
    ('job.annotations.qsl.dev/probe-code-sha256', 'job.annotations[qsl.dev/probe-code-sha256]'),
    ('job.annotations["qsl.dev/probe-code-sha256"]', 'job.annotations[qsl.dev/probe-code-sha256]'),
    ('job.annotations[qsl.dev/probe-code-sha256]', 'job.annotations[qsl.dev/probe-code-sha256]'),
])
def test_control_error_publishes_only_confirmed_schema_paths_and_normalizes_aliases(field, canonical):
    safe = control.control_error_projection({'code': 400, 'status': 'INVALID_ARGUMENT',
        'message': 'Invalid value: synthetic-private-token',
        'details': [{'@type': 'type.googleapis.com/google.rpc.BadRequest', 'fieldViolations': [
            {'field': field, 'description': 'Invalid value: synthetic-private-selector'}]}]})
    assert safe['bad_request_fields'] == [canonical]
    assert safe['bad_request_violations'] == [{'field': canonical, 'category': 'invalid_value'}]
    assert safe['unlisted_field_violations'] == 0
    assert 'synthetic-private' not in json.dumps(safe)


@pytest.mark.parametrize('field', [
    'job..template', 'job.template[-1]', 'job.template[1000000]',
    'job.template["synthetic-private-token"]',
    'job.template\nsynthetic-private-token', 'job.template=synthetic-private-token',
    'https://synthetic-private-token', 'job.' + 'a' * 65, 'job.' + 'a.' * 256 + 'x',
    None, 123, True,
])
def test_control_error_rejects_values_and_unbounded_paths(field):
    safe = control.control_error_projection({'code': 400, 'status': 'INVALID_ARGUMENT',
        'details': [{'@type': 'type.googleapis.com/google.rpc.BadRequest', 'fieldViolations': [
            {'field': field, 'description': 'synthetic-private-token'}]}]})
    assert safe['bad_request_fields'] == []
    assert safe['unlisted_field_violations'] == 1
    assert 'synthetic-private' not in json.dumps(safe)


def test_control_error_categorizes_server_constraint_without_exporting_request_values():
    safe = control.control_error_projection({'code': 400, 'status': 'INVALID_ARGUMENT',
        'message': 'Name must be at most 49 characters: synthetic-private-resource',
        'details': [{'@type': 'type.googleapis.com/google.rpc.BadRequest', 'fieldViolations': [
            {'field': 'job.name', 'description': 'Name length exceeds maximum: synthetic-private-value'},
            {'field': 'job.template.annotations', 'description': 'unsupported field: synthetic-private-value'}]},
            {'@type': 'type.googleapis.com/google.rpc.ErrorInfo',
             'reason': 'SYNTHETIC_PRIVATE_TOKEN', 'metadata': {'value': 'synthetic-private-value'}}]})
    assert safe['error_category'] == 'maximum_length_exceeded'
    assert safe['bad_request_violations'] == [
        {'field': 'job.name', 'category': 'maximum_length_exceeded'},
        {'field': 'job.template.annotations', 'category': 'unsupported_field'}]
    assert 'synthetic-private' not in json.dumps(safe).lower()
    assert 'synthetic_private' not in json.dumps(safe).lower()


@pytest.mark.parametrize('violation,state,kind,length', [
    ({}, 'missing', 'missing', None),
    ({'field': None}, 'null', 'null', None),
    ({'field': 123}, 'non_string', 'integer', None),
    ({'field': ''}, 'empty', 'string', 0),
    ({'field': 'x' * 513}, 'too_long', 'string', 513),
    ({'field': '/job/template/containers/0/args/3'}, 'syntax_rejected', 'string', 33),
    ({'field': 'job.template.synthetic_private_token'}, 'unknown_schema', 'string', 36),
    ({'field': 'job.template[synthetic_private_token]'}, 'unknown_schema', 'string', 37),
])
def test_control_error_rejected_field_still_retains_description_category(violation, state, kind, length):
    violation = {**violation, 'description': 'Name length exceeds maximum: synthetic_private_value'}
    safe = control.control_error_projection({'code': 400, 'status': 'INVALID_ARGUMENT',
        'message': 'Request rejected', 'details': [{'@type': 'type.googleapis.com/google.rpc.BadRequest',
                                                 'fieldViolations': [violation]}]})
    row = safe['field_violation_diagnostics'][0]
    assert row['violation_type'] == 'object'
    assert row['category'] == 'maximum_length_exceeded'
    assert row['field_state'] == state and row['field_type'] == kind
    assert row['field_length'] == length
    assert row['detail_index'] == 0 and row['violation_index'] == 0
    assert safe['bad_request_fields'] == [] and safe['unlisted_field_violations'] == 1
    assert 'synthetic_private' not in json.dumps(safe)


def test_control_error_preserves_duplicates_and_reports_truncated_scan():
    violation = {'field': 'job.name', 'description': 'Invalid value: synthetic_private_value'}
    detail = {'@type': 'type.googleapis.com/google.rpc.BadRequest', 'fieldViolations': [violation] * 33}
    safe = control.control_error_projection({'code': 400, 'status': 'INVALID_ARGUMENT',
                                            'details': [detail] * 11})
    scan = safe['bad_request_scan']
    assert scan['details_total'] == 11 and scan['details_seen'] == 10
    assert scan['details_truncated_count'] == 1
    assert scan['violations_total_in_seen_details'] == 330
    assert scan['violations_seen'] == 320 and scan['violations_truncated_in_seen_details'] == 10
    assert scan['incomplete'] is True
    assert len(safe['field_violation_diagnostics']) == 320
    assert safe['field_violation_diagnostics'][-1]['detail_index'] == 9
    assert safe['field_violation_diagnostics'][-1]['violation_index'] == 31
    assert 'synthetic_private' not in json.dumps(safe)


def test_control_error_mixed_rows_keep_rejected_categories_and_same_field_different_constraints():
    violations = [
        {'field': 'job.name', 'description': 'Invalid value: synthetic_private_value'},
        {'field': 'job.name', 'description': 'Name length exceeds maximum: synthetic_private_value'},
        {'field': '', 'description': 'Name length exceeds maximum: synthetic_private_value'},
        {'field': 'job.template.synthetic_private_token', 'description': 'Missing field is required'},
        {'field': 'job.template[synthetic_private_token]', 'description': 'Invalid format: synthetic_private_value'},
        {'field': 'job.template.template.service_account', 'description': 'Malformed value'},
    ]
    safe = control.control_error_projection({'code': 400, 'status': 'INVALID_ARGUMENT',
        'message': 'Request rejected', 'details': [{'@type': 'type.googleapis.com/google.rpc.BadRequest',
                                                 'fieldViolations': violations}]})
    rows = safe['field_violation_diagnostics']
    assert len(rows) == 6 and [r['violation_index'] for r in rows] == list(range(6))
    assert [r['category'] for r in rows] == ['invalid_value', 'maximum_length_exceeded',
        'maximum_length_exceeded', 'required_field', 'invalid_format', 'invalid_format']
    assert rows[3]['known_parent'] == rows[4]['known_parent'] == 'job.template'
    assert rows[5]['field'] == 'job.template.template.serviceAccount'
    assert safe['bad_request_scan']['violations_seen'] == 6 and safe['bad_request_scan']['incomplete'] is False
    assert 'synthetic_private' not in json.dumps(safe)


@pytest.mark.parametrize('value,kind', [(None, 'null'), (False, 'boolean'), (1.5, 'number'),
                                      ([], 'array'), ({'private': 'synthetic_private_value'}, 'object')])
def test_control_error_non_object_violation_shape_is_visible_without_value(value, kind):
    safe = control.control_error_projection({'code': 400, 'status': 'INVALID_ARGUMENT',
        'details': [{'@type': 'type.googleapis.com/google.rpc.BadRequest', 'fieldViolations': [value]}]})
    row = safe['field_violation_diagnostics'][0]
    assert row['violation_type'] == kind
    if kind == 'object':
        assert row['field_state'] == 'missing'
    else:
        assert row['field_state'] == 'violation_not_object'
    assert row['field_type'] == 'missing'
    assert 'synthetic_private' not in json.dumps(safe)


@pytest.mark.parametrize('details', [None, {'private': 'synthetic_private_value'},
    [{'@type': 'type.googleapis.com/google.rpc.BadRequest'}],
    [{'@type': 'type.googleapis.com/google.rpc.BadRequest', 'fieldViolations': None}],
    [{'@type': 'type.googleapis.com/google.rpc.BadRequest', 'fieldViolations': {}}],
    [{'@type': 'synthetic_private_type', 'fieldViolations': []}], [None]])
def test_control_error_malformed_details_have_shape_and_incomplete_markers(details):
    safe = control.control_error_projection({'code': 400, 'status': 'INVALID_ARGUMENT', 'details': details})
    scan = safe['bad_request_scan']
    assert scan['incomplete'] is True and scan['violations_seen'] == 0
    assert scan['details_state'] in {'null', 'object', 'array'}
    assert 'synthetic_private' not in json.dumps(safe)


def test_control_error_reports_text_classification_truncation_without_exporting_text():
    safe = control.control_error_projection({'code': 400, 'status': 'INVALID_ARGUMENT',
        'message': 'Request rejected ' + 'x' * 4096,
        'details': [{'@type': 'type.googleapis.com/google.rpc.BadRequest', 'fieldViolations': [
            {'field': '', 'description': 'Name length exceeds maximum ' + 'x' * 4096}]}]})
    assert safe['message_truncated'] is True
    assert safe['field_violation_diagnostics'][0]['description_truncated'] is True
    assert safe['field_violation_diagnostics'][0]['category'] == 'maximum_length_exceeded'


@pytest.mark.parametrize('status,code,sdk', [(404.0, 404, 'NOT_FOUND'), (404, 404.0, 'NOT_FOUND'),
    (404, '404', 'NOT_FOUND'), (403, 403, 'PERMISSION_DENIED'), (404, 404, 'UNKNOWN')])
def test_readback_denial_or_unproved_absence_stops_after_one_get(status, code, sdk):
    session, report = Replies((status, {'error': {'code': code, 'status': sdk}})), {}
    with pytest.raises(control.Stop):
        control.read_probe(session, report)
    assert len(session.calls) == 1 and session.calls[0][0] == 'GET'
    assert 'execution_submit_attempts' not in report


def test_readback_verified_absence_never_creates():
    session, report = Replies((404, {'error': {'code': 404, 'status': 'NOT_FOUND'}})), {}
    control.read_probe(session, report)
    assert report['status'] == 'job_absent_verified' and report['job_found'] is False
    assert len(session.calls) == 1 and session.calls[0][0] == 'GET'
    assert 'fields=' in session.calls[0][1]


def test_readback_existing_execution_reads_only_exact_names_and_safe_counts():
    name = control.JOB + '/executions/existing-one'
    session = Replies((200, {'name': control.JOB, 'executionCount': 1, 'latestCreatedExecution': {'name': name},
                            'private_extra': 'synthetic-private-value'}),
                      (200, {'name': name, 'job': control.JOB, 'taskCount': 1, 'succeededCount': 0,
                             'failedCount': 1, 'conditions': [{'type': 'Completed', 'state': 'CONDITION_FAILED'}]}))
    report = {}
    control.read_probe(session, report)
    assert report['status'] == 'job_observed' and report['execution']['failed_count'] == 1
    assert [method for method, _, _ in session.calls] == ['GET', 'GET']
    assert name in session.calls[-1][1] and 'fields=' in session.calls[-1][1]
    assert 'synthetic-private' not in json.dumps(report)


def test_readback_foreign_execution_never_follows_or_lists():
    session = Replies((200, {'name': control.JOB, 'executionCount': 1,
                            'latestCreatedExecution': {'name': control.JOB + '-other/executions/foreign'}}))
    with pytest.raises(control.Stop, match='execution_identity_rejected'):
        control.read_probe(session, {})
    assert len(session.calls) == 1


def test_readback_missing_execution_count_stays_unknown():
    session, report = Replies((200, {'name': control.JOB})), {}
    control.read_probe(session, report)
    assert report['status'] == 'job_observed' and report['execution_count'] is None
    assert len(session.calls) == 1


def test_readback_requires_approval_false_and_original_identity():
    env = admitted('probe-readback')
    assert control.validate_dispatch('probe-readback', env) == 'a' * 40
    env['INPUT_APPROVE_MARKER_PROBE'] = 'true'
    with pytest.raises(control.Stop):
        control.validate_dispatch('probe-readback', env)


@pytest.mark.parametrize('code', [7, 16, 403, 999, 9.0])
def test_denied_or_unknown_terminal_operation_never_reads_logs(code):
    class InvalidOperation(Session):
        def request(self, method, url, **kwargs):
            response = super().request(method, url, **kwargs)
            if url.endswith(':run'):
                response.json()['error']['code'] = code
            return response
    session = InvalidOperation(failed=True, terminal_error=True)
    with pytest.raises(control.Stop):
        control.execute_probe(session, {}, sleep=lambda _: None)
    assert not any(x[1].endswith('entries:list') for x in session.calls)


def test_failure_receipt_cannot_export_unrecognized_reason_or_fields():
    safe = control.safe_probe_receipt({'schema_version': 1, 'probe_id': control.NONCE, 'status': 'stop',
        'reason': 'synthetic-private-reason', 'residual_generations': ['11'], 'write_outcome_unknown': True,
        'raw_private_field': 'synthetic-private-value', 'http_status': '403'})
    assert safe['reason'] == 'unrecognized_probe_reason'
    assert safe['residual_generations'] == ['11'] and safe['write_outcome_unknown'] is True
    assert 'synthetic-private' not in json.dumps(safe)
    assert 'http_status' not in safe


def test_runtime_initialization_failure_still_has_probe_identity(monkeypatch, capsys):
    class DeniedRuntime:
        def __init__(self):
            raise probe.Stop('permission_denied', 403)
    monkeypatch.setattr(probe, 'RuntimeClient', DeniedRuntime)
    monkeypatch.setattr(control.os, 'environ', {})
    assert probe.main() == 1
    safe = json.loads(capsys.readouterr().out)
    assert safe['probe_id'] == probe.NONCE and safe['status'] == 'stop'


@pytest.mark.parametrize('status,sdk', [(403, 'PERMISSION_DENIED'), (404, 'UNKNOWN'), (200, 'UNKNOWN')])
def test_no_create_after_denial_or_unknown_absence(status, sdk):
    session = Session(status=status, sdk=sdk)
    with pytest.raises(control.Stop):
        control.execute_probe(session, {}, sleep=lambda _: None)
    assert len(session.calls) == 1


def test_job_etag_conflict_does_not_resubmit_or_cleanup():
    session = Session(stale=True)
    with pytest.raises(control.Stop):
        control.execute_probe(session, {}, sleep=lambda _: None)
    assert len([x for x in session.calls if x[1].endswith(':run')]) == 1
    assert not any(x[0] == 'DELETE' for x in session.calls)


def test_workflow_isolation_and_no_private_projection_artifact():
    source = (ROOT / '.github/workflows/sync-cloud-run-env-protected.yml').read_text()
    assert source.count("inputs.notification_marker_mode == 'disabled'") == 2
    read = source.split('  notification-binding-read:\n')[1].split('  notification-marker-probe:\n')[0]
    assert 'id-token:' not in read and 'google-github-actions' not in read
    assert 'path: ${{ runner.temp }}/notification-binding.safe.json' in read
    assert 'path: ${{ runner.temp }}/notification-binding.private.json' not in read
    assert 'notification_marker_mode' in (ROOT / '.github/workflows/sync-cloud-run-env.yml').read_text()


@pytest.mark.parametrize('mode', ['binding-read', 'execute-probe', 'probe-readback', 'probe-validate-only', 'unknown-mode'])
def test_isolated_or_unknown_mode_never_starts_lifecycle(mode):
    wrapper = (ROOT / '.github/workflows/sync-cloud-run-env.yml').read_text()
    expression = wrapper.split('run-name: ${{ ', 1)[1].split(' }}', 1)[0]
    title = eval(expression.replace('inputs.notification_marker_mode', repr(mode))
                 .replace('&&', 'and').replace('||', 'or'), {'__builtins__': {}})
    lifecycle = (ROOT / '.github/workflows/runtime-target-lifecycle.yml').read_text()
    condition = lifecycle.split('    if: ', 1)[1].splitlines()[0]
    def admitted(event, display_title):
        return eval(condition.replace('github.event_name', repr(event))
                    .replace('github.event.workflow_run.display_title', repr(display_title))
                    .replace('||', 'or'), {'__builtins__': {}})
    # Both probe approval values use the same title: it is independent of
    # approval and no skipped/failed check can enable the downstream jobs.
    assert title == 'Notification check (isolated)'
    assert not admitted('workflow_run', title)
    assert not admitted('workflow_run', '')
    assert admitted('workflow_run', 'Deploy Cloud Run')
    assert admitted('schedule', '') and admitted('workflow_dispatch', '')


def test_workflow_readback_step_cannot_select_execute_command():
    source = (ROOT / '.github/workflows/sync-cloud-run-env-protected.yml').read_text()
    create = source.split('      - name: Create and execute fixed Job once; retain object and Job\n')[1].split('      - name: Read fixed nonce Job status only\n')[0]
    read = source.split('      - name: Read fixed nonce Job status only\n')[1].split('      - name: Preserve synthetic receipt only\n')[0]
    assert "if: inputs.notification_marker_mode == 'execute-probe'" in create
    assert "if: inputs.notification_marker_mode == 'probe-readback'" in read
    assert '--mode probe-readback' in read and '--mode execute-probe' not in read


class ValidationSession:
    def __init__(self, *, failure=None, existing=False, pending=False):
        self.calls = []
        self.failure, self.existing, self.pending = failure, existing, pending
    def request(self, method, url, json=None, **kwargs):
        self.calls.append((method, url, copy.deepcopy(json)))
        if method == 'GET':
            if self.existing:
                return Session.response(200, {'name': control.JOB})
            return Session.response(404, {'error': {'code': 404, 'status': 'NOT_FOUND'}})
        assert url.endswith('/jobs?jobId=' + control.JOB_ID + '&validateOnly=true')
        if self.failure:
            return Session.response(self.failure, {'error': {'code': self.failure, 'status':
                'PERMISSION_DENIED' if self.failure == 403 else 'INVALID_ARGUMENT',
                'message': 'synthetic-private-token', 'details': [{'@type': 'type.googleapis.com/google.rpc.BadRequest',
                    'fieldViolations': [{'field': 'job.template.template.containers[0].args[3]',
                                         'description': 'synthetic-private-token'}]}]}})
        return Session.response(200, {'name': control.PARENT + '/operations/synthetic-validation',
                                     'done': not self.pending, 'response': {}})


def test_validation_only_posts_same_frozen_body_once_and_never_runs():
    session, report = ValidationSession(), {}
    control.validate_probe(session, report)
    assert report['status'] == 'validated_only_job_absent'
    assert report['request_body_sha256'] == control.JOB_CREATE_BODY_SHA == \
        'fb60ad7396a7b65164341e00517133d3f5bc45972466eb86525eacee08ea4685'
    assert [x[0] for x in session.calls] == ['GET', 'POST', 'GET']
    assert session.calls[1][2] == control.job_body()
    assert 'name' not in session.calls[1][2]
    assert all(':run' not in x[1] for x in session.calls)
    assert report['execution_submissions'] == 0


@pytest.mark.parametrize('failure', [400, 403])
def test_validation_rejection_stops_without_retry_and_keeps_safe_detail(failure):
    session, report = ValidationSession(failure=failure), {}
    with pytest.raises(control.Stop):
        control.validate_probe(session, report)
    assert [x[0] for x in session.calls] == ['GET', 'POST']
    assert report['requests'][-1]['http_status'] == failure
    assert report['requests'][-1]['bad_request_fields'] == ['job.template.template.containers[0].args[3]']
    assert 'synthetic-private-token' not in json.dumps(report)


@pytest.mark.parametrize('field,state', [('', 'empty'),
    ('job.annotations["qsl.dev/probe-code-sha256"]', 'accepted'),
    ('job.template.synthetic_private_token', 'unknown_schema')])
def test_control_error_main_report_and_stdout_keep_rejected_constraint_safely(field, state, monkeypatch, tmp_path, capsys):
    from scripts import deploy_notification_only as deployment
    class LocalSession(Replies):
        closed = False
        def close(self):
            self.closed = True
    session = LocalSession((404, {'error': {'code': 404, 'status': 'NOT_FOUND'}}),
        (400, {'error': {'code': 400, 'status': 'INVALID_ARGUMENT', 'message': 'Request rejected',
            'details': [{'@type': 'type.googleapis.com/google.rpc.BadRequest', 'fieldViolations': [
                {'field': field, 'description': 'Name length exceeds maximum: synthetic_private_value'}]}]}}))
    monkeypatch.setattr(deployment, 'authorized_session', lambda: session)
    for key, value in admitted('probe-validate-only').items():
        monkeypatch.setenv(key, value)
    path = tmp_path / 'safe.json'
    monkeypatch.setattr('sys.argv', ['check', '--mode', 'probe-validate-only', '--report', str(path)])
    assert control.main() == 1
    printed = capsys.readouterr().out
    saved = json.loads(path.read_text())
    assert json.loads(printed) == saved
    row = saved['requests'][-1]['field_violation_diagnostics'][0]
    assert row['field_state'] == state and row['category'] == 'maximum_length_exceeded'
    assert 'synthetic_private' not in printed + path.read_text()
    assert [x[0] for x in session.calls] == ['GET', 'POST'] and session.closed
    assert session.calls[1][2]['json'] == control.job_body()
    assert ':run' not in session.calls[1][1]


def test_validation_existing_job_or_changed_body_or_pending_stops(monkeypatch):
    existing = ValidationSession(existing=True)
    with pytest.raises(control.Stop, match='job_already_exists'):
        control.validate_probe(existing, {})
    assert [x[0] for x in existing.calls] == ['GET']
    pending = ValidationSession(pending=True)
    with pytest.raises(control.Stop, match='validation_completion_unknown'):
        control.validate_probe(pending, {})
    assert [x[0] for x in pending.calls] == ['GET', 'POST']
    monkeypatch.setattr(control, 'job_body', lambda: {'changed': True})
    changed = ValidationSession()
    with pytest.raises(control.Stop, match='job_create_body_changed'):
        control.validate_probe(changed, {})
    assert [x[0] for x in changed.calls] == ['GET']


def test_validation_dispatch_requires_original_identity_and_no_execution_approval():
    env = admitted('probe-validate-only')
    assert control.validate_dispatch('probe-validate-only', env) == 'a' * 40
    env['INPUT_APPROVE_MARKER_PROBE'] = 'true'
    with pytest.raises(control.Stop):
        control.validate_dispatch('probe-validate-only', env)
    env = admitted('probe-validate-only')
    env['GCP_RUNTIME_SERVICE_ACCOUNT'] = 'synthetic-other'
    with pytest.raises(control.Stop):
        control.validate_dispatch('probe-validate-only', env)


def test_workflow_validation_step_is_independent_of_execute_and_readback():
    source = (ROOT / '.github/workflows/sync-cloud-run-env-protected.yml').read_text()
    validate = source.split('      - name: Validate original fixed Job request without creating resources\n')[1].split('      - name: Preserve synthetic receipt only\n')[0]
    assert "if: inputs.notification_marker_mode == 'probe-validate-only'" in validate
    assert '--mode probe-validate-only' in validate
    assert '--mode execute-probe' not in validate and '--mode probe-readback' not in validate
