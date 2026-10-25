from copy import deepcopy
from datetime import datetime, timezone

import pytest

from application.ibkr_period_return import PeriodReturnError, build_ibkr_period_return


OBSERVED = datetime(2026, 10, 8, 10, tzinfo=timezone.utc)
SCOPE = dict(target_id="synthetic-target", source_binding_id="a" * 64,
             account_scope="synthetic-live", expected_account_ids=("DU0000000",))


def ledger():
    return {
        "schema_version": "ibkr_flex_ledger.v1", "account_ids": ["DU0000000"],
        "account_base_currency": "EUR", "currency": None,
        "period": {"from": "2026-01-01", "to": "2026-01-31", "period": "private-alias"},
        "native_returns": {"twr": "-1.25", "unit": "percent", "from": "2026-01-01",
                           "to": "2026-01-31", "source": "ChangeInNAV.twr"},
        "return_assessment": {"computable": True, "method": "native_ibkr_twr",
                              "value": "-0.0125", "unit": "ratio", "source": "ChangeInNAV.twr"},
        "external_flows": [{"amount": "987654"}], "ending_valuation": None,
    }


def test_projects_only_native_interval_preserving_non_usd_base_currency():
    result = build_ibkr_period_return(ledger(), observed_at=OBSERVED, **SCOPE)
    assert result == {
        "schema_version": "ibkr_account_period_return.v1", "target_id": "synthetic-target",
        "source_binding_id": "a" * 64, "account_scope": "synthetic-live", "account_ids": ["DU0000000"],
        "observed_at": "2026-10-08T10:00:00Z", "currency": "EUR",
        "period": {"from": "2026-01-01", "to": "2026-01-31"},
        "method": "native_ibkr_twr", "source": "ChangeInNAV.twr", "source_unit": "percent",
        "source_value": "-1.25", "unit": "ratio", "value": "-0.0125",
    }
    assert "private-alias" not in repr(result) and "987654" not in repr(result)


@pytest.mark.parametrize(("section", "key", "value"), [
    (None, "account_ids", ["DU0000004"]), (None, "account_ids", ["DU0000000", "DU0000004"]),
    (None, "account_base_currency", None), (None, "account_base_currency", "USD "),
    ("native_returns", "unit", "ratio"), ("native_returns", "source", "NAV.change"),
    ("native_returns", "from", "2025-12-01"), ("native_returns", "to", "2026-02-01"),
    ("return_assessment", "computable", False), ("return_assessment", "method", "local_twr"),
    ("return_assessment", "source", "other"), ("return_assessment", "unit", "percent"),
    ("return_assessment", "value", "-1.25"), ("return_assessment", "value", "NaN"),
    ("native_returns", "twr", "1e3"), ("native_returns", "twr", "9" * 65),
    ("period", "from", "2026-02-31"), ("period", "from", "2026-02-01"),
])
def test_rejects_identity_method_unit_period_and_value_mismatches(section, key, value):
    data = deepcopy(ledger())
    (data if section is None else data[section])[key] = value
    with pytest.raises(PeriodReturnError):
        build_ibkr_period_return(data, observed_at=OBSERVED, **SCOPE)


def test_missing_native_never_invents_return_from_valuation_or_flows():
    data = ledger()
    data["native_returns"] = {}
    with pytest.raises(PeriodReturnError, match="native_twr_unavailable"):
        build_ibkr_period_return(data, observed_at=OBSERVED, **SCOPE)


def test_observation_requires_timezone_and_period_not_in_future():
    for observed in (OBSERVED.replace(tzinfo=None), datetime(2026, 1, 15, tzinfo=timezone.utc)):
        with pytest.raises(PeriodReturnError, match="observation_invalid"):
            build_ibkr_period_return(ledger(), observed_at=observed, **SCOPE)


def test_exact_percent_relation_is_independent_of_decimal_context():
    from decimal import localcontext
    data = ledger()
    data["native_returns"]["twr"] = "1234567890123456789012345678.1"
    data["return_assessment"]["value"] = "12345678901234567890123456.781"
    with localcontext() as context:
        context.prec = 4
        result = build_ibkr_period_return(data, observed_at=OBSERVED, **SCOPE)
    assert result["value"] == data["return_assessment"]["value"]


def nav_source():
    return {"source": "ChangeInNAV", "account_id": "DU0000000", "model": "", "currency": "BASE_SUMMARY",
        "period": {"from": "2026-01-01", "to": "2026-01-31"}, "components": {
            "startingValue": "10000.00", "endingValue": "11250.00", "depositsWithdrawals": "1000.00",
            "internalCashTransfers": "0", "assetTransfers": "-0.00", "linkingAdjustments": "0.00"}}


