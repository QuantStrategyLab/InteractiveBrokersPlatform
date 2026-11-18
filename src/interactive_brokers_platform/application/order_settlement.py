"""Durable, read-only settlement observations beside the execution markers.

These records are reporting evidence only. They never release account submission
claims, cash/risk reservations, or UNKNOWN fences, and never resubmit orders.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import time

from quant_platform_kit.common.execution_state import ExecutionMarkerStore, submission_binding_digest
from quant_platform_kit.ibkr.order_outcome import OrderOutcome, resolve_order_outcome


class SettlementStore:
    def __init__(self, marker_store: ExecutionMarkerStore, *, account_id: str, profile: str):
        self.store = replace(marker_store, namespace='ibkr_order_settlement')
        self.account_id, self.profile = account_id, profile
        self.prefix = 'v1/' + submission_binding_digest({'account': account_id, 'profile': profile})

    def _key(self, attempt, suffix):
        if not re.fullmatch(r'[a-f0-9]{64}', attempt):
            raise ValueError('settlement_attempt_invalid')
        return f'{self.prefix}/{attempt}/{suffix}'

    def _read(self, attempt, suffix):
        value = self.store.read_marker(self._key(attempt, suffix))
        return value['metadata'] if value is not None else None

    def _create(self, attempt, suffix, data):
        key = self._key(attempt, suffix)
        if self.store.claim_marker(key, metadata=data):
            return True
        existing = self._read(attempt, suffix)
        if existing != data:
            raise RuntimeError('settlement_identity_conflict')
        return False

    def prepare(self, attempt, *, symbol, side, quantity, client_id, submitted_at, source_run_id, source_started_at=None):
        quantity = float(quantity)
        if (not math.isfinite(quantity) or quantity <= 0 or side.lower() not in {'buy', 'sell'}
                or not re.fullmatch(r'[A-Z0-9._-]{1,32}', symbol)
                or type(client_id) is not int or client_id < 0
                or datetime.fromisoformat(submitted_at).utcoffset() is None
                or not isinstance(source_run_id, str) or not source_run_id):
            raise ValueError('settlement_order_invalid')
        self._create(attempt, 'pending', dict(attempt_id=attempt, symbol=symbol, side=side.lower(),
            quantity=quantity, client_id=client_id, submitted_at=submitted_at,
            source_run_id=source_run_id, source_started_at=source_started_at or submitted_at, strategy=self.profile))

    def bind_order(self, attempt, order_id):
        if not str(order_id).isdecimal() or int(order_id) <= 0:
            raise ValueError('settlement_order_id_invalid')
        self._create(attempt, 'broker', {'order_id': str(order_id)})

    def records(self):
        # Reuse the marker store's backend and bounded enumeration. Truncation
        # is a failed read, never a claim that the remaining records settled.
        truncated = []
        entries = self.store._recent_ledger_entries(namespace=self.store.namespace,
            relative_prefix=self.prefix, record_limit=10000, truncation_out=truncated)
        if any(truncated):
            raise RuntimeError('settlement_scan_incomplete')
        records = {}
        events = {}
        for _, raw in entries:
            value = json.loads(raw)
            key = value['marker_key']
            if not key.startswith(self.prefix + '/'):
                raise RuntimeError('settlement_scope_invalid')
            attempt, suffix = key[len(self.prefix) + 1:].split('/', 1)
            self._key(attempt, suffix)
            data = value['metadata']
            events.setdefault(attempt, {})[suffix] = data
            if suffix == 'pending':
                if data['attempt_id'] != attempt or data['strategy'] != self.profile:
                    raise RuntimeError('settlement_record_invalid')
                records[attempt] = {**data, 'order_id': None, 'permanent_id': None, 'observations': []}
        if set(events) != set(records):
            raise RuntimeError('settlement_pending_record_missing')
        for attempt, record in records.items():
            event = events[attempt]
            if 'broker' in event:
                record['order_id'] = event['broker']['order_id']
            if 'permanent' in event:
                record['permanent_id'] = event['permanent']['permanent_id']
            observations = sorted((key, value) for key, value in event.items() if key.startswith('observed/'))
            previous = None
            for index, (key, value) in enumerate(observations, 1):
                if key != f'observed/{index:08d}' or value['sequence'] != index:
                    raise RuntimeError('settlement_sequence_invalid')
                self._validate_outcome(record, value['outcome'])
                if previous is not None:
                    if previous['terminal'] or (previous['filled_qty'] or 0) > (value['outcome']['filled_qty'] or 0):
                        raise RuntimeError('settlement_history_invalid')
                previous = value['outcome']
                record['observations'].append(value)
        return [records[key] for key in sorted(records)]

    @staticmethod
    def _validate_outcome(record, outcome):
        if outcome['order_id'] != record['order_id']:
            raise ValueError('settlement_order_mismatch')
        status, filled, remaining = outcome['status'], outcome['filled_qty'], outcome['remaining']
        if status not in {'filled', 'partially_filled', 'pending', 'cancelled', 'rejected', 'unknown'}:
            raise ValueError('settlement_status_invalid')
        if status == 'unknown':
            if outcome['terminal']:
                raise ValueError('settlement_unknown_terminal')
            return
        if (type(outcome['terminal']) is not bool or filled is None or remaining is None
                or not all(math.isfinite(x) and x >= 0 for x in (filled, remaining))
                or not math.isclose(filled + remaining, record['quantity'])
                or (filled > 0 and (outcome['avg_price'] is None or not math.isfinite(outcome['avg_price']) or outcome['avg_price'] <= 0))
                or (outcome['terminal'] != (status in {'filled', 'cancelled', 'rejected'}))
                or (status == 'filled' and (remaining != 0 or filled != record['quantity']))
                or (status == 'rejected' and filled != 0)):
            raise ValueError('settlement_facts_invalid')

    def observe(self, attempt, outcome: OrderOutcome):
        data = json.loads(json.dumps(asdict(outcome), allow_nan=False))
        for _ in range(8):
            record = next(r for r in self.records() if r['attempt_id'] == attempt)
            self._validate_outcome(record, data)
            prior = record['observations'][-1] if record['observations'] else None
            if prior is not None:
                # Re-read after the broker call; an older observer cannot erase
                # a terminal observation written by another process.
                if prior['outcome']['terminal']:
                    if data['terminal'] and data != prior['outcome']:
                        raise RuntimeError('settlement_terminal_conflict')
                    return
                if prior['outcome'] == data:
                    return
                if (data['filled_qty'] or 0) < (prior['outcome']['filled_qty'] or 0):
                    raise RuntimeError('settlement_nonmonotonic_fill')
            sequence = len(record['observations']) + 1
            key = self._key(attempt, f'observed/{sequence:08d}')
            if self.store.claim_marker(key, metadata={'sequence': sequence, 'outcome': data}):
                return
        raise RuntimeError('settlement_concurrent_update')


def settlement_summary(store):
    summary = {key: [] for key in ('orders_submitted', 'orders_filled', 'orders_partially_filled',
        'orders_rejected_or_cancelled', 'orders_pending', 'option_orders_filled', 'option_orders_pending',
        'option_orders_partially_filled')}
    for record in store.records():
        observed = record['observations'][-1] if record['observations'] else None
        outcome = observed['outcome'] if observed else asdict(OrderOutcome(record['order_id'], 'unknown'))
        terminal = outcome['terminal']
        row = dict(symbol=record['symbol'], side=record['side'], quantity=record['quantity'],
            broker_order_id=record['order_id'], filled_quantity=outcome['filled_qty'],
            average_fill_price=outcome['avg_price'], remaining=outcome['remaining'],
            commission=outcome['commission'], last_update=outcome['last_update'],
            broker_confirmed_terminal=terminal, settlement_attempt_id=record['attempt_id'],
            settlement_sequence=observed['sequence'] if observed else 0, source_run_id=record['source_run_id'],
            submitted_at=record['submitted_at'], source_started_at=record['source_started_at'], fills=outcome['fills'],
            broker_status=outcome['status'], status='UNKNOWN')
        summary['orders_submitted'].append(row)
        if terminal:
            row['status'] = {'filled': 'Filled', 'cancelled': 'Cancelled', 'rejected': 'Rejected'}[outcome['status']]
            if outcome['status'] == 'filled':
                summary['orders_filled'].append(row)
            else:
                summary['orders_rejected_or_cancelled'].append(row)
                if outcome['filled_qty'] > 0:
                    summary['orders_partially_filled'].append(row)
        else:
            summary['orders_pending'].append(row)
    summary['execution_status'] = 'pending_reconciliation' if summary['orders_pending'] else 'completed'
    summary['reconciliation_required'] = bool(summary['orders_pending'])
    return summary


def settle_pending(store, ib, *, wait_seconds=0, max_orders=100, timeout_seconds=15, resolver=resolve_order_outcome,
                   monotonic=time.monotonic, sleep=None):
    if not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= 30 or not 1 <= max_orders <= 100:
        raise ValueError('settlement_bounds_invalid')
    sleep = sleep or getattr(ib, 'sleep', time.sleep)
    deadline = monotonic() + wait_seconds
    read_deadline = monotonic() + timeout_seconds
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
        raise ValueError('settlement_timeout_invalid')
    records = store.records()
    pending = [r for r in records if not r['observations'] or not r['observations'][-1]['outcome']['terminal']]
    if len(pending) > max_orders:
        raise RuntimeError('settlement_batch_incomplete')
    for record in pending:
        if not record['order_id'] or monotonic() >= read_deadline:
            continue
        while True:
            if monotonic() >= read_deadline:
                break
            if hasattr(ib, 'RequestTimeout'):
                ib.RequestTimeout = max(.01, min(2.0, (read_deadline - monotonic()) / 3))
            result = resolver(ib, record['order_id'], account_id=store.account_id, client_id=record['client_id'],
                symbol=record['symbol'], side=record['side'], quantity=record['quantity'],
                submitted_at=record['submitted_at'], permanent_id=record['permanent_id'])
            if result.order_id != record['order_id']:
                result = OrderOutcome(record['order_id'], 'unknown')
            store.observe(record['attempt_id'], result)
            if result.terminal or monotonic() >= deadline:
                break
            sleep(min(.25, max(0, deadline - monotonic())))
    return settlement_summary(store)


class _CaptureOrder:
    def __init__(self, ib, store, attempt):
        self._ib, self._store, self._attempt = ib, store, attempt

    def __getattr__(self, name):
        return getattr(self._ib, name)

    def placeOrder(self, contract, order):
        trade = self._ib.placeOrder(contract, order)
        self._store.bind_order(self._attempt, str(trade.order.orderId))
        self.trade = trade
        return trade


def track_submission(ib, intent, *, submit, store, attempt_id, source_run_id,
                     wait_seconds=0, observation_ib=None, source_started_at=None, clock=lambda: datetime.now(timezone.utc)):
    store.prepare(attempt_id, symbol=intent.symbol, side=intent.side, quantity=intent.quantity,
        client_id=int(getattr(getattr(ib, 'client', None), 'clientId', 0)),
        submitted_at=clock().isoformat(), source_run_id=source_run_id, source_started_at=source_started_at)
    capture = _CaptureOrder(ib, store, attempt_id)
    report = submit(capture)
    if report.broker_order_id:
        store.bind_order(attempt_id, report.broker_order_id)
    permanent_id = getattr(getattr(getattr(capture, 'trade', None), 'order', None), 'permId', None)
    if type(permanent_id) is int and permanent_id > 0:
        store._create(attempt_id, 'permanent', {'permanent_id': permanent_id})
    if wait_seconds:
        reader = ib if observation_ib is None else observation_ib
        previous_timeout = reader.RequestTimeout
        try:
            # The explicit read connection may be beneath a submit-only proxy.
            # Its actual SDK timeout must change, not a shadow proxy attribute.
            reader.RequestTimeout = max(.1, wait_seconds / 3)
            settle_pending(store, reader, wait_seconds=wait_seconds, timeout_seconds=wait_seconds)
        finally:
            reader.RequestTimeout = previous_timeout
    # Observation must not change rebalance/trading decisions in this cycle.
    return report
