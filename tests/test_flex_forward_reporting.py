"""Synthetic forward-report regressions; no collector transport or cloud client."""
import copy
import json
from datetime import date, datetime, timezone
from xml.etree import ElementTree as ET

import pytest

from application.flex_daily import runner, state
from application.flex_daily.__main__ import pull_outcome
from application.flex_daily.storage import Conflict
from tests.test_flex_daily_cloud import Memory, config, report

START = '2026-11-03'
NOW = datetime(2026, 11, 4, 22, tzinfo=timezone.utc)


def forward_config():
    c = config()
    c.update(forward_start=START, snapshot_time_field='nativeAsOf',
             snapshot_timezone_field='nativeZone',
             event_time_fields={'CashTransactions': 'dateTime', 'Trades': 'dateTime'})
    c['accounts'] = {'U00001': 'account-1'}
    return c


def native_report(day=START, cutoff='2026-11-03T16:00:00-05:00', events=()):
    root = ET.fromstring(report(day, day))
    s = root.find('.//FlexStatement')
    s.set('nativeAsOf', cutoff)
    s.set('nativeZone', 'America/New_York')
    for section, attrs in events:
        container = s.find(section)
        tag = {'CashTransactions': 'CashTransaction', 'Trades': 'Trade'}[section]
        ET.SubElement(container, tag, **attrs)
    return ET.tostring(root)


def cash_event(time, identity='synthetic-event', **extra):
    return ('CashTransactions', dict(currency='USD', amount='0', type='Settlement',
                                    dateTime=time, transactionID=identity, **extra))


def collect(store=None, content=None, c=None, now=NOW):
    store = store or Memory()
    content = content or native_report()
    result = runner.collect(store, c or forward_config(), lambda *_: content, now=lambda: now)
    return store, result


def test_forward_seed_restart_keeps_history_but_excludes_it_from_queue_summary():
    store = Memory()
    old = {'schema': 1, 'days': {'2026-11-02': {'accounts': {'account-1': {'status': 'missing', 'issues': ['old']}}}},
           'months': {'2026-10-3': {'reconciliation_status': 'difference'}}}
    store.put('state/coverage.json', old)
    calls = []
    runner.collect(store, forward_config(), lambda a, b: calls.append((a, b)) or native_report(), now=lambda: NOW)
    coverage, _ = store.json('state/coverage.json')
    assert calls == [(START, START)]
    assert coverage['days']['2026-11-02'] == old['days']['2026-11-02']
    safe, _ = store.json('status/latest.json')
    assert safe['target_date'] == START
    assert safe['oldest_gap'] == START
    assert not safe['residual_nonzero']
    runner.collect(store, forward_config(), lambda a, b: calls.append((a, b)) or native_report(), now=lambda: NOW)
    assert all(a >= START for a, b in calls)


def test_first_forward_day_queues_nothing_and_no_history():
    calls = []
    now = datetime(2026, 11, 3, 22, tzinfo=timezone.utc)
    result = runner.collect(Memory(), forward_config(), lambda *a: calls.append(a), now=lambda: now)
    assert calls == []
    assert result['target_date'] is None
    assert result['forward_validation'] == 'NOT_DUE'


def test_forward_months_exclude_partial_november_and_old_revision():
    s = {'days': {}, 'months': {}, 'backfill_start': '2026-01-01', 'forward_start': START}
    state.seed(s, date(2026, 11, 2), date(2027, 1, 11), ['account-1'])
    items = state.windows(s, date(2027, 1, 11), datetime(2027, 1, 12, tzinfo=timezone.utc))
    assert all(start >= START for _, start, _ in items)
    assert not any(kind != 'daily' and start < '2026-12-01' for kind, start, end in items)
    assert ('2026-12-3', '2026-12-01', '2026-12-31') in items


def test_fetched_invalid_mixed_accounts_is_nonzero_and_no_success_watermark():
    c = forward_config()
    c['accounts']['U00002'] = 'account-2'
    store, result = collect(c=c)
    assert result['last_run']['reports_fetched'] == 1
    assert result['last_run']['windows_succeeded'] == 0
    assert pull_outcome(result) == ('incomplete', 3)
    assert result['forward_validation'] == 'UNRECONCILED'
    coverage, _ = store.json('state/coverage.json')
    assert 'validated_through' not in coverage
    assert coverage['days'][START]['accounts']['account-2']['status'] == 'missing'


