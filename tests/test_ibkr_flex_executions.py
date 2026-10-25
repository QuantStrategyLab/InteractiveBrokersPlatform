"""Synthetic Flex execution facts; no broker, cloud or private account data."""

import hashlib
import json
from datetime import datetime, timezone
from xml.etree import ElementTree

import pytest

from application.ibkr_flex_executions import build_ibkr_executions
from application.ibkr_flex_ledger import build_flex_ledger
from application.ibkr_flex_source import FlexReport, FlexSourceError


ACCOUNT = "DU0000000"
CONFIG = {"target_id": "synthetic-target", "source_binding_id": "a" * 64,
          "account_scope": "synthetic-live", "expected_account_ids": (ACCOUNT,),
          "observed_at": datetime(2026, 2, 1, tzinfo=timezone.utc)}


def trade(**changes):
    attributes = {"accountId": ACCOUNT, "levelOfDetail": "EXECUTION", "ibExecID": "synthetic-execution",
                  "tradeDate": "20260123", "dateTime": "20260123;153045", "buySell": "SELL",
                  "quantity": "-2.5000", "tradePrice": "10.2500", "currency": "USD",
                  "assetCategory": "STK", "symbol": "SYNTH", "ibCommission": "-0.35",
                  "ibCommissionCurrency": "EUR", "taxes": "0"}
    attributes.update(changes)
    return ElementTree.tostring(ElementTree.Element("Trade", {key: value for key, value in attributes.items()
                                                              if value is not None}), encoding="unicode")


def ledger(section="<Trades/>", *, twr=True):
    native = '<ChangeInNAV fromDate="20260101" toDate="20260131" twr="1.25"/>' if twr else ""
    content = (f'<FlexQueryResponse type="AF"><FlexStatements><FlexStatement accountId="{ACCOUNT}" '
               f'fromDate="20260101" toDate="20260131"><AccountInformation currency="USD"/>{native}'
               f'{section}</FlexStatement></FlexStatements></FlexQueryResponse>').encode()
    report = FlexReport(content_sha256=hashlib.sha256(content).hexdigest(), content=content)
    return build_flex_ledger(report, expected_account_ids=(ACCOUNT,))


def test_native_execution_keeps_signed_decimals_fees_and_report_lineage():
    source = ledger(f"<Trades>{trade()}</Trades>")
    payload = build_ibkr_executions(source, **CONFIG)
    assert payload["schema_version"] == "broker_executions.v1"
    assert payload["account_selector"] == ACCOUNT
    assert payload["coverage"] == "reported_executions"
    assert payload["period"] == {"from": "2026-01-01", "to": "2026-01-31"}
    assert payload["source"] == "ibkr_flex" and payload["source_ref"] == source["source_ref"]
    assert payload["records"] == [{
        "execution_ref": hashlib.sha256(f"{ACCOUNT}|synthetic-execution".encode()).hexdigest(),
        "report_ref": source["source_ref"], "trade_date": "2026-01-23",
        "local_time": "2026-01-23T15:30:45", "time_zone": None,
        "symbol": "SYNTH", "asset_category": "STK", "side": "SELL", "quantity": "-2.5000",
        "price": "10.2500", "currency": "USD", "commission": "-0.35",
        "commission_currency": "EUR", "taxes": "0",
    }]


def test_explicit_empty_section_only_covers_this_report_not_complete_history():
    source = ledger()
    payload = build_ibkr_executions(source, **CONFIG)
    assert payload["records"] == [] and payload["coverage"] == "reported_executions"
    assert source["executions"]["diagnostic"]["section_present"] is True
    assert "complete" not in json.dumps(source["executions"])


@pytest.mark.parametrize("section,reason", [
    ("", "trades_section_missing"),
    (f'<Trades>{trade(levelOfDetail="ORDER")}</Trades>', "execution_level_required"),
    (f'<Trades>{trade(levelOfDetail="SUMMARY")}</Trades>', "execution_level_required"),
    (f'<Trades>{trade(levelOfDetail=None)}</Trades>', "execution_level_required"),
    ('<Trades><Order/></Trades>', "unsupported_trade_rows"),
    (f'<Trades>{trade(tradeDate="20251231")}</Trades>', "execution_date_outside_period"),
    (f'<Trades>{trade(dateTime="20260124;153045")}</Trades>', "execution_datetime_invalid"),
    (f'<Trades>{trade(dateTime="private-invalid-time")}</Trades>', "execution_datetime_invalid"),
])
def test_unknown_source_never_becomes_empty_records(section, reason):
    source = ledger(section)
    assert source["executions"]["status"] == "source_unavailable"
    assert source["executions"]["records"] is None
    assert source["executions"]["diagnostic"]["reason"] == reason
    with pytest.raises(ValueError, match="source_unavailable"):
        build_ibkr_executions(source, **CONFIG)
    assert source["return_assessment"]["computable"] is True


@pytest.mark.parametrize("field", ["ibExecID", "tradeDate", "buySell", "quantity", "tradePrice",
                                    "currency", "assetCategory", "symbol", "accountId"])
def test_missing_required_native_field_is_safe_and_does_not_drop_bad_row(field):
    source = ledger(f"<Trades>{trade(**{field: None})}</Trades>")
    diagnostic = source["executions"]["diagnostic"]
    assert source["executions"]["status"] == "source_unavailable"
    assert diagnostic["missing_fields"] == [field]
    output = json.dumps(diagnostic)
    assert ACCOUNT not in output and "synthetic-execution" not in output and "10.2500" not in output


def test_trade_owned_by_other_account_is_rejected_before_projection():
    with pytest.raises(FlexSourceError) as caught:
        ledger(f'<Trades>{trade(accountId="DU0000001")}</Trades>')
    assert caught.value.safe_diagnostic["nested_account_mismatch"] is True


