"""Synthetic transport and private storage only; no broker/cloud connection."""
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest
from google.api_core.exceptions import PreconditionFailed
from quant_platform_kit.cloud.gcp_provider import GcpObjectStore

from scripts import capture_web_portfolio_ledger as capture
from scripts.deploy_pilot_no_traffic import ReleaseError

PRIVATE = "synthetic-private-must-not-be-logged"
NOW = "2026-10-26T12:00:00+00:00"


@pytest.fixture
def chain():
    request = {"schema_version": "ibkr.web_ledger_capture_request.v1", "account_id": "SYNTH123",
               "currency": "USD", "gcp_project_id": "synthetic-project",
               "report_prefix": "gs://synthetic-private/reports/tqqq",
               "capture_id": "1f72542a-599c-4d46-a288-ac3d083d80ef", "binding_source_sha256": "a" * 64}
    ledger = {"USD": {"acctcode": "SYNTH123", "currency": "USD", "cashbalance": 123,
                      "settledcash": 100, "timestamp": 1793016000, "sessionid": PRIVATE},
              "BASE": {"currency": "BASE", "settledcash": 999}}
    state = {"request": request, "accounts": [{"accountId": "SYNTH123", "id": "SYNTH123", "displayName": PRIVATE}],
             "ledger": ledger, "calls": [], "store_calls": [], "uploads": [], "reads": [],
             "objects": {}, "upload_error": None, "readback_mismatch": False, "get_error": None,
             "out": capture.result(), "accounts_raw": None, "ledger_raw": None}

    class Portal:
        def get(self, path):
            state["calls"].append(path)
            if state["get_error"]:
                raise state["get_error"]
            key = "accounts" if path == "/portfolio/accounts" else "ledger"
            return state[key + "_raw"] or json.dumps(state[key]).encode()

    class Blob:
        def __init__(self, uri):
            self.uri = uri

        def upload_from_string(self, payload, **kwargs):
            state["uploads"].append((payload, kwargs))
            assert kwargs == {"content_type": "application/json", "if_generation_match": 0, "retry": None, "timeout": 60}
            if state["upload_error"]:
                raise state["upload_error"]
            if self.uri in state["objects"]:
                raise PreconditionFailed(PRIVATE)
            state["objects"][self.uri] = payload

        def download_as_bytes(self, **kwargs):
            state["reads"].append(kwargs)
            assert kwargs == {"retry": None, "timeout": 60}
            return b"different" if state["readback_mismatch"] else state["objects"][self.uri]

    store = GcpObjectStore(project_id="synthetic-project")
    store._client = SimpleNamespace(bucket=lambda bucket: SimpleNamespace(blob=lambda path: Blob(f"gs://{bucket}/{path}")))

    def get_store(project):
        state["store_calls"].append(project)
        return store

    state["run"] = lambda: capture.capture(request, Portal(), get_store=get_store, out=state["out"], now=lambda: NOW)
    return state


def test_account_then_ledger_create_readback_and_minimal_projection(chain):
    before = copy.deepcopy((chain["request"], chain["accounts"], chain["ledger"]))
    out = chain["run"]()
    assert chain["calls"] == ["/portfolio/accounts", "/portfolio/SYNTH123/ledger"]
    assert len(chain["uploads"]) == len(chain["reads"]) == 1
    saved = json.loads(chain["uploads"][0][0])
    assert saved["synthetic"] is False
    assert saved["selected_fields"] == {"acctcode": "SYNTH123", "currency": "USD", "cashbalance": "123",
                                        "settledcash": "100", "timestamp": 1793016000}
    assert saved["binding_source_sha256"] == "a" * 64
    assert PRIVATE not in chain["uploads"][0][0].decode()
    assert "BASE" not in chain["uploads"][0][0].decode()
    assert out["status"] == "observed" and out["readback_verified"] and out["account_matched"]
    assert out["original_brokerage_session_preserved"] is None
    assert not out["spendable_cash_verified"] and not out["complete_cash_obligations_verified"] and not out["execution_authorized"]
    assert PRIVATE not in json.dumps(out) and "SYNTH123" not in json.dumps(out)
    assert before == (chain["request"], chain["accounts"], chain["ledger"])


