"""Actual adapter reservations must not turn local identities into delivery."""
from copy import deepcopy
from dataclasses import replace
import hashlib
from types import SimpleNamespace

import pytest

from notifications import unified_adapter as adapter
from notifications.digest_snapshot.unified_contract import order_identity_digest
from notifications.telegram import send_telegram_message
from tests.test_unified_notification_adapter import StateStore, event

CASES = ["boolean", "boolean_false", "missing", "null", "empty", "blank", "negative_ack", "transport_ack",
         "transport_ack_wrapped", "transport_ack_path", "transport_ack_underscore",
         "transport_ack_case", "incident", "transition", "event", "order_digest",
         "incident_uri", "transition_wrapped", "event_wrapped", "transition_upper",
         "provider", "provider_numeric", "provider_hash"]


def outcome_for(case, event, marker_key, marker):
    identities = {"incident": marker_key, "transition": marker["transition_key"],
                  "event": event.to_candidate()["dedupe_key"], "order_digest": event.order_execution_digest}
    if case == "boolean":
        return True
    if case == "boolean_false":
        return False
    if case == "missing":
        return {"acknowledged": True}
    receipt = {
        "null": None, "empty": "", "blank": "  ", "negative_ack": "provider-message-unacknowledged",
        "transport_ack": "transport-ack:" + hashlib.sha256(b"arbitrary-local-transport-ack").hexdigest(),
        "transport_ack_wrapped": "sender:transport-ack:arbitrary-local-claim",
        "transport_ack_path": "transport-ack/arbitrary-local-claim",
        "transport_ack_underscore": "transport_ack:arbitrary-local-claim",
        "transport_ack_case": "  Transport-Ack:another-unrelated-value  ",
        "incident_uri": "gs://synthetic/notification-markers/" + marker_key + ".json",
        "transition_wrapped": "notification-transition:" + marker["transition_key"],
        "event_wrapped": "qsl.notification_event.v1:" + identities["event"],
        "transition_upper": marker["transition_key"].upper(),
        "provider": "independent-send-history/message-unique-21", "provider_numeric": "4711",
        "provider_hash": hashlib.sha256(b"independent-provider-returned-receipt").hexdigest(),
        **identities,
    }[case]
    return {"acknowledged": case not in {"negative_ack", "boolean_false"}, "receipt_id": receipt}


@pytest.mark.parametrize("case", CASES)
def test_actual_adapter_receipt_identity_and_double_call(case):
    store, calls, reserved = StateStore(), [], []
    source = replace(event("real_error"), order_execution_digest=order_identity_digest("synthetic-order"))
    def send():
        calls.append("send")
        key, (_, marker) = next(iter(store.backing.items()))
        assert marker["delivery_status"] == "pending" and marker["receipt_id"] is None
        reserved.append(deepcopy(marker))
        return outcome_for(case, source, key, marker)
    first = adapter.immediate_incident(source, send, store=store)
    _, marker = next(iter(store.backing.values()))
    delivered = case.startswith("provider")
    assert marker["delivery_status"] == ("delivered" if delivered else "uncertain")
    assert bool(marker["receipt_id"]) is delivered
    assert first is (case not in {"negative_ack", "boolean_false"})
    for name in ("active", "generation", "state", "transition_key"):
        assert marker[name] == reserved[0][name]
    assert adapter.immediate_incident(source, send, store=store)
    assert calls == ["send"]
    assert next(iter(store.backing.values()))[1] == marker


def test_real_sender_boolean_does_not_promote_provider_response_to_receipt():
    store, calls = StateStore(), []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True, "result": {"message_id": 4711}})
    def send():
        return send_telegram_message("synthetic alert", token="synthetic-token", chat_id="synthetic-chat",
            requests_module=SimpleNamespace(post=post), printer=lambda *args, **kwargs: None)
    source = event("real_error")
    assert adapter.immediate_incident(source, send, store=store)
    assert adapter.immediate_incident(source, send, store=store)
    marker = next(iter(store.backing.values()))[1]
    assert len(calls) == 1 and marker["delivery_status"] == "uncertain" and marker["receipt_id"] is None
    assert calls[0][1]["json"]["text"] == "synthetic alert" and calls[0][1]["timeout"] == 10
