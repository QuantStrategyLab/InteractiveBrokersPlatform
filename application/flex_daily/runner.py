"""Bounded cloud collector; credentials stay in memory and errors stay coded."""
import hashlib
import json
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from application.ibkr_flex_source import (
    FlexReportPending, FlexSourceError, collect_activity_flex_xml, request_activity_flex_report,
)
from application.flex_daily.checks import (
    ReportScopeError, compare_month, inspect_report, private_projection, require_account_scope,
)
from application.flex_daily.state import complete, failed, seed, summary, target_day, windows
from application.flex_daily.storage import Lease
from application.flex_daily.forward import qualify, validate_config

BLOCKING = {'1010', '1011', '1012', '1013', '1014', '1015', '1016', '1020'}
# Provider says "not ready" (1001) or "too many requests" (1018). The window is
# failed and later windows may continue, but repeated refusals still end the run.
TRANSIENT = {'1001', '1018'}
MAX_CONSECUTIVE_TRANSIENT = 3
THROTTLE_BACKOFF = 60


def utcnow():
    return datetime.now(timezone.utc)


class Fetcher:
    def __init__(self, token, query, deadline, clock=time.monotonic, sleep=time.sleep):
        self.token, self.query, self.deadline = token, query, deadline
        self.clock, self.sleep, self.last = clock, sleep, clock()
        self.floor = 0

    def wait(self, delay=10):
        delay = max(delay, self.floor)
        pause = max(0, (self.last + delay - self.clock()) if self.last is not None else 0)
        if self.clock() + pause + 25 >= self.deadline:
            raise TimeoutError('budget_exhausted')
        self.sleep(pause)
        self.last = self.clock()

    def __call__(self, start, end):
        try:
            return self.fetch(start, end)
        except FlexReportPending:
            raise
        except FlexSourceError as exc:
            if exc.safe_diagnostic.get('provider_error_code') == '1018':
                # Rate limited: every later request in this run waits at least
                # THROTTLE_BACKOFF seconds after the previous one.
                self.floor = THROTTLE_BACKOFF
            raise

    def fetch(self, start, end):
        self.wait()
        handle = request_activity_flex_report(token=self.token, query_id=self.query, from_date=start, to_date=end)
        for delay in (10, 20, 40, 80, 120, 120):
            self.wait(delay)
            try:
                return collect_activity_flex_xml(handle).content
            except FlexReportPending:
                continue
        raise FlexReportPending(handle)


