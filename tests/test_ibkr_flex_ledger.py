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


def test_cash_report_shape_uses_only_target_usd_rows_without_amounts_or_qualification():
    from application.ibkr_flex_ledger import build_flex_cash_diagnostic
    report = _wrap(_statement('<CashReport>'
        '<CashReportCurrency currency="BASE_SUMMARY" endingSettledCash="555.55"/>'
        '<CashReportCurrency currency="USD" endingCash="123.456" endingSettledCash="98.765" '
        'fromDate="20260101" toDate="20260131" reportDate="20260131" privateKey="private-value"/>'
        '<PrivateChild privateKey="private-value"/></CashReport>'),
        _statement('<CashReport><CashReportCurrency currency="USD" endingSettledCash="987654.321"/></CashReport>', account="DU0000001"))
    ledger = build_flex_ledger(report, expected_account_ids=(ACCOUNT,), allowed_report_account_ids=(ACCOUNT, "DU0000001"))
    result = build_flex_cash_diagnostic(ledger)
    shape = result["cash_report"]
    assert result["source_ref"] == report.content_sha256
    assert result["period"] == {"from": "2026-01-01", "to": "2026-01-31"}
    assert shape["record_count"] == 2 and shape["usd_record_count"] == 1
    assert shape["unsupported_record_count"] == shape["unknown_field_count"] == 1
    assert shape["usd_field_type_counts"]["endingSettledCash"] == {"decimal_text": 1}
    assert shape["usd_field_type_counts"]["fromDate"] == {"date_text": 1}
    assert shape["coverage"] == "not_evaluated"
    assert shape["spendable_cash_verified"] is shape["complete_cash_obligations_verified"] is False
    assert shape["missing_fields_are_zero"] is False
    assert all(v not in json.dumps(result) for v in (ACCOUNT, "DU0000001", "123.456", "98.765", "555.55", "987654.321", "privateKey", "private-value", "PrivateChild"))


@pytest.mark.parametrize("section,count", [("", 0), ("<CashReport/>", 1),
    ('<CashReport><CashReportCurrency currency="BASE_SUMMARY" endingSettledCash="100"/></CashReport>', 1)])
def test_cash_shape_distinguishes_absent_empty_and_no_native_usd(section, count):
    shape = build_flex_ledger(_wrap(_statement(section)), expected_account_ids=(ACCOUNT,))["cash_report_source_diagnostic"]
    assert shape["section_count"] == count and shape["usd_record_count"] == 0
    assert shape["usd_field_type_counts"] == {}


def test_cash_shape_preserves_duplicate_malformed_and_missing_fields():
    report = _wrap(_statement('<CashReport>'
        '<CashReportCurrency currency="USD" endingSettledCash="NaN" toDate="20260230"/>'
        '<CashReportCurrency currency="USD" endingSettledCash="1e10000"/>'
        '<CashReportCurrency currency="USD" endingCash=""/></CashReport>'))
    shape = build_flex_ledger(report, expected_account_ids=(ACCOUNT,))["cash_report_source_diagnostic"]
    assert shape["usd_record_count"] == 3
    assert shape["usd_field_type_counts"]["endingSettledCash"] == {"missing": 1, "nonfinite_text": 1, "scientific_text": 1}
    assert shape["usd_field_type_counts"]["endingCash"] == {"empty_text": 1, "missing": 2}
    assert shape["usd_field_type_counts"]["toDate"] == {"missing": 2, "unsupported_text": 1}


def test_cash_row_cannot_belong_to_another_approved_report_account():
    report = _wrap(_statement('<CashReport><CashReportCurrency accountId="DU0000001" currency="USD" endingSettledCash="10"/></CashReport>'))
    with pytest.raises(FlexSourceError):
        build_flex_ledger(report, expected_account_ids=(ACCOUNT,), allowed_report_account_ids=(ACCOUNT, "DU0000001"))


