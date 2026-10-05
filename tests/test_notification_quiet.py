"""Offline notification disposition tests; broker and cloud calls are forbidden."""
from __future__ import annotations

import copy
import datetime as dt
import json
import socket
import subprocess
import urllib.request

import pytest

from application import rebalance_service as rebalance
from scripts import daily_dry_run_digest as drill
from scripts import execution_report_heartbeat as heartbeat

LOAD_DRILL_TARGETS = drill._targets

NOW = dt.datetime(2026, 10, 5, 13, 0, tzinfo=dt.timezone.utc)
TARGET = drill.DrillTarget('mock-target', 'mock_profile', 'mock_scope', 'mock-service', '0 12 * * *', 'UTC')


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('No network, cloud, scheduler, or broker operations in this suite')
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(subprocess, 'run', denied)
    monkeypatch.setattr(urllib.request, 'urlopen', denied)
    for key in list(__import__('os').environ):
        if key.startswith(('RUNTIME_HEARTBEAT_', 'DRILL_DIGEST_', 'CLOUD_RUN_')):
            monkeypatch.delenv(key, raising=False)


def report():
    return {'service_name': TARGET.service, 'strategy_profile': TARGET.profile,
            'account_scope': TARGET.scope, 'started_at': '2026-10-05T12:00:00Z',
            'finished_at': '2026-10-05T12:01:00Z', 'status': 'ok', 'dry_run': True,
            'errors': [], 'summary': {'execution_status': 'no_op', 'orders_submitted_count': 0},
            'diagnostics': {}, 'market': 'US', 'market_calendar': 'NYSE', 'market_timezone': 'America/New_York'}


def mock_drill(monkeypatch, payload, *, targets=None):
    monkeypatch.setattr(heartbeat, '_market_closed_by_calendar', lambda payload: payload.get('market_timezone') == 'America/New_York')
    monkeypatch.setattr(drill, '_targets', lambda: targets if targets is not None else [TARGET])
    monkeypatch.setattr(drill, '_service_is_drill_only', lambda target: (True, 'https://mock.invalid'))
    def job(name):
        if name == TARGET.live_job:
            return {'state': 'PAUSED'}
        return {'state': 'ENABLED', 'schedule': TARGET.schedule, 'timeZone': TARGET.timezone,
                'lastAttemptTime': '2026-10-05T12:00:00Z', 'status': {'code': 0},
                'httpTarget': {'uri': 'https://mock.invalid/dry-run', 'httpMethod': 'POST',
                               'oidcToken': {'serviceAccountEmail': 'mock@example.invalid', 'audience': 'https://mock.invalid'}}}
    monkeypatch.setattr(drill, '_job', job)
    monkeypatch.setattr(drill, '_today_reports', lambda target, day: [payload] if payload is not None else [])
    sent = []
    monkeypatch.setattr(drill, '_send_telegram', lambda msg, **kw: sent.append((msg, kw)) or True)
    return sent


@pytest.mark.parametrize('kind', ['success', 'market_closed'])
def test_verified_drill_is_recorded_without_sending(monkeypatch, capsys, kind):
    payload = report()
    if kind == 'market_closed':
        payload.update(status='skipped', diagnostics={'skip_reason': 'market_closed'}, summary={'orders_submitted_count': 0})
    sent = mock_drill(monkeypatch, payload)
    assert drill.main(NOW) == 0
    assert not sent
    assert 'mock-target' in capsys.readouterr().out


