import hashlib
import json

import pytest

from application.ibkr_flex_ledger import build_flex_ledger
from application import ibkr_flex_source
from application.ibkr_flex_source import (
    FlexReport,
    FlexSourceError,
    import_activity_flex_ledger,
)


ACCOUNT = "DU0000000"


def _wrap(*statements: str, report_type: str = "AF") -> FlexReport:
    body = "".join(statements)
    xml = (
        f'<FlexQueryResponse queryName="ledger" type="{report_type}">'
        f'<FlexStatements count="{len(statements)}">{body}</FlexStatements>'
        "</FlexQueryResponse>"
    ).encode()
    return FlexReport(content_sha256=hashlib.sha256(xml).hexdigest(), content=xml)


def _statement(
    inner: str,
    *,
    account: str = ACCOUNT,
    from_date: str = "20260101",
    to_date: str = "20260131",
    period: str = "LastMonth",
) -> str:
    return (
        f'<FlexStatement accountId="{account}" fromDate="{from_date}" toDate="{to_date}" '
        f'period="{period}" whenGenerated="20260201;120000">{inner}</FlexStatement>'
    )


def test_extracts_period_valuation_flows_fees_and_native_twr():
    report = _wrap(_statement(
        f'<AccountInformation accountId="{ACCOUNT}" currency="USD"/>'
        "<EquitySummaryInBase>"
        f'<EquitySummaryByReportDateInBase accountId="{ACCOUNT}" currency="USD" '
        'reportDate="20260115" total="11000.00"/>'
        f'<EquitySummaryByReportDateInBase accountId="{ACCOUNT}" currency="USD" '
        'reportDate="20260131" total="12345.67"/>'
        "</EquitySummaryInBase>"
        "<StmtFunds>"
        f'<StatementOfFundsLine accountId="{ACCOUNT}" currency="USD" date="20260105" '
        'activityCode="DEP" amount="1000.00"/>'
        f'<StatementOfFundsLine accountId="{ACCOUNT}" currency="USD" date="20260120" '
        'activityCode="WITH" amount="-250.00"/>'
        f'<StatementOfFundsLine accountId="{ACCOUNT}" currency="USD" date="20260131" '
        'activityCode="OFEE" amount="-2.50"/>'
        "</StmtFunds>"
        "<ConversionRates>"
        '<ConversionRate reportDate="20260131" fromCurrency="EUR" toCurrency="USD" rate="1.0850"/>'
        "</ConversionRates>"
        f'<ChangeInNAV accountId="{ACCOUNT}" fromDate="20260101" toDate="20260131" '
        'startingValue="11000" endingValue="12345.67" depositsWithdrawals="750" twr="1.25"/>'
    ))

    ledger = build_flex_ledger(report, expected_account_ids=(ACCOUNT,))

    assert ledger["schema_version"] == "ibkr_flex_ledger.v1"
    assert ledger["account_ids"] == [ACCOUNT]
    assert ledger["currency"] == "USD"
    assert ledger["account_base_currency"] == "USD"
    assert ledger["period"] == {"from": "2026-01-01", "to": "2026-01-31", "period": "LastMonth"}
    assert ledger["ending_valuation"] == {
        "amount": "12345.67",
        "currency": "USD",
        "report_date": "2026-01-31",
        "source": "EquitySummaryByReportDateInBase.total",
    }
    assert [(flow["direction"], flow["amount"]) for flow in ledger["external_flows"]] == [
        ("deposit", "1000.00"),
        ("withdrawal", "-250.00"),
    ]
    assert ledger["net_external_flow"] == {"amount": "750.00", "currency": "USD", "line_count": 2}
    assert ledger["fee_lines"] == [{
        "date": "2026-01-31",
        "activity_code": "OFEE",
        "kind": "fee",
        "currency": "USD",
        "amount": "-2.50",
    }]
    assert ledger["fee_total"] == {"amount": "-2.50", "currency": "USD", "line_count": 1}
    assert ledger["fx_rates"] == [{
        "from_currency": "EUR",
        "to_currency": "USD",
        "rate": "1.0850",
        "report_date": "2026-01-31",
    }]
    assert ledger["native_returns"] == {
        "twr": "1.25",
        "unit": "percent",
        "from": "2026-01-01",
        "to": "2026-01-31",
        "source": "ChangeInNAV.twr",
    }
    assert ledger["return_assessment"] == {
        "computable": True,
        "method": "native_ibkr_twr",
        "value": "0.0125",
        "unit": "ratio",
        "source": "ChangeInNAV.twr",
        "missing": [],
    }
    assert ledger["warnings"] == []


