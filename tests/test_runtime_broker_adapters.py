from types import SimpleNamespace

import pytest

from application.runtime_broker_adapters import (
    IBKRGatewayUnavailableError,
    IBKRTradingPermissionError,
    build_runtime_broker_adapters,
)


def _build_adapters(
    *,
    account_ids=("U00000003",),
    execution_mode="paper",
    trading_permission_probe_fn=None,
):
    return build_runtime_broker_adapters(
        host_resolver=lambda: "127.0.0.1",
        ib_port=4001,
        ib_client_id=11,
        connect_timeout_seconds=60,
        connect_attempts=1,
        connect_retry_delay_seconds=0,
        client_id_retry_offset=100,
        ensure_event_loop_fn=lambda: None,
        connect_ib_fn=lambda *_args, **_kwargs: SimpleNamespace(managedAccounts=lambda: ["U00000003"]),
        fetch_portfolio_snapshot_fn=lambda *_args, **_kwargs: None,
        fetch_quote_snapshots_fn=lambda *_args, **_kwargs: None,
        submit_order_intent_fn=lambda *_args, **_kwargs: None,
        application_get_market_prices_fn=lambda *_args, **_kwargs: None,
        application_check_order_submitted_fn=lambda *_args, **_kwargs: None,
        application_execute_rebalance_fn=lambda *_args, **_kwargs: None,
        execute_paper_liquidation_fn=lambda *_args, **_kwargs: None,
        translator=lambda key, **_kwargs: key,
        strategy_profile="global_etf_rotation",
        account_group="live-slot-a",
        service_name="interactive-brokers-live-slot-a-service",
        account_ids=account_ids,
        execution_mode=execution_mode,
        dry_run_only=False,
        cash_reserve_ratio=0.0,
        cash_reserve_floor_usd=0.0,
        rebalance_threshold_ratio=0.02,
        limit_buy_premium=1.005,
        quantity_step=1.0,
        min_order_notional=50.0,
        safe_haven_cash_substitute_threshold_usd=750.0,
        sell_settle_delay_sec=0.0,
        separator="---",
        strategy_display_name="Test Strategy",
        sleep_fn=lambda _seconds: None,
        printer=lambda *_args, **_kwargs: None,
        trading_permission_probe_fn=(
            trading_permission_probe_fn
            if trading_permission_probe_fn is not None
            else lambda ib: ib.whatIfOrder(None, None)
        ),
    )


def test_connect_ib_accepts_configured_managed_account():
    adapters = _build_adapters(account_ids=("U00000003",))

    ib = adapters.connect_ib()

    assert ib.managedAccounts() == ["U00000003"]


def test_connect_ib_resolves_gateway_host_again_for_each_retry():
    observed = {"hosts": [], "refreshes": 0}
    current_host = {"value": "10.0.0.8"}

    def resolve_host():
        return current_host["value"]

    def refresh_host():
        observed["refreshes"] += 1
        current_host["value"] = "10.0.0.9"
        return current_host["value"]

    def connect(host, *_args, **_kwargs):
        observed["hosts"].append(host)
        if len(observed["hosts"]) == 1:
            raise ConnectionRefusedError("gateway restarting")
        return SimpleNamespace(managedAccounts=lambda: ["U00000003"])

    adapters = _build_adapters()
    adapters = adapters.__class__(
        **{
            **adapters.__dict__,
            "host_resolver": resolve_host,
            "refresh_host_fn": refresh_host,
            "connect_ib_fn": connect,
            "connect_attempts": 2,
        }
    )

    adapters.connect_ib()

    assert observed == {
        "hosts": ["10.0.0.8", "10.0.0.9"],
        "refreshes": 1,
    }


def test_connect_ib_stops_retry_when_gateway_project_lookup_fails():
    attempted_hosts = []

    def connect(host, *_args, **_kwargs):
        attempted_hosts.append(host)
        raise ConnectionRefusedError("gateway restarting")

    def refresh_host():
        raise IBKRGatewayUnavailableError("configured Gateway project lookup unavailable")

    adapters = _build_adapters()
    adapters = adapters.__class__(
        **{
            **adapters.__dict__,
            "host_resolver": lambda: "10.0.0.8",
            "refresh_host_fn": refresh_host,
            "connect_ib_fn": connect,
            "connect_attempts": 3,
        }
    )

    with pytest.raises(IBKRGatewayUnavailableError, match="project lookup unavailable"):
        adapters.connect_ib()
    assert attempted_hosts == ["10.0.0.8"]


