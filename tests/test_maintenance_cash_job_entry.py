"""The one-shot entry uses existing read helpers, never the strategy cycle."""
from types import SimpleNamespace
import asyncio
from datetime import datetime

import pytest

from scripts.maintenance_cash_job_entry import observe
from scripts import maintenance_cash_job_entry as entry


ENV = dict(CLOUD_RUN_JOB="synthetic-job", CLOUD_RUN_EXECUTION="synthetic-job-abc",
           CLOUD_RUN_TASK_INDEX="0", CLOUD_RUN_TASK_ATTEMPT="0", CLOUD_RUN_TASK_COUNT="1")


@pytest.mark.parametrize("failure", [None, "connect", "snapshot", "identity", "summary"])
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
    def summary_reader(ib, account_ids):
        calls.append(("summary", account_ids))
        if failure == "summary":
            raise RuntimeError("PRIVATE_ERROR_CANARY")
        return {"source_method": "ibkr.reqAccountSummary"}
    result = observe(runtime, lambda _: facts, ENV, summary_reader=summary_reader)
    assert result == (0 if failure is None else 1)
    assert calls[0] == ("connect", {"read_only": True, "validate_trading_permissions": False})
    assert ("disconnect" in calls) == (failure != "connect")
    assert calls[-1][0] == "save"
    assert report["diagnostics"]["maintenance_cash_observation"] is True
    assert report["diagnostics"]["execution_kind"] == "cloud_run_job"
    assert report["diagnostics"]["cloud_run_execution"] == ENV["CLOUD_RUN_EXECUTION"]
    assert "PRIVATE_ERROR_CANARY" not in str(report)
    assert report["status"] == ("ok" if failure is None else "error")
    assert ("summary", ["DU00000001"]) in calls if failure in {None, "summary"} else not any(
        isinstance(call, tuple) and call[0] == "summary" for call in calls)
    if failure is None:
        assert report["summary"]["account_facts"] == facts
        assert report["summary"]["broker_account_summary"]["source_method"] == "ibkr.reqAccountSummary"


@pytest.mark.parametrize("key,value", [("CLOUD_RUN_TASK_ATTEMPT", "1"), ("CLOUD_RUN_TASK_COUNT", "2"),
    ("CLOUD_RUN_TASK_INDEX", "1"), ("CLOUD_RUN_EXECUTION", ""), ("K_REVISION", "forged-service")])
def test_invalid_execution_stops_before_runtime(key, value):
    with pytest.raises(ValueError, match="job_identity_invalid"):
        observe(None, None, {**ENV, key: value})


class SummaryBroker:
    """Only the pinned SDK's event/request surface, with no network."""
    def __init__(self, rows, failure=None):
        from eventkit import Event
        self.accountSummaryEvent = Event()
        self.wrapper = SimpleNamespace(acctSummary={}, startReq=self.start)
        self.client = SimpleNamespace(getReqId=lambda: 17, reqAccountSummary=self.request,
                                      cancelAccountSummary=lambda req: self.calls.append(("cancel", req)))
        self.rows, self.failure, self.calls = rows, failure, []
        self.loop = asyncio.new_event_loop()

    def managedAccounts(self):
        return ["DU00000001", "DU00000002"]

    def start(self, req):
        self.future = self.loop.create_future()
        return self.future

    def request(self, req, group, tags):
        self.calls.append(("request", req, group, tags))
        if self.failure == "request":
            raise RuntimeError("PRIVATE_ERROR_CANARY")
        for row in self.rows:
            self.accountSummaryEvent.emit(SimpleNamespace(**row))
        if self.failure != "timeout":
            self.future.set_result(None)  # The SDK completes on accountSummaryEnd.

    def _run(self, awaitable):
        return self.loop.run_until_complete(awaitable)


def test_explicit_summary_retains_source_decimal_and_target_only():
    rows = [dict(account="DU00000001", tag="SettledCash", currency="USD", value="0.00000001"),
            dict(account="DU00000001", tag="AvailableFunds", currency="USD", value="3.14"),
            dict(account="DU00000002", tag="SettledCash", currency="USD", value="PRIVATE_AMOUNT_CANARY"),
            dict(account="DU00000001", tag="SettledCash", currency="BASE", value="900")]
    broker = SummaryBroker(rows)
    try:
        value = entry.read_account_summary_cash(broker, ["DU00000001"])
        assert value["source_method"] == "ibkr.reqAccountSummary"
        assert value["query_completed"] is True and value["snapshot_atomic"] is False
        assert value["account_ids"] == ["DU00000001"]
        assert value["broker_cash_observations"]["values"] == [
            dict(currency="USD", source_tag="AvailableFunds", value="3.14"),
            dict(currency="USD", source_tag="SettledCash", value="0.00000001")]
        assert "PRIVATE_AMOUNT_CANARY" not in str(value) and "900" not in str(value)
        assert broker.calls == [("request", 17, "All", "SettledCash,AvailableFunds,FullAvailableFunds"), ("cancel", 17)]
        assert len(broker.accountSummaryEvent) == 0
        assert datetime.fromisoformat(value["observed_started_at"]).utcoffset() is not None
    finally:
        broker.loop.close()


@pytest.mark.parametrize("failure", ["request", "timeout", "cached", "account"])
def test_summary_failure_never_retries_or_retains_subscription(failure):
    broker = SummaryBroker([], failure)
    if failure == "cached":
        broker.wrapper.acctSummary["old"] = object()
    try:
        with pytest.raises((RuntimeError, ValueError, TimeoutError)):
            entry.read_account_summary_cash(broker, ["DU00000003"] if failure == "account" else ["DU00000001"], timeout_seconds=0.001)
        assert len(broker.accountSummaryEvent) == 0
        requests = [row for row in broker.calls if row[0] == "request"]
        assert len(requests) == (0 if failure in {"cached", "account"} else 1)
        assert ("cancel", 17) in broker.calls if requests else not broker.calls
    finally:
        broker.loop.close()


def test_summary_duplicate_native_fields_stay_invalid():
    rows = [dict(account="DU00000001", tag="SettledCash", currency="USD", value="1")] * 2
    broker = SummaryBroker(rows)
    try:
        value = entry.read_account_summary_cash(broker, ["DU00000001"])
        assert "values" not in value["broker_cash_observations"]
        assert value["broker_cash_observations"]["invalid_fields"] == [dict(currency="USD", source_tag="SettledCash")]
    finally:
        broker.loop.close()


def test_real_sdk_wrapper_completes_summary_without_network(monkeypatch):
    from ib_insync import IB
    from ib_insync.util import getLoop
    previous = getLoop()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        broker = IB()
        broker.wrapper.accounts = ["DU00000001"]
        calls = []
        monkeypatch.setattr(broker.client, "getReqId", lambda: 19)
        def request(req, group, tags):
            calls.append((req, group, tags))
            broker.wrapper.accountSummary(req, "DU00000001", "SettledCash", "40.0123", "USD")
            broker.wrapper.accountSummaryEnd(req)
        monkeypatch.setattr(broker.client, "reqAccountSummary", request)
        monkeypatch.setattr(broker.client, "cancelAccountSummary", lambda req: calls.append(("cancel", req)))
        value = entry.read_account_summary_cash(broker, ["DU00000001"])
        assert value["query_completed"] is True
        assert value["broker_cash_observations"]["values"] == [dict(currency="USD", source_tag="SettledCash", value="40.0123")]
        assert calls[-1] == ("cancel", 19) and len(calls) == 2
        assert not broker.wrapper._futures and len(broker.accountSummaryEvent) == 0
    finally:
        loop.close()
        asyncio.set_event_loop(previous)
