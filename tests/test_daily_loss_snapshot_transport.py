"""Native daily-loss transport regressions; fake snapshots and local journals."""
from dataclasses import replace

import pytest

from application import account_new_risk_gate_support as gate
from application.rebalance_service import _snapshot_to_portfolio_view
from tests.test_snapshot_risk_fact_transport import native_snapshot, healthy_projection
from tests.test_persistent_drawdown_breaker import session_at


@pytest.fixture(autouse=True)
def offline_context(monkeypatch):
    for name in ("IBKR_MAX_DAILY_LOSS_USD", "MAX_DAILY_LOSS_USD", "RUNTIME_TARGET_JSON"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PORTFOLIO_LOSS_STOPLOSS_ENABLED", "false")
    monkeypatch.setattr(gate, "resolve_production_drift_status_from_store", lambda **_kw: None)


@pytest.mark.parametrize("daily_loss", [100, 101, None, "invalid", float("nan")])
def test_native_daily_loss_breach_cannot_be_hidden_by_old_projection(tmp_path, daily_loss):
    snapshot = native_snapshot(40000)
    snapshot = replace(snapshot, metadata={**snapshot.metadata, "daily_loss_usd": daily_loss})
    _, values = _snapshot_to_portfolio_view(snapshot)
    metadata = healthy_projection()
    metadata["account_new_risk_snapshot"].update(max_daily_loss_usd=100, daily_loss_usd=0)
    # Control: the downstream adapter correctly blocks when it receives the fact.
    direct = gate.evaluate_persistent_account_new_risk_admission(
        {**values, "daily_loss_usd": daily_loss}, signal_metadata=metadata,
        session=session_at(tmp_path / "direct"),
    )
    assert gate.new_risk_buy_prohibited(direct)
    result = gate.evaluate_persistent_account_new_risk_admission(
        values, signal_metadata=metadata, session=session_at(tmp_path / "native"),
    )
    assert gate.new_risk_buy_prohibited(result), "native daily loss was discarded and old zero loss admitted"
