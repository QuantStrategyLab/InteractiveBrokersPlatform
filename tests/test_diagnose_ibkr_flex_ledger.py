import json
import hashlib

from application import ibkr_flex_source
from application.ibkr_flex_source import FlexReport
from scripts import diagnose_ibkr_flex_ledger as cli


def _set_input(monkeypatch, *, enabled="true"):
    monkeypatch.setenv("IBKR_FLEX_DIAGNOSTIC_ENABLED", enabled)
    monkeypatch.setenv("IBKR_FLEX_TOKEN", "synthetic-token")
    monkeypatch.setenv("IBKR_FLEX_QUERY_ID", "12345")
    monkeypatch.setenv("IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON", '["DU0000000"]')


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
