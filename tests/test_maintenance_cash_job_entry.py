"""The one-shot entry uses existing read helpers, never the strategy cycle."""
from types import SimpleNamespace
import asyncio
from datetime import datetime

import pytest

from scripts.maintenance_cash_job_entry import observe
from scripts import maintenance_cash_job_entry as entry


ENV = dict(CLOUD_RUN_JOB="synthetic-job", CLOUD_RUN_EXECUTION="synthetic-job-abc",
           CLOUD_RUN_TASK_INDEX="0", CLOUD_RUN_TASK_ATTEMPT="0", CLOUD_RUN_TASK_COUNT="1")


@pytest.mark.parametrize("failure", [None, "connect", "snapshot", "identity", "summary", "funding"])
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
    def funding_reader(ib, account_ids):
        calls.append(("funding", account_ids))
        if failure == "funding":
            raise RuntimeError("PRIVATE_ERROR_CANARY")
        return {"schema_version": "ibkr.funding_inputs.v1"}
    result = observe(runtime, lambda _: facts, ENV, summary_reader=summary_reader, funding_reader=funding_reader)
    assert result == (0 if failure is None else 1)
    assert calls[0] == ("connect", {"read_only": True, "validate_trading_permissions": False})
    assert ("disconnect" in calls) == (failure != "connect")
    assert calls[-1][0] == "save"
    assert report["diagnostics"]["maintenance_cash_observation"] is True
    assert report["diagnostics"]["execution_kind"] == "cloud_run_job"
    assert report["diagnostics"]["cloud_run_execution"] == ENV["CLOUD_RUN_EXECUTION"]
    assert "PRIVATE_ERROR_CANARY" not in str(report)
    assert report["status"] == ("ok" if failure is None else "error")
    assert ("summary", ["DU00000001"]) in calls if failure in {None, "summary", "funding"} else not any(
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
        self.wrapper = SimpleNamespace(acctSummary={}, startReq=self.start,
            accountSummary=lambda req, account, tag, value, currency: self.accountSummaryEvent.emit(
                SimpleNamespace(account=account, tag=tag, value=value, currency=currency)))
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
            self.wrapper.accountSummary(row.get("request_id", req), row["account"], row["tag"], row["value"], row["currency"])
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
        assert "PRIVATE_AMOUNT_CANARY" not in str(value)
        assert all(row["currency"] != "BASE" for row in value["broker_cash_observations"]["values"])
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


@pytest.mark.parametrize("amounts", [
    ["1", "1"], ["1", "1.00"], ["-0", "0.00"], ["-2", "-2.00"], ["0.00000001", "1e-8"],
])
def test_summary_identical_repeats_are_one_observation_not_spending_authority(amounts):
    from application.ibkr_cash_observations import inspect_cash_observations
    rows = [dict(account="DU00000001", tag="SettledCash", currency="USD", value=amount) for amount in amounts]
    broker = SummaryBroker(rows)
    try:
        value = entry.read_account_summary_cash(broker, ["DU00000001"])
        attachment = value["broker_cash_observations"]
        assert len(attachment["values"]) == 1 and "invalid_fields" not in attachment
        out = inspect_cash_observations(attachment, observed_at=value["observed_finished_at"])
        assert out["fields"]["SettledCash"] == "OBSERVED"
        assert out["spendable_cash_verified"] is out["complete_cash_obligations_verified"] is False
        assert value["field_diagnostics"]["SettledCash"] == dict(usd_rows=2, usd_invalid_values=0,
            usd_duplicate_rows=1, base_rows=0, other_currency_rows=0)
    finally:
        broker.loop.close()


@pytest.mark.parametrize("amounts", [
    ["1", "2"], ["1", "2", "1"], ["1", "1.00000000000000000001"],
    ["1", None, "1"], ["NaN", "1"], ["1", "NaN"],
])
def test_summary_conflicting_or_invalid_repeats_stay_invalid(amounts):
    rows = [dict(account="DU00000001", tag="SettledCash", currency="USD", value=amount) for amount in amounts]
    broker = SummaryBroker(rows)
    try:
        value = entry.read_account_summary_cash(broker, ["DU00000001"])
        assert "values" not in value["broker_cash_observations"]
        assert value["broker_cash_observations"]["invalid_fields"] == [dict(currency="USD", source_tag="SettledCash")]
    finally:
        broker.loop.close()


def test_summary_scopes_request_ids_and_diagnoses_without_exposing_invalid_values():
    rows = [dict(request_id=18, account="DU00000001", tag="SettledCash", currency="USD", value="2"),
        dict(account="DU00000001", tag="SettledCash", currency="BASE", value="11"),
        dict(account="DU00000001", tag="AvailableFunds", currency="USD", value="3"),
        dict(account="DU00000001", tag="AvailableFunds", currency="USD", value="PRIVATE_BAD_VALUE")]
    broker = SummaryBroker(rows)
    original = broker.wrapper.accountSummary
    try:
        result = entry.read_account_summary_cash(broker, ["DU00000001"])
        assert broker.wrapper.accountSummary is original and result["request_scoped"] is True
        assert result["field_diagnostics"]["AvailableFunds"] == dict(usd_rows=2, usd_invalid_values=1,
            usd_duplicate_rows=1, base_rows=0, other_currency_rows=0)
        assert result["field_diagnostics"]["SettledCash"] == dict(usd_rows=0, usd_invalid_values=0,
            usd_duplicate_rows=0, base_rows=1, other_currency_rows=0)
        assert result["broker_cash_observations"]["invalid_fields"] == [dict(currency="USD", source_tag="AvailableFunds")]
        assert "PRIVATE_BAD_VALUE" not in str(result)
    finally:
        broker.loop.close()


class FundingBroker:
    def __init__(self, *, orders=(), cached=(), version=164, minimum=1, increment=1, failure=None):
        from ib_insync import ContractDetails, Stock
        self.client = SimpleNamespace(clientId=7, serverVersion=lambda: version)
        self.orders, self.cached, self.calls, self.failure = orders, cached, [], failure
        self.details = [ContractDetails(contract=Stock("TQQQ", "SMART", "USD", conId=1234),
            minSize=minimum, sizeIncrement=increment)]
        self.loop = asyncio.new_event_loop()

    def managedAccounts(self):
        return ["DU00000001", "DU00000002"]

    async def reqAllOpenOrdersAsync(self):
        self.calls.append("all_orders")
        if self.failure == "orders":
            raise RuntimeError("PRIVATE_CANARY")
        if self.failure == "orders_timeout":
            await self.loop.create_future()
        return self.orders

    def openTrades(self):
        return self.cached

    async def reqContractDetailsAsync(self, contract):
        assert contract.symbol == "TQQQ" and contract.exchange == "SMART" and contract.currency == "USD"
        self.calls.append("contract_details")
        if self.failure == "contract":
            raise RuntimeError("PRIVATE_CANARY")
        return self.details

    def _run(self, awaitable):
        return self.loop.run_until_complete(awaitable)


def _trade(account="DU00000001", *, remaining=0):
    from ib_insync import Trade, Order, OrderStatus, Stock
    return Trade(Stock("TQQQ", "SMART", "USD", conId=1234),
        Order(account=account, totalQuantity=2, action="BUY", orderType="MKT"), OrderStatus(status="Submitted", remaining=remaining))


def test_funding_read_keeps_snapshot_and_unresolved_cache_without_inventing_cash_reserve():
    first, cached = _trade(), _trade(remaining=1)
    broker = FundingBroker(orders=[first, _trade("DU00000002")], cached=[first, cached])
    try:
        result = entry.read_native_funding_inputs(broker, ["DU00000001"], configured_quantity_step=1)
        assert result["open_order_count"] == 2 and len(result["open_orders"]) == 2
        assert all(row["account"] == "DU00000001" for row in result["open_orders"])
        assert result["contract_rules"]["status"] == "OBSERVED"
        assert result["contract_rules"]["minSize"] == result["contract_rules"]["sizeIncrement"] == "1"
        assert result["complete_cash_obligations_verified"] is False
        assert result["account_trading_permissions_verified"] is result["account_fee_plan_verified"] is False
        assert "reserved_cash" not in result and broker.calls == ["all_orders", "contract_details"]
    finally:
        broker.loop.close()


@pytest.mark.parametrize("change", ["client_zero", "foreign", "unscoped_order", "orders", "orders_timeout", "contract", "invalid_response"])
def test_funding_failures_do_not_retry_or_call_later_surfaces(change):
    broker = FundingBroker(failure=change, orders=[_trade("")] if change == "unscoped_order" else None if change == "invalid_response" else [])
    if change == "client_zero":
        broker.client.clientId = 0
    try:
        with pytest.raises(Exception):
            entry.read_native_funding_inputs(broker, ["DU00000099"] if change == "foreign" else ["DU00000001"], configured_quantity_step=1,
                timeout_seconds=0.001 if change == "orders_timeout" else 10)
        assert broker.calls == ([] if change in {"client_zero", "foreign"} else ["all_orders", "contract_details"] if change == "contract" else ["all_orders"])
    finally:
        broker.loop.close()


@pytest.mark.parametrize("version,minimum,increment", [(163, 1, 1), (164, 0, 1), (164, 1, float("nan"))])
def test_unsupported_or_default_size_fields_never_become_rules(version, minimum, increment):
    broker = FundingBroker(version=version, minimum=minimum, increment=increment)
    try:
        result = entry.read_native_funding_inputs(broker, ["DU00000001"], configured_quantity_step="0.01")
        assert result["contract_rules"]["status"] == "UNAVAILABLE"
        assert result["open_order_count"] == 0 and result["open_orders_query_completed"] is True
    finally:
        broker.loop.close()


@pytest.mark.parametrize("amounts,valid", [(["40.0123"], True), (["40.0123", "40.012300"], True),
    (["40.0123", "40.0124", "40.0123"], False)])
def test_real_sdk_wrapper_completes_summary_without_network(monkeypatch, amounts, valid):
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
            for amount in amounts:
                broker.client.decoder.interpret(["63", "1", str(req), "DU00000001", "SettledCash", amount, "USD"])
            broker.client.decoder.interpret(["64", "1", str(req)])
        monkeypatch.setattr(broker.client, "reqAccountSummary", request)
        monkeypatch.setattr(broker.client, "cancelAccountSummary", lambda req: calls.append(("cancel", req)))
        value = entry.read_account_summary_cash(broker, ["DU00000001"])
        assert value["query_completed"] is True
        if valid:
            assert value["broker_cash_observations"]["values"] == [dict(currency="USD", source_tag="SettledCash", value="40.0123")]
        else:
            assert value["broker_cash_observations"]["invalid_fields"] == [dict(currency="USD", source_tag="SettledCash")]
            assert "values" not in value["broker_cash_observations"]
        assert calls[-1] == ("cancel", 19) and len(calls) == 2
        assert not broker.wrapper._futures and len(broker.accountSummaryEvent) == 0
    finally:
        loop.close()
        asyncio.set_event_loop(previous)


def test_real_sdk_funding_futures_complete_without_order_write(monkeypatch):
    from ib_insync import IB, Stock, Order, OrderState, ContractDetails
    from ib_insync.util import getLoop
    previous = getLoop()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        broker = IB()
        broker.wrapper.accounts = ["DU00000001"]
        broker.client.clientId = 7
        monkeypatch.setattr(broker.client, "getReqId", lambda: 19)
        monkeypatch.setattr(broker.client, "serverVersion", lambda: 164)
        contract = Stock("TQQQ", "SMART", "USD", conId=1234)
        def orders():
            broker.wrapper.openOrder(10, contract, Order(account="DU00000001", orderId=10, clientId=7,
                totalQuantity=2, action="BUY", orderType="MKT"), OrderState(status="Submitted"))
            broker.wrapper.openOrderEnd()
        def details(req, request_contract):
            assert request_contract.symbol == "TQQQ"
            broker.wrapper.contractDetails(req, ContractDetails(contract=contract, minSize=1, sizeIncrement=1))
            broker.wrapper.contractDetailsEnd(req)
        monkeypatch.setattr(broker.client, "reqAllOpenOrders", orders)
        monkeypatch.setattr(broker.client, "reqContractDetails", details)
        monkeypatch.setattr(broker.client, "placeOrder", lambda *_: pytest.fail("order write"))
        result = entry.read_native_funding_inputs(broker, ["DU00000001"], configured_quantity_step=1)
        assert result["open_order_count"] == 1  # one object appears in both SDK surfaces
        assert result["contract_rules"]["status"] == "OBSERVED"
        assert result["complete_cash_obligations_verified"] is False
        assert not broker.wrapper._futures
    finally:
        loop.close()
        asyncio.set_event_loop(previous)
