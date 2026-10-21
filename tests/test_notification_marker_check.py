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
            return self.response(200, {**self.body, 'etag': 'synthetic-etag'})
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


@pytest.mark.parametrize('mode', ['binding-read', 'execute-probe', 'probe-readback', 'unknown-mode'])
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
