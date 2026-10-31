"""Synthetic amounts only; tests never access broker credentials or the network."""

import copy
import hashlib
import http.client
import json
import logging
import os
from pathlib import Path

import pytest

from application import ibkr_flex_amount_export as export
from application import ibkr_flex_source as source
from scripts import export_ibkr_flex_amounts as cli
from tests.test_flex_diagnostic_readonly import configure
from tests.test_ibkr_flex_source import PENDING, REFERENCE, SEND, TOKEN, Session


PERIOD = {"from": "2026-01-01", "to": "2026-01-31"}
ACCOUNT = "DU0000000"
XML = b'''<FlexQueryResponse type="AF"><FlexStatements><FlexStatement accountId="DU0000000" fromDate="20260101" toDate="20260131">
<AccountInformation currency="USD" acctAlias="PRIVATE_ALIAS"/>
<Trades><Trade accountId="DU0000000" levelOfDetail="EXECUTION" currency="USD" symbol="XYZ" buySell="BUY" assetCategory="STK" quantity="2" tradePrice="11.123456789" proceeds="-22.246913578" netCash="-23.246913578" ibCommission="-1.00" ibCommissionCurrency="USD" taxes="0" tradeDate="20260105" dateTime="20260105;120001" settleDateTarget="20260202" tradeID="PRIVATE_TRADE" ibExecID="PRIVATE_EXEC" description="PRIVATE_DESCRIPTION"/></Trades>
<CashTransactions><CashTransaction accountId="DU0000000" currency="EUR" amount="3456.123456789012345678" dateTime="20260105;110000" type="Deposits &amp; Withdrawals" description="PRIVATE_MEMO" clientReference="PRIVATE_REF" transactionID="PRIVATE_TRANSACTION"/></CashTransactions>
<StmtFunds><StatementOfFundsLine accountId="DU0000000" currency="EUR" amount="3456.123456789012345678" credit="3456.123456789012345678" debit="0" balance="4456.123456789012345678" activityCode="DEP" date="20260105" transactionID="PRIVATE_TRANSACTION"/><StatementOfFundsLine currency="USD" amount="-23.246913578" activityCode="BUY" date="20260105" tradeID="PRIVATE_TRADE"/></StmtFunds>
</FlexStatement></FlexStatements></FlexQueryResponse>'''


def project(xml=XML, accounts=(ACCOUNT,)):
    report = source.FlexReport(hashlib.sha256(xml).hexdigest(), xml)
    return export.build_redacted_amount_export(report, account_ids=accounts, period=PERIOD)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(source.requests, "Session", lambda: pytest.fail("no real HTTP allowed"))


def setup_cli(monkeypatch, tmp_path, *responses):
    configure(monkeypatch)
    monkeypatch.setenv("IBKR_FLEX_AMOUNT_EXPORT_ENABLED", "true")
    monkeypatch.setenv("IBKR_FLEX_QUERY_ID", "7777777777")
    monkeypatch.setenv("IBKR_FLEX_EXPECTED_PERIOD_FROM", PERIOD["from"])
    monkeypatch.setenv("IBKR_FLEX_EXPECTED_PERIOD_TO", PERIOD["to"])
    monkeypatch.delenv("IBKR_FLEX_CASHFLOW_REQUEST_FROM", raising=False)
    monkeypatch.delenv("IBKR_FLEX_CASHFLOW_REQUEST_TO", raising=False)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    class ClosedSession(Session):
        def close(self):
            pass
    session = ClosedSession(*responses)
    monkeypatch.setattr(source.requests, "Session", lambda: session)
    delays = []
    monkeypatch.setattr(cli.time, "sleep", delays.append)
    return session, delays


def test_preserves_exact_decimal_currency_precision_missing_fields_and_source_rows():
    result = project()
    sections = result["accounts"][0]["sections"]
    assert result["accounts"][0]["account"] == "account-1"
    trade = sections["Trades"][0]
    assert trade["proceeds"] == "-22.246913578"
    assert trade["netCash"] == "-23.246913578" and trade["ibCommission"] == "-1.00"
    assert trade["tradeMoney"] is None and trade["dateTime"] == "2026-01-05T12:00:01"
    assert trade["settleDateTarget"] == "2026-02-02"  # Settlement can follow the report window.
    cash = sections["CashTransactions"][0]
    funds = sections["StmtFunds"][0]
    assert cash["amount"] == funds["amount"] == "3456.123456789012345678"
    assert cash["currency"] == funds["currency"] == "EUR"
    assert cash["transactionID"] == funds["transactionID"]
    assert trade["tradeID"] == sections["StmtFunds"][1]["tradeID"]
    assert funds["date"] == "2026-01-05" and funds["dateTime"] is None
    assert result["reconciliation"] == "not_performed" and result["missing_fields_are_zero"] is False
    assert result["coverage"] == "reported_rows_only_not_complete_history"
    content = export.serialize_redacted_export(result, forbidden_values=(TOKEN, REFERENCE, ACCOUNT))
    for secret in (ACCOUNT, TOKEN, REFERENCE, "PRIVATE_", hashlib.sha256(XML).hexdigest()):
        assert secret not in content


