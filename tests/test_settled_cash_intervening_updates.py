"""Reviewer acceptance checks. Synthetic broker, account and journal only."""
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from tests import test_settled_cash_containment as containment
from tests.test_settled_cash_containment import ACCOUNT, FakeIB

native = containment.native


@pytest.mark.parametrize('latest', ['0', '-1', 'unknown', 'nan', '100'])
def test_settlement_update_during_qualification_revokes_old_budget(native, latest):
    plan = native({'SOXL': 400.}, cash=500.)
    ib = FakeIB(cash=500., settled=500.)
    qualify = ib.qualifyContracts
    def qualification_with_broker_update(contract):
        ib.settled = latest
        ib.accountValueEvent.emit(NS(account=ACCOUNT, currency='USD',
                                     tag='SettledCash', value=latest))
        return qualify(contract)
    ib.qualifyContracts = qualification_with_broker_update
    plan.run(ib, prices={'SOXL': 25.})
    notional = sum(o['quantity'] * o['limit_price'] for o in ib.orders)
    budget = 100. if latest == '100' else 0.
    assert notional <= budget, (latest, ib.orders, notional, budget)


@pytest.mark.parametrize('latest', ['0', '-1', 'unknown', '100'])
def test_real_sdk_new_settlement_callback_before_place_order(native, monkeypatch, latest):
    from ib_insync import IB, ContractDetails
    from ib_insync.util import getLoop

    plan = native({'SOXL': 400.}, cash=500.)
    ib = IB()
    ib.client._apiReady = True  # In-memory client; every transport operation is replaced.
    ib.RequestTimeout = .1
    orders = []
    seen = []
    monkeypatch.setattr(ib, 'reqAllOpenOrders', lambda: [])
    def refresh(subscribe, account):
        def callbacks():
            ib.wrapper.updateAccountValue('CashBalance', '500', 'USD', account)
            ib.wrapper.updateAccountValue('SettledCash', '500', 'USD', account)
            ib.wrapper.accountDownloadEnd(account)
        getLoop().call_soon(callbacks)
    def qualify(req_id, contract):
        def callbacks():
            # This uses the real SDK cache and accountValueEvent implementation
            # while real qualifyContracts() is pumping its request future.
            ib.wrapper.updateAccountValue('SettledCash', latest, 'USD', ACCOUNT)
            ib.wrapper.contractDetails(req_id, ContractDetails(contract=contract))
            ib.wrapper.contractDetailsEnd(req_id)
        getLoop().call_soon(callbacks)
    def place(order_id, contract, order):
        seen.extend(r.value for r in ib.accountValues()
                    if r.account == ACCOUNT and r.currency == 'USD' and r.tag == 'SettledCash')
        orders.append((order.totalQuantity, order.lmtPrice))
    monkeypatch.setattr(ib.client, 'reqAccountUpdates', refresh)
    monkeypatch.setattr(ib.client, 'reqContractDetails', qualify)
    monkeypatch.setattr(ib.client, 'placeOrder', place)
    plan.run(ib, prices={'SOXL': 25.})
    budget = 100. if latest == '100' else 0.
    assert sum(q * p for q, p in orders) <= budget, (seen, orders, budget)


@pytest.mark.parametrize('cash,step,price', [
    (100., 1., 25.), (100., .001, 25.137), (0.10, .001, .07),
    (37.31, .0001, 12.127), (500., 1., 99.999), (1.01, .01, .11),
])
def test_decimal_notional_within_settled_budget(native, cash, step, price):
    plan = native({'SOXL': 400.}, cash=500.)
    ib = FakeIB(cash=500., settled=cash)
    plan.run(ib, prices={'SOXL': price}, step=step)
    total = sum((Decimal(str(o['quantity'])) * Decimal(str(o['limit_price']))
                 for o in ib.orders), Decimal(0))
    assert total <= Decimal(str(cash))


@pytest.mark.parametrize('mode', ['missing', 'stale', 'negative', 'nonnumeric', 'currency', 'timeout'])
def test_invalid_settlement_preserves_qualified_sell_and_blocks_buy(native, mode):
    plan = native({'TQQQ': 0., 'SOXL': 400.}, cash=500.)
    ib = FakeIB(cash=500., settled=500.)
    rows = ib.accountValues()
    row = next(r for r in rows if r.tag == 'SettledCash')
    if mode == 'missing':
        rows.remove(row)
    elif mode == 'stale':
        row.stale = True
    elif mode in {'negative', 'nonnumeric'}:
        row.value = '-1' if mode == 'negative' else 'unknown'
    elif mode == 'currency':
        row.currency = 'HKD'
    else:
        def timeout(account):
            raise TimeoutError('synthetic timeout')
        ib.reqAccountUpdates = timeout
    ib.accountValues = lambda: rows
    plan.run(ib, positions={'TQQQ': {'quantity': 10.}}, prices={'TQQQ': 80., 'SOXL': 25.})
    assert [(o['symbol'], o['side'], o['quantity']) for o in ib.orders] == [('TQQQ', 'sell', 10)]


def emit_settlement(ib, value, **fields):
    row = NS(account=ACCOUNT, currency='USD', tag='SettledCash', value=value)
    for key, item in fields.items():
        setattr(row, key, item)
    ib.accountValueEvent.emit(row)
    return row


