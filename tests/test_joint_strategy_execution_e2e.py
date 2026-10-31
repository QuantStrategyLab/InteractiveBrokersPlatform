from tests.candidate_dependency_guard import require_candidate_dependencies

require_candidate_dependencies()

BROKER = "ibkr"

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from application.daily_strategy_state import DailyStateBinding
from application.decision_cycle import decision_cycle
from application.joint_strategy_execution import evaluate_joint_tqqq
from quant_platform_kit.common.account_domain import account_domain_key
from quant_platform_kit.common.account_identity import BrokerAccountIdentity
from quant_platform_kit.common.execution_state import AccountSubmissionSession, ExecutionMarkerStore, submission_binding_digest
from quant_platform_kit.common.models import PortfolioSnapshot, Position
from quant_platform_kit.common.strategy_contracts import StrategyContext
from quant_platform_kit.common.strategy_risk_state import StrategyRiskStateIdentity, StrategyRiskStateStore, build_strategy_risk_state_transition
from us_equity_strategies.entrypoints import compute_tqqq_growth_income_decision


def fixture(tmp_path, broker):
    from quant_platform_kit.common.execution_state import submission_release_digest
    receipt = {"attestation_state":"self_attested", "release_id":"synthetic-test-release", "strategy_release":{"strategy_revision":"a" * 40}}
    release = submission_release_digest(receipt)
    account = "U00000001" if broker == "ibkr" else "synthetic-account-hash"
    domain = {"identity": {"environment": "LIVE", "platform_id": broker, "server": "synthetic",
        "account_id_fingerprint": "sha256:" + "1"*64, "lifecycle_id": "one"},
        "policy": {"capital_kind": "OWN_REAL", "product": "BROKERAGE", "phase": "LIVE", "account_type": "CASH",
            "style": "INTRADAY", "preference": "CAPITAL_PRESERVATION", "currency": "USD", "source": "synthetic",
            "effective_at": "2026-10-01T00:00:00Z", "valid_until": "2026-11-01T00:00:00Z", "version": "one"}}
    observed = BrokerAccountIdentity(platform_id=broker, account_types=("CASH",), account_modes=("LIVE",),
        account_id_fingerprint="sha256:" + "1"*64)
    store = ExecutionMarkerStore(local_dir=tmp_path)
    session = AccountSubmissionSession(store, broker=broker, account_id=account, owner_id="owner",
        profile="tqqq_growth_income", intent_id="a"*64, release_id=release, account_domain=domain,
        observed_account_id=account, broker_identity=observed, observed_at="2026-10-30T02:00:00Z",
        decision_at="2026-10-30T02:00:00Z", observed_environment="LIVE", observed_server="synthetic",
        observed_currency="USD", observed_lifecycle_id="one", clock=lambda: "2026-10-30T02:00:00Z")
    identity = StrategyRiskStateIdentity("tqqq_growth_income", "synthetic", "candidate", "c"*64)
    intent = {"id": "intent-1", "alpha_epoch": 1, "signal_session": "2026-10-30"}
    ctx = StrategyContext(as_of="2026-10-30", portfolio=PortfolioSnapshot(as_of=datetime(2026,10,30,tzinfo=timezone.utc),
        total_equity=100000., buying_power=150., cash_balance=150., positions=(Position(symbol="TQQQ", quantity=10, market_value=8000.),
            Position(symbol="QQQM", quantity=20, market_value=10000.), Position(symbol="BIL", quantity=818.5, market_value=81850.)), metadata={"account_id":account, "currency":"USD", "source_digest":"sha256:" + "2" * 64}),
        market_data={"benchmark_history": [{"close": 100+i*.5} for i in range(260)]}, runtime_config={"income_layer_enabled": False})
    from us_equity_strategies.conditional_tqqq_candidate import canonical_tqqq_context_sha256
    input_sha = canonical_tqqq_context_sha256(ctx)
    transition = build_strategy_risk_state_transition(identity=identity, effective_session="2026-10-30", input_sha256=input_sha,
        state={"schema_version": "tqqq_daily_state.v1", "session": "2026-10-30", "alpha_trend_active": True,
            "alpha_epoch": 1, "volatility_latch_active": False, "pending_intent": intent, "intent_invalidation_reason": None})
    legacy = StrategyRiskStateStore(local_dir=tmp_path / "legacy")
    legacy.append(transition)
    session.enroll_strategy_state(transition, legacy_store=legacy, migration_authorization={
        "account_domain_key": account_domain_key(domain), "account_binding_sha256": submission_binding_digest({"broker": broker, "account_id": account}),
        "owner_id": "owner", "release_id": release, "legacy_head_sha256": transition.transition_sha256,
        "strategy_identity": identity.to_dict(), "authority_receipt_sha256": "e"*64})
    binding = DailyStateBinding(legacy, identity, "2026-10-30", input_sha, None, fresh_intent=intent, joint_planning_session=session)
    entrypoint = SimpleNamespace(manifest=SimpleNamespace(profile="tqqq_growth_income"), evaluate=compute_tqqq_growth_income_decision)
    return session, transition, ctx, binding, entrypoint, receipt


