"""Synthetic native fields only; no cloud, broker, or real financial inputs."""
from copy import deepcopy
import json

import pytest

from application.ibkr_cash_observations import build_cash_observations, inspect_cash_observations

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