@pytest.mark.parametrize('change', [
    {'summary': {'execution_status': value, 'orders_submitted_count': 0}}
    for value in ['unknown', 'blocked', 'partial', 'pending', 'pending_reconciliation', 'submitted', 'broker_acknowledged', 'partially_filled', 'reconciliation_required', '']
] + [
    {'summary': {'execution_status': 'no_op', 'orders_submitted_count': value}}
    for value in [False, '0', None, 1]
] + [
    {'errors': ['persistence failed']}, {'errors': None}, {'finished_at': None},
    {'summary': {'execution_status': 'no_op'}}, {'summary': {'execution_status': 'no_op', 'orders_submitted_count': 0, 'orders_pending_count': 1}},
    {'summary': {'execution_status': 'no_op', 'orders_submitted_count': 0, 'plugin_error': 'failed'}},
    {'status': 'skipped', 'diagnostics': {'skip_reason': 'already_running'}},
    {'status': 'skipped', 'diagnostics': {'skip_reason': 'market_closed'}, 'errors': ['failed']},
    {'status': 'skipped', 'diagnostics': {'skip_reason': 'market_closed'}, 'market_timezone': 'unknown-zone'},
    {'stage': 'ERROR'}, {'summary': {'status': 'failed', 'execution_status': 'no_op', 'orders_submitted_count': 0}}, {'finished_at': '2026-10-05T14:00:00Z'},
    {'service_name': 'other-service'}, {'started_at': '2026-10-04T12:00:00Z'},
])
def test_drill_unconfirmed_and_conflicting_evidence_stays_visible(monkeypatch, change):
    payload = report()
    payload.update(copy.deepcopy(change))
    sent = mock_drill(monkeypatch, payload)
    assert drill.main(NOW) == 1
    assert len(sent) == 1
    assert sent[0][1] == {'retry_429_once': True}


def test_drill_missing_report_and_read_failure_stay_visible(monkeypatch):
    sent = mock_drill(monkeypatch, None)
    assert drill.main(NOW) == 1
    assert sent
    monkeypatch.setattr(drill, '_today_reports', lambda *args: (_ for _ in ()).throw(RuntimeError('read failed')))
    assert drill.main(NOW) == 1
    assert len(sent) == 2


def test_drill_empty_targets_quiet_and_invalid_inventory_not_success(monkeypatch):
    sent = mock_drill(monkeypatch, None, targets=[])
    assert drill.main(NOW) == 0
    assert not sent
    monkeypatch.setenv('CLOUD_RUN_SERVICE_TARGETS_JSON', '{')
    with pytest.raises(ValueError):
        LOAD_DRILL_TARGETS()


def test_drill_mixed_targets_only_sends_exception_and_records_every_target(monkeypatch, capsys):
    other = drill.DrillTarget('other-target', 'other', 'other', 'other-service', '0 12 * * *', 'UTC')
    sent = mock_drill(monkeypatch, report(), targets=[TARGET, other])
    def target_status(target, now):
        return NOW.date(), ('complete' if target == TARGET else 'unknown'), target == TARGET
    monkeypatch.setattr(drill, '_target_status', target_status)
    assert drill.main(NOW) == 1
    assert 'other-target' in sent[0][0]
    assert 'mock-target' not in sent[0][0]
    assert 'mock-target' in capsys.readouterr().out


def mock_heartbeat(monkeypatch, payload):
    monkeypatch.setattr(heartbeat, '_runtime_target_enabled', lambda: True)
    monkeypatch.setattr(heartbeat, 'runtime_target_configuration_present', lambda env: False)
    monkeypatch.setattr(heartbeat, 'load_runtime_targets', lambda env: [])
    monkeypatch.setattr(heartbeat, '_hydrate_runtime_target_schedules', lambda targets, **kw: targets)
    monkeypatch.setattr(heartbeat, 'filter_due_targets', lambda *args, **kw: ([], False))
    monkeypatch.setattr(heartbeat, '_runtime_target_scheduler_skip_reason', lambda *args: None)
    monkeypatch.setattr(heartbeat, '_resolve_required_services', lambda **kw: ([], None, False))
    monkeypatch.setattr(heartbeat, '_report_globs', lambda *args: ['gs://mock/reports'])
    monkeypatch.setattr(heartbeat, '_list_gcs_objects', lambda *args, **kw: [{'url': 'gs://mock/report.json', 'metadata': {'updated': NOW.isoformat()}}])
    monkeypatch.setattr(heartbeat, '_cat_gcs_json', lambda *args, **kw: payload)
    sent = []
    monkeypatch.setattr(heartbeat, '_send_telegram', lambda msg, **kw: sent.append(msg) or True)
    return sent


@pytest.mark.parametrize('flag', [None, 'false', 'true'])
def test_heartbeat_explicit_normal_preserves_manual_flag(monkeypatch, capsys, flag):
    if flag is not None:
        monkeypatch.setenv('RUNTIME_HEARTBEAT_NOTIFY_ON_SUCCESS', flag)
    sent = mock_heartbeat(monkeypatch, report())
    assert heartbeat.main(NOW) == 0
    assert bool(sent) is (flag == 'true')
    assert 'heartbeat OK' in capsys.readouterr().out


