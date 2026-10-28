"""One original no-submit cycle; preserve calendar and original risk decisions."""
import inspect
import json
import re
import threading
from pathlib import Path

# Exact ib_insync 0.9.86 request IDs for the reviewed read/subscription methods.
# Order placement/cancellation, exercise, FA replacement and unknown IDs reject.
READ_IDS = frozenset({1, 2, 5, 6, 7, 8, 9, 16, 20, 25, 49, 59, 61, 62, 63, 64,
    71, 76, 77, 78, 87, 90, 91, 99})
CLOUD_DENIAL_KINDS = ("object_store", "document_store", "requests_http", "urllib_http")
ISOLATION_DENIAL_KINDS = ("subprocess", "network", "credential_path", "outside_path", "broker_notification")


class CycleEffectForbidden(RuntimeError):
    pass


def install_cloud_guards():
    import requests
    import urllib.request
    from quant_platform_kit.cloud.gcp_provider import GcpDocumentStore, GcpObjectStore
    counters = dict(cloud_write_attempts=0, broker_write_attempts=0, connects=0,
        signals_started=0, signals_completed=0, rebalance_started=0, rebalance_completed=0)
    counters["cloud_denials"] = dict.fromkeys(CLOUD_DENIAL_KINDS, 0)
    private_save = threading.local()
    original_request = requests.Session.request
    original_open = urllib.request.OpenerDirector.open
    def deny_cloud(kind):
        def denied(*args, **kwargs):
            counters["cloud_write_attempts"] += 1
            counters["cloud_denials"][kind] += 1
            raise CycleEffectForbidden("cloud_write_forbidden")
        return denied
    for cls, names in ((GcpObjectStore, ("write_text", "write_bytes")),
            (GcpDocumentStore, ("set", "update", "delete", "create_if_absent", "delete_if_owner"))):
        for name in names:
            setattr(cls, name, deny_cloud("object_store" if cls is GcpObjectStore else "document_store"))
    def http(self, method, url, *args, **kwargs):
        if str(method).upper() not in {"GET", "HEAD"} and not getattr(private_save, "enabled", False):
            return deny_cloud("requests_http")()
        return original_request(self, method, url, *args, **kwargs)
    def urlopen(self, fullurl, data=None, *args, **kwargs):
        method = fullurl.get_method() if isinstance(fullurl, urllib.request.Request) else "POST" if data is not None else "GET"
        if method not in {"GET", "HEAD"} and not getattr(private_save, "enabled", False):
            return deny_cloud("urllib_http")()
        return original_open(self, fullurl, data, *args, **kwargs)
    requests.Session.request = http
    urllib.request.OpenerDirector.open = urlopen
    return counters, private_save


def install_broker_guards(sdk, originals, expected, counters):
    original_connect = originals[(sdk.IB, "connect")]
    original_send = originals[(sdk.client.Client, "send")]
    def deny(*args, **kwargs):
        counters["broker_write_attempts"] += 1
        raise CycleEffectForbidden("broker_write_forbidden")
    def connect(self, *args, **kwargs):
        bound = inspect.signature(original_connect).bind(self, *args, **kwargs)
        bound.apply_defaults()
        values = bound.arguments
        if (values.get("host") != expected["host"] or values.get("port") != expected["port"]
                or values.get("clientId") != expected["client_id"] or values.get("readonly") is not True
                or type(values.get("clientId")) is not int or values["clientId"] <= 0 or counters["connects"]):
            return deny()
        counters["connects"] += 1
        return original_connect(self, *args, **kwargs)
    def send(self, *fields, **kwargs):
        if not fields or type(fields[0]) is not int or fields[0] not in READ_IDS:
            return deny()
        return original_send(self, *fields, **kwargs)
    # Restore only the original transport functions that preflight disabled.
    # Trading and what-if methods retain the rejecting preflight callbacks.
    sdk.IB.connect = connect
    sdk.client.Client.connect = originals[(sdk.client.Client, "connect")]
    sdk.client.Client.sendMsg = originals[(sdk.client.Client, "sendMsg")]
    sdk.client.Client.send = send
    for cls, names in ((sdk.IB, ("placeOrder", "cancelOrder", "reqGlobalCancel", "whatIfOrder", "whatIfOrderAsync", "exerciseOptions", "replaceFA")),
            (sdk.client.Client, ("placeOrder", "cancelOrder", "reqGlobalCancel", "exerciseOptions", "replaceFA", "reqAutoOpenOrders"))):
        for name in names:
            setattr(cls, name, deny)


def result():
    return dict(status="unavailable", stage="calendar", market_open=None, cycle_completed=False,
        broker_called=False, signals_completed=False, rebalance_completed=False,
        broker_write_attempts=0, cloud_write_attempts=0, isolated_effect_attempts=0, http_status=None, report_status=None,
        orders_previewed_count=None, orders_skipped_count=None, saved_readback_verified=False,
        cloud_denials=dict.fromkeys(CLOUD_DENIAL_KINDS, 0),
        isolation_denials=dict.fromkeys(ISOLATION_DENIAL_KINDS, 0))


