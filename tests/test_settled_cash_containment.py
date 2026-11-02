"""Actual pinned mapper/executor/journal/IBKR adapters, artificial inputs only.

The SDK-facing object is an in-memory fake. No patched risk/admission/rounding
functions, real accounts, remote storage, connections, or live-order authority.
"""
from tests.synthetic_account_refresh import SyntheticAccountRefresh
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest

from application import execution_service as service
from decision_mapper import map_strategy_decision
from quant_platform_kit.common.execution_state import (
    AccountSubmissionSession, ExecutionMarkerStore, submission_intent_digest,
    submission_release_digest,
)
from quant_platform_kit.common.capital_base import (
    CapitalBaseBinding, CapitalBaseSnapshot, CapitalScope, CapitalValuationBasis,
)
from quant_platform_kit.common.models import OrderIntent
from quant_platform_kit.common.strategy_contracts import PositionTarget, StrategyDecision
from quant_platform_kit.risk.contracts import RuntimeRiskLimits
from quant_platform_kit.risk.gate import apply_risk_gate

ACCOUNT = 'DU00000001'  # Generated artificial paper identity.
PROFILE = 'soxl_soxx_trend_income'
TODAY = datetime.now(ZoneInfo('America/New_York')).date().isoformat()


class FakeIB(SyntheticAccountRefresh):
    def __init__(self, *, cash=1000., settled=None, status='Filled', fill=None,
                 fill_price=None, pending=(), error=None):
        self.cash, self.settled = cash, cash if settled is None else settled
        self.status, self.fill, self.fill_price = status, fill, fill_price
        self.pending, self.error = list(pending), error
        self.orders = []

    def openTrades(self):
        return self.pending

    reqAllOpenOrders = openTrades

    def fills(self):
        return []

    def accountValues(self):
        return [NS(account=ACCOUNT, tag=tag, currency='USD', value=str(value))
                for tag, value in [('CashBalance', self.cash), ('SettledCash', self.settled),
                                   ('AvailableFunds', 10000.)]]

    def qualifyContracts(self, contract):
        return [contract]

    def placeOrder(self, contract, order):
        self.orders.append({'symbol': contract.symbol, 'side': order.action.lower(),
                            'quantity': order.totalQuantity, 'type': order.orderType,
                            'limit_price': getattr(order, 'lmtPrice', None)})
        if self.error:
            raise self.error
        filled = (order.totalQuantity if self.status == 'Filled' else 0) if self.fill is None else self.fill
        price = self.fill_price
        if price is None:
            price = order.lmtPrice if order.action.upper() == 'BUY' else 65.
        if filled and order.action.upper() == 'BUY':
            assert 0 < price <= order.lmtPrice
        return NS(order=NS(orderId=len(self.orders), account=ACCOUNT),
                  orderStatus=NS(status=self.status, filled=filled,
                                 avgFillPrice=price if filled else 0.))


def gated(values, diagnostics=None, profile=PROFILE):
    limits = RuntimeRiskLimits(allowed_symbols=tuple(values),
        product_leverage_factors={s: 3 for s in values}, nominal_caps={s: 1 for s in values},
        total_nominal_exposure_cap=1, total_effective_exposure_cap=3,
        max_positions=len(values))
    decision = StrategyDecision(positions=tuple(PositionTarget(s, target_value=v)
        for s, v in values.items()), diagnostics=diagnostics or {})
    # Weight translation is exercised by the real mapper after the real gate.
    binding = dict(account_scope='synthetic-account', runtime_scope='synthetic-runtime',
        strategy_scope=profile, target_currency='USD', capital_scope=CapitalScope.ACCOUNT,
        valuation_basis=CapitalValuationBasis.BROKER_ACCOUNT_NET_LIQUIDATION)
    capital = CapitalBaseSnapshot(**binding, reported_equity=2000., reported_currency='USD',
        fx_rate_to_target=1., as_of=datetime.now(timezone.utc), source_digest_sha256='a' * 64)
    return apply_risk_gate(decision, runtime_risk_limits=limits,
        portfolio_snapshot={'total_equity': 2000.}, enforce_value_target_exposure=True,
        capital_base=capital, capital_base_binding=CapitalBaseBinding(**binding))