@pytest.mark.parametrize("scenario", ["normal", "changed_context", "changed_target", "changed_order", "changed_risk", "duplicate", "partial", "unknown", "timeout"])
def test_joint_strategy_actual_ibkr_wrapper_dispatches_once(tmp_path, monkeypatch, strategy_module_factory, scenario):
    import json
    from datetime import datetime as RealDateTime
    from zoneinfo import ZoneInfo
    from application.execution_service import execute_rebalance
    from application.joint_account_facts import JointAccountFacts
    from decision_mapper import map_strategy_decision
    from notifications.telegram import build_translator
    from quant_platform_kit.common.execution_state import submission_intent_digest, AccountSubmissionError
    from quant_platform_kit.common.models import OrderIntent, ExecutionReport

    class FixedDateTime(RealDateTime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 30, 12, tzinfo=ZoneInfo("America/New_York")).astimezone(tz)

    monkeypatch.setattr("application.execution_service.datetime", FixedDateTime)
    session, transition, ctx, binding, entrypoint, receipt = fixture(tmp_path, BROKER)
    snapshot = {"balance": 100000, "equity": 100000, "cash": 150, "currency": "USD",
        "valuation_at": "2026-10-30T00:00:00Z", "source_digest": "sha256:" + "2" * 64,
        "complete": True, "position_risk": 0, "position_3x_exposure": 8000,
        "account_domain_key": account_domain_key(session.account_domain), "account_id": session.account_id,
        "valid_until": "2026-10-30T04:00:00Z", "pending_3x_exposure": 0,
        "open_orders_complete": True, "positions": {"TQQQ":10, "QQQM":20, "BIL":818.5}, "open_orders_empty": True}
    session.record_account_equity(snapshot=snapshot, cash_flow=0, baseline_confirmed=True)
    class FakeIB:
        def reqAllOpenOrders(self): return []
        def openTrades(self): return []
        def openOrders(self): return []
        def fills(self): return []
        def accountValues(self): return []
        def sleep(self, _seconds): pass
    sent = []
    platform = strategy_module_factory(STRATEGY_PROFILE="tqqq_growth_income")
    def fake_sdk(ib, order, **_kwargs):
        sent.append(order)
        if scenario == "timeout":
            raise TimeoutError("synthetic transport timeout")
        return ExecutionReport(symbol=order.symbol, side=order.side, quantity=order.quantity,
            broker_order_id=None if scenario == "unknown" else "synthetic-order",
            status={"partial": "PartiallyFilled", "unknown": "Unknown"}.get(scenario, "Submitted"),
            filled_quantity=0.5 if scenario == "partial" else 0,
            average_fill_price=100. if scenario == "partial" else None)
    monkeypatch.setattr(platform, "submit_order_intent", fake_sdk)
    with decision_cycle() as cycle:
        decision = evaluate_joint_tqqq(entrypoint, ctx, binding)
        targets, _, _, _, metadata = map_strategy_decision(decision, strategy_profile="tqqq_growth_income", runtime_metadata={
            "portfolio_total_equity": 100000., "market_values": {"TQQQ":8000., "QQQM":10000.}, "liquid_cash":150.,
            "account_ids": (session.account_id,), "trade_date":"2026-10-30", "signal_date":"2026-10-30", "effective_date":"2026-10-30"})
        account_session = session.for_intent(submission_intent_digest(metadata, "tqqq_growth_income"))
        order = OrderIntent(symbol="QQQM", side="buy", quantity=1, order_type="limit", limit_price=100., time_in_force="DAY", account_id=session.account_id)
        risk = {"floor":90000, "cost_buffer":0, "amount":10, "risk_fraction":.005, "loss_per_unit":1,
            "estimated_fee":0, "quantity_step":1, "product_leverage_factor":1, "unit_price":100,
            "existing_3x_exposure":8000, "pending_3x_exposure":0, "leverage_hitl_approved":False,
            "account_budget_policy":{"preference":"CAPITAL_PRESERVATION", "version":"one", "source":"synthetic",
                "per_trade_fraction":.005, "total_future_risk_limit":1000, "contract_floor":None, "floor":90000, "cost_buffer":0}}
        frozen = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        cycle["joint_strategy_binding"]["account_facts"] = JointAccountFacts(frozen, 0, {account_session.order_binding_digest(order):risk})
        if scenario == "changed_context":
            cycle["joint_strategy_binding"]["context"].market_data["benchmark_history"][-1]["close"] += 1
        if scenario == "changed_target":
            metadata["allocation"]["targets"]["QQQM"] += .01
        if scenario == "changed_risk":
            risk["amount"] = 11
        def execute():
            return execute_rebalance(FakeIB(), targets, {"TQQQ":{"quantity":10,"avg_cost":800.}, "QQQM":{"quantity":20,"avg_cost":500.}},
            {"equity":100000., "buying_power":150.}, fetch_quote_snapshots=lambda _ib, syms:{s:SimpleNamespace(last_price=101. if scenario == "changed_order" else 100.) for s in syms},
            submit_order_intent=platform.submit_market_order_intent, order_intent_cls=OrderIntent, account_execution_session=account_session,
            runtime_release_receipt=receipt, acquire_execution_claim=lambda:True, translator=build_translator("en"),
            strategy_profile="tqqq_growth_income", account_ids=(session.account_id,), signal_metadata=metadata,
            dry_run_only=False, cash_reserve_ratio=0., rebalance_threshold_ratio=0., limit_buy_premium=1., sell_settle_delay_sec=0.,
            execution_lock_dir=tmp_path, return_summary=True)
        if scenario in {"changed_context", "changed_target", "changed_order", "changed_risk"}:
            expected = {"changed_context":"joint strategy inputs changed", "changed_target":"order limit is invalid",
                        "changed_order":"exact-order risk facts unavailable", "changed_risk":"risk facts changed"}[scenario]
            with pytest.raises((RuntimeError, ValueError), match=expected):
                execute()
            assert not sent
            return
        if scenario == "timeout":
            with pytest.raises(TimeoutError, match="synthetic transport timeout"):
                execute()
            logs, summary = [], {}
        else:
            logs, summary = execute()
        if scenario == "duplicate":
            try:
                execute()
            except RuntimeError:
                pass
    assert len(sent) == 1, (logs, summary)
    assert sent[0] == order
    restarted = AccountSubmissionSession(ExecutionMarkerStore(local_dir=tmp_path), broker=session.broker,
        account_id=session.account_id, owner_id=session.owner_id, profile=session.profile, intent_id="f"*64,
        release_id=session.release_id, account_domain=session.account_domain, observed_account_id=session.observed_account_id,
        broker_identity=session.broker_identity, observed_at=session.observed_at, decision_at=session.decision_at,
        observed_environment=session.observed_environment, observed_server=session.observed_server,
        observed_currency=session.observed_currency, observed_lifecycle_id=session.observed_lifecycle_id, clock=session.clock)
    attempts = restarted.read_state()["attempts"]
    assert len(attempts) == 1
    attempt = next(iter(attempts.values()))
    assert attempt["certainty"] == ("UNKNOWN" if scenario in {"unknown", "timeout"} else "KNOWN_PENDING")
    if scenario == "partial":
        assert attempt["filled_quantity"] == 0.5
        assert summary["orders_partially_filled"][0]["filled_quantity"] == 0.5
        assert not summary["orders_filled"]
    if scenario == "unknown":
        assert summary["outcome_unknown"] is True
        assert summary["submission_halted"] is True
    with pytest.raises(AccountSubmissionError):
        restarted.reserve(order,
            strategy_state_sha256=transition.transition_sha256,
            strategy_intent_sha256=submission_binding_digest(transition.state["pending_intent"]),
            strategy_session=transition.effective_session)

