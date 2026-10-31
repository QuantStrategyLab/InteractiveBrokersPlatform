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
