"""Pure projection of one verified Flex native interval for QRS private ingress."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from fractions import Fraction
import re
from typing import Any


SCHEMA_VERSION = "ibkr_account_period_return.v1"
_ACCOUNT = re.compile(r"(?:U|DU)[0-9]+", re.ASCII)
_TARGET = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", re.ASCII)
_SCOPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", re.ASCII)
_BINDING = re.compile(r"[a-f0-9]{64}", re.ASCII)
_CURRENCY = re.compile(r"[A-Z]{3}", re.ASCII)
_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", re.ASCII)


class PeriodReturnError(ValueError):
    """Fixed, safe projection failure; never includes private source values."""


def validate_period_return_scope(
    *, target_id: str, source_binding_id: str, account_scope: str,
    expected_account_ids: Sequence[str],
) -> None:
    if (
        not isinstance(target_id, str) or not _TARGET.fullmatch(target_id)
        or not isinstance(source_binding_id, str) or not _BINDING.fullmatch(source_binding_id)
        or not isinstance(account_scope, str) or not _SCOPE.fullmatch(account_scope)
        or not isinstance(expected_account_ids, (tuple, list)) or len(expected_account_ids) != 1
        or not isinstance(expected_account_ids[0], str) or not _ACCOUNT.fullmatch(expected_account_ids[0])
    ):
        raise PeriodReturnError("scope_invalid")


def _day(value: object) -> str:
    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        raise PeriodReturnError("period_invalid") from None
    return value


def _number(value: object) -> Fraction:
    if not isinstance(value, str) or len(value) > 64 or not _DECIMAL.fullmatch(value):
        raise PeriodReturnError("native_value_invalid")
    # Fraction(Decimal) compares exactly, independent of the ambient Decimal context.
    return Fraction(Decimal(value))


def build_ibkr_period_return(
    ledger: Mapping[str, Any], *, target_id: str, source_binding_id: str,
    account_scope: str, expected_account_ids: Sequence[str], observed_at: datetime,
) -> dict[str, Any]:
    """Keep the broker's interval, base currency and percent; never calculate TWR."""
    validate_period_return_scope(
        target_id=target_id, source_binding_id=source_binding_id,
        account_scope=account_scope, expected_account_ids=expected_account_ids,
    )
    if (
        not isinstance(ledger, Mapping) or ledger.get("schema_version") != "ibkr_flex_ledger.v1"
        or ledger.get("account_ids") != list(expected_account_ids)
    ):
        raise PeriodReturnError("identity_mismatch")
    native, assessment = ledger.get("native_returns"), ledger.get("return_assessment")
    if not isinstance(native, Mapping) or not native or not isinstance(assessment, Mapping) or assessment.get("computable") is not True:
        raise PeriodReturnError("native_twr_unavailable")
    if (
        native.get("unit") != "percent" or native.get("source") != "ChangeInNAV.twr"
        or assessment.get("method") != "native_ibkr_twr" or assessment.get("source") != "ChangeInNAV.twr"
        or assessment.get("unit") != "ratio"
    ):
        raise PeriodReturnError("native_method_invalid")
    period = ledger.get("period")
    if not isinstance(period, Mapping):
        raise PeriodReturnError("period_invalid")
    start, end = _day(period.get("from")), _day(period.get("to"))
    if start > end or native.get("from") != start or native.get("to") != end:
        raise PeriodReturnError("period_invalid")
    if not isinstance(observed_at, datetime) or observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise PeriodReturnError("observation_invalid")
    observed = observed_at.astimezone(timezone.utc)
    if end > observed.date().isoformat():
        raise PeriodReturnError("observation_invalid")
    currency = ledger.get("account_base_currency")
    if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency):
        raise PeriodReturnError("currency_invalid")
    percent, ratio = _number(native.get("twr")), _number(assessment.get("value"))
    if ratio * 100 != percent or ratio < -1:
        raise PeriodReturnError("native_value_invalid")
    return {
        "schema_version": SCHEMA_VERSION, "target_id": target_id,
        "source_binding_id": source_binding_id, "account_scope": account_scope,
        "account_ids": list(expected_account_ids), "observed_at": observed.isoformat().replace("+00:00", "Z"),
        "currency": currency, "period": {"from": start, "to": end},
        "method": "native_ibkr_twr", "source": "ChangeInNAV.twr", "source_unit": "percent",
        "source_value": native["twr"], "unit": "ratio", "value": assessment["value"],
    }
