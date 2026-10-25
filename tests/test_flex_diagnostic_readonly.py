"""Bounded synthetic Flex reads never acquire publication authority."""
import json
from pathlib import Path

import pytest

from application import ibkr_flex_source as source
from scripts import diagnose_ibkr_flex_ledger as cli
from tests.test_ibkr_flex_source import PENDING, REFERENCE, SEND, TOKEN, XML, Session


def configure(monkeypatch):
    monkeypatch.setenv("IBKR_FLEX_DIAGNOSTIC_ENABLED", "true")
    monkeypatch.setenv("IBKR_FLEX_TOKEN", TOKEN)
    monkeypatch.setenv("IBKR_FLEX_QUERY_ID", "12345")
    monkeypatch.setenv("IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON", '["DU0000000"]')
    monkeypatch.delenv("IBKR_FLEX_EXPECTED_PERIOD_FROM", raising=False)
    monkeypatch.delenv("IBKR_FLEX_EXPECTED_PERIOD_TO", raising=False)
    monkeypatch.delenv("IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON", raising=False)
    monkeypatch.delenv("IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON", raising=False)


@pytest.fixture(autouse=True)
def no_real_session(monkeypatch):
    def forbidden():
        pytest.fail("synthetic tests must not open a real HTTP session")
    monkeypatch.setattr(source.requests, "Session", forbidden)


def test_pending_diagnostic_collects_original_handle_without_second_generation(monkeypatch):
    session = Session(SEND, PENDING, PENDING, XML)
    delays = []
    monkeypatch.setattr(source.time, "sleep", delays.append)
    out = source.diagnose_activity_flex_ledger(token=TOKEN, query_id="12345",
        expected_account_ids=("DU0000000",), session=session, collect_pending=True)
    assert out["status"] == "available"
    assert delays == [3, 3]
    assert [name for name, _ in session.calls] == ["SendRequest", "GetStatement", "GetStatement", "GetStatement"]
    assert all(params["q"] == REFERENCE for _, params in session.calls[1:])
    assert TOKEN not in json.dumps(out) and REFERENCE not in json.dumps(out)


def test_pending_diagnostic_exhausts_five_collections_without_replacement(monkeypatch):
    session = Session(SEND, *([PENDING] * 6))
    delays = []
    monkeypatch.setattr(source.time, "sleep", delays.append)
    with pytest.raises(source.FlexReportPending) as caught:
        source.diagnose_activity_flex_ledger(token=TOKEN, query_id="12345",
            expected_account_ids=("DU0000000",), session=session, collect_pending=True)
    assert len(session.calls) == 7 and delays == [3] * 5
    assert caught.value.request.reference_code == REFERENCE
    assert sum(name == "SendRequest" for name, _ in session.calls) == 1


@pytest.mark.parametrize("bad", [b"broken XML", b'<FlexStatementResponse><ErrorCode>1012</ErrorCode></FlexStatementResponse>',
    XML.replace(b"DU0000000", b"DU0000001")])
def test_nonpending_parse_or_scope_failures_stop_immediately(monkeypatch, bad):
    session = Session(SEND, PENDING, bad)
    delays = []
    monkeypatch.setattr(source.time, "sleep", delays.append)
    with pytest.raises(source.FlexSourceError):
        source.diagnose_activity_flex_ledger(token=TOKEN, query_id="12345",
            expected_account_ids=("DU0000000",), session=session, collect_pending=True)
    assert len(session.calls) == 3 and delays == [3]


def test_default_diagnostic_keeps_single_collection(monkeypatch):
    session = Session(SEND, PENDING)
    monkeypatch.setattr(source.time, "sleep", lambda _: pytest.fail("default must not wait"))
    with pytest.raises(source.FlexReportPending):
        source.diagnose_activity_flex_ledger(token=TOKEN, query_id="12345",
            expected_account_ids=("DU0000000",), session=session)
    assert len(session.calls) == 2


