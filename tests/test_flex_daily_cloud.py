import copy
from datetime import date, datetime, timedelta, timezone
from xml.etree import ElementTree as ET

import pytest

from application.flex_daily import checks, runner, state, watch
from application.flex_daily.storage import Conflict, Lease
from application.ibkr_flex_source import FlexSourceError


class Memory:
    def __init__(self):
        self.values = {}
        self.generation = 0

    def json(self, key, default=None):
        value, gen = self.values.get(key, (default, 0))
        return copy.deepcopy(value), gen

    def put(self, key, value, generation=0):
        _, actual = self.values.get(key, (None, 0))
        if actual != generation:
            raise Conflict('stale')
        self.generation += 1
        self.values[key] = (copy.deepcopy(value), self.generation)
        return self.generation

    def read(self, key):
        return copy.deepcopy(self.values.get(key, (None, 0)))

    def immutable(self, key, body):
        if key in self.values:
            assert self.values[key][0] == body
        else:
            self.put(key, body)


def config():
    return {'schema': 1, 'ip_restriction': 'none', 'accounts': {'U00001': 'account-1', 'U00002': 'account-2'},
            'query_id': '123', 'backfill_start': '2026-10-01',
            'configured_sections': list(checks.FIELDS) + ['ChangeInNAV']}


def report(start='2026-10-31', end='2026-10-31', account='U00001'):
    root = ET.Element('FlexQueryResponse', type='AF')
    statements = ET.SubElement(root, 'FlexStatements')
    statement = ET.SubElement(statements, 'FlexStatement', accountId=account, fromDate=start, toDate=end,
                              whenGenerated='20261101;101700')
    for section, (tag, fields) in checks.FIELDS.items():
        container = ET.SubElement(statement, section)
        if section in {'CashReport', 'EquitySummaryInBase', 'InterestAccruals', 'ConversionRates'}:
            row = ET.SubElement(container, tag, **{field: '0' for field in fields.split()})
            row.set('currency', 'USD')
            for field in ('reportDate', 'fromDate', 'toDate'):
                if field in row.attrib:
                    row.set(field, (start if field == 'fromDate' else end).replace('-', ''))
            if section == 'CashReport':
                row.attrib.update({k: '0' for k in checks.CASH_FIELDS})
    ET.SubElement(statement, 'ChangeInNAV', fromDate=start.replace('-', ''), toDate=end.replace('-', ''), currency='USD', **{k: '0' for k in checks.NAV_FIELDS + ['startingValue', 'endingValue', 'realized', 'changeInUnrealized', 'forexCommissions']})
    return ET.tostring(root)


def test_queue_keeps_old_holes_and_never_uses_monthly_as_daily():
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    s = {'days': {}, 'months': {}}
    state.seed(s, date(2026, 10, 1), date(2026, 10, 31), ['account-1'])
    result = state.windows(s, date(2026, 10, 31), now)
    assert result[0] == ('daily', '2026-10-31', '2026-10-31')
    assert ('daily', '2026-10-01', '2026-10-01') in result
    assert len(result) == 30
    assert state.summary(s, now)['continuous_through']['account-1'] is None
    s['months']['2026-10-3'] = {'done': True}
    assert not state.complete(s['days']['2026-10-01'])


def test_missed_month_checkpoint_is_caught_up():
    s = {'days': {}, 'months': {}}
    state.seed(s, date(2026, 9, 1), date(2026, 10, 15), ['account-1'])
    items = state.windows(s, date(2026, 10, 15), datetime(2026, 10, 16, tzinfo=timezone.utc))
    assert ('2026-09-3', '2026-09-01', '2026-09-30') in items
    assert ('2026-09-10', '2026-09-01', '2026-09-30') in items


def test_failure_backoff_and_ny_date():
    now = datetime(2026, 11, 1, 1, tzinfo=timezone.utc)
    assert state.target_day(now) == date(2026, 10, 30)
    slot = {}
    for _ in range(3):
        state.failed(slot, now, 'missing')
    assert not state.due(slot, now + timedelta(hours=23))
    assert state.due(slot, now + timedelta(hours=24))


def test_missing_account_and_section_never_complete():
    c = config()
    result = checks.inspect_report(report(), '2026-10-31', '2026-10-31', c['accounts'], c['configured_sections'])
    assert result['account-2']['status'] == 'missing'
    assert result['account-1']['reconciliation_status'] == 'partial'
    root = ET.fromstring(report())
    statement = root.find('./FlexStatements/FlexStatement')
    statement.remove(statement.find('Transfers'))
    result = checks.inspect_report(ET.tostring(root), '2026-10-31', '2026-10-31', c['accounts'], c['configured_sections'])
    assert result['account-1']['status'] == 'received'


@pytest.mark.parametrize('start,account', [('2026-10-30', 'U00001'), ('2026-10-31', 'U99999')])
def test_scope_or_period_rejected(start, account):
    c = config()
    with pytest.raises(ValueError):
        checks.inspect_report(report(start=start, account=account), '2026-10-31', '2026-10-31', c['accounts'], c['configured_sections'])


