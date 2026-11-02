from dataclasses import replace
from datetime import datetime, timezone
from multiprocessing import get_context
from types import SimpleNamespace as NS

import pytest

from application.order_settlement import SettlementStore, settle_pending, track_submission, settlement_summary
from quant_platform_kit.common.execution_state import ExecutionMarkerStore
from quant_platform_kit.common.models import ExecutionReport, OrderIntent
from quant_platform_kit.ibkr.order_outcome import OrderOutcome, OutcomeFill

NOW = datetime(2026, 11, 3, 14, 31, tzinfo=timezone.utc)
PENDING = OrderOutcome('73', 'pending', 0, 3)
FILLED = OrderOutcome('73', 'filled', 3, 0, 52, .35, NOW.isoformat(), True,
                      (OutcomeFill('synthetic-exec-1', 3, 52),))


def store(tmp_path, profile='synthetic-alpha'):
    return SettlementStore(ExecutionMarkerStore(local_dir=tmp_path), account_id='U00000003', profile=profile)


def seed(s, attempt='a' * 64, run_id='synthetic-run'):
    s.prepare(attempt, symbol='TQQQ', side='sell', quantity=3, client_id=7,
              submitted_at=NOW.isoformat(), source_run_id=run_id)
    s.bind_order(attempt, '73')
    return attempt


def settle(s, resolve, **kwargs):
    return settle_pending(s, object(), resolver=lambda *a, **k: resolve(), **kwargs)


def test_pending_to_filled_and_duplicate_settle(tmp_path):
    s = store(tmp_path); seed(s)
    assert settle(s, lambda: PENDING)['orders_pending'][0]['status'] == 'UNKNOWN'
    result = settle(s, lambda: FILLED)
    assert result['orders_filled'][0]['filled_quantity'] == 3
    def forbidden():
        raise AssertionError('terminal order must not be queried again')
    assert settle(s, forbidden) == result


def test_partial_to_cancelled_is_terminal_partial_fill(tmp_path):
    s = store(tmp_path); seed(s)
    partial = replace(FILLED, status='partially_filled', filled_qty=1, remaining=2, terminal=False,
                      fills=(OutcomeFill('synthetic-exec-1', 1, 52),))
    assert not settle(s, lambda: partial)['orders_partially_filled']
    result = settle(s, lambda: replace(partial, status='cancelled', terminal=True))
    assert result['orders_partially_filled'][0]['filled_quantity'] == 1
    assert result['orders_rejected_or_cancelled'][0]['status'] == 'Cancelled'
    assert result['orders_pending'] == []


def test_rejected(tmp_path):
    s = store(tmp_path); seed(s)
    result = settle(s, lambda: OrderOutcome('73', 'rejected', 0, 3, terminal=True))
    assert result['orders_filled'] == []
    assert result['orders_rejected_or_cancelled'][0]['filled_quantity'] == 0


def test_timeout_is_bounded_and_unresolved(tmp_path):
    s = store(tmp_path); seed(s); calls = []; elapsed = [0.0]
    def resolve(*args, **kwargs):
        calls.append(1)
        elapsed[0] += .4
        return PENDING
    def sleep(seconds):
        elapsed[0] += seconds
    result = settle_pending(s, object(), resolver=resolve, wait_seconds=.5, timeout_seconds=.5,
                            max_orders=1, monotonic=lambda: elapsed[0], sleep=sleep)
    assert len(calls) == 1 and elapsed[0] <= .5
    assert result['execution_status'] == 'pending_reconciliation'
    assert result['orders_pending'][0]['status'] == 'UNKNOWN'


def test_restart_without_receipt_retains_identity_and_fills(tmp_path):
    seed(store(tmp_path))
    result = settle(store(tmp_path), lambda: FILLED)
    assert result['orders_filled'][0]['symbol'] == 'TQQQ'


def test_crash_before_broker_id_is_unresolved_without_resubmit(tmp_path):
    s = store(tmp_path)
    s.prepare('a' * 64, symbol='TQQQ', side='sell', quantity=3, client_id=7,
              submitted_at=NOW.isoformat(), source_run_id='synthetic-run')
    def forbidden():
        raise AssertionError('missing ID cannot be guessed')
    assert settle(s, forbidden)['orders_pending'][0]['broker_order_id'] is None


