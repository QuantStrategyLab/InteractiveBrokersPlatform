"""Synthetic mapper only; no execution adapter is imported or called."""
import pytest

import decision_mapper
from quant_platform_kit.common.strategy_contracts import PositionTarget, StrategyDecision
from tests.test_tqqq_budget_guard import _inputs


def _decision():
    return StrategyDecision(
        positions=(PositionTarget(symbol="SYNTH_EQUITY", target_weight=0.5),),
        risk_flags=("risk_gate:passed",), diagnostics={"risk_gate": "APPROVE"},
    )


def _metadata(inputs):
    return {"portfolio_total_equity": 100.0, "liquid_cash": 100.0,
            "tqqq_budget_policy": inputs["policy"], "tqqq_budget_snapshot": inputs["snapshot"],
            "tqqq_budget_runtime_binding": inputs["runtime_binding"],
            "tqqq_budget_decision_at": inputs["decision_at"]}


def test_enabled_over_budget_cannot_reach_allocation_translation(monkeypatch):
    inputs = _inputs()
    inputs["snapshot"]["reservations"][-1]["amount_usd"] += 1.0

    def forbidden(*args, **kwargs):
        pytest.fail("rejected budget reached allocation translation")

    monkeypatch.setattr(decision_mapper, "_normalize_to_weight_decision", forbidden)
    weights, _, _, _, metadata = decision_mapper.map_strategy_decision(
        _decision(), strategy_profile="tqqq_growth_income", runtime_metadata=_metadata(inputs),
    )
    assert weights is None
    assert metadata["actionable"] is False
    assert metadata["execution_blocked_reason"] == "rejected:budget_limit"
    assert "allocation" not in metadata
    assert "risk_authority" not in metadata
    assert not (set(metadata) & {"tqqq_budget_policy", "tqqq_budget_snapshot", "tqqq_budget_runtime_binding"})


def test_enabled_with_missing_snapshot_is_blocked():
    inputs = _inputs()
    inputs["snapshot"] = None
    weights, _, _, _, metadata = decision_mapper.map_strategy_decision(
        _decision(), strategy_profile="tqqq_growth_income", runtime_metadata=_metadata(inputs),
    )
    assert weights is None
    assert metadata["execution_blocked_reason"] == "rejected:budget_snapshot"


@pytest.mark.parametrize("enabled", [True, False])
def test_valid_or_disabled_guard_preserves_existing_approved_allocation(enabled):
    inputs = _inputs()
    inputs["policy"] = inputs["policy"] if enabled else {"enabled": False}
    if not enabled:
        inputs["snapshot"] = None
    weights, _, _, _, metadata = decision_mapper.map_strategy_decision(
        _decision(), strategy_profile="tqqq_growth_income", runtime_metadata=_metadata(inputs),
    )
    assert weights == {"SYNTH_EQUITY": 0.5}
    assert metadata["actionable"] is True
    assert metadata["tqqq_budget_status"] == ("checked" if enabled else "disabled")


def test_budget_check_never_substitutes_for_riskengine_approval():
    inputs = _inputs()
    weights, _, _, _, metadata = decision_mapper.map_strategy_decision(
        StrategyDecision(positions=_decision().positions), strategy_profile="tqqq_growth_income",
        runtime_metadata=_metadata(inputs),
    )
    assert weights is None
    assert metadata["execution_blocked_reason"] == "missing_risk_approval"