def test_all_approved_accounts_exported_in_configuration_order_without_raw_mapping():
    statement = XML.split(b"<FlexStatements>")[1].split(b"</FlexStatements>")[0]
    xml = XML.replace(b"</FlexStatements>", statement.replace(b"DU0000000", b"DU0000001") + b"</FlexStatements>")
    result = project(xml, ("DU0000001", ACCOUNT))
    assert [a["account"] for a in result["accounts"]] == ["account-1", "account-2"]
    assert "DU000000" not in json.dumps(result)
    with pytest.raises(source.FlexSourceError):
        project(XML, (ACCOUNT, "DU0000001"))


def test_empty_sections_export_as_reported_empty_and_missing_sections_reject():
    empty = XML[:XML.index(b"<Trades>")] + b"<Trades/><CashTransactions/><StmtFunds/></FlexStatement></FlexStatements></FlexQueryResponse>"
    assert project(empty)["accounts"][0]["sections"] == {k: [] for k in export._SECTIONS}
    with pytest.raises(export.FlexAmountExportError):
        project(empty.replace(b"<Trades/>", b""))


@pytest.mark.parametrize("old,new", [
    (b'toDate="20260131"', b'toDate="20260130"'),
    (b'type="AF"', b'type="TF"'),
    (b'accountId="DU0000000" currency="EUR"', b'accountId="DU0000001" currency="EUR"'),
    (b'<Trades>', b'<Trades model="PRIVATE_MODEL">'),
    (b'<Trade accountId=', b'<Trade model="PRIVATE_MODEL" accountId='),
    (b'</Trades>', b'</Trades><Trades/>'),
    (b'<Trade accountId=', b'<Unknown accountId='),
    (b'tradePrice="11.123456789"', b'tradePrice="NaN"'),
    (b'ibCommission="-1.00"', b'ibCommission="Infinity"'),
    (b'proceeds="-22.246913578"', b'proceeds="1e9999"'),
    (b'proceeds="-22.246913578"', b'proceeds=""'),
    (b'levelOfDetail="EXECUTION"', b'levelOfDetail="ORDER"'),
    (b'currency="EUR"', b'currency="BASE_SUMMARY"'),
    (b'date="20260105"', b'date="20260201"'),
    (b'date="20260105"', b'date="20260230"'),
    (b'dateTime="20260105;110000"', b'dateTime="20260105;250000"'),
    (b'dateTime="20260105;110000"', b'dateTime=""'),
    (b'symbol="XYZ"', b'symbol="PRIVATE_SECRET&#10;VALUE"'),
    (b'<StmtFunds>', b'<StmtFunds><Unexpected/>'),
    (b'<Trades>', b'<Trades><Trade><Nested/></Trade>'),
])
def test_invalid_reports_reject_everything_with_fixed_error(old, new):
    with pytest.raises((export.FlexAmountExportError, source.FlexSourceError)) as caught:
        project(XML.replace(old, new))
    assert "PRIVATE_" not in str(caught.value)


@pytest.mark.parametrize("body", [b"broken", b'<!DOCTYPE root [<!ENTITY secret "PRIVATE">]>' + XML,
                                  XML.decode().encode("utf-16"), b"x" * (8 * 1024 * 1024 + 1)])
def test_unsafe_xml_rejects(body):
    with pytest.raises((export.FlexAmountExportError, source.FlexSourceError)):
        project(body)


def test_missing_identity_is_null_and_repeated_native_rows_are_not_deduplicated():
    xml = XML.replace(b'transactionID="PRIVATE_TRANSACTION"', b'transactionID=""')
    row = xml.split(b"<CashTransactions>")[1].split(b"</CashTransactions>")[0]
    result = project(xml.replace(b"</CashTransactions>", row + b"</CashTransactions>"))
    rows = result["accounts"][0]["sections"]["CashTransactions"]
    assert len(rows) == 2 and all(r["transactionID"] is None for r in rows)


def test_balance_only_funds_row_is_not_a_zero_flow():
    xml = XML.replace(b'amount="3456.123456789012345678" credit=', b'credit=')
    funds = project(xml)["accounts"][0]["sections"]["StmtFunds"][0]
    assert funds["amount"] is None and funds["balance"] == "4456.123456789012345678"


