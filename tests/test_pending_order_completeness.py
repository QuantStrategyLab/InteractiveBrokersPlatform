"""Full execution entrypoint with synthetic authority and in-memory order IO."""
from datetime import datetime
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest

from application import execution_service as service
from quant_platform_kit.common.models import OrderIntent
from tests.conftest import synthetic_account_submission


class FakeIB:
    """No SDK, connections, secrets or real broker implementation."""
    def __init__(self, mode, symbol):
        self.symbol = symbol
        if mode != "missing_api":
            self.openTrades = lambda: self._rows(mode)
        if mode not in {"missing_api", "missing_all_api"}:
            self.reqAllOpenOrders = lambda: self._rows(mode)

    def _rows(self, mode):
        if mode == "read_error":
            raise RuntimeError("synthetic read failure")
        if mode == "null_snapshot":
            return None
        if mode == "disjoint_cash_unknown":
            return [NS(contract=NS(symbol="MSFT", secType="STK"),
                       order=NS(account=None, action="BUY", totalQuantity=1),
                       orderStatus=NS(status="Submitted"))]
        if mode in {"missing_account", "missing_symbol", "pending"}:
            return [NS(contract=NS(symbol=None if mode == "missing_symbol" else self.symbol),
                       order=NS(account=None if mode == "missing_account" else "DU00000003"),
                       orderStatus=NS(status="Submitted"))]
        return []

    def fills(self):
        return []

    def accountValues(self):
        return [NS(account="DU00000003", tag="CashBalance", currency="USD", value="1000")]


@pytest.fixture
def execute(monkeypatch, tmp_path):
    # Keep the actual default-on admission, authority validator, claim, lock,
    # pending collector and submit wrapper. Only notification IO is replaced.
    monkeypatch.delenv("ACCOUNT_NEW_RISK_GATE", raising=False)
    monkeypatch.setattr(service, "maybe_publish_attention_for_admission", lambda *a, **k: {})
    submitted, claims = [], []

    def call(mode="empty", *, profile="soxl_soxx_trend_income", reducing=False,
             claim=True, unknown_pending=False, additional_buy=False):
        symbol = "TQQQ" if profile == "tqqq_growth_income" else "SOXL"
        today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        targets = {symbol: .1}
        if additional_buy:
            targets["VOO"] = .1
        metadata = {
            "trade_date": today, "effective_date": today,
            "risk_gate": "APPROVE", "risk_flags": ("risk_gate:passed",),
            "account_new_risk_snapshot": {"observation_status": "COMPLETE",
                "reconciliation_status": "VERIFIED", "circuit_breaker_state": "CLOSED",
                "production_drift_status": "healthy"},
            "allocation": {"target_mode": "weight", "strategy_symbols": tuple(targets),
                "risk_symbols": tuple(targets), "income_symbols": (), "safe_haven_symbols": (),
                "targets": targets},
            "risk_authority": {"strategy_profile": profile, "account_ids": ("DU00000003",),
                "trade_date": today, "signal_date": None, "effective_date": today,
                "snapshot_as_of": None, "portfolio_equity": 1000.,
                "targets": targets, "max_target_values": {s: 100. for s in targets}},
        }
        account = {"equity": 1000., "buying_power": 1000.}
        if unknown_pending:
            account["unknown_pending_orders"] = True
            metadata["account_new_risk_snapshot"]["reconciliation_status"] = "UNVERIFIED"

        def submit(ib, intent):
            submitted.append(intent)
            return NS(status="Filled", broker_order_id="synthetic-only", symbol=intent.symbol,
                      side=intent.side, quantity=intent.quantity,
                      filled_quantity=intent.quantity, average_fill_price=100.)

        def acquire():
            claims.append(True)
            return claim

        session, submit, receipt = synthetic_account_submission(
            submit, local_dir=tmp_path / "account-journal", metadata=metadata,
            profile=profile, account_id="DU00000003",
        )
        return service.execute_rebalance(
            FakeIB(mode, symbol), {}, {symbol: {"quantity": 2}} if reducing else {}, account,
            fetch_quote_snapshots=lambda ib, symbols: {s: NS(last_price=100.) for s in symbols},
            submit_order_intent=submit, order_intent_cls=OrderIntent,
            translator=lambda key, **kwargs: key, acquire_execution_claim=acquire,
            account_execution_session=session, runtime_release_receipt=receipt,
            strategy_profile=profile, account_ids=("DU00000003",), signal_metadata=metadata,
            dry_run_only=False, execution_mode="live", cash_reserve_ratio=0.,
            rebalance_threshold_ratio=0., limit_buy_premium=1., sell_settle_delay_sec=0,
            execution_lock_dir=tmp_path, return_summary=True,
        )

    call.submitted, call.claims = submitted, claims
    return call