def test_connect_ib_raises_dedicated_error_after_retries_are_exhausted():
    adapters = _build_adapters()
    adapters = adapters.__class__(
        **{
            **adapters.__dict__,
            "connect_ib_fn": lambda *_args, **_kwargs: (_ for _ in ()).throw(
                TimeoutError("handshake timeout")
            ),
            "connect_attempts": 2,
        }
    )

    with pytest.raises(IBKRGatewayUnavailableError, match="unavailable after 2 attempt") as exc_info:
        adapters.connect_ib()

    assert isinstance(exc_info.value.__cause__, TimeoutError)


def test_connect_ib_rejects_configured_account_not_visible_to_gateway_username():
    observed = {"disconnects": 0}

    class FakeIB:
        def managedAccounts(self):
            return ["U00000009"]

        def disconnect(self):
            observed["disconnects"] += 1

    adapters = _build_adapters(account_ids=("U00000003",))
    adapters = adapters.__class__(
        **{
            **adapters.__dict__,
            "connect_ib_fn": lambda *_args, **_kwargs: FakeIB(),
        }
    )

    with pytest.raises(RuntimeError, match="Configured IBKR account_ids are not available"):
        adapters.connect_ib()

    assert observed["disconnects"] == 1


def test_connect_ib_rejects_live_mode_without_configured_account_ids():
    observed = {"disconnects": 0}

    class FakeIB:
        def managedAccounts(self):
            return ["U00000003"]

        def disconnect(self):
            observed["disconnects"] += 1

    adapters = _build_adapters(account_ids=(), execution_mode="live")
    adapters = adapters.__class__(
        **{
            **adapters.__dict__,
            "connect_ib_fn": lambda *_args, **_kwargs: FakeIB(),
        }
    )

    with pytest.raises(RuntimeError, match="IBKR live execution requires configured account_ids"):
        adapters.connect_ib()

    assert observed["disconnects"] == 1


def test_connect_ib_rejects_live_gateway_read_only_mode_without_retry():
    observed = {"disconnects": 0, "permission_probe_requests": 0}

    class FakeEvent:
        def __init__(self):
            self.handlers = []

        def __iadd__(self, handler):
            self.handlers.append(handler)
            return self

        def __isub__(self, handler):
            self.handlers.remove(handler)
            return self

        def emit(self, *args):
            for handler in tuple(self.handlers):
                handler(*args)

    class FakeIB:
        RaiseRequestErrors = False
        RequestTimeout = 0

        def __init__(self):
            self.errorEvent = FakeEvent()

        def managedAccounts(self):
            return ["U00000003"]

        def whatIfOrder(self, _contract, _order):
            observed["permission_probe_requests"] += 1
            self.errorEvent.emit(-1, 321, "API is in Read-Only mode", None)
            raise TimeoutError("open orders timed out")

        def disconnect(self):
            observed["disconnects"] += 1

    adapters = _build_adapters(account_ids=("U00000003",), execution_mode="live")
    adapters = adapters.__class__(
        **{
            **adapters.__dict__,
            "connect_ib_fn": lambda *_args, **_kwargs: FakeIB(),
            "connect_attempts": 3,
        }
    )

    with pytest.raises(IBKRTradingPermissionError, match="Read-Only"):
        adapters.connect_ib()

    assert observed == {
        "disconnects": 1,
        "permission_probe_requests": 1,
    }


def test_connect_ib_accepts_margin_rejection_as_write_access_proof():
    class FakeEvent:
        def __iadd__(self, handler):
            return self

        def __isub__(self, handler):
            return self

    class FakeIB:
        RaiseRequestErrors = False
        RequestTimeout = 0

        def __init__(self):
            self.errorEvent = FakeEvent()

        def managedAccounts(self):
            return ["U00000003"]

        def whatIfOrder(self, _contract, _order):
            raise RuntimeError(
                "Error 201, reqId 21: Order rejected - reason:YOUR ORDER IS NOT ACCEPTED. "
                "IN ORDER TO OBTAIN THE DESIRED POSITION YOUR EQUITY WITH LOAN VALUE "
                "[390.06 USD] MUST EXCEED THE INITIAL MARGIN [434.70 USD]"
            )

    adapters = _build_adapters(account_ids=("U00000003",), execution_mode="live")
    adapters = adapters.__class__(
        **{
            **adapters.__dict__,
            "connect_ib_fn": lambda *_args, **_kwargs: FakeIB(),
        }
    )

    assert adapters.connect_ib().managedAccounts() == ["U00000003"]


