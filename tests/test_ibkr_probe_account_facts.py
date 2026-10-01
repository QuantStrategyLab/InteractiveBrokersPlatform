from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


def _snapshot():
    return SimpleNamespace(
        as_of=datetime(2026, 9, 30, 12, tzinfo=timezone.utc),
        positions=(),
        buying_power=987.0,
        total_equity=654.0,
        metadata={
            "account_ids": ("DU123",),
            "total_equity_source": "broker_net_liquidation",
            "broker_net_liquidation": "100.25",
            "cash_balances": (
                {"account_id": "DU123", "currency": "USD", "NetLiquidation": "100.25", "CashBalance": "10"},
                {"account_id": "DU123", "currency": "EUR", "$LEDGER-TotalCashBalance": "-2.5"},
            ),
        },
    )


@pytest.mark.parametrize(
    ("selectors", "expected"),
    [
        (("DU123",), True),
        (("DU456",), False),
        (("default",), False),
        (("DU123", "DU456"), False),
    ],
)
def test_probe_projects_one_readonly_snapshot_only_for_unique_matching_selector(
    strategy_module_factory, monkeypatch, selectors, expected
):
    module = strategy_module_factory()
    monkeypatch.setattr(
        module,
        "RUNTIME_SETTINGS",
        replace(
            module.RUNTIME_SETTINGS,
            runtime_target=replace(module.RUNTIME_SETTINGS.runtime_target, account_selector=selectors),
        ),
    )

    observed = {"snapshot_reads": 0, "connect_kwargs": None, "disconnects": 0, "notifications": []}

    class FakeIB:
        def disconnect(self):
            observed["disconnects"] += 1

    def connect(**kwargs):
        observed["connect_kwargs"] = kwargs
        return FakeIB()

    def read_snapshot(_ib):
        observed["snapshot_reads"] += 1
        return _snapshot()

    report = {}
    monkeypatch.setattr(module, "build_request_log_context", lambda: SimpleNamespace(run_id="synthetic-probe"))
    monkeypatch.setattr(module, "build_execution_report", lambda *_args, **_kwargs: report)
    monkeypatch.setattr(module, "connect_ib", connect)
    monkeypatch.setattr(module, "build_portfolio_snapshot", read_snapshot)
    monkeypatch.setattr(module, "finalize_runtime_report", lambda target, **kwargs: target.update(kwargs))
    monkeypatch.setattr(module, "persist_execution_report", lambda *_args, **_kwargs: "synthetic-report")
    monkeypatch.setattr(module, "log_runtime_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        module,
        "run_strategy_core",
        lambda **_kwargs: pytest.fail("/probe must not run strategy code"),
    )
    monkeypatch.setattr(
        module,
        "build_broker_adapters",
        lambda **_kwargs: pytest.fail("/probe must not build order adapters"),
    )
    monkeypatch.setattr(
        module,
        "_publish_runtime_failure_notification",
        lambda **kwargs: observed["notifications"].append(kwargs),
    )

    with module.app.test_request_context("/probe", method="POST"):
        result = module.handle_probe()

    assert result == ("Probe OK", 200)
    assert observed["connect_kwargs"] == {"read_only": True, "validate_trading_permissions": False}
    assert observed["snapshot_reads"] == 1
    assert observed["disconnects"] == 1
    assert observed["notifications"] == []
    assert report["status"] == "ok"
    summary = report["summary"]
    assert summary["buying_power"] == 987.0
    assert summary["total_equity"] == 654.0
    assert summary["positions_count"] == 0
    if expected:
        assert summary["account_facts"]["net_assets"] == "100.25"
        assert summary["account_facts"]["cash"] == [
            {"currency": "USD", "cash_balance": "10", "source_tag": "CashBalance"},
            {"currency": "EUR", "cash_balance": "-2.5", "source_tag": "$LEDGER-TotalCashBalance"},
        ]
    else:
        assert "account_facts" not in summary


def test_probe_snapshot_failure_keeps_error_path_without_facts(strategy_module_factory, monkeypatch):
    module = strategy_module_factory()
    observed = {"snapshot_reads": 0, "disconnects": 0}

    class FakeIB:
        def disconnect(self):
            observed["disconnects"] += 1

    def read_snapshot(_ib):
        observed["snapshot_reads"] += 1
        raise RuntimeError("synthetic snapshot failure")

    report = {}
    monkeypatch.setattr(module, "build_request_log_context", lambda: SimpleNamespace(run_id="synthetic-probe-failure"))
    monkeypatch.setattr(module, "build_execution_report", lambda *_args, **_kwargs: report)
    monkeypatch.setattr(module, "connect_ib", lambda **_kwargs: FakeIB())
    monkeypatch.setattr(module, "build_portfolio_snapshot", read_snapshot)
    monkeypatch.setattr(module, "append_runtime_report_error", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "finalize_runtime_report", lambda target, **kwargs: target.update(kwargs))
    monkeypatch.setattr(module, "persist_execution_report", lambda *_args, **_kwargs: "synthetic-report")
    monkeypatch.setattr(module, "log_runtime_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "_publish_runtime_failure_notification", lambda **_kwargs: None)

    with module.app.test_request_context("/probe", method="POST"):
        result = module.handle_probe()

    assert result == ("Error", 500)
    assert observed == {"snapshot_reads": 1, "disconnects": 1}
    assert report["status"] == "error"
    assert "summary" not in report