def test_explicit_approved_report_scope_selects_one_target_without_widening(monkeypatch):
    other = b'<FlexStatement accountId="DU0000001"><AccountInformation currency="USD"/></FlexStatement>'
    report = XML.replace(b"</FlexStatements>", other + b"</FlexStatements>")
    for allowed, succeeds in [(None, False), (("DU0000000", "DU0000001"), True)]:
        session = Session(SEND, report)
        if succeeds:
            out = source.diagnose_activity_flex_ledger(token=TOKEN, query_id="12345",
                expected_account_ids=("DU0000000",), allowed_report_account_ids=allowed, session=session)
            assert out["period"] == {"from": "2026-01-01", "to": "2026-01-31"}
        else:
            with pytest.raises(source.FlexSourceError):
                source.diagnose_activity_flex_ledger(token=TOKEN, query_id="12345",
                    expected_account_ids=("DU0000000",), session=session)
        assert len(session.calls) == 2


@pytest.mark.parametrize("expected,allowed", [((), None), (("DU0000000", "DU0000001"), None),
    (("OTHER",), None), (("DU0000000",), ("DU0000001",)),
    (("DU0000000",), ("DU0000000", "DU0000000"))])
def test_bad_scope_rejected_before_generation(expected, allowed):
    session = Session()
    with pytest.raises(source.FlexSourceError):
        source.diagnose_activity_flex_ledger(token=TOKEN, query_id="12345",
            expected_account_ids=expected, allowed_report_account_ids=allowed, session=session)
    assert session.calls == []


@pytest.mark.parametrize("args", [("--preflight",), ("--unknown-private-value",), ("--read-once", "--preflight")])
def test_preflight_and_unknown_arguments_make_no_request(monkeypatch, capsys, args):
    configure(monkeypatch)
    assert cli.main(args) == (0 if args == ("--preflight",) else 2)
    text = capsys.readouterr().out
    assert TOKEN not in text and "DU0000000" not in text and "private-value" not in text


@pytest.mark.parametrize("scope", ['[]', '["DU0000000","DU0000001"]', '["OTHER"]', '{"id":"DU0000000"}'])
def test_cli_requires_one_target_before_requests(monkeypatch, capsys, scope):
    configure(monkeypatch)
    monkeypatch.setenv("IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON", scope)
    assert cli.main(("--read-once",)) == 2
    assert json.loads(capsys.readouterr().out)["missing"] == ["expected_account_scope_missing_or_invalid"]


@pytest.mark.parametrize("start,end", [("2026-02-30", "2026-03-01"), ("2026-02-01", ""),
    ("2026-02-01", "2026-01-31"), ("private-value", "2026-01-31")])
def test_invalid_expected_period_fails_before_request(monkeypatch, capsys, start, end):
    configure(monkeypatch)
    monkeypatch.setenv("IBKR_FLEX_EXPECTED_PERIOD_FROM", start)
    monkeypatch.setenv("IBKR_FLEX_EXPECTED_PERIOD_TO", end)
    assert cli.main(("--read-once",)) == 2
    assert json.loads(capsys.readouterr().out)["missing"] == ["expected_period_invalid"]


@pytest.mark.parametrize("end,code", [("2026-01-31", 0), ("2026-01-30", 1)])
def test_expected_period_checks_result_without_altering_query(monkeypatch, capsys, end, code):
    configure(monkeypatch)
    monkeypatch.setenv("IBKR_FLEX_EXPECTED_PERIOD_FROM", "2026-01-01")
    monkeypatch.setenv("IBKR_FLEX_EXPECTED_PERIOD_TO", end)
    calls = []
    def diagnose(**kwargs):
        calls.append(kwargs)
        return {"status":"available", "method":"native_ibkr_twr", "currency":"EUR",
            "period":{"from":"2026-01-01", "to":"2026-01-31"}, "missing":[], "warnings":[]}
    monkeypatch.setattr(cli, "diagnose_activity_flex_ledger", diagnose)
    assert cli.main(("--read-once",)) == code
    out = json.loads(capsys.readouterr().out)
    assert calls == [{"token":TOKEN, "query_id":"12345", "expected_account_ids":("DU0000000",), "collect_pending":True}]
    assert out["missing"] == ([] if code == 0 else ["expected_period_mismatch"])


