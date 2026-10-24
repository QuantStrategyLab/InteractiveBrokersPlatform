import json
import hashlib
import os
from datetime import datetime, timezone
from urllib.error import HTTPError

import pytest

from scripts import publish_ibkr_flex_period_return as cli
from application.ibkr_flex_source import (
    FlexReportPending, import_activity_flex_ledger_from_request,
)


def configure(monkeypatch):
    values = {
        "IBKR_PERIOD_RETURN_PUBLISH_ENABLED": "true", "IBKR_PERIOD_RETURN_TARGET_ID": "synthetic-target",
        "IBKR_PERIOD_RETURN_SOURCE_BINDING_ID": "a" * 64, "IBKR_PERIOD_RETURN_ACCOUNT_SCOPE": "synthetic-live",
        "IBKR_PERIOD_RETURN_ACCOUNT_KEY": "synthetic-account-key", "IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON": '["DU0000000"]',
        "IBKR_FLEX_TOKEN": "synthetic-flex-token", "IBKR_FLEX_QUERY_ID": "12345",
        "IBKR_ACCOUNT_FACTS_SYNC_TOKEN": "synthetic-sync-token",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    for name in cli._TOKEN_ALIASES:
        monkeypatch.delenv(name, raising=False)
    return values


class Response:
    status = status_code = 200

    def __init__(self, content):
        self.content = content
        self.limits = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def iter_content(self, **_kwargs):
        yield self.content

    def read(self, limit):
        self.limits.append(limit)
        return self.content[:limit]


def xml(*, twr="1.25", account="DU0000000", currency="EUR"):
    native = f'<ChangeInNAV fromDate="20260101" toDate="20260131" twr="{twr}"/>' if twr is not None else ""
    return (f'<FlexQueryResponse type="AF"><FlexStatements><FlexStatement accountId="{account}" '
            f'fromDate="20260101" toDate="20260131"><AccountInformation currency="{currency}"/>'
            f'{native}</FlexStatement></FlexStatements></FlexQueryResponse>').encode()


class FlexSession:
    def __init__(self, report):
        self.report = report
        self.calls = []
        self.closed = False
        self.trust_env = True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        assert self.trust_env is False
        assert kwargs["allow_redirects"] is False
        if url.endswith("/SendRequest"):
            return Response(b'<FlexStatementResponse><Status>Success</Status><ReferenceCode>98765</ReferenceCode></FlexStatementResponse>')
        return Response(self.report)


class Opener:
    def __init__(self, *, patch=None, error=None, raw=None):
        self.calls = []
        self.patch, self.error, self.raw = patch, error, raw

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        payload = json.loads(request.data)
        ack = {"ok": True, "stored": True, "unchanged": False, "account_key": "synthetic-account-key",
               "period": payload["period"], "currency": payload["currency"], "method": payload["method"]}
        ack.update(self.patch or {})
        self.response = Response(self.raw if self.raw is not None else json.dumps(ack).encode())
        return self.response


def test_full_synthetic_flex_fetch_import_projection_one_post_ack(monkeypatch, capsys):
    configure(monkeypatch)
    session, opener = FlexSession(xml()), Opener()
    monkeypatch.setattr(cli.requests, "Session", lambda: session)
    monkeypatch.setattr(cli, "build_opener", lambda *_args: opener)
    assert cli.main() == 0
    assert capsys.readouterr().out == '{"status": "published"}\n'
    assert len(session.calls) == 2 and session.closed
    assert session.calls[0][1]["params"] == {"t": "synthetic-flex-token", "q": "12345", "v": "3"}
    assert len(opener.calls) == 1
    request, timeout = opener.calls[0]
    assert request.full_url == cli.SYNC_URL and request.get_method() == "POST" and timeout == 15
    assert request.get_header("Authorization") == "Bearer synthetic-sync-token"
    payload = json.loads(request.data)
    assert payload["currency"] == "EUR" and payload["value"] == "0.0125" and payload["source_value"] == "1.25"
    assert payload["account_ids"] == ["DU0000000"]
    assert opener.response.limits == [8193]


@pytest.mark.parametrize("bad_name,bad_value", [
    ("IBKR_PERIOD_RETURN_PUBLISH_ENABLED", "false"), ("IBKR_FLEX_TOKEN", ""),
    ("IBKR_FLEX_QUERY_ID", "invalid"), ("IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON", '["DU0000000","DU0000004"]'),
    ("IBKR_PERIOD_RETURN_SOURCE_BINDING_ID", "bad"), ("IBKR_PERIOD_RETURN_ACCOUNT_KEY", ""),
    ("IBKR_ACCOUNT_FACTS_SYNC_TOKEN", "bad\n"), ("EXECUTION_EVIDENCE_SYNC_TOKEN", "synthetic-sync-token"),
    ("IBKR_ACCOUNT_FACTS_SYNC_TOKEN", "synthetic-flex-token"),
    ("CYCLE_HEALTH_PROVISIONER_TOKEN", "synthetic-sync-token"),
])
def test_disabled_or_invalid_config_has_zero_flex_and_post(monkeypatch, capsys, bad_name, bad_value):
    configure(monkeypatch)
    monkeypatch.setenv(bad_name, bad_value)
    def forbidden(*_args, **_kwargs):
        pytest.fail("external capability used before complete configuration")
    monkeypatch.setattr(cli.requests, "Session", forbidden)
    monkeypatch.setattr(cli, "build_opener", forbidden)
    assert cli.main() == 2
    assert json.loads(capsys.readouterr().out)["status"] in {"disabled", "configuration_incomplete"}


def test_enable_flag_absent_is_disabled_without_any_http(monkeypatch, capsys):
    configure(monkeypatch)
    monkeypatch.delenv("IBKR_PERIOD_RETURN_PUBLISH_ENABLED")
    def forbidden(*_args, **_kwargs):
        pytest.fail("default-off publisher used an external capability")
    monkeypatch.setattr(cli.requests, "Session", forbidden)
    monkeypatch.setattr(cli, "build_opener", forbidden)
    assert cli.main() == 2
    assert capsys.readouterr().out == '{"status": "disabled"}\n'


def test_flex_failure_has_safe_stdout_and_zero_post(monkeypatch, capsys):
    configure(monkeypatch)
    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic-private-token/DU0000000/1.25")
    monkeypatch.setattr(cli.requests, "Session", fail)
    monkeypatch.setattr(cli, "build_opener", lambda *_args: pytest.fail("failed source posted"))
    assert cli.main() == 1
    output = capsys.readouterr().out
    assert json.loads(output) == {"status": "flex_import_failed", "source_failure_kind": "runtime", "exception_type": "RuntimeError"}
    assert "synthetic-private" not in output and "DU0000000" not in output


def test_generation_pending_has_zero_post_and_no_replacement_request(monkeypatch, capsys):
    import time

    configure(monkeypatch)
    pending_xml = b"<FlexStatementResponse><Status>Fail</Status><ErrorCode>1019</ErrorCode><ErrorMessage>synthetic-private-token</ErrorMessage></FlexStatementResponse>"
    session = FlexSession(pending_xml)
    monkeypatch.setattr(cli.requests, "Session", lambda: session)
    monkeypatch.setattr(time, "sleep", lambda _delay: None)
    monkeypatch.setattr(cli, "build_opener", lambda *_args: pytest.fail("pending source posted"))
    assert cli.main() == 1
    assert capsys.readouterr().out == '{"status": "flex_pending"}\n'
    assert len(session.calls) == 7 and session.closed
    assert sum(url.endswith("/SendRequest") for url, _ in session.calls) == 1
    assert all(kw["params"]["q"] == "98765" for _, kw in session.calls[1:])


def test_pending_generation_completes_in_same_run_without_second_generation(monkeypatch, capsys):
    import time

    configure(monkeypatch)
    session = FlexSession(b"<FlexStatementResponse><ErrorCode>1019</ErrorCode></FlexStatementResponse>")
    monkeypatch.setattr(cli.requests, "Session", lambda: session)
    waits = []
    def generated(delay):
        waits.append(delay)
        session.report = xml()
    monkeypatch.setattr(time, "sleep", generated)
    opener = Opener()
    monkeypatch.setattr(cli, "build_opener", lambda *_args: opener)
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"status": "published"}
    assert waits == [3]
    assert len(opener.calls) == 1
    assert sum(url.endswith("/SendRequest") for url, _ in session.calls) == 1
    statements = [kw["params"] for url, kw in session.calls if url.endswith("/GetStatement")]
    assert len(statements) == 2 and statements[0] == statements[1]