@pytest.mark.parametrize('execution_status', ['unknown', 'partial', 'pending_reconciliation', 'reconciliation_required'])
def test_accepted_business_or_unknown_report_is_visible_with_success_flag_off(monkeypatch, execution_status):
    payload = report()
    payload['summary']['execution_status'] = execution_status
    sent = mock_heartbeat(monkeypatch, payload)
    assert heartbeat.main(NOW) == 0
    assert len(sent) == 1
    assert payload['summary']['execution_status'] == execution_status


@pytest.mark.parametrize('change', [{'errors': None}, {'finished_at': None}, {'summary': {}}, {'summary': {'execution_status': 'no_op', 'orders_submitted_count': '0'}}])
def test_accepted_incomplete_report_is_visible(monkeypatch, change):
    payload = report()
    payload.update(change)
    sent = mock_heartbeat(monkeypatch, payload)
    assert heartbeat.main(NOW) == 0
    assert sent


def test_heartbeat_nested_error_does_not_hide_in_top_level_ok(monkeypatch):
    payload = report()
    payload['summary']['errors'] = ['plugin failed']
    sent = mock_heartbeat(monkeypatch, payload)
    assert heartbeat.main(NOW) == 1
    assert sent


def test_outside_window_cannot_suppress_real_error_or_business_state():
    assert not rebalance._should_suppress_noop_notification({'no_op_reason': 'outside_execution_window'}, order_count=0, has_error=True, notify_no_trade_cycles=False)
    for status in ['unknown', 'blocked', 'pending', 'submitted', 'broker_acknowledged', 'partially_filled', 'pending_reconciliation']:
        assert not rebalance._should_suppress_noop_notification({'no_op_reason': 'outside_execution_window', 'execution_status': status}, order_count=0, notify_no_trade_cycles=False)


@pytest.mark.parametrize('field', ['errors', 'plugin_error', 'persistence_error', 'notification_error', 'execution_blocked_reason', 'fail_reason'])
def test_metadata_problem_cannot_suppress_noop(field):
    assert not rebalance._should_suppress_noop_notification({'actionable': False, 'risk_flags': (), field: 'mock failure'}, order_count=0, notify_no_trade_cycles=False)


@pytest.mark.parametrize('execution_status', ['executed', 'completed', 'submitted', 'broker_acknowledged', 'partially_filled', 'pending'])
def test_healthy_receipt_does_not_repeat_trade_notification(monkeypatch, execution_status):
    payload = report()
    payload['summary'].update(execution_status=execution_status, orders_submitted_count=1)
    sent = mock_heartbeat(monkeypatch, payload)
    assert heartbeat.main(NOW) == 0
    assert not sent
    assert payload['summary']['execution_status'] == execution_status


def test_newest_rejected_report_cannot_be_hidden_by_older_healthy_report(monkeypatch):
    payload = report()
    sent = mock_heartbeat(monkeypatch, payload)
    monkeypatch.setattr(heartbeat, '_list_gcs_objects', lambda *args, **kw: [
        {'url': 'gs://mock/new.json', 'metadata': {'updated': NOW.isoformat()}},
        {'url': 'gs://mock/old.json', 'metadata': {'updated': (NOW - dt.timedelta(minutes=2)).isoformat()}}])
    def read(uri, **kw):
        result = report()
        if uri.endswith('new.json'):
            result['summary']['errors'] = ['persistence failed']
        return result
    monkeypatch.setattr(heartbeat, '_cat_gcs_json', read)
    assert heartbeat.main(NOW) == 1
    assert sent


def test_noop_problem_guard_keeps_existing_live_strategy_cycle_policy():
    metadata = {'actionable': False, 'risk_flags': ()}
    assert not rebalance._should_suppress_noop_notification(metadata, order_count=0, notify_no_trade_cycles=True)


def test_drill_calendar_failure_is_visible_even_with_skipped_market_closed(monkeypatch):
    payload = report()
    payload.update(status='skipped', diagnostics={'skip_reason': 'market_closed'}, summary={'orders_submitted_count': 0})
    sent = mock_drill(monkeypatch, payload)
    monkeypatch.setattr(heartbeat, '_market_closed_by_calendar', lambda payload: False)
    assert drill.main(NOW) == 1
    assert sent