def test_nonzero_decimal_residual_never_matched():
    root = ET.fromstring(report())
    root.find('.//ChangeInNAV').set('endingValue', '0.00000000001')
    c = config()
    result = checks.inspect_report(ET.tostring(root), '2026-10-31', '2026-10-31', c['accounts'], c['configured_sections'])
    assert result['account-1']['reconciliation_status'] == 'difference'
    assert result['account-1']['residuals']['nav'] == '1E-11'


def test_lease_rejects_overlap_and_fences_stale_owner():
    store = Memory()
    now = datetime.now(timezone.utc)
    first = Lease(store, 'state/lease', now)
    with pytest.raises(Conflict):
        Lease(store, 'state/lease', now)
    Lease(store, 'state/lease', now + timedelta(minutes=32))
    with pytest.raises(Conflict):
        first.check(now)


@pytest.mark.parametrize('code', ['1012', '1013', '1015', '1018', '1001'])
def test_provider_stop_persists_holes(code):
    store = Memory()
    calls = []
    def fetch(start, end):
        calls.append(start)
        raise FlexSourceError('never log', endpoint='SendRequest', category='provider', code=code)
    result = runner.collect(store, config(), fetch)
    assert len(calls) == 1
    assert result['missing_account_days'] > 0
    assert bool(result['blocked']) == (code in runner.BLOCKING)
    if code == '1013':
        runner.collect(store, config(), fetch)
        assert len(calls) == 1


def test_ip_unknown_never_calls_provider():
    c = config()
    c['ip_restriction'] = 'unknown'
    result = runner.collect(Memory(), c, lambda *_: pytest.fail('broker called'))
    assert result['blocked']


def test_collector_resume_and_raw_write_before_coverage(monkeypatch):
    store = Memory()
    calls = []
    def fetch(start, end):
        calls.append(start)
        return report(start, end)
    c = config()
    c['backfill_start'] = state.target_day(datetime.now(timezone.utc)).isoformat()
    first = runner.collect(store, c, fetch)
    assert first['missing_account_days'] == 2
    assert first['continuous_through']['account-1'] == c['backfill_start']
    assert first['continuous_through']['account-2'] is None
    raw = [key for key in store.values if key.startswith('raw/')]
    assert len(raw) == 2
    runner.collect(store, c, fetch)
    assert len([key for key in store.values if key.startswith('raw/')]) == 2


def test_watch_unknown_deduplicates_and_has_no_private_payload():
    store = Memory()
    now = datetime.now(timezone.utc)
    store.put('status/latest.json', {'target_date': '2026-10-31', 'missing_account_days': 2,
                                    'last_finished_at': now.isoformat(), 'residual_nonzero': True})
    sent = []
    def notify(text):
        sent.append(text)
        return {'delivery': 'unknown'}
    assert watch.watch(store, notify, now)['delivery'] == 'unknown'
    assert watch.watch(store, notify, now + timedelta(hours=1))['delivery'] == 'deduplicated'
    assert len(sent) == 1
    assert 'U00001' not in sent[0] and '0.00000000001' not in sent[0]


def test_watch_missing_run_and_expiry():
    now = datetime(2026, 11, 1, 13, tzinfo=timezone.utc)
    reasons = watch.events({'token_expires_at': (now + timedelta(hours=12)).isoformat()}, {}, now)
    assert 'missed_run' in reasons and 'token_expiry_1' in reasons


def test_same_reference_polling_and_minimum_interval(monkeypatch):
    timestamps, handles = [], []
    current = [0]
    def sleep(seconds):
        current[0] += seconds
    handle = object()
    monkeypatch.setattr(runner, 'request_activity_flex_report', lambda **kw: handle)
    def collect(received):
        assert received is handle
        handles.append(received)
        timestamps.append(current[0])
        if len(handles) < 3:
            raise runner.FlexReportPending(handle)
        return type('Report', (), {'content': b'synthetic'})()
    monkeypatch.setattr(runner, 'collect_activity_flex_xml', collect)
    fetch = runner.Fetcher('synthetic', '123', 1500, clock=lambda: current[0], sleep=sleep)
    assert fetch('2026-10-31', '2026-10-31') == b'synthetic'
    assert len(handles) == 3
    assert timestamps == [20, 40, 80]


def test_crash_after_raw_write_does_not_commit_completion():
    class Crash(Memory):
        def immutable(self, key, body):
            super().immutable(key, body)
            if key.startswith('raw/'):
                raise RuntimeError('simulated_crash')
    store = Crash()
    with pytest.raises(RuntimeError):
        runner.collect(store, config(), lambda start, end: report(start, end))
    coverage, _ = store.json('state/coverage.json')
    assert all(not state.complete(slot) for slot in coverage['days'].values())
    assert len([key for key in store.values if key.startswith('raw/')]) == 1


def test_monthly_activity_multisets_preserve_equal_real_trades():
    root = ET.fromstring(report())
    trades = root.find('.//Trades')
    ET.SubElement(trades, 'Trade', tradeID='synthetic-1', quantity='1')
    ET.SubElement(trades, 'Trade', tradeID='synthetic-2', quantity='1')
    both = ET.tostring(root)
    assert checks.compare_activity(both, [both])
    trades.remove(trades[-1])
    assert not checks.compare_activity(both, [ET.tostring(root)])