PROFILES = ["soxl_soxx_trend_income", "tqqq_growth_income"]


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("mode", ["missing_api", "missing_all_api", "null_snapshot", "missing_account", "missing_symbol"])
def test_incomplete_pending_read_cannot_reach_submission(execute, profile, mode):
    _, summary = execute(mode, profile=profile)
    assert execute.submitted == []
    assert execute.claims == []
    assert summary["execution_status"] == "blocked"
    assert summary["no_op_reason"] == "pending_orders_unavailable"
    assert summary["lock_path"] is None


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("reducing", [False, True])
def test_pending_read_error_never_reaches_submission(execute, profile, reducing):
    try:
        _, summary = execute("read_error", profile=profile, reducing=reducing)
    except RuntimeError:
        pass  # Baseline already fails closed by propagating a broker read error.
    else:
        assert summary["execution_status"] == "blocked"
        assert summary["no_op_reason"] == "pending_orders_unavailable"
    assert execute.submitted == [] and execute.claims == []


@pytest.mark.parametrize("profile", PROFILES)
def test_known_pending_still_blocks_before_lock_and_claim(execute, profile):
    _, summary = execute("pending", profile=profile)
    assert summary["no_op_reason"].startswith("pending_orders_detected:")
    assert summary["lock_path"] is None
    assert execute.submitted == [] and execute.claims == []


@pytest.mark.parametrize("profile", PROFILES)
def test_complete_empty_snapshot_still_permits_synthetic_buy(execute, profile):
    _, summary = execute(profile=profile)
    assert [i.side for i in execute.submitted] == ["buy"]
    assert len(execute.claims) == 1 and summary["lock_path"]


@pytest.mark.parametrize("profile", PROFILES)
def test_complete_empty_snapshot_preserves_reductions(execute, profile):
    _, summary = execute(profile=profile, reducing=True)
    assert [(i.side, i.quantity) for i in execute.submitted] == [("sell", 1)]
    assert len(execute.claims) == 1 and summary["lock_path"]


def test_claim_failure_still_prevents_delegate(execute):
    with pytest.raises(RuntimeError, match="execution claim required"):
        execute(claim=False)
    assert execute.submitted == [] and len(execute.claims) == 1


def test_successful_cycle_still_holds_same_day_lock(execute):
    execute()
    _, summary = execute()
    assert len(execute.submitted) == 1 and len(execute.claims) == 1
    assert summary["no_op_reason"].startswith("same_day_execution_locked:")


@pytest.mark.parametrize("profile", PROFILES)
def test_explicit_unknown_pending_is_already_blocked_upstream(execute, profile):
    _, summary = execute(profile=profile, unknown_pending=True)
    assert summary["account_new_risk_buy_blocked"] is True
    assert execute.submitted == [] and execute.claims == []


@pytest.mark.parametrize("profile", PROFILES)
def test_disjoint_cash_uncertainty_preserves_qualified_reduction(execute, profile):
    _, summary = execute("disjoint_cash_unknown", profile=profile, reducing=True)
    assert [(i.side, i.quantity) for i in execute.submitted] == [("sell", 1)]
    assert len(execute.claims) == 1 and summary["lock_path"]
    assert summary["pending_order_cash_risk_unknown"] is True


