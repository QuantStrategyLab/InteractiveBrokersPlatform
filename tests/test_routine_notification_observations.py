"""Decision-point telemetry and private producer plumbing; all data synthetic."""
from copy import deepcopy
import json
import math
from types import SimpleNamespace

import pytest

from application import execution_service
from notifications import unified_adapter as adapter
from notifications.routine_observations import account_alias, minimum_notional_decision, routine_decisions
from tests.test_execution_service import OrderIntent, _signal_metadata, execute_rebalance, translate
from tests.test_unified_notification_adapter import cycle, event, healthy_receipt_report, report, TARGET, NOW
from tests.test_unified_notification_adapter import routing as routing


@pytest.fixture(autouse=True)
def synthetic_display_defaults(monkeypatch):
    from notifications import display_names
    monkeypatch.setattr(display_names, "_defaults", lambda: {"platforms": {"ibkr": "Demo IBKR"},
        "strategies": {"global_etf_rotation": "Synthetic global strategy"}})


def observation(symbol="DEMO", amount=25.0):
    return dict(reason_code="min_notional", observed_value=amount, limit=50.0, currency="USD", symbol=symbol)


@pytest.mark.parametrize("case", ["no_sell", "sell", "missing_quote"])
def test_minimum_notional_is_recorded_at_each_actual_skip_without_changing_broker_actions(tmp_path, monkeypatch, case):
    class FakeIB:
        def openTrades(self):
            return []

        reqAllOpenOrders = openTrades
        def fills(self):
            return []
        def accountValues(self):
            return [SimpleNamespace(tag="AvailableFunds", currency="USD", value="1000")]

    targets = {"DEMO": 0.4}
    positions = {"DEMO": {"quantity": 3.75, "avg_cost": 100.0}}
    with_sell = case == "sell"
    if with_sell:
        targets = {"AAA": 0.4, **targets}
        positions["AAA"] = {"quantity": 5.0, "avg_cost": 100.0}
    if case == "missing_quote":
        targets["NOQUOTE"] = 0.2
    submitted, claims = [], []
    def submit(_ib, intent):
        submitted.append(intent)
        return SimpleNamespace(broker_order_id="synthetic-order", status="Submitted")
    monkeypatch.setattr(execution_service.time, "sleep", lambda _: None)
    _, summary = execute_rebalance(FakeIB(), targets, positions,
        {"equity": 1000.0, "buying_power": 1000.0},
        fetch_quote_snapshots=lambda _ib, symbols: {s: SimpleNamespace(last_price=100.0) for s in symbols if s != "NOQUOTE"},
        submit_order_intent=submit, order_intent_cls=OrderIntent,
        acquire_execution_claim=lambda: claims.append(True) or True,
        translator=translate, strategy_profile="tech_communication_pullback_enhancement",
        signal_metadata=_signal_metadata(targets, risk_symbols=tuple(targets)),
        dry_run_only=False, cash_reserve_ratio=0.0, rebalance_threshold_ratio=0.001,
        limit_buy_premium=1.0, min_order_notional=50.0, market_currency="USD",
        sell_settle_delay_sec=0, execution_lock_dir=tmp_path, return_summary=True)
    assert summary["notification_routine_decisions"] == [observation()]
    assert [(i.side, i.symbol) for i in submitted] == ([("sell", "AAA")] if with_sell else [])
    assert len(claims) == int(with_sell)
    if case == "missing_quote":
        assert summary["execution_status"] == "blocked"
        assert summary["no_op_reason"] == "missing_price:NOQUOTE"
        assert summary["orders_skipped"] == [{"symbol": "NOQUOTE", "reason": "missing_price"}]
    elif not with_sell:
        assert summary["execution_status"] == "no_op"
        assert summary["no_op_reason"] == "min_notional:DEMO"
        assert summary["orders_skipped"] == []
    else:
        assert summary["orders_skipped"] == [{"symbol": "DEMO", "side": "buy", "reason": "min_notional"}]


def test_cycle_and_monitor_export_same_bound_alias_and_plan_amounts_once(routing, monkeypatch):
    monkeypatch.setenv("DIGEST_ACCOUNT_ALIAS", "canonical-demo")
    decisions = [observation(), observation("OTHER", 15.0)]
    summary = dict(execution_status="no_op", no_op_reason="min_notional:DEMO,OTHER",
        orders_submitted=[], orders_filled=[], notification_routine_decisions=decisions)
    before = deepcopy(summary)
    assert cycle(summary)[1] == []
    assert cycle(summary)[1] == []
    source = report(summary={**summary, "notification_digest": {
        "schema_version": "qsl.notification_cycle.v1", "dry_run": False, "order_count": 0, "fill_count": 0}})
    sends = []
    adapter.publish_monitor(kind="healthy_heartbeat", report=source, now=NOW,
        send=lambda: sends.append(True) or True)
    assert sends == []
    rows = next(iter(routing[1].rows.values()))[1]["runs"]
    assert len(rows) == 1
    assert rows[0]["account_alias"] == "canonical-demo"
    assert rows[0]["routine_decisions"] == decisions
    assert (rows[0]["order_count"], rows[0]["fill_count"], rows[0]["cycle_count"]) == (0, 0, 1)
    assert summary == before