def test_fetch_importer_and_redacted_report_keep_deposit_separate_from_native_twr(monkeypatch):
    report = _wrap(_statement(
        f'<AccountInformation accountId="{ACCOUNT}" currency="USD"/>'
        "<EquitySummaryInBase>"
        f'<EquitySummaryByReportDateInBase accountId="{ACCOUNT}" currency="USD" '
        'reportDate="20260131" total="11250.00"/>'
        "</EquitySummaryInBase>"
        "<StmtFunds>"
        f'<StatementOfFundsLine accountId="{ACCOUNT}" currency="USD" date="20260115" '
        'activityCode="DEP" amount="1000.00"/>'
        "</StmtFunds>"
        f'<ChangeInNAV accountId="{ACCOUNT}" fromDate="20260101" toDate="20260131" '
        'startingValue="10000.00" endingValue="11250.00" depositsWithdrawals="1000.00" twr="1.25"/>'
    ))
    observed = {}

    def fake_fetch(*, token, query_id, session=None):
        observed.update(token=token, query_id=query_id, session=session)
        return report

    monkeypatch.setattr(ibkr_flex_source, "fetch_activity_flex_xml", fake_fetch)
    ledger = import_activity_flex_ledger(
        token="synthetic-secret",
        query_id="12345",
        expected_account_ids=(ACCOUNT,),
    )

    assert observed == {"token": "synthetic-secret", "query_id": "12345", "session": None}
    assert ledger["external_flows"][0]["amount"] == "1000.00"
    assert ledger["return_assessment"]["value"] == "0.0125"
    assert ledger["return_assessment"]["value"] != "0.125"

    diagnostic = ibkr_flex_source.diagnose_activity_flex_ledger(
        token="synthetic-secret",
        query_id="12345",
        expected_account_ids=(ACCOUNT,),
    )
    assert diagnostic == {
        "schema_version": "ibkr_flex_diagnostic.v1",
        "status": "available",
        "method": "native_ibkr_twr",
        "period": {"from": "2026-01-01", "to": "2026-01-31"},
        "currency": "USD",
        "missing": [],
        "warnings": [],
    }
    serialized = json.dumps(diagnostic)
    for private_value in (ACCOUNT, "1000.00", "11250.00", "1.25", "0.0125"):
        assert private_value not in serialized
    assert "amount" not in diagnostic
    assert "account_ids" not in diagnostic


def test_fees_are_not_double_counted_across_sections():
    report = _wrap(_statement(
        f'<AccountInformation accountId="{ACCOUNT}" currency="USD"/>'
        "<CashReport>"
        f'<CashReportCurrency accountId="{ACCOUNT}" currency="USD" commissions="9.99" '
        'otherFees="5.00" endingCash="100"/>'
        "</CashReport>"
        "<Trades>"
        f'<Trade accountId="{ACCOUNT}" currency="USD" symbol="AAA" ibCommission="-9.99"/>'
        "</Trades>"
        "<StmtFunds>"
        f'<StatementOfFundsLine accountId="{ACCOUNT}" currency="USD" date="20260110" '
        'activityCode="BUY" symbol="AAA" amount="-1000" tradeCommission="-1.25"/>'
        f'<StatementOfFundsLine accountId="{ACCOUNT}" currency="USD" date="20260131" '
        'activityCode="OFEE" amount="-2.50"/>'
        "</StmtFunds>"
    ))

    ledger = build_flex_ledger(report, expected_account_ids=(ACCOUNT,))

    assert ledger["fee_total"] == {"amount": "-3.75", "currency": "USD", "line_count": 2}
    assert [(line["kind"], line["amount"]) for line in ledger["fee_lines"]] == [
        ("trade_commission", "-1.25"),
        ("fee", "-2.50"),
    ]


