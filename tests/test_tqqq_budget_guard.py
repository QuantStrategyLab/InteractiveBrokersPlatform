"""Pure synthetic budget checks. No broker/client or live entrypoint."""
from copy import deepcopy

import pytest

from application.tqqq_budget_guard import check_tqqq_budget


def _inputs():
    binding = {"account_scope": "SYNTH_ACCOUNT", "account_hash": "SYNTH_ACCOUNT_HASH",
               "runtime_scope": "SYNTH_RUNTIME", "execution_mode": "SYNTH_MODE",
               "strategy_profile": "tqqq_growth_income", "ues_revision": "SYNTH_REVISION"}
    return {
        "policy": {"enabled": True, "binding": dict(binding),
                   "max_budget_usd": 100.0, "max_borrowing_usd": 5.0, "max_age_seconds": 60.0},
        "runtime_binding": dict(binding),
        "decision_at": "2040-01-02T12:00:00+00:00",
        "target_values": {"SYNTH_EQUITY": 50.0},
        "snapshot": {
            "binding": dict(binding), "decision_at": "2040-01-02T12:00:00+00:00",
            "observed_at": "2040-01-02T11:59:30+00:00", "borrowing_usd": 5.0,
            "target_values": {"SYNTH_EQUITY": 50.0},
            "reservations": [
                {"id": category, "category": category, "amount_usd": amount}
                for category, amount in [("equity", 50.0), ("cash_substitute", 10.0),
                                         ("income", 10.0), ("direct_options", 10.0), ("pending", 20.0)]
            ],
        },
    }


def test_default_disabled_is_noop_without_selecting_a_limit():
    assert check_tqqq_budget().status == "disabled"
    assert check_tqqq_budget(policy={"enabled": False}).allowed is True


def test_exact_synthetic_budget_and_borrowing_boundary():
    result = check_tqqq_budget(**_inputs())
    assert result.allowed is True
    assert result.status == "checked"


@pytest.mark.parametrize("mutation", [
    "missing_limit", "limit_bool", "limit_nan", "missing_binding", "wrong_runtime",
    "wrong_snapshot", "missing_snapshot", "wrong_cycle", "stale", "future",
    "naive_time", "unknown_borrowing", "excess_borrowing", "excess_budget",
    "missing_pending", "duplicate_reservation", "negative_amount", "nonfinite_amount",
    "wrong_targets", "understated_target_budget", "string_enabled",
])
def test_enabled_guard_rejects_incomplete_or_excess_budget(mutation):
    inputs = deepcopy(_inputs())
    policy, snapshot = inputs["policy"], inputs["snapshot"]
    if mutation == "missing_limit":
        del policy["max_budget_usd"]
    elif mutation == "limit_bool":
        policy["max_budget_usd"] = True
    elif mutation == "limit_nan":
        policy["max_budget_usd"] = float("nan")
    elif mutation == "missing_binding":
        del policy["binding"]
    elif mutation == "wrong_runtime":
        inputs["runtime_binding"]["account_scope"] = "SYNTH_OTHER"
    elif mutation == "wrong_snapshot":
        snapshot["binding"]["ues_revision"] = "SYNTH_OTHER"
    elif mutation == "missing_snapshot":
        inputs["snapshot"] = None
    elif mutation == "wrong_cycle":
        snapshot["decision_at"] = "2040-01-01T12:00:00+00:00"
    elif mutation == "stale":
        snapshot["observed_at"] = "2040-01-02T11:58:59+00:00"
    elif mutation == "future":
        snapshot["observed_at"] = "2040-01-02T12:00:01+00:00"
    elif mutation == "naive_time":
        snapshot["observed_at"] = "2040-01-02T11:59:30"
    elif mutation == "unknown_borrowing":
        snapshot["borrowing_usd"] = None
    elif mutation == "excess_borrowing":
        snapshot["borrowing_usd"] = 5.01
    elif mutation == "excess_budget":
        snapshot["reservations"][-1]["amount_usd"] += 0.01
    elif mutation == "missing_pending":
        snapshot["reservations"].pop()
    elif mutation == "duplicate_reservation":
        snapshot["reservations"][-1]["id"] = "equity"
    elif mutation == "negative_amount":
        snapshot["reservations"][-1]["amount_usd"] = -1.0
    elif mutation == "nonfinite_amount":
        snapshot["reservations"][-1]["amount_usd"] = float("inf")
    elif mutation == "wrong_targets":
        inputs["target_values"]["SYNTH_EQUITY"] = 51.0
    elif mutation == "understated_target_budget":
        snapshot["reservations"][0]["amount_usd"] = 1.0
    elif mutation == "string_enabled":
        policy["enabled"] = "false"
    result = check_tqqq_budget(**inputs)
    assert result.allowed is False
    assert result.status.startswith("rejected:")
