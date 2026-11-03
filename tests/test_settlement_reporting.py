from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace as NS
import threading

import pytest
from flask import Flask

from application import settlement_route as route
from application.order_settlement import settle_pending
from application.settlement_reporting import settlement_candidate
from notifications import unified_adapter as adapter
from tests.test_unified_notification_adapter import StateStore, CandidateStore, event
from tests.test_order_settlement import store, seed, FILLED, PENDING


def setup_candidates(monkeypatch):
    state, candidates = StateStore(), CandidateStore()
    env = {'DIGEST_STORE_TARGET': 'synthetic-slot'}
    def persist(fact, **kwargs):
        return adapter.persist_candidate(fact, state_store=state, candidate_store=candidates, env=env, **kwargs)
    def factory(kind, report=None, reason=None):
        return event(kind, run=report['run_id'])
    return state, candidates, persist, factory


def latest(candidates):
    return next(iter(candidates.rows.values()))[1]['runs'][0]


def test_pending_then_terminal_amends_original_run_without_double_counts(tmp_path, monkeypatch):
    _, candidates, persist, factory = setup_candidates(monkeypatch)
    initial = event('order_submitted', run='synthetic-run', order_count=1, fill_count=None,
                    order_execution_digest='b' * 64)
    assert persist(initial.to_candidate())
    s = store(tmp_path); seed(s)
    pending = settle_pending(s, object(), resolver=lambda *a, **k: PENDING)['orders_submitted'][0]
    settlement_candidate(pending, event_factory=factory, persist=persist)
    assert latest(candidates)['execution_status'] == 'reconciliation_required'
    filled = settle_pending(s, object(), resolver=lambda *a, **k: FILLED)['orders_submitted'][0]
    for _ in range(2):
        settlement_candidate(filled, event_factory=factory, persist=persist)
    row = latest(candidates)
    assert (row['order_count'], row['fill_count'], row['cycle_count'], row['execution_status']) == (1, 1, 1, 'ok_filled')
    # A delayed read of an earlier snapshot cannot erase the final observation.
    settlement_candidate(pending, event_factory=factory, persist=persist)
    assert latest(candidates) == row


def test_partial_cancelled_digest_counts_confirmed_executions(tmp_path, monkeypatch):
    _, candidates, persist, factory = setup_candidates(monkeypatch)
    assert persist(event('order_submitted', run='synthetic-run', order_count=1,
                         order_execution_digest='b' * 64).to_candidate())
    s = store(tmp_path); seed(s)
    partial = replace(FILLED, status='cancelled', filled_qty=1, remaining=2,
                      fills=(replace(FILLED.fills[0], quantity=1),))
    row = settle_pending(s, object(), resolver=lambda *a, **k: partial)['orders_submitted'][0]
    settlement_candidate(row, event_factory=factory, persist=persist)
    result = latest(candidates)
    assert result['fill_count'] == 1 and result['reason_code'] == 'order_rejected_or_cancelled'


def test_incomplete_run_coverage_stays_unresolved(tmp_path, monkeypatch):
    _, candidates, persist, factory = setup_candidates(monkeypatch)
    persist(event('order_submitted', run='synthetic-run', order_count=2,
                  order_execution_digest='b' * 64).to_candidate())
    s = store(tmp_path); seed(s)
    row = settle_pending(s, object(), resolver=lambda *a, **k: FILLED)['orders_submitted'][0]
    settlement_candidate(row, event_factory=factory, persist=persist)
    assert latest(candidates)['fill_count'] is None
    assert latest(candidates)['execution_status'] == 'reconciliation_required'


def test_settlement_route_is_authenticated_readonly_and_idempotent(tmp_path, monkeypatch):
    s = store(tmp_path); seed(s)
    class Broker:
        def disconnect(self): pass
    broker = Broker(); reads = []; sends = []
    def connect(**kwargs):
        reads.append(kwargs)
        assert kwargs == {'read_only': True, 'validate_trading_permissions': False}
        return broker
    def settle(store, ib, **kwargs):
        return settle_pending(store, ib, resolver=lambda *a, **k: FILLED)
    main = NS(STRATEGY_RUN_LOCK=threading.Lock(), connect_ib=connect,
              send_tg_message=lambda text: sends.append(text) or True, NOTIFY_LANG='en')
    monkeypatch.setenv('IBKR_ORDER_SETTLEMENT_ENABLED', 'true')
    monkeypatch.setattr(route, 'authenticate_settlement', lambda: True)
    monkeypatch.setattr(route, 'runtime_settlement_store', lambda main: s)
    monkeypatch.setattr(route, 'settle_pending', settle)
    monkeypatch.setattr(adapter, 'persist_candidate', lambda *a, **k: True)
    monkeypatch.setattr(adapter, 'event_for', lambda kind, report, reason: event(kind, run=report['run_id']))
    app = Flask(__name__)
    for _ in range(2):
        with app.test_request_context('/settle', method='POST'):
            response, code = route.handle_settle(main)
            assert code == 200 and response['filled'] == 1
            assert 'U00000003' not in str(response)
    assert len(sends) == 1 and len(reads) == 2


def test_route_transport_failure_returns_unknown_without_raw_error(tmp_path, monkeypatch):
    s = store(tmp_path); seed(s)
    monkeypatch.setenv('IBKR_ORDER_SETTLEMENT_ENABLED', 'true')
    monkeypatch.setattr(route, 'authenticate_settlement', lambda: True)
    monkeypatch.setattr(route, 'runtime_settlement_store', lambda main: s)
    def connect(**kwargs): raise TimeoutError('synthetic-private-broker-diagnostic')
    main = NS(STRATEGY_RUN_LOCK=threading.Lock(), connect_ib=connect)
    with Flask(__name__).test_request_context('/settle', method='POST'):
        response, code = route.handle_settle(main)
    assert code == 503 and response['status'] == 'UNKNOWN'
    assert 'synthetic-private-broker-diagnostic' not in str(response)
    assert main.STRATEGY_RUN_LOCK.acquire(blocking=False)
