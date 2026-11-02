"""Merged breaker/settlement boundaries with synthetic journals and broker IO."""
import multiprocessing
import os

import pytest

from application.account_new_risk_gate_support import build_snapshot_from_portfolio
from tests import test_settled_cash_containment as containment
from tests.test_settled_cash_containment import FakeIB

native = containment.native


@pytest.fixture(autouse=True)
def isolate_startup_environment():
    # The reused startup fixture installs inert values directly in os.environ.
    keys = ("ACCOUNT_GROUP", "IB_ACCOUNT_GROUP_CONFIG_JSON",
            "IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME", "RUNTIME_TARGET_JSON")
    previous = {key: os.environ.get(key) for key in keys}
    yield
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _open_in_another_process(session):
    session.record_circuit_breaker_open(snapshot=build_snapshot_from_portfolio({
        "total_equity": 1600., "peak_equity_usd": 2000., "drawdown_from_peak": .2,
    }))


@pytest.mark.parametrize("phase", ["qualification", "after_reservation"])
def test_other_process_open_blocks_deferred_cash_buy(native, monkeypatch, phase):
    from quant_platform_kit.common.execution_state import AccountSubmissionSession

    plan = native({"SOXL": 400.}, cash=500.)
    ib = FakeIB(cash=500., settled=500.)

    def open_breaker():
        writer = multiprocessing.get_context("fork").Process(
            target=_open_in_another_process, args=(plan.session(),),
        )
        writer.start()
        writer.join(10)
        assert writer.exitcode == 0

    if phase == "qualification":
        qualify = ib.qualifyContracts

        def qualify_then_open(contract):
            prepared = qualify(contract)
            open_breaker()
            return prepared

        ib.qualifyContracts = qualify_then_open
    else:
        reserve = AccountSubmissionSession.reserve

        def reserve_then_open(session, order, **kwargs):
            grant = reserve(session, order, **kwargs)
            open_breaker()
            return grant

        monkeypatch.setattr(AccountSubmissionSession, "reserve", reserve_then_open)

    with pytest.raises(RuntimeError, match="circuit breaker"):
        plan.run(ib, prices={"SOXL": 25.})
    assert ib.orders == []
    restarted = plan.session().read_state()
    assert restarted["circuit_breaker_open"] is True
    assert len(ib.accountValueEvent) == 0


def test_deferred_buy_marks_resized_exact_intent_as_new_risk(native):
    from tests.test_settled_cash_intervening_updates import emit_settlement

    plan = native({"SOXL": 400.}, cash=500.)
    ib = FakeIB(cash=500., settled=500.)
    qualify = ib.qualifyContracts

    def reduce_budget(contract):
        emit_settlement(ib, "100")
        return qualify(contract)

    ib.qualifyContracts = reduce_budget
    plan.run(ib, prices={"SOXL": 25.})
    attempt = next(iter(plan.session().read_state()["attempts"].values()))
    assert attempt["new_risk"] is True
    assert attempt["quantity"] == 3
    assert [order["quantity"] for order in ib.orders] == [3]
