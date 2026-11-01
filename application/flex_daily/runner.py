"""Bounded cloud collector; credentials stay in memory and errors stay coded."""
import hashlib
import json
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from application.ibkr_flex_source import (
    FlexReportPending, FlexSourceError, collect_activity_flex_xml, request_activity_flex_report,
)
from application.flex_daily.checks import compare_activity, inspect_report, private_projection
from application.flex_daily.state import failed, seed, summary, target_day, windows
from application.flex_daily.storage import Lease

BLOCKING = {'1010', '1011', '1012', '1013', '1014', '1015', '1016', '1020'}


def utcnow():
    return datetime.now(timezone.utc)


class Fetcher:
    def __init__(self, token, query, deadline, clock=time.monotonic, sleep=time.sleep):
        self.token, self.query, self.deadline = token, query, deadline
        self.clock, self.sleep, self.last = clock, sleep, clock()

    def wait(self, delay=10):
        pause = max(0, (self.last + delay - self.clock()) if self.last is not None else 0)
        if self.clock() + pause + 25 >= self.deadline:
            raise TimeoutError('budget_exhausted')
        self.sleep(pause)
        self.last = self.clock()

    def __call__(self, start, end):
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
    deadline = clock() + 25 * 60

    def persist(finished=False):
        nonlocal generation, status_generation
        lease.check(now())
        if finished:
            state['last_finished_at'] = now().isoformat()
        generation = store.put('state/coverage.json', state, generation)
        safe = summary(state, now())
        safe['token_expires_at'] = config.get('token_expires_at')
        status_generation = store.put('status/latest.json', safe, status_generation)
        return safe

    try:
        if state.get('blocked'):
            return persist()
        seed(state, date.fromisoformat(config['backfill_start']) - timedelta(days=1), target_day(started), accounts.values())
        state['last_started_at'] = started.isoformat()
        persist()
        for kind, start, end in windows(state, target_day(started), started):
            if clock() + 30 >= deadline:
                break
            slot = state['days'][start] if kind == 'daily' else state['months'].setdefault(kind, {})
            try:
                content = fetch(start, end)
                digest = hashlib.sha256(content).hexdigest()
                # Immutable raw survives a crash before coverage commit. A retry
                # reuses its content address; neither double counts nor replaces it.
                raw_key = f'raw/{digest}.xml'
                store.immutable(raw_key, content)
                try:
                    previous_day = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
                    previous_slot = state['days'].get(previous_day, {})
                    previous_content = store.read(previous_slot['raw'])[0] if previous_slot.get('raw') else None
                    checked = inspect_report(content, start, end, accounts, config['configured_sections'], previous_content)
                except ValueError:
                    slot['quarantine_raw'] = raw_key
                    raise
                projection = {'schema': 1, 'start': start, 'end': end, 'checks': checked,
                              'source_sha256': digest, 'rows': private_projection(content)}
                derived = json.dumps(projection, sort_keys=True).encode()
                derived_hash = hashlib.sha256(derived).hexdigest()
                store.immutable(f'derived/{derived_hash}.json', derived)
                old_hash = slot.get('sha256')
                slot.update(raw=raw_key, sha256=digest, derived=f'derived/{derived_hash}.json', checked_at=now().isoformat())
                if kind == 'daily':
                    slot['accounts'] = {key: {k: v for k, v in value.items() if k != 'residuals'} for key, value in checked.items()}
                else:
                    # Monthly changes invalidate only the corresponding daily
                    # completeness; monthly files never fabricate daily snapshots.
                    month_changed = any(v.get('sha256') and v['sha256'] != digest
                                        for k, v in state['months'].items() if k[:7] == start[:7])
                    if month_changed or old_hash and old_hash != digest:
                        for day, daily in state['days'].items():
                            if start <= day <= end:
                                for value in daily['accounts'].values():
                                    value['status'] = 'pending'
                    slot['done'] = all(v['status'] == 'complete' for v in checked.values())
                    daily_slots = [v for day, v in sorted(state['days'].items()) if start <= day <= end]
                    expected_days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
                    eligible = len(daily_slots) == expected_days and all(
                        v.get('raw') and all(a.get('status') == 'complete' for a in v['accounts'].values()) for v in daily_slots)
                    slot['comparison_status'] = 'partial'
                    if eligible and clock() + 90 < deadline:
                        payloads = [store.read(v['raw'])[0] for v in daily_slots]
                        if all(payloads):
                            same_activity = compare_activity(content, payloads)
                            slot['comparison_status'] = 'activity_matched' if same_activity else 'difference'
                            if not same_activity:
                                for daily in daily_slots:
                                    for value in daily['accounts'].values():
                                        value['status'] = 'pending'
                                slot['done'] = False
                    # Exact activity comparison is not balance, grant or full
                    # reconciliation certification.
                if any(v['status'] != 'complete' for v in checked.values()):
                    failed(slot, now(), 'incomplete_report')
                else:
                    slot['attempts'] = 0
                    slot.pop('next_attempt_at', None)
            except FlexReportPending:
                failed(slot, now(), 'pending')
            except FlexSourceError as exc:
                code = exc.safe_diagnostic.get('provider_error_code', 'transport_or_format')
                failed(slot, now(), code)
                if code in BLOCKING:
                    state['blocked'] = 'ip_restricted' if code == '1013' else 'authorization_or_configuration'
                    state['blocked_code'] = code
                if code in BLOCKING | {'1018', '1001'} or exc.safe_diagnostic.get('source_failure_kind') in {'http', 'transport'}:
                    persist()
                    break
            except ValueError:
                failed(slot, now(), 'report_validation_failed')
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