def test_connect_ib_retries_when_trading_permission_probe_loses_connection():
    observed = {
        "connects": 0,
        "disconnects": 0,
        "permission_probe_requests": 0,
        "refreshes": 0,
    }

    class FakeEvent:
        def __init__(self):
            self.handlers = []

        def __iadd__(self, handler):
            self.handlers.append(handler)
            return self

        def __isub__(self, handler):
            self.handlers.remove(handler)
            return self

    class FakeIB:
        RaiseRequestErrors = False
        RequestTimeout = 0

        def __init__(self, attempt):
            self.attempt = attempt
            self.errorEvent = FakeEvent()

        def managedAccounts(self):
            return ["U00000003"]

        def whatIfOrder(self, _contract, _order):
            observed["permission_probe_requests"] += 1
            if self.attempt == 1:
                raise ConnectionError("gateway connection dropped")
            return []

        def disconnect(self):
            observed["disconnects"] += 1

    def connect(*_args, **_kwargs):
        observed["connects"] += 1
        return FakeIB(observed["connects"])

    def refresh_host():
        observed["refreshes"] += 1
        return "127.0.0.1"

    adapters = _build_adapters(account_ids=("U00000003",), execution_mode="live")
    adapters = adapters.__class__(
        **{
            **adapters.__dict__,
            "connect_ib_fn": connect,
            "connect_attempts": 2,
            "refresh_host_fn": refresh_host,
        }
    )

    assert adapters.connect_ib().managedAccounts() == ["U00000003"]
    assert observed == {
        "connects": 2,
        "disconnects": 1,
        "permission_probe_requests": 2,
        "refreshes": 1,
    }


def test_connect_ib_does_not_misclassify_unrelated_321_as_read_only():
    class FakeEvent:
        def __init__(self):
            self.handlers = []

        def __iadd__(self, handler):
            self.handlers.append(handler)
            return self

        def __isub__(self, handler):
            self.handlers.remove(handler)
            return self

        def emit(self, *args):
            for handler in tuple(self.handlers):
                handler(*args)

    class FakeIB:
        RaiseRequestErrors = False
        RequestTimeout = 0

        def __init__(self):
            self.errorEvent = FakeEvent()

        def managedAccounts(self):
            return ["U00000003"]

        def whatIfOrder(self, _contract, _order):
            self.errorEvent.emit(-1, 321, "Generic validation error", None)
            return SimpleNamespace(warningText="")

    adapters = _build_adapters(account_ids=("U00000003",), execution_mode="live")
    adapters = adapters.__class__(
        **{
            **adapters.__dict__,
            "connect_ib_fn": lambda *_args, **_kwargs: FakeIB(),
        }
    )

    assert adapters.connect_ib().managedAccounts() == ["U00000003"]


def test_connect_ib_rejects_empty_what_if_response():
    disconnected = []
    ib = SimpleNamespace(
        managedAccounts=lambda: ["U00000003"],
        whatIfOrder=lambda *_args: None,
        disconnect=lambda: disconnected.append(True),
        RaiseRequestErrors=False,
        RequestTimeout=0,
    )
    adapters = _build_adapters(account_ids=("U00000003",), execution_mode="live")
    adapters = adapters.__class__(
        **{**adapters.__dict__, "connect_ib_fn": lambda *_args, **_kwargs: ib}
    )

    with pytest.raises(IBKRTradingPermissionError, match="could not verify"):
        adapters.connect_ib()
    assert disconnected == [True]


def test_connect_ib_skips_trading_permission_probe_for_dry_run():
    class FakeIB:
        def managedAccounts(self):
            return ["U00000003"]

        def whatIfOrder(self, _contract, _order):
            pytest.fail("dry-run connection must not probe trading permissions")

    adapters = _build_adapters(account_ids=("U00000003",), execution_mode="live")
    adapters = adapters.__class__(
        **{
            **adapters.__dict__,
            "connect_ib_fn": lambda *_args, **_kwargs: FakeIB(),
            "dry_run_only": True,
        }
    )

    assert adapters.connect_ib().managedAccounts() == ["U00000003"]


