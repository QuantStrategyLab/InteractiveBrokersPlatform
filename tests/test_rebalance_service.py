import json

from application.rebalance_service import build_dashboard, run_strategy_core
from application.rebalance_service import _build_notification_trade_lines
from notifications.telegram import build_translator


def _weight_allocation(targets, *, risk_symbols=(), income_symbols=(), safe_haven_symbols=()):
    return {
        "target_mode": "weight",
        "strategy_symbols": tuple(targets.keys()),
        "risk_symbols": tuple(risk_symbols),
        "income_symbols": tuple(income_symbols),
        "safe_haven_symbols": tuple(safe_haven_symbols),
        "targets": dict(targets),
    }


def _build_test_translator():
    templates = {
        "heartbeat_title": "heartbeat",
        "rebalance_title": "rebalance",
        "no_trades": "no trades",
        "equity": "equity",
        "buying_power": "buying_power",
        "empty_positions": "(empty positions)",
        "empty_target_weights": "(empty target positions)",
        "target_weights_title": "Target Weights",
        "strategy_label": "strategy={name}",
        "strategy_profile_detail": "strategy_profile={profile}",
        "regime_detail": "regime={value}",
        "breadth_detail": "breadth={value}",
        "target_stock_detail": "target_stock={value}",
        "realized_stock_detail": "realized_stock={value}",
        "safe_haven_target_detail": "safe_haven_target={value}",
        "snapshot_decision_detail": "snapshot_decision={value}",
        "snapshot_as_of_detail": "snapshot_as_of={value}",
        "snapshot_age_days_detail": "snapshot_age_days={value}",
        "snapshot_file_ts_detail": "snapshot_file_ts={value}",
        "snapshot_path_detail": "snapshot_path={value}",
        "config_source_detail": "config_source={value}",
        "target_diff_summary": "target_changes {details}",
        "same_day_execution_locked_notice": "same_day_execution_locked_notice {mode} {trade_date} {snapshot_date}",
        "dry_run_snapshot_prices": "dry_run_snapshot_prices count={count} symbols={symbols}",
        "dry_run_buy_batch": "dry_run_buy_batch count={count} details={details}",
        "dry_run_sell_batch": "dry_run_sell_batch count={count} details={details}",
        "submitted_buy_batch": "submitted_buy_batch count={count} details={details}",
        "submitted_sell_batch": "submitted_sell_batch count={count} details={details}",
        "filled_buy_batch": "filled_buy_batch count={count} details={details}",
        "filled_sell_batch": "filled_sell_batch count={count} details={details}",
        "partial_buy_batch": "partial_buy_batch count={count} details={details}",
        "partial_sell_batch": "partial_sell_batch count={count} details={details}",
    }

    def translate(key, **kwargs):
        template = templates.get(key, key)
        return template.format(**kwargs) if kwargs else template

    return translate


def test_build_dashboard_localizes_strategy_details():
    dashboard = build_dashboard(
        positions={},
        account_values={"equity": 1000.0, "buying_power": 500.0},
        signal_desc="保持观望",
        status_desc="breadth=0.0%",
        strategy_profile="global_etf_rotation",
        target_weights={},
        signal_metadata={
            "regime": "risk_off",
            "breadth_ratio": 0.0,
            "target_stock_weight": 0.0,
            "realized_stock_weight": 0.0,
            "snapshot_as_of": None,
            "snapshot_guard_decision": "proceed",
            "snapshot_age_days": 8,
            "feature_snapshot_path": "gs://bucket/snapshot.csv",
            "strategy_config_source": "external_config",
            "allocation": _weight_allocation({}, safe_haven_symbols=("BOXX",)),
        },
        translator=build_translator("zh"),
        separator="---",
        strategy_display_name="全球 ETF 轮动",
    )

    assert "🧭 策略: 全球 ETF 轮动" in dashboard
    assert "目标持仓" in dashboard
    assert "市场阶段=risk_off" in dashboard
    assert "快照路径" not in dashboard
    assert "配置来源" not in dashboard
    assert "快照账龄" not in dashboard


def test_build_dashboard_localizes_snapshot_guard_text_for_zh():
    dashboard = build_dashboard(
        positions={},
        account_values={"equity": 1000.0, "buying_power": 500.0},
        signal_desc="feature snapshot guard blocked execution",
        status_desc="fail_closed | reason=feature_snapshot_path_missing",
        strategy_profile="tech_communication_pullback_enhancement",
        target_weights={},
        signal_metadata={
            "allocation": _weight_allocation({}, safe_haven_symbols=("BOXX",)),
        },
        translator=build_translator("zh"),
        separator="---",
        strategy_display_name="科技通信回调增强",
        status_icon="🛑",
    )

    assert "🛑 关闭执行 | 原因=缺少特征快照路径" in dashboard
    assert "🎯 特征快照校验阻止执行" in dashboard


