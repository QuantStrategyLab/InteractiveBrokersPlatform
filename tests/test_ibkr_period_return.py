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
    (None, "account_ids", ["DU9999999"]), (None, "account_ids", ["DU0000000", "DU9999999"]),
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
