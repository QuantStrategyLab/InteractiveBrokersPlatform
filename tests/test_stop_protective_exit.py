"""Stop diagnostics through the native journal/adapters; synthetic venue only."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest

from application import execution_service as service
from application.protective_exit import stop_submission_identity
from decision_mapper import map_strategy_decision
from quant_platform_kit.common.execution_state import (
    AccountSubmissionSession, ExecutionMarkerStore, submission_intent_digest,
    submission_release_digest,
)
from quant_platform_kit.common.models import OrderIntent, PortfolioSnapshot, Position
from quant_platform_kit.common.strategy_contracts import PositionTarget, StrategyDecision
from us_equity_strategies.entrypoints._common import apply_risk_gate

ACCOUNT = "DU00000001"  # Artificial identity, never a real account.
TODAY = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
RECEIPT = {"attestation_state": "self_attested", "release_id": "synthetic-stop-release",
           "strategy_release": {"strategy_revision": "a" * 40}}


class FakeIB:
    def __init__(self, status="Filled", fill=None, error=None, pending=()):
        self.status, self.fill, self.error = status, fill, error
        self.pending, self.orders = list(pending), []

    def reqAllOpenOrders(self):
        return self.pending

    def openTrades(self):
        return self.pending

    def qualifyContracts(self, contract):
        return [contract]

    def placeOrder(self, contract, order):
        self.orders.append({"symbol": contract.symbol, "side": order.action,
                            "quantity": order.totalQuantity, "order_type": order.orderType})
        if self.error:
            raise self.error
        fill = (order.totalQuantity if self.status == "Filled" else 0) if self.fill is None else self.fill
        return NS(order=NS(orderId=len(self.orders), account=ACCOUNT),
                  orderStatus=NS(status=self.status, filled=fill, avgFillPrice=65. if fill else 0.))

    def isConnected(self):
        return False


def mapped(symbols=("TQQQ",), *, profile="tqqq_growth_income"):
    # Actual source diagnostics: 10 shares cost 100, marked at 65; -35% of NAV.
    snapshot = PortfolioSnapshot(as_of=datetime.now(timezone.utc), total_equity=1000. * len(symbols),
        buying_power=0., positions=tuple(Position(s, 10., 650., average_cost=100.) for s in symbols))
    decision = apply_risk_gate(StrategyDecision(positions=tuple(
        PositionTarget(s, target_weight=.2) for s in symbols)), portfolio_snapshot=snapshot)
    assert decision.risk_flags == ("rejected:stop_loss",)
    assert decision.diagnostics["unrealized_pnl_pct"] == -.35
    result = map_strategy_decision(decision, strategy_profile=profile,
        runtime_metadata={"portfolio_total_equity": snapshot.total_equity,
                          "managed_symbols": symbols, "account_ids": (ACCOUNT,),
                          "trade_date": TODAY, "effective_date": TODAY})
    return decision, result


def evidence(name, summary, ib):
    root = os.environ.get("RC_EVIDENCE_DIR")
    if root:
        path = Path(root) / "stop-cases"
        path.mkdir(exist_ok=True)
        (path / (name + ".json")).write_text(json.dumps(
            {"synthetic": True, "summary": summary, "orders": ib.orders}, indent=2) + "\n")


@pytest.fixture
def native(tmp_path):
    # No import/module stubs; inert startup settings, then the real final wrapper.
    from scripts.validate_cloud_run_startup import _install_smoke_environment
    _install_smoke_environment()
    import main

    def run(ib, symbols=("TQQQ",), *, metadata=None, dry_run=False, claim=lambda: True,
            profile="tqqq_growth_income", positions=None, admission=False, session_account=ACCOUNT):
        _, projection = mapped(symbols, profile=profile)
        weights, _, _, _, default_metadata = projection
        metadata = default_metadata if metadata is None else metadata
        session = AccountSubmissionSession(ExecutionMarkerStore(local_dir=tmp_path / "journal"),
            broker="ibkr", account_id=session_account, owner_id=profile, profile=profile,
            intent_id=submission_intent_digest(stop_submission_identity(metadata), profile), release_id=submission_release_digest(RECEIPT))
        intents = []

        def submit(ib, intent, **kwargs):
            intents.append(intent)
            return main.submit_market_order_intent(ib, intent, wait_seconds=0, **kwargs)

        logs, summary = service.execute_rebalance(ib, weights,
            {s: {"quantity": 10., "avg_cost": 100.} for s in symbols} if positions is None else positions,
            {"equity": 1000., "buying_power": 0.}, signal_metadata=metadata,
            fetch_quote_snapshots=(lambda _ib, symbols: {s: NS(last_price=65.) for s in symbols}) if admission else
                (lambda *_a, **_k: pytest.fail("stop market exit needs no quote")),
            submit_order_intent=submit, order_intent_cls=OrderIntent, translator=lambda key, **kw: key,
            acquire_execution_claim=claim, account_execution_session=session,
            strategy_profile=profile, account_ids=(ACCOUNT,), runtime_release_receipt=RECEIPT,
            dry_run_only=dry_run, execution_mode="paper", cash_reserve_ratio=0.,
            rebalance_threshold_ratio=.99, min_order_notional=5000., limit_buy_premium=1.,
            sell_settle_delay_sec=0., return_summary=True, paper_execution_admission_enabled=admission)
        return summary, session, intents
    return run


@pytest.mark.parametrize("symbol,profile", [("TQQQ", "tqqq_growth_income"), ("SOXL", "soxl_soxx_trend_income")])
def test_stop_rejection_maps_only_protective_targets(symbol, profile):
    decision, (weights, _, _, _, metadata) = mapped((symbol,), profile=profile)
    assert decision.positions == ()
    assert weights == {symbol: 0.}
    assert metadata["actionable"] is True
    assert metadata["risk_gate"] == "REJECT"
    assert "risk_authority" not in metadata
    assert metadata["stop_event_id"]
    assert metadata["protective_exit"]["symbols"] == (symbol,)


@pytest.mark.parametrize("status,fill,remaining,final", [
    ("Filled", 10., 0., "PROVEN"), ("PartiallyFilled", 4., 6., "UNKNOWN"),
    ("Rejected", 0., 10., "PROVEN"), ("Submitted", 0., None, "UNKNOWN"),
    ("Unknown", 0., None, "UNKNOWN"),
])
@pytest.mark.parametrize("symbol,profile", [("TQQQ", "tqqq_growth_income"), ("SOXL", "soxl_soxx_trend_income")])
def test_native_stop_gap_partial_reject_ack_unknown(native, status, fill, remaining, final, symbol, profile):
    ib = FakeIB(status, fill)
    summary, session, intents = native(ib, (symbol,), profile=profile)
    assert ib.orders == [{"symbol": symbol, "side": "SELL", "quantity": 10., "order_type": "MKT"}]
    row = summary["protective_exits"][0]
    assert row["stop_event_id"] == summary["stop_event_id"] == intents[0].metadata["stop_event_id"]
    assert row["remaining_quantity"] == remaining
    assert row["final_fill_outcome"] == final
    attempt = next(iter(session.read_state()["attempts"].values()))
    assert attempt["filled_quantity"] == fill
    assert row["attempt_id"] in session.read_state()["attempts"]
    if fill:
        assert row["average_fill_price"] == 65.
    assert summary["outcome_unknown"] == (final == "UNKNOWN")
    evidence(symbol + "-" + status, summary, ib)


@pytest.mark.parametrize("error", [TimeoutError("synthetic timeout"), OSError("synthetic 503")], ids=["timeout", "5xx"])
def test_native_stop_transport_unknown_and_restart(native, error):
    ib = FakeIB(error=error)
    summary, session, _ = native(ib)
    assert summary["stop_event_id"]
    assert summary["protective_exits"][0]["remaining_quantity"] is None
    assert summary["protective_exits"][0]["final_fill_outcome"] == "UNKNOWN"
    assert next(iter(session.read_state()["attempts"].values()))["certainty"] == "UNKNOWN"
    retry, _, intents = native(ib)
    assert retry["stop_event_id"] == summary["stop_event_id"]
    assert retry["protective_exits"][0]["remaining_quantity"] is None
    assert retry["outcome_unknown"] is True
    assert len(ib.orders) == 1 and intents == []
    evidence("timeout" if isinstance(error, TimeoutError) else "5xx", summary, ib)
    evidence("restart-" + type(error).__name__, retry, ib)


def test_joint_scope_stop_does_not_overwrite_other_symbol(native):
    ib = FakeIB()
    summary, session, intents = native(ib, ("TQQQ", "SOXL"), profile="soxl_soxx_trend_income")
    assert {o["symbol"] for o in ib.orders} == {"TQQQ", "SOXL"}
    assert len(session.read_state()["attempts"]) == len(intents) == 2
    assert all(row["remaining_quantity"] == 0. for row in summary["protective_exits"])
    assert all(i.metadata["stop_event_id"] == summary["stop_event_id"] for i in intents)
    evidence("joint-scope", summary, ib)


def test_pending_managed_order_blocks_reduction(native):
    pending = NS(contract=NS(symbol="TQQQ", secType="STK"), order=NS(account=ACCOUNT),
                 orderStatus=NS(status="Submitted"))
    ib = FakeIB(pending=[pending])
    summary, session, intents = native(ib)
    assert not ib.orders and not intents and not session.read_state()["attempts"]
    assert summary["execution_status"] == "blocked"
    assert summary["protective_exits"][0]["remaining_quantity"] is None


def test_dry_run_has_no_journal_or_submit(native):
    ib = FakeIB()
    summary, session, intents = native(ib, dry_run=True)
    assert not ib.orders and not intents and not session.read_state()["attempts"]
    assert summary["execution_status"] == "dry_run_completed"
    assert summary["protective_exits"][0]["remaining_quantity"] == 10.


@pytest.mark.parametrize("mutation", ["positive_target", "different_account", "wrong_event", "wrong_timing", "non_stop"])
def test_forged_or_modified_exit_metadata_cannot_submit(native, mutation):
    _, (_, _, _, _, metadata) = mapped()
    metadata = deepcopy(metadata)
    if mutation == "positive_target":
        metadata["allocation"]["targets"]["TQQQ"] = .1
    elif mutation == "different_account":
        metadata["protective_exit"]["account_ids"] = ("DU00000002",)
    elif mutation == "wrong_event":
        metadata["stop_event_id"] = "0" * 64
    elif mutation == "wrong_timing":
        metadata["execution_timing_contract"] = "changed-stop-identity"
    else:
        metadata["risk_flags"] = ("rejected:circuit_breaker",)
    ib = FakeIB()
    with pytest.raises((RuntimeError, ValueError)):
        native(ib, metadata=metadata)
    assert ib.orders == []


def test_enabled_paper_admission_still_requires_a_command(native):
    ib = FakeIB()
    summary, session, intents = native(ib, admission=True)
    assert summary["no_op_reason"] == "paper_execution_admission_blocked"
    assert summary["paper_execution_admission"]["broker_write_allowed"] is False
    assert not ib.orders and not intents and not session.read_state()["attempts"]


@pytest.fixture
def cycle(native, tmp_path):
    from application.rebalance_service import run_strategy_core
    from application.runtime_dependencies import IBKRRebalanceConfig, IBKRRebalanceRuntime
    from quant_platform_kit.common.port_adapters import CallableNotificationPort, CallablePortfolioPort
    import main
    store = ExecutionMarkerStore(local_dir=tmp_path / "cycle-state")

    def run(ib, symbol="TQQQ", profile="tqqq_growth_income"):
        _, projection = mapped((symbol,), profile=profile)
        snapshot = PortfolioSnapshot(as_of=datetime.now(timezone.utc), total_equity=1000., buying_power=0.,
            positions=(Position(symbol, 10., 650., average_cost=100.),), metadata={"account_ids": (ACCOUNT,)})
        config = IBKRRebalanceConfig(translator=lambda key, **kw: key, separator="---",
            strategy_profile=profile, account_ids=(ACCOUNT,), execution_dedup_enabled=True,
            execution_state_store=store, execution_state_account_scope="synthetic-paper",
            runtime_release_receipt=RECEIPT, reconciliation_output_path=tmp_path / (profile + ".json"))

        def execute(ib, weights, positions, values, **kwargs):
            return service.execute_rebalance(ib, weights, positions, values, **kwargs,
                fetch_quote_snapshots=lambda *_a, **_k: pytest.fail("unexpected quote"),
                submit_order_intent=lambda ib, intent, **kw: main.submit_market_order_intent(
                    ib, intent, wait_seconds=0, **kw), order_intent_cls=OrderIntent,
                translator=lambda key, **kw: key, strategy_profile=profile, account_ids=(ACCOUNT,),
                runtime_release_receipt=RECEIPT, execution_mode="paper", cash_reserve_ratio=0.,
                rebalance_threshold_ratio=.05, limit_buy_premium=1., sell_settle_delay_sec=0., return_summary=True)
        runtime = IBKRRebalanceRuntime(connect_ib=lambda: ib,
            portfolio_port_factory=lambda _ib: CallablePortfolioPort(lambda: snapshot),
            compute_signals=lambda *_a: projection, execute_rebalance=execute,
            notifications=CallableNotificationPort(lambda _message: None))
        return run_strategy_core(runtime=runtime, config=config)
    return run, store


def test_full_cycle_stop_reaches_producer_and_restart_is_not_success(cycle):
    run, _ = cycle
    ib = FakeIB("Submitted", 0.)
    first = run(ib)
    assert len(ib.orders) == 1
    assert first.execution_summary["protective_exits"][0]["remaining_quantity"] is None
    second = run(ib)
    assert len(ib.orders) == 1
    assert second.execution_summary["stop_event_id"] == first.signal_metadata["stop_event_id"]
    assert second.execution_summary["protective_exits"][0]["remaining_quantity"] is None
    assert second.execution_summary["outcome_unknown"] is True
    assert second.execution_summary["execution_status"] == "blocked"
    evidence("cycle-restart", second.execution_summary, ib)


def test_same_account_two_profiles_preserve_owner_fence(cycle):
    run, _ = cycle
    ib = FakeIB()
    first = run(ib)
    second = run(ib, symbol="SOXL", profile="soxl_soxx_trend_income")
    assert [o["symbol"] for o in ib.orders] == ["TQQQ"]
    assert first.execution_summary["protective_exits"][0]["remaining_quantity"] == 0.
    assert second.execution_summary["execution_status"] == "blocked"
    assert second.execution_summary["stop_event_id"]
    assert second.execution_summary["protective_exits"][0]["remaining_quantity"] is None
    evidence("two-profiles-same-account", second.execution_summary, ib)


def test_stop_does_not_consume_ordinary_report_dedup(cycle, monkeypatch):
    run, store = cycle
    # The ordinary report index does not carry stop_event_id and cannot attest
    # liquidation. Its answer must not suppress the distinct protective event.
    monkeypatch.setattr(type(store), "has_prior_execution_report", lambda self, **kw: True)
    ib = FakeIB()
    result = run(ib)
    assert len(ib.orders) == 1
    assert result.execution_summary["protective_exits"][0]["remaining_quantity"] == 0.


@pytest.mark.parametrize("status", ["Submitted", "PartiallyFilled"])
def test_independent_process_restart_never_reclaims_a_stop(native, status):
    import subprocess
    import sys
    ib = FakeIB(status, 4. if status == "PartiallyFilled" else 0.)
    summary, session, _ = native(ib)
    code = r"""