@pytest.mark.parametrize("secret", ["XYZ", "3456", "2026", "DU1234567", 'quoted"secret'])
def test_final_guard_rejects_secret_values_in_any_exported_string(secret):
    result = copy.deepcopy(project())
    result["accounts"][0]["sections"]["Trades"][0]["symbol"] = secret
    with pytest.raises(export.FlexAmountExportError):
        export.serialize_redacted_export(result, forbidden_values=(secret,))


def test_account_like_symbol_is_rejected_even_when_not_in_protected_scope():
    result = project(XML.replace(b'symbol="XYZ"', b'symbol="DU9999999"'))
    with pytest.raises(export.FlexAmountExportError):
        export.serialize_redacted_export(result, forbidden_values=())


def test_excess_row_count_rejects_without_partial_projection():
    row = XML.split(b"<CashTransactions>")[1].split(b"</CashTransactions>")[0]
    with pytest.raises(export.FlexAmountExportError):
        project(XML.replace(row, row * 10_001))


def test_cli_sensitive_value_collision_fails_before_file_creation(monkeypatch, tmp_path, capsys):
    session, _ = setup_cli(monkeypatch, tmp_path, SEND, XML)
    # An accidental numeric collision also fails closed, rather than editing an amount.
    monkeypatch.setenv("IBKR_FLEX_QUERY_ID", "3456")
    assert cli.main(("--export-once",)) == 1
    assert len(session.calls) == 2 and not list(tmp_path.iterdir())
    assert capsys.readouterr().out == "flex_amount_export_failed\n"


def test_configured_accounts_use_existing_protected_bindings(monkeypatch, tmp_path, capsys):
    from tests.test_flex_protected_report_scope import protected_config

    session, _ = setup_cli(monkeypatch, tmp_path)
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON", '["DU0000000"]')
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON", json.dumps(protected_config()))
    token, query_id, accounts, period = cli._configuration()
    assert accounts == tuple(f"DU000000{i}" for i in range(4))
    assert period == PERIOD and token == TOKEN and query_id == "7777777777"
    assert cli.main(("--preflight",)) == 0 and session.calls == []
    assert capsys.readouterr().out == "flex_amount_export_configuration_valid\n"


def test_cli_success_writes_only_redacted_file_and_no_amount_in_logs(monkeypatch, tmp_path, capsys):
    session, delays = setup_cli(monkeypatch, tmp_path, SEND, PENDING, XML)
    assert cli.main(("--export-once",)) == 0
    captured = capsys.readouterr()
    assert captured.out == "flex_amount_export_written\n" and captured.err == ""
    path = tmp_path / cli.ARTIFACT_NAME
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text()) == project()
    assert list(tmp_path.iterdir()) == [path]
    assert [name for name, _ in session.calls] == ["SendRequest", "GetStatement", "GetStatement"]
    assert session.calls[0][1]["fd"] == "20260101" and session.calls[0][1]["td"] == "20260131"
    assert all(params["q"] == REFERENCE for _, params in session.calls[1:])
    assert delays == [1, 3]


