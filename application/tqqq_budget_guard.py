"""Default-off TQQQ projected-budget check with no I/O or order ports.

Enabled use requires an independently approved policy and a complete projected
reservation snapshot. This module neither creates policy nor observes balances.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
import math


@dataclass(frozen=True)
class BudgetCheck:
    allowed: bool
    status: str


_BINDING_KEYS = frozenset({
    "account_scope", "account_hash", "runtime_scope", "strategy_profile", "ues_revision", "execution_mode",
})
_CATEGORIES = frozenset({"equity", "cash_substitute", "income", "direct_options", "pending"})


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid amount")
    return float(value)


def _time(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timezone required")
    return result


def check_tqqq_budget(
    *, policy=None, snapshot=None, runtime_binding=None, decision_at=None, target_values=None,
) -> BudgetCheck:
    """Check post-decision occupation, including options and pending orders.

    No amount or age limit is inferred. Zero reservations must be explicit;
    all five categories share the same configured budget. The snapshot is a
    supplied contract, not verified account evidence or a final-submit check.
    """
    if policy is None or (isinstance(policy, Mapping) and policy.get("enabled", False) is False):
        return BudgetCheck(True, "disabled")
    try:
        if not isinstance(policy, Mapping) or policy.get("enabled") is not True:
            raise ValueError("invalid enabled")
        binding = policy["binding"]
        if (not isinstance(binding, Mapping) or set(binding) != _BINDING_KEYS
                or any(not isinstance(value, str) or not value.strip() for value in binding.values())
                or binding["strategy_profile"] != "tqqq_growth_income"
                or runtime_binding != binding):
            return BudgetCheck(False, "rejected:budget_binding")
        budget = _number(policy["max_budget_usd"])
        borrowing_limit = _number(policy["max_borrowing_usd"])
        age_limit = _number(policy["max_age_seconds"])
        if age_limit <= 0:
            raise ValueError("invalid age")
        if not isinstance(snapshot, Mapping) or snapshot.get("binding") != binding:
            return BudgetCheck(False, "rejected:budget_snapshot")
        now = _time(decision_at)
        if _time(snapshot["decision_at"]) != now:
            return BudgetCheck(False, "rejected:budget_cycle")
        age = (now - _time(snapshot["observed_at"])).total_seconds()
        if not 0 <= age <= age_limit:
            return BudgetCheck(False, "rejected:budget_freshness")
        if _number(snapshot["borrowing_usd"]) > borrowing_limit:
            return BudgetCheck(False, "rejected:borrowing_limit")
        if not isinstance(target_values, Mapping) or not target_values:
            raise ValueError("missing targets")
        actual_targets = {symbol: _number(value) for symbol, value in target_values.items()}
        projected_targets = snapshot.get("target_values")
        if not isinstance(projected_targets, Mapping):
            raise ValueError("missing projected targets")
        projected_targets = {symbol: _number(value) for symbol, value in projected_targets.items()}
        if projected_targets != actual_targets:
            return BudgetCheck(False, "rejected:budget_targets")
        reservations = snapshot["reservations"]
        if not isinstance(reservations, (tuple, list)):
            raise ValueError("invalid reservations")
        ids = set()
        categories = set()
        total = 0.0
        security_total = 0.0
        for row in reservations:
            identifier, category = row["id"], row["category"]
            if (not isinstance(identifier, str) or not identifier.strip() or identifier in ids
                    or category not in _CATEGORIES):
                raise ValueError("invalid reservation")
            ids.add(identifier)
            categories.add(category)
            amount = _number(row["amount_usd"])
            total += amount
            if category in {"equity", "cash_substitute", "income"}:
                security_total += amount
        if categories != _CATEGORIES or security_total < sum(actual_targets.values()):
            return BudgetCheck(False, "rejected:budget_incomplete")
        if not math.isfinite(total) or total > budget:
            return BudgetCheck(False, "rejected:budget_limit")
    except (KeyError, TypeError, ValueError, OverflowError):
        return BudgetCheck(False, "rejected:budget_input")
    return BudgetCheck(True, "checked")