def test_connect_ib_skips_trading_permission_probe_for_explicit_read_only_connection():
    class FakeIB:
        def managedAccounts(self):
            return ["U00000003"]

        def whatIfOrder(self, _contract, _order):
            pytest.fail("read-only connection must not probe trading permissions")

    adapters = _build_adapters(account_ids=("U00000003",), execution_mode="live")
    adapters = adapters.__class__(
        **{
            **adapters.__dict__,
            "connect_ib_fn": lambda *_args, **_kwargs: FakeIB(),
        }
    )

    assert adapters.connect_ib(validate_trading_permissions=False).managedAccounts() == ["U00000003"]


def test_connect_ib_redacts_read_only_connection_diagnostics():
    marker = "gateway.example.internal:4999 client_id=72 account=demo-account"
    messages = []
    adapters = _build_adapters()
    adapters = adapters.__class__(
        **{
            **adapters.__dict__,
            "host_resolver": lambda: "gateway.example.internal",
            "ib_port": 4999,
            "ib_client_id": 72,
            "connect_ib_fn": lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError(marker)),
            "printer": lambda message, **_kwargs: messages.append(message),
        }
    )

    with pytest.raises(IBKRGatewayUnavailableError, match="read-only attempt") as exc_info:
        adapters.connect_ib(redact_connection_diagnostics=True)

    assert marker not in str(exc_info.value)
    rendered = "\n".join(messages)
    assert marker not in rendered
    assert "gateway.example.internal" not in rendered
    assert "4999" not in rendered
    assert "client_id" not in rendered


def _paper_facts_snapshot(*, empty=False):
    from datetime import datetime, timezone

    return SimpleNamespace(
        as_of=datetime(2026, 9, 30, 1, 0, 1, tzinfo=timezone.utc),
        total_equity=10.0,
        buying_power=10.0,
        positions=() if empty else (
            SimpleNamespace(
                symbol="VOO", quantity=1.0, market_value=10.0,
                average_cost=10.0, currency="USD",
            ),
        ),
        metadata={
            "account_ids": ("U00000001",),
            "cash_balances": (
                {"account_id": "U00000001", "currency": "USD", "CashBalance": 10},
            ),
            "private_native_marker": "synthetic-private-native-metadata",
        },
    )


def _paper_facts_harness(snapshots):
    observed = {"fetches": 0, "sleeps": [], "executions": [], "messages": [], "logs": [], "disconnects": 0}
    remaining = iter(snapshots)

    def disconnect():
        observed["disconnects"] += 1

    ib = SimpleNamespace(disconnect=disconnect)

    def fetch(_ib, **_kwargs):
        assert _ib is ib
        observed["fetches"] += 1
        item = next(remaining)
        if isinstance(item, Exception):
            raise item
        return item

    def unexpected_order(*_args, **_kwargs):
        pytest.fail("synthetic paper report tests must not submit an order")

    def execute(_ib, positions, **kwargs):
        assert _ib is ib
        assert kwargs["submit_order_intent"] is unexpected_order
        assert kwargs["dry_run_only"] is True
        observed["executions"].append(positions)
        return {
            "mode": "dry_run", "execution_status": "dry_run" if positions else "no_op",
            "positions_seen": len(positions), "orders_submitted": [], "orders_skipped": [],
        }

    def publish(**kwargs):
        assert kwargs["detailed_text"] == kwargs["compact_text"]
        observed["messages"].append(kwargs["detailed_text"])

    adapters = _build_adapters(account_ids=("U00000001",), execution_mode="paper")
    adapters = adapters.__class__(
        **{
            **adapters.__dict__, "fetch_portfolio_snapshot_fn": fetch,
            "execute_paper_liquidation_fn": execute, "submit_order_intent_fn": unexpected_order,
            "dry_run_only": True, "sleep_fn": observed["sleeps"].append,
            "printer": lambda message, **_kwargs: observed["logs"].append(message),
        }
    )

    def cycle(*, getter=None):
        return adapters.run_paper_liquidation_cycle(
            connect_ib_fn=lambda: ib,
            get_current_portfolio_fn=getter or adapters.get_current_portfolio,
            publish_notification_fn=publish,
        )

    return adapters, ib, observed, cycle