@pytest.mark.parametrize("args,env", [
    (("--preflight",), {}), (("--unknown-secret",), {}),
    (("--export-once",), {"IBKR_FLEX_AMOUNT_EXPORT_ENABLED": "false"}),
    (("--export-once",), {"IBKR_FLEX_TOKEN": ""}),
    (("--export-once",), {"IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON": "[]"}),
    (("--export-once",), {"IBKR_FLEX_EXPECTED_PERIOD_TO": "2026-02-01"}),
    (("--export-once",), {"IBKR_FLEX_EXPECTED_PERIOD_FROM": ""}),
    (("--export-once",), {"IBKR_FLEX_CASHFLOW_REQUEST_FROM": "2026-01-01"}),
    (("--export-once",), {"IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON": '["DU0000000"]'}),
])
def test_invalid_or_preflight_invocation_has_no_requests_or_files(monkeypatch, tmp_path, capsys, args, env):
    session, _ = setup_cli(monkeypatch, tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert cli.main(args) == (0 if args == ("--preflight",) else 2)
    assert session.calls == [] and list(tmp_path.iterdir()) == []
    captured = capsys.readouterr()
    assert TOKEN not in captured.out and "unknown-secret" not in captured.out and captured.err == ""


@pytest.mark.parametrize("response", [b"broken", XML.replace(b"DU0000000", b"DU0000001"),
    ('<!DOCTYPE root [<!ENTITY secret "PRIVATE">]>' + XML.decode()).encode("utf-16"),
    XML.replace(b'amount="3456.123456789012345678"', b'amount="NaN"'),
    b"<FlexStatementResponse><ErrorCode>1018</ErrorCode><ErrorMessage>PRIVATE</ErrorMessage></FlexStatementResponse>"])
def test_read_failure_never_leaves_an_artifact_or_retries(monkeypatch, tmp_path, capsys, response):
    session, _ = setup_cli(monkeypatch, tmp_path, SEND, response)
    assert cli.main(("--export-once",)) == 1
    assert len(session.calls) == 2 and not list(tmp_path.iterdir())
    assert capsys.readouterr().out == "flex_amount_export_failed\n"


def test_pending_exhaustion_never_regenerates_or_saves_handle(monkeypatch, tmp_path, capsys):
    session, delays = setup_cli(monkeypatch, tmp_path, SEND, *([PENDING] * 6))
    assert cli.main(("--export-once",)) == 1
    assert len(session.calls) == 7 and delays == [1] + [3] * 5
    assert sum(name == "SendRequest" for name, _ in session.calls) == 1
    assert not list(tmp_path.iterdir()) and REFERENCE not in capsys.readouterr().out


def test_unexpected_exception_and_debug_logging_never_leak(monkeypatch, tmp_path, capsys):
    setup_cli(monkeypatch, tmp_path)
    monkeypatch.setattr(http.client.HTTPConnection, "debuglevel", 1)
    original_logging = logging.root.manager.disable
    def fail(**kwargs):
        assert http.client.HTTPConnection.debuglevel == 0
        logging.error("private token=%s", TOKEN)
        raise RuntimeError(f"{TOKEN} {REFERENCE} amount=3456.123456")
    monkeypatch.setattr(cli, "request_activity_flex_report", fail)
    assert cli.main(("--export-once",)) == 1
    captured = capsys.readouterr()
    assert captured.out == "flex_amount_export_failed\n" and captured.err == ""
    assert logging.root.manager.disable == original_logging
    assert http.client.HTTPConnection.debuglevel == 1 and not list(tmp_path.iterdir())


@pytest.mark.parametrize("symlink", [False, True])
def test_existing_output_is_not_overwritten_or_uploaded(monkeypatch, tmp_path, symlink):
    session, _ = setup_cli(monkeypatch, tmp_path)
    path = tmp_path / cli.ARTIFACT_NAME
    if symlink:
        path.symlink_to(tmp_path / "missing")
    else:
        path.write_text("previous")
    assert cli.main(("--export-once",)) == 1 and session.calls == []
    assert path.is_symlink() if symlink else path.read_text() == "previous"


def test_partial_write_is_removed_and_reports_only_fixed_failure(monkeypatch, tmp_path, capsys):
    setup_cli(monkeypatch, tmp_path, SEND, XML)
    original = os.fdopen
    class BrokenWriter:
        def __init__(self, fd, *args, **kwargs):
            self.stream = original(fd, *args, **kwargs)
        def __enter__(self):
            return self
        def write(self, content):
            self.stream.write(content[:100])
            raise OSError(TOKEN)
        def __exit__(self, *args):
            self.stream.close()
    monkeypatch.setattr(cli.os, "fdopen", BrokenWriter)
    assert cli.main(("--export-once",)) == 1
    assert not list(tmp_path.iterdir()) and capsys.readouterr().out == "flex_amount_export_failed\n"


def test_workflow_export_is_explicit_exclusive_and_success_only():
    text = Path(".github/workflows/diagnose-ibkr-flex-readonly.yml").read_text()
    inputs = text.split("permissions:")[0]
    assert "export_amounts:" in inputs
    assert "default: false" in inputs.split("export_amounts:")[1].split("cashflow_period_from:")[0]
    assert '"$EXECUTION_SOURCE_ONLY" "$EXPORT_AMOUNTS"; do' in text
    assert text.index("--preflight") < text.index("--export-once")
    assert "github.ref == 'refs/heads/main'" in text and "github.run_attempt == 1" in text
    upload = text.split("      - name: Upload only")[1].split("      - name: Remove")[0]
    assert "success() && inputs.export_amounts && steps.read.outcome == 'success'" in upload
    assert "path: ${{ runner.temp }}/ibkr-flex-redacted-amounts.json" in upload
    for required in ("retention-days: 1", "if-no-files-found: error", "include-hidden-files: false", "overwrite: false"):
        assert required in upload
    assert "always() && inputs.export_amounts" in text
    assert text.count("actions/upload-artifact@") == 1
    for forbidden in ("continue-on-error", "schedule:", "id-token:", "contents: write", "owner-approved", "cat ", "tee "):
        assert forbidden not in text
