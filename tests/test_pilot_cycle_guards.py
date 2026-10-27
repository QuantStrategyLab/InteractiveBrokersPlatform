"""Fake SDK/HTTP/storage only; no real connection, state or order."""
from pathlib import Path
import json
from types import SimpleNamespace
import urllib.request

import pytest
import requests

from scripts import pilot_app_isolation as isolation
from scripts import pilot_cycle_entry as cycle
from scripts import deploy_pilot_no_traffic as pilot


def sdk_fixture():
    calls = []
    class IB:
        def connect(self, host, port=4001, clientId=0, timeout=4, readonly=False):
            calls.append((host, port, clientId, readonly))
            return "connected"
    class Client:
        def connect(self, *args, **kwargs):
            pass
        def send(self, *args, **kwargs):
            calls.append(args)
            return "sent"
        def sendMsg(self, msg):
            pass
    sdk = SimpleNamespace(IB=IB, client=SimpleNamespace(Client=Client))
    originals = {(cls, name): getattr(cls, name) for cls, names in (
        (IB, ("connect",)), (Client, ("connect", "send", "sendMsg"))) for name in names}
    counters = dict(connects=0, broker_write_attempts=0)
    expected = dict(host="192.0.2.1", port=4001, client_id=7)
    cycle.install_broker_guards(sdk, originals, expected, counters)
    return sdk, calls, counters


def test_only_one_exact_positive_readonly_sdk_connection_is_allowed():
    sdk, calls, counters = sdk_fixture()
    assert sdk.IB().connect("192.0.2.1", 4001, clientId=7, readonly=True) == "connected"
    with pytest.raises(cycle.CycleEffectForbidden):
        sdk.IB().connect("192.0.2.1", 4001, clientId=7, readonly=True)
    assert calls == [("192.0.2.1", 4001, 7, True)] and counters["connects"] == 1


@pytest.mark.parametrize("host,client,readonly", [("192.0.2.2", 7, True), ("192.0.2.1", 0, True),
    ("192.0.2.1", 7, False), ("192.0.2.1", 8, True)])
def test_other_binding_or_writable_connection_never_reaches_sdk(host, client, readonly):
    sdk, calls, counters = sdk_fixture()
    with pytest.raises(cycle.CycleEffectForbidden):
        sdk.IB().connect(host, 4001, clientId=client, readonly=readonly)
    assert not calls and counters["connects"] == 0


@pytest.mark.parametrize("message", [3, 4, 15, 19, 21, 58, 999, "1"])
def test_trade_binding_configuration_and_unknown_protocol_ids_reject(message):
    sdk, calls, counters = sdk_fixture()
    with pytest.raises(cycle.CycleEffectForbidden):
        sdk.client.Client().send(message, "synthetic")
    assert not calls and counters["broker_write_attempts"] == 1


def test_reviewed_read_request_and_cancellation_of_data_subscription_are_allowed():
    sdk, calls, counters = sdk_fixture()
    assert sdk.client.Client().send(1, "synthetic") == "sent"
    assert sdk.client.Client().send(2, "synthetic") == "sent"
    with pytest.raises(cycle.CycleEffectForbidden):
        sdk.IB().placeOrder(None, None)
    assert calls == [(1, "synthetic"), (2, "synthetic")]
    assert counters["broker_write_attempts"] == 1


def test_cloud_guards_reject_application_writes_and_allow_only_controlled_thread_save(monkeypatch):
    from quant_platform_kit.cloud.gcp_provider import GcpDocumentStore, GcpObjectStore
    calls = []
    monkeypatch.setattr(requests.Session, "request", lambda self, method, url, *a, **k: calls.append(method) or "read")
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", urllib.request.OpenerDirector.open)
    for cls, names in ((GcpObjectStore, ("write_text", "write_bytes")),
            (GcpDocumentStore, ("set", "update", "delete", "create_if_absent", "delete_if_owner"))):
        for name in names:
            monkeypatch.setattr(cls, name, getattr(cls, name))
    counters, allowed = cycle.install_cloud_guards()
    session = requests.Session()
    assert session.request("GET", "https://synthetic.invalid") == "read"
    with pytest.raises(cycle.CycleEffectForbidden):
        session.request("POST", "https://synthetic.invalid")
    with pytest.raises(cycle.CycleEffectForbidden):
        GcpDocumentStore().set("synthetic", "synthetic", {})
    with pytest.raises(cycle.CycleEffectForbidden):
        GcpObjectStore().write_text("gs://synthetic/forbidden", "synthetic")
    assert calls == ["GET"] and counters["cloud_write_attempts"] == 3
    allowed.enabled = True
    assert session.request("POST", "https://synthetic.invalid") == "read"
    import threading
    blocked = []
    def other_thread():
        try:
            session.request("POST", "https://synthetic.invalid")
        except cycle.CycleEffectForbidden:
            blocked.append(True)
    thread = threading.Thread(target=other_thread)
    thread.start()
    thread.join()
    assert blocked == [True] and calls == ["GET", "POST"]


