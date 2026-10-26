"""The one-shot entry uses existing read helpers, never the strategy cycle."""
from types import SimpleNamespace

import pytest

from scripts.maintenance_cash_job_entry import observe


ENV = dict(CLOUD_RUN_JOB="synthetic-job", CLOUD_RUN_EXECUTION="synthetic-job-abc",
           CLOUD_RUN_TASK_INDEX="0", CLOUD_RUN_TASK_ATTEMPT="0", CLOUD_RUN_TASK_COUNT="1")


@pytest.mark.parametrize("failure", [None, "connect", "snapshot", "identity"])
def test_read_save_and_disconnect(failure):
    calls, report = [], {"diagnostics": {}}
    facts = {"account_ids": ["DU00000001"]}

    def connect(**kwargs):
        calls.append(("connect", kwargs))
        if failure == "connect":
            raise RuntimeError("PRIVATE_ERROR_CANARY")
        return SimpleNamespace(disconnect=lambda: calls.append("disconnect"))

    def snapshot(ib):
        if failure == "snapshot":
            raise RuntimeError("PRIVATE_ERROR_CANARY")
        return object()

    def finalize(value, **kwargs):
        if "diagnostics" in kwargs:
            value["diagnostics"].update(kwargs.pop("diagnostics"))
        value.update(kwargs)

    runtime = SimpleNamespace(
        build_composer=lambda **_: SimpleNamespace(build_reporting_adapters=lambda: SimpleNamespace(
            build_log_context=lambda: object())),
        build_execution_report=lambda *_, **__: report, connect_ib=connect,
        build_portfolio_snapshot=snapshot, _account_facts_match_runtime_target=lambda _: failure != "identity",
        finalize_runtime_report=finalize,
        persist_execution_report=lambda value, **_: calls.append(("save", value.copy())) or "gs://synthetic/private.json")
    result = observe(runtime, lambda _: facts, ENV)
    assert result == (0 if failure is None else 1)
    assert calls[0] == ("connect", {"read_only": True, "validate_trading_permissions": False})
    assert ("disconnect" in calls) == (failure != "connect")
    assert calls[-1][0] == "save"
    assert report["diagnostics"]["maintenance_cash_observation"] is True
    assert report["diagnostics"]["execution_kind"] == "cloud_run_job"
    assert report["diagnostics"]["cloud_run_execution"] == ENV["CLOUD_RUN_EXECUTION"]
    assert "PRIVATE_ERROR_CANARY" not in str(report)
    assert report["status"] == ("ok" if failure is None else "error")


@pytest.mark.parametrize("key,value", [("CLOUD_RUN_TASK_ATTEMPT", "1"), ("CLOUD_RUN_TASK_COUNT", "2"),
    ("CLOUD_RUN_TASK_INDEX", "1"), ("CLOUD_RUN_EXECUTION", ""), ("K_REVISION", "forged-service")])
def test_invalid_execution_stops_before_runtime(key, value):
    with pytest.raises(ValueError, match="job_identity_invalid"):
        observe(None, None, {**ENV, key: value})
