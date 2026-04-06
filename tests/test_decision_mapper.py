from quant_platform_kit.strategy_contracts import PositionTarget, StrategyDecision

from decision_mapper import map_strategy_decision


def test_map_strategy_decision_maps_weight_positions_and_safe_haven():
    decision = StrategyDecision(
        positions=(
            PositionTarget(symbol="AAA", target_weight=0.6),
            PositionTarget(symbol="BOXX", target_weight=0.4, role="safe_haven"),
        ),
        diagnostics={
            "signal_description": "risk on",
            "status_description": "breadth=60.0%",
        },
    )

    target_weights, signal_desc, is_emergency, status_desc, metadata = map_strategy_decision(
        decision,
        strategy_profile="tech_pullback_cash_buffer",
        runtime_metadata={"status_icon": "🧲", "dry_run_only": True},
    )

    assert target_weights == {"AAA": 0.6, "BOXX": 0.4}
    assert signal_desc == "risk on"
    assert is_emergency is False
    assert status_desc == "breadth=60.0%"
    assert metadata["safe_haven_symbol"] == "BOXX"
    assert metadata["managed_symbols"] == ("AAA", "BOXX")
    assert metadata["status_icon"] == "🧲"


def test_map_strategy_decision_returns_noop_when_flagged_no_execute():
    decision = StrategyDecision(
        risk_flags=("no_execute",),
        diagnostics={
            "signal_description": "feature snapshot guard blocked execution",
            "status_description": "fail_closed | reason=feature_snapshot_path_missing",
        },
    )

    target_weights, signal_desc, is_emergency, status_desc, metadata = map_strategy_decision(
        decision,
        strategy_profile="tech_pullback_cash_buffer",
        runtime_metadata={
            "status_icon": "🛑",
            "snapshot_guard_decision": "fail_closed",
            "managed_symbols": (),
        },
    )

    assert target_weights is None
    assert signal_desc == "feature snapshot guard blocked execution"
    assert is_emergency is False
    assert status_desc == "fail_closed | reason=feature_snapshot_path_missing"
    assert metadata["actionable"] is False
    assert metadata["snapshot_guard_decision"] == "fail_closed"
