"""Fixed IBKR business-binding read or one retained synthetic marker Job."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import quote

from scripts.notification_marker_probe import BUCKET as MARKER_BUCKET, OBJECT as MARKER_OBJECT

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
PROBE_SHA = 'ce49359d6d6275a49a74bfbee4e0acccb61ee3f2ade7f796419eb29ed16c9c97'
JOB_CREATE_BODY_SHA = 'fb60ad7396a7b65164341e00517133d3f5bc45972466eb86525eacee08ea4685'
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
    require(mode in {'binding-read', 'execute-probe', 'probe-readback', 'probe-validate-only'} and env.get('INPUT_NOTIFICATION_MARKER_MODE') == mode, 'mode_not_admitted')
    for key, value in {'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_REF': 'refs/heads/main',
                       'GITHUB_RUN_ATTEMPT': '1', 'INPUT_TARGET': 'configured', 'INPUT_CONFIGURED_SERVICE': '',
                       'INPUT_NOTIFICATION_ONLY': 'false', 'INPUT_NOTIFICATION_READBACK_ONLY': 'false',
                       'INPUT_DEPLOY_IMAGE': 'false', 'INPUT_SYNC_ENV': 'false',
                       'INPUT_APPROVE_TRAFFIC_SHIFT': 'false', 'INPUT_APPROVE_SCHEDULER_SYNC': 'false'}.items():
        require(env.get(key, '') == value, 'dispatch_boundary_rejected')
    source = env.get('GITHUB_SHA', '')
    require(re.fullmatch('[a-f0-9]{40}', source) and env.get('INPUT_SOURCE_COMMIT') == source, 'exact_controller_source_required')
    if mode in {'execute-probe', 'probe-readback', 'probe-validate-only'}:
        for key, value in {'INPUT_APPROVE_MARKER_PROBE': 'true' if mode == 'execute-probe' else 'false', 'ENABLE_GITHUB_CLOUD_RUN_DEPLOY': 'true',
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
    return {'annotations': {'qsl.dev/probe-code-sha256': PROBE_SHA},
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


def safe_probe_receipt(value):
    require(type(value.get('schema_version')) is int and value['schema_version'] == 1
            and value.get('probe_id') == NONCE and isinstance(value.get('status'), str)
            and value['status'] in {'stop', 'pass_retained'}, 'probe_receipt_shape_rejected')
    safe = {key: value[key] for key in ('schema_version', 'probe_id', 'status')}
    reasons = {'qualified_retained_without_cleanup', 'generation_unknown', 'generation_mismatch', 'own_payload_mismatch',
               'precondition_unexpected_success', 'object_already_exists', 'generation_not_advanced', 'live_generation_changed',
               'local_unknown', 'runtime_principal_mismatch', 'runtime_token_unavailable', 'endpoint_rejected',
               'request_budget_exhausted', 'response_size_unknown', 'permission_denied', 'not_found', 'precondition_failed',
               'http_status_unknown', 'transport_unknown', 'object_or_method_rejected', 'response_json_unknown'}
    if 'reason' in value:
        safe['reason'] = value['reason'] if isinstance(value['reason'], str) and value['reason'] in reasons else 'unrecognized_probe_reason'
    for key in ('CAS_checks_passed', 'runtime_principal_matches', 'cleanup_attempted', 'write_outcome_unknown', 'production_marker_touched'):
        if type(value.get(key)) is bool:
            safe[key] = value[key]
    for key in ('observed_generations', 'residual_generations'):
        generations = value.get(key)
        if isinstance(generations, list) and len(generations) <= 4 and all(
                isinstance(x, str) and re.fullmatch('[1-9][0-9]{0,31}', x) for x in generations):
            safe[key] = generations
    retained = value.get('retained_live_generation')
    if isinstance(retained, str) and re.fullmatch('[1-9][0-9]{0,31}', retained):
        safe['retained_live_generation'] = retained
    if value.get('old_generation_retention') == 'unknown':
        safe['old_generation_retention'] = 'unknown'
    if type(value.get('http_status')) is int and 100 <= value['http_status'] <= 599:
        safe['http_status'] = value['http_status']
    return safe


def control_error_projection(error):
    states = {'CANCELLED', 'UNKNOWN', 'INVALID_ARGUMENT', 'DEADLINE_EXCEEDED', 'NOT_FOUND', 'ALREADY_EXISTS',
              'PERMISSION_DENIED', 'UNAUTHENTICATED', 'RESOURCE_EXHAUSTED', 'FAILED_PRECONDITION', 'ABORTED',
              'OUT_OF_RANGE', 'UNIMPLEMENTED', 'INTERNAL', 'UNAVAILABLE', 'DATA_LOSS'}
    code, state = error.get('code'), error.get('status')
    safe = {'sdk_code': code if type(code) is int and 0 <= code <= 599 else None,
            'sdk_status': state if isinstance(state, str) and state in states else 'UNKNOWN'}
    def category(text):
        # Classify in memory; never export the server's request-value-bearing text.
        text = text[:4096].lower() if isinstance(text, str) else ''
        if re.search(r'length|characters|bytes', text) and re.search(r'at most|maximum|too long|exceeds|no more than', text):
            return 'maximum_length_exceeded'
        if re.search(r'unsupported|not supported|unknown field|unrecognized field', text):
            return 'unsupported_field'
        if re.search(r'required|must be provided|missing field', text):
            return 'required_field'
        if re.search(r'out of range|must be between', text):
            return 'value_out_of_range'
        if re.search(r'invalid format|must match|malformed', text):
            return 'invalid_format'
        if re.search(r'invalid value|not a valid', text):
            return 'invalid_value'
        return safe['sdk_status'].lower()
    safe['error_category'] = category(error.get('message'))
    safe['message_truncated'] = isinstance(error.get('message'), str) and len(error['message']) > 4096
    # Only this fixed request's known schema and static map keys may be public.
    schema = {'job', 'job.name', 'job.annotations', 'job.labels',
              'job.annotations[qsl.dev/probe-code-sha256]', 'job.template',
              'job.template.annotations', 'job.template.labels', 'job.template.taskCount',
              'job.template.parallelism', 'job.template.template',
              'job.template.template.serviceAccount', 'job.template.template.maxRetries',
              'job.template.template.timeout', 'job.template.template.containers',
              'job.template.template.containers[]', 'job.template.template.containers[].image',
              'job.template.template.containers[].command', 'job.template.template.containers[].command[]',
              'job.template.template.containers[].args', 'job.template.template.containers[].args[]',
              'job.template.template.containers[].resources', 'job.template.template.containers[].resources.limits',
              'job.template.template.containers[].resources.limits.cpu',
              'job.template.template.containers[].resources.limits.memory', 'parent', 'jobId'}
    aliases = {'task_count': 'taskCount', 'service_account': 'serviceAccount',
               'max_retries': 'maxRetries', 'job_id': 'jobId'}
    key = r'[A-Za-z_][A-Za-z0-9_]{0,63}'
    path_pattern = key + rf'(?:\.{key}|\[(?:[0-9]{{1,6}}|{key})\])*'
    absent = object()
    def json_type(value):
        if value is absent:
            return 'missing'
        return {type(None): 'null', bool: 'boolean', int: 'integer', float: 'number',
                str: 'string', list: 'array', dict: 'object'}.get(type(value), 'other')
    def public_path(value):
        path = re.sub(key, lambda m: aliases.get(m[0], m[0]), value)
        path = path if path.startswith('job.') or path in {'job', 'parent', 'jobId'} else 'job.' + path
        annotation = 'qsl.dev/probe-code-sha256'
        if path in {'job.annotations.' + annotation, 'job.annotations[' + annotation + ']',
                    'job.annotations["' + annotation + '"]', "job.annotations['" + annotation + "']"}:
            path = 'job.annotations[' + annotation + ']'
        for name in ('cpu', 'memory'):
            for quoted in (name, '"' + name + '"', "'" + name + "'"):
                path = path.replace('.limits[' + quoted + ']', '.limits.' + name)
        def normalize(p):
            return re.sub(r'\[[0-9]{1,6}\]', '[]', p)
        prefixes = [path[:m.start()] for m in re.finditer(r'[.\[]', path)] + [path]
        parents = [p for p in prefixes if normalize(p) in schema]
        parent = max(parents, key=len) if parents else None
        if normalize(path) in schema:
            return path, parent, 'accepted'
        state = 'unknown_schema' if re.fullmatch(path_pattern, path) else 'syntax_rejected'
        return None, parent, state
    fields, categorized, diagnostics, unlisted = set(), set(), [], 0
    details = error.get('details', absent)
    scan = {'details_state': json_type(details), 'details_total': len(details) if isinstance(details, list) else None,
            'details_seen': 0, 'details_truncated_count': max(0, len(details) - 10) if isinstance(details, list) else 0,
            'violations_total_in_seen_details': 0, 'violations_seen': 0,
            'violations_truncated_in_seen_details': 0, 'detail_shapes': []}
    for di, detail in enumerate(details[:10] if isinstance(details, list) else []):
        scan['details_seen'] += 1
        if not isinstance(detail, dict) or detail.get('@type') != 'type.googleapis.com/google.rpc.BadRequest':
            scan['detail_shapes'].append({'detail_index': di, 'state': 'non_object' if not isinstance(detail, dict)
                                         else 'other_detail_type', 'type': json_type(detail)})
            continue
        violations = detail.get('fieldViolations', absent)
        if not isinstance(violations, list):
            scan['detail_shapes'].append({'detail_index': di, 'state': 'field_violations_not_array',
                                         'type': json_type(violations)})
            continue
        scan['violations_total_in_seen_details'] += len(violations)
        scan['violations_truncated_in_seen_details'] += max(0, len(violations) - 32)
        for vi, violation in enumerate(violations[:32]):
            scan['violations_seen'] += 1
            field = violation.get('field', absent) if isinstance(violation, dict) else absent
            description = violation.get('description') if isinstance(violation, dict) else None
            row = {'detail_index': di, 'violation_index': vi, 'category': category(description),
                   'violation_type': json_type(violation),
                   'field_type': json_type(field), 'field_length': len(field) if isinstance(field, str) else None,
                   'description_truncated': isinstance(description, str) and len(description) > 4096}
            path, parent = None, None
            if not isinstance(violation, dict):
                state = 'violation_not_object'
            elif field is absent or field is None:
                state = json_type(field)
            elif not isinstance(field, str):
                state = 'non_string'
            elif not field:
                state = 'empty'
            elif len(field) > 512:
                state = 'too_long'
            else:
                path, parent, state = public_path(field)
            row.update(field_state=state, known_parent=parent)
            if path is not None:
                row['field'] = path
                fields.add(path)
                categorized.add((path, row['category']))
            else:
                unlisted += 1
            diagnostics.append(row)
    scan['incomplete'] = bool(scan['details_truncated_count'] or scan['violations_truncated_in_seen_details']
                              or scan['detail_shapes'] or json_type(details) not in {'array', 'missing'})
    safe['bad_request_scan'] = scan
    if diagnostics:
        safe.update(bad_request_fields=sorted(fields), unlisted_field_violations=unlisted,
                    bad_request_violations=[{'field': field, 'category': kind} for field, kind in sorted(categorized)],
                    field_violation_diagnostics=diagnostics)
    return safe


def control_request(session, report, method, url, *, body=None, missing=False, operation_result=False,
                    phase, deadline=None, clock=time.monotonic):
    deadline = clock() + 30 if deadline is None else deadline
    require(clock() < deadline, 'control_deadline_exceeded_no_retry')
    entry = {'phase': phase, 'http_status': None, 'sdk_status': 'UNKNOWN', 'sdk_code': None}
    report.setdefault('requests', []).append(entry)
    try:
        response = session.request(method, url, json=body, timeout=min(10, deadline - clock()), allow_redirects=False)
    except Exception:
        raise Stop('control_transport_unknown_no_retry') from None
    require(type(response.status_code) is int and 100 <= response.status_code <= 599, 'control_HTTP_status_unknown_no_retry')
    entry['http_status'] = response.status_code
    try:
        value = response.json()
    except Exception:
        raise Stop('control_payload_unknown_no_retry') from None
    require(isinstance(value, dict), 'control_payload_unknown_no_retry')
    error = value.get('error', {})
    require(isinstance(error, dict), 'control_payload_unknown_no_retry')
    if error:
        entry.update(control_error_projection(error))
        if missing and response.status_code == 404 and entry['sdk_code'] == 404 and entry['sdk_status'] == 'NOT_FOUND':
            return None
        # Only a bound completed run's failure metadata can be considered later.
        if operation_result and response.status_code == 200 and value.get('done') is True:
            return value
    require(response.status_code == 200 and not error, 'control_request_failed_no_retry')
    entry['sdk_status'] = 'OK'
    return value


def read_probe(session, report):
    # Server-side partial responses exclude environment, arguments, secrets,
    # free-text condition messages and arbitrary annotations.
    fields = 'name,executionCount,latestCreatedExecution(name)'
    job = control_request(session, report, 'GET', API + JOB + '?fields=' + fields,
                          missing=True, phase='job_status_read_only')
    report.update(job_found=job is not None, actual_delivery='not_attempted', production_mutations=0)
    if job is None:
        report['status'] = 'job_absent_verified'
        return
    require(job.get('name') == JOB, 'job_identity_rejected')
    count = job.get('executionCount')
    require(count is None or type(count) is int and count >= 0, 'execution_count_unknown')
    report.update(job_name=JOB, execution_count=count)
    latest = job.get('latestCreatedExecution') or {}
    require(isinstance(latest, dict), 'execution_identity_rejected')
    residual = not latest and count == 1
    if residual:
        # Jobs.get can report an execution count without the latest pointer.
        # List only this exact Job; never enumerate a project or another Job.
        fields = 'executions(name,job),nextPageToken'
        listed = control_request(session, report, 'GET', API + JOB + '/executions?pageSize=2&fields=' + fields,
                                 phase='exact_job_executions_read_only')
        rows = listed.get('executions')
        require(isinstance(rows, list) and len(rows) == 1 and not listed.get('nextPageToken')
                and isinstance(rows[0], dict), 'execution_list_not_unique')
        latest = rows[0]
        report['execution_source'] = 'exact_job_list'
    if latest or count:
        name = latest.get('name', '')
        require(isinstance(name, str) and re.fullmatch(re.escape(JOB) + r'/executions/[a-zA-Z0-9_-]+', name), 'execution_identity_rejected')
        fields = 'name,job,taskCount,succeededCount,failedCount,cancelledCount,conditions(type,state,reason,executionReason)'
        execution = control_request(session, report, 'GET', API + name + '?fields=' + fields, phase='existing_execution_read_only')
        require(execution.get('name') == name and execution.get('job') == JOB, 'execution_identity_rejected')
        observed = {'name': name}
        for source, key in (('taskCount', 'task_count'), ('succeededCount', 'succeeded_count'),
                            ('failedCount', 'failed_count'), ('cancelledCount', 'cancelled_count')):
            value = execution.get(source)
            observed[key] = value if type(value) is int and value >= 0 else None
        conditions = execution.get('conditions', [])
        require(isinstance(conditions, list), 'execution_conditions_unknown')
        observed['completed_state'] = next((x.get('state') for x in conditions
            if isinstance(x, dict) and x.get('type') == 'Completed'
            and x.get('state') in {'CONDITION_SUCCEEDED', 'CONDITION_FAILED', 'CONDITION_PENDING'}), 'UNKNOWN')
        completed = next((x for x in conditions
            if isinstance(x, dict) and x.get('type') == 'Completed'), {})
        for source in ('reason', 'executionReason'):
            value = completed.get(source)
            if isinstance(value, str) and re.fullmatch('[A-Z_]{1,64}', value):
                observed[source] = value
        report['execution'] = observed
        if residual:
            read_fixed_object_metadata(session, report)
            read_fixed_execution_receipt(session, report, name)
    report['status'] = 'job_observed'


def read_fixed_object_metadata(session, report):
    """Read metadata only for the approved nonce object; never fetch its body."""
    url = ('https://storage.googleapis.com/storage/v1/b/' + MARKER_BUCKET + '/o/'
           + quote(MARKER_OBJECT, safe='') + '?fields=name,generation,size')
    entry = {'phase': 'fixed_synthetic_object_metadata', 'http_status': None}
    report.setdefault('requests', []).append(entry)
    try:
        response = session.request('GET', url, timeout=10, allow_redirects=False)
    except Exception:
        raise Stop('object_metadata_transport_unknown_no_retry') from None
    status = response.status_code
    require(type(status) is int and 100 <= status <= 599, 'object_metadata_http_unknown_no_retry')
    entry['http_status'] = status
    try:
        value = response.json()
    except Exception:
        raise Stop('object_metadata_payload_unknown_no_retry') from None
    require(isinstance(value, dict), 'object_metadata_payload_unknown_no_retry')
    if (status == 404 and isinstance(value.get('error'), dict)
            and type(value['error'].get('code')) is int and value['error']['code'] == 404):
        report['synthetic_object'] = {'found': False}
        return
    if status == 403:
        entry['error_category'] = 'permission_denied'
        raise Stop('object_metadata_permission_denied')
    require(status == 200 and value.get('name') == MARKER_OBJECT, 'object_metadata_unbound_no_retry')
    generation = value.get('generation')
    size = value.get('size')
    require(isinstance(generation, str) and re.fullmatch('[1-9][0-9]{0,31}', generation)
            and isinstance(size, str) and size.isdecimal(), 'object_metadata_shape_unknown_no_retry')
    report['synthetic_object'] = {'found': True, 'generation': generation, 'size_bytes': int(size)}


def read_fixed_execution_receipt(session, report, name):
    suffix = name.rsplit('/', 1)[1]
    entries = control_request(session, report, 'POST', 'https://logging.googleapis.com/v2/entries:list', body={
        'resourceNames': ['projects/' + PROJECT], 'pageSize': 10, 'orderBy': 'timestamp desc',
        'filter': f'resource.type="cloud_run_job" AND resource.labels.location="{REGION}" AND resource.labels.job_name="{JOB_ID}" AND labels."run.googleapis.com/execution_name"="{suffix}"'},
        phase='exact_execution_safe_log_read_only')
    require(isinstance(entries.get('entries', []), list) and not entries.get('nextPageToken'),
            'safe_probe_receipt_page_incomplete')
    certificates = []
    for entry in entries.get('entries', []):
        value = entry.get('jsonPayload')
        if not isinstance(value, dict):
            try:
                value = json.loads(entry.get('textPayload', ''))
            except (ValueError, TypeError):
                continue
        if isinstance(value, dict) and value.get('probe_id') == NONCE:
            certificates.append(value)
    require(len(certificates) <= 1, 'safe_probe_receipt_ambiguous')
    report['safe_probe_receipt_found'] = bool(certificates)
    if certificates:
        report['probe_receipt'] = safe_probe_receipt(certificates[0])


def validate_probe(session, report, *, sleep=time.sleep, clock=time.monotonic):
    """Validate the fixed body and read its exact operation without persisting a Job."""
    deadline = clock() + 90
    def request(method, url, **kwargs):
        return control_request(session, report, method, url, deadline=deadline, clock=clock, **kwargs)
    url = API + JOB + '?fields=name'
    require(request('GET', url, missing=True, phase='validation_job_absence') is None,
            'job_already_exists_no_validation')
    expected = job_body()
    require(digest(expected) == JOB_CREATE_BODY_SHA, 'job_create_body_changed')
    report.update(request_body_sha256=JOB_CREATE_BODY_SHA, validation_attempts=1,
                  execution_submissions=0, actual_delivery='not_attempted', production_mutations=0)
    value = request('POST',
        API + PARENT + '/jobs?jobId=' + JOB_ID + '&validateOnly=true', body=expected,
        phase='job_validate_only')
    name = value.get('name')
    parents = (PARENT, f'projects/303168642265/locations/{REGION}')
    require(isinstance(name, str) and any(re.fullmatch(re.escape(parent) + r'/operations/[A-Za-z0-9_-]+', name)
                                          for parent in parents), 'validation_operation_identity_rejected')
    report.update(validation_operation_name_verified=True, validation_operation_done=False,
                  validation_operation_polls=0)
    while value.get('done') is not True:
        require(value.get('done') is None or value.get('done') is False,
                'validation_operation_state_unknown')
        remaining = deadline - clock()
        require(remaining > 0, 'validation_operation_timeout_no_retry')
        sleep(min(2, remaining))
        require(clock() < deadline, 'validation_operation_timeout_no_retry')
        report['validation_operation_polls'] += 1
        value = request('GET', API + name + '?fields=name,done,error', phase='validation_operation_read_only')
        require(value.get('name') == name, 'validation_operation_identity_changed')
    report['validation_operation_done'] = True
    job = request('GET', url, missing=True, phase='validation_job_absence_after')
    report['job_found'] = job is not None
    require(job is None, 'job_present_after_validation')
    report.update(status='validated_only_job_absent', job_found=False)


def execute_probe(session, report, *, sleep=time.sleep, clock=time.monotonic):
    deadline = clock() + 180
    def request(method, url, **kwargs):
        return control_request(session, report, method, url, deadline=deadline, clock=clock, **kwargs)
    def operation(value, phase, *, failed_execution=False):
        name = value.get('name', '')
        require(re.fullmatch(re.escape(PARENT) + r'/operations/[a-zA-Z0-9_-]+', name), 'operation_identity_rejected')
        while not value.get('done'):
            sleep(2)
            value = request('GET', API + name, operation_result=failed_execution, phase=phase)
            require(value.get('name') == name, 'operation_identity_changed')
        if value.get('error') and failed_execution:
            error, metadata = value['error'], value.get('metadata')
            require(type(error.get('code')) is int and 1 <= error['code'] <= 16 and error['code'] not in {7, 16}
                    and error.get('status') not in {'PERMISSION_DENIED', 'UNAUTHENTICATED'}
                    and isinstance(metadata, dict) and metadata.get('failedCount') == 1
                    and any(x.get('type') == 'Completed' and x.get('state') == 'CONDITION_FAILED'
                            for x in metadata.get('conditions', [])), 'operation_result_failed_no_retry')
            report['execution_operation_failed'] = True
            return metadata
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
    execution = operation(request('POST', API + JOB + ':run', body={'etag': etag}, operation_result=True,
                                  phase='execute_once'), 'execution_operation', failed_execution=True)
    name = execution.get('name', '')
    require(re.fullmatch(re.escape(JOB) + r'/executions/[a-zA-Z0-9_-]+', name) and execution.get('job') == JOB, 'execution_identity_rejected')
    report['execution_name'] = name
    completed = [x.get('state') for x in execution.get('conditions', []) if x.get('type') == 'Completed']
    require(len(completed) == 1 and completed[0] in {'CONDITION_SUCCEEDED', 'CONDITION_FAILED'}, 'execution_completion_unproved')
    successful = (execution.get('taskCount') == 1 and execution.get('succeededCount') == 1
                  and execution.get('failedCount', 0) == 0 and execution.get('cancelledCount', 0) == 0
                  and completed[0] == 'CONDITION_SUCCEEDED' and not report.get('execution_operation_failed'))
    report['execution_successful'] = successful
    suffix = name.rsplit('/', 1)[1]
    entries = request('POST', 'https://logging.googleapis.com/v2/entries:list', body={
        'resourceNames': ['projects/' + PROJECT], 'pageSize': 10, 'orderBy': 'timestamp desc',
        'filter': f'resource.type="cloud_run_job" AND resource.labels.job_name="{JOB_ID}" AND labels."run.googleapis.com/execution_name"="{suffix}"'}, phase='anonymous_execution_receipt')
    certificates = []
    for entry in entries.get('entries', []):
        value = entry.get('jsonPayload')
        if not isinstance(value, dict):
            try:
                value = json.loads(entry.get('textPayload', ''))
            except (ValueError, TypeError):
                continue
        if isinstance(value, dict) and value.get('probe_id') == NONCE:
            certificates.append(value)
    require(len(certificates) == 1, 'anonymous_receipt_missing_or_ambiguous_no_retry')
    proof = safe_probe_receipt(certificates[0])
    report['probe_receipt'] = proof
    require(successful, 'execution_not_successful')
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
    parser.add_argument('--mode', choices=['binding-read', 'execute-probe', 'probe-readback', 'probe-validate-only'], required=True)
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
                if args.mode == 'probe-readback':
                    read_probe(session, report)
                elif args.mode == 'probe-validate-only':
                    validate_probe(session, report)
                else:
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
    return 0 if report['status'] in {'binding_verified', 'pass_retained', 'job_absent_verified', 'job_observed', 'validated_only_job_absent'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