@pytest.mark.parametrize('calendar_result', ['holiday', 'closed', 'open', 'read_error', 'wrong_timezone'])
def test_closure_requires_calendar_evidence(monkeypatch, calendar_result):
    from types import SimpleNamespace
    import sys
    class Calendar:
        tz = 'UTC' if calendar_result == 'wrong_timezone' else 'America/New_York'
        def schedule(self, **kw):
            if calendar_result == 'read_error':
                raise RuntimeError('calendar unavailable')
            return SimpleNamespace(index=[] if calendar_result == 'holiday' else [1], iterrows=lambda: iter([(1, {'market_open': '2026-10-05T11:00:00Z' if calendar_result == 'open' else '2026-10-05T13:30:00Z', 'market_close': '2026-10-05T20:00:00Z'})]))
        def open_at_time(self, schedule, when, **kw):
            return calendar_result == 'open'
    monkeypatch.setitem(sys.modules, 'pandas_market_calendars', SimpleNamespace(get_calendar=lambda name: Calendar()))
    assert heartbeat._market_closed_by_calendar(report()) is (calendar_result in ['holiday', 'closed'])


def test_accepted_report_does_not_cover_incomplete_listing_or_read(monkeypatch):
    sent = mock_heartbeat(monkeypatch, report())
    monkeypatch.setattr(heartbeat, '_report_globs', lambda *args: ['gs://mock/good', 'gs://mock/bad'])
    def listing(glob, **kw):
        if glob.endswith('bad'):
            raise RuntimeError('list failed')
        return [{'url': 'gs://mock/ok.json', 'metadata': {'updated': NOW.isoformat()}}]
    monkeypatch.setattr(heartbeat, '_list_gcs_objects', listing)
    assert heartbeat.main(NOW) == 1
    assert sent


def test_drill_preview_retains_records_without_sending(monkeypatch, capsys):
    sent = mock_drill(monkeypatch, None)
    monkeypatch.setenv('DRILL_DIGEST_PREVIEW', 'true')
    assert drill.main(NOW) == 0
    assert not sent
    assert 'alerts=1' in capsys.readouterr().out


def test_unscoped_mixed_services_unknown_is_visible(monkeypatch):
    sent = mock_heartbeat(monkeypatch, report())
    monkeypatch.setattr(heartbeat, '_list_gcs_objects', lambda *args, **kw: [
        {'url': 'gs://mock/a.json', 'metadata': {'updated': NOW.isoformat()}},
        {'url': 'gs://mock/b.json', 'metadata': {'updated': (NOW - dt.timedelta(minutes=2)).isoformat()}}])
    def read(uri, **kw):
        payload = report()
        if uri.endswith('b.json'):
            payload['service_name'] = 'other-service'
            payload['summary']['execution_status'] = 'unknown'
        return payload
    monkeypatch.setattr(heartbeat, '_cat_gcs_json', read)
    assert heartbeat.main(NOW) == 0
    assert len(sent) == 1
    assert 'unknown' in sent[0]


def test_conflicting_latest_identity_cannot_be_hidden_by_old_healthy(monkeypatch):
    sent = mock_heartbeat(monkeypatch, report())
    monkeypatch.setenv('RUNTIME_HEARTBEAT_ACCOUNT_SCOPE', TARGET.scope)
    monkeypatch.setattr(heartbeat, '_resolve_required_services', lambda **kw: ([TARGET.service], None, False))
    monkeypatch.setattr(heartbeat, '_list_gcs_objects', lambda *args, **kw: [
        {'url': 'gs://mock/new.json', 'metadata': {'updated': NOW.isoformat()}},
        {'url': 'gs://mock/old.json', 'metadata': {'updated': (NOW - dt.timedelta(minutes=2)).isoformat()}}])
    def read(uri, **kw):
        payload = report()
        if uri.endswith('new.json'):
            payload['account_scope'] = 'wrong-scope'
        return payload
    monkeypatch.setattr(heartbeat, '_cat_gcs_json', read)
    assert heartbeat.main(NOW) == 1
    assert len(sent) == 1
    assert 'conflicting report identity' in sent[0]


