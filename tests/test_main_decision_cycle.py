def test_main_cycle_uses_one_clock_for_plugin_load_core_and_report(strategy_module_factory, monkeypatch):
    module = strategy_module_factory(STRATEGY_PROFILE="tqqq_growth_income")
    observed = {}
    monkeypatch.setattr(module, "is_market_open_now", lambda **_kwargs: True)
    monkeypatch.setattr(module, "load_strategy_plugin_signals", lambda: (observed.setdefault("loader_clock", module.current_cycle()["decision_time"]) and (), None))
    monkeypatch.setattr(module, "attach_strategy_plugin_report", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "run_strategy_core", lambda **kwargs: observed.setdefault("core_clock", module.current_cycle()["decision_time"]) and "OK")
    monkeypatch.setattr(module, "attach_cycle_execution_receipt", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "persist_execution_report", lambda report, **kwargs: observed.setdefault("report", dict(report)) or "/tmp/report")
    with module.app.test_request_context("/run", method="POST"):
        _, status = module.handle_request()
    assert status == 200
    assert observed["loader_clock"] == observed["core_clock"]
    assert observed["report"]["decision_at"] == observed["loader_clock"].isoformat()
    assert module.current_cycle() is None


def test_main_captures_observed_order_facts_for_report_link(strategy_module_factory, monkeypatch):
    module = strategy_module_factory(STRATEGY_PROFILE="tqqq_growth_income")
    from application.cycle_result import StrategyCycleResult
    observed = {}
    order = {"symbol": "TQQQ", "side": "buy", "status": "Submitted", "broker_order_id": "synthetic-order"}
    summary = {key: [] for key in ("orders_filled", "orders_pending", "orders_partially_filled", "option_orders_filled", "option_orders_pending", "option_orders_partially_filled", "orders_skipped", "option_orders_skipped")}
    summary["orders_pending"] = [order]
    monkeypatch.setattr(module, "is_market_open_now", lambda **kwargs: True)
    monkeypatch.setattr(module, "load_strategy_plugin_signals", lambda: ((), None))
    monkeypatch.setattr(module, "attach_strategy_plugin_report", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "run_strategy_core", lambda **kwargs: StrategyCycleResult(result="OK", execution_summary=summary))
    monkeypatch.setattr(module, "attach_cycle_execution_receipt", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "build_execution_report", lambda *args, **kwargs: {"dry_run": False})
    def persist(report, **kwargs):
        observed["dry_run"] = report.get("dry_run")
        observed["orders"] = module.current_cycle().get("execution_order_records")
        observed["fills"] = module.current_cycle().get("execution_fill_records")
        return "/tmp/synthetic-report"
    monkeypatch.setattr(module, "persist_execution_report", persist)
    with module.app.test_request_context("/run", method="POST"):
        _, status = module.handle_request()
    assert status == 200
    assert observed["orders"] == [order] and observed["fills"] is None, observed


def test_main_does_not_claim_complete_orders_when_account_brake_also_submits(strategy_module_factory, monkeypatch):
    module = strategy_module_factory(STRATEGY_PROFILE="tqqq_growth_income")
    from application.cycle_result import StrategyCycleResult
    observed = {}
    order = {"symbol": "TQQQ", "side": "buy", "status": "Submitted", "broker_order_id": "synthetic-order"}
    summary = {key: [] for key in ("orders_filled", "orders_pending", "orders_partially_filled", "option_orders_filled", "option_orders_pending", "option_orders_partially_filled", "orders_skipped", "option_orders_skipped")}
    summary["orders_pending"] = [order]
    metadata = {"account_brake_observation": {"submission_status": "Submitted"}}
    monkeypatch.setattr(module, "is_market_open_now", lambda **kwargs: True)
    monkeypatch.setattr(module, "load_strategy_plugin_signals", lambda: ((), None))
    monkeypatch.setattr(module, "attach_strategy_plugin_report", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "run_strategy_core", lambda **kwargs: StrategyCycleResult(result="OK", execution_summary=summary, signal_metadata=metadata))
    monkeypatch.setattr(module, "attach_cycle_execution_receipt", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "build_execution_report", lambda *args, **kwargs: {"dry_run": False})
    def persist(report, **kwargs):
        observed["orders"] = module.current_cycle().get("execution_order_records")
        return "/tmp/synthetic-report"
    monkeypatch.setattr(module, "persist_execution_report", persist)
    with module.app.test_request_context("/run", method="POST"):
        _, status = module.handle_request()
    assert status == 200 and observed["orders"] is None