def test_two_strategies_one_account_do_not_share_records(tmp_path):
    first, second = store(tmp_path), store(tmp_path, 'synthetic-beta')
    seed(first); seed(second)
    settle(first, lambda: FILLED)
    assert settlement_summary(second)['orders_pending']
    assert not settlement_summary(first)['orders_pending']


def test_wrong_order_id_cannot_fill(tmp_path):
    s = store(tmp_path); seed(s)
    result = settle(s, lambda: replace(FILLED, order_id='74'))
    assert not result['orders_filled'] and result['orders_pending']


def test_stale_observation_cannot_overwrite_terminal(tmp_path):
    s = store(tmp_path); attempt = seed(s)
    def stale():
        other = store(tmp_path)
        other.observe(attempt, FILLED)
        return PENDING
    result = settle(s, stale)
    assert result['orders_filled'] and not result['orders_pending']


def _process_observe(root):
    s = store(root); s.observe('a' * 64, FILLED)


def test_two_processes_record_same_terminal_once(tmp_path):
    s = store(tmp_path); seed(s)
    ctx = get_context('fork')
    children = [ctx.Process(target=_process_observe, args=(str(tmp_path),)) for _ in range(2)]
    for child in children: child.start()
    for child in children:
        child.join(5)
        assert child.exitcode == 0
    assert len(s.records()[0]['observations']) == 1


def test_persistence_failure_blocks_before_submit(tmp_path):
    s = store(tmp_path)
    def fail(*args, **kwargs): raise RuntimeError('synthetic-persist-failure')
    s.prepare = fail
    with pytest.raises(RuntimeError):
        track_submission(NS(client=NS(clientId=7)), OrderIntent('TQQQ', 'sell', 3),
                         submit=lambda b: pytest.fail('no submission'), store=s,
                         attempt_id='a' * 64, source_run_id='synthetic-run')


def test_lost_receipt_is_recovered_from_place_order_capture(tmp_path):
    s = store(tmp_path)
    class Broker:
        client = NS(clientId=7)
        def placeOrder(self, contract, order):
            return NS(order=NS(orderId=73))
    def submit(b):
        b.placeOrder(NS(), NS())
        raise TimeoutError('synthetic-receipt-lost')
    with pytest.raises(TimeoutError):
        track_submission(Broker(), OrderIntent('TQQQ', 'sell', 3), submit=submit, store=s,
                         attempt_id='a' * 64, source_run_id='synthetic-run', clock=lambda: NOW)
    assert settle(store(tmp_path), lambda: FILLED)['orders_filled']


def test_default_wait_off_preserves_submit_report(tmp_path):
    s = store(tmp_path)
    report = ExecutionReport('TQQQ', 'sell', 3, 'Submitted', broker_order_id='73')
    result = track_submission(NS(client=NS(clientId=7)), OrderIntent('TQQQ', 'sell', 3),
        submit=lambda b: report, store=s, attempt_id='a' * 64, source_run_id='synthetic-run')
    assert result is report
    assert not s.records()[0]['observations']


def test_settled_notice_masks_id_and_never_exposes_account(tmp_path):
    from application.settlement_reporting import settled_notice, publish_settlement_notices
    s = store(tmp_path); seed(s)
    summary = settle(s, lambda: FILLED)
    row = summary['orders_filled'][0]
    row['broker_order_id'] = '987654321'
    text = settled_notice(row, locale='zh')
    assert '987654321' not in text and 'U00000003' not in text
    assert '成交 3 @ 52' in text
    sends = []
    for _ in range(2):
        publish_settlement_notices(s, summary, send=lambda text: sends.append(text) or True,
                                  publish_candidate=lambda row: True)
    assert len(sends) == 1


