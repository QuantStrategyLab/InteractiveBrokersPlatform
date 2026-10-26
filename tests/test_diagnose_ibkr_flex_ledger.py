import json
import hashlib

import pytest

from application import ibkr_flex_source
from application.ibkr_flex_source import FlexReport
from scripts import diagnose_ibkr_flex_ledger as cli


def _set_input(monkeypatch, *, enabled="true"):
    monkeypatch.setenv("IBKR_FLEX_DIAGNOSTIC_ENABLED", enabled)
    monkeypatch.setenv("IBKR_FLEX_TOKEN", "synthetic-token")
    monkeypatch.setenv("IBKR_FLEX_QUERY_ID", "12345")
    monkeypatch.setenv("IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON", '["DU0000000"]')
    monkeypatch.delenv("IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON", raising=False)
    monkeypatch.delenv("IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON", raising=False)


def test_cli_is_disabled_by_default_and_does_not_fetch(monkeypatch, capsys):
    monkeypatch.delenv("IBKR_FLEX_DIAGNOSTIC_ENABLED", raising=False)
    monkeypatch.setattr(cli, "diagnose_activity_flex_ledger", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("network path called")))

    assert cli.main() == 2

    output = capsys.readouterr().out
    assert json.loads(output) == {
        "schema_version": "ibkr_flex_diagnostic.v1",
        "status": "disabled",
        "method": None,
        "period": {"from": None, "to": None},
        "currency": None,
        "missing": ["flex_import_not_enabled"],
        "warnings": [],
    }


def test_pending_diagnostic_retains_only_fixed_status(monkeypatch, capsys):
    _set_input(monkeypatch)
    request = ibkr_flex_source.FlexReportRequest(reference_code="987654321098765", token="synthetic-token")
    def pending(**_kwargs):
        raise ibkr_flex_source.FlexReportPending(request)
    monkeypatch.setattr(cli, "diagnose_activity_flex_ledger", pending)
    assert cli.main() == 1
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["status"] == "pending" and result["missing"] == ["flex_generation_pending"]
    assert request.reference_code not in output and request.token not in output


def test_cli_reports_missing_inputs_without_fetching(monkeypatch, capsys):
    _set_input(monkeypatch)
    monkeypatch.delenv("IBKR_FLEX_TOKEN")
    monkeypatch.setattr(cli, "diagnose_activity_flex_ledger", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("network path called")))

    assert cli.main() == 2

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "incomplete"
    assert result["missing"] == ["flex_token_missing"]


def test_cli_calls_diagnostic_once_and_prints_only_sanitized_result(monkeypatch, capsys):
    _set_input(monkeypatch)
    observed = {}
    safe_result = {
        "schema_version": "ibkr_flex_diagnostic.v1",
        "status": "available",
        "method": "native_ibkr_twr",
        "period": {"from": "2026-01-01", "to": "2026-01-31"},
        "currency": "USD",
        "missing": [],
        "warnings": [],
    }

    def fake_diagnose(**kwargs):
        observed.update(kwargs)
        return safe_result

    monkeypatch.setattr(cli, "diagnose_activity_flex_ledger", fake_diagnose)

    assert cli.main() == 0

    output = capsys.readouterr().out
    assert json.loads(output) == safe_result
    assert observed == {
        "token": "synthetic-token",
        "query_id": "12345",
        "expected_account_ids": ("DU0000000",),
    }
    for private_value in ("synthetic-token", "DU0000000", "1.25", "1000.00"):
        assert private_value not in output


def test_cli_full_synthetic_fetch_parse_redacts_period_alias(monkeypatch, capsys):
    _set_input(monkeypatch)
    private_period_label = "SyntheticPrivateAlias9"
    xml = (
        '<FlexQueryResponse type="AF"><FlexStatements count="1">'
        '<FlexStatement accountId="DU0000000" fromDate="20260101" toDate="20260131" '
        f'period="{private_period_label}">'
        '<AccountInformation accountId="DU0000000" currency="USD"/>'
        '<EquitySummaryInBase><EquitySummaryByReportDateInBase accountId="DU0000000" '
        'currency="USD" reportDate="20260131" total="11250.00"/></EquitySummaryInBase>'
        '<StmtFunds><StatementOfFundsLine accountId="DU0000000" currency="USD" '
        'date="20260115" activityCode="DEP" amount="1000.00"/></StmtFunds>'
        '<ChangeInNAV accountId="DU0000000" fromDate="20260101" toDate="20260131" '
        'startingValue="10000.00" endingValue="11250.00" depositsWithdrawals="1000.00" twr="1.25"/>'
        '</FlexStatement></FlexStatements></FlexQueryResponse>'
    ).encode()
    report = FlexReport(content_sha256=hashlib.sha256(xml).hexdigest(), content=xml)
    monkeypatch.setattr(ibkr_flex_source, "fetch_activity_flex_xml", lambda **_kwargs: report)

    assert cli.main() == 0

    output = capsys.readouterr().out
    result = json.loads(output)
    assert result == {
        "schema_version": "ibkr_flex_diagnostic.v1",
        "status": "available",
        "method": "native_ibkr_twr",
        "period": {"from": "2026-01-01", "to": "2026-01-31"},
        "currency": "USD",
        "missing": [],
        "warnings": [],
    }
    assert private_period_label not in output
    for private_value in ("DU0000000", "11250.00", "1000.00", "1.25", "0.0125"):
        assert private_value not in output
    private_ledger = ibkr_flex_source.import_activity_flex_ledger(
        token="synthetic-token",
        query_id="12345",
        expected_account_ids=("DU0000000",),
    )
    assert private_ledger["period"]["period"] == private_period_label