def test_opening_quantity_residual_is_exact_and_partial():
    prior, current = ET.fromstring(report(start='2026-10-30', end='2026-10-30')), ET.fromstring(report())
    ET.SubElement(prior.find('.//OpenPositions'), 'OpenPosition', conid='1', currency='USD', position='10.1234', levelOfDetail='SUMMARY', reportDate='20261030')
    ET.SubElement(current.find('.//OpenPositions'), 'OpenPosition', conid='1', currency='USD', position='10.1200', levelOfDetail='SUMMARY', reportDate='20261031')
    c = config()
    result = checks.inspect_report(ET.tostring(current), '2026-10-31', '2026-10-31', c['accounts'], c['configured_sections'], ET.tostring(prior))
    assert result['account-1']['residuals']['quantity_observed_0'] == '-0.0034'
    assert result['account-1']['reconciliation_status'] == 'difference'
    assert 'grant_activity' in result['account-1']['manual_evidence_required']


def test_plan_preserves_resource_and_iam_boundaries():
    from scripts.prepare_flex_cloud import plan
    output = plan('synthetic-project', 'asia-southeast1', 'synthetic-bucket', 'synthetic-flex',
                  'registry.invalid/image@sha256:' + '0' * 64, 'synthetic-bot', '1', '2', 'operator@example.invalid')
    assert [s['schedule'] for s in output['schedulers']] == ['17 10 * * *', '47 * * * *']
    assert all(s['body'] == {} for s in output['schedulers'])
    assert all(b['role'] == 'roles/run.invoker' for b in output['job_iam_additions'])
    for binding in output['bucket_iam_additions']:
        expression = binding['condition']['expression']
        if 'raw/' in expression:
            assert binding['role'] in {'roles/storage.objectViewer', 'roles/storage.objectCreator'}
        if 'synthetic-flex-watch@' in binding['members'][0]:
            assert 'raw/' not in expression and 'derived/' not in expression and '/objects/state/' not in expression
    for job in output['jobs']:
        spec = job['spec']['template']['spec']['template']['spec']
        assert spec['maxRetries'] == 0
        assert spec['containers'][0]['resources']['limits'] == {'cpu': '1', 'memory': '512Mi'}


def test_telegram_unknown_and_rate_limit(monkeypatch):
    class Response:
        status_code = 429
        def json(self):
            return {'parameters': {'retry_after': 999}}
    class Session:
        def post(self, *args, **kwargs):
            assert kwargs['allow_redirects'] is False
            return Response()
        def close(self):
            pass
    monkeypatch.setattr(watch.requests, 'Session', Session)
    assert watch.send('synthetic', 'synthetic', 'redacted') == {'delivery': 'rate_limited', 'retry_after': 999}
    Response.status_code = 200
    assert watch.send('synthetic', 'synthetic', 'redacted') == {'delivery': 'unknown'}
    Response.status_code = 403
    assert watch.send('synthetic', 'synthetic', 'redacted') == {'delivery': 'blocked'}


def test_unknown_status_to_known_status_does_not_crash():
    now = datetime.now(timezone.utc)
    assert watch.events({'missing_account_days': 1}, {'missing_account_days': None}, now)


def test_nav_period_mismatch_cannot_be_complete():
    root = ET.fromstring(report())
    root.find('.//ChangeInNAV').set('fromDate', '20260101')
    c = config()
    result = checks.inspect_report(ET.tostring(root), '2026-10-31', '2026-10-31', c['accounts'], c['configured_sections'])
    assert result['account-1']['status'] == 'received'
    assert 'nav_period_or_currency_unqualified' in result['account-1']['issues']


def test_opening_snapshot_does_not_expand_monthly_request_scope():
    s = {'days': {}, 'months': {}, 'backfill_start': '2026-09-01'}
    state.seed(s, date(2026, 8, 31), date(2026, 10, 31), ['account-1'])
    items = state.windows(s, date(2026, 10, 31), datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert ('2026-09-3', '2026-09-01', '2026-09-30') in items
    assert not any(kind != 'daily' and start < '2026-09-01' for kind, start, end in items)


def test_month_control_remains_pending_until_all_daily_activity_available():
    store = Memory()
    c = config()
    c['backfill_start'] = '2026-09-01'
    c['accounts'] = {'U00001': 'account-1'}
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    runner.collect(store, c, lambda start, end: report(start, end), now=lambda: now)
    coverage, _ = store.json('state/coverage.json')
    assert coverage['months']['2026-09-3']['comparison_status'] == 'partial'
    assert coverage['months']['2026-09-3']['done'] is False
    assert coverage['months']['2026-09-10']['done'] is False


def test_unknown_account_stops_before_any_raw_upload():
    store = Memory()
    calls = []
    def fetch(start, end):
        calls.append(start)
        return report(start, end, account='U99999')
    result = runner.collect(store, config(), fetch)
    assert result['blocked'] == 'account_scope'
    assert len(calls) == 1
    assert not any(key.startswith('raw/') for key in store.values)