def test_nonpending_error_after_generation_wait_is_not_retried(monkeypatch, capsys):
    import time

    configure(monkeypatch)
    session = FlexSession(b"<FlexStatementResponse><ErrorCode>1019</ErrorCode></FlexStatementResponse>")
    monkeypatch.setattr(cli.requests, "Session", lambda: session)
    def rejected(_delay):
        session.report = b"<FlexStatementResponse><ErrorCode>1012</ErrorCode></FlexStatementResponse>"
    monkeypatch.setattr(time, "sleep", rejected)
    monkeypatch.setattr(cli, "build_opener", lambda *_args: pytest.fail("failed report must not publish"))
    assert cli.main() == 1
    assert json.loads(capsys.readouterr().out) == {"status": "flex_import_failed", "source_endpoint": "GetStatement",
                                                "source_failure_kind": "provider", "provider_error_code": "1012"}
    assert len(session.calls) == 3


def test_explicit_same_reference_collection_to_projection_and_single_ack(monkeypatch, capsys):
    config = cli._config(configure(monkeypatch))
    session = FlexSession(b"<FlexStatementResponse><ErrorCode>1019</ErrorCode></FlexStatementResponse>")
    session.trust_env = False
    with pytest.raises(FlexReportPending) as caught:
        cli.import_activity_flex_ledger(token=config["flex_token"], query_id=config["query_id"],
            expected_account_ids=tuple(config["expected_account_ids"]), session=session)
    request = caught.value.request
    assert len(session.calls) == 2  # No automatic second collection or generation.
    session.report = xml()  # An explicit authorized caller resumes in this process.
    ledger = import_activity_flex_ledger_from_request(request,
        expected_account_ids=tuple(config["expected_account_ids"]), session=session)
    payload = cli.build_ibkr_period_return(ledger, observed_at=datetime.now(timezone.utc),
        **{key: config[key] for key in ("target_id", "source_binding_id", "account_scope", "expected_account_ids")})
    opener = Opener()
    monkeypatch.setattr(cli, "build_opener", lambda *_args: opener)
    assert cli.publish_period_return(payload, sync_token=config["sync_token"], expected_account_key=config["account_key"]) == "published"
    assert len(opener.calls) == 1 and len(session.calls) == 3
    assert sum(url.endswith("/SendRequest") for url, _ in session.calls) == 1
    assert session.calls[1][1]["params"] == session.calls[2][1]["params"]
    assert capsys.readouterr().out == ""