import json
import sys
from pathlib import Path
from scripts.validate_cloud_run_startup import _install_smoke_environment
_install_smoke_environment()
import main
from tests.test_stop_protective_exit import mapped, ACCOUNT, RECEIPT, FakeIB
from application.execution_service import execute_rebalance
from application.protective_exit import stop_submission_identity
from quant_platform_kit.common.execution_state import AccountSubmissionSession, ExecutionMarkerStore, submission_intent_digest, submission_release_digest
from quant_platform_kit.common.models import OrderIntent
_, (weights, _, _, _, metadata) = mapped()
session = AccountSubmissionSession(ExecutionMarkerStore(local_dir=Path(sys.argv[1])), broker='ibkr', account_id=ACCOUNT,
    owner_id='tqqq_growth_income', profile='tqqq_growth_income', intent_id=submission_intent_digest(stop_submission_identity(metadata), 'tqqq_growth_income'),
    release_id=submission_release_digest(RECEIPT))
ib = FakeIB()
def forbidden(*a, **kw):
    raise AssertionError('restart must never dispatch')
_, summary = execute_rebalance(ib, weights, {'TQQQ': {'quantity': 10.}}, {'equity': 1000.},
    signal_metadata=metadata, fetch_quote_snapshots=forbidden, submit_order_intent=forbidden, order_intent_cls=OrderIntent,
    translator=lambda k, **kw: k, account_execution_session=session, strategy_profile='tqqq_growth_income',
    account_ids=(ACCOUNT,), runtime_release_receipt=RECEIPT, acquire_execution_claim=forbidden,
    cash_reserve_ratio=0., rebalance_threshold_ratio=.05, limit_buy_premium=1., sell_settle_delay_sec=0., return_summary=True)