def test_paper_portfolio_view_preserves_only_projected_native_facts():
    import copy

    from application.account_facts import build_ibkr_account_facts

    snapshot = _paper_facts_snapshot()
    before = copy.deepcopy(snapshot)
    expected = build_ibkr_account_facts(snapshot)
    adapters, ib, observed, _cycle = _paper_facts_harness([snapshot])

    positions, values = adapters.get_current_portfolio(ib)

    assert positions == {"VOO": {"quantity": 1.0, "avg_cost": 10.0}}
    assert values == {"equity": 10.0, "buying_power": 10.0, "account_facts": expected}
    assert expected["observed_at"] == "2026-09-30T01:00:01Z"
    assert expected["net_assets"] is None
    assert "private_native_marker" not in expected
    assert snapshot == before
    assert observed["fetches"] == 1


@pytest.mark.parametrize("invalid", ["missing", "multi", "account_mismatch", "cash_missing", "cash_invalid", "naive_time"])
def test_paper_portfolio_view_keeps_legacy_shape_when_native_facts_are_unknown(invalid):
    snapshot = _paper_facts_snapshot()
    if invalid == "missing":
        snapshot.metadata = {}
    elif invalid == "multi":
        snapshot.metadata["account_ids"] = ("U00000001", "U00000002")
    elif invalid == "account_mismatch":
        snapshot.metadata["account_ids"] = ("U00000002",)
    elif invalid == "cash_missing":
        snapshot.metadata["cash_balances"] = ()
    elif invalid == "cash_invalid":
        snapshot.metadata["cash_balances"][0]["CashBalance"] = float("nan")
    else:
        snapshot.as_of = snapshot.as_of.replace(tzinfo=None)
    adapters, ib, observed, _cycle = _paper_facts_harness([snapshot])

    _positions, values = adapters.get_current_portfolio(ib)

    assert values == {"equity": 10.0, "buying_power": 10.0}
    assert observed["fetches"] == 1


def test_paper_portfolio_view_optional_projector_failure_does_not_change_legacy_result(monkeypatch):
    from application import runtime_broker_adapters as broker_adapters

    def failed(_snapshot):
        raise RuntimeError("synthetic-private-projector-failure")

    monkeypatch.setattr(broker_adapters, "build_ibkr_account_facts", failed, raising=False)
    adapters, ib, observed, _cycle = _paper_facts_harness([_paper_facts_snapshot()])

    _positions, values = adapters.get_current_portfolio(ib)

    assert values == {"equity": 10.0, "buying_power": 10.0}
    assert observed["fetches"] == 1
    assert not observed["logs"]


def test_paper_cycle_preserves_native_facts_without_extra_fetch_or_fact_logging():
    from application.account_facts import build_ibkr_account_facts
    from application.cycle_result import coerce_strategy_cycle_result

    snapshot = _paper_facts_snapshot()
    _adapters, _ib, observed, cycle = _paper_facts_harness([snapshot])

    result = cycle()

    assert result.account_facts == build_ibkr_account_facts(snapshot)
    assert type(result.account_facts) is dict
    assert coerce_strategy_cycle_result(result) is result
    assert result.result == "OK"
    assert result.execution_summary["execution_status"] == "dry_run"
    assert observed["fetches"] == 1
    assert observed["sleeps"] == []
    assert observed["executions"] == [{"VOO": {"quantity": 1.0, "avg_cost": 10.0}}]
    assert len(observed["messages"]) == observed["disconnects"] == 1
    rendered = "\n".join(observed["messages"] + observed["logs"])
    assert "U00000001" not in rendered
    assert result.account_facts["observed_at"] not in rendered
    assert "synthetic-private-native-metadata" not in rendered


@pytest.mark.parametrize("last_state", ["valid", "invalid", "empty"])
def test_paper_cycle_uses_only_last_existing_retry_snapshot(last_state):
    from datetime import timedelta

    from application.account_facts import build_ibkr_account_facts

    first = _paper_facts_snapshot(empty=True)
    last = _paper_facts_snapshot(empty=last_state == "empty")
    last.as_of += timedelta(seconds=1)
    last.metadata["cash_balances"][0]["CashBalance"] = 20
    if last_state == "invalid":
        last.metadata["cash_balances"] = ()
    _adapters, _ib, observed, cycle = _paper_facts_harness([first, last])

    result = cycle()

    assert result.account_facts == build_ibkr_account_facts(last)
    assert result.account_facts != build_ibkr_account_facts(first)
    assert observed["fetches"] == 2
    assert observed["sleeps"] == [2.0]
    assert len(observed["executions"]) == len(observed["messages"]) == observed["disconnects"] == 1


