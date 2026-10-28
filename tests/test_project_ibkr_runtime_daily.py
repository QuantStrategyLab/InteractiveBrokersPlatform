from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest
from quant_platform_kit.common.execution_receipts import attach_runtime_execution_receipt

from scripts.project_ibkr_runtime_daily import project_ibkr_runtime_daily
from tests.test_publish_ibkr_account_facts import _report, _observability_publish_args


TARGET = 'ibkr-primary-service|fixture_strategy|live-primary|' + 'a' * 32
OBSERVED = datetime(2026, 9, 30, 5, tzinfo=timezone.utc)
EXPECTED = _observability_publish_args()


def source():
    report = _report()
    report.update(run_id='private-run', strategy_profile='fixture_strategy', status='ok', errors=[], dry_run=False)
    report['runtime_target']['execution_mode'] = 'live'
    report['runtime_release_receipt'] = {'attestation_state': 'self_attested', 'strategy_release': {'strategy_revision': 'b' * 40}}
    return report


def project(reports, **kwargs):
    return project_ibkr_runtime_daily(reports, expected=EXPECTED, target_key=TARGET,
        business_date=kwargs.get('day', '2026-09-29'), observed_at=OBSERVED)


def test_missing_report_is_incomplete_and_does_not_mean_not_run():
    result = project([])
    record = result['records'][0]
    assert record['status'] == 'read_incomplete'
    assert record['schedule']['state'] == 'unevaluable'
    assert result['read_errors'] == ['coverage_unconfirmed']
    assert record['runs'] == [] and record['fills']['count'] is None


def test_real_contract_receipt_binding_and_private_fields_stay_out():
    report = source()
    attach_runtime_execution_receipt(report, outcome='submitted', broker_confirmation='not_observed', observed_at='2026-09-30T01:00:01Z')
    original = deepcopy(report)
    result = project([report])
    run = result['records'][0]['runs'][0]
    assert run['activity'] == 'submitted'
    assert run['evidence']['receipt_broker_confirmation'] == 'not_observed'
    assert run['evidence']['orders_pending_count'] is None
    assert result['records'][0]['completeness'] == 'incomplete'
    assert report == original
    rendered = json.dumps(result)
    for secret in ('U00000001', 'private-run', '12345.6700', 'example-project', 'runtime-revision-001'):
        assert secret not in rendered


def test_missing_and_mismatched_receipt_stays_unknown():
    report = source()
    assert project([report])['records'][0]['runs'][0]['activity'] == 'unknown'
    attach_runtime_execution_receipt(report, outcome='filled', broker_confirmation='filled', observed_at='2026-09-30T01:00:01Z')
    report['runtime_release_receipt']['strategy_release']['strategy_revision'] = 'c' * 40
    row = project([report])['records'][0]['runs'][0]
    assert row['activity'] == 'unknown' and row['evidence']['receipt_outcome'] is None


@pytest.mark.parametrize('mutation', ['selector', 'account_ids', 'revision', 'profile', 'platform'])
def test_other_identity_is_never_attributed(mutation):
    report = source()
    if mutation == 'selector': report['runtime_target']['account_selector'] = ['U00000002']
    if mutation == 'account_ids': report['summary']['account_facts']['account_ids'] = ['U00000002']
    if mutation == 'revision': report['diagnostics']['runtime_revision'] = 'other'
    if mutation == 'profile': report['strategy_profile'] = 'other'
    if mutation == 'platform': report['platform'] = 'other'
    result = project([report])
    assert result['records'][0]['runs'] == []
    assert result['unmatched_reports'][0]['reason'] == 'identity_unverified'


def test_cross_midnight_uses_start_and_unknown_errors_do_not_become_false():
    report = source()
    report['started_at'] = '2026-09-30T03:59:59Z'
    report['finished_at'] = '2026-09-30T04:00:01Z'
    assert len(project([report])['records'][0]['runs']) == 1
    assert project([report], day='2026-09-30')['records'][0]['runs'] == []
    del report['errors']
    assert project([report])['records'][0]['excluded_reports'][0]['reason'] == 'invalid_report'