@pytest.mark.parametrize("accounts", [[], [{"accountId": "OTHER"}], {}, [None],
    [{"accountId": "SYNTH123", "id": "OTHER"}], [{"accountId": "SYNTH123"}] * 2])
def test_account_mismatch_stops_before_ledger_and_storage(chain, accounts):
    chain["accounts"] = accounts
    with pytest.raises(ReleaseError):
        chain["run"]()
    assert chain["calls"] == ["/portfolio/accounts"]
    assert not chain["store_calls"]


@pytest.mark.parametrize("field,value", [("settledcash", None), ("settledcash", True), ("cashbalance", "100"),
    ("settledcash", float("nan")), ("settledcash", float("inf")), ("timestamp", True),
    ("timestamp", 1793016000000), ("timestamp", 1793016000.0), ("acctcode", "OTHER"), ("currency", "EUR")])
def test_ledger_invalid_fields_stop_before_storage(chain, field, value):
    chain["ledger"]["USD"][field] = value
    with pytest.raises(ReleaseError):
        chain["run"]()
    assert not chain["store_calls"]


@pytest.mark.parametrize("field", capture.FIELDS)
def test_missing_is_not_zero(chain, field):
    del chain["ledger"]["USD"][field]
    with pytest.raises(ReleaseError, match="ledger_fields_missing"):
        chain["run"]()
    assert not chain["uploads"]


def test_currency_not_substituted_from_base(chain):
    del chain["ledger"]["USD"]
    with pytest.raises(ReleaseError, match="ledger_currency_missing"):
        chain["run"]()
    assert not chain["uploads"]


def test_precision_negatives_and_zero_are_observations(chain):
    chain["ledger_raw"] = b'{"USD":{"acctcode":"SYNTH123","currency":"USD","cashbalance":-0.123456789123456789,"settledcash":0,"timestamp":1793016000}}'
    chain["run"]()
    row = json.loads(chain["uploads"][0][0])["selected_fields"]
    assert row["cashbalance"] == "-0.123456789123456789" and row["settledcash"] == "0"
    assert chain["out"]["spendable_cash_verified"] is False


@pytest.mark.parametrize("raw", [b'{"USD":{},"USD":{}}', b"<html>" + PRIVATE.encode(), b"x" * (capture.MAX_BYTES + 1)])
def test_ambiguous_json_or_login_page_not_saved(chain, raw):
    chain["ledger_raw"] = raw
    with pytest.raises(ReleaseError, match="invalid_json"):
        chain["run"]()
    assert not chain["store_calls"]


def test_unknown_upload_is_not_retried_or_read_back(chain):
    chain["upload_error"] = TimeoutError(PRIVATE)
    with pytest.raises(ReleaseError, match="write_unknown"):
        chain["run"]()
    assert len(chain["uploads"]) == 1 and not chain["reads"]
    assert chain["out"]["object_create_attempted"] and not chain["out"]["readback_verified"]


def test_existing_different_observation_not_overwritten(chain):
    uri = capture.validate_request(chain["request"])
    chain["objects"][uri] = b"original"
    with pytest.raises(ReleaseError, match="readback_mismatch"):
        chain["run"]()
    assert chain["objects"][uri] == b"original" and len(chain["uploads"]) == 1


def test_readback_mismatch_is_not_success(chain):
    chain["readback_mismatch"] = True
    with pytest.raises(ReleaseError, match="readback_mismatch"):
        chain["run"]()
    assert chain["out"]["object_create_completed"] and not chain["out"]["readback_verified"]


