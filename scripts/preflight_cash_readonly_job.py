"""Inspect one protected service and archive target; never connect to IBKR or mutate cloud."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os

from scripts import diagnose_account_facts_identity as identity
from scripts import diagnose_ibkr_flex_runtime_binding as binding
from scripts import diagnose_serving_image_identity as image_identity
from scripts import inspect_account_facts_archive as archive
from scripts.account_cash_readonly_entry import report_prefix_sha256, target_binding_sha256


def _sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def inspect(env, *, expected, reader=binding._read):
    """Fail closed on missing identity, disabled state, immutable image or archive prefix."""
    binding.require(env.get('REQUESTED_TARGET') in binding.PROFILES, 'target_not_supported')
    _, targets, project, region = binding.configuration(env)
    binding.require(len(targets) == 1, 'target_not_unique')
    archive.resolve_profile_slot(env)
    profile, (service_name, target) = next(iter(targets.items()))
    binding.require(expected['expected_project_id'] == project and expected['expected_service_name'] == service_name,
                    'archive_target_mismatch')
    service = reader('services', service_name, project, region)
    binding.require(service.get('metadata', {}).get('name') == service_name, 'runtime_identity_unavailable')
    version = service.get('metadata', {}).get('resourceVersion')
    binding.require(binding.single_line_text(version), 'runtime_identity_unavailable')
    revision_name = binding._serving(service)
    binding.require(revision_name == expected['expected_runtime_revision'], 'configured_revision_mismatch')
    revision = reader('revisions', revision_name, project, region)
    binding.require(revision.get('metadata', {}).get('name') == revision_name and any(
        row.get('type') == 'Ready' and row.get('status') == 'True'
        for row in revision.get('status', {}).get('conditions', []) if isinstance(row, dict)),
        'runtime_identity_unavailable')
    spec = revision.get('spec', {})
    env_rows = binding._environment(spec)
    target_raw = env_rows.get('RUNTIME_TARGET_JSON')
    override = env_rows.get('QSL_RUNTIME_TARGET_JSON')
    binding.require('QSL_RUNTIME_TARGET_JSON' not in env_rows or isinstance(override, str), 'runtime_scope_unavailable')
    binding.require(isinstance(target_raw, str) and target_raw, 'runtime_scope_unavailable')
    runtime = binding._json(target_raw)
    binding.require(not override or binding._json(override) == runtime, 'runtime_scope_mismatch')
    binding.require(runtime.get('strategy_profile') == profile and runtime.get('execution_mode') == 'live' and all(
        binding.account_selector(runtime.get(field)) == binding.account_selector(target.get(field))
        if field == 'account_selector' else runtime.get(field) == target.get(field)
        for field in binding.IDENTITY_FIELDS), 'runtime_scope_mismatch')
    binding.require(str(env_rows.get('RUNTIME_TARGET_ENABLED', '')).strip().lower() == 'false', 'runtime_not_explicitly_disabled')
    target_digest = target_binding_sha256(runtime)
    report_digest = report_prefix_sha256(env_rows.get('EXECUTION_REPORT_GCS_URI'))
    image, container = image_identity.immutable_image(revision)
    service_account = spec.get('serviceAccountName')
    binding.require(binding.single_line_text(service_account), 'service_account_unavailable')
    network = revision.get('metadata', {}).get('annotations', {})
    binding.require(isinstance(network, dict), 'network_metadata_unavailable')
    network = {key: value for key, value in network.items() if key in {
        'run.googleapis.com/vpc-access-connector', 'run.googleapis.com/vpc-access-egress',
        'run.googleapis.com/network-interfaces'}}
    rows = container.get('env', [])
    secret_refs = [row.get('valueFrom') for row in rows if isinstance(row, dict) and 'valueFrom' in row]
    after = reader('services', service_name, project, region)
    binding.require(after.get('metadata', {}).get('resourceVersion') == version and binding._serving(after) == revision_name,
                    'serving_changed_during_read')
    return {'status': 'metadata_verified', 'target_binding_unique': True, 'target_disabled': True,
            'archive_prefix_configured': True, 'serving_stable': True,
            'target_binding_sha256': target_digest, 'report_prefix_sha256': report_digest,
            'serving_revision_sha256': _sha(revision_name), 'immutable_image_sha256': _sha(image),
            'service_account_sha256': _sha(service_account), 'network_declarations_sha256': _sha(network),
            'secret_references_sha256': _sha(secret_refs), 'secret_reference_count': len(secret_refs),
            'worker_account_binding_verified': None, 'job_permissions': 'not_checked',
            'worker_storage_permissions': 'not_checked', 'network_reachability': 'not_checked',
            'concurrent_execution': 'not_checked', 'execution_path': 'not_checked',
            'broker_requests': 0, 'cloud_writes': 0}


def main():
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            slot = archive.resolve_profile_slot(os.environ)
            os.environ['IBKR_ACCOUNT_FACTS_TARGET'] = slot
            result = inspect(os.environ, expected=identity.resolve_expected())
    except Exception:
        result = {'status': 'stopped', 'reason': 'metadata_preflight_unavailable',
                  'job_permissions': 'not_checked', 'broker_requests': 0, 'cloud_writes': 0}
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] == 'metadata_verified' else 1


if __name__ == '__main__':
    raise SystemExit(main())