def test_cli_rejects_malformed_scope_without_fetching(monkeypatch, capsys):
    _set_input(monkeypatch)
    monkeypatch.setenv("IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON", '["DU0000000", 1]')
    monkeypatch.setattr(cli, "diagnose_activity_flex_ledger", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("network path called")))

    assert cli.main() == 2

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "incomplete"
    assert result["missing"] == ["expected_account_scope_missing_or_invalid"]


def test_cli_does_not_print_private_exception_text(monkeypatch, capsys):
    _set_input(monkeypatch)
    monkeypatch.setattr(
        cli,
        "diagnose_activity_flex_ledger",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("URL token=secret DU0000000 NAV=12345 TWR=1.25")),
    )

    assert cli.main() == 1

    output = capsys.readouterr().out
    assert json.loads(output)["missing"] == ["flex_import_failed"]
    for private_value in ("secret", "DU0000000", "12345", "1.25", "token="):
        assert private_value not in output


def _cash_report():
    xml = ('<FlexQueryResponse type="AF"><FlexStatements count="1">'
        '<FlexStatement accountId="DU0000000" fromDate="20260101" toDate="20260131">'
        '<CashReport><CashReportCurrency currency="USD" endingSettledCash="678.123456" '
        'privateKey="PRIVATE_CANARY"/></CashReport></FlexStatement></FlexStatements></FlexQueryResponse>').encode()
    return FlexReport(content=xml, content_sha256=hashlib.sha256(xml).hexdigest())


def test_cash_cli_one_fetch_is_independent_of_missing_twr_and_never_prints_money(monkeypatch, capsys):
    _set_input(monkeypatch)
    calls = []
    def fetch(**kwargs):
        calls.append(kwargs)
        return _cash_report()
    monkeypatch.setattr(ibkr_flex_source, "fetch_activity_flex_xml", fetch)
    assert cli.main(("--cash-read-once",)) == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert len(calls) == 1
    assert result["schema_version"] == "ibkr_flex_cash_diagnostic.v1"
    assert result["cash_report"]["usd_field_type_counts"]["endingSettledCash"] == {"decimal_text": 1}
    assert result["cash_report"]["spendable_cash_verified"] is False
    assert all(v not in output for v in ("DU0000000", "678.123456", "PRIVATE_CANARY", "privateKey", "synthetic-token"))


def test_cash_stdout_accepts_verified_period_end_without_current_cash_authority():
    from application.ibkr_flex_ledger import build_flex_cash_diagnostic, build_flex_ledger
    report = _cash_report()
    content = report.content.replace(b'currency="USD" endingSettledCash=',
        b'accountId="DU0000000" model="" fromDate="20260101" toDate="20260131" currency="USD" endingCash="0" endingSettledCash=')
    report = FlexReport(content=content, content_sha256=hashlib.sha256(content).hexdigest())
    result = cli._validated_cash_report(build_flex_cash_diagnostic(build_flex_ledger(report, expected_account_ids=("DU0000000",))))
    assert result["period_cash"] == {"status": "available_period_end", "missing": []}
    assert result["cash_report"]["spendable_cash_verified"] is False


@pytest.mark.parametrize("raw,expected", [("Cash", "CASH"), ("Margin", "MARGIN"),
    ("Portfolio Margin", "PORTFOLIO_MARGIN"), ("", "MISSING"), (None, "MISSING"),
    ("PRIVATE_CAPABILITY_CANARY", "UNRECOGNIZED")])
def test_cash_cli_reports_only_statement_account_capability_enum(monkeypatch, capsys, raw, expected):
    _set_input(monkeypatch)
    report = _cash_report()
    attribute = f' accountCapabilities="{raw}"' if raw is not None else ''
    info = f'<AccountInformation accountId="DU0000000" accountType="Margin" currency="USD"{attribute}/>'
    content = report.content.replace(b'<CashReport>', info.encode() + b'<CashReport>')
    report = FlexReport(content=content, content_sha256=hashlib.sha256(content).hexdigest())
    monkeypatch.setattr(ibkr_flex_source, "fetch_activity_flex_xml", lambda **_: report)
    assert cli.main(("--cash-read-once",)) == 0
    output = capsys.readouterr().out
    value = json.loads(output)
    assert value["account_capabilities"] == expected
    assert value["scope"] == "STATEMENT_PERIOD_END_ONLY"
    assert value["cash_report"]["spendable_cash_verified"] is False
    assert all(text not in output for text in ("PRIVATE_CAPABILITY_CANARY", "DU0000000", "678.123456", "synthetic-token"))