def _native_cash_row(**overrides):
    attrs = {"accountId": ACCOUNT, "currency": "USD", "model": "", "fromDate": "20260101",
             "toDate": "20260131", "endingCash": "12.000000000000001", "endingSettledCash": "-2.000000000000002"}
    attrs.update(overrides)
    return '<CashReportCurrency ' + ' '.join(f'{key}="{value}"' for key, value in attrs.items() if value is not None) + '/>'


def test_native_period_cash_preserves_sign_precision_and_original_dates_without_current_authority():
    from application.ibkr_flex_ledger import build_flex_cash_diagnostic
    report = _wrap(_statement('<CashReport>' + _native_cash_row() + '</CashReport>'))
    ledger = build_flex_ledger(report, expected_account_ids=(ACCOUNT,))
    facts = ledger["period_cash_facts"]
    assert facts["status"] == "available_period_end" and facts["missing"] == []
    assert facts["source_ref"] == report.content_sha256 and facts["account_ids"] == [ACCOUNT]
    assert facts["values"] == {"endingCash": "12.000000000000001", "endingSettledCash": "-2.000000000000002"}
    assert facts["period"] == {"from": "2026-01-01", "to": "2026-01-31"}
    assert facts["spendable_cash_verified"] is facts["complete_cash_obligations_verified"] is False
    safe = build_flex_cash_diagnostic(ledger)
    assert safe["period_cash"] == {"status": "available_period_end", "missing": []}
    assert all(value not in json.dumps(safe) for value in (ACCOUNT, *facts["values"].values()))


@pytest.mark.parametrize("override,reason", [
    ({"currency": "BASE_SUMMARY"}, "usd_cash_row_missing"),
    ({"accountId": None}, "cash_row_account_unverified"),
    ({"model": None}, "cash_row_model_scope_unverified"),
    ({"model": "private-model"}, "cash_row_model_scope_unverified"),
    ({"fromDate": None}, "cash_period_mismatch"),
    ({"toDate": "20260130"}, "cash_period_mismatch"),
    ({"toDate": "20260131;120000"}, "cash_period_mismatch"),
    ({"reportDate": "20260201"}, "cash_report_date_mismatch"),
    ({"endingSettledCash": None}, "endingSettledCash_missing_or_invalid"),
    ({"endingSettledCash": "NaN"}, "endingSettledCash_missing_or_invalid"),
    ({"endingSettledCash": "1e99999"}, "endingSettledCash_missing_or_invalid"),
    ({"endingCash": " 12 "}, "endingCash_missing_or_invalid"),
])
def test_period_cash_rejects_unverified_scope_dates_and_values(override, reason):
    report = _wrap(_statement('<CashReport>' + _native_cash_row(**override) + '</CashReport>'))
    facts = build_flex_ledger(report, expected_account_ids=(ACCOUNT,))["period_cash_facts"]
    assert facts["status"] == "incomplete" and facts["values"] is None
    assert reason in facts["missing"]


@pytest.mark.parametrize("section,reason", [
    ('<CashReport>{row}{row}</CashReport>', "usd_cash_row_ambiguous"),
    ('<CashReport>{row}</CashReport><CashReport/>', "cash_report_structure_ambiguous"),
    ('<CashReport>{row}<PrivateRow/></CashReport>', "cash_report_structure_ambiguous"),
])
def test_period_cash_never_chooses_one_of_duplicate_or_unknown_rows(section, reason):
    facts = build_flex_ledger(_wrap(_statement(section.format(row=_native_cash_row()))), expected_account_ids=(ACCOUNT,))["period_cash_facts"]
    assert facts["values"] is None and reason in facts["missing"]


