"""Synthetic reports only; maintenance cannot shadow the live report selector."""
from dataclasses import dataclass
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("maintenance", [False, True, None])
def test_maintenance_report_uses_child_namespace_and_leaves_adapter_unchanged(strategy_module, monkeypatch, maintenance):
    @dataclass(frozen=True)
    class Reporting:
        report_base_dir: str = "/tmp/synthetic-reports"
        report_gcs_prefix_uri: str = "gs://example-bucket/reports"

        def persist_execution_report(self, report):
            return self.report_base_dir, self.report_gcs_prefix_uri

    original = Reporting()
    monkeypatch.setattr(strategy_module, "build_composer", lambda **_: SimpleNamespace(build_reporting_adapters=lambda: original))
    report = {"diagnostics": None if maintenance is None else {"maintenance_cash_observation": maintenance}}
    paths = strategy_module.persist_execution_report(report, dry_run_only_override=True)
    suffix = "/maintenance-cash" if maintenance else ""
    assert paths == (original.report_base_dir + suffix, original.report_gcs_prefix_uri + suffix)
    assert original == Reporting()


@pytest.mark.parametrize("header", [None, "1"])
@pytest.mark.parametrize("failure", [False, True])
def test_probe_sets_namespace_before_connection_and_preserves_it_on_failure(strategy_module, monkeypatch, header, failure):
    saved = []
    monkeypatch.setattr(strategy_module, "build_request_log_context", lambda: SimpleNamespace(run_id="synthetic-run"))
    monkeypatch.setattr(strategy_module, "build_execution_report", lambda *_, **__: {"diagnostics": {}})
    monkeypatch.setattr(strategy_module, "log_runtime_event", lambda *_, **__: None)
    monkeypatch.setattr(strategy_module, "_publish_runtime_failure_notification", lambda **_: None)
    monkeypatch.setattr(strategy_module, "persist_execution_report", lambda report, **_: saved.append(report.copy()))
    monkeypatch.setattr(strategy_module, "build_portfolio_snapshot", lambda _: SimpleNamespace(positions=(), buying_power=0, total_equity=0))

    def connect(**kwargs):
        assert kwargs == {"read_only": True, "validate_trading_permissions": False}
        if failure:
            raise RuntimeError("synthetic failure")
        return SimpleNamespace(disconnect=lambda: None)

    monkeypatch.setattr(strategy_module, "connect_ib", connect)
    with strategy_module.app.test_request_context("/probe", method="POST", headers={"X-QSL-Maintenance-Cash": header} if header else {}):
        _, code = strategy_module.handle_probe()
    assert code == (500 if failure else 200)
    assert len(saved) == 1
    assert saved[0]["diagnostics"].get("maintenance_cash_observation", False) is (header == "1")
