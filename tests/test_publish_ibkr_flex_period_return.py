import json
from urllib.error import HTTPError

import pytest

from scripts import publish_ibkr_flex_period_return as cli


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
    ("IBKR_FLEX_QUERY_ID", "invalid"), ("IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON", '["DU0000000","DU9999999"]'),
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
    assert capsys.readouterr().out == '{"status": "flex_import_failed"}\n'


@pytest.mark.parametrize("report", [xml(twr=None), xml(account="DU9999999"), xml(currency="")])
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
