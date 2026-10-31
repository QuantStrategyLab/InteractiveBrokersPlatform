import json
from datetime import datetime, timezone
from urllib.error import HTTPError
from xml.etree import ElementTree

import pytest

from application.ibkr_external_flow import build_ibkr_external_flow, project_flex_external_flows
from application.ibkr_flex_ledger import build_flex_ledger
from application.ibkr_flex_source import FlexReport
from scripts import publish_ibkr_flex_period_return as cli
from tests.test_publish_ibkr_flex_period_return import configure, FlexSession, Response


ACCOUNT = "DU0000000"
PERIOD = {"from": "2026-10-01", "to": "2026-10-23"}


def row(**patch):
    fields = dict(accountId=ACCOUNT, activityCode="DEP", date="20261002", currency="USD", amount="100.12345678", transactionID="synthetic-native-id")
    fields.update(patch)
    return '<StatementOfFundsLine ' + ' '.join(f'{k}="{v}"' for k, v in fields.items() if v is not None) + '/>'


def report(rows=None):
    if rows is None:
        rows = row()
    return (f'<FlexQueryResponse type="AF"><FlexStatements><FlexStatement accountId="{ACCOUNT}" fromDate="20261001" toDate="20261023">'
            f'<AccountInformation currency="USD"/><StmtFunds>{rows}</StmtFunds><ChangeInNAV fromDate="20261001" toDate="20261023" twr="1.25"/></FlexStatement></FlexStatements></FlexQueryResponse>').encode()


def ledger(rows=None):
    import hashlib
    raw = report(rows)
    return build_flex_ledger(FlexReport(content=raw, content_sha256=hashlib.sha256(raw).hexdigest()), expected_account_ids=(ACCOUNT,))


def test_native_dates_stable_id_and_exact_amount_ignore_other_cash_activity():
    facts = ledger(row() + ''.join(row(activityCode=kind) for kind in ("DIV", "INT", "OFEE", "BUY", "SELL", "FX")))["external_flow_facts"]
    assert facts["status"] == "available" and len(facts["records"]) == 1
    record = facts["records"][0]
    assert record["effective_date"] == "2026-10-02" and "effective_at" not in record
    assert record["amount"] == "100.12345678"
    assert record["source_ref"] == ledger()["external_flow_facts"]["records"][0]["source_ref"]
    assert "synthetic-native-id" not in json.dumps(facts)
    assert ledger(row()+row())["external_flow_facts"]["records"] == facts["records"]


@pytest.mark.parametrize("patch", [{"transactionID": ""}, {"date": "20260230"}, {"date": "20260930"}, {"amount": "-1"},
    {"amount": "0"}, {"amount": "NaN"}, {"accountId": "DU0000001"}, {"model": "synthetic-model"},
    {"settleDate": "20260230"}, {"currency": "BASE_SUMMARY"}])
def test_bad_external_facts_do_not_acquire_defaults(patch):
    statement = ElementTree.fromstring(f'<FlexStatement accountId="{ACCOUNT}"><StmtFunds>{row(**patch)}</StmtFunds></FlexStatement>')
    assert project_flex_external_flows(statement, period=PERIOD)["status"] == "source_unavailable"


def test_id_conflict_missing_section_and_reported_empty_are_distinct():
    assert ledger(row()+row(amount="101"))["external_flow_facts"]["reason"] == "flow_identity_conflict"
    assert ledger("")["external_flow_facts"]["status"] == "available"
    no_section = ElementTree.fromstring(f'<FlexStatement accountId="{ACCOUNT}"/>')
    assert project_flex_external_flows(no_section, period=PERIOD)["reason"] == "funds_section_missing"
    assert ledger(row(activityCode="WITH", amount="-1"))["external_flow_facts"]["records"][0]["type"] == "withdrawal"


def test_legacy_flow_total_uses_the_identity_checked_projection():
    original = ledger(row())
    duplicate = ledger(row() + row())
    assert duplicate["net_external_flow"] == original["net_external_flow"]
    assert len(duplicate["external_flows"]) == 2  # Raw legacy rows remain visible.
    for rows in (row() + row(amount="101"), row() + row() + row(transactionID=None)):
        assert ledger(rows)["net_external_flow"] is None
        assert {"code": "duplicate_external_flow_identity_unverified"} in ledger(rows)["warnings"]
    assert ledger("")["net_external_flow"] == {"amount": "0", "currency": "USD", "line_count": 0}
    assert ledger(row(transactionID=None))["net_external_flow"]["amount"] == "100.12345678"
    unique_legacy = ledger(row(accountId=None, date=None))
    assert unique_legacy["external_flow_facts"]["status"] == "source_unavailable"
    assert unique_legacy["net_external_flow"]["amount"] == "100.12345678"
    assert ledger(row() + row(transactionID=None))["net_external_flow"]["line_count"] == 2
    long_period = report(row()).replace(b'fromDate="20261001"', b'fromDate="20260901"')
    legacy = build_flex_ledger(FlexReport(content=long_period, content_sha256="a" * 64), expected_account_ids=(ACCOUNT,))
    assert legacy["external_flow_facts"]["status"] == "source_unavailable"
    assert legacy["net_external_flow"]["amount"] == "100.12345678"