def evidence(store, day=START):
    coverage, _ = store.json('state/coverage.json')
    slot = coverage['days'][day]
    projection = json.loads(store.read(slot['derived'])[0])
    return projection['forward']['account-1']


def test_generation_is_not_asof_and_public_output_contains_no_native_values():
    private = 'synthetic-private-timestamp'
    content = native_report(cutoff=private)
    store, result = collect(content=content)
    ev = evidence(store)
    assert ev['source_as_of'] is None
    assert 'snapshot_time_unqualified' in ev['issues']
    assert private not in json.dumps(result)
    assert 'U00001' not in json.dumps(result)
    assert result['reconciliation_status'] == 'UNRECONCILED'


def test_first_anchor_preserves_actual_time_and_missing_opening():
    store, result = collect()
    ev = evidence(store)
    assert ev['source_as_of']['utc'] == '2026-11-03T21:00:00+00:00'
    assert ev['capture_started_at'] == NOW.isoformat()
    assert ev['capture_finished_at'] == NOW.isoformat()
    assert ev['anchor_qualified']
    assert 'opening_snapshot_missing' in ev['issues']
    assert ev['cash'][0]['definition'] == 'endingCash'
    assert ev['cash'][0]['settled'] is None
    assert pull_outcome(result) == ('incomplete', 3)


def two_days(events=(), cutoff='2026-11-04T16:00:00-05:00'):
    store, _ = collect()
    content = native_report('2026-11-04', cutoff, events)
    tomorrow = datetime(2026, 11, 5, 22, tzinfo=timezone.utc)
    # Already observed first day must not be rewritten by this synthetic fetch.
    def fetch(start, end):
        return native_report() if start == START else content
    result = runner.collect(store, forward_config(), fetch, now=lambda: tomorrow)
    return store, result


@pytest.mark.parametrize('time,expected', [
    ('2026-11-02T23:00:00-05:00', 'excluded_pre_start'),
    ('2026-11-03T16:00:00-05:00', 'excluded_before_or_at_previous'),
    ('2026-11-03T16:00:01-05:00', 'selected'),
    ('2026-11-04T16:00:00-05:00', 'selected'),
    ('2026-11-04T16:00:01-05:00', 'excluded_after_current'),
    ('20261104', 'unknown'), ('', 'unknown'),
])
def test_actual_cutoff_selection(time, expected):
    store, _ = two_days([cash_event(time)])
    ev = evidence(store, '2026-11-04')
    assert ev['events'][0]['eligibility'] == expected
    assert ev['previous_actual_cutoff'] == '2026-11-03T21:00:00+00:00'


def test_late_settlement_keeps_trade_and_posting_facts_separate():
    store, _ = two_days([cash_event('2026-11-04T10:00:00-05:00', tradeDate='20261102',
                                   settleDate='20261104', reportDate='20261104')])
    row = evidence(store, '2026-11-04')['events'][0]
    assert row['eligibility'] == 'selected'
    assert row['facts']['tradeDate'] == '20261102'
    assert row['facts']['settleDate'] == '20261104'


@pytest.mark.parametrize('cutoff', ['2026-11-04', '2026-11-04T16:00:00',
                                   '2026-11-04T16:00:00+03:00', '2026-11-06T16:00:00-05:00'])
def test_unknown_timezone_or_future_snapshot_cannot_anchor(cutoff):
    store, _ = two_days(cutoff=cutoff)
    assert not evidence(store, '2026-11-04')['anchor_qualified']


def test_missing_currency_and_unsupported_instrument_stay_unknown():
    event = cash_event('2026-11-04T10:00:00-05:00')
    event[1]['currency'] = ''
    trade = ('Trades', dict(tradeID='synthetic-trade', originalTradeID='', conid='1', currency='USD',
                            quantity='1', levelOfDetail='EXECUTION', reportDate='20261104',
                            dateTime='2026-11-04T10:00:00-05:00', assetCategory='OPT'))
    store, _ = two_days([event, trade])
    ev = evidence(store, '2026-11-04')
    assert all(e['eligibility'] == 'unknown' for e in ev['events'])