def test_resized_buy_binds_exact_journal_quantity_and_summary(native):
    plan = native({'SOXL': 400.}, cash=500.)
    ib = FakeIB(cash=500., settled=500.)
    calls = []
    qualify = ib.qualifyContracts
    def prepare(contract):
        calls.append(contract.symbol)
        # Event evidence restricts the budget even if a fake cache lags behind.
        row = emit_settlement(ib, '100')
        row.value = '500'  # Mutating the old event cannot restore authority.
        return qualify(contract)
    ib.qualifyContracts = prepare
    _, summary = plan.run(ib, prices={'SOXL': 25.})
    assert calls == ['SOXL']
    assert [o['quantity'] for o in ib.orders] == [3]
    attempt = next(iter(plan.session().read_state()['attempts'].values()))
    assert attempt['quantity'] == attempt['filled_quantity'] == 3
    assert attempt['certainty'] == 'TERMINAL'
    assert summary['orders_filled'][0]['quantity'] == 3
    assert len(ib.accountValueEvent) == 0


@pytest.mark.parametrize('value', ['0', '-1', 'unknown', 'nan', 'inf'])
def test_intervening_invalid_event_does_not_use_positive_cache(native, value):
    plan = native({'SOXL': 400.}, cash=500.)
    ib = FakeIB(cash=500., settled=500.)
    def prepare(contract):
        emit_settlement(ib, value)
        emit_settlement(ib, '500')  # A later increase cannot erase this restriction.
        return [contract]
    ib.qualifyContracts = prepare
    plan.run(ib, prices={'SOXL': 25.})
    assert ib.orders == []
    assert plan.session().read_state()['attempts'] == {}
    assert len(ib.accountValueEvent) == 0


@pytest.mark.parametrize('fields', [
    {'account': 'synthetic-other-account'}, {'currency': 'HKD'}, {'tag': 'CashBalance'},
])
def test_nonmatching_intervening_update_cannot_revoke_settled_authority(native, fields):
    plan = native({'SOXL': 400.}, cash=500.)
    ib = FakeIB(cash=500., settled=500.)
    def prepare(contract):
        emit_settlement(ib, '0', **fields)
        return [contract]
    ib.qualifyContracts = prepare
    plan.run(ib, prices={'SOXL': 25.})
    assert [o['quantity'] for o in ib.orders] == [16]
    assert len(ib.accountValueEvent) == 0


@pytest.mark.parametrize('latest', ['100', '1000'])
def test_qualification_update_keeps_shared_budget_and_reserve(native, latest):
    plan = native({'TQQQ': 400., 'SOXL': 400.}, cash=1000.)
    ib = FakeIB(cash=1000., settled=500.)
    def prepare(contract):
        if not ib.orders:
            emit_settlement(ib, latest)
        return [contract]
    ib.qualifyContracts = prepare
    plan.run(ib, prices={'TQQQ': 25., 'SOXL': 25.}, reserve=25.)
    total = sum(Decimal(str(o['quantity'])) * Decimal(str(o['limit_price'])) for o in ib.orders)
    assert total <= (Decimal('75') if latest == '100' else Decimal('475'))
    assert [o['quantity'] for o in ib.orders] == ([2] if latest == '100' else [15, 3])
    assert all(a['quantity'] == a['filled_quantity'] and a['certainty'] == 'TERMINAL'
               for a in plan.session().read_state()['attempts'].values())


@pytest.mark.parametrize('latest', ['0', '-1', 'unknown', 'nan', '100'])
def test_post_reservation_update_blocks_final_write_and_retains_unknown(native, monkeypatch, latest):
    from quant_platform_kit.common.execution_state import AccountSubmissionSession
    from quant_platform_kit.common.models import OrderIntent

    plan = native({'SOXL': 400.}, cash=500.)
    ib = FakeIB(cash=500., settled=500.)
    reserve = AccountSubmissionSession.reserve
    def reserve_then_update(session, order, **kwargs):
        grant = reserve(session, order, **kwargs)
        emit_settlement(ib, latest)
        return grant
    monkeypatch.setattr(AccountSubmissionSession, 'reserve', reserve_then_update)
    with pytest.raises(RuntimeError, match='settled_cash_changed'):
        plan.run(ib, prices={'SOXL': 25.})
    assert ib.orders == []
    attempts = plan.session().read_state()['attempts']
    assert len(attempts) == 1
    assert next(iter(attempts.values()))['certainty'] == 'UNKNOWN'
    with pytest.raises(RuntimeError, match='unresolved'):
        plan.session().reserve(OrderIntent(symbol='SOXL', side='buy', quantity=16,
            order_type='limit', limit_price=25., time_in_force='DAY', account_id=ACCOUNT))
    assert plan.session().read_state()['attempts'] == attempts
    assert len(ib.accountValueEvent) == 0


def test_newer_update_does_not_extend_original_deadline(native, monkeypatch):
    from application import execution_service as service

    plan = native({'SOXL': 400.}, cash=500.)
    ib = FakeIB(cash=500., settled=500.)
    offset = [0.]
    clock = service.time.monotonic
    monkeypatch.setattr(service.time, 'monotonic', lambda: clock() + offset[0])
    def prepare(contract):
        offset[0] += 31.
        emit_settlement(ib, '1000')
        return [contract]
    ib.qualifyContracts = prepare
    with pytest.raises(RuntimeError, match='settled_cash_stale'):
        plan.run(ib, prices={'SOXL': 25.})
    assert ib.orders == []
    assert {a['certainty'] for a in plan.session().read_state()['attempts'].values()} == {'UNKNOWN'}
    assert len(ib.accountValueEvent) == 0