@pytest.mark.parametrize('value', [True, 1, 'bad'])
def test_malformed_nested_error_remains_alertable(monkeypatch, value):
    payload = report()
    payload['error_summary'] = {'errors': value}
    sent = mock_heartbeat(monkeypatch, payload)
    assert heartbeat.main(NOW) == 1
    assert sent


@pytest.mark.parametrize('value', [None, [], 'bad'])
def test_incomplete_summary_does_not_crash_before_visibility(monkeypatch, value):
    payload = report()
    payload['summary'] = value
    sent = mock_heartbeat(monkeypatch, payload)
    assert heartbeat.main(NOW) == 0
    assert sent


@pytest.mark.parametrize('field', ['strategy_plugin_error', 'strategy_plugin_alert_error', 'strategy_run_persistence_error', 'market_hours_check_error'])
def test_actual_producer_error_field_stays_visible(monkeypatch, field):
    payload = report()
    payload['diagnostics'][field] = 'mock failure'
    sent = mock_heartbeat(monkeypatch, payload)
    assert heartbeat.main(NOW) == 1
    assert sent


@pytest.mark.parametrize('stage', ['UNKNOWN', 'PENDING', 'PARTIAL'])
def test_drill_unrecognized_stage_not_quiet(monkeypatch, stage):
    payload = report()
    payload['stage'] = stage
    sent = mock_drill(monkeypatch, payload)
    assert drill.main(NOW) == 1
    assert sent


@pytest.mark.parametrize('outcome', ['reconciliation_required', 'failed', 'risk_blocked', 'filled', 'submitted'])
def test_receipt_fact_is_preserved_and_controls_health_disposition(monkeypatch, outcome):
    from quant_platform_kit.common.execution_receipts import build_execution_receipt
    payload = report()
    payload['platform'] = 'interactive_brokers'
    payload['execution_receipt'] = build_execution_receipt(platform='ibkr', strategy_profile=TARGET.profile, strategy_revision='a' * 40, execution_mode='paper', outcome=outcome, observed_at=NOW, **({'broker_confirmation': 'not_observed'} if outcome == 'failed' else {}))
    original = copy.deepcopy(payload['execution_receipt'])
    sent = mock_heartbeat(monkeypatch, payload)
    assert heartbeat.main(NOW) == 0
    assert bool(sent) is (outcome in ['reconciliation_required', 'failed', 'risk_blocked'])
    assert payload['execution_receipt'] == original


@pytest.mark.parametrize('summary', [{'execution_status': 'submitted', 'orders_submitted_count': 1}, {'execution_status': 'pending', 'orders_submitted_count': 0}, {'orders_pending_count': 1}, {'orders_submitted': ['mock']}])
def test_conflicting_closed_report_stays_visible(monkeypatch, summary):
    payload = report()
    payload.update(status='skipped', summary=summary, diagnostics={'skip_reason': 'market_closed'})
    sent = mock_heartbeat(monkeypatch, payload)
    monkeypatch.setattr(heartbeat, '_market_closed_by_calendar', lambda payload: True)
    assert heartbeat.main(NOW) == 0
    assert sent


@pytest.mark.parametrize('inventory', [['bad'], [{'drill_precheck_enabled': 'true'}], [{'drill_precheck_enabled': 1}]])
def test_invalid_inventory_not_treated_as_empty(monkeypatch, inventory):
    monkeypatch.setenv('CLOUD_RUN_SERVICE_TARGETS_JSON', json.dumps(inventory))
    with pytest.raises(ValueError):
        LOAD_DRILL_TARGETS()


def test_newest_healthy_report_restores_quiet_after_old_unknown(monkeypatch):
    sent = mock_heartbeat(monkeypatch, report())
    monkeypatch.setattr(heartbeat, '_list_gcs_objects', lambda *args, **kw: [
        {'url': 'gs://mock/new.json', 'metadata': {'updated': NOW.isoformat()}},
        {'url': 'gs://mock/old.json', 'metadata': {'updated': (NOW - dt.timedelta(minutes=2)).isoformat()}}])
    def read(uri, **kw):
        payload = report()
        if uri.endswith('old.json'):
            payload['summary']['execution_status'] = 'unknown'
        return payload
    monkeypatch.setattr(heartbeat, '_cat_gcs_json', read)
    assert heartbeat.main(NOW) == 0
    assert not sent