def test_duplicate_and_invalid_time_stay_incomplete():
    report = source()
    result = project([report, deepcopy(report)])
    assert result['records'][0]['status'] == 'conflict'
    assert len(result['records'][0]['runs']) == len(result['records'][0]['conflicts']) == 1
    report['finished_at'] = '2026-09-30T00:00:00Z'
    assert project([report])['records'][0]['excluded_reports'][0]['reason'] == 'invalid_run_time'
    with pytest.raises(ValueError, match='report_limit'):
        project([source()] * 21)


def test_receipt_from_another_time_and_report_error_are_not_credited():
    report = source()
    attach_runtime_execution_receipt(report, outcome='filled', broker_confirmation='filled', observed_at='2026-09-30T00:59:00Z')
    assert project([report])['records'][0]['runs'][0]['activity'] == 'unknown'
    report = source()
    attach_runtime_execution_receipt(report, outcome='filled', broker_confirmation='filled', observed_at='2026-09-30T01:00:01Z')
    report['errors'] = ['private error']
    row = project([report])['records'][0]['runs'][0]
    assert row['activity'] == 'unknown' and row['evidence']['errors_present'] is True


def test_daylight_saving_time_uses_new_york_date():
    report = source()
    report['started_at'] = '2026-11-01T05:30:00Z'
    report['finished_at'] = '2026-11-01T06:30:00Z'
    result = project_ibkr_runtime_daily([report], expected=EXPECTED, target_key=TARGET,
        business_date='2026-11-01', observed_at=datetime(2026, 11, 1, 7, tzinfo=timezone.utc))
    assert len(result['records'][0]['runs']) == 1


def test_conflicting_execution_status_does_not_promote_fill():
    report = source()
    attach_runtime_execution_receipt(report, outcome='filled', broker_confirmation='filled', observed_at='2026-09-30T01:00:01Z')
    report['summary']['execution_status'] = 'submitted'
    run = project([report])['records'][0]['runs'][0]
    assert run['activity'] == 'unknown' and run['evidence']['receipt_outcome'] is None


@pytest.mark.parametrize('execution', ['skipped', 'outside_market_hours', 'dry_run', 'previewed'])
@pytest.mark.parametrize('outcome,confirmation', [('filled', 'filled'), ('submitted', 'not_observed')])
def test_non_execution_status_does_not_promote_submission_or_fill(execution, outcome, confirmation):
    report = source()
    attach_runtime_execution_receipt(report, outcome=outcome, broker_confirmation=confirmation,
        observed_at='2026-09-30T01:00:01Z')
    report['summary']['execution_status'] = execution
    run = project([report])['records'][0]['runs'][0]
    assert run['activity'] == 'unknown'
    assert run['evidence']['execution_status'] == execution
    assert run['evidence']['receipt_outcome'] is None


@pytest.mark.parametrize('execution', ['failed', 'failure', 'error', 'blocked', 'risk_blocked'])
@pytest.mark.parametrize('outcome,confirmation', [
    ('filled', 'filled'), ('submitted', 'not_observed'), ('no_action', 'not_applicable'),
])
def test_failed_or_blocked_execution_does_not_promote_conflicting_receipt(execution, outcome, confirmation):
    report = source()
    attach_runtime_execution_receipt(report, outcome=outcome, broker_confirmation=confirmation,
        observed_at='2026-09-30T01:00:01Z')
    report['summary']['execution_status'] = execution
    run = project([report])['records'][0]['runs'][0]
    assert run['activity'] == 'unknown'
    assert run['evidence']['receipt_outcome'] is None
    assert run['evidence']['execution_status'] == execution


@pytest.mark.parametrize('execution,outcome,confirmation,activity', [
    ('failed', 'failed', 'not_observed', 'failed'),
    ('error', 'failed', 'not_observed', 'failed'),
    ('blocked', 'risk_blocked', 'not_applicable', 'blocked'),
    ('risk_blocked', 'risk_blocked', 'not_applicable', 'blocked'),
    ('filled', 'filled', 'filled', 'filled'),
])
def test_matching_execution_and_receipt_remain_observable(execution, outcome, confirmation, activity):
    report = source()
    attach_runtime_execution_receipt(report, outcome=outcome, broker_confirmation=confirmation,
        observed_at='2026-09-30T01:00:01Z')
    report['summary']['execution_status'] = execution
    run = project([report])['records'][0]['runs'][0]
    assert run['activity'] == activity
    assert run['evidence']['receipt_outcome'] == outcome