def test_revision_and_duplicate_identity_not_silently_recounted():
    event = cash_event('2026-11-04T10:00:00-05:00')
    store, _ = two_days([event, copy.deepcopy(event)])
    ev = evidence(store, '2026-11-04')
    assert 'event_identity_duplicate' in ev['issues']
    corrected = cash_event('2026-11-04T10:00:00-05:00', originalTransactionID='synthetic-old')
    store, _ = two_days([corrected])
    assert evidence(store, '2026-11-04')['events'][0]['eligibility'] == 'unknown'


def test_failed_revalidation_revokes_old_complete_status_after_restart():
    store, _ = collect()
    coverage, gen = store.json('state/coverage.json')
    coverage['days'][START]['accounts']['account-1']['status'] = 'complete'
    coverage['days'][START].pop('next_attempt_at', None)
    store.put('state/coverage.json', coverage, gen)
    def invalid(*_):
        raise ValueError('synthetic-private-message')
    result = runner.collect(store, forward_config(), invalid, now=lambda: NOW)
    after, _ = store.json('state/coverage.json')
    assert after['days'][START]['accounts']['account-1']['status'] != 'complete'
    assert not after['days'][START].get('forward')
    assert pull_outcome(result) == ('incomplete', 3)
    assert 'synthetic-private-message' not in json.dumps(result)


def test_lease_loss_after_fetch_cannot_upload_or_commit_forward_evidence():
    store = Memory()
    def stolen(*_):
        lease, gen = store.json('state/pull-lease.json')
        lease['owner'] = 'synthetic-other-owner'
        store.put('state/pull-lease.json', lease, gen)
        return native_report()
    with pytest.raises(Conflict):
        runner.collect(store, forward_config(), stolen, now=lambda: NOW)
    assert not any(k.startswith(('raw/', 'derived/')) for k in store.values)
    coverage, _ = store.json('state/coverage.json')
    assert not state.complete(coverage['days'][START])


def test_cross_observation_revision_cannot_reuse_a_native_identity():
    event = cash_event('2026-11-04T10:00:00-05:00')
    store, _ = two_days([event])
    revised = cash_event('2026-11-04T10:00:00-05:00')
    revised[1]['amount'] = '1'
    now = datetime(2026, 11, 6, 22, tzinfo=timezone.utc)
    def fetch(start, end):
        if start == START:
            return native_report()
        if start == '2026-11-04':
            return native_report(start, '2026-11-04T16:00:00-05:00', [event])
        return native_report(start, '2026-11-05T16:00:00-05:00', [revised])
    runner.collect(store, forward_config(), fetch, now=lambda: now)
    ev = evidence(store, '2026-11-05')
    assert ev['events'][0]['eligibility'] == 'unknown'
    assert 'event_revision_unqualified' in ev['issues']


def test_later_anchor_never_fills_missing_first_day_or_reads_prestart_raw():
    store = Memory()
    old = {'schema': 1, 'days': {'2026-11-02': {'accounts': {'account-1': {'status': 'complete'}},
                                              'raw': 'raw/forbidden-history.xml'}}, 'months': {}}
    store.put('state/coverage.json', old)
    now = datetime(2026, 11, 6, 22, tzinfo=timezone.utc)
    def fetch(start, end):
        if start == START:
            raise ValueError('missing')
        return native_report(start, start + 'T16:00:00-05:00')
    original_read = store.read
    def read(key):
        assert key != 'raw/forbidden-history.xml'
        return original_read(key)
    store.read = read
    result = runner.collect(store, forward_config(), fetch, now=lambda: now)
    ev = evidence(store, '2026-11-04')
    assert ev['anchor_qualified']
    assert 'opening_snapshot_missing' in ev['issues']
    assert result['oldest_gap'] == START
    assert result['continuous_through']['account-1'] is None


def test_unknown_effective_semantics_and_invalid_settled_cash_fail_closed():
    c = forward_config()
    c.pop('event_time_fields')
    content = native_report(events=[cash_event('2026-11-03T17:00:00-05:00')])
    store, _ = collect(content=content, c=c)
    assert evidence(store)['events'][0]['eligibility'] == 'unknown'
    root = ET.fromstring(native_report())
    root.find('.//CashReportCurrency').set('endingSettledCash', 'NaN')
    store, _ = collect(content=ET.tostring(root))
    assert not evidence(store)['anchor_qualified']
    assert 'settled_cash_unqualified' in evidence(store)['issues']


