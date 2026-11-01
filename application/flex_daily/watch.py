"""Redacted notifications with durable claims and unknown delivery handling."""
import hashlib
from datetime import datetime, timedelta, timezone

import requests

from application.flex_daily.storage import Lease


def events(status, previous, now):
    reasons = []
    scheduled = now.replace(hour=10, minute=17, second=0, microsecond=0)
    if now < scheduled + timedelta(hours=2):
        scheduled -= timedelta(days=1)
    finished = datetime.fromisoformat(status['last_finished_at']) if status.get('last_finished_at') else None
    if finished is None or finished < scheduled:
        reasons.append('missed_run')
    if status.get('field_issue_count', 0):
        reasons.append('fields_missing')
    if status.get('residual_nonzero'):
        reasons.append('reconciliation_difference')
    if status.get('blocked'):
        reasons.append('ip_restricted' if str(status['blocked']).startswith('ip_') else 'authorization_or_configuration')
    if status.get('oldest_gap') and (now.date() - datetime.fromisoformat(status['oldest_gap']).date()).days > 2:
        reasons.append('coverage_delayed')
    if ((status.get('missing_account_days') or 0) > (previous.get('missing_account_days') or 0)
            or 'gap_queue_increased' in previous.get('reasons', [])
            and (status.get('missing_account_days') or 0) >= (previous.get('missing_account_days') or 0)):
        reasons.append('gap_queue_increased')
    try:
        expiry = datetime.fromisoformat(status.get('token_expires_at'))
        if expiry.utcoffset() is None:
            raise ValueError('timezone_required')
        remaining = (expiry - now).total_seconds() / 86400
    except (ValueError, TypeError):
        reasons.append('token_expiry_unqualified')
    else:
        for threshold in (1, 7, 30):
            if remaining <= threshold:
                reasons.append('token_expiry_' + str(threshold))
                break
    return sorted(reasons)


def send(token, chat_id, text):
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.post(f'https://api.telegram.org/bot{token}/sendMessage',
                                json={'chat_id': chat_id, 'text': text}, timeout=20, allow_redirects=False)
        if response.status_code in (401, 403):
            return {'delivery': 'blocked'}
        if response.status_code == 429:
            body = response.json()
            delay = body.get('parameters', {}).get('retry_after')
            return {'delivery': 'rate_limited', 'retry_after': delay if type(delay) is int and delay > 0 else 3600}
        if response.status_code != 200:
            return {'delivery': 'unknown'}
        body = response.json()
        message_id = body.get('result', {}).get('message_id')
        if body.get('ok') is True and type(message_id) is int:
            return {'delivery': 'sent', 'message_id': message_id}
        return {'delivery': 'unknown'}
    except (requests.RequestException, ValueError):
        return {'delivery': 'unknown'}
    finally:
        session.close()


def watch(store, notify, now=None):
    now = now or datetime.now(timezone.utc)
    lease = Lease(store, 'notifications/lease.json', now)
    try:
        status, _ = store.json('status/latest.json', {})
        previous, generation = store.json('notifications/latest.json', {})
        if previous.get('delivery') == 'blocked':
            return {'delivery': 'blocked'}
        reasons = events(status, previous, now)
        incident = hashlib.sha256(','.join(reasons).encode()).hexdigest()[:12]
        recovery = bool(previous.get('reasons')) and not reasons
        same = previous.get('incident') == incident
        last = datetime.fromisoformat(previous['attempted_at']) if previous.get('attempted_at') else None
        if previous.get('retry_at') and now < datetime.fromisoformat(previous['retry_at']):
            return {'delivery': 'deferred'}
        # Unknown and in-flight claims consume the same reminder interval as a
        # successful send. A worker crash must not cause an immediate duplicate.
        if same and last and (now - last < timedelta(hours=24) if reasons else now.date() == last.date()):
            return {'delivery': 'deduplicated'}
        count = status.get('missing_account_days')
        count_text = str(count) if type(count) is int and count >= 0 else 'unknown'
        day = status.get('target_date')
        try:
            day = datetime.strptime(day, '%Y-%m-%d').strftime('%Y-%m-%d')
        except (TypeError, ValueError):
            day = 'unknown'
        text = f'IBKR {"恢复" if recovery else "PARTIAL"}｜截至 {day}｜缺 {count_text} 个账户日｜核对 partial｜事件 {incident}'
        if reasons:
            text += '｜' + ','.join(reasons)
        claim = {'incident': incident, 'reasons': reasons, 'attempted_at': now.isoformat(),
                 'delivery': 'in_flight', 'missing_account_days': count}
        generation = store.put('notifications/latest.json', claim, generation)
        lease.check(now)
        outcome = notify(text)
        claim.update(outcome)
        if outcome.get('delivery') == 'rate_limited':
            claim['retry_at'] = (now + timedelta(seconds=outcome['retry_after'])).isoformat()
            claim['incident'] = None  # retry only after the server-specified delay
        lease.check(datetime.now(timezone.utc))
        store.put('notifications/latest.json', claim, generation)
        return outcome
    finally:
        lease.release()