def test_report_scope_preflight_uses_only_existing_protected_account_configuration(monkeypatch, capsys):
    from tests.test_diagnose_account_facts_identity import _environment

    configure(monkeypatch)
    settings, additional = _environment(monkeypatch)
    monkeypatch.setenv("IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON", settings["IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON"])
    config = cli._config(os.environ)
    approved = set(config["allowed_report_account_ids"])
    assert approved == set(json.loads(settings["IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON"])) | {
        row["account_selector"][0] for row in additional.values()}
    assert cli.main(("--preflight",)) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["approved_report_account_count"] == len(approved)
    assert not any(account in output for account in approved)


def test_partial_protected_report_scope_fails_before_broker_access(monkeypatch, capsys):
    configure(monkeypatch)
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON", '["DU0000000"]')
    monkeypatch.delenv("IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON", raising=False)
    monkeypatch.setattr(cli.requests, "Session", lambda: pytest.fail("invalid scope reached broker"))
    assert cli.main() == 2
    assert json.loads(capsys.readouterr().out)["status"] == "configuration_incomplete"


@pytest.mark.parametrize("pending", [False, True])
def test_approved_multiaccount_source_publishes_only_target_and_keeps_one_request(monkeypatch, capsys, pending):
    from tests.test_diagnose_account_facts_identity import _environment
    from xml.etree import ElementTree
    import time

    configure(monkeypatch)
    settings, additional = _environment(monkeypatch)
    expected = json.loads(settings["IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON"])
    monkeypatch.setenv("IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON", json.dumps(expected))
    root = ElementTree.fromstring(xml(account=expected[0]))
    other = next(iter(additional.values()))["account_selector"][0]
    root.find("FlexStatements").insert(0, ElementTree.fromstring(xml(account=other, twr="80")).find("./FlexStatements/FlexStatement"))
    report = ElementTree.tostring(root)
    session = FlexSession(b"<FlexStatementResponse><ErrorCode>1019</ErrorCode></FlexStatementResponse>" if pending else report)
    monkeypatch.setattr(cli.requests, "Session", lambda: session)
    monkeypatch.setattr(time, "sleep", lambda _delay: setattr(session, "report", report))
    opener = Opener()
    monkeypatch.setattr(cli, "build_opener", lambda *_args: opener)
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"status": "published"}
    assert len(opener.calls) == 1
    payload = json.loads(opener.calls[0][0].data)
    assert payload["account_ids"] == expected and payload["value"] == "0.0125"
    assert sum(url.endswith("/SendRequest") for url, _ in session.calls) == 1


