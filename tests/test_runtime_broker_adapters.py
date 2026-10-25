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


def test_persistent_main_submit_without_grant_never_reaches_adapter(strategy_module_factory, monkeypatch):
    """A real public submit wrapper must reject before its venue adapter."""
    from quant_platform_kit.common.models import OrderIntent

    module = strategy_module_factory()
    calls = []
    monkeypatch.setattr(module, "submit_order_intent", lambda *args, **kwargs: calls.append((args, kwargs)))
    with pytest.raises(RuntimeError, match="session|grant|contract"):
        module.submit_market_order_intent(
            object(), OrderIntent(symbol="VOO", side="buy", quantity=1, account_id="U00000003"),
        )
    assert calls == []


@pytest.fixture
def persistent_real_caller(tmp_path, monkeypatch, strategy_module_factory):
    """Actual main -> adapter -> executor -> platform/QPK -> fake placeOrder."""
    from dataclasses import replace
    from functools import partial
    from application import execution_service as service
    from application.ibkr_order_execution import submit_order_intent as platform_submit
    from quant_platform_kit.common import execution_state as contract
    from quant_platform_kit.common.models import OrderIntent
    from notifications.telegram import build_translator

    module = strategy_module_factory()
    account = "U00000003"
    receipt = {"attestation_state": "self_attested", "release_id": "synthetic-release",
               "strategy_release": {"strategy_revision": "a" * 40}, "loaded_at": "synthetic-time-a"}
    targets = {"VOO": 0.0, "QQQM": 0.5}
    metadata = {
        "strategy_profile": "global_etf_rotation", "trade_date": "2026-10-20",
        "effective_date": "2026-10-20", "risk_gate": "APPROVE", "risk_flags": ("risk_gate:passed",),
        "account_new_risk_snapshot": {"observation_status": "COMPLETE", "reconciliation_status": "VERIFIED", "circuit_breaker_state": "CLOSED"},
        "allocation": {"target_mode": "weight", "strategy_symbols": tuple(targets), "risk_symbols": tuple(targets),
                       "income_symbols": (), "safe_haven_symbols": (), "targets": targets},
        "risk_authority": {"strategy_profile": "global_etf_rotation", "account_ids": (account,),
                           "trade_date": "2026-10-20", "signal_date": None, "effective_date": "2026-10-20",
                           "snapshot_as_of": None, "portfolio_equity": 2000.0, "targets": targets,
                           "max_target_values": {"VOO": 0.0, "QQQM": 1000.0}},
    }
    state = SimpleNamespace(submitted=[], status="Submitted", filled=0, fill_price=0,
                            metadata=metadata, receipt=receipt, executor_calls=[], claim_calls=[])

    class FakeIB:
        def openTrades(self): return []
        reqAllOpenOrders = openTrades
        def fills(self): return []
        def accountValues(self):
            return [SimpleNamespace(tag="CashBalance", currency="USD", value="1500", account=account)]
        def qualifyContracts(self, value): return [value]
        def placeOrder(self, instrument, order):
            state.submitted.append((instrument, order))
            return SimpleNamespace(order=SimpleNamespace(orderId=len(state.submitted)), orderStatus=SimpleNamespace(
                status=state.status, filled=state.filled, avgFillPrice=state.fill_price,
            ))

    ib = FakeIB()
    monkeypatch.setattr(service.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(service, "maybe_publish_attention_for_admission", lambda *_a, **_kw: {})
    monkeypatch.setattr("application.account_new_risk_gate_support.resolve_production_drift_status_from_store", lambda **_kw: None)
    monkeypatch.setattr(module, "submit_order_intent", lambda _ib, intent, **kwargs: platform_submit(
        _ib, intent, wait_seconds=0,
        stock_factory=lambda symbol, _ex, _cur: SimpleNamespace(symbol=symbol),
        market_order_factory=lambda side, qty: SimpleNamespace(action=side, totalQuantity=qty),
        limit_order_factory=lambda side, qty, price: SimpleNamespace(action=side, totalQuantity=qty, lmtPrice=price),
        **kwargs,
    ))
    # A partial binds the real executor; no fake final executor in this chain.
    executor = partial(service.execute_rebalance, execution_lock_dir=tmp_path / "locks")
    adapters = replace(
        _build_adapters(account_ids=(account,)), strategy_profile="global_etf_rotation",
        submit_order_intent_fn=module.submit_market_order_intent,
        application_execute_rebalance_fn=executor, order_intent_cls=OrderIntent,
        fetch_quote_snapshots_fn=lambda _ib, symbols: {s: SimpleNamespace(last_price=50.0) for s in symbols},
        translator=build_translator("en"), limit_buy_premium=1.0,
        runtime_release_receipt={**receipt, "loaded_at": "synthetic-time-b"},
        sell_settle_delay_sec=0,
    )
    monkeypatch.setattr(module, "build_broker_adapters", lambda **_kw: state.adapters)
    store = contract.ExecutionMarkerStore(local_dir=tmp_path / "state")
    state.module, state.adapters, state.store, state.ib = module, adapters, store, ib

    def session():
        cls = getattr(contract, "AccountSubmissionSession", None)
        if cls is None:
            return None
        return cls(store, broker="ibkr", account_id=account, owner_id="global_etf_rotation", profile="global_etf_rotation",
                   intent_id=contract.submission_intent_digest(metadata, "global_etf_rotation"),
                   release_id=contract.submission_release_digest(receipt))

    state.session = session

    def run(*, execution_session="new", dry_run=False):
        selected = session() if execution_session == "new" else execution_session
        state.adapters = replace(state.adapters, dry_run_only=dry_run)
        return module.execute_rebalance(
            ib, targets, {"VOO": {"quantity": 10.0, "avg_cost": 50.0}},
            {"equity": 2000.0, "buying_power": 1500.0}, signal_metadata=metadata,
            acquire_execution_claim=lambda: state.claim_calls.append(True) or True,
            account_execution_session=selected,
        )

    state.run = run
    state.tmp_path = tmp_path
    return state


def test_persistent_main_real_caller_transmits_and_records_two_known_acks(persistent_real_caller):
    state = persistent_real_caller
    _logs, summary = state.run()
    assert [(c.symbol, o.action, o.totalQuantity) for c, o in state.submitted] == [("VOO", "SELL", 10), ("QQQM", "BUY", 20)]
    assert summary["execution_status"] == "pending_reconciliation"
    attempts = state.session().read_state()["attempts"]
    assert len(attempts) == 2
    assert {a["certainty"] for a in attempts.values()} == {"KNOWN_PENDING"}
    assert len(state.claim_calls) == 2


def test_persistent_main_unknown_account_survives_new_date_and_intent(persistent_real_caller):
    state = persistent_real_caller
    state.status = "Unknown"
    _logs, summary = state.run()
    assert len(state.submitted) == 1
    assert summary["outcome_unknown"] is True
    state.metadata["trade_date"] = "2026-10-21"
    state.metadata["effective_date"] = "2026-10-21"
    state.metadata["risk_authority"].update(trade_date="2026-10-21", effective_date="2026-10-21")
    with pytest.raises(RuntimeError, match="unresolved"):
        state.run()
    assert len(state.submitted) == 1


def test_persistent_main_missing_session_rejects(persistent_real_caller):
    state = persistent_real_caller
    with pytest.raises(RuntimeError, match="session|contract"):
        state.run(execution_session=None)
    assert state.submitted == []


def test_persistent_main_release_mismatch_rejects_before_reservation(persistent_real_caller):
    from dataclasses import replace
    state = persistent_real_caller
    session = state.session()
    state.adapters = replace(state.adapters, runtime_release_receipt={**state.receipt, "release_id": "different"})
    with pytest.raises(RuntimeError, match="binding"):
        state.run(execution_session=session)
    assert state.submitted == []
    assert session.read_state()["attempts"] == {}


def test_persistent_main_dry_run_no_reservations_or_venue(persistent_real_caller):
    state = persistent_real_caller
    _logs, summary = state.run(dry_run=True)
    assert summary["mode"] == "dry_run"
    assert state.submitted == []
    assert state.claim_calls == []
    assert not list((state.tmp_path / "state").rglob("*.json"))


def test_persistent_main_old_contract_imports_but_no_submit(persistent_real_caller, monkeypatch):
    from quant_platform_kit.common import execution_state as contract
    state = persistent_real_caller
    monkeypatch.delattr(contract, "AccountSubmissionSession", raising=False)
    with pytest.raises(RuntimeError, match="session|contract"):
        state.run(execution_session=None)
    assert state.submitted == []


@pytest.mark.parametrize("status,filled,price", [("PartiallyFilled", 1, 49), ("Rejected", 0, 0)])
def test_persistent_main_first_partial_or_reject_retains_healthy_path(persistent_real_caller, status, filled, price):
    state = persistent_real_caller
    state.status, state.filled, state.fill_price = status, filled, price
    _logs, summary = state.run()
    assert len(state.submitted) == 2
    assert not summary.get("outcome_unknown")
    attempts = state.session().read_state()["attempts"]
    assert {a["certainty"] for a in attempts.values()} == ({"KNOWN_PENDING"} if filled else {"TERMINAL"})
    assert {a["filled_quantity"] for a in attempts.values()} == {float(filled)}


def test_persistent_main_transport_after_place_order_is_durable_unknown(persistent_real_caller, monkeypatch):
    state = persistent_real_caller
    original = state.ib.placeOrder

    def accepted_then_disconnect(instrument, order):
        original(instrument, order)
        raise ConnectionError("synthetic broker disconnect after accepted call")

    monkeypatch.setattr(state.ib, "placeOrder", accepted_then_disconnect)
    with pytest.raises(ConnectionError):
        state.run()
    assert len(state.submitted) == 1
    assert {a["certainty"] for a in state.session().read_state()["attempts"].values()} == {"UNKNOWN"}


def test_persistent_main_old_worker_paused_at_venue_cannot_be_stolen(persistent_real_caller, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from quant_platform_kit.common.models import OrderIntent
    from quant_platform_kit.common import execution_state as contract
    state = persistent_real_caller
    original = state.ib.placeOrder
    paused, resume = threading.Event(), threading.Event()

    def before_actual_venue(instrument, order):
        paused.set()
        assert resume.wait(5)
        return original(instrument, order)

    monkeypatch.setattr(state.ib, "placeOrder", before_actual_venue)
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(state.run)
        assert paused.wait(5)
        try:
            other = contract.AccountSubmissionSession(
                state.store, broker="ibkr", account_id="U00000003", owner_id="same-owner",
                profile="global_etf_rotation", intent_id="c"*64,
                release_id=contract.submission_release_digest(state.receipt),
            )
            with pytest.raises(RuntimeError, match="unresolved"):
                other.reserve(OrderIntent(symbol="QQQM", side="buy", quantity=1, account_id="U00000003"))
            assert state.submitted == []
        finally:
            resume.set()
        worker.result(timeout=5)
    # One original SELL and its legal known-ACK successor; no competing worker order.
    assert [(c.symbol, o.action) for c, o in state.submitted] == [("VOO", "SELL"), ("QQQM", "BUY")]


def test_persistent_main_rejects_wrong_broker_grant_and_future_contract(persistent_real_caller, monkeypatch):
    from quant_platform_kit.common import execution_state as contract
    from quant_platform_kit.common.models import OrderIntent
    state = persistent_real_caller
    order = OrderIntent(symbol="VOO", side="sell", quantity=1, account_id="U00000003")
    wrong = contract.AccountSubmissionSession(
        state.store, broker="other", account_id="U00000003", owner_id="same", profile="global_etf_rotation",
        intent_id="c"*64, release_id=contract.submission_release_digest(state.receipt),
    )
    grant = wrong.reserve(order)
    with pytest.raises(RuntimeError, match="session|contract"):
        state.module.submit_market_order_intent(state.ib, order, account_execution_session=wrong, execution_grant=grant)
    monkeypatch.setattr(contract.AccountSubmissionSession, "contract_version", "future.v2")
    with pytest.raises(RuntimeError, match="session|contract"):
        state.run()
    assert state.submitted == []