@pytest.mark.parametrize("key,value", [("account_id", "../OTHER"), ("account_id", "SYNTH123?x=y"),
    ("currency", "BASE"), ("capture_id", "../file"), ("binding_source_sha256", ""),
    ("report_prefix", "https://example.com"), ("report_prefix", "gs://private/reports/../other"),
    ("report_prefix", "gs://user:password@private/reports"), ("report_prefix", "gs://private/reports?token=secret")])
def test_invalid_request_makes_no_io(chain, key, value):
    chain["request"][key] = value
    with pytest.raises(ReleaseError, match="request_invalid"):
        chain["run"]()
    assert not chain["calls"] and not chain["store_calls"]


def test_private_binding_file_permissions_and_symlinks(chain, tmp_path):
    path = tmp_path / "request.json"
    path.write_text(json.dumps(chain["request"]))
    path.chmod(0o600)
    assert capture.read_request(path) == chain["request"]
    path.chmod(0o640)
    with pytest.raises(ReleaseError, match="request_permissions_invalid"):
        capture.read_request(path)
    path.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        capture.read_request(link)
    assert os.stat(path).st_uid == os.geteuid()


def test_cli_on_mac_does_not_read_private_file_or_authenticate(monkeypatch, capsys):
    monkeypatch.setattr(capture.sys, "platform", "darwin")
    monkeypatch.setattr(capture, "read_request", lambda *_: pytest.fail("must not read private binding on Mac"))
    assert capture.main(["--request-file", PRIVATE, "--ca-file", PRIVATE]) == 1
    stdout = capsys.readouterr().out
    assert PRIVATE not in stdout and json.loads(stdout)["reason"] == "protected_linux_required"


def test_cli_never_outputs_unexpected_error_details(monkeypatch, capsys):
    monkeypatch.setattr(capture.sys, "platform", "linux")
    monkeypatch.setattr(capture, "read_request", lambda *_: (_ for _ in ()).throw(OSError(PRIVATE)))
    assert capture.main(["--request-file", PRIVATE, "--ca-file", PRIVATE]) == 1
    assert PRIVATE not in capsys.readouterr().out


def test_transport_disables_proxies_keeps_tls_and_get_only(monkeypatch):
    captured = []
    def opener(*handlers):
        captured.extend(handlers)
        return SimpleNamespace(open=lambda *_args, **_kwargs: pytest.fail("no network in this test"))
    monkeypatch.setattr(capture, "build_opener", opener)
    portal = capture.LocalPortal(None, "SYNTH123")
    assert captured[0].proxies == {}
    assert captured[1]._context.check_hostname and captured[1]._context.verify_mode == capture.ssl.CERT_REQUIRED
    with pytest.raises(ReleaseError, match="endpoint_not_allowed"):
        portal.get("/iserver/auth/ssodh/init")
    with pytest.raises(ReleaseError, match="http_redirect_blocked"):
        captured[2].redirect_request(None, None, 302, None, None, "https://example.com")


@pytest.mark.parametrize("error,reason", [(HTTPError(PRIVATE, 401, PRIVATE, {}, None), "read_only_session_unavailable"),
    (HTTPError(PRIVATE, 403, PRIVATE, {}, None), "read_only_session_unavailable"),
    (HTTPError(PRIVATE, 500, PRIVATE, {}, None), "http_read_failed"), (URLError(PRIVATE), "http_read_failed")])
def test_transport_auth_errors_sanitized_no_retry(error, reason):
    portal = capture.LocalPortal(None, "SYNTH123")
    calls = []
    def fail(request, **kwargs):
        calls.append(request)
        assert request.get_method() == "GET" and not request.has_header("Authorization") and request.data is None
        assert kwargs == {"timeout": 30}
        raise error
    portal.opener = SimpleNamespace(open=fail)
    with pytest.raises(ReleaseError, match=reason) as caught:
        portal.get("/portfolio/accounts")
    assert PRIVATE not in str(caught.value) and len(calls) == 1


def test_no_runtime_registration():
    root = Path(__file__).resolve().parents[1]
    assert "capture_web_portfolio_ledger" not in (root / "main.py").read_text()