@pytest.mark.parametrize("currency", ["EUR", "BASE_SUMMARY"])
def test_optional_nav_amount_preserves_components_period_and_native_twr(currency):
    data = ledger()
    data["native_nav_change"] = nav_source()
    data["native_nav_change"]["currency"] = currency
    # Extra ledger cash/fees are not added or deducted a second time.
    data["fee_total"] = {"amount": "987654.321"}
    result = build_ibkr_period_return(data, observed_at=OBSERVED, **SCOPE)
    assert result["nav_change"] == {"method": "ibkr_nav_reconciliation", "source": "ChangeInNAV",
        "components": nav_source()["components"], "amount": "250.00"}
    assert result["period"] == data["native_nav_change"]["period"] and result["currency"] == "EUR"
    del result["nav_change"]
    assert result == build_ibkr_period_return(ledger(), observed_at=OBSERVED, **SCOPE)


@pytest.mark.parametrize("field", ["startingValue", "endingValue", "depositsWithdrawals",
                                  "internalCashTransfers", "assetTransfers", "linkingAdjustments"])
@pytest.mark.parametrize("value", [None, "", "NaN", "1e3", " 1.00", "9" * 65])
def test_invalid_or_missing_nav_components_never_block_twr(field, value):
    data = ledger()
    data["native_nav_change"] = nav_source()
    data["native_nav_change"]["components"][field] = value
    assert build_ibkr_period_return(data, observed_at=OBSERVED, **SCOPE) == build_ibkr_period_return(
        ledger(), observed_at=OBSERVED, **SCOPE)


@pytest.mark.parametrize("field", ["internalCashTransfers", "assetTransfers", "linkingAdjustments"])
@pytest.mark.parametrize("value", ["1.00", "-1.00", "0.000000000000000001"])
def test_nonzero_ambiguous_capital_components_omit_only_nav_amount(field, value):
    data = ledger()
    data["native_nav_change"] = nav_source()
    data["native_nav_change"]["components"][field] = value
    result = build_ibkr_period_return(data, observed_at=OBSERVED, **SCOPE)
    assert "nav_change" not in result and result["value"] == "-0.0125"


@pytest.mark.parametrize("field,value", [
    ("source", "Other"), ("account_id", "DU0000001"), ("model", "model-name"), ("model", None),
    ("currency", "USD"), ("currency", None), ("currency", ["EUR"]),
    ("period", {"from": "2026-01-01", "to": "2026-01-30"}), ("period", None),
])
def test_nav_source_identity_currency_model_and_period_must_match_without_blocking_twr(field, value):
    data = ledger()
    data["native_nav_change"] = nav_source()
    data["native_nav_change"][field] = value
    result = build_ibkr_period_return(data, observed_at=OBSERVED, **SCOPE)
    assert "nav_change" not in result and result["method"] == "native_ibkr_twr"


@pytest.mark.parametrize("candidate", [None, [], "private-value", {"components": None}])
def test_missing_or_malformed_optional_nav_source_does_not_change_old_payload(candidate):
    data = ledger()
    data["native_nav_change"] = candidate
    assert build_ibkr_period_return(data, observed_at=OBSERVED, **SCOPE) == build_ibkr_period_return(
        ledger(), observed_at=OBSERVED, **SCOPE)


@pytest.mark.parametrize("change", ["missing", "extra"])
def test_nav_component_set_is_exact_and_does_not_leak_unknown_financial_fields(change):
    data = ledger()
    data["native_nav_change"] = nav_source()
    components = data["native_nav_change"]["components"]
    if change == "missing":
        del components["depositsWithdrawals"]
    else:
        components["privateFinancialKey"] = "987654.321"
    assert "nav_change" not in build_ibkr_period_return(data, observed_at=OBSERVED, **SCOPE)


def test_nav_amount_uses_exact_decimal_arithmetic_independent_of_context():
    from decimal import localcontext

    data = ledger()
    data["native_nav_change"] = nav_source()
    data["native_nav_change"]["components"].update({
        "startingValue": "1000000000000000000000000000.000000000000000001",
        "endingValue": "1000000000000000000000000001.000000000000000003",
        "depositsWithdrawals": "0.000000000000000001"})
    with localcontext() as context:
        context.prec = 4
        result = build_ibkr_period_return(data, observed_at=OBSERVED, **SCOPE)
    assert result["nav_change"]["amount"] == "1.000000000000000001"


@pytest.mark.parametrize("ending,deposit,expected", [("10001.00", "1.00", "0.00"),
    ("9999.50", "1.00", "-1.50"), ("10001.00", "-1.00", "2.00")])
def test_nav_reconciliation_preserves_signed_deposits_losses_and_zero(ending, deposit, expected):
    data = ledger()
    data["native_nav_change"] = nav_source()
    data["native_nav_change"]["components"].update({"endingValue": ending, "depositsWithdrawals": deposit})
    assert build_ibkr_period_return(data, observed_at=OBSERVED, **SCOPE)["nav_change"]["amount"] == expected


def test_nav_amount_outside_wire_decimal_limit_omits_only_optional_amount():
    data = ledger()
    data["native_nav_change"] = nav_source()
    data["native_nav_change"]["components"].update({"startingValue": "-" + "9" * 63,
                                                 "endingValue": "9" * 64, "depositsWithdrawals": "0"})
    assert "nav_change" not in build_ibkr_period_return(data, observed_at=OBSERVED, **SCOPE)