@pytest.mark.parametrize("foreign", [False, True])
def test_account_capability_never_selects_first_duplicate_or_foreign_row(foreign):
    from application.ibkr_flex_ledger import build_flex_cash_diagnostic, build_flex_ledger
    from application.ibkr_flex_source import FlexSourceError
    report = _cash_report()
    info = b'<AccountInformation accountId="DU0000000" currency="USD" accountCapabilities="Cash"/>'
    info += b'<AccountInformation accountId="DU0000001" currency="USD" accountCapabilities="Margin"/>' if foreign else info
    content = report.content.replace(b'<CashReport>', info + b'<CashReport>')
    report = FlexReport(content=content, content_sha256=hashlib.sha256(content).hexdigest())
    if foreign:
        with pytest.raises(FlexSourceError):
            build_flex_ledger(report, expected_account_ids=("DU0000000",))
    else:
        result = build_flex_cash_diagnostic(build_flex_ledger(report, expected_account_ids=("DU0000000",)))
        assert result["account_capabilities"] == "AMBIGUOUS"


@pytest.mark.parametrize("raw", ["PRIVATE_CAPABILITY_CANARY", None, True, ["CASH"]])
def test_cash_stdout_rejects_unvalidated_capability_values(raw):
    from application.ibkr_flex_ledger import build_flex_cash_diagnostic, build_flex_ledger
    value = build_flex_cash_diagnostic(build_flex_ledger(_cash_report(), expected_account_ids=("DU0000000",)))
    value["account_capabilities"] = raw
    result = cli._validated_cash_report(value)
    assert result["missing"] == ["flex_cash_diagnostic_result_invalid"]
    assert "PRIVATE_CAPABILITY_CANARY" not in json.dumps(result)


@pytest.mark.parametrize("change", ["digest", "period", "field", "type", "count", "boolean", "cash_fact_code", "cash_fact_status"])
def test_cash_stdout_rejects_invalid_or_private_dynamic_fields(change):
    from application.ibkr_flex_ledger import build_flex_cash_diagnostic, build_flex_ledger
    value = build_flex_cash_diagnostic(build_flex_ledger(_cash_report(), expected_account_ids=("DU0000000",)))
    shape = value["cash_report"]
    if change == "digest":
        value["source_ref"] = "PRIVATE_CANARY"
    elif change == "period":
        value["period"]["from"] = "PRIVATE_CANARY"
    elif change == "field":
        shape["usd_field_type_counts"]["PRIVATE_CANARY"] = {"text": 1}
    elif change == "type":
        shape["usd_field_type_counts"]["endingCash"] = {"PRIVATE_CANARY": 1}
    elif change == "count":
        shape["usd_field_type_counts"]["endingCash"] = {"text": 2}
    elif change == "boolean":
        shape["usd_record_count"] = True
    elif change == "cash_fact_code":
        value["period_cash"]["missing"] = ["PRIVATE_CANARY"]
    else:
        value["period_cash"]["status"] = "available_period_end"
    result = cli._validated_cash_report(value)
    assert result["missing"] == ["flex_cash_diagnostic_result_invalid"]
    assert "PRIVATE_CANARY" not in json.dumps(result)


def test_cash_cli_period_mismatch_stops_and_errors_remain_private(monkeypatch, capsys):
    _set_input(monkeypatch)
    monkeypatch.setenv("IBKR_FLEX_EXPECTED_PERIOD_FROM", "2026-02-01")
    monkeypatch.setenv("IBKR_FLEX_EXPECTED_PERIOD_TO", "2026-02-28")
    monkeypatch.setattr(ibkr_flex_source, "fetch_activity_flex_xml", lambda **_: _cash_report())
    assert cli.main(("--cash-read-once",)) == 1
    assert json.loads(capsys.readouterr().out)["missing"] == ["expected_period_mismatch"]


def test_cash_pending_collects_the_original_handle_without_regenerating(monkeypatch, capsys):
    _set_input(monkeypatch)
    request = ibkr_flex_source.FlexReportRequest(reference_code="987654321098765", token="synthetic-token")
    calls = []
    def pending(**_):
        calls.append("generate")
        raise ibkr_flex_source.FlexReportPending(request)
    def collect(handle, **_):
        calls.append(("collect", handle))
        from application.ibkr_flex_ledger import build_flex_ledger
        return build_flex_ledger(_cash_report(), expected_account_ids=("DU0000000",))
    monkeypatch.setattr(ibkr_flex_source, "fetch_activity_flex_xml", pending)
    monkeypatch.setattr(ibkr_flex_source, "collect_pending_activity_flex_ledger", collect)
    assert cli.main(("--cash-read-once",)) == 0
    assert calls == ["generate", ("collect", request)]
    assert request.reference_code not in capsys.readouterr().out
