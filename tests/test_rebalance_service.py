import json

from application.rebalance_service import run_strategy_core


def test_run_strategy_core_passes_signal_metadata_to_execution():
    observed = {"messages": [], "strategy_symbols": None}

    class FakeIB:
        def isConnected(self):
            return True

        def disconnect(self):
            observed["disconnected"] = True

    def fake_execute_rebalance(
        _ib,
        _weights,
        _positions,
        _account_values,
        *,
        strategy_symbols=None,
        signal_metadata=None,
    ):
        observed["strategy_symbols"] = strategy_symbols
        observed["signal_metadata"] = signal_metadata
        return []

    result = run_strategy_core(
        connect_ib=lambda: FakeIB(),
        get_current_portfolio=lambda _ib: ({}, {"equity": 1000.0, "buying_power": 500.0}),
        compute_signals=lambda _ib, _holdings: (
            {"AAA": 0.9, "BOXX": 0.1},
            "signal",
            False,
            "breadth=60.0%",
            {"managed_symbols": ("AAA", "BOXX"), "status_icon": "📏"},
        ),
        execute_rebalance=fake_execute_rebalance,
        send_tg_message=lambda message: observed["messages"].append(message),
        translator=lambda key, **_kwargs: {
            "heartbeat_title": "heartbeat",
            "rebalance_title": "rebalance",
            "no_trades": "no trades",
            "equity": "equity",
            "buying_power": "buying_power",
        }.get(key, key),
        separator="---",
    )

    assert result == "OK - executed"
    assert observed["strategy_symbols"] == ("AAA", "BOXX")
    assert observed["signal_metadata"]["managed_symbols"] == ("AAA", "BOXX")
    assert observed["messages"]
    assert "📏 breadth=60.0%" in observed["messages"][0]


def test_run_strategy_core_writes_reconciliation_record(tmp_path):
    observed = {"messages": []}

    class FakeIB:
        def isConnected(self):
            return True

        def disconnect(self):
            return None

    output_path = tmp_path / "reconciliation.json"

    result = run_strategy_core(
        connect_ib=lambda: FakeIB(),
        get_current_portfolio=lambda _ib: ({}, {"equity": 1000.0, "buying_power": 500.0}),
        compute_signals=lambda _ib, _holdings: (
            {"AAA": 0.6, "BOXX": 0.4},
            "signal",
            False,
            "breadth=41.0%",
            {
                "strategy_profile": "tech_pullback_cash_buffer",
                "managed_symbols": ("AAA", "BOXX"),
                "status_icon": "🧲",
                "trade_date": "2026-04-01",
                "snapshot_as_of": "2026-03-31",
                "snapshot_guard_decision": "proceed",
                "regime": "soft_defense",
                "breadth_ratio": 0.41,
                "target_stock_weight": 0.6,
                "realized_stock_weight": 0.6,
                "safe_haven_weight": 0.4,
                "safe_haven_symbol": "BOXX",
                "dry_run_only": True,
            },
        ),
        execute_rebalance=lambda *_args, **_kwargs: (
            ["DRY_RUN buy AAA 1 @100.00"],
            {
                "mode": "dry_run",
                "execution_status": "executed",
                "orders_submitted": [{"symbol": "AAA", "side": "buy", "quantity": 1, "status": "dry_run"}],
                "orders_filled": [],
                "orders_partially_filled": [],
                "orders_skipped": [],
                "skipped_reasons": [],
                "residual_cash_estimate": 400.0,
                "realized_safe_haven_weight": 0.4,
                "target_vs_current": [{"symbol": "AAA", "current_weight": 0.0, "target_weight": 0.6, "delta_weight": 0.6}],
            },
        ),
        send_tg_message=lambda message: observed["messages"].append(message),
        translator=lambda key, **_kwargs: {
            "heartbeat_title": "heartbeat",
            "rebalance_title": "rebalance",
            "no_trades": "no trades",
            "equity": "equity",
            "buying_power": "buying_power",
        }.get(key, key),
        separator="---",
        reconciliation_output_path=output_path,
    )

    assert result == "OK - executed"
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["strategy_profile"] == "tech_pullback_cash_buffer"
    assert payload["snapshot_as_of"] == "2026-03-31"
    assert payload["orders_submitted"][0]["symbol"] == "AAA"