@pytest.fixture
def native(monkeypatch, tmp_path):
    from scripts.validate_cloud_run_startup import _install_smoke_environment
    _install_smoke_environment()
    import main
    monkeypatch.delenv('ACCOUNT_NEW_RISK_GATE', raising=False)
    # Notification IO is independent of the financial path under examination.
    monkeypatch.setattr(service, 'maybe_publish_attention_for_admission', lambda *a, **k: {})

    def prepare(values, *, cash=1000., scale_half=False, diagnostics=None, profile=PROFILE):
        decision = gated(values, diagnostics, profile)
        projection = {'observation_status': 'COMPLETE', 'reconciliation_status': 'VERIFIED',
            'circuit_breaker_state': 'CLOSED', 'production_drift_status': 'healthy',
            'equity_usd': 2000., 'peak_equity_usd': 2000., 'realized_vol': .40 if scale_half else None}
        weights, _, _, _, metadata = map_strategy_decision(decision,
            strategy_profile=profile, runtime_metadata={'portfolio_total_equity': 2000.,
                'liquid_cash': cash, 'account_ids': (ACCOUNT,), 'trade_date': TODAY,
                'effective_date': TODAY, 'account_new_risk_snapshot': projection})
        receipt = {'attestation_state': 'self_attested', 'release_id': 'synthetic-release',
                   'strategy_release': {'strategy_revision': 'a' * 40}}
        def session():
            return AccountSubmissionSession(ExecutionMarkerStore(local_dir=tmp_path/'journal'),
                broker='ibkr', account_id=ACCOUNT, owner_id=profile, profile=profile,
                intent_id=submission_intent_digest(metadata, profile),
                release_id=submission_release_digest(receipt))
        def run(ib, *, positions=None, prices=None, reserve=0., step=1., minimum=0., cash_only=True, dry_run=False):
            return service.execute_rebalance(ib, weights, positions or {},
                {'equity': 2000., 'buying_power': cash},
                fetch_quote_snapshots=lambda _ib, symbols: {s: NS(last_price=(prices or {}).get(s, 100.)) for s in symbols},
                submit_order_intent=lambda ib, intent, **kw: main.submit_market_order_intent(
                    ib, intent, wait_seconds=0, **kw), order_intent_cls=OrderIntent,
                translator=lambda key, **kw: key, account_execution_session=session(),
                runtime_release_receipt=receipt, acquire_execution_claim=lambda: True,
                strategy_profile=profile, account_ids=(ACCOUNT,), signal_metadata=metadata,
                cash_only_execution=cash_only, dry_run_only=dry_run, execution_mode='paper', cash_reserve_ratio=0.,
                cash_reserve_floor_usd=reserve, rebalance_threshold_ratio=0.,
                limit_buy_premium=1., sell_settle_delay_sec=0., quantity_step=step,
                min_order_notional=minimum, execution_lock_dir=tmp_path/'locks', return_summary=True)
        return NS(decision=decision, weights=weights, metadata=metadata, session=session, run=run)
    return prepare




@pytest.mark.parametrize("settled", [0., 100., 500., -1.])
@pytest.mark.parametrize("symbol,profile", [("TQQQ", "tqqq_growth_income"), ("SOXL", PROFILE)])
def test_unverified_cash_ledger_cannot_admit_equity_buy(native, settled, symbol, profile):
    plan = native({symbol: 400.}, cash=500., profile=profile)
    ib = FakeIB(cash=500., settled=settled)
    # Amounts without an explicit settlement tag remain unknown.
    original = ib.accountValues
    ib.accountValues = lambda: [r for r in original() if r.tag != "SettledCash"]
    _, summary = plan.run(ib, prices={symbol: 25.})
    assert not ib.orders
    assert summary["execution_status"] == "blocked"
    assert summary["no_op_reason"] == "account_new_risk_gate"
    assert "SETTLED_CASH_LEDGER_UNVERIFIED" in summary["account_new_risk_reason_codes"]
    assert summary["lock_path"] is None
    assert plan.session().read_state()["attempts"] == {}