def test_funding_guard_is_visible_even_with_top_level_ok(monkeypatch):
    payload = report()
    payload['stage'] = 'FUNDING_BLOCKED'
    sent = mock_heartbeat(monkeypatch, payload)
    assert heartbeat.main(NOW) == 0
    assert sent


@pytest.mark.parametrize('stage', ['PARTIAL_SUBMITTED', 'SUBMITTED', 'FUNDING_BLOCKED', 'ORDERS_PLANNED'])
def test_drill_accepted_business_stage_is_not_complete_zero_action(monkeypatch, stage):
    payload = report()
    payload['stage'] = stage
    sent = mock_drill(monkeypatch, payload)
    assert drill.main(NOW) == 1
    assert sent


@pytest.mark.parametrize('outcome', ['filled', 'submitted', 'broker_acknowledged', 'partially_filled'])
def test_drill_action_receipt_conflicts_with_dry_run_zero_count(monkeypatch, outcome):
    from quant_platform_kit.common.execution_receipts import build_execution_receipt
    payload = report()
    payload['platform'] = 'interactive_brokers'
    payload['execution_receipt'] = build_execution_receipt(platform='ibkr', strategy_profile=TARGET.profile, strategy_revision='a' * 40, execution_mode='live', outcome=outcome, observed_at=NOW)
    sent = mock_drill(monkeypatch, payload)
    assert drill.main(NOW) == 1
    assert sent


def test_missing_timezone_evidence_is_visible(monkeypatch):
    payload = report()
    payload['started_at'] = '2026-10-05T12:00:00'
    sent = mock_heartbeat(monkeypatch, payload)
    assert heartbeat.main(NOW) == 0
    assert sent


def test_closure_crossing_open_cannot_be_quiet(monkeypatch):
    import sys
    from types import SimpleNamespace
    class Calendar:
        tz = 'America/New_York'
        def schedule(self, **kw):
            return SimpleNamespace(index=[1], iterrows=lambda: iter([(1, {'market_open': '2026-10-05T13:30:00Z', 'market_close': '2026-10-05T20:00:00Z'})]))
    monkeypatch.setitem(sys.modules, 'pandas_market_calendars', SimpleNamespace(get_calendar=lambda name: Calendar()))
    payload = report()
    payload['finished_at'] = '2026-10-05T13:31:00Z'
    assert not heartbeat._market_closed_by_calendar(payload)


def test_target_qualified_current_health_is_not_overridden_by_old_wrong_scope(monkeypatch):
    sent = mock_heartbeat(monkeypatch, report())
    target = {'service': TARGET.service, 'strategy_profile': TARGET.profile, 'account_scope': TARGET.scope}
    monkeypatch.setattr(heartbeat, 'load_runtime_targets', lambda env: [target])
    monkeypatch.setattr(heartbeat, 'filter_due_targets', lambda *args, **kw: ([target], True))
    monkeypatch.setattr(heartbeat, '_list_gcs_objects', lambda *args, **kw: [
        {'url': 'gs://mock/new.json', 'metadata': {'updated': NOW.isoformat()}},
        {'url': 'gs://mock/old.json', 'metadata': {'updated': (NOW - dt.timedelta(minutes=2)).isoformat()}}])
    def read(uri, **kw):
        payload = report()
        if uri.endswith('old.json'):
            payload['account_scope'] = 'wrong-scope'
        return payload
    monkeypatch.setattr(heartbeat, '_cat_gcs_json', read)
    assert heartbeat.main(NOW) == 0
    assert not sent