def test_mixed_currency_flows_are_not_silently_merged():
    report = _wrap(_statement(
        f'<AccountInformation accountId="{ACCOUNT}" currency="USD"/>'
        "<StmtFunds>"
        f'<StatementOfFundsLine accountId="{ACCOUNT}" currency="USD" date="20260105" '
        'activityCode="DEP" amount="1000"/>'
        f'<StatementOfFundsLine accountId="{ACCOUNT}" currency="EUR" date="20260106" '
        'activityCode="DEP" amount="500"/>'
        "</StmtFunds>"
    ))

    ledger = build_flex_ledger(report, expected_account_ids=(ACCOUNT,))

    assert ledger["currency"] == "USD"
    assert [flow["currency"] for flow in ledger["external_flows"]] == ["USD", "EUR"]
    assert ledger["net_external_flow"] is None
    assert {"code": "external_flows_mixed_currency"} in ledger["warnings"]
    assert "external_flows_mixed_currency" in ledger["return_assessment"]["missing"]


def test_rejects_account_scope_mismatch():
    report = _wrap(_statement(
        '<AccountInformation accountId="DU9999999" currency="USD"/>',
        account="DU9999999",
    ))
    with pytest.raises(FlexSourceError):
        build_flex_ledger(report, expected_account_ids=(ACCOUNT,))


def test_rejects_multiple_statement_accounts():
    inner = '<AccountInformation accountId="{id}" currency="USD"/>'
    report = _wrap(
        _statement(inner.format(id="DU0000000"), account="DU0000000"),
        _statement(inner.format(id="DU1111111"), account="DU1111111"),
    )
    with pytest.raises(FlexSourceError):
        build_flex_ledger(report, expected_account_ids=("DU0000000", "DU1111111"))


def test_rejects_non_activity_report():
    report = _wrap(
        _statement(f'<AccountInformation accountId="{ACCOUNT}" currency="USD"/>'),
        report_type="TCF",
    )
    with pytest.raises(FlexSourceError):
        build_flex_ledger(report, expected_account_ids=(ACCOUNT,))


def test_missing_valuation_is_reported_not_invented():
    report = _wrap(_statement(
        f'<AccountInformation accountId="{ACCOUNT}" currency="USD"/>'
        "<StmtFunds>"
        f'<StatementOfFundsLine accountId="{ACCOUNT}" currency="USD" date="20260105" '
        'activityCode="DEP" amount="100"/>'
        "</StmtFunds>"
    ))

    ledger = build_flex_ledger(report, expected_account_ids=(ACCOUNT,))

    assert ledger["ending_valuation"] is None
    assert ledger["return_assessment"]["computable"] is False
    assert "ending_valuation_unavailable" in ledger["return_assessment"]["missing"]


def test_bad_amount_marks_flows_incomplete():
    report = _wrap(_statement(
        f'<AccountInformation accountId="{ACCOUNT}" currency="USD"/>'
        "<EquitySummaryInBase>"
        f'<EquitySummaryByReportDateInBase accountId="{ACCOUNT}" currency="USD" '
        'reportDate="20260131" total="100"/>'
        "</EquitySummaryInBase>"
        "<StmtFunds>"
        f'<StatementOfFundsLine accountId="{ACCOUNT}" currency="USD" date="20260105" '
        'activityCode="DEP" amount="100"/>'
        f'<StatementOfFundsLine accountId="{ACCOUNT}" currency="USD" date="20260106" '
        'activityCode="DEP" amount="not-a-number"/>'
        "</StmtFunds>"
    ))

    ledger = build_flex_ledger(report, expected_account_ids=(ACCOUNT,))

    assert ledger["net_external_flow"] is None
    assert {"code": "invalid_external_flow", "activity_code": "DEP"} in ledger["warnings"]
    assert "external_flows_incomplete" in ledger["return_assessment"]["missing"]