def test_selects_only_target_statement_from_explicitly_approved_report_scope():
    other = "DU0000001"
    report = _wrap(
        _statement('<AccountInformation currency="USD"/><ChangeInNAV fromDate="20260101" toDate="20260131" twr="80"/>', account=other),
        _statement('<AccountInformation currency="USD"/><ChangeInNAV fromDate="20260101" toDate="20260131" twr="3.25"/>'),
    )
    ledger = build_flex_ledger(report, expected_account_ids=(ACCOUNT,), allowed_report_account_ids=(ACCOUNT, other))
    assert ledger["native_returns"]["twr"] == "3.25"
    assert ledger["account_ids"] == [ACCOUNT]
    with pytest.raises(FlexSourceError):
        build_flex_ledger(report, expected_account_ids=(ACCOUNT,))


@pytest.mark.parametrize("actual", [("DU0000001",), (ACCOUNT, "DU0000099"), (ACCOUNT, ACCOUNT)])
def test_approved_report_scope_still_rejects_missing_unknown_or_duplicate_accounts(actual):
    report = _wrap(*(_statement('<AccountInformation currency="USD"/>', account=a) for a in actual))
    with pytest.raises(FlexSourceError) as caught:
        build_flex_ledger(report, expected_account_ids=(ACCOUNT,), allowed_report_account_ids=(ACCOUNT, "DU0000001"))
    diagnostic = caught.value.safe_diagnostic
    assert diagnostic["reported_account_count"] == len(actual)
    assert diagnostic["target_accounts_present"] == (ACCOUNT in actual)
    assert not any(account in json.dumps(diagnostic) for account in actual)


def test_selected_statement_rejects_a_row_owned_by_another_account():
    report = _wrap(_statement('<AccountInformation currency="USD"/><StmtFunds><StatementOfFundsLine accountId="DU0000001" currency="USD" amount="99" activityCode="DEP"/></StmtFunds>'))
    with pytest.raises(FlexSourceError) as caught:
        build_flex_ledger(report, expected_account_ids=(ACCOUNT,), allowed_report_account_ids=(ACCOUNT, "DU0000001"))
    assert caught.value.safe_diagnostic["nested_account_mismatch"] is True


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
        '<AccountInformation accountId="DU0000004" currency="USD"/>',
        account="DU0000004",
    ))
    with pytest.raises(FlexSourceError):
        build_flex_ledger(report, expected_account_ids=(ACCOUNT,))


def test_rejects_multiple_statement_accounts():
    inner = '<AccountInformation accountId="{id}" currency="USD"/>'
    report = _wrap(
        _statement(inner.format(id="DU0000000"), account="DU0000000"),
        _statement(inner.format(id="DU0000001"), account="DU0000001"),
    )
    with pytest.raises(FlexSourceError):
        build_flex_ledger(report, expected_account_ids=("DU0000000", "DU0000001"))


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


def test_cashflow_source_diagnostic_has_fixed_fields_and_no_private_values():
    private = (ACCOUNT, "76543.210987", "synthetic-private-transaction", "privateXmlKey", "privateXmlValue")
    report = _wrap(_statement(
        '<CashTransactions><CashTransaction accountId="' + ACCOUNT + '" currency="USD" '
        'type="Deposits/Withdrawals" amount="76543.210987" transactionID="synthetic-private-transaction" '
        'dateTime="20260105;123456 EDT" timeZone="America/New_York" privateXmlKey="privateXmlValue"/>'
        '<CashTransaction accountId="' + ACCOUNT + '" currency="USD" type="Dividends" '
        'amount="1e-3" dateTime="2026-01-05T12:34:56+08:00"/></CashTransactions>'
        '<StmtFunds><StatementOfFundsLine accountId="' + ACCOUNT + '" currency="USD" '
        'date="20260105" reportDate="2026-01-05" activityCode="DEP" amount="76543.210987" '
        'transactionID="synthetic-private-transaction"/></StmtFunds>'
    ))
    diagnostic = build_flex_ledger(report, expected_account_ids=(ACCOUNT,))["cashflow_source_diagnostic"]
    assert diagnostic["schema_version"] == "ibkr.flex_cashflow_source_shape.v1"
    assert diagnostic["coverage"] == "not_evaluated"
    cash = diagnostic["sections"]["CashTransactions"]
    funds = diagnostic["sections"]["StmtFunds"]
    assert cash["section_count"] == 1 and cash["record_count"] == 2
    assert cash["field_type_counts"]["amount"] == {"decimal_text": 1, "scientific_text": 1}
    assert cash["date_time_format_counts"]["dateTime"] == {"named_zone_datetime": 1, "offset_datetime": 1}
    assert cash["timezone_format_counts"]["timeZone"] == {"iana_like": 1, "missing": 1}
    assert cash["missing_required_fields"] == ["transactionID"]
    assert funds["record_count"] == 1 and funds["missing_required_fields"] == []
    assert funds["date_time_format_counts"]["date"] == {"date_only": 1}
    serialized = json.dumps(diagnostic)
    assert all(value not in serialized for value in private)
    assert "America/New_York" not in serialized and "20260105" not in serialized
    assert "complete" not in serialized


