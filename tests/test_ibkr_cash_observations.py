"""Synthetic native fields only; no cloud, broker, or real financial inputs."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from application.ibkr_cash_observations import (
    build_cash_observations, inspect_cash_observations, build_cash_schedule_observations,
    capture_cash_schedule_rows, inspect_cash_schedule_observations,
)

TIME = "2000-01-03T12:00:00Z"


def attachment():
    return build_cash_observations([
        {"currency": "USD", "CashBalance": 100, "SettledCash": 0, "AvailableFunds": -2},
        {"currency": "EUR", "CashBalance": 8, "SettledCash": 7},
        {"currency": "BASE", "SettledCash": 999},
    ], observed_at=TIME)


def test_projection_keeps_zero_negative_and_currency_separate():
    value = attachment()
    assert all(row["currency"] != "BASE" for row in value["values"])
    out = inspect_cash_observations(value, observed_at=TIME)
    assert out["fields"]["SettledCash"] == "OBSERVED"
    assert out["fields"]["AvailableFunds"] == "OBSERVED"
    assert out["fields"]["FullAvailableFunds"] == "MISSING"
    assert out["spendable_cash_verified"] is False
    assert "100" not in json.dumps(out) and "999" not in json.dumps(out)


@pytest.mark.parametrize("bad", [None, True, False, "NaN", "Infinity", float("nan"), object()])
def test_invalid_native_field_is_distinct_from_missing_and_zero(bad):
    value = build_cash_observations([{"currency": "USD", "CashBalance": 100, "SettledCash": bad}], observed_at=TIME)
    assert value["invalid_fields"] == [{"currency": "USD", "source_tag": "SettledCash"}]
    out = inspect_cash_observations(value, observed_at=TIME)
    assert out["fields"]["SettledCash"] == "INVALID"
    assert out["fields"]["AvailableFunds"] == "MISSING"
    assert out["fields"]["CashBalance"] == "OBSERVED"


@pytest.mark.parametrize("mutation", [
    lambda v: v.update(schema_version="wrong"),
    lambda v: v.update(observed_at="2000-01-03T11:59:00Z"),
    lambda v: v.update(unknown="private-value"),
    lambda v: v.update(values=None),
    lambda v: v.update(invalid_fields=None),
    lambda v: v["values"].append(deepcopy(v["values"][0])),
    lambda v: v["values"][0].update(value=True),
    lambda v: v["values"][0].update(value="NaN"),
    lambda v: v["values"][0].update(currency="BASE"),
    lambda v: v["values"][0].update(source_tag=["private-value"]),
    lambda v: v.update(invalid_fields=[{"currency":"USD", "source_tag":"SettledCash"}]),
])
def test_tampering_or_conflicting_fields_cannot_certify_settled_cash(mutation):
    value = attachment()
    mutation(value)
    out = inspect_cash_observations(value, observed_at=TIME)
    assert out["status"] == "INVALID"
    assert "OBSERVED" not in out["fields"].values()
    assert "private-value" not in json.dumps(out)


def test_same_field_must_match_legacy_projection_different_tags_need_not_match():
    value = attachment()
    for selected in ({"source_tag":"CashBalance", "cash_balance":"101"},
                     {"source_tag":["private-value"], "cash_balance":"100"}):
        assert inspect_cash_observations(value, observed_at=TIME, selected_cash=selected)["status"] == "INVALID"
    selected = {"source_tag":"CashBalance", "cash_balance":"100.00"}
    assert inspect_cash_observations(value, observed_at=TIME, selected_cash=selected)["status"] == "OBSERVED_FIELDS_ONLY"


def test_old_reports_remain_unreported_and_header_alone_does_not_imply_zero():
    assert inspect_cash_observations(None, observed_at=TIME)["status"] == "NOT_REPORTED"
    value = build_cash_observations([], observed_at=TIME)
    out = inspect_cash_observations(value, observed_at=TIME)
    assert out["status"] == "OBSERVED_FIELDS_ONLY"
    assert set(out["fields"].values()) == {"MISSING"}
    assert inspect_cash_observations(value, observed_at=None)["status"] == "INVALID"


def test_other_currency_settled_cash_does_not_fill_usd_field():
    value = build_cash_observations([{"currency":"EUR", "SettledCash":10}], observed_at=TIME)
    assert inspect_cash_observations(value, observed_at=TIME)["fields"]["SettledCash"] == "MISSING"


def schedule(raw="20000103:123.4567890123456789", *, currency="USD"):
    rows = capture_cash_schedule_rows([SimpleNamespace(account="DU123", currency=currency,
        tag="SettledCashByDate", value=raw)], account_ids=("DU123",))
    return build_cash_schedule_observations(rows, account_id="DU123", observed_at=TIME)


@pytest.mark.parametrize("raw", ["0:0", "20991231:1.0000000000000000001", " 20000103:-1.5 ",
    "20000103:1;20991231:2", "opaque-private-source-text", "x" * 4096])
def test_schedule_preserves_raw_text_without_claiming_date_or_cash_semantics(raw):
    value = schedule(raw)
    assert value["values"][0]["raw_value"] == raw
    out = inspect_cash_schedule_observations(value, observed_at=TIME)
    assert out["fields"]["SettledCashByDate"] == "OBSERVED_RAW"
    assert out["status"] == "RAW_FIELDS_ONLY"
    for name in ("date_key_semantics_verified", "settled_cash_equivalence_verified",
            "current_settlement_selected", "spendable_cash_verified"):
        assert out[name] is False
    assert raw not in json.dumps(out)


@pytest.mark.parametrize("raw", [None, True, 12, [], "", " \n", "x" * 4097, "界" * 1366, "\ud800"])
def test_schedule_bad_or_unserializable_value_retains_a_nonempty_invalid_row(raw):
    value = schedule(raw)
    assert value["values"] == [{"currency": "USD", "source_tag": "SettledCashByDate"}]
    out = inspect_cash_schedule_observations(value, observed_at=TIME)
    assert out["fields"]["SettledCashByDate"] == "RAW_NOT_PRESERVED"
    assert out["fields"]["SettledCashByDate-C"] == "MISSING"


def test_schedule_keeps_duplicates_and_base_separate_and_filters_account_and_tags():
    rows = capture_cash_schedule_rows([SimpleNamespace(account=account, currency=currency,
        tag=tag, value="20000103:1") for account, currency, tag in [
            ("DU123", "USD", "SettledCashByDate"), ("DU123", "USD", "SettledCashByDate"),
            ("DU123", "BASE", "SettledCashByDate-S"), ("DU123", "EUR", "SettledCashByDate-C"),
            ("DU999", "USD", "SettledCashByDate-C"), ("DU123", "US D", "SettledCashByDate-C"),
            ("DU123", "USD", "SettledCash"), ("DU123", "USD", "unrelated")]], account_ids=("DU123",))
    assert len(rows) == 4
    value = build_cash_schedule_observations(rows, account_id="DU123", observed_at=TIME)
    assert len(value["values"]) == 4
    out = inspect_cash_schedule_observations(value, observed_at=TIME)
    assert out["fields"] == {"SettledCashByDate": "AMBIGUOUS_ROWS",
        "SettledCashByDate-C": "MISSING", "SettledCashByDate-S": "MISSING"}
    assert inspect_cash_schedule_observations(value, observed_at=TIME, currency="BASE")["fields"]["SettledCashByDate-S"] == "OBSERVED_RAW"
    assert build_cash_schedule_observations(rows, account_id="DU999", observed_at=TIME) is None


@pytest.mark.parametrize("mutation", [lambda v: v.update(schema_version="wrong"),
    lambda v: v.update(observed_at="different"), lambda v: v.update(unexpected="private-text"),
    lambda v: v.update(values=None), lambda v: v["values"][0].update(currency="US D"),
    lambda v: v["values"][0].update(source_tag=["private-text"]),
    lambda v: v["values"][0].update(raw_value=True)])
def test_malformed_schedule_never_leaks_raw_text_or_selects_settlement(mutation):
    value = schedule("private-text")
    mutation(value)
    out = inspect_cash_schedule_observations(value, observed_at=TIME)
    assert out["status"] == "INVALID"
    assert "private-text" not in json.dumps(out)
    assert not out["current_settlement_selected"]


def test_absent_and_empty_schedule_do_not_imply_a_zero_balance():
    assert inspect_cash_schedule_observations(None, observed_at=TIME)["status"] == "NOT_REPORTED"
    value = build_cash_schedule_observations([], account_id="DU123", observed_at=TIME)
    assert "values" not in value
    out = inspect_cash_schedule_observations(value, observed_at=TIME)
    assert out["status"] == "RAW_FIELDS_ONLY" and set(out["fields"].values()) == {"MISSING"}