@pytest.mark.parametrize("status,fill", [("Filled", 10), ("Submitted", 0), ("PartiallyFilled", 4), ("Unknown", 0)])
def test_cash_containment_preserves_explicit_reduction_journal(native, status, fill):
    plan = native({"SOXL": 0.})
    ib = FakeIB(status=status, fill=fill, fill_price=65.)
    plan.run(ib, positions={"SOXL": {"quantity": 10.}}, prices={"SOXL": 80.})
    assert [(o["side"], o["quantity"]) for o in ib.orders] == [("sell", 10)]
    attempt = next(iter(plan.session().read_state()["attempts"].values()))
    assert attempt["filled_quantity"] == fill
    if status == "Unknown":
        assert attempt["certainty"] == "UNKNOWN"


def test_cash_containment_does_not_credit_sale_proceeds_for_new_buy(native):
    plan = native({"TQQQ": 0., "SOXL": 400.})
    ib = FakeIB(cash=0., settled=0.)
    plan.run(ib, positions={"TQQQ": {"quantity": 10.}}, prices={"TQQQ": 80., "SOXL": 25.})
    assert [(o["symbol"], o["side"]) for o in ib.orders] == [("TQQQ", "sell")]


def test_margin_path_is_not_changed_by_cash_containment(native):
    plan = native({"SOXL": 400.})
    ib = FakeIB(cash=500., settled=0.)
    plan.run(ib, prices={"SOXL": 25.}, cash_only=False)
    assert [(o["side"], o["quantity"]) for o in ib.orders] == [("buy", 16)]


@pytest.mark.parametrize("symbol,profile", [("TQQQ", "tqqq_growth_income"), ("SOXL", PROFILE)])
@pytest.mark.parametrize("settled,expected", [(500., 16), (100., 3), (0., 0), (-1., 0)])
def test_explicit_settled_cash_controls_buy_budget(native, symbol, profile, settled, expected):
    plan = native({symbol: 400.}, cash=500., profile=profile)
    ib = FakeIB(cash=500., settled=settled)
    plan.run(ib, prices={symbol: 25.})
    assert [(o["side"], o["quantity"]) for o in ib.orders] == ([("buy", expected)] if expected else [])
    assert sum(o["quantity"] * o["limit_price"] for o in ib.orders) <= max(0., settled)


@pytest.mark.parametrize("mode", ["missing", "error", "null", "nan", "infinity", "invalid",
                                  "conflict", "wrong_account", "wrong_currency", "anonymous"])
def test_unknown_settlement_fails_closed_before_lock(native, mode):
    plan = native({"SOXL": 400.}, cash=500.)
    ib = FakeIB(cash=500., settled=500.)
    rows = ib.accountValues()
    settled = next(r for r in rows if r.tag == "SettledCash")
    if mode == "missing":
        rows.remove(settled)
    elif mode == "null":
        rows = None
    elif mode in {"nan", "infinity", "invalid"}:
        settled.value = {"nan": "nan", "infinity": "inf", "invalid": "unknown"}[mode]
    elif mode == "conflict":
        rows.append(NS(account=ACCOUNT, currency="USD", tag="SettledCash", value="0"))
    elif mode == "wrong_account":
        settled.account = "synthetic-other-account"
    elif mode == "wrong_currency":
        settled.currency = "HKD"
    elif mode == "anonymous":
        settled.account = None
    def read():
        if mode == "error":
            raise RuntimeError("synthetic settlement read failure")
        return rows
    ib.accountValues = read
    _, summary = plan.run(ib, prices={"SOXL": 25.})
    assert ib.orders == []
    assert summary["execution_status"] == "blocked"
    assert summary["lock_path"] is None
    assert "SETTLED_CASH_LEDGER_UNVERIFIED" in summary["account_new_risk_reason_codes"]
    assert plan.session().read_state()["attempts"] == {}


