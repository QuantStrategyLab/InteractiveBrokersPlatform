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
            "execution_annotations": {"dashboard_text": "strategy dashboard"},
        },
    )

    target_weights, signal_desc, is_emergency, status_desc, metadata = map_strategy_decision(
        decision,
        strategy_profile="tech_communication_pullback_enhancement",
        runtime_metadata={"status_icon": "🧲", "dry_run_only": True},
    )

    assert target_weights == {"AAA": 0.6, "BOXX": 0.4}
    assert signal_desc == "risk on"
    assert is_emergency is False
    assert status_desc == "breadth=60.0%"
    assert metadata["safe_haven_symbol"] == "BOXX"
    assert metadata["managed_symbols"] == ("AAA", "BOXX")
    assert metadata["status_icon"] == "🧲"
    assert metadata["allocation"]["target_mode"] == "weight"
    assert metadata["allocation"]["strategy_symbols"] == ("AAA", "BOXX")
    assert metadata["allocation"]["targets"] == {"AAA": 0.6, "BOXX": 0.4}
    assert metadata["allocation"]["positions"][1]["role"] == "safe_haven"
    assert metadata["execution_annotations"]["dashboard_text"] == "strategy dashboard"
    assert metadata["dashboard_text"] == "strategy dashboard"
    assert "target_mode" not in metadata


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
        strategy_profile="tech_communication_pullback_enhancement",
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
    assert "allocation" not in metadata


def test_map_strategy_decision_translates_value_targets_for_semiconductor_strategy():
    decision = StrategyDecision(
        positions=(
            PositionTarget(symbol="SOXL", target_value=30000.0),
            PositionTarget(symbol="SOXX", target_value=0.0),
            PositionTarget(symbol="QQQI", target_value=3500.0, role="income"),
            PositionTarget(symbol="SPYI", target_value=1500.0, role="income"),
            PositionTarget(symbol="BOXX", target_value=15000.0, role="safe_haven"),
        ),
        diagnostics={
            "signal_description": "risk on",
            "status_description": "soxl>ma150",
        },
    )

    target_weights, signal_desc, is_emergency, status_desc, metadata = map_strategy_decision(
        decision,
        strategy_profile="soxl_soxx_trend_income",
        runtime_metadata={
            "portfolio_total_equity": 50000.0,
            "status_icon": "🚀",
            "managed_symbols": ("SOXL", "SOXX", "QQQI", "SPYI", "BOXX"),
        },
    )

    assert target_weights == {
        "SOXL": 0.6,
        "SOXX": 0.0,
        "QQQI": 0.07,
        "SPYI": 0.03,
        "BOXX": 0.3,
    }
    assert signal_desc == "risk on"
    assert is_emergency is False
    assert status_desc == "soxl>ma150"
    assert metadata["safe_haven_symbol"] == "BOXX"
    assert metadata["allocation"]["target_mode"] == "weight"
    assert metadata["allocation"]["strategy_symbols"] == ("SOXL", "SOXX", "QQQI", "SPYI", "BOXX")
    assert metadata["allocation"]["targets"]["SOXL"] == 0.6