@pytest.mark.parametrize("period", [{"from":None, "to":"2026-01-31"},
    {"from":"2026-02-01", "to":"2026-01-31"}, {"from":"secret", "to":"secret"}])
def test_available_with_invalid_period_is_never_accepted(monkeypatch, capsys, period):
    configure(monkeypatch)
    monkeypatch.setattr(cli, "diagnose_activity_flex_ledger", lambda **_: dict(status="available",
        method="native_ibkr_twr", currency="EUR", period=period, missing=[], warnings=[]))
    assert cli.main(("--read-once",)) == 1
    text = capsys.readouterr().out
    assert "secret" not in text and json.loads(text)["missing"] == ["flex_diagnostic_result_invalid"]


def test_cli_rejects_half_configured_approved_scope_before_http(monkeypatch, capsys):
    configure(monkeypatch)
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON", '{"unapproved":"DU0000001"}')
    other = b'<FlexStatement accountId="DU0000001"/>'
    report = XML.replace(b"</FlexStatements>", other + b"</FlexStatements>")
    class ClosedSession(Session):
        def close(self):
            pass
    session = ClosedSession(SEND, report)
    monkeypatch.setattr(source.requests, "Session", lambda: session)
    assert cli.main(("--read-once",)) == 2
    out = capsys.readouterr().out
    assert json.loads(out)["missing"] == ["approved_report_scope_invalid"]
    assert "DU0000001" not in out and session.calls == []


def test_cli_bounded_pending_keeps_private_handle_out_of_stdout(monkeypatch, capsys):
    configure(monkeypatch)
    class ClosedSession(Session):
        def close(self):
            pass
    session = ClosedSession(SEND, *([PENDING] * 6))
    monkeypatch.setattr(source.requests, "Session", lambda: session)
    monkeypatch.setattr(source.time, "sleep", lambda _: None)
    assert cli.main(("--read-once",)) == 1
    text = capsys.readouterr().out
    assert json.loads(text)["missing"] == ["flex_generation_pending"]
    assert len(session.calls) == 7
    for private in (TOKEN, REFERENCE, "DU0000000", "synthetic-private-message"):
        assert private not in text


def test_pending_transport_failure_is_not_retried_or_echoed(monkeypatch, capsys):
    configure(monkeypatch)
    class FailedSession(Session):
        def get(self, url, **kwargs):
            if len(self.calls) >= 2:
                self.calls.append(("GetStatement", kwargs["params"]))
                raise source.requests.RequestException(f"private {TOKEN} {REFERENCE} amount=123.45")
            return super().get(url, **kwargs)
        def close(self):
            pass
    session = FailedSession(SEND, PENDING)
    monkeypatch.setattr(source.requests, "Session", lambda: session)
    monkeypatch.setattr(source.time, "sleep", lambda _: None)
    assert cli.main(("--read-once",)) == 1
    text = capsys.readouterr().out
    assert json.loads(text)["missing"] == ["flex_import_failed"]
    assert len(session.calls) == 3
    for private in (TOKEN, REFERENCE, "private", "123.45"):
        assert private not in text


def test_manual_workflow_has_only_exact_scope_diagnostic_and_no_publication():
    text = Path(".github/workflows/diagnose-ibkr-flex-readonly.yml").read_text()
    assert "workflow_dispatch:" in text and "schedule:" not in text
    assert "github.ref == 'refs/heads/main'" in text and "github.run_attempt == 1" in text
    assert "inputs.read_once" in text and "default: false" in text
    assert text.index("--preflight") < text.index("--read-once")
    assert "group: ibkr-flex-period-return" in text
    setup = text.split("      - name: Validate the exact target", 1)[0]
    assert "secrets." not in setup  # No token on the job, checkout, setup or install steps.
    for secret in ("IBKR_FLEX_TOKEN", "IBKR_FLEX_QUERY_ID", "IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON",
            "IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON", "IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON"):
        assert text.count("${{ secrets." + secret + " }}") == 2
    for forbidden in ("publish_ibkr_flex_period_return", "SYNC_TOKEN", "PERIOD_RETURN_", "google-github-actions/auth",
            "actions/upload-artifact", "IBKR_FLEX_ALLOWED"):
        assert forbidden not in text
