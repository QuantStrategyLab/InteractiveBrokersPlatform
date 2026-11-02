"""Settlement projections and masked user notices, without execution authority."""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import os

from quant_platform_kit.notifications._redaction import redact_sensitive_text, mask_identifier


def settlement_enabled(env=None):
    values = os.environ if env is None else env
    value = str(values.get('IBKR_ORDER_SETTLEMENT_ENABLED', 'false')).lower()
    if value not in {'true', 'false', '1', '0'}:
        raise ValueError('settlement_switch_invalid')
    return value in {'true', '1'}


def settled_notice(row, *, locale='en'):
    zh = locale in {'zh', 'zh-CN'}
    identity = mask_identifier(row.get('broker_order_id'))
    title = '订单结算' if zh else 'Order settlement'
    qty, filled, price = row['quantity'], row['filled_quantity'], row['average_fill_price']
    if not row.get('broker_confirmed_terminal'):
        detail = 'UNKNOWN：仍未确认最终状态' if zh else 'UNKNOWN: final state unresolved'
    elif row['status'] == 'Filled':
        detail = (f'成交 {filled:g} @ {price:g}' if zh else f'filled {filled:g} @ {price:g}')
    else:
        status = ('已取消' if row['status'] == 'Cancelled' else '已拒绝') if zh else row['status'].lower()
        partial = (f'部分成交 {filled:g}/{qty:g} @ {price:g}；' if zh else f'partial {filled:g}/{qty:g} @ {price:g}; ') if filled else ''
        detail = partial + status
    return redact_sensitive_text(f'{title} | {row["symbol"]} {row["side"]} | {detail} | ID {identity}')


def publish_settlement_notices(store, summary, *, send, publish_candidate, locale='en'):
    """Persist digest evidence before reserving a single notification attempt.

    A crash/ambiguous send remains delivery unknown and is never blindly resent.
    Candidate publication is safely retryable through the existing CAS publisher.
    """
    for row in summary['orders_submitted']:
        if not row['source_run_id']:
            raise ValueError('settlement_source_run_missing')
        publish_candidate(row)
        if not row['broker_confirmed_terminal']:
            continue
        attempt = row['settlement_attempt_id']
        key = store._key(attempt, 'notice-attempt')
        if store.store.claim_marker(key, metadata={'state': 'pending'}):
            # No receipt marker on exceptions, negative acknowledgements or
            # process loss. Keep the attempt as ambiguous; do not send twice.
            if send(settled_notice(row, locale=locale)) is not True:
                raise RuntimeError('settlement_notification_unconfirmed')
            store._create(attempt, 'notice-receipt', {'state': 'acknowledged'})
        elif store._read(attempt, 'notice-receipt') is None:
            raise RuntimeError('settlement_notification_delivery_unknown')


def settlement_candidate(row, *, event_factory, persist):
    # Use the original run and submission day. Settlement is an amendment to
    # its order facts, not another trading cycle or a second submitted order.
    from dataclasses import replace
    from datetime import datetime
    report = {'run_id': row['source_run_id'], 'started_at': row.get('source_started_at') or row['submitted_at']}
    event = event_factory('order_settlement', report=report, reason='order_settlement')
    event = replace(event, order_execution_digest=hashlib.sha256(row['settlement_attempt_id'].encode()).hexdigest())
    fact = event.to_candidate()
    # The custom event includes the attempt in its key, since it is not part
    # of the shared order-event enumeration.
    fact['dedupe_key'] = hashlib.sha256((str(fact['dedupe_key']) + row['settlement_attempt_id']).encode()).hexdigest()
    if persist(fact, settlement=row) is not True:
        raise RuntimeError('settlement_candidate_unconfirmed')


def merge_settlement_record(prior, record):
    """CAS retries cannot replace a newer settlement with an older snapshot."""
    before, after = prior['settlement'], record['settlement']
    immutable = ('settlement_attempt_id', 'source_run_id', 'submitted_at', 'symbol', 'side', 'quantity')
    if any(before[k] != after[k] for k in immutable):
        raise ValueError('settlement_candidate_identity_conflict')
    old_id, new_id = before['broker_order_id'], after['broker_order_id']
    if old_id is not None and new_id is not None and old_id != new_id:
        raise ValueError('settlement_candidate_order_conflict')
    if old_id is not None and new_id is None:
        return prior
    if before['broker_confirmed_terminal'] or before['settlement_sequence'] > after['settlement_sequence']:
        return prior
    if before['settlement_sequence'] == after['settlement_sequence']:
        if old_id is None and new_id is not None:
            return record
        if before != after:
            raise ValueError('settlement_candidate_sequence_conflict')
        return prior
    return record


def project_settlement_rows(row, items):
    """Replace pending counts only with complete, disjoint terminal coverage."""
    settlements = [item['settlement'] for item in items if 'settlement' in item]
    if not settlements:
        return
    ids = [item['settlement_attempt_id'] for item in settlements]
    if len(ids) != len(set(ids)):
        raise ValueError('settlement_candidate_duplicate')
    expected = row.get('order_count')
    complete = type(expected) is int and expected == len(settlements)
    terminal = complete and all(item['broker_confirmed_terminal'] for item in settlements)
    row['rebalance_conclusion'] = '; '.join(settled_notice(item) for item in settlements)[:1024]
    row['rebalance_kind'] = 'pending' if not terminal else 'rebalance'
    row['evidence_persistence_status'] = row.pop('persistence_status', 'persisted')
    row['evidence_delivery_status'] = row.pop('delivery_status', 'pending_central')
    # Export existing event kinds so the current QRS consumer can classify
    # outcomes without a new event vocabulary or schema change.
    projected = []
    for item in items:
        event = dict(item['event'])
        if 'settlement' in item:
            value = item['settlement']
            event['event_kind'] = ('fill' if value['broker_confirmed_terminal'] and value['filled_quantity'] > 0
                                   else 'order_cancelled' if value['broker_confirmed_terminal']
                                   else 'order_status_unknown')
        elif terminal and event['event_kind'] == 'order_status_unknown':
            continue
        projected.append(event)
    row['notification_events'] = projected
    if not terminal:
        row.update(status='alert', execution_status='reconciliation_required', reason_code='order_settlement_unresolved')
        row['fill_count'] = None
        row['field_status']['fill_count'] = 'unknown'
        return
    fills = [fill['execution_id'] for item in settlements for fill in item['fills']]
    known = (len(fills) == len(set(fills)) and all(
        item['filled_quantity'] == 0 or bool(item['fills']) for item in settlements))
    row['fill_count'] = len(fills) if known else None
    row['field_status']['fill_count'] = 'known' if known else 'unknown'
    # Preserve unrelated risk/error incidents. Only the pending-order evidence
    # from this same source run can be superseded by complete broker evidence.
    unrelated = any(item.get('conflict') or item['event']['event_kind'] not in {
        'routine_cycle', 'healthy_heartbeat', 'order_submitted', 'fill', 'order_settlement', 'order_status_unknown'} for item in items)
    cancelled = any(item['status'] in {'Cancelled', 'Rejected'} for item in settlements)
    row.update(status='alert' if unrelated or cancelled else 'ok',
               execution_status='execution_alert' if unrelated or cancelled else 'ok_filled')
    if unrelated or cancelled:
        row['reason_code'] = 'notification_incident_observed' if unrelated else 'order_rejected_or_cancelled'
    else:
        row.pop('reason_code', None)