def test_settled_budget_is_shared_by_symbols_and_reserve(native):
    plan = native({"TQQQ": 400., "SOXL": 400.}, cash=1000.)
    ib = FakeIB(cash=1000., settled=500.)
    plan.run(ib, prices={"TQQQ": 25., "SOXL": 25.}, reserve=100.)
    assert ib.orders
    assert all(o["side"] == "buy" for o in ib.orders)
    assert sum(o["quantity"] * o["limit_price"] for o in ib.orders) <= 400.


@pytest.mark.parametrize("settled", [0., 500., float("nan")])
def test_switch_off_cash_gate_does_not_create_reduction_targets(native, monkeypatch, settled):
    monkeypatch.setenv("PORTFOLIO_LOSS_STOPLOSS_ENABLED", "false")
    plan = native({"TQQQ": 250., "SOXL": 250.}, cash=500.)
    ib = FakeIB(cash=500., settled=settled)
    original_weights = dict(plan.weights)
    plan.run(ib, positions={"TQQQ": {"quantity": 10.}, "SOXL": {"quantity": 10.}},
             prices={"TQQQ": 25., "SOXL": 25.})
    assert ib.orders == []
    assert plan.weights == original_weights


@pytest.mark.parametrize("status", ["Filled", "Submitted"])
def test_sale_proceeds_do_not_increase_positive_settled_budget(native, status):
    plan = native({"TQQQ": 0., "SOXL": 400.}, cash=500.)
    ib = FakeIB(cash=500., settled=100.)
    ib.status = status
    plan.run(ib, positions={"TQQQ": {"quantity": 10.}},
             prices={"TQQQ": 80., "SOXL": 25.})
    buys = [o for o in ib.orders if o["side"] == "buy"]
    assert buys
    assert sum(o["quantity"] * o["limit_price"] for o in buys) <= 100.


def test_duplicate_settled_rows_do_not_double_budget(native):
    plan = native({"SOXL": 400.}, cash=500.)
    ib = FakeIB(cash=500., settled=100.)
    rows = ib.accountValues()
    rows.append(NS(account=ACCOUNT, currency="USD", tag="SettledCash", value="100"))
    ib.accountValues = lambda: rows
    plan.run(ib, prices={"SOXL": 25.})
    assert [(o["side"], o["quantity"]) for o in ib.orders] == [("buy", 3)]


@pytest.mark.parametrize("settled,expected", [(0., 0), (100., 3)])
def test_dry_run_sale_does_not_supply_settled_cash(native, settled, expected):
    plan = native({"TQQQ": 0., "SOXL": 400.}, cash=500.)
    ib = FakeIB(cash=500., settled=settled)
    _, summary = plan.run(ib, positions={"TQQQ": {"quantity": 10.}},
                          prices={"TQQQ": 80., "SOXL": 25.}, dry_run=True)
    assert ib.orders == []
    buys = [o for o in summary["orders_submitted"] if o["side"] == "buy"]
    assert [o["quantity"] for o in buys] == ([expected] if expected else [])


def test_every_selected_account_requires_settlement_evidence():
    ib = FakeIB(settled=500.)
    assert service._settled_cash_for_currency(
        ib, currency="USD", account_ids=(ACCOUNT, "synthetic-other-account")) is None


# Ported from the independent #955 review's four failing reproductions.
def test_stale_positive_settlement_must_block(native):
    plan = native({'SOXL': 400.}, cash=500.)
    ib = FakeIB(cash=500., settled=500.)
    rows = ib.accountValues()
    for row in rows:
        row.as_of = datetime(2000, 1, 1, tzinfo=timezone.utc)
        row.observed_at = row.as_of
        row.stale = True
    ib.accountValues = lambda: rows
    _, summary = plan.run(ib, prices={'SOXL': 25.})
    assert ib.orders == [], (ib.orders, summary.get('settled_cash_ledger_status'))


@pytest.mark.parametrize('latest', ['0', '-1', 'unknown'])
def test_updated_settlement_before_buy_must_block(native, latest):
    plan = native({'SOXL': 400.}, cash=500.)
    ib = FakeIB(cash=500., settled=500.)
    original = ib.accountValues
    calls = 0
    def read():
        nonlocal calls
        calls += 1
        rows = original()
        if calls > 1:
            next(r for r in rows if r.tag == 'SettledCash').value = latest
        return rows
    ib.accountValues = read
    plan.run(ib, prices={'SOXL': 25.})
    assert ib.orders == [], (latest, calls, ib.orders)