@pytest.mark.parametrize("report", [xml(twr=None), xml(account="DU0000004"), xml(currency="")])
def test_no_native_or_wrong_scope_or_currency_has_zero_post(monkeypatch, capsys, report):
    configure(monkeypatch)
    monkeypatch.setattr(cli.requests, "Session", lambda: FlexSession(report))
    monkeypatch.setattr(cli, "build_opener", lambda *_args: pytest.fail("unqualified source posted"))
    assert cli.main() == 1
    output = capsys.readouterr().out
    assert json.loads(output)["status"] in {"native_twr_unavailable", "flex_import_failed", "projection_rejected"}
    assert "DU" not in output and "1.25" not in output


@pytest.mark.parametrize("patch,raw", [
    ({"account_key": "other-synthetic"}, None), ({"stored": False}, None),
    ({"period": {"from": "2025-01-01", "to": "2025-01-31"}}, None), ({"currency": "USD"}, None),
    ({"method": "local_twr"}, None), ({"unchanged": "true"}, None), ({"account_ids": ["DU0000000"]}, None),
    ({}, b'not-json'), ({}, b'{}' + b' ' * 8192), ({}, b'{"ok":true,"ok":false}'),
])
def test_invalid_ack_never_claims_success_or_retries(monkeypatch, capsys, patch, raw):
    configure(monkeypatch)
    monkeypatch.setattr(cli.requests, "Session", lambda: FlexSession(xml()))
    opener = Opener(patch=patch, raw=raw)
    monkeypatch.setattr(cli, "build_opener", lambda *_args: opener)
    assert cli.main() == 1
    assert capsys.readouterr().out == '{"status": "sync_unknown"}\n'
    assert len(opener.calls) == 1


@pytest.mark.parametrize("error,status", [
    (TimeoutError("synthetic-private-token/value"), "sync_unknown"),
    (HTTPError(cli.SYNC_URL, 302, "private", {}, None), "sync_rejected"),
    (HTTPError(cli.SYNC_URL, 403, "private", {}, None), "sync_rejected"),
    (HTTPError(cli.SYNC_URL, 503, "private", {}, None), "sync_unknown"),
])
def test_transport_failures_are_fixed_one_attempt(monkeypatch, capsys, error, status):
    configure(monkeypatch)
    monkeypatch.setattr(cli.requests, "Session", lambda: FlexSession(xml()))
    opener = Opener(error=error)
    monkeypatch.setattr(cli, "build_opener", lambda *_args: opener)
    assert cli.main() == 1
    assert capsys.readouterr().out == json.dumps({"status": status}) + "\n"
    assert len(opener.calls) == 1


def test_unchanged_ack_is_success_without_second_post(monkeypatch, capsys):
    configure(monkeypatch)
    monkeypatch.setattr(cli.requests, "Session", lambda: FlexSession(xml()))
    opener = Opener(patch={"unchanged": True})
    monkeypatch.setattr(cli, "build_opener", lambda *_args: opener)
    assert cli.main() == 0
    assert capsys.readouterr().out == '{"status": "unchanged"}\n'
    assert len(opener.calls) == 1


_PREFLIGHT_FIELDS = (
    "IBKR_PERIOD_RETURN_TARGET_ID", "IBKR_PERIOD_RETURN_SOURCE_BINDING_ID",
    "IBKR_PERIOD_RETURN_ACCOUNT_SCOPE", "IBKR_PERIOD_RETURN_ACCOUNT_KEY",
    "IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON", "IBKR_FLEX_TOKEN", "IBKR_FLEX_QUERY_ID",
    "IBKR_ACCOUNT_FACTS_SYNC_TOKEN",
)
_PREFLIGHT_SOURCE_SHA = "e2ec696a9ba3ec64156804a701a556ad69d5b64b"