def test_ambiguous_notice_is_not_retried(tmp_path):
    from application.settlement_reporting import publish_settlement_notices
    s = store(tmp_path); seed(s); summary = settle(s, lambda: FILLED); sends = []
    with pytest.raises(RuntimeError, match='unconfirmed'):
        publish_settlement_notices(s, summary, send=lambda text: sends.append(text) or False,
                                  publish_candidate=lambda row: True)
    with pytest.raises(RuntimeError, match='delivery_unknown'):
        publish_settlement_notices(s, summary, send=lambda text: pytest.fail('no resend'),
                                  publish_candidate=lambda row: True)
    assert len(sends) == 1


def test_settlement_route_requires_verified_identity_before_reads(monkeypatch):
    from flask import Flask
    from application import settlement_route as route
    app = Flask(__name__)
    main = NS(connect_ib=lambda **k: pytest.fail('no broker reads'))
    with app.test_request_context('/settle', method='POST'):
        assert route.handle_settle(main)[1] == 403
    env = {'IBKR_SETTLEMENT_OIDC_AUDIENCE': 'https://synthetic.invalid',
           'IBKR_SETTLEMENT_CALLER_SUBJECTS': 'synthetic-subject'}
    with app.test_request_context('/settle', method='POST', headers={'Authorization': 'Bearer synthetic-token'}):
        def verify(*args, **kwargs):
            return {'iss': 'https://accounts.google.com', 'sub': 'synthetic-subject'}
        assert route.authenticate_settlement(env=env, verifier=verify)
        assert not route.authenticate_settlement(env=env, verifier=lambda *a, **k: {'iss': 'untrusted', 'sub': 'synthetic-subject'})
        assert not route.authenticate_settlement(env=env, verifier=lambda *a, **k: {'iss': 'https://accounts.google.com', 'sub': 'other'})


def test_unconfirmed_terminal_fill_is_not_a_receipt():
    from application.execution_receipt_adapter import attach_cycle_execution_receipt
    report = {'platform': 'ibkr', 'strategy_profile': 'synthetic-alpha', 'dry_run': False,
              'runtime_target': {'execution_mode': 'live'},
              'runtime_release_receipt': {'attestation_state': 'self_attested',
                  'strategy_release': {'strategy_revision': 'a' * 40}}}
    attach_cycle_execution_receipt(report, {'orders_filled': [{'symbol': 'TQQQ', 'status': 'Filled'}]}, {}, execution_failed=False)
    assert report['execution_receipt']['outcome'] == 'reconciliation_required'


def test_nonterminal_partial_is_not_a_fill_receipt():
    from application.execution_receipt_adapter import attach_cycle_execution_receipt
    report = {'platform': 'ibkr', 'strategy_profile': 'synthetic-alpha', 'dry_run': False,
              'runtime_target': {'execution_mode': 'live'},
              'runtime_release_receipt': {'attestation_state': 'self_attested',
                  'strategy_release': {'strategy_revision': 'a' * 40}}}
    attach_cycle_execution_receipt(report, {'orders_partially_filled': [{'symbol': 'TQQQ', 'status': 'PartiallyFilled',
        'filled_quantity': 1, 'quantity': 3, 'average_fill_price': 52, 'broker_order_id': '73'}]}, {}, execution_failed=False)
    assert report['execution_receipt']['outcome'] == 'reconciliation_required'


def test_wait_sets_actual_sdk_timeout_beneath_submit_proxy(tmp_path, monkeypatch):
    from application import order_settlement as module
    s = store(tmp_path)
    reader = NS(RequestTimeout=0)
    broker = NS(client=NS(clientId=7))
    report = ExecutionReport('TQQQ', 'sell', 3, 'Submitted', broker_order_id='73')
    def observe(store, ib, **kwargs):
        assert ib is reader and 0 < ib.RequestTimeout <= 1
        return {}
    monkeypatch.setattr(module, 'settle_pending', observe)
    assert track_submission(broker, OrderIntent('TQQQ', 'sell', 3), submit=lambda b: report,
        store=s, attempt_id='a' * 64, source_run_id='synthetic-run', wait_seconds=1,
        observation_ib=reader) is report
    assert reader.RequestTimeout == 0