def test_real_closed_calendar_stops_before_enabling_network_or_sdk(monkeypatch):
    monkeypatch.setenv("IBKR_FORCE_RUN", "true")
    context = dict(main=SimpleNamespace(is_market_open_now=lambda **kwargs: False,
        MARKET_CALENDAR="NYSE", MARKET_TIMEZONE="America/New_York"), network_policy=dict(enabled=False), violations=[])
    counters = dict(connects=0, signals_completed=0, rebalance_completed=0, broker_write_attempts=0, cloud_write_attempts=0)
    value = cycle.run(context, counters, SimpleNamespace(), "gs://synthetic/reports")
    assert value["status"] == "waiting_market" and value["stage"] == "market_closed"
    assert value["market_open"] is False and not value["cycle_completed"]
    assert not context["network_policy"]["enabled"] and not value["broker_called"]


def test_cycle_schema_cannot_claim_completion_from_http_alone():
    app = dict(schema_version="ibkr.application_isolation.v1", stage="isolation_observed", isolation_passed=True,
        candidate_source_verified=True, dependencies_verified=True, application_imported=True, binding_verified=True,
        outputs_isolated=True, report_saved_readback=True, sdk_guards_installed=True, notification_guards_installed=True,
        external_effect_attempts=0, broker_called=False, strategy_computed=False, production_state_read=False)
    value = dict(schema_version="ibkr.guarded_cycle.v1", isolation=app, cycle=cycle.result())
    value["cycle"].update(cycle_completed=True, http_status=200, report_status="ok")
    with pytest.raises(pilot.ReleaseError, match="cycle_response_invalid"):
        isolation.cycle_measurement([{"jsonPayload": value}])
    value["cycle"] = cycle.result()
    value["cycle"].update(status="waiting_market", stage="market_closed", market_open=False)
    assert isolation.cycle_measurement([{"jsonPayload": value}]) == value
    value["cycle"]["private_error"] = "must never be emitted"
    with pytest.raises(pilot.ReleaseError, match="cycle_response_invalid"):
        isolation.cycle_measurement([{"jsonPayload": value}])


def test_cycle_bundle_has_only_one_reviewed_operator_module():
    import io
    import gzip
    import tarfile
    from tests.test_pilot_app_isolation import archive
    data, digest = isolation.make_bundle(archive([("uv.lock", b"synthetic")]), cycle=True)
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(data))) as tar:
        names = tar.getnames()
        assert names == ["uv.lock", "_p4_operator_cycle.py"]
        assert tar.extractfile("_p4_operator_cycle.py").read() == Path(cycle.__file__).read_bytes()
    assert len(digest) == 64


def test_complete_fake_cycle_requires_phase_evidence_and_private_readback(monkeypatch, tmp_path):
    monkeypatch.setenv("IBKR_FORCE_RUN", "true")
    class IB:
        def connect(self, host, port, *, clientId, readonly):
            return "connected"
    class Client:
        def connect(self):
            pass
        def send(self, *args):
            pass
        def sendMsg(self, *args):
            pass
    sdk = SimpleNamespace(IB=IB, client=SimpleNamespace(Client=Client))
    originals = {(cls, name): getattr(cls, name) for cls, names in ((IB, ("connect",)), (Client, ("connect", "send", "sendMsg"))) for name in names}
    counters = dict(connects=0, signals_started=0, signals_completed=0, rebalance_started=0,
        rebalance_completed=0, broker_write_attempts=0, cloud_write_attempts=0)
    app = SimpleNamespace(is_market_open_now=lambda **kwargs: True, MARKET_CALENDAR="NYSE", MARKET_TIMEZONE="America/New_York",
        compute_signals=lambda: "synthetic signals", execute_rebalance=lambda: "synthetic preview")
    class ClientContext:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, route):
            assert route == "/dry-run"
            sdk.IB().connect("192.0.2.1", 4001, clientId=7, readonly=True)
            app.compute_signals()
            app.execute_rebalance()
            (tmp_path / "report.json").write_text(json.dumps(dict(dry_run=True, status="ok", diagnostics={}, summary={"orders_previewed_count": 2})))
            return SimpleNamespace(status_code=200)
    app.app = SimpleNamespace(test_client=ClientContext)
    saved = []
    monkeypatch.setattr(cycle, "save_private", lambda path, store, thread: saved.append(json.loads(path.read_text())))
    context = dict(main=app, sdk=sdk, sdk_originals=originals, expected=dict(host="192.0.2.1", port=4001, client_id=7),
        paths={"EXECUTION_REPORT_OUTPUT_DIR": tmp_path}, network_policy=dict(enabled=False), violations=[])
    value = cycle.run(context, counters, SimpleNamespace(), "gs://synthetic/reports")
    assert value["cycle_completed"] and value["saved_readback_verified"]
    assert value["signals_completed"] and value["rebalance_completed"] and value["broker_called"]
    assert value["orders_previewed_count"] == 2 and value["orders_skipped_count"] is None
    assert saved[0]["diagnostics"]["no_submit_trial"] is True
    assert value["broker_write_attempts"] == value["cloud_write_attempts"] == value["isolated_effect_attempts"] == 0