def test_native_twr_period_mismatch_is_not_retained():
    report = _wrap(_statement(
        f'<AccountInformation accountId="{ACCOUNT}" currency="USD"/>'
        f'<ChangeInNAV accountId="{ACCOUNT}" fromDate="20260101" toDate="20260115" twr="0.02"/>'
    ))

    ledger = build_flex_ledger(report, expected_account_ids=(ACCOUNT,))

    assert ledger["native_returns"] == {}
    assert {"code": "native_twr_period_mismatch"} in ledger["warnings"]
    assert ledger["return_assessment"]["computable"] is False
    assert "native_twr_unavailable" in ledger["return_assessment"]["missing"]


def test_missing_period_end_blocks_native_return_and_valuation():
    statement = (
        f'<FlexStatement accountId="{ACCOUNT}" fromDate="20260101" period="LastMonth">'
        f'<AccountInformation accountId="{ACCOUNT}" currency="USD"/>'
        "<EquitySummaryInBase>"
        f'<EquitySummaryByReportDateInBase accountId="{ACCOUNT}" currency="USD" '
        'reportDate="20260131" total="100"/>'
        "</EquitySummaryInBase>"
        f'<ChangeInNAV accountId="{ACCOUNT}" fromDate="20260101" toDate="20260131" twr="1.25"/>'
        "</FlexStatement>"
    )
    report = _wrap(statement)

    ledger = build_flex_ledger(report, expected_account_ids=(ACCOUNT,))

    assert ledger["period"]["to"] is None
    assert ledger["ending_valuation"] is None
    assert ledger["native_returns"] == {}
    assert {"code": "native_twr_period_unavailable"} in ledger["warnings"]
    assert ledger["return_assessment"]["computable"] is False
    assert "ending_valuation_unavailable" in ledger["return_assessment"]["missing"]


def test_reversed_period_blocks_native_return_and_valuation():
    report = _wrap(_statement(
        f'<AccountInformation accountId="{ACCOUNT}" currency="USD"/>'
        "<EquitySummaryInBase>"
        f'<EquitySummaryByReportDateInBase accountId="{ACCOUNT}" currency="USD" '
        'reportDate="20260101" total="100"/>'
        "</EquitySummaryInBase>"
        f'<ChangeInNAV accountId="{ACCOUNT}" fromDate="20260131" toDate="20260101" twr="1.25"/>',
        from_date="20260131",
        to_date="20260101",
    ))

    ledger = build_flex_ledger(report, expected_account_ids=(ACCOUNT,))

    assert ledger["ending_valuation"] is None
    assert ledger["native_returns"] == {}
    assert {"code": "native_twr_period_unavailable"} in ledger["warnings"]
    assert ledger["return_assessment"]["computable"] is False


def test_cross_period_valuation_is_not_called_ending():
    report = _wrap(_statement(
        f'<AccountInformation accountId="{ACCOUNT}" currency="USD"/>'
        "<EquitySummaryInBase>"
        f'<EquitySummaryByReportDateInBase accountId="{ACCOUNT}" currency="USD" '
        'reportDate="20260215" total="100"/>'
        "</EquitySummaryInBase>"
    ))

    ledger = build_flex_ledger(report, expected_account_ids=(ACCOUNT,))

    assert ledger["period"]["to"] == "2026-01-31"
    assert ledger["ending_valuation"] is None
    assert "ending_valuation_unavailable" in ledger["return_assessment"]["missing"]


def test_non_usd_base_currency_is_not_relabelled():
    report = _wrap(_statement(
        f'<AccountInformation accountId="{ACCOUNT}" currency="EUR"/>'
        "<EquitySummaryInBase>"
        f'<EquitySummaryByReportDateInBase accountId="{ACCOUNT}" currency="EUR" '
        'reportDate="20260131" total="100"/>'
        "</EquitySummaryInBase>"
    ))

    ledger = build_flex_ledger(report, expected_account_ids=(ACCOUNT,))

    assert ledger["currency"] is None
    assert ledger["account_base_currency"] == "EUR"
    assert {"code": "account_base_currency_not_usd"} in ledger["warnings"]