@pytest.mark.parametrize("optional", [None, "invalid", [], {"account_ids": ["U00000001"]}])
def test_paper_cycle_legacy_view_or_invalid_optional_shape_does_not_synthesize_facts(optional):
    _adapters, _ib, observed, cycle = _paper_facts_harness([])
    values = {"equity": 10.0, "buying_power": 10.0}
    if optional is not None:
        values["account_facts"] = optional

    result = cycle(getter=lambda _ib: ({"VOO": {"quantity": 1.0, "avg_cost": 10.0}}, values))

    assert result.account_facts == {}
    assert observed["fetches"] == 0
    assert len(observed["executions"]) == len(observed["messages"]) == observed["disconnects"] == 1


@pytest.mark.parametrize("legacy_shape", ["missing_fields", "invalid_time", "invalid_cash", "wrong_currency", "complete"])
def test_paper_cycle_does_not_trust_legacy_sidecar_claiming_native_schema(legacy_shape):
    from application.account_facts import build_ibkr_account_facts

    facts = build_ibkr_account_facts(_paper_facts_snapshot())
    if legacy_shape == "missing_fields":
        facts = {"schema_version": "ibkr_account_snapshot.v1", "account_ids": ["U00000001"]}
    elif legacy_shape == "invalid_time":
        facts["observed_at"] = "invalid"
    elif legacy_shape == "invalid_cash":
        facts["cash"] = [{"currency": "USD", "cash_balance": "invalid", "source_tag": "CashBalance"}]
    elif legacy_shape == "wrong_currency":
        facts["currency"] = "invalid"
    _adapters, _ib, observed, cycle = _paper_facts_harness([])

    result = cycle(getter=lambda _ib: (
        {"VOO": {"quantity": 1.0, "avg_cost": 10.0}},
        {"equity": 10.0, "buying_power": 10.0, "account_facts": facts},
    ))

    assert result.account_facts == {}
    assert observed["fetches"] == 0
    assert len(observed["executions"]) == len(observed["messages"]) == observed["disconnects"] == 1


@pytest.mark.parametrize("retry", [False, True])
def test_paper_cycle_native_fetch_failure_propagates_and_disconnects_without_execution(retry):
    failure = RuntimeError("synthetic-snapshot-fetch-failed")
    reads = [_paper_facts_snapshot(empty=True), failure] if retry else [failure]
    _adapters, _ib, observed, cycle = _paper_facts_harness(reads)

    with pytest.raises(RuntimeError, match="synthetic-snapshot-fetch-failed"):
        cycle()

    assert observed["fetches"] == (2 if retry else 1)
    assert observed["sleeps"] == ([2.0] if retry else [])
    assert observed["executions"] == observed["messages"] == []
    assert observed["disconnects"] == 1


@pytest.mark.parametrize(
    ("selector", "expected_present"),
    [(('U00000001',), True), (('default',), False), (('U00000002',), False), (('U00000001', 'U00000002'), False)],
)
def test_paper_cycle_facts_still_require_original_report_selector_guard(
    selector, expected_present, strategy_module_factory, monkeypatch,
):
    from application.cycle_result import coerce_strategy_cycle_result

    strategy_module = strategy_module_factory(IBKR_DRY_RUN_ONLY="true")
    monkeypatch.setattr(
        strategy_module, "RUNTIME_SETTINGS",
        SimpleNamespace(runtime_target=SimpleNamespace(account_selector=selector)),
    )
    _adapters, _ib, observed, cycle = _paper_facts_harness([_paper_facts_snapshot()])
    result = coerce_strategy_cycle_result(cycle())

    summary = strategy_module._build_cycle_report_summary(
        result, result.execution_summary, {}, dry_run=True,
    )

    assert ("account_facts" in summary) is expected_present
    if expected_present:
        assert summary["account_facts"] == result.account_facts
    assert observed["fetches"] == 1
