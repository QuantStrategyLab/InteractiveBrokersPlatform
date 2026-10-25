"""Source-preserving cash fields and a value-free diagnostic projection.

The parent account snapshot supplies identity and observation time. These are
reported fields, not a settlement query, a complete obligation book or spending
authority. Neither function connects to a broker or computes buying power.
"""
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
import re

SCHEMA = "ibkr.cash_observations.v1"
TAGS = frozenset({"$LEDGER-CashBalance", "$LEDGER-TotalCashBalance", "CashBalance",
    "TotalCashBalance", "SettledCash", "AvailableFunds", "FullAvailableFunds"})


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() else None


def _decimal_text(value):
    amount = _decimal(value)
    if amount is None:
        return None
    digits, exponent = len(amount.as_tuple().digits), amount.as_tuple().exponent
    # Bound serialization, not a financial balance. Never expand malformed
    # vendor scientific notation into an unbounded string or truncate a value.
    if digits + abs(exponent) > 4096:
        return None
    return format(amount, "f")


def capture_cash_observation_rows(account_values, *, account_ids):
    """Keep selected native fields before float conversion and key overwrite.

    Invalid raw values become None, never stored raw error text. Duplicate
    rows remain separate so the projection can mark that field ambiguous.
    """
    rows = []
    for item in account_values:
        account = str(getattr(item, "account", "") or "").strip()
        currency = str(getattr(item, "currency", "") or "").strip().upper()
        tag = str(getattr(item, "tag", "") or "").strip()
        if account not in account_ids or not re.fullmatch(r"[A-Z]{3}", currency) or tag not in TAGS:
            continue
        rows.append(dict(account_id=account, currency=currency,
            **{tag: _decimal_text(getattr(item, "value", None))}))
    return tuple(rows)


def build_cash_observations(rows, *, observed_at):
    """Keep every supported field after the parent has checked account scope."""
    values, invalid, seen = {}, set(), set()
    for row in rows:
        currency = str(row.get("currency") or "").strip().upper()
        if currency == "BASE":
            continue
        for tag in sorted(TAGS & row.keys()):
            identity = currency, tag
            amount = _decimal_text(row[tag])
            if amount is None or identity in seen:
                invalid.add(identity)
                values.pop(identity, None)
            else:
                values[identity] = amount
            seen.add(identity)
    result = dict(schema_version=SCHEMA, observed_at=observed_at)
    # Runtime reports omit empty containers. Optional arrays keep the source
    # attachment byte-shape stable through that existing writer and readback.
    if values:
        result["values"] = [dict(currency=currency, source_tag=tag, value=amount)
            for (currency, tag), amount in sorted(values.items())]
    if invalid:
        result["invalid_fields"] = [dict(currency=currency, source_tag=tag)
            for currency, tag in sorted(invalid)]
    return result


def inspect_cash_observations(value, *, observed_at, currency="USD", selected_cash=None):
    """Validate the attachment; emit only fixed per-tag states, never amounts.

    Call after the original report identity/time checks. An absent attachment
    keeps old reports readable. An invalid attachment cannot certify a field.
    """
    result = dict(status="NOT_REPORTED" if value is None else "INVALID",
        scope="REPORTED_ACCOUNT_VALUES_ONLY", fields=dict.fromkeys(sorted(TAGS), "MISSING"),
        spendable_cash_verified=False, complete_cash_obligations_verified=False)
    if (not isinstance(value, Mapping)
            or not {"schema_version", "observed_at"} <= set(value)
            or set(value) - {"schema_version", "observed_at", "values", "invalid_fields"}
            or value["schema_version"] != SCHEMA or value["observed_at"] != observed_at
            or not isinstance(observed_at, str) or not observed_at
            or not isinstance(value.get("values", []), list)
            or not isinstance(value.get("invalid_fields", []), list)):
        return result
    seen, fields, amounts = set(), dict(result["fields"]), {}
    for name, state in (("values", "OBSERVED"), ("invalid_fields", "INVALID")):
        for row in value.get(name, []):
            expected = {"currency", "source_tag", "value"} if name == "values" else {"currency", "source_tag"}
            if (not isinstance(row, Mapping) or set(row) != expected
                    or not isinstance(row["currency"], str) or not re.fullmatch(r"[A-Z]{3}", row["currency"])
                    or not isinstance(row["source_tag"], str) or row["source_tag"] not in TAGS):
                return result
            key = (row["currency"], row["source_tag"])
            if key in seen:
                return result
            seen.add(key)
            if name == "values":
                amount = _decimal(row["value"]) if isinstance(row["value"], str) else None
                if amount is None:
                    return result
                amounts[key] = amount
            if key[0] == currency:
                fields[key[1]] = state
    # Two projections of the same native field must agree. Different native
    # tags can legitimately differ and are never forced to equal one another.
    if isinstance(selected_cash, Mapping):
        tag = selected_cash.get("source_tag")
        if not isinstance(tag, str):
            return result
        key = (currency, tag)
        if key in seen and (key not in amounts or amounts[key] != _decimal(selected_cash.get("cash_balance"))):
            return result
    result.update(status="OBSERVED_FIELDS_ONLY", fields=fields)
    return result