@pytest.mark.parametrize("profile", PROFILES)
def test_disjoint_cash_uncertainty_still_blocks_new_buy(execute, profile):
    _, summary = execute("disjoint_cash_unknown", profile=profile)
    assert execute.submitted == [] and execute.claims == []
    assert summary["account_new_risk_buy_blocked"] is True
    assert "PENDING_ORDER_CASH_RISK_UNKNOWN" in summary["account_new_risk_reason_codes"]


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("mode", ["missing_api", "missing_all_api", "null_snapshot", "missing_account", "missing_symbol"])
def test_unqualified_reduction_stays_blocked(execute, profile, mode):
    _, summary = execute(mode, profile=profile, reducing=True)
    assert execute.submitted == [] and execute.claims == []
    assert summary["no_op_reason"] == "pending_orders_unavailable"
    assert summary["lock_path"] is None


def test_disjoint_row_does_not_hide_later_related_pending(execute, monkeypatch):
    rows = FakeIB("disjoint_cash_unknown", "SOXL")._rows("disjoint_cash_unknown")
    rows += FakeIB("pending", "SOXL")._rows("pending")
    monkeypatch.setattr(FakeIB, "_rows", lambda self, mode: rows)
    _, summary = execute("mixed", reducing=True)
    assert execute.submitted == [] and execute.claims == []
    assert summary["no_op_reason"] == "pending_orders_detected:SOXL"


def _external_order(*, status="Submitted", account="DU00000003", action="BUY"):
    return NS(
        contract=NS(symbol="MSFT", secType="STK", currency="USD"),
        order=NS(account=account, action=action, totalQuantity=20, lmtPrice=50),
        orderStatus=NS(status=status, filled=5, remaining=15),
    )


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("status", ["Submitted", "PendingCancel", "PartiallyFilled", "Unknown"])
def test_same_account_external_pending_cannot_spend_shared_cash(execute, monkeypatch, profile, status):
    monkeypatch.setattr(FakeIB, "_rows", lambda self, mode: [_external_order(status=status)])
    _, summary = execute("external", profile=profile)
    assert execute.submitted == [] and execute.claims == []
    assert summary["pending_order_cash_risk_unknown"] is True
    assert summary["account_new_risk_buy_blocked"] is True
    assert "PENDING_ORDER_CASH_RISK_UNKNOWN" in summary["account_new_risk_reason_codes"]
    assert summary["lock_path"] is None


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("status", ["Submitted", "PendingCancel", "PartiallyFilled", "Unknown"])
def test_same_account_external_pending_preserves_qualified_reduction(execute, monkeypatch, profile, status):
    monkeypatch.setattr(FakeIB, "_rows", lambda self, mode: [_external_order(status=status)])
    _, summary = execute("external", profile=profile, reducing=True)
    assert [(i.side, i.quantity) for i in execute.submitted] == [("sell", 1)]
    assert len(execute.claims) == 1 and summary["lock_path"]
    assert summary["pending_order_cash_risk_unknown"] is True


@pytest.mark.parametrize("profile", PROFILES)
def test_same_account_external_sell_is_not_spendable_proceeds(execute, monkeypatch, profile):
    monkeypatch.setattr(FakeIB, "_rows", lambda self, mode: [_external_order(action="SELL")])
    _, summary = execute("external", profile=profile)
    assert execute.submitted == [] and execute.claims == []
    assert summary["account_new_risk_buy_blocked"] is True


@pytest.mark.parametrize("profile", PROFILES)
def test_external_pending_preserves_sell_without_reinvesting_proceeds(execute, monkeypatch, profile):
    monkeypatch.setattr(FakeIB, "_rows", lambda self, mode: [_external_order()])
    _, summary = execute("external", profile=profile, reducing=True, additional_buy=True)
    assert [(i.side, i.quantity) for i in execute.submitted] == [("sell", 1)]
    assert len(execute.claims) == 1
    assert summary["account_new_risk_buy_blocked"] is True
    assert {"symbol": "VOO", "side": "buy", "reason": "account_new_risk_gate"} in summary["orders_skipped"]


