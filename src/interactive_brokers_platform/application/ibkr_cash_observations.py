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
SCHEDULE_SCHEMA = "ibkr.cash_schedule_observations.v1"
SCHEDULE_TAGS = frozenset({"SettledCashByDate", "SettledCashByDate-C", "SettledCashByDate-S"})


def _schedule_text(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return value if len(value.encode("utf-8")) <= 4096 else None
    except UnicodeError:
        return None


def capture_cash_schedule_rows(account_values, *, account_ids):
    """Preserve bounded native strings; never parse dates or select cash.

    BASE remains a separate source currency. Invalid types and oversized
    strings retain a null marker, never a truncated or fabricated value.
    """
    rows = []
    for item in account_values:
        account = str(getattr(item, "account", "") or "").strip()
        currency = str(getattr(item, "currency", "") or "").strip().upper()
        tag = str(getattr(item, "tag", "") or "").strip()
        if (account not in account_ids or tag not in SCHEDULE_TAGS
                or not (currency == "BASE" or re.fullmatch(r"[A-Z]{3}", currency))):
            continue
        raw = _schedule_text(getattr(item, "value", None))
        rows.append(dict(account_id=account, currency=currency, source_tag=tag, raw_value=raw))
    return tuple(rows)


def build_cash_schedule_observations(rows, *, account_id, observed_at):
    """Optional evidence only: malformed metadata does not invalidate cash."""
    if not isinstance(rows, (tuple, list)):
        return None
    values = []
    for row in rows:
        if (not isinstance(row, Mapping)
                or set(row) != {"account_id", "currency", "source_tag", "raw_value"}
                or row["account_id"] != account_id
                or not _schedule_row_valid({key: row[key] for key in ("currency", "source_tag", "raw_value")})):
            return None
        values.append({key: row[key] for key in ("currency", "source_tag", "raw_value") if row[key] is not None})
    result = dict(schema_version=SCHEDULE_SCHEMA, observed_at=observed_at)
    if values:
        result["values"] = values
    return result


def _schedule_row_valid(row):
    return (isinstance(row, Mapping) and {"currency", "source_tag"} <= set(row)
        and not set(row) - {"currency", "source_tag", "raw_value"}
        and isinstance(row["currency"], str)
        and (row["currency"] == "BASE" or re.fullmatch(r"[A-Z]{3}", row["currency"]))
        and isinstance(row["source_tag"], str) and row["source_tag"] in SCHEDULE_TAGS
        and (row.get("raw_value") is None or (isinstance(row["raw_value"], str)
            and _schedule_text(row["raw_value"]) is not None)))


def inspect_cash_schedule_observations(value, *, observed_at, currency="USD"):
    """Fixed states only; raw dates, amounts and arbitrary strings stay private."""
    result = dict(status="NOT_REPORTED" if value is None else "INVALID",
        fields=dict.fromkeys(sorted(SCHEDULE_TAGS), "MISSING"),
        date_key_semantics_verified=False, settled_cash_equivalence_verified=False,
        current_settlement_selected=False, spendable_cash_verified=False)
    if (not isinstance(value, Mapping)
            or not {"schema_version", "observed_at"} <= set(value)
            or set(value) - {"schema_version", "observed_at", "values"}
            or value["schema_version"] != SCHEDULE_SCHEMA
            or not isinstance(observed_at, str) or not observed_at
            or value["observed_at"] != observed_at
            or not isinstance(value.get("values", []), list)
            or any(not _schedule_row_valid(row) for row in value.get("values", []))):
        return result
    fields = {}
    for tag in sorted(SCHEDULE_TAGS):
        rows = [row for row in value.get("values", []) if row["currency"] == currency and row["source_tag"] == tag]
        fields[tag] = ("MISSING" if not rows else "AMBIGUOUS_ROWS" if len(rows) > 1
            else "RAW_NOT_PRESERVED" if rows[0].get("raw_value") is None else "OBSERVED_RAW")
    result.update(status="RAW_FIELDS_ONLY", fields=fields)
    return result


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


def capture_cash_observation_rows(account_values, *, account_ids, source_method="unspecified"):
    """Keep selected native fields before float conversion and key overwrite.

    Invalid raw values become None, never stored raw error text. Duplicate
    rows remain separate so the projection can mark that field ambiguous.
    """
    if source_method not in {"account_values_cache", "unspecified"}:
        raise ValueError("cash observation source is unsupported")
    rows = []
    for item in account_values:
        account = str(getattr(item, "account", "") or "").strip()
        currency = str(getattr(item, "currency", "") or "").strip().upper()
        tag = str(getattr(item, "tag", "") or "").strip()
        if account not in account_ids or not re.fullmatch(r"[A-Z]{3}", currency) or tag not in TAGS:
            continue
        raw = getattr(item, "value", None)
        model = getattr(item, "modelCode", None)
        model_scope = "no_model_code" if model == "" else "model_specific" if isinstance(model, str) and model.strip() else "unknown"
        rows.append(dict(account_id=account, currency=currency,
            source_method=source_method, model_scope=model_scope,
            value_representation="native_text" if isinstance(raw, str) else "non_text",
            segment_key="unsuffixed", **{tag: _decimal_text(raw)}))
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


_DETAIL_KEYS = ("source_method", "model_scope", "value_representation", "segment_key")


def build_cash_source_details(rows, *, observed_at):
    """Optional metadata for valid, unambiguous values in the original attachment."""
    details, seen, invalid = {}, set(), set()
    for row in rows:
        currency = str(row.get("currency") or "").strip().upper()
        for tag in sorted(TAGS & row.keys()):
            key = (currency, tag)
            if key in seen or _decimal_text(row[tag]) is None:
                invalid.add(key)
                details.pop(key, None)
            elif (key not in invalid and row.get("source_method") == "account_values_cache"
                    and all(k in row for k in _DETAIL_KEYS)):
                details[key] = {k: row[k] for k in _DETAIL_KEYS}
            seen.add(key)
    if not details:
        return None
    return dict(observed_at=observed_at, fields=[dict(currency=currency, source_tag=tag, **detail)
        for (currency, tag), detail in sorted(details.items())])


def build_account_risk_fact_readiness(rows, *, observed_at, currency="USD"):
    """Describe evidence available in this snapshot, without granting admission.

    The capture time is not a broker event watermark or a freshness guarantee.
    An account-wide settled value requires exactly one native row with explicit
    unmodelled account scope. Legacy grouped rows cannot establish that scope.
    """
    candidates = [row for row in rows if row.get("currency") == currency and "SettledCash" in row]
    settled = {"status": "MISSING", "currency": currency, "source_tag": "SettledCash"}
    if len(candidates) > 1:
        settled["status"] = "AMBIGUOUS"
    elif candidates:
        row = candidates[0]
        value = _decimal_text(row["SettledCash"])
        if value is None:
            settled["status"] = "INVALID"
        elif row.get("source_method") != "account_values_cache" or row.get("model_scope") != "no_model_code":
            settled["status"] = "UNVERIFIED_SCOPE"
        else:
            settled.update(status="OBSERVED", value=value, source_method="account_values_cache",
                model_scope="no_model_code", observed_at=observed_at)
    return {
        "schema_version": "ibkr.account_risk_fact_readiness.v1",
        "scope": "SNAPSHOT_REPORT_ONLY",
        "admission": "NOT_EVALUATED",
        "status": "NOT_READY",
        "snapshot_observed_at": observed_at,
        "broker_freshness": "UNVERIFIED",
        "settled_cash": settled,
        "fee_schedule": "UNVERIFIED",
        "quantity_permissions": "UNVERIFIED",
        "order_observation_watermark": "MISSING",
        "account_domain_and_approved_budget": "UNVERIFIED",
    }


def inspect_cash_source_details(value, *, cash_observations, observed_at, currency="USD", selected_cash=None):
    """Expose bounded metadata only for validated, present cash values."""
    result = dict(status="NOT_REPORTED" if value is None else "INVALID", fields={})
    if (not isinstance(value, Mapping) or set(value) != {"observed_at", "fields"}
            or value["observed_at"] != observed_at or not isinstance(value["fields"], list)
            or inspect_cash_observations(cash_observations, observed_at=observed_at, currency=currency, selected_cash=selected_cash)["status"] != "OBSERVED_FIELDS_ONLY"):
        return result
    allowed = {(row["currency"], row["source_tag"]) for row in cash_observations.get("values", [])}
    details = {}
    for row in value["fields"]:
        if (not isinstance(row, Mapping) or set(row) != {"currency", "source_tag", *_DETAIL_KEYS}
                or row.get("source_method") not in ("account_values_cache", "unspecified")
                or row.get("model_scope") not in ("no_model_code", "model_specific", "unknown")
                or row.get("value_representation") not in ("native_text", "non_text")
                or row.get("segment_key") != "unsuffixed"
                or not isinstance(row.get("currency"), str) or not isinstance(row.get("source_tag"), str)):
            return result
        key = (row["currency"], row["source_tag"])
        if key not in allowed or key in details:
            return result
        details[key] = {k: row[k] for k in _DETAIL_KEYS}
    result.update(status="OBSERVED_DETAILS", fields={tag: detail for (cur, tag), detail in details.items() if cur == currency})
    return result
