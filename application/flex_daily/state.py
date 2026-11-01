"""Persistent coverage and bounded scheduling; all dates are broker dates."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


def target_day(now):
    return now.astimezone(ZoneInfo('America/New_York')).date() - timedelta(days=1)


def days(start, end):
    while start <= end:
        yield start.isoformat()
        start += timedelta(days=1)


def seed(state, start, target, accounts):
    for day in days(start, target):
        slot = state.setdefault('days', {}).setdefault(day, {'accounts': {}, 'attempts': 0})
        for account in accounts:
            slot['accounts'].setdefault(account, {'status': 'missing'})


def complete(slot):
    return bool(slot.get('accounts')) and all(
        item.get('status') == 'complete' for item in slot['accounts'].values())


def windows(state, target, now, limit=30):
    """Recent revisions first, durable monthly controls, then oldest holes."""
    selected = []
    recent = [(target - timedelta(days=n)).isoformat() for n in range(7)]
    for day in recent:
        slot = state['days'].get(day)
        if slot and due(slot, now):
            selected.append(('daily', day, day))
    # Persist each month's two control runs: an outage must not skip day 3/10.
    start = date.fromisoformat(state.get('backfill_start', min(state['days'])))
    month = start.replace(day=1)
    if month < start:
        month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    while month < target.replace(day=1):
        following = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        end = following - timedelta(days=1)
        for checkpoint in (3, 10):
            key = f'{month:%Y-%m}-{checkpoint}'
            slot = state.setdefault('months', {}).get(key, {})
            if now.date() >= following.replace(day=checkpoint) and not slot.get('done') and due(slot, now):
                selected.append((key, month.isoformat(), end.isoformat()))
        month = following
    for day, slot in sorted(state['days'].items()):
        if day not in recent and not complete(slot) and due(slot, now):
            selected.append(('daily', day, day))
    return selected[:limit]


def due(slot, now):
    return not slot.get('next_attempt_at') or datetime.fromisoformat(slot['next_attempt_at']) <= now


def failed(slot, now, reason):
    slot['attempts'] = slot.get('attempts', 0) + 1
    slot['last_error'] = reason
    # No generated retry inside this run. Three failures need daily spacing.
    hours = 24 if slot['attempts'] >= 3 else 1
    slot['next_attempt_at'] = (now + timedelta(hours=hours)).isoformat()


def summary(state, now):
    missing = [(day, account) for day, slot in sorted(state['days'].items())
               for account, item in slot['accounts'].items() if item.get('status') != 'complete']
    continuous = {}
    accounts = next(iter(state['days'].values()))['accounts'] if state['days'] else {}
    for account in accounts:
        last = None
        for day, slot in sorted(state['days'].items()):
            if slot['accounts'][account]['status'] != 'complete':
                break
            last = day
        continuous[account] = last
    return {'schema': 1, 'checked_at': now.isoformat(), 'target_date': max(state['days'], default=None),
            'missing_account_days': len(missing), 'oldest_gap': missing[0][0] if missing else None,
            'continuous_through': continuous, 'reconciliation_status': 'partial',
            'field_issue_count': sum(len(item.get('issues', [])) for slot in state['days'].values() for item in slot['accounts'].values()),
            'residual_nonzero': any(item.get('reconciliation_status') == 'difference' for slot in state['days'].values() for item in slot['accounts'].values()),
            'blocked': state.get('blocked'), 'last_started_at': state.get('last_started_at'),
            'last_finished_at': state.get('last_finished_at')}