@pytest.mark.parametrize("profile", PROFILES)
def test_other_account_external_pending_does_not_reserve_this_accounts_cash(execute, monkeypatch, profile):
    monkeypatch.setattr(FakeIB, "_rows", lambda self, mode: [_external_order(account="DU00000004")])
    _, summary = execute("external", profile=profile)
    assert [(i.side, i.quantity) for i in execute.submitted] == [("buy", 1)]
    assert "pending_order_cash_risk_unknown" not in summary


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("status", ["Cancelled", "ApiCancelled", "Inactive", "Filled"])
def test_terminal_external_order_does_not_withhold_cash_or_add_proceeds(execute, monkeypatch, profile, status):
    monkeypatch.setattr(FakeIB, "_rows", lambda self, mode: [_external_order(status=status)])
    _, summary = execute("external", profile=profile)
    assert [(i.side, i.quantity) for i in execute.submitted] == [("buy", 1)]
    assert "pending_order_cash_risk_unknown" not in summary


def test_external_cash_block_does_not_take_execution_lock_before_fresh_retry(execute, monkeypatch):
    rows = [_external_order()]
    monkeypatch.setattr(FakeIB, "_rows", lambda self, mode: rows)
    _, blocked = execute("external")
    _, repeated = execute("external")
    assert execute.submitted == [] and execute.claims == []
    assert blocked["lock_path"] is None and repeated["lock_path"] is None
    rows.clear()  # New complete observation, not a guessed cancellation or cash release.
    _, complete = execute()
    assert [(i.side, i.quantity) for i in execute.submitted] == [("buy", 1)]
    assert complete["lock_path"] and len(execute.claims) == 1


def test_same_account_external_row_does_not_hide_related_pending(execute, monkeypatch):
    related = FakeIB("pending", "SOXL")._rows("pending")
    monkeypatch.setattr(FakeIB, "_rows", lambda self, mode: [_external_order(), *related])
    _, summary = execute("external", reducing=True)
    assert execute.submitted == [] and execute.claims == []
    assert summary["no_op_reason"] == "pending_orders_detected:SOXL"


@pytest.mark.parametrize("profile", PROFILES)
def test_all_client_snapshot_finds_order_absent_from_local_cache(execute, monkeypatch, profile):
    calls = []

    def all_orders(self):
        calls.append(True)
        return [_external_order()]

    monkeypatch.setattr(FakeIB, "__init__", lambda self, mode, symbol: setattr(self, "symbol", symbol))
    monkeypatch.setattr(FakeIB, "openTrades", lambda self: [], raising=False)
    monkeypatch.setattr(FakeIB, "reqAllOpenOrders", all_orders, raising=False)
    _, summary = execute(profile=profile)
    assert calls == [True]
    assert execute.submitted == [] and execute.claims == []
    assert summary["pending_order_cash_risk_unknown"] is True
    assert summary["lock_path"] is None


@pytest.mark.parametrize("reducing", [False, True])
@pytest.mark.parametrize("mode", ["null", "error", "timeout"])
def test_all_client_failure_cannot_fall_back_to_empty_cache(execute, monkeypatch, reducing, mode):
    def all_orders(self):
        if mode == "null":
            return None
        if mode == "timeout":
            raise TimeoutError("synthetic private diagnostic")
        raise RuntimeError("synthetic private diagnostic")

    monkeypatch.setattr(FakeIB, "__init__", lambda self, mode, symbol: setattr(self, "symbol", symbol))
    monkeypatch.setattr(FakeIB, "openTrades", lambda self: [], raising=False)
    monkeypatch.setattr(FakeIB, "reqAllOpenOrders", all_orders, raising=False)
    logs, summary = execute(reducing=reducing)
    assert execute.submitted == [] and execute.claims == []
    assert summary["no_op_reason"] == "pending_orders_unavailable"
    assert summary["lock_path"] is None
    assert "synthetic private diagnostic" not in str((logs, summary))


def test_all_client_snapshot_retains_unconfirmed_cached_order(execute, monkeypatch):
    monkeypatch.setattr(FakeIB, "__init__", lambda self, mode, symbol: setattr(self, "symbol", symbol))
    monkeypatch.setattr(FakeIB, "openTrades", lambda self: [_external_order()], raising=False)
    monkeypatch.setattr(FakeIB, "reqAllOpenOrders", lambda self: [], raising=False)
    _, summary = execute()
    assert execute.submitted == [] and execute.claims == []
    assert summary["pending_order_cash_risk_unknown"] is True