@pytest.mark.parametrize('unreadable_is_newer', [False, True])
def test_only_current_window_unreadable_can_override_complete_exact_targets(monkeypatch, unreadable_is_newer):
    sent = mock_heartbeat(monkeypatch, report())
    target = {'service': TARGET.service, 'strategy_profile': TARGET.profile, 'account_scope': TARGET.scope}
    monkeypatch.setattr(heartbeat, 'load_runtime_targets', lambda env: [target])
    monkeypatch.setattr(heartbeat, 'filter_due_targets', lambda *args, **kw: ([target], True))
    bad_time = NOW if unreadable_is_newer else NOW - dt.timedelta(minutes=4)
    monkeypatch.setattr(heartbeat, '_list_gcs_objects', lambda *args, **kw: [
        {'url': 'gs://mock/good.json', 'metadata': {'updated': (NOW - dt.timedelta(minutes=2)).isoformat()}},
        {'url': 'gs://mock/bad.json', 'metadata': {'updated': bad_time.isoformat()}}])
    monkeypatch.setattr(heartbeat, '_cat_gcs_json', lambda uri, **kw: None if uri.endswith('bad.json') else report())
    assert heartbeat.main(NOW) == (1 if unreadable_is_newer else 0)
    assert bool(sent) is unreadable_is_newer


@pytest.mark.parametrize('scheduler_status', [{}, {'code': 0}, None, [], {'code': 1}, {'code': False}])
def test_scheduler_proto_default_code_is_not_a_missing_attempt(monkeypatch, scheduler_status):
    sent = mock_drill(monkeypatch, report())
    original_job = drill._job
    def job(name):
        result = original_job(name)
        if name == TARGET.precheck_job:
            if scheduler_status is None:
                result.pop('status')
            else:
                result['status'] = scheduler_status
        return result
    monkeypatch.setattr(drill, '_job', job)
    success = scheduler_status == {} or (isinstance(scheduler_status, dict) and type(scheduler_status.get('code')) is int and scheduler_status['code'] == 0)
    assert drill.main(NOW) == (0 if success else 1)
    assert bool(sent) is not success


@pytest.mark.parametrize('kind', ['unknown', 'incomplete', 'reconciliation_required', 'persistence_error'])
def test_unconfirmed_check_message_never_claims_normal_or_trade_failure(monkeypatch, kind):
    payload = report()
    if kind == 'incomplete':
        payload['finished_at'] = None
    elif kind == 'persistence_error':
        payload['diagnostics']['strategy_run_persistence_error'] = 'mock failure'
    else:
        payload['summary']['execution_status'] = kind
    sent = mock_heartbeat(monkeypatch, payload)
    assert heartbeat.main(NOW) == (1 if kind == 'persistence_error' else 0)
    assert sent
    assert heartbeat._notice('status_normal') not in sent[0]
    assert '✅' not in sent[0]
    assert '交易失败' not in sent[0]


def test_confirmed_manual_opt_in_uses_existing_normal_formatter(monkeypatch):
    monkeypatch.setenv('RUNTIME_HEARTBEAT_NOTIFY_ON_SUCCESS', 'true')
    sent = mock_heartbeat(monkeypatch, report())
    assert heartbeat.main(NOW) == 0
    assert heartbeat._notice('status_normal') in sent[0]


@pytest.mark.parametrize('due_at', [None, NOW - dt.timedelta(hours=2), NOW - dt.timedelta(minutes=30)])
def test_reuploaded_old_payload_is_visible_only_against_existing_precise_due(monkeypatch, due_at):
    sent = mock_heartbeat(monkeypatch, report())
    target = {'service': TARGET.service, 'strategy_profile': TARGET.profile, 'account_scope': TARGET.scope}
    if due_at is not None:
        target['_heartbeat_latest_due_at'] = due_at
    monkeypatch.setattr(heartbeat, 'load_runtime_targets', lambda env: [target])
    monkeypatch.setattr(heartbeat, 'filter_due_targets', lambda *args, **kw: ([target], True))
    assert heartbeat.main(NOW) == 0
    stale = due_at is not None and due_at > dt.datetime(2026, 10, 5, 12, 1, tzinfo=dt.timezone.utc)
    assert bool(sent) is stale
    if stale:
        assert heartbeat._notice('status_normal') not in sent[0]
        assert 'predates latest due' in sent[0]


@pytest.mark.parametrize('count', ['missing', False, '0', 0])
def test_market_closed_drill_requires_explicit_strict_zero_count(monkeypatch, count):
    payload = report()
    summary = {} if count == 'missing' else {'orders_submitted_count': count}
    payload.update(status='skipped', diagnostics={'skip_reason': 'market_closed'}, summary=summary)
    sent = mock_drill(monkeypatch, payload)
    success = type(count) is int and count == 0
    assert drill.main(NOW) == (0 if success else 1)
    assert bool(sent) is not success
