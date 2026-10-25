"""Artificial metadata only; never read a report, secret, balance or broker."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import subprocess

import pytest

from scripts import diagnose_account_cash_settings as diagnostic

PRIVATE = "private-sentinel-never-output"
SERVICE = "artificial-service"
SERVING = SERVICE + "-serving"
NOW = datetime(2000, 1, 4, 12, tzinfo=timezone.utc)


def expected():
    return dict(expected_project_id=PRIVATE + "-project", expected_service_name=SERVICE,
        expected_account_scope=PRIVATE + "-scope", expected_account_selector=(PRIVATE + "-account",),
        expected_deployment_selector=PRIVATE + "-deployment", expected_runtime_revision=SERVICE + "-old")


def service():
    return dict(metadata=dict(name=SERVICE), status=dict(latestReadyRevisionName=SERVICE + "-candidate",
        latestCreatedRevisionName=SERVICE + "-candidate", traffic=[
            dict(revisionName=SERVING, percent=100), dict(revisionName=SERVICE + "-candidate", percent=0)]))


def target():
    e = expected()
    return dict(platform_id="ibkr", strategy_profile="artificial_profile", service_name=SERVICE,
        account_scope=e['expected_account_scope'], account_selector=list(e['expected_account_selector']),
        deployment_selector=e['expected_deployment_selector'], private=PRIVATE)


def revision():
    return dict(metadata=dict(name=SERVING), spec=dict(containers=[dict(image=PRIVATE, env=[
        dict(name="RUNTIME_TARGET_JSON", value=json.dumps(target())),
        dict(name=diagnostic.RATIO, value="0.03"), dict(name=diagnostic.FLOOR, value="1234.57"),
        dict(name="UNRELATED_SECRET", value=PRIVATE)])]))


@pytest.fixture(autouse=True)
def no_external(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unmocked external call forbidden")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(diagnostic.identity.publisher, "_latest_report_uri", forbidden)
    monkeypatch.setattr(diagnostic.identity.publisher, "_load_gcs_report", forbidden)
    monkeypatch.setattr(diagnostic.identity.publisher, "publish_ibkr_account_facts_history", forbidden)


def mock_reads(monkeypatch, *, initial=None, deployed=None, after=None, failed_call=None, timeout=False):
    calls = []
    values = [service() if initial is None else initial, revision() if deployed is None else deployed,
        service() if after is None else after]
    def run(command, **kwargs):
        calls.append(command)
        assert command[:4] in {('gcloud', 'run', 'services', 'describe'),
            ('gcloud', 'run', 'revisions', 'describe')}
        assert set(kwargs) == {'capture_output', 'text', 'check', 'timeout', 'env'}
        assert kwargs['capture_output'] is True and kwargs['text'] is True and kwargs['check'] is False
        assert kwargs['timeout'] == 30
        assert kwargs['env']['CLOUDSDK_CORE_DISABLE_FILE_LOGGING'] == 'true'
        assert kwargs['env']['CLOUDSDK_CORE_LOG_HTTP'] == 'false'
        if len(calls) == failed_call:
            if timeout: raise subprocess.TimeoutExpired(command, 30, output=PRIVATE, stderr=PRIVATE)
            return subprocess.CompletedProcess(command, 1, PRIVATE, PRIVATE)
        return subprocess.CompletedProcess(command, 0, json.dumps(values[len(calls)-1]), PRIVATE)
    monkeypatch.setattr(subprocess, 'run', run)
    return calls


def diagnose(**kwargs):
    return diagnostic.diagnose_cash_settings(kwargs.pop('expected', expected()),
        strategy_profile='artificial_profile', region='us-central1', now=NOW, **kwargs)


def assert_unarmed(out):
    for name in ('runtime_consumption_verified', 'spendable_cash_verified', 'account_policy_bound',
            'settings_applied', 'execution_authorized', 'breaker_reset', 'missing_fields_defaulted'):
        assert out[name] is False
    raw = json.dumps(out)
    assert PRIVATE not in raw and '1234.57' not in raw and '0.03' not in raw


def test_read_exact_serving_revision_recheck_traffic_and_only_emit_hashes(monkeypatch):
    calls = mock_reads(monkeypatch)
    out = diagnose()
    assert out['status'] == 'OBSERVED', out
    assert out['scope_verified'] and out['serving_stable']
    assert out['fields'][diagnostic.FLOOR]['status'] == 'EXPLICIT'
    assert out['selected_ratio_field'] == diagnostic.RATIO
    assert calls[1][4] == SERVING
    assert len(calls) == 3
    assert_unarmed(out)


@pytest.mark.parametrize('selector_type', [list, tuple])
@pytest.mark.parametrize('same_account', [True, False])
def test_expected_selector_uses_existing_sequence_semantics(monkeypatch, selector_type, same_account):
    wanted = expected()
    wanted['expected_account_selector'] = selector_type(wanted['expected_account_selector'])
    deployed = revision()
    if not same_account:
        body = target()
        body['account_selector'] = ['different-artificial-account']
        deployed['spec']['containers'][0]['env'][0]['value'] = json.dumps(body)
    calls = mock_reads(monkeypatch, deployed=deployed)
    out = diagnose(expected=wanted)
    assert out['scope_equalities']['account_selector'] is same_account
    assert out['status'] == ('OBSERVED' if same_account else 'UNAVAILABLE')
    assert len(calls) == (3 if same_account else 2)
    if not same_account:
        assert out['reason'] == 'deployed_target_mismatch'
        assert out['fields'] == {}
    # Canonicalization must not mutate the source binding supplied by callers.
    assert type(wanted['expected_account_selector']) is selector_type
    assert_unarmed(out)


@pytest.mark.parametrize(('rows', 'state', 'zero'), [
    ([], 'ABSENT', None), ([dict(name=diagnostic.FLOOR, value='')], 'BLANK', None),
    ([dict(name=diagnostic.FLOOR, value='0')], 'EXPLICIT', True),
    ([dict(name=diagnostic.FLOOR, value='10')], 'EXPLICIT', False),
    ([dict(name=diagnostic.FLOOR, valueFrom={'secretKeyRef': {'name': PRIVATE}})], 'SECRET_REFERENCE', None),
    ([dict(name=diagnostic.FLOOR, value=True)], 'INVALID', None),
    ([dict(name=diagnostic.FLOOR, value='NaN')], 'INVALID', None),
    ([dict(name=diagnostic.FLOOR, value='Infinity')], 'INVALID', None),
    ([dict(name=diagnostic.FLOOR, value='1e999')], 'INVALID', None),
    ([dict(name=diagnostic.FLOOR, value='-1')], 'INVALID', None),
    ([dict(name=diagnostic.FLOOR, value='0')]*2, 'DUPLICATE', None),
    ([dict(name=diagnostic.FLOOR, value='0', valueFrom={})], 'INVALID', None),
])
def test_explicit_zero_absent_unread_secret_and_invalid_are_distinct(rows, state, zero):
    fields, _ = diagnostic.project_cash_fields(rows)
    assert fields[diagnostic.FLOOR]['status'] == state
    assert fields[diagnostic.FLOOR]['explicit_zero'] is zero
    assert set(fields[diagnostic.FLOOR]) == {'status', 'explicit_zero'}
    assert PRIVATE not in json.dumps(fields)


@pytest.mark.parametrize('value', ['1.01', '-.01', 'true', 'NaN', '1e999'])
def test_invalid_ratios_do_not_become_a_default(value):
    fields, _ = diagnostic.project_cash_fields([dict(name=diagnostic.RATIO, value=value)])
    assert fields[diagnostic.RATIO]['status'] == 'INVALID'


def test_ratio_only_does_not_require_user_to_invent_a_fixed_floor(monkeypatch):
    deployed = revision()
    deployed['spec']['containers'][0]['env'] = [r for r in deployed['spec']['containers'][0]['env']
        if r['name'] != diagnostic.FLOOR]
    mock_reads(monkeypatch, deployed=deployed)
    out = diagnose()
    assert out['status'] == 'OBSERVED', out
    assert out['fields'][diagnostic.FLOOR] == dict(status='ABSENT', explicit_zero=None)
    assert_unarmed(out)


@pytest.mark.parametrize(('override', 'selected', 'state'), [
    ('0', diagnostic.OVERRIDE_RATIO, 'EXPLICIT'), ('0.2', diagnostic.OVERRIDE_RATIO, 'EXPLICIT'),
    ('', diagnostic.RATIO, 'BLANK'), (None, diagnostic.OVERRIDE_RATIO, 'SECRET_REFERENCE'),
    ('  ', diagnostic.OVERRIDE_RATIO, 'BLANK'),
])
def test_original_ratio_override_priority_is_observed_not_assumed_consumed(override, selected, state):
    row = dict(name=diagnostic.OVERRIDE_RATIO)
    row.update(valueFrom={'secretKeyRef': {'name': PRIVATE}}) if override is None else row.update(value=override)
    fields, actual = diagnostic.project_cash_fields([dict(name=diagnostic.RATIO, value='.03'), row])
    assert actual['ratio'] == selected and fields[diagnostic.OVERRIDE_RATIO]['status'] == state


def test_equivalent_runtime_numbers_compare_without_publishing_numeric_hashes():
    rows = [dict(name=diagnostic.RATIO, value='.0300')]
    same = diagnostic.compare_cash_fields(rows, {diagnostic.RATIO:'3e-2'})
    different = diagnostic.compare_cash_fields(rows, {diagnostic.RATIO:'.04'})
    assert same[diagnostic.RATIO]['same_explicit_number'] is True
    assert different[diagnostic.RATIO]['same_explicit_number'] is False
    assert same[diagnostic.FLOOR]['same_explicit_number'] is None
    assert 'sha256' not in json.dumps(diagnostic.project_cash_fields(rows))


@pytest.mark.parametrize(('override', 'selected', 'state'), [
    ('500', diagnostic.OVERRIDE_FLOOR, 'EXPLICIT'), ('0', diagnostic.OVERRIDE_FLOOR, 'EXPLICIT'),
    ('', diagnostic.FLOOR, 'BLANK'), ('  ', diagnostic.OVERRIDE_FLOOR, 'BLANK'),
    (None, diagnostic.OVERRIDE_FLOOR, 'SECRET_REFERENCE'),
])
def test_floor_uses_the_original_qsl_override_priority(override, selected, state):
    row = dict(name=diagnostic.OVERRIDE_FLOOR)
    row.update(valueFrom={'secretKeyRef': {'name': PRIVATE}}) if override is None else row.update(value=override)
    rows = [dict(name=diagnostic.FLOOR, value='0'), row]
    fields, actual = diagnostic.project_cash_fields(rows)
    assert actual['floor'] == selected
    assert fields[diagnostic.OVERRIDE_FLOOR]['status'] == state
    if override == '500':
        assert fields[diagnostic.FLOOR]['explicit_zero'] is True
        assert fields[diagnostic.OVERRIDE_FLOOR]['explicit_zero'] is False
    assert PRIVATE not in json.dumps(fields)


def test_alias_only_floor_is_not_reported_as_no_configured_floor():
    rows = [dict(name=diagnostic.OVERRIDE_FLOOR, value='500')]
    fields, selected = diagnostic.project_cash_fields(rows)
    assert selected['floor'] == diagnostic.OVERRIDE_FLOOR
    assert fields[diagnostic.FLOOR]['status'] == 'ABSENT'
    assert fields[diagnostic.OVERRIDE_FLOOR]['status'] == 'EXPLICIT'


@pytest.mark.parametrize('change', ['service', 'revision', 'scope', 'selector', 'profile', 'platform',
    'split', 'containers', 'target_secret', 'duplicate_target', 'override_target', 'invalid_ratio', 'changed'])
def test_scope_shape_unknown_or_changed_metadata_never_releases_partial_fields(monkeypatch, change):
    initial, deployed, after = service(), revision(), service()
    rows = deployed['spec']['containers'][0]['env']
    body = target()
    if change == 'service': initial['metadata']['name'] = 'another-service'
    elif change == 'revision': deployed['metadata']['name'] = SERVICE + '-candidate'
    elif change == 'scope': body['account_scope'] = 'other'
    elif change == 'selector': body['account_selector'] = ['other']
    elif change == 'profile': body['strategy_profile'] = 'other'
    elif change == 'platform': body['platform_id'] = 'other'
    elif change == 'split': initial['status']['traffic'] = [dict(revisionName=SERVING, percent=50), dict(revisionName=SERVICE+'-other', percent=50)]
    elif change == 'containers': deployed['spec']['containers'].append(deepcopy(deployed['spec']['containers'][0]))
    elif change == 'target_secret': rows[0] = dict(name='RUNTIME_TARGET_JSON', valueFrom={'secretKeyRef': {'name': PRIVATE}})
    elif change == 'duplicate_target': rows.append(deepcopy(rows[0]))
    elif change == 'override_target': rows.append(dict(name='QSL_RUNTIME_TARGET_JSON', value=json.dumps(dict(body, account_scope='other'))))
    elif change == 'invalid_ratio': rows[1]['value'] = PRIVATE
    elif change == 'changed': after['status']['traffic'] = [dict(revisionName=SERVICE+'-other', percent=100)]
    if change in ('scope', 'selector', 'profile', 'platform'): rows[0]['value'] = json.dumps(body)
    mock_reads(monkeypatch, initial=initial, deployed=deployed, after=after)
    out = diagnose()
    assert out['status'] == 'UNAVAILABLE', out
    assert out['fields'] == {} and out['intent_comparisons'] == {}
    assert not out['scope_verified']
    assert_unarmed(out)


@pytest.mark.parametrize('failed_call', [1, 2, 3])
@pytest.mark.parametrize('timeout', [False, True])
def test_read_failure_and_timeout_are_unavailable_not_empty_success(monkeypatch, failed_call, timeout):
    calls = mock_reads(monkeypatch, failed_call=failed_call, timeout=timeout)
    out = diagnose()
    assert out['status'] == 'UNAVAILABLE' and out['reason'] == 'metadata_read_failed'
    assert out['fields'] == {} and len(calls) == failed_call
    assert_unarmed(out)


def test_invalid_expected_scope_fails_before_any_cloud_read():
    bad = expected(); bad['expected_account_selector'] = []
    out = diagnose(expected=bad)
    assert out['reason'] == 'expected_target_invalid'
    assert_unarmed(out)


def test_duplicate_target_json_keys_cannot_silently_pick_last_value(monkeypatch):
    deployed = revision()
    raw = json.dumps(target())
    deployed['spec']['containers'][0]['env'][0]['value'] = raw[:-1] + ',"platform_id":"ibkr"}'
    mock_reads(monkeypatch, deployed=deployed)
    assert diagnose()['reason'] == 'deployed_target_mismatch'


@pytest.mark.parametrize('mode', ['alias_only', 'same_both', 'empty_override'])
def test_original_target_alias_forms_preserve_the_same_bound_identity(monkeypatch, mode):
    deployed = revision()
    rows = deployed['spec']['containers'][0]['env']
    if mode == 'alias_only': rows[0]['name'] = 'QSL_RUNTIME_TARGET_JSON'
    else: rows.append(dict(name='QSL_RUNTIME_TARGET_JSON', value='' if mode == 'empty_override' else rows[0]['value']))
    mock_reads(monkeypatch, deployed=deployed)
    out = diagnose()
    assert out['status'] == 'OBSERVED', out
    assert_unarmed(out)


def test_whitespace_target_override_does_not_silently_fall_back(monkeypatch):
    deployed = revision()
    deployed['spec']['containers'][0]['env'].append(dict(name='QSL_RUNTIME_TARGET_JSON', value=' '))
    mock_reads(monkeypatch, deployed=deployed)
    assert diagnose()['reason'] == 'deployed_target_mismatch'


def test_cli_uses_existing_slot_identity_and_never_a_report(monkeypatch, capsys):
    mock_reads(monkeypatch)
    monkeypatch.setattr(diagnostic.identity, 'resolve_expected', expected)
    monkeypatch.setenv('CLOUD_RUN_REGION', 'us-central1')
    assert diagnostic.main(['--strategy-profile', 'artificial_profile']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['status'] == 'OBSERVED'
    assert_unarmed(out)


def test_in_memory_intent_comparison_and_unknown_configured_revision(monkeypatch):
    mock_reads(monkeypatch)
    minimal = expected(); minimal.pop('expected_runtime_revision')
    out = diagnose(expected=minimal, expected_declarations={diagnostic.RATIO:'.0300'})
    assert out['status'] == 'OBSERVED'
    assert out['intent_comparisons'][diagnostic.RATIO]['same_explicit_number'] is True
    assert out['intent_comparisons'][diagnostic.FLOOR]['same_explicit_number'] is None
    assert 'sha256' not in json.dumps(out['fields'])
    assert_unarmed(out)


def test_cli_arguments_do_not_echo_private_input(capsys):
    assert diagnostic.main([PRIVATE]) == 1
    assert PRIVATE not in capsys.readouterr().out
