"""Display-only observations; never execution, count or routing authority."""
from __future__ import annotations

import math
import re
from collections.abc import Mapping


def minimum_notional_decision(*, symbol, observed_value, limit, currency):
    for value in (observed_value, limit):
        try:
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                return None
        except OverflowError:
            return None
    if limit is not None and limit <= 0:
        return None
    if observed_value is not None and limit is not None and observed_value >= limit:
        return None
    if not isinstance(currency, str) or currency and not re.fullmatch(r"[A-Z]{3}", currency):
        return None
    if not isinstance(symbol, str) or symbol and not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,15}", symbol):
        return None
    return dict(reason_code="min_notional", observed_value=observed_value, limit=limit,
                currency=currency, symbol=symbol)


def routine_decisions(execution):
    """Use amounts recorded at the decision point; old reports stay unknown."""
    if not isinstance(execution, Mapping):
        return []
    raw = execution.get("notification_routine_decisions")
    if raw is not None:
        if not isinstance(raw, (list, tuple)) or len(raw) > 100:
            return []
        result = []
        for item in raw:
            if not isinstance(item, Mapping) or item.get("reason_code") != "min_notional":
                return []
            clean = minimum_notional_decision(**{key: item.get(key, "" if key in {"symbol", "currency"} else None)
                for key in ("symbol", "observed_value", "limit", "currency")})
            if clean is None:
                return []
            if clean not in result:
                result.append(clean)
        return result
    reason = execution.get("no_op_reason")
    if not isinstance(reason, str) or reason.split(":", 1)[0] != "min_notional":
        return []
    symbols = reason.partition(":")[2].split(",") if ":" in reason else [""]
    result = []
    for symbol in symbols[:100]:
        clean = minimum_notional_decision(symbol=symbol, observed_value=None, limit=None,
            currency=execution.get("market_currency") or "")
        if clean is not None and clean not in result:
            result.append(clean)
    return result


def account_alias(values):
    """Only the owner-bound display alias, never infer from broker/target IDs."""
    value = values.get("DIGEST_ACCOUNT_ALIAS", "")
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value) else ""