@pytest.mark.parametrize('sold', [False, True])
@pytest.mark.parametrize('latest', ['100', '0', '-1', 'unknown'])
def test_buy_phase_refresh_caps_settlement_with_or_without_sale(native, sold, latest):
    values = {'TQQQ': 0., 'SOXL': 400.} if sold else {'SOXL': 400.}
    plan = native(values, cash=500.)
    ib = FakeIB(cash=500., settled=500.)
    original = ib.reqAccountUpdates
    refreshes = []
    def refresh(account):
        refreshes.append((account, len(ib.orders)))
        if len(refreshes) > 1:
            ib.settled = latest
        original(account)
    ib.reqAccountUpdates = refresh
    _, summary = plan.run(ib, positions={'TQQQ': {'quantity': 10.}} if sold else {},
                          prices={'TQQQ': 80., 'SOXL': 25.})
    buys = [o for o in ib.orders if o['side'] == 'buy']
    assert len(refreshes) >= 2
    assert refreshes[0] == (ACCOUNT, 0)
    assert refreshes[1] == (ACCOUNT, int(sold))
    assert [o['quantity'] for o in buys] == ([3] if latest == '100' else [])
    if latest == 'unknown':
        assert summary['settled_cash_ledger_status'] == 'UNKNOWN'


@pytest.mark.parametrize('latest', ['100', '1000', '0', '-1', 'unknown'])
def test_each_buy_refresh_preserves_shared_spent_budget(native, latest):
    plan = native({'TQQQ': 400., 'SOXL': 400.}, cash=1000.)
    ib = FakeIB(cash=1000., settled=500.)
    original = ib.reqAccountUpdates
    refreshes = []
    def refresh(account):
        refreshes.append(len(ib.orders))
        if ib.orders:
            ib.settled = latest
        original(account)
    ib.reqAccountUpdates = refresh
    plan.run(ib, prices={'TQQQ': 25., 'SOXL': 25.}, reserve=25.)
    assert len(refreshes) == 4  # Planning, phase, first buy, second buy.
    expected = [15, 2] if latest == '100' else ([15, 3] if latest == '1000' else [15])
    assert [o['quantity'] for o in ib.orders] == expected
    assert sum(o['quantity'] * o['limit_price'] for o in ib.orders) <= 475.


@pytest.mark.parametrize('mode', ['missing_api', 'missing_event', 'cached_only', 'timeout',
                                  'error', 'stale', 'old_as_of', 'old_observed_at',
                                  'future', 'naive', 'malformed_timestamp', 'slow'])
def test_refresh_evidence_failures_block_buys_and_restore_sdk_settings(native, monkeypatch, mode):
    from datetime import timedelta
    plan = native({'SOXL': 400.}, cash=500.)
    ib = FakeIB(cash=500., settled=500.)
    ib.RequestTimeout, ib.RaiseRequestErrors = .5, False
    original = ib.reqAccountUpdates
    # Keep a reference to verify that temporary observers are removed.
    event = ib.accountValueEvent
    if mode == 'missing_api':
        ib.reqAccountUpdates = None
    elif mode == 'missing_event':
        ib._synthetic_account_value_event = None
    elif mode == 'cached_only':
        ib.reqAccountUpdates = lambda _account: None
    elif mode in {'timeout', 'error'}:
        def fail(account):
            original(account)  # Partial data without completion stays unknown.
            raise TimeoutError() if mode == 'timeout' else RuntimeError('synthetic refresh error')
        ib.reqAccountUpdates = fail
    elif mode == 'slow':
        clock = iter([0., 0., 0., 0., 31.])
        monkeypatch.setattr(service.time, 'monotonic', lambda: next(clock))
    else:
        rows = ib.accountValues()
        row = next(r for r in rows if r.tag == 'SettledCash')
        now = datetime.now(timezone.utc)
        if mode == 'stale':
            row.stale = True
        elif mode in {'old_as_of', 'old_observed_at'}:
            setattr(row, 'as_of' if mode == 'old_as_of' else 'observed_at', now - timedelta(seconds=31))
        elif mode == 'future':
            row.observed_at = now + timedelta(minutes=1)
        elif mode == 'naive':
            row.observed_at = now.replace(tzinfo=None)
        else:
            row.observed_at = 'unknown'
        ib.accountValues = lambda: rows
    _, summary = plan.run(ib, prices={'SOXL': 25.})
    assert ib.orders == []
    assert summary['settled_cash_ledger_status'] == 'UNKNOWN'
    assert summary['lock_path'] is None
    assert ib.RequestTimeout == .5
    assert ib.RaiseRequestErrors is False
    assert len(event) == 0