def test_forward_cannot_restart_in_legacy_mode():
    store, _ = collect()
    with pytest.raises(ValueError, match='forward_configuration_required'):
        runner.collect(store, config(), lambda *_: pytest.fail('must not fetch'), now=lambda: NOW)


@pytest.mark.parametrize('field', ['whenGenerated', 'toDate', 'fromDate'])
def test_generation_or_period_field_cannot_be_configured_as_snapshot(field):
    c = forward_config()
    c['snapshot_time_field'] = field
    with pytest.raises(ValueError, match='snapshot_field_not_financial_asof'):
        collect(c=c)


def test_changed_prior_snapshot_revokes_later_interval_watermark():
    store, _ = two_days()
    coverage, gen = store.json('state/coverage.json')
    coverage['days']['2026-11-04']['validated_at'] = NOW.isoformat()
    coverage['days'][START].pop('next_attempt_at', None)
    store.put('state/coverage.json', coverage, gen)
    changed = native_report(cutoff='2026-11-03T17:00:00-05:00')
    def fetch(start, end):
        return changed if start == START else native_report(start, start + 'T16:00:00-05:00')
    runner.collect(store, forward_config(), fetch, now=lambda: datetime(2026, 11, 5, 22, tzinfo=timezone.utc))
    coverage, _ = store.json('state/coverage.json')
    later = coverage['days']['2026-11-04']
    assert 'validated_at' not in later
    assert later['accounts']['account-1']['status'] == 'pending'
    assert not later.get('forward')


def test_mixed_account_failure_preserves_independently_valid_account():
    store, _ = collect()
    c = forward_config()
    c['accounts']['U00002'] = 'account-2'
    now = datetime(2026, 11, 5, 22, tzinfo=timezone.utc)
    def fetch(start, end):
        return native_report(start, start + 'T16:00:00-05:00')
    result = runner.collect(store, c, fetch, now=lambda: now)
    coverage, _ = store.json('state/coverage.json')
    current = coverage['days']['2026-11-04']['accounts']
    assert current['account-1']['status'] == 'complete'
    assert current['account-2']['status'] == 'missing'
    assert pull_outcome(result) == ('incomplete', 3)


def test_successful_attempt_is_distinct_from_initial_gap_and_financial_acceptance():
    store, _ = collect()
    coverage, gen = store.json('state/coverage.json')
    coverage['days'][START]['next_attempt_at'] = '2026-11-10T00:00:00+00:00'
    store.put('state/coverage.json', coverage, gen)
    now = datetime(2026, 11, 5, 22, tzinfo=timezone.utc)
    result = runner.collect(store, forward_config(),
                            lambda a, b: native_report(a, a + 'T16:00:00-05:00'), now=lambda: now)
    assert result['forward_validation'] == 'VALIDATED'
    assert pull_outcome(result) == ('finished', 0)
    assert result['oldest_gap'] == START
    assert result['reconciliation_status'] == 'UNRECONCILED'
    after, _ = store.json('state/coverage.json')
    assert after['days']['2026-11-04']['validated_at'] == now.isoformat()


def test_native_account_rebinding_cannot_reuse_other_accounts_anchor():
    store, _ = collect()
    c = forward_config()
    c['accounts'] = {'U00002': 'account-1'}
    root = ET.fromstring(native_report('2026-11-04', '2026-11-04T16:00:00-05:00'))
    root.find('.//FlexStatement').set('accountId', 'U00002')
    now = datetime(2026, 11, 5, 22, tzinfo=timezone.utc)
    # Old window is not due; the native account on the new response is synthetic.
    coverage, gen = store.json('state/coverage.json')
    coverage['days'][START]['next_attempt_at'] = '2026-11-10T00:00:00+00:00'
    store.put('state/coverage.json', coverage, gen)
    runner.collect(store, c, lambda *_: ET.tostring(root), now=lambda: now)
    ev = evidence(store, '2026-11-04')
    assert ev['previous_actual_cutoff'] is None
    assert 'opening_snapshot_missing' in ev['issues']
