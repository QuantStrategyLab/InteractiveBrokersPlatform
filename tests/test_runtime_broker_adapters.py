from types import SimpleNamespace

import pytest

from application.runtime_broker_adapters import build_runtime_broker_adapters


def _build_adapters(*, account_ids=("U1234567",)):
    return build_runtime_broker_adapters(
        host_resolver=lambda: "127.0.0.1",
        ib_port=4001,
        ib_client_id=11,
        connect_timeout_seconds=60,
        connect_attempts=1,
        connect_retry_delay_seconds=0,
        client_id_retry_offset=100,
        ensure_event_loop_fn=lambda: None,
        connect_ib_fn=lambda *_args, **_kwargs: SimpleNamespace(managedAccounts=lambda: ["U1234567"]),
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
    )


def test_connect_ib_accepts_configured_managed_account():
    adapters = _build_adapters(account_ids=("U1234567",))

    ib = adapters.connect_ib()

    assert ib.managedAccounts() == ["U1234567"]


def test_connect_ib_rejects_configured_account_not_visible_to_gateway_username():
    observed = {"disconnects": 0}

    class FakeIB:
        def managedAccounts(self):
            return ["U7654321"]

        def disconnect(self):
            observed["disconnects"] += 1

    adapters = _build_adapters(account_ids=("U1234567",))
    adapters = adapters.__class__(
        **{
            **adapters.__dict__,
            "connect_ib_fn": lambda *_args, **_kwargs: FakeIB(),
        }
    )

    with pytest.raises(RuntimeError, match="Configured IBKR account_ids are not available"):
        adapters.connect_ib()

    assert observed["disconnects"] == 1