@pytest.mark.parametrize("section", ["", "<CashTransactions/><StmtFunds/>"])
def test_cashflow_empty_or_missing_sections_never_certify_coverage(section):
    diagnostic = build_flex_ledger(_wrap(_statement(section)), expected_account_ids=(ACCOUNT,))["cashflow_source_diagnostic"]
    assert diagnostic["coverage"] == "not_evaluated"
    for source in diagnostic["sections"].values():
        assert source["section_count"] == int(bool(section)) and source["record_count"] == 0
        assert source["field_type_counts"] == {} and source["missing_required_fields"] == []


@pytest.mark.parametrize("value,expected", [
    ("20260105", "date_only"), ("2026-01-05", "date_only"),
    ("20260105;123456", "local_datetime"), ("2026-01-05T12:34:56", "local_datetime"),
    ("2026-01-05T12:34:56Z", "utc_datetime"),
    ("20260105;12:34:56-0500", "offset_datetime"),
    ("20260105;123456 America/New_York", "named_zone_datetime"),
    ("2026-02-30T12:34:56Z", "unsupported"), ("2026-01-05T25:00:00Z", "unsupported"),
    ("2026-01-05T12:34:56+25:00", "unsupported"), ("synthetic-private-time", "unsupported"),
    ("", "empty_text"),
])
def test_cashflow_datetime_diagnostic_classifies_formats_without_values(value, expected):
    report = _wrap(_statement(f'<CashTransactions><CashTransaction dateTime="{value}"/></CashTransactions>'))
    section = build_flex_ledger(report, expected_account_ids=(ACCOUNT,))["cashflow_source_diagnostic"]["sections"]["CashTransactions"]
    assert section["date_time_format_counts"]["dateTime"] == {expected: 1}
    assert value not in json.dumps(section) if value else True


def test_cashflow_diagnostic_only_visits_selected_statement_and_counts_unknown_rows():
    selected = _statement('<CashTransactions><UnknownPrivateTag privateKey="private-value"/></CashTransactions><StmtFunds/><StmtFunds/>')
    other = _statement('<CashTransactions><CashTransaction transactionID="private-id"/></CashTransactions>', account="DU0000099")
    report = _wrap(selected, other)
    diagnostic = build_flex_ledger(report, expected_account_ids=(ACCOUNT,), allowed_report_account_ids=(ACCOUNT, "DU0000099"))["cashflow_source_diagnostic"]
    assert diagnostic["sections"]["CashTransactions"]["record_count"] == 0
    assert diagnostic["sections"]["CashTransactions"]["unsupported_record_count"] == 1
    assert diagnostic["sections"]["StmtFunds"]["section_count"] == 2
    assert all(value not in json.dumps(diagnostic) for value in ("UnknownPrivateTag", "privateKey", "private-value", "private-id", "DU0000099"))