def test_identical_execution_deduplicates_and_conflicting_execution_is_unknown():
    source = ledger(f"<Trades>{trade()}{trade()}</Trades>")
    assert len(build_ibkr_executions(source, **CONFIG)["records"]) == 1
    source = ledger(f'<Trades>{trade()}{trade(tradePrice="11")}</Trades>')
    assert source["executions"]["diagnostic"]["reason"] == "execution_conflict"
    assert source["executions"]["records"] is None


@pytest.mark.parametrize("changes", [{"notes": "Ca"}, {"notes": "O;Co"}, {"origTradeID": "synthetic-original"},
                                      {"origExecID": "synthetic-original"}, {"origTradeDate": "20260122"}])
def test_cancel_or_correct_requires_reconciliation_not_new_execution(changes):
    source = ledger(f"<Trades>{trade(**changes)}</Trades>")
    assert source["executions"]["diagnostic"]["reason"] == "execution_correction_unresolved"
    assert source["executions"]["records"] is None
    assert source["return_assessment"]["computable"] is True


def test_absent_optional_fees_time_and_meaningless_original_id_are_not_fabricated():
    native = trade(dateTime=None, ibCommission=None, ibCommissionCurrency=None, taxes=None, origTradeID="0")
    source = ledger(f'<Trades>{native}</Trades>')
    row = build_ibkr_executions(source, **CONFIG)["records"][0]
    assert row["local_time"] is None and row["time_zone"] is None
    assert row["commission"] is None and row["commission_currency"] is None and row["taxes"] is None


def test_missing_fee_currency_is_not_inherited_and_zero_fee_is_retained():
    source = ledger(f'<Trades>{trade(ibCommission="0", ibCommissionCurrency=None)}</Trades>')
    row = build_ibkr_executions(source, **CONFIG)["records"][0]
    assert row["commission"] == "0" and row["commission_currency"] is None


@pytest.mark.parametrize(("value", "shape"), [
    ("-0.123456789", {"kind": "decimal", "integer_digits": 1, "fraction_digits": 9, "has_whitespace": False}),
    ("-1.25E-9", {"kind": "scientific", "integer_digits": 1, "fraction_digits": 2, "has_whitespace": False}),
    (" -0.25 ", {"kind": "decimal", "integer_digits": 1, "fraction_digits": 2, "has_whitespace": True}),
    ("NaN", {"kind": "nonfinite"}),
    ("synthetic-private-fee", {"kind": "unsupported"}),
])
def test_invalid_commission_reports_only_safe_format_and_preserves_rejection(value, shape):
    source = ledger(f'<Trades>{trade(ibCommission=value)}</Trades>')
    executions = source["executions"]
    assert executions["status"] == "source_unavailable" and executions["records"] is None
    assert executions["diagnostic"]["invalid_numeric_shapes"] == {"ibCommission": shape}
    assert executions["diagnostic"]["invalid_fields"] == ["ibCommission"]
    output = json.dumps(executions["diagnostic"])
    assert value not in output and ACCOUNT not in output and "synthetic-execution" not in output
    assert source["return_assessment"]["computable"] is True


@pytest.mark.parametrize("changes", [{"quantity": "0"}, {"quantity": "NaN"}, {"tradePrice": "-1"},
                                      {"tradePrice": "1e2"}, {"quantity": "0.123456789"},
                                      {"tradePrice": "1000000000000"}, {"buySell": "BOT"},
                                      {"symbol": "<private>"}, {"assetCategory": "private"},
                                      {"taxes": "private"}])
def test_invalid_native_values_make_whole_source_unknown(changes):
    source = ledger(f"<Trades>{trade(**changes)}</Trades>")
    assert source["executions"]["status"] == "source_unavailable"
    assert source["executions"]["records"] is None
    assert source["executions"]["diagnostic"]["missing_fields"] == []
    assert source["executions"]["diagnostic"]["invalid_fields"] == sorted(changes)


def test_execution_projection_does_not_require_twr_or_account_base_currency():
    source = ledger(f"<Trades>{trade()}</Trades>", twr=False)
    assert source["return_assessment"]["computable"] is False
    assert len(build_ibkr_executions(source, **CONFIG)["records"]) == 1


def test_record_limit_and_late_bad_row_do_not_publish_truncated_history():
    rows = "".join(trade(ibExecID=f"synthetic-{index}") for index in range(257))
    source = ledger(f"<Trades>{rows}</Trades>")
    assert source["executions"]["diagnostic"]["reason"] == "execution_record_limit"
    source = ledger(f'<Trades>{trade()}{trade(ibExecID="synthetic-2", currency=None)}</Trades>')
    assert source["executions"]["records"] is None


def test_report_debug_shape_contains_field_names_counts_and_safe_level_only():
    source = ledger(f'<Trades>{trade(levelOfDetail="synthetic-private-value")}</Trades>')
    diagnostic = source["executions"]["diagnostic"]
    assert diagnostic["field_names"] == sorted(ElementTree.fromstring(trade()).attrib)
    assert diagnostic["native_record_count"] == 1 and diagnostic["level_types"] == ["other"]
    output = json.dumps(diagnostic)
    assert not any(value in output for value in (ACCOUNT, "synthetic-private-value", "synthetic-execution", "10.2500"))


@pytest.mark.parametrize("change", [{"expected_account_ids": ("DU0000001",)},
                                     {"observed_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
                                     {"observed_at": datetime(2026, 2, 1)}])
def test_scope_or_observation_mismatch_never_creates_payload(change):
    with pytest.raises(ValueError):
        build_ibkr_executions(ledger(), **{**CONFIG, **change})