def test_repeated_id_never_omits_flows_under_legacy_activity_code_normalization():
    from application.ibkr_flex_ledger import build_flex_ledger_diagnostic

    for rows in (row(activityCode="dep") * 2, row(activityCode="DEP ") * 2,
                 row() * 2 + row(activityCode="dep", transactionID="other")):
        source = ledger(rows)
        assert source["net_external_flow"] is None
        assert {"code": "duplicate_external_flow_identity_unverified"} in source["warnings"]
        assert "duplicate_external_flow_identity_unverified" in build_flex_ledger_diagnostic(source)["warnings"]
    assert ledger(row(activityCode="dep"))["net_external_flow"]["amount"] == "100.12345678"


class FlowOpener:
    def __init__(self, patch=None, error=None):
        self.patch, self.error, self.calls = patch or {}, error, []

    def open(self, request, timeout):
        self.calls.append(request)
        if self.error:
            raise self.error
        payload = json.loads(request.data)
        windows = payload["externalFlow"]["windows"]
        return Response(json.dumps({"ok": True, "stored": True, "unchanged": False, "readback_verified": True,
            "account_key": "synthetic-account-key", "window_count": len(windows),
            "record_count": len({r["source_ref"] for w in windows for r in w["records"]}), **self.patch}).encode())


def test_flow_only_fetch_one_report_publish_no_twr_trade_or_snapshot(monkeypatch, capsys):
    configure(monkeypatch)
    monkeypatch.setenv("IBKR_FLOW_PERIOD_FROM", PERIOD["from"]); monkeypatch.setenv("IBKR_FLOW_PERIOD_TO", PERIOD["to"])
    session, opener = FlexSession(report()), FlowOpener()
    monkeypatch.setattr(cli.requests, "Session", lambda: session)
    monkeypatch.setattr(cli, "build_opener", lambda *_: opener)
    assert cli.main(("--cashflow-only",)) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["external_flow_record_count"] == 1 and output["external_flow_status"] == "published"
    assert output["external_flow_period"] == PERIOD
    assert "100.12345678" not in json.dumps(output) and ACCOUNT not in json.dumps(output)
    assert len(opener.calls) == 1 and opener.calls[0].full_url.endswith("/external-flow/sync")
    assert session.calls[0][1]["params"]["fd"] == "20261001"
    assert session.calls[0][1]["params"]["td"] == "20261023"


@pytest.mark.parametrize("patch", [{"record_count": True}, {"readback_verified": False}, {"record_count": 2}, {"unchanged": "false"}])
def test_invalid_ack_is_unknown_and_never_retried(monkeypatch, patch):
    config = cli._config(configure(monkeypatch))
    payload = build_ibkr_external_flow(ledger(), **{k:config[k] for k in ("target_id", "source_binding_id", "account_scope", "expected_account_ids")})
    opener = FlowOpener(patch)
    monkeypatch.setattr(cli, "build_opener", lambda *_: opener)
    detail = {}
    assert cli.publish_external_flow(payload, sync_token=config["sync_token"], expected_account_key=config["account_key"], diagnostics=detail) == "sync_unknown"
    assert detail == {"external_flow_sync_reason": "invalid_ack"}
    assert len(opener.calls) == 1


def test_native_period_return_is_not_replaced_by_date_only_flows():
    from application.ibkr_period_return import build_ibkr_period_return
    values = dict(target_id="synthetic-target", source_binding_id="a"*64, account_scope="synthetic-live", expected_account_ids=(ACCOUNT,))
    source = ledger()
    payload = build_ibkr_period_return(source, **values, observed_at=datetime.now(timezone.utc))
    assert payload["method"] == "native_ibkr_twr" and payload["value"] == "0.0125"


@pytest.mark.parametrize("start,end", [("2026-10-01", ""), ("2026-10-24", "2026-10-01"), ("2026-09-01", "2026-10-23"), ("bad", "2026-10-23")])
def test_bad_flow_range_stops_before_requests(monkeypatch, capsys, start, end):
    configure(monkeypatch)
    monkeypatch.setenv("IBKR_FLOW_PERIOD_FROM", start); monkeypatch.setenv("IBKR_FLOW_PERIOD_TO", end)
    monkeypatch.setattr(cli.requests, "Session", lambda: pytest.fail("invalid range must not request"))
    assert cli.main(("--cashflow-only",)) == 2
    assert json.loads(capsys.readouterr().out) == {"external_flow_status": "configuration_incomplete"}


def test_flow_only_rejection_reports_status_without_private_response_or_retry(monkeypatch, capsys):
    configure(monkeypatch)
    session = FlexSession(report())
    opener = FlowOpener(error=HTTPError(cli.EXTERNAL_FLOW_SYNC_URL, 409, "synthetic-secret", {}, None))
    monkeypatch.setattr(cli.requests, "Session", lambda: session)
    monkeypatch.setattr(cli, "build_opener", lambda *_: opener)
    assert cli.main(("--cashflow-only",)) == 1
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["external_flow_status"] == "sync_rejected"
    assert result["external_flow_http_status"] == 409
    assert len(opener.calls) == 1 and "synthetic-secret" not in output and ACCOUNT not in output