def test_nav_source_diagnostic_covers_public_components_without_private_values_or_bridge_claim():
    report = _wrap(_statement(
        f'<ChangeInNAV accountId="{ACCOUNT}" acctAlias="synthetic-private-alias" model="synthetic-private-model" '
        'currency="EUR" mode="synthetic-private-mode" fromDate="20260101" toDate="20260131" '
        'startingValue="76543.210987" mtm="76543.210987" realized="76543.210987" changeInUnrealized="76543.210987" '
        'depositsWithdrawals="76543.210987" internalCashTransfers="76543.210987" assetTransfers="76543.210987" '
        'dividends="76543.210987" withholdingTax="76543.210987" withholding871m="76543.210987" '
        'changeInDividendAccruals="76543.210987" interest="76543.210987" changeInInterestAccruals="76543.210987" '
        'advisorFees="76543.210987" clientFees="76543.210987" otherFees="76543.210987" feesReceivables="76543.210987" '
        'commissions="76543.210987" commissionReceivables="76543.210987" forexCommissions="76543.210987" '
        'transactionTax="76543.210987" taxReceivables="76543.210987" salesTax="76543.210987" softDollars="76543.210987" '
        'netFxTrading="76543.210987" fxTranslation="76543.210987" linkingAdjustments="76543.210987" other="76543.210987" '
        'endingValue="76543.210987" twr="1.25" privateNavKey="synthetic-private-value"/>'
    ))
    ledger = build_flex_ledger(report, expected_account_ids=(ACCOUNT,))
    diagnostic = ledger["nav_source_diagnostic"]
    assert diagnostic["schema_version"] == "ibkr.flex_nav_source_shape.v1"
    assert diagnostic["record_count"] == 1 and diagnostic["unsupported_record_count"] == 0
    assert diagnostic["unknown_field_count"] == 1
    assert len(diagnostic["field_type_counts"]) == 37
    assert list(diagnostic["field_type_counts"].values()).count({"decimal_text": 1}) == 30
    assert list(diagnostic["field_type_counts"].values()).count({"text": 1}) == 7
    assert diagnostic["date_time_format_counts"] == {"fromDate": {"date_only": 1}, "toDate": {"date_only": 1}}
    assert diagnostic["coverage"] == diagnostic["nav_bridge"] == "not_evaluated"
    assert diagnostic["missing_fields_are_zero"] is False
    # Even deliberately inconsistent components do not create or overwrite P/L.
    assert ledger["native_returns"]["twr"] == "1.25" and "native_nav_change" not in ledger
    output = json.dumps(diagnostic)
    assert all(value not in output for value in (ACCOUNT, "76543.210987", "1.25", "EUR", "20260101", "20260131",
        "synthetic-private-alias", "synthetic-private-model", "synthetic-private-mode", "privateNavKey", "synthetic-private-value"))


@pytest.mark.parametrize("value,expected", [
    ("-76543.210987", "decimal_text"), (" 1.25 ", "decimal_text"), ("1e-3", "scientific_text"),
    ("NaN", "nonfinite_text"), ("-Infinity", "nonfinite_text"), ("not-a-number", "unsupported_text"),
    ("9" * 257, "oversized_text"), ("", "empty_text"),
])
def test_nav_numeric_formats_are_classified_without_value_sign_or_precision(value, expected):
    report = _wrap(_statement(f'<ChangeInNAV startingValue="{value}"/>'))
    diagnostic = build_flex_ledger(report, expected_account_ids=(ACCOUNT,))["nav_source_diagnostic"]
    assert diagnostic["field_type_counts"]["startingValue"] == {expected: 1}
    assert diagnostic["field_type_counts"]["depositsWithdrawals"] == {"missing": 1}
    assert value not in json.dumps(diagnostic) if value else True