@pytest.mark.parametrize('latest', ['timeout', 'cached_only'])
def test_pre_buy_refresh_failure_cannot_reuse_initial_balance(native, latest):
    plan = native({'SOXL': 400.}, cash=500.)
    ib = FakeIB(cash=500., settled=500.)
    original = ib.reqAccountUpdates
    refreshes = []
    def refresh(account):
        refreshes.append(account)
        if len(refreshes) == 1:
            original(account)
        elif latest == 'timeout':
            raise TimeoutError()
    ib.reqAccountUpdates = refresh
    _, summary = plan.run(ib, prices={'SOXL': 25.})
    assert ib.orders == []
    assert len(refreshes) >= 2
    assert summary['settled_cash_ledger_status'] == 'UNKNOWN'


def test_settlement_expiring_during_buy_sizing_blocks_submission(native, monkeypatch):
    plan = native({'SOXL': 400.}, cash=500.)
    ib = FakeIB(cash=500., settled=500.)
    offset = [0.]
    monotonic = service.time.monotonic
    monkeypatch.setattr(service.time, 'monotonic', lambda: monotonic() + offset[0])
    original = service._planned_buy_order_quantity
    def size(*a, **k):
        result = original(*a, **k)
        # Planning happens before execution; only delay the actual buy sizing.
        if ib.__dict__.get('_refresh_count', 0) >= 3:
            offset[0] += 31.
        return result
    refresh = ib.reqAccountUpdates
    def count(account):
        ib._refresh_count = ib.__dict__.get('_refresh_count', 0) + 1
        refresh(account)
    ib.reqAccountUpdates = count
    monkeypatch.setattr(service, '_planned_buy_order_quantity', size)
    _, summary = plan.run(ib, prices={'SOXL': 25.})
    assert ib.orders == []
    assert 'settled_cash_stale:SOXL' in summary['skipped_reasons']


@pytest.mark.parametrize('mode', ['complete', 'cached_only', 'partial', 'wrong_scope'])
def test_pinned_sdk_account_refresh_requires_updated_settlement_and_completion(monkeypatch, mode):
    # Exercise the pinned SDK's actual future, cache and callbacks without sockets.
    from ib_insync import IB
    from ib_insync.util import getLoop
    ib = IB()
    ib.RequestTimeout = .01
    ib.RaiseRequestErrors = False
    ib.wrapper.updateAccountValue('SettledCash', '500', 'USD', ACCOUNT)
    def request(subscribe, account):
        assert subscribe is True
        assert account == ACCOUNT
        assert ib.RequestTimeout == .01
        assert ib.RaiseRequestErrors is True
        def callbacks():
            if mode in {'complete', 'partial'}:
                ib.wrapper.updateAccountValue('SettledCash', '100', 'USD', account)
            elif mode == 'wrong_scope':
                ib.wrapper.updateAccountValue('SettledCash', '100', 'USD', 'synthetic-other-account')
            if mode != 'partial':
                ib.wrapper.accountDownloadEnd(account)
        getLoop().call_soon(callbacks)
    monkeypatch.setattr(ib.client, 'reqAccountUpdates', request)
    result = service._settled_cash_for_currency(ib, currency='USD', account_ids=(ACCOUNT,))
    assert result == (100. if mode == 'complete' else None)
    assert ib.RequestTimeout == .01
    assert ib.RaiseRequestErrors is False
    assert len(ib.accountValueEvent) == 0