def collect(store, config, fetch, now=utcnow, clock=time.monotonic):
    started = now()
    forward = 'forward_start' in config
    if forward:
        validate_config(config)
    # Unknown is not an assertion of unrestricted IP. Operator must attest the
    # actual token setting without exposing its value, before any broker request.
    if config.get('ip_restriction') != 'none':
        return {'blocked': 'ip_restriction_unconfirmed_or_present'}
    accounts = config['accounts']
    if not accounts or len(set(accounts.values())) != len(accounts) or any(
            not label.isascii() or not label.startswith('account-') or not label[8:].isdigit()
            for label in accounts.values()):
        raise ValueError('invalid_account_labels')
    if config.get('schema') != 1 or not config.get('query_id', '').isdigit():
        raise ValueError('invalid_configuration')
    lease = Lease(store, 'state/pull-lease.json', started)
    state, generation = store.json('state/coverage.json', {'schema': 1, 'days': {}, 'months': {}})
    status, status_generation = store.json('status/latest.json', {})
    if state.get('forward_start') and not forward:
        lease.release()
        raise ValueError('forward_configuration_required')
    deadline = clock() + 25 * 60

    def persist(finished=False):
        nonlocal generation, status_generation
        lease.check(now())
        if finished:
            state['last_finished_at'] = now().isoformat()
        generation = store.put('state/coverage.json', state, generation)
        safe = summary(state, now())
        if forward:
            safe['forward_start'] = config['forward_start']
            safe['reconciliation_status'] = 'UNRECONCILED'
            planned = run['windows_planned']
            safe['forward_validation'] = (
                'NOT_DUE' if target_day(started).isoformat() < config['forward_start'] else
                'VALIDATED' if planned > 0 and run['windows_succeeded'] == planned and not run['windows_failed'] and all(
                    (state['days'][a].get('validated_at') and complete(state['days'][a])) if kind == 'daily'
                    else (state['months'][kind].get('validated_at') and state['months'][kind].get('done'))
                    for kind, a, b in queue) else
                'UNRECONCILED')
        safe['token_expires_at'] = config.get('token_expires_at')
        status_generation = store.put('status/latest.json', safe, status_generation)
        return safe

    # Codes and counts only: safe for status objects and container logs.
    run = state['last_run'] = {'windows_planned': 0, 'reports_fetched': 0, 'windows_succeeded': 0,
                               'windows_failed': 0, 'last_provider_error_code': None}
    queue = []
    try:
        if state.get('blocked'):
            return persist()
        if forward:
            state['forward_start'] = config['forward_start']
            state['forward_target'] = target_day(started).isoformat()
        else:
            state['backfill_start'] = config['backfill_start']
        seed_start = config['forward_start'] if forward else config['backfill_start']
        seed(state, date.fromisoformat(seed_start) - timedelta(days=0 if forward else 1), target_day(started), accounts.values())
        state['last_started_at'] = started.isoformat()
        queue = windows(state, target_day(started), started)
        run['windows_planned'] = len(queue)
        persist()
        transient_streak = 0
        for kind, start, end in queue:
            if clock() + 30 >= deadline:
                break
            slot = state['days'][start] if kind == 'daily' else state['months'].setdefault(kind, {})

            old_raw = slot.get('raw')

            def invalidate_following():
                # Later intervals were qualified against this earlier evidence.
                # Keep their immutable objects, but require fresh validation.
                for day, later in state['days'].items():
                    if start < day and config['forward_start'] <= day <= state['forward_target']:
                        later.pop('validated_at', None)
                        later.pop('forward', None)
                        for item in later['accounts'].values():
                            item.update(status='pending', issues=['previous_anchor_revised'])
                for key, month in state.get('months', {}).items():
                    if key[:7] + '-01' >= config['forward_start']:
                        month['done'] = False
                        month.pop('validated_at', None)

            if forward:
                slot.pop('validated_at', None)
                slot.pop('forward', None)
                if kind == 'daily':
                    slot['accounts'] = {label: {'status': 'missing', 'issues': ['validation_pending'],
                                                       'forward_checked': True} for label in accounts.values()}
                else:
                    slot['done'] = False
                persist()

            def fail(reason):
                if forward and old_raw and not slot.get('forward'):
                    invalidate_following()
                failed(slot, now(), reason)
                run['windows_failed'] += 1

            try:
                capture_start = now()
                content = fetch(start, end)
                capture_end = now()
                if forward:
                    lease.check(capture_end)
                run['reports_fetched'] += 1
                transient_streak = 0
                require_account_scope(content, accounts)
                digest = hashlib.sha256(content).hexdigest()
                # Immutable raw survives a crash before coverage commit. A retry
                # reuses its content address; neither double counts nor replaces it.
                raw_key = f'raw/{digest}.xml'
                store.immutable(raw_key, content)
                try:
                    previous_day = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
                    previous_slot = {} if forward else state['days'].get(previous_day, {})
                    previous_content = store.read(previous_slot['raw'])[0] if previous_slot.get('raw') else None
                    checked = inspect_report(content, start, end, accounts, config['configured_sections'], previous_content)
                except ValueError:
                    slot['quarantine_raw'] = raw_key
                    raise
                projection = {'schema': 1, 'start': start, 'end': end, 'checks': checked,
                              'source_sha256': digest, 'rows': private_projection(content)}
                if forward:
                    previous = {}
                    for day, previous_slot in sorted(state['days'].items()):
                        if config['forward_start'] <= day < start:
                            for label, ev in previous_slot.get('forward', {}).items():
                                if ev.get('anchor_qualified'):
                                    previous[label] = ev
                    evidence = qualify(content, config, checked, previous, capture_start, capture_end)
                    projection['forward'] = evidence
                    slot['forward'] = evidence
                    if old_raw and old_raw != raw_key:
                        invalidate_following()
                    for label, value in checked.items():
                        value['forward_checked'] = True
                        ev = evidence.get(label)
                        if ev and ev['issues']:
                            value['issues'] = sorted(set(value['issues'] + ev['issues']))
                        if value['status'] == 'complete' and (not ev or ev['issues'] or value['reconciliation_status'] == 'difference'):
                            value['status'] = 'received'
                derived = json.dumps(projection, sort_keys=True).encode()
                derived_hash = hashlib.sha256(derived).hexdigest()
                store.immutable(f'derived/{derived_hash}.json', derived)
                slot['reconciliation_status'] = 'difference' if any(v['reconciliation_status'] == 'difference' for v in checked.values()) else 'partial'
                slot.update(raw=raw_key, sha256=digest, derived=f'derived/{derived_hash}.json', checked_at=now().isoformat())
                if kind == 'daily':
                    slot['accounts'] = {key: {k: v for k, v in value.items() if k != 'residuals'} for key, value in checked.items()}
                else:
                    # Raw metadata changes preserve daily completeness. Only a
                    # semantic control difference below queues daily reports again.
                    schema_complete = all(v['status'] == 'complete' for v in checked.values())
                    slot['done'] = False
                    daily_slots = [v for day, v in sorted(state['days'].items()) if start <= day <= end]
                    expected_days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
                    eligible = len(daily_slots) == expected_days and all(
                        v.get('raw') and all((not forward or a.get('forward_checked')) and a.get('status') == 'complete' for a in v['accounts'].values()) for v in daily_slots)
                    slot['comparison_status'] = 'partial'
                    if eligible and clock() + 90 < deadline:
                        payloads = [store.read(v['raw'])[0] for v in daily_slots]
                        if all(payloads):
                            same_activity = compare_month(content, payloads)
                            slot['comparison_status'] = 'controls_matched' if same_activity else 'difference'
                            slot['done'] = same_activity and schema_complete
                            if not same_activity:
                                for daily in daily_slots:
                                    for value in daily['accounts'].values():
                                        value['status'] = 'pending'
                                    if forward:
                                        daily.pop('validated_at', None)
                                slot['done'] = False
                    # Exact opening/closing controls still do not certify grants,
                    # settlement lineage or full financial reconciliation.
                if any(v['status'] != 'complete' for v in checked.values()) or kind != 'daily' and not slot.get('done'):
                    fail('incomplete_report_or_daily_control')
                else:
                    slot['attempts'] = 0
                    slot.pop('next_attempt_at', None)
                    run['windows_succeeded'] += 1
                    if forward:
                        slot['validated_at'] = now().isoformat()
            except FlexReportPending:
                fail('pending')
                transient_streak = 0
            except FlexSourceError as exc:
                provider_code = exc.safe_diagnostic.get('provider_error_code')
                code = provider_code or 'transport_or_format'
                fail(code)
                if provider_code:
                    run['last_provider_error_code'] = provider_code
                if code in BLOCKING:
                    state['blocked'] = 'ip_restricted' if code == '1013' else 'authorization_or_configuration'
                    state['blocked_code'] = code
                transient_streak = transient_streak + 1 if code in TRANSIENT else 0
                if code in BLOCKING or transient_streak >= MAX_CONSECUTIVE_TRANSIENT or (
                        code not in TRANSIENT and exc.safe_diagnostic.get('source_failure_kind') in {'http', 'transport'}):
                    persist()
                    break
            except ReportScopeError:
                fail('account_scope_mismatch')
                state['blocked'] = 'account_scope'
                persist()
                break
            except ValueError:
                fail('report_validation_failed')
            except TimeoutError:
                break
            persist()
        return persist(finished=True)
    finally:
        lease.release()


def read_token(path):
    # Called only inside the authorized cloud Job. No local bootstrap invocation.
    value = Path(path).read_text().strip()
    if not value:
        raise ValueError('token_empty')
    return value