@pytest.mark.parametrize("section,count", [("", 0), ("<ChangeInNAV/>", 1), ("<ChangeInNAV/><ChangeInNAV/>", 2)])
def test_nav_missing_empty_or_duplicate_records_never_certify_zero_or_coverage(section, count):
    diagnostic = build_flex_ledger(_wrap(_statement(section)), expected_account_ids=(ACCOUNT,))["nav_source_diagnostic"]
    assert diagnostic["record_count"] == count and diagnostic["unknown_field_count"] == 0
    if count:
        assert len(diagnostic["field_type_counts"]) == 37
        assert all(types == {"missing": count} for types in diagnostic["field_type_counts"].values())
    else:
        assert diagnostic["field_type_counts"] == {}
    assert diagnostic["coverage"] == diagnostic["nav_bridge"] == "not_evaluated"
    assert diagnostic["missing_fields_are_zero"] is False


def test_nav_diagnostic_counts_only_selected_records_and_unknown_names_stay_hidden():
    selected = _statement('<ChangeInNAV privateNavKey="private-nav-value">'
        '<PrivateNavChild privateChildKey="private-child-value"/></ChangeInNAV>'
        '<ChangeInNAV otherPrivateKey="other-private-value" startingValue="1e3" fromDate="bad-private-date"/>')
    other = _statement('<ChangeInNAV startingValue="987654.321" otherAccountPrivateKey="private-value"/>', account="DU0000099")
    diagnostic = build_flex_ledger(_wrap(selected, other), expected_account_ids=(ACCOUNT,),
        allowed_report_account_ids=(ACCOUNT, "DU0000099"))["nav_source_diagnostic"]
    assert diagnostic["record_count"] == diagnostic["unknown_field_count"] == 2
    assert diagnostic["unsupported_record_count"] == 1
    assert diagnostic["field_type_counts"]["startingValue"] == {"missing": 1, "scientific_text": 1}
    assert diagnostic["date_time_format_counts"]["fromDate"] == {"missing": 1, "unsupported": 1}
    assert not any(value in json.dumps(diagnostic) for value in ("privateNavKey", "PrivateNavChild", "privateChildKey",
        "otherPrivateKey", "otherAccountPrivateKey", "987654.321", "DU0000099", "private-nav-value", "bad-private-date"))


def _nav_amount_report(*, attributes=None, nav_copies=1, information_copies=1, child=""):
    attrs = {"accountId": ACCOUNT, "model": "", "currency": "BASE_SUMMARY",
        "fromDate": "20260101", "toDate": "20260131", "twr": "1.25",
        "startingValue": "10000.00", "endingValue": "11250.00", "depositsWithdrawals": "1000.00",
        "internalCashTransfers": "0", "assetTransfers": "-0.00", "linkingAdjustments": "0.00",
        "mtm": "99999.123456", "commissions": "-98765.4321", "dividends": "87654.321"}
    attrs.update(attributes or {})
    row = "<ChangeInNAV " + " ".join(f'{key}="{value}"' for key, value in attrs.items() if value is not None) + ">" + child + "</ChangeInNAV>"
    return _wrap(_statement('<AccountInformation currency="EUR"/>' * information_copies + row * nav_copies))


@pytest.mark.parametrize("currency", ["BASE_SUMMARY", "EUR"])
def test_ledger_preserves_one_native_total_nav_row_without_calculating_or_forwarding_unrelated_components(currency):
    ledger = build_flex_ledger(_nav_amount_report(attributes={"currency": currency}), expected_account_ids=(ACCOUNT,))
    assert ledger["native_nav_change"] == {"source": "ChangeInNAV", "account_id": ACCOUNT,
        "model": "", "currency": currency, "period": {"from": "2026-01-01", "to": "2026-01-31"},
        "components": {"startingValue": "10000.00", "endingValue": "11250.00", "depositsWithdrawals": "1000.00",
            "internalCashTransfers": "0", "assetTransfers": "-0.00", "linkingAdjustments": "0.00"}}
    assert ledger["native_returns"]["twr"] == "1.25"
    diagnostic = json.dumps(ledger["nav_source_diagnostic"])
    assert all(value not in diagnostic for value in (ACCOUNT, "10000.00", "11250.00", "1000.00", "99999.123456", "-98765.4321"))


