import json

import pytest

from scripts import publish_ibkr_runtime_daily as publisher
from tests.test_project_ibkr_runtime_daily import EXPECTED, OBSERVED, TARGET, project


def test_single_post_checks_ack_and_does_not_expose_private_data(monkeypatch):
    projection = project([])
    monkeypatch.setattr(publisher.daily, "project_ibkr_runtime_daily_from_archive", lambda **kw: projection)
    calls = []
    ack = {"ok": True, "stored": True, "platform": "ibkr", "account_key": "fixture-account",
           "target_key": TARGET, "business_date": "2026-09-29", "observed_at": projection["observed_at"]}

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size): return json.dumps(ack).encode()

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            return Response()

    monkeypatch.setattr(publisher, "build_opener", lambda *_: Opener())
    kwargs = dict(expected=EXPECTED, target_key=TARGET, account_key="fixture-account", business_date="2026-09-29",
                  observed_at=OBSERVED, token="fixture-token", origin="https://fixture.example")
    result = publisher.publish_once(**kwargs)
    assert result == {"status": "published", "observed_at": projection["observed_at"], "completeness": "incomplete"}
    assert len(calls) == 1 and calls[0].get_method() == "POST"
    assert calls[0].get_header("X-qsl-source-binding-id")
    assert "fixture-token" not in json.dumps(result) and "fixture-account" not in json.dumps(result)
    ack["account_key"] = "wrong-account"
    result = publisher.publish_once(**kwargs)
    assert result == {"status": "unconfirmed", "reason": "daily_publish_outcome_unknown"} and len(calls) == 2
    ack.update(account_key="fixture-account", target_key="wrong-target")
    result = publisher.publish_once(**kwargs)
    assert result == {"status": "unconfirmed", "reason": "daily_publish_outcome_unknown"} and len(calls) == 3
    class FailedOpener:
        def open(self, request, timeout):
            calls.append(request)
            raise OSError("private-source-or-token")
    monkeypatch.setattr(publisher, "build_opener", lambda *_: FailedOpener())
    result = publisher.publish_once(**kwargs)
    assert result == {"status": "unconfirmed", "reason": "daily_publish_outcome_unknown"} and len(calls) == 4
    assert "private-source-or-token" not in json.dumps(result)


def test_missing_identity_and_bad_origin_stop_before_archive(monkeypatch):
    monkeypatch.setattr(publisher.daily, "project_ibkr_runtime_daily_from_archive", lambda **kw: pytest.fail("archive read"))
    kwargs = dict(expected=EXPECTED, target_key=TARGET, account_key="fixture-account", business_date="2026-09-29",
                  observed_at=OBSERVED, token="", origin="https://fixture.example")
    with pytest.raises(ValueError): publisher.publish_once(**kwargs)
    kwargs.update(token="fixture-token", origin="http://fixture.example")
    with pytest.raises(ValueError): publisher.publish_once(**kwargs)
