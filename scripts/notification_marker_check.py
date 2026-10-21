"""Fixed IBKR business-binding read or one retained synthetic marker Job."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time

PROJECT = 'interactivebrokersquant'
REGION = 'us-central1'
DEPLOYER = f'ibkr-platform-deploy@{PROJECT}.iam.gserviceaccount.com'
RUNTIME = f'ibkr-platform-runtime@{PROJECT}.iam.gserviceaccount.com'
TARGET_HASH = 'c9912a3bf99c97605a0791e5ecbcc1d344444daf4f64b2c699d717b2d8b8c8ac'
IMAGE = f'{REGION}-docker.pkg.dev/{PROJECT}/cloud-run-source-deploy/interactivebrokersplatform/cloud-run@sha256:c99569018e551cb44631c97d0af59f0eb1035a45a4cba688529093be5718134b'
JOB_ID = 'ibkr-notification-marker-check-20261021-3c5b1830'
PARENT = f'projects/{PROJECT}/locations/{REGION}'
JOB = f'{PARENT}/jobs/{JOB_ID}'
API = 'https://run.googleapis.com/v2/'
NONCE = '3c5b1830-9184-4966-9232-f8f1e290fd18'
PROBE_SHA = 'beeaed150492e7f6f8887276652f9f62856a4aae0a995c017305075c9dde59f1'
IDENTITY = ('platform_id', 'opaque_account_uid', 'target_id', 'strategy_profile')
SECRET_NAMES = {'token', 'password', 'private_key', 'access_token', 'refresh_token', 'api_key', 'secret', 'client_secret', 'credentials', 'authorization_header'}


class Stop(RuntimeError):
    pass


def require(value, reason):
    if not value:
        raise Stop(reason)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate_dispatch(mode, env):
    require(mode in {'binding-read', 'execute-probe'} and env.get('INPUT_NOTIFICATION_MARKER_MODE') == mode, 'mode_not_admitted')
    for key, value in {'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_REF': 'refs/heads/main',
                       'GITHUB_RUN_ATTEMPT': '1', 'INPUT_TARGET': 'configured', 'INPUT_CONFIGURED_SERVICE': '',
                       'INPUT_NOTIFICATION_ONLY': 'false', 'INPUT_NOTIFICATION_READBACK_ONLY': 'false',
                       'INPUT_DEPLOY_IMAGE': 'false', 'INPUT_SYNC_ENV': 'false',
                       'INPUT_APPROVE_TRAFFIC_SHIFT': 'false', 'INPUT_APPROVE_SCHEDULER_SYNC': 'false'}.items():
        require(env.get(key, '') == value, 'dispatch_boundary_rejected')
    source = env.get('GITHUB_SHA', '')
    require(re.fullmatch('[a-f0-9]{40}', source) and env.get('INPUT_SOURCE_COMMIT') == source, 'exact_controller_source_required')
    if mode == 'execute-probe':
        for key, value in {'INPUT_APPROVE_MARKER_PROBE': 'true', 'ENABLE_GITHUB_CLOUD_RUN_DEPLOY': 'true',
                           'GCP_PROJECT_ID': PROJECT, 'GCP_DEPLOY_SERVICE_ACCOUNT': DEPLOYER,
                           'GCP_RUNTIME_SERVICE_ACCOUNT': RUNTIME}.items():
            require(env.get(key) == value, 'probe_authorization_or_identity_rejected')
    else:
        require(env.get('INPUT_APPROVE_MARKER_PROBE') == 'false', 'read_mode_has_no_probe_approval')
    return source


def sensitive(value):
    if isinstance(value, dict):
        return any(str(k).lower() in SECRET_NAMES or str(k).lower().endswith(('_token', '_password', '_private_key', '_secret', '_credentials'))
                   or (k != 'live_continuity' and sensitive(v)) for k, v in value.items())
    if isinstance(value, list):
        return any(sensitive(x) for x in value)
    return isinstance(value, str) and ('-----BEGIN ' in value or value.startswith('Bearer ') or bool(re.search(r'://[^/\s]+@', value)))


def parse(value):
    require(isinstance(value, str) and len(value) <= 1048576, 'business_configuration_size_invalid')
    try:
        return json.loads(value)
    except (ValueError, TypeError, RecursionError):
        raise Stop('business_configuration_invalid') from None


def target(value):
    require(value is not None and value != '', 'runtime_target_missing')
    value = parse(value) if isinstance(value, str) else value
    require(isinstance(value, dict), 'runtime_target_not_object')
    require(not sensitive(value), 'sensitive_target_not_extracted')
    selected = {k: v for k, v in value.items() if k != 'live_continuity'}
    encoded = json.dumps(selected, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()
    return value, hashlib.sha256(encoded).hexdigest()


def binding_projection(env):
    raw = env.get('DIGEST_TARGET_BINDINGS_JSON', '').strip()
    rows = parse(raw) if raw else []
    require(isinstance(rows, list) and len(rows) <= 100, 'business_bindings_shape_invalid')
    matches = []
    for row in rows:
        require(isinstance(row, dict) and not sensitive(row), 'sensitive_binding_not_extracted')
        bound, fingerprint = target(row.get('RUNTIME_TARGET_JSON'))
        if fingerprint == TARGET_HASH:
            matches.append((row, bound))
    if matches:
        require(len(matches) == 1, 'exact_binding_not_unique')
        selected, bound = matches[0]
        origin = 'matching_binding'
    else:
        bound, fingerprint = target(env.get('RUNTIME_TARGET_JSON', ''))
        require(fingerprint == TARGET_HASH, 'global_target_not_selected')
        selected, origin = env, 'exact_global_target'
    require(bound.get('strategy_profile') == 'tqqq_growth_income' and bound.get('execution_mode') == 'live'
            and bound.get('dry_run_only') is False, 'fixed_profile_mode_mismatch')
    uid = selected.get('DIGEST_OPAQUE_ACCOUNT_UID') or selected.get('IBKR_DIGEST_OPAQUE_ACCOUNT_UID')
    tid = selected.get('DIGEST_TARGET_ID') or selected.get('IBKR_DIGEST_TARGET_ID')
    slot = selected.get('DIGEST_STORE_TARGET')
    if origin == 'matching_binding' and not all((uid, tid, slot)):
        # Original globals are usable only when their complete target is the same
        # sealed target. A matching strategy name alone cannot lend any field.
        _, global_fingerprint = target(env.get('RUNTIME_TARGET_JSON', ''))
        require(global_fingerprint == TARGET_HASH, 'global_defaults_not_same_target')
        uid = uid or env.get('DIGEST_OPAQUE_ACCOUNT_UID') or env.get('IBKR_DIGEST_OPAQUE_ACCOUNT_UID')
        tid = tid or env.get('DIGEST_TARGET_ID') or env.get('IBKR_DIGEST_TARGET_ID')
        slot = slot or env.get('DIGEST_STORE_TARGET')
        origin = 'matching_binding_with_exact_global_defaults'
    require(all(isinstance(x, str) and x.strip() and not sensitive(x) for x in (uid, tid, slot)), 'selected_binding_fields_missing_or_sensitive')
    require(re.fullmatch('[a-z0-9][a-z0-9_-]{0,40}', slot), 'selected_slot_invalid')
    row = dict(zip(IDENTITY, ('ibkr', uid, tid, bound['strategy_profile'])))
    tuple_hash = hashlib.sha256(json.dumps([row[k] for k in IDENTITY], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return row, {'status': 'binding_verified', 'source': origin, 'target_fingerprint_matches': True,
                 'exact_binding_count': 1, 'four_field_projection_sha256': digest(row),
                 'identity_tuple_sha256': tuple_hash, 'slot_sha256': hashlib.sha256(slot.encode()).hexdigest(),
                 'website_alias_match': 'not_tested', 'private_selector_exported': False, 'expected_list_written': False}


def job_body():
    code = Path(__file__).with_name('notification_marker_probe.py').read_text()
    require(hashlib.sha256(code.encode()).hexdigest() == PROBE_SHA, 'reviewed_probe_code_hash_mismatch')
    return {'name': JOB, 'annotations': {'qsl.dev/probe-code-sha256': PROBE_SHA},
            'template': {'taskCount': 1, 'parallelism': 1, 'template': {'serviceAccount': RUNTIME, 'maxRetries': 0,
                'timeout': '60s', 'containers': [{'image': IMAGE, 'command': ['/usr/local/bin/python3.12'],
                    'args': ['-I', '-S', '-c', code], 'resources': {'limits': {'cpu': '1', 'memory': '512Mi'}}}]}}}


def verify_job(value, expected):
    require(value.get('name') == JOB and value.get('etag'), 'job_identity_or_etag_missing')
    template = value.get('template', {})
    task = template.get('template', {})
    original = expected['template']['template']
    require(template.get('taskCount') == 1 and template.get('parallelism') == 1, 'job_task_count_changed')
    require(task.get('serviceAccount') == RUNTIME and task.get('maxRetries', 0) == 0 and task.get('timeout') == '60s', 'job_identity_or_limits_changed')
    require(not task.get('volumes') and not task.get('vpcAccess'), 'job_has_unapproved_mount_or_network')
    containers = task.get('containers', [])
    require(len(containers) == 1, 'job_container_count_changed')
    got, want = containers[0], original['containers'][0]
    require(all(got.get(k) == want[k] for k in ('image', 'command', 'args')), 'job_image_or_entrypoint_changed')
    require(not got.get('env') and not got.get('volumeMounts'), 'job_has_unapproved_environment')
    require(got.get('resources', {}).get('limits') == want['resources']['limits'], 'job_resources_changed')
    require(value.get('annotations', {}).get('qsl.dev/probe-code-sha256') == PROBE_SHA, 'job_probe_annotation_changed')
    return value['etag']


def execute_probe(session, report, *, sleep=time.sleep, clock=time.monotonic):
    deadline = clock() + 180
    def request(method, url, *, body=None, missing=False, phase):
        require(clock() < deadline, 'control_deadline_exceeded_no_retry')
        entry = {'phase': phase, 'http_status': None, 'sdk_status': 'UNKNOWN', 'sdk_code': None}
        report.setdefault('requests', []).append(entry)
        try:
            response = session.request(method, url, json=body, timeout=min(10, deadline - clock()), allow_redirects=False)
        except Exception:
            raise Stop('control_transport_unknown_no_retry') from None
        entry['http_status'] = response.status_code
        try:
            value = response.json()
        except Exception:
            raise Stop('control_payload_unknown_no_retry') from None
        error = value.get('error', {}) if isinstance(value, dict) else {}
        if error:
            entry['sdk_code'] = error.get('code') if type(error.get('code')) is int else None
            state = error.get('status')
            if state in {'NOT_FOUND', 'PERMISSION_DENIED', 'UNAUTHENTICATED', 'FAILED_PRECONDITION', 'ABORTED', 'UNAVAILABLE'}:
                entry['sdk_status'] = state
            if missing and response.status_code == 404 and error.get('code') == 404 and state == 'NOT_FOUND':
                return None
        require(response.status_code == 200 and isinstance(value, dict) and not error, 'control_request_failed_no_retry')
        entry['sdk_status'] = 'OK'
        return value
    def operation(value, phase):
        name = value.get('name', '')
        require(re.fullmatch(re.escape(PARENT) + r'/operations/[a-zA-Z0-9_-]+', name), 'operation_identity_rejected')
        while not value.get('done'):
            sleep(2)
            value = request('GET', API + name, phase=phase)
        require(not value.get('error') and isinstance(value.get('response'), dict), 'operation_result_failed_no_retry')
        return value['response']
    require(request('GET', API + JOB, missing=True, phase='job_absence') is None, 'job_already_exists_no_reuse')
    expected = job_body()
    report['job_create_attempts'] = 1
    created = request('POST', API + PARENT + '/jobs?jobId=' + JOB_ID, body=expected, phase='job_create')
    operation(created, 'job_create_operation')
    job = request('GET', API + JOB, phase='job_readback')
    etag = verify_job(job, expected)
    report.update(job_template_verified=True, job_name=JOB, probe_code_sha256=PROBE_SHA, job_retained=True)
    report['execution_submit_attempts'] = 1
    execution = operation(request('POST', API + JOB + ':run', body={'etag': etag}, phase='execute_once'), 'execution_operation')
    name = execution.get('name', '')
    require(re.fullmatch(re.escape(JOB) + r'/executions/[a-zA-Z0-9_-]+', name) and execution.get('job') == JOB, 'execution_identity_rejected')
    report['execution_name'] = name
    require(execution.get('taskCount') == 1 and execution.get('succeededCount') == 1
            and execution.get('failedCount', 0) == 0 and execution.get('cancelledCount', 0) == 0, 'execution_not_successful')
    require(any(x.get('type') == 'Completed' and x.get('state') == 'CONDITION_SUCCEEDED' for x in execution.get('conditions', [])), 'execution_completion_unproved')
    suffix = name.rsplit('/', 1)[1]
    entries = request('POST', 'https://logging.googleapis.com/v2/entries:list', body={
        'resourceNames': ['projects/' + PROJECT], 'pageSize': 10, 'orderBy': 'timestamp desc',
        'filter': f'resource.type="cloud_run_job" AND resource.labels.job_name="{JOB_ID}" AND labels."run.googleapis.com/execution_name"="{suffix}"'}, phase='anonymous_execution_receipt')
    certificates = []
    for entry in entries.get('entries', []):
        try:
            value = json.loads(entry.get('textPayload', ''))
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict) and value.get('probe_id') == NONCE:
            certificates.append(value)
    require(len(certificates) == 1, 'anonymous_receipt_missing_or_ambiguous_no_retry')
    proof = certificates[0]
    require(proof.get('status') == 'pass_retained' and proof.get('CAS_checks_passed') is True
            and proof.get('runtime_principal_matches') is True and proof.get('cleanup_attempted') is False
            and proof.get('write_outcome_unknown') is False and proof.get('production_marker_touched') is False, 'probe_receipt_rejected')
    gens = proof.get('observed_generations')
    require(isinstance(gens, list) and len(gens) == 2 and len(set(gens)) == 2
            and all(isinstance(x, str) and x.isdecimal() and int(x) > 0 for x in gens), 'observed_generations_missing')
    require(proof.get('retained_live_generation') == gens[-1], 'retained_live_generation_missing')
    report.update(status='pass_retained', observed_generations=gens, retained_live_generation=gens[-1], cleanup_attempted=False,
                  runtime_principal_matches=True, actual_delivery='not_attempted', production_mutations=0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['binding-read', 'execute-probe'], required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--private-out', type=Path)
    args = parser.parse_args()
    report = {'schema_version': 1, 'status': 'stop', 'cleanup_attempted': False}
    try:
        report['controller_source'] = validate_dispatch(args.mode, os.environ)
        if args.mode == 'binding-read':
            report['configuration_present'] = {key: bool(os.environ.get(key, '').strip()) for key in (
                'RUNTIME_TARGET_JSON', 'DIGEST_TARGET_BINDINGS_JSON', 'DIGEST_OPAQUE_ACCOUNT_UID',
                'DIGEST_TARGET_ID', 'IBKR_DIGEST_OPAQUE_ACCOUNT_UID', 'IBKR_DIGEST_TARGET_ID', 'DIGEST_STORE_TARGET')}
            row, proof = binding_projection(os.environ)
            require(args.private_out is not None, 'private_projection_path_required')
            descriptor = os.open(args.private_out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w') as file:
                json.dump(row, file, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            report.update(proof)
        else:
            from scripts.deploy_notification_only import authorized_session, ReleaseError
            try:
                session = authorized_session()
            except ReleaseError as error:
                raise Stop(str(error)) from None
            try:
                execute_probe(session, report)
            finally:
                session.close()
    except Stop as error:
        report.update(status='stop', reason=str(error))
    except Exception:
        report.update(status='stop', reason='local_unknown_no_retry')
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, sort_keys=True, indent=2) + '\n')
    print(json.dumps(report, sort_keys=True))
    return 0 if report['status'] in {'binding_verified', 'pass_retained'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