@pytest.mark.parametrize("field,value", [("currency", "USD"), ("currency", None), ("currency", " EUR"),
    ("model", None), ("model", "synthetic-model"), ("model", " "), ("accountId", None)])
def test_native_twr_rejects_explicit_nav_scope_conflicts(field, value):
    ledger = build_flex_ledger(_nav_amount_report(attributes={field: value}), expected_account_ids=(ACCOUNT,))
    assert "native_nav_change" not in ledger
    if (field, value) in {("currency", "USD"), ("currency", " EUR"),
                          ("model", "synthetic-model"), ("model", " ")}:
        assert ledger["native_returns"] == {}
    else:
        assert ledger["native_returns"]["twr"] == "1.25"


@pytest.mark.parametrize("kwargs", [{"nav_copies": 2}, {"information_copies": 2}, {"child": "<PrivateChild/>"}])
def test_ambiguous_nav_source_structure_blocks_native_outputs(kwargs):
    ledger = build_flex_ledger(_nav_amount_report(**kwargs), expected_account_ids=(ACCOUNT,))
    assert "native_nav_change" not in ledger and ledger["native_returns"] == {}


@pytest.mark.parametrize("field,value", [("fromDate", "20260102"), ("toDate", "20260130"),
    ("fromDate", "20260101;120000"), ("toDate", "20260131;120000")])
def test_nav_source_period_requires_matching_native_calendar_dates(field, value):
    ledger = build_flex_ledger(_nav_amount_report(attributes={field: value}), expected_account_ids=(ACCOUNT,))
    assert "native_nav_change" not in ledger


def test_period_return_rejects_explicit_scope_conflicts_and_ambiguous_rows():
    from datetime import datetime, timezone
    from application.ibkr_period_return import PeriodReturnError, build_ibkr_period_return

    kwargs = dict(target_id="synthetic-target", source_binding_id="a" * 64,
                  account_scope="synthetic-scope", expected_account_ids=[ACCOUNT],
                  observed_at=datetime(2026, 2, 1, tzinfo=timezone.utc))
    def project(info, rows):
        ledger = build_flex_ledger(_wrap(_statement(info + rows)), expected_account_ids=(ACCOUNT,))
        return ledger, lambda: build_ibkr_period_return(ledger, **kwargs)

    info = '<AccountInformation currency="USD"/>'
    # Missing optional child scope fields preserve the pre-existing source contract.
    legacy = '<ChangeInNAV fromDate="20260101" toDate="20260131" twr="1"/>'
    _, valid = project(info, legacy)
    assert valid()["source_value"] == "1"
    total = (f'<ChangeInNAV accountId="{ACCOUNT}" model="" currency="USD" '
             'fromDate="20260101" toDate="20260131" twr="1"/>')
    model = (f'<ChangeInNAV accountId="{ACCOUNT}" model="model-A" currency="EUR" '
             'fromDate="20260101" toDate="20260131" twr="50"/>')
    for source_info, rows in [
        (info, model + total), (info, total + model), (info, model),
        (info, total.replace('currency="USD"', 'currency="EUR"')),
        ('<AccountInformation currency="USD" model="model-A"/>', total),
        (info + info, total),
    ]:
        ledger, invalid = project(source_info, rows)
        assert "native_nav_change" not in ledger
        with pytest.raises(PeriodReturnError, match="native_twr_unavailable"):
            invalid()
    with pytest.raises(FlexSourceError):
        project(info, total.replace(ACCOUNT, "DU0000001"))


def test_model_scoped_account_information_never_emits_nav_components():
    report = _nav_amount_report()
    body = report.content.replace(b'<AccountInformation currency="EUR"/>',
                                  b'<AccountInformation currency="EUR" model="model-A"/>')
    ledger = build_flex_ledger(FlexReport(hashlib.sha256(body).hexdigest(), body),
                               expected_account_ids=(ACCOUNT,))
    assert ledger["native_returns"] == {}
    assert "native_nav_change" not in ledger