@pytest.mark.parametrize("field,first,second", [
    ("display_alias", "canonical-demo", "changed-demo"),
    ("decisions", [observation()], [observation(amount=30.0)]),
])
def test_conflicting_display_observations_are_hidden_without_tainting_counts_or_state(routing, field, first, second):
    fact = event(order_count=0, fill_count=0).to_candidate()
    for value in (first, second, first):
        assert adapter.persist_candidate(fact, **{field: value})
    row = next(iter(routing[1].rows.values()))[1]["runs"][0]
    assert (row["order_count"], row["fill_count"], row["cycle_count"], row["status"]) == (0, 0, 1, "ok")
    assert ("account_alias" if field == "display_alias" else "routine_decisions") not in row


def test_alias_uses_only_an_exact_owner_bound_target_and_never_infers_from_ids(routing, monkeypatch):
    monkeypatch.setenv("DIGEST_ACCOUNT_ALIAS", "global-demo")
    monkeypatch.setenv("DIGEST_TARGET_BINDINGS_JSON", json.dumps([{
        "RUNTIME_TARGET_JSON": TARGET, "DIGEST_OPAQUE_ACCOUNT_UID": "synthetic-account",
        "DIGEST_TARGET_ID": "synthetic-target", "DIGEST_ACCOUNT_ALIAS": "bound-demo"}]))
    assert cycle(dict(execution_status="completed", orders_submitted=[]))[1] == []
    row = next(iter(routing[1].rows.values()))[1]["runs"][0]
    assert row["account_alias"] == "bound-demo"
    _, mismatch = adapter._identity({**TARGET, "service_name": "unmatched-service"})
    assert "DIGEST_ACCOUNT_ALIAS" not in mismatch
    assert account_alias({"DIGEST_OPAQUE_ACCOUNT_UID": "synthetic-account", "account_selector": "synthetic-selector"}) == ""


def test_an_exact_binding_without_alias_cannot_inherit_another_targets_global_alias(routing, monkeypatch):
    monkeypatch.setenv("DIGEST_ACCOUNT_ALIAS", "target-a-alias")
    target_b = {**TARGET, "service_name": "synthetic-service-b", "account_scope": "synthetic-scope-b"}
    monkeypatch.setenv("DIGEST_TARGET_BINDINGS_JSON", json.dumps([{
        "RUNTIME_TARGET_JSON": target_b, "DIGEST_OPAQUE_ACCOUNT_UID": "synthetic-account-b",
        "DIGEST_TARGET_ID": "synthetic-target-b"}]))
    identity, values = adapter._identity(target_b)
    assert identity["opaque_account_uid"] == "synthetic-account-b"
    assert identity["target_id"] == "synthetic-target-b"
    assert account_alias(values) == ""


def test_real_order_and_urgent_paths_remain_immediate_with_display_observations(routing, monkeypatch):
    monkeypatch.setenv("DIGEST_ACCOUNT_ALIAS", "canonical-demo")
    order = {"status": "filled", "order_id": "synthetic-order", "symbol": "AAA"}
    for summary in (dict(execution_status="completed", orders_submitted=[order]),
                    dict(execution_status="pending_reconciliation", orders_pending=[order])):
        summary["notification_routine_decisions"] = [observation()]
        assert cycle(summary)[1] == ["compact"]


def test_existing_archive_bridge_carries_the_bound_alias_and_recorded_amount_without_zeroing_unknown_fills(routing):
    payload = healthy_receipt_report()
    payload["summary"].update(no_op_reason="min_notional:DEMO", notification_routine_decisions=[observation()])
    env = {adapter.FLAG: "true", "RUNTIME_TARGET_JSON": json.dumps(TARGET),
           "DIGEST_OPAQUE_ACCOUNT_UID": "synthetic-account", "DIGEST_TARGET_ID": "synthetic-target",
           "DIGEST_STORE_TARGET": "synthetic-slot", "DIGEST_ACCOUNT_ALIAS": "canonical-demo"}
    before = deepcopy(payload)
    assert adapter.persist_archived_reports([payload], env, now=NOW)["uploaded"]
    row = next(iter(routing[1].rows.values()))[1]["runs"][0]
    assert row["account_alias"] == "canonical-demo"
    assert row["routine_decisions"] == [observation()]
    assert row["order_count"] == 0 and row["fill_count"] is None
    assert payload == before


def test_main_report_whitelist_preserves_decision_point_observations_for_the_archive_bridge(strategy_module, monkeypatch):
    monkeypatch.setenv(adapter.FLAG, "true")
    execution = dict(execution_status="no_op", no_op_reason="min_notional:DEMO",
        orders_submitted=[], orders_skipped=[], orders_filled=[], orders_pending=[],
        notification_routine_decisions=[observation()])
    before = deepcopy(execution)
    summary = strategy_module._build_cycle_report_summary(
        SimpleNamespace(result="OK - no-op", account_facts={}), execution, {}, dry_run=False)
    assert summary["notification_routine_decisions"] == [observation()]
    assert summary["notification_digest"]["order_count"] == 0
    assert summary["orders_submitted_count"] == 0
    assert execution == before


def test_legacy_reports_keep_missing_amounts_unknown_without_reconstructing_a_plan():
    assert routine_decisions({"no_op_reason": "min_notional:DEMO,OTHER"}) == [
        dict(reason_code="min_notional", observed_value=None, limit=None, currency="", symbol=s)
        for s in ("DEMO", "OTHER")]
    assert routine_decisions({"execution_status": "no_action", "equity": 1000.0}) == []
    for value in (True, math.inf, math.nan, -1, 50.0, 10**1000):
        assert minimum_notional_decision(symbol="DEMO", observed_value=value, limit=50.0, currency="USD") is None
    for raw in (None, [], "private", {"notification_routine_decisions": "private"},
                {"notification_routine_decisions": [{"reason_code": "risk_rejected"}]}):
        assert routine_decisions(raw) == []
