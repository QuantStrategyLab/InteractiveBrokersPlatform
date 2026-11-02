"""Actual pinned mapper/executor/journal/IBKR adapters, artificial inputs only.

The SDK-facing object is an in-memory fake. No patched risk/admission/rounding
functions, real accounts, remote storage, connections, or live-order authority.
"""
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


class FakeIB:
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
        def run(ib, *, positions=None, prices=None, reserve=0., step=1., minimum=0., cash_only=True):
            return service.execute_rebalance(ib, weights, positions or {},
                {'equity': 2000., 'buying_power': cash},
                fetch_quote_snapshots=lambda _ib, symbols: {s: NS(last_price=(prices or {}).get(s, 100.)) for s in symbols},
                submit_order_intent=lambda ib, intent, **kw: main.submit_market_order_intent(
                    ib, intent, wait_seconds=0, **kw), order_intent_cls=OrderIntent,
                translator=lambda key, **kw: key, account_execution_session=session(),
                runtime_release_receipt=receipt, acquire_execution_claim=lambda: True,
                strategy_profile=profile, account_ids=(ACCOUNT,), signal_metadata=metadata,
                cash_only_execution=cash_only, dry_run_only=False, execution_mode='paper', cash_reserve_ratio=0.,
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