def _forbid_preflight_io(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("configuration-only preflight used an external capability")
    for name in ("import_activity_flex_ledger", "publish_period_return", "build_opener", "Request"):
        monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr(cli.requests, "Session", forbidden)


@pytest.mark.parametrize("enabled", [None, "false", "true"])
def test_preflight_reuses_original_config_without_flex_or_publish_even_when_enabled(monkeypatch, capsys, enabled):
    values = configure(monkeypatch)
    if enabled is None:
        monkeypatch.delenv("IBKR_PERIOD_RETURN_PUBLISH_ENABLED")
    else:
        monkeypatch.setenv("IBKR_PERIOD_RETURN_PUBLISH_ENABLED", enabled)
    monkeypatch.setenv("GITHUB_SHA", _PREFLIGHT_SOURCE_SHA)
    _forbid_preflight_io(monkeypatch)
    original, calls = cli._config, []
    def checked(env):
        calls.append(True)
        return original(env)
    monkeypatch.setattr(cli, "_config", checked)
    assert cli.main(("--preflight",)) == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result == {
        "schema_version": "ibkr.flex_configuration_preflight.v1",
        "source_sha": _PREFLIGHT_SOURCE_SHA, "status": "configuration_shape_valid",
        "fields": dict.fromkeys(_PREFLIGHT_FIELDS, "configuration_shape_valid"),
        "binding_fingerprints": {
            f"{name}_sha256": hashlib.sha256(values[f"IBKR_PERIOD_RETURN_{name.upper()}"].encode()).hexdigest()
            for name in ("target_id", "source_binding_id")
        },
    }
    assert calls == [True] and output.err == ""
    assert not any(value in output.out for key, value in values.items()
                   if key != "IBKR_PERIOD_RETURN_PUBLISH_ENABLED")


@pytest.mark.parametrize("field", _PREFLIGHT_FIELDS)
@pytest.mark.parametrize("missing", [None, ""])
def test_preflight_identifies_each_missing_field_without_values_or_io(monkeypatch, capsys, field, missing):
    configure(monkeypatch)
    if missing is None:
        monkeypatch.delenv(field)
    else:
        monkeypatch.setenv(field, missing)
    _forbid_preflight_io(monkeypatch)
    assert cli.main(("--preflight",)) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "missing"
    assert result["fields"][field] == "missing"
    assert set(result["fields"]) == set(_PREFLIGHT_FIELDS)
    assert all(status == "configuration_shape_valid" for key, status in result["fields"].items() if key != field)


@pytest.mark.parametrize("field,bad", [
    ("IBKR_PERIOD_RETURN_TARGET_ID", "BAD_private_target"),
    ("IBKR_PERIOD_RETURN_SOURCE_BINDING_ID", "private-binding"),
    ("IBKR_PERIOD_RETURN_ACCOUNT_SCOPE", "private scope"),
    ("IBKR_PERIOD_RETURN_ACCOUNT_KEY", "private/key"),
    ("IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON", "private-not-json"),
    ("IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON", "[]"),
    ("IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON", '["DU00000003","DU00000009"]'),
    ("IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON", '["not-account"]'),
    ("IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON", '[1234567]'),
    ("IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON", '"DU00000003"'),
    ("IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON", "[" * 1100 + "]" * 1100),
    ("IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON", "[" + "1" * 5000 + "]"),
    ("IBKR_FLEX_TOKEN", " private-token"),
    ("IBKR_FLEX_TOKEN", "private\ntoken"),
    ("IBKR_FLEX_QUERY_ID", "private-query"),
    ("IBKR_FLEX_QUERY_ID", "١٢٣"),
    ("IBKR_ACCOUNT_FACTS_SYNC_TOKEN", "private token"),
    ("IBKR_ACCOUNT_FACTS_SYNC_TOKEN", "private\u00a0token"),
])
def test_preflight_identifies_exact_invalid_shape_without_echo(monkeypatch, capsys, field, bad):
    configure(monkeypatch)
    monkeypatch.setenv(field, bad)
    _forbid_preflight_io(monkeypatch)
    assert cli.main(("--preflight",)) == 2
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["status"] == "invalid"
    assert result["fields"][field] == "invalid"
    assert all(status == "configuration_shape_valid" for key, status in result["fields"].items() if key != field)
    assert bad not in output.out and output.err == ""


@pytest.mark.parametrize("alias", ("IBKR_FLEX_TOKEN", *cli._TOKEN_ALIASES))
def test_preflight_preserves_existing_sync_token_separation(monkeypatch, capsys, alias):
    values = configure(monkeypatch)
    monkeypatch.setenv(alias, values["IBKR_ACCOUNT_FACTS_SYNC_TOKEN"])
    _forbid_preflight_io(monkeypatch)
    assert cli.main(("--preflight",)) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "invalid"
    assert result["fields"]["IBKR_ACCOUNT_FACTS_SYNC_TOKEN"] == "invalid"
    assert all(status == "configuration_shape_valid" for key, status in result["fields"].items()
               if key != "IBKR_ACCOUNT_FACTS_SYNC_TOKEN")


def test_preflight_redacts_invalid_source_sha_and_unknown_cli_arguments(monkeypatch, capsys):
    configure(monkeypatch)
    monkeypatch.setenv("GITHUB_SHA", "private-revision-value")
    _forbid_preflight_io(monkeypatch)
    assert cli.main(("--preflight",)) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["source_sha"] is None and "private-revision-value" not in output
    assert cli.main(("--preflight", "private-cli-value")) == 2
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "invalid" and "private-cli-value" not in output


def test_preflight_original_validator_failure_is_fixed_and_never_echoes_exception(monkeypatch, capsys):
    configure(monkeypatch)
    _forbid_preflight_io(monkeypatch)
    def failed(_env):
        raise RuntimeError("private-validator-error-body")
    monkeypatch.setattr(cli, "_config", failed)
    assert cli.main(("--preflight",)) == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["status"] == "invalid"
    assert "private-validator-error-body" not in output.out and output.err == ""


def test_preflight_workflow_is_manual_main_configuration_only_with_exact_report_secrets():
    from pathlib import Path
    import re

    workflow = Path(".github/workflows/check-ibkr-flex-configuration.yml").read_text()
    assert "workflow_dispatch:" in workflow and "github.ref == 'refs/heads/main'" in workflow
    assert "github.repository == 'QuantStrategyLab/InteractiveBrokersPlatform'" in workflow
    assert "contents: read" in workflow and "persist-credentials: false" in workflow
    assert "uv sync --frozen --no-dev" in workflow
    assert "astral-sh/setup-uv@37802adc94f370d6bfd71619e3f0bf239e1f3b78" in workflow
    assert workflow.count("python -m scripts.publish_ibkr_flex_period_return --preflight") == 1
    assert set(re.findall(r"secrets\.([A-Z0-9_]+)", workflow)) == set(_PREFLIGHT_FIELDS) | {
        "IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON", "IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON",
    }
    assert set(re.findall(r"vars\.([A-Z0-9_]+)", workflow)) == {"QSL_CONSOLE_BASE_URL"}
    assert "GITHUB_SHA: ${{ github.sha }}" in workflow
    for unsafe in ("schedule:", "workflow_run:", "id-token:", "google-github-actions/auth", "gcloud",
                   "upload-artifact", "TELEGRAM", "CLOUDFLARE", "publish_once", "PUBLISH_ENABLED"):
        assert unsafe not in workflow


def test_daily_history_override_is_account_scoped_and_published_once(monkeypatch, capsys):
    configure(monkeypatch)
    monkeypatch.setenv("IBKR_NATIVE_RETURN_DATE", "2026-01-31")
    session = FlexSession(xml().replace(b"20260101", b"20260131"))
    opener = Opener()
    monkeypatch.setattr(cli.requests, "Session", lambda: session)
    monkeypatch.setattr(cli, "build_opener", lambda *_args: opener)
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "published"
    assert len(opener.calls) == 1
    assert opener.calls[0][0].full_url == cli.SYNC_URL + "?historical=true"
    assert session.calls[0][1]["params"]["fd"] == "20260131"
    assert session.calls[0][1]["params"]["td"] == "20260131"


def test_provider_ignoring_date_override_never_publishes(monkeypatch, capsys):
    configure(monkeypatch)
    monkeypatch.setenv("IBKR_NATIVE_RETURN_DATE", "2026-01-15")
    session = FlexSession(xml())
    monkeypatch.setattr(cli.requests, "Session", lambda: session)
    monkeypatch.setattr(cli, "build_opener", lambda *_args: pytest.fail("must not publish mismatched period"))
    assert cli.main() == 1
    assert json.loads(capsys.readouterr().out)["status"] == "historical_period_mismatch"