def denial_counts(counters, violations):
    cloud = counters.get("cloud_denials", {})
    return (dict((kind, cloud.get(kind, 0)) for kind in CLOUD_DENIAL_KINDS),
            dict((kind, violations.count(kind)) for kind in ISOLATION_DENIAL_KINDS))


def save_private(report_path, store, private_save):
    import os
    from google.cloud import storage
    execution = os.environ.get("CLOUD_RUN_EXECUTION", "")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", execution) or not store.startswith("gs://"):
        raise CycleEffectForbidden("private_report_binding_unverified")
    location = store[5:].rstrip("/") + "/p4-no-submit/" + execution + "/report.json"
    bucket, separator, key = location.partition("/")
    if not bucket or not separator or not key:
        raise CycleEffectForbidden("private_report_binding_unverified")
    payload = report_path.read_bytes()
    blob = storage.Client().bucket(bucket).blob(key)
    private_save.enabled = True
    try:
        # The exact new object is create-only, with no SDK retry after ambiguity.
        blob.upload_from_string(payload, content_type="application/json", if_generation_match=0, retry=None, timeout=30)
        if blob.download_as_bytes(retry=None, timeout=30) != payload:
            raise CycleEffectForbidden("private_report_readback_unverified")
    finally:
        private_save.enabled = False


def run(context, counters, private_save, store):
    import os
    out = result()
    app = context["main"]
    try:
        out["market_open"] = app.is_market_open_now(calendar_name=app.MARKET_CALENDAR,
            timezone_name=app.MARKET_TIMEZONE)
        if out["market_open"] is not True:
            out.update(status="waiting_market", stage="market_closed")
            return out
        # Preserve the real calendar; the trial never adopts a force-run flag.
        os.environ["IBKR_FORCE_RUN"] = "false"
        context["network_policy"]["enabled"] = True
        install_broker_guards(context["sdk"], context["sdk_originals"], context["expected"], counters)
        for name, started, completed in (("compute_signals", "signals_started", "signals_completed"),
                ("execute_rebalance", "rebalance_started", "rebalance_completed")):
            original = getattr(app, name)
            def observed(*args, _original=original, _started=started, _completed=completed, **kwargs):
                counters[_started] += 1
                value = _original(*args, **kwargs)
                counters[_completed] += 1
                return value
            setattr(app, name, observed)
        out["stage"] = "original_dry_run"
        with app.app.test_client() as client:
            response = client.post("/dry-run")
        out["http_status"] = response.status_code
        reports = []
        for path in context["paths"]["EXECUTION_REPORT_OUTPUT_DIR"].rglob("*.json"):
            value = json.loads(path.read_text())
            if not value.get("diagnostics", {}).get("isolation_preflight"):
                reports.append((path, value))
        if len(reports) != 1:
            raise CycleEffectForbidden("cycle_report_unverified")
        path, report = reports[0]
        if report.get("dry_run") is not True or report.get("runtime_revision"):
            raise CycleEffectForbidden("cycle_report_unverified")
        report.setdefault("diagnostics", {}).update(execution_kind="cloud_run_job", no_submit_trial=True,
            cloud_run_job=os.environ.get("CLOUD_RUN_JOB"), cloud_run_execution=os.environ.get("CLOUD_RUN_EXECUTION"))
        path.write_text(json.dumps(report, sort_keys=True))
        out["report_status"] = report.get("status") if report.get("status") in {"ok", "error", "skipped", "started"} else "unavailable"
        for key in ("orders_previewed_count", "orders_skipped_count"):
            value = report.get("summary", {}).get(key)
            if type(value) is int and value >= 0:
                out[key] = value
        out["stage"] = "private_report_save"
        save_private(path, store, private_save)
        out.update(saved_readback_verified=True, broker_called=counters["connects"] > 0,
            signals_completed=counters["signals_completed"] > 0, rebalance_completed=counters["rebalance_completed"] > 0,
            broker_write_attempts=counters["broker_write_attempts"], cloud_write_attempts=counters["cloud_write_attempts"],
            isolated_effect_attempts=len(context["violations"]))
        out["cloud_denials"], out["isolation_denials"] = denial_counts(counters, context["violations"])
        complete = (response.status_code == 200 and out["report_status"] == "ok" and out["broker_called"]
            and out["signals_completed"] and out["rebalance_completed"] and not out["broker_write_attempts"]
            and not out["cloud_write_attempts"] and not out["isolated_effect_attempts"])
        out.update(status="cycle_observed" if complete else "stopped", stage="cycle_observed", cycle_completed=complete)
    except Exception as error:
        out.update(status="stopped", broker_called=counters["connects"] > 0,
            signals_completed=counters["signals_completed"] > 0, rebalance_completed=counters["rebalance_completed"] > 0,
            broker_write_attempts=counters["broker_write_attempts"], cloud_write_attempts=counters["cloud_write_attempts"],
            isolated_effect_attempts=len(context["violations"]))
        out["cloud_denials"], out["isolation_denials"] = denial_counts(counters, context["violations"])
        if isinstance(error, CycleEffectForbidden) and str(error) in {
                "private_report_binding_unverified", "private_report_readback_unverified", "cycle_report_unverified", "broker_write_forbidden", "cloud_write_forbidden"}:
            out["stage"] = str(error)
    return out