def test_build_dashboard_localizes_qqq_tech_diagnostics_for_zh():
    dashboard = build_dashboard(
        positions={},
        account_values={"equity": 1000.0, "buying_power": 500.0},
        signal_desc=(
            "regime=soft_defense breadth=41.2% benchmark_trend=down "
            "target_stock=60.0% realized_stock=60.0% selected=8 top=CIEN(0.92)"
        ),
        status_desc="regime=soft_defense | breadth=41.2% | target_stock=60.0% | realized_stock=60.0%",
        strategy_profile="tech_communication_pullback_enhancement",
        target_weights={},
        signal_metadata={
            "allocation": _weight_allocation({}, safe_haven_symbols=("BOXX",)),
        },
        translator=build_translator("zh"),
        separator="---",
        strategy_display_name="科技通信回调增强",
    )

    assert "市场阶段=软防御 | 市场宽度=41.2% | 目标股票仓位=60.0% | 实际股票仓位=60.0%" in dashboard
    assert "基准趋势=向下" in dashboard
    assert "入选标的数=8 前排标的=CIEN(0.92)" in dashboard


def test_notification_trade_lines_localize_runtime_diagnostic_tail_for_zh():
    lines = _build_notification_trade_lines(
        [
            (
                "执行配置=soxl_soxx_trend_income | 市场阶段=<none> | 宽度=0.0% | "
                "目标股票仓位=0.0% | 实际股票仓位=0.0% | 快照日期=<none> | 交易日=<none>"
            )
        ],
        execution_summary={},
        translator=build_translator("zh"),
    )

    assert lines == [
        (
            "执行配置=SOXL/SOXX 半导体趋势收益 | 市场阶段=无 | 宽度=0.0% | "
            "目标股票仓位=0.0% | 实际股票仓位=0.0% | 快照日期=无 | 交易日=无"
        )
    ]


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
            {
                "managed_symbols": ("AAA", "BOXX"),
                "status_icon": "📏",
                "allocation": _weight_allocation(
                    {"AAA": 0.9, "BOXX": 0.1},
                    risk_symbols=("AAA",),
                    safe_haven_symbols=("BOXX",),
                ),
            },
        ),
        execute_rebalance=fake_execute_rebalance,
        send_tg_message=lambda message: observed["messages"].append(message),
        translator=_build_test_translator(),
        separator="---",
        strategy_display_name="Global ETF Rotation",
    )

    assert result == "OK - executed"
    assert observed["strategy_symbols"] == ("AAA", "BOXX")
    assert observed["signal_metadata"]["managed_symbols"] == ("AAA", "BOXX")
    assert observed["messages"]
    assert "📏 breadth=60.0%" in observed["messages"][0]
    assert "Target Weights" in observed["messages"][0]


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
                "strategy_profile": "tech_communication_pullback_enhancement",
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
                "allocation": _weight_allocation(
                    {"AAA": 0.6, "BOXX": 0.4},
                    risk_symbols=("AAA",),
                    safe_haven_symbols=("BOXX",),
                ),
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
                "price_source_mode": "mixed_market_quote_snapshot_close",
                "snapshot_price_fallback_used": True,
                "snapshot_price_fallback_count": 1,
                "snapshot_price_fallback_symbols": ["AAA"],
                "target_vs_current": [{"symbol": "AAA", "current_weight": 0.0, "target_weight": 0.6, "delta_weight": 0.6}],
            },
        ),
        send_tg_message=lambda message: observed["messages"].append(message),
        translator=_build_test_translator(),
        separator="---",
        strategy_display_name="Tech/Communication Pullback Enhancement",
        reconciliation_output_path=output_path,
    )

    assert result == "OK - executed"
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["strategy_profile"] == "tech_communication_pullback_enhancement"
    assert payload["snapshot_as_of"] == "2026-03-31"
    assert payload["orders_submitted"][0]["symbol"] == "AAA"
    assert payload["snapshot_price_fallback_used"] is True
    assert payload["snapshot_price_fallback_symbols"] == ["AAA"]
    assert "dry_run_buy_batch count=1 details=AAA 1" in observed["messages"][0]
    assert "target_changes AAA +60.0%" in observed["messages"][0]
    assert "DRY_RUN buy AAA 1 @100.00" not in observed["messages"][0]
    assert "目标差异" not in observed["messages"][0]


def test_run_strategy_core_writes_reconciliation_record_under_strategy_dir(tmp_path):
    class FakeIB:
        def isConnected(self):
            return True

        def disconnect(self):
            return None

    output_root = tmp_path / "tech_communication_pullback_enhancement" / "reconciliation"

    result = run_strategy_core(
        connect_ib=lambda: FakeIB(),
        get_current_portfolio=lambda _ib: ({}, {"equity": 1000.0, "buying_power": 500.0}),
        compute_signals=lambda _ib, _holdings: (
            None,
            "signal",
            False,
            "outside execution window",
            {
                "strategy_profile": "tech_communication_pullback_enhancement",
                "trade_date": "2026-04-01",
                "snapshot_as_of": "2026-03-31",
                "snapshot_guard_decision": "no_op",
                "no_op_reason": "outside_execution_window",
                "dry_run_only": True,
            },
        ),
        execute_rebalance=lambda *_args, **_kwargs: [],
        send_tg_message=lambda _message: None,
        translator=_build_test_translator(),
        separator="---",
        strategy_display_name="Tech/Communication Pullback Enhancement",
        reconciliation_output_path=output_root,
    )

    assert result == "OK - heartbeat"
    candidate_paths = [
        output_root,
        output_root / "2026-04-01" / "reconciliation.json",
    ]
    payload_path = next((path for path in candidate_paths if path.is_file()), None)
    assert payload_path is not None
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["no_op_reason"] == "outside_execution_window"