assert summary['outcome_unknown'] is True
assert summary['protective_exits'][0]['remaining_quantity'] is None
assert not ib.orders
Path(sys.argv[2]).write_text(json.dumps(summary))
"""
    output = Path(session.store.local_dir).parent / "process-restart.json"
    result = subprocess.run([sys.executable, "-c", code, str(session.store.local_dir), str(output)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    recovered = json.loads(output.read_text())
    assert recovered["stop_event_id"] == summary["stop_event_id"]
    evidence("process-restart-" + status, recovered, ib)


def test_wrong_physical_account_session_cannot_sell_snapshot(native):
    ib = FakeIB()
    summary, session, intents = native(ib, session_account="DU00000002")
    assert summary["no_op_reason"] == "stop_account_session_binding_mismatch"
    assert not ib.orders and not intents and not session.read_state()["attempts"]


@pytest.mark.parametrize("field", ["signal_date", "effective_date", "snapshot_as_of"])
def test_same_stop_event_does_not_retry_after_source_dates_change(native, field):
    ib = FakeIB()
    first, _, _ = native(ib)
    _, (_, _, _, _, metadata) = mapped()
    metadata[field] = "2026-01-02"
    retry, _, intents = native(ib, metadata=metadata)
    assert retry["stop_event_id"] == first["stop_event_id"]
    assert retry["execution_status"] == "blocked"
    assert len(ib.orders) == 1 and intents == []


def test_concurrent_foreign_attempt_cannot_prove_this_stop_filled(native, monkeypatch):
    from quant_platform_kit.common.execution_state import AccountSubmissionError
    from quant_platform_kit.common.models import ExecutionReport
    reserve = AccountSubmissionSession.reserve

    def competing_writer(session, order):
        # Deterministically interleave a different real journal writer between
        # producer reads, then fail the current reservation. No venue dispatch.
        other = AccountSubmissionSession(session.store, broker="ibkr", account_id=ACCOUNT,
            owner_id=session.owner_id, profile=session.profile, intent_id="b" * 64,
            release_id=session.release_id)
        foreign = OrderIntent("SOXL", "sell", 7., account_id=ACCOUNT)
        grant = reserve(other, foreign)
        other.dispatch(grant, foreign, lambda: ExecutionReport("SOXL", "sell", 7., "Filled",
            filled_quantity=7., average_fill_price=65., broker_order_id="synthetic-other-order"))
        raise AccountSubmissionError("synthetic reservation contention")

    monkeypatch.setattr(AccountSubmissionSession, "reserve", competing_writer)
    ib = FakeIB()
    summary, _, intents = native(ib)
    assert summary["no_op_reason"] == "stop_submission_observation_binding_mismatch"
    assert summary["outcome_unknown"] is True
    assert summary["protective_exits"][0]["remaining_quantity"] is None
    assert not ib.orders and not intents
