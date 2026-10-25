"""Artificial reference paths, no downloaded prices or personal policy values."""
from copy import deepcopy
import json
import socket

import pytest

from application.relative_risk_preview import RelativeRiskPreviewError, preview_relative_risk
from tests.test_relative_risk_preview import assert_unarmed, inputs


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("reference validation prohibits network")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


def reference_inputs():
    packet = inputs()
    packet["strategy_context"].update(account_scope="ARTIFICIAL_SCOPE",
        wealth_scope="ALLOCATED_ACCOUNT_NET_WEALTH", account_risk_benchmark="buy_hold_QQQ",
        decision_cutoff_utc="2000-01-10T12:00:00Z")
    packet["benchmark_context"].update(account_scope="ARTIFICIAL_SCOPE",
        wealth_scope="ACCOUNT_RISK_REFERENCE_NET_WEALTH")
    context = deepcopy(packet["benchmark_context"])
    context.update(window_id="ARTIFICIAL_FROZEN_REFERENCE", source_sha256="d" * 64)
    reference = dict(benchmark_symbol="buy_hold_QQQ", reference_id="artificial-reference",
        available_at_utc="2000-01-03T00:00:00Z", frozen_at_utc="2000-01-04T00:00:00Z",
        dates=["1999-12-29", "1999-12-30", "1999-12-31"], net_returns=[.04, -.17, .03], context=context)
    return packet, reference


def test_reference_computes_a_separate_account_ceiling_without_a_numeric_preference():
    packet, reference = reference_inputs()
    out = preview_relative_risk(**packet, account_drawdown_reference=reference)
    rule = out["drawdown_rule_previews"]["benchmark_anchored_account"]
    assert rule["limit_fraction"] == pytest.approx(.17)
    assert rule["status"] == "WITHIN"
    assert rule["reference_window"]["start_date"] == "1999-12-29"
    assert rule["reference_qualified"] is False
    assert out["drawdown_rule_previews"]["bounded_excess"]["status"] == "UNDETERMINED"
    assert "account_reference_source_qualification" in out["remaining_policy_fields"]
    assert "absolute_loss_budget" not in out["remaining_policy_fields"]
    assert "selected_drawdown_rule" in out["remaining_policy_fields"]
    assert_unarmed(out)


def test_missing_reference_is_unknown_and_not_a_global_market_default():
    out = preview_relative_risk(**inputs())
    rule = out["drawdown_rule_previews"]["benchmark_anchored_account"]
    assert rule["status"] == "UNDETERMINED" and rule["limit_fraction"] is None
    assert_unarmed(out)


def test_account_reference_is_selected_separately_from_the_strategy_underlying():
    packet, reference = reference_inputs()
    packet["strategy_context"]["account_risk_benchmark"] = "CUSTOM_ACCOUNT_REFERENCE"
    reference["benchmark_symbol"] = reference["context"]["series_id"] = "CUSTOM_ACCOUNT_REFERENCE"
    out = preview_relative_risk(**packet, account_drawdown_reference=reference)
    assert out["binding"]["benchmark_symbol"] == "buy_hold_QQQ"
    assert out["drawdown_rule_previews"]["benchmark_anchored_account"]["benchmark_symbol"] == "CUSTOM_ACCOUNT_REFERENCE"
    assert_unarmed(out)


def test_reference_metric_failure_uses_a_fixed_error_without_source_details(monkeypatch):
    from application import relative_risk_preview as module
    original = module.compute_window_metrics

    def fail_reference(path, **kwargs):
        if len(path) == 3:
            raise ValueError("untrusted-source-details")
        return original(path, **kwargs)

    monkeypatch.setattr(module, "compute_window_metrics", fail_reference)
    packet, reference = reference_inputs()
    with pytest.raises(RelativeRiskPreviewError, match="^ACCOUNT_REFERENCE_METRIC_UNCOMPUTABLE$"):
        preview_relative_risk(**packet, account_drawdown_reference=reference)


def test_reference_ceiling_is_independent_of_candidate_and_current_benchmark_losses():
    packet, reference = reference_inputs()
    original = preview_relative_risk(**packet, account_drawdown_reference=reference)
    packet["strategy_returns"] *= .01
    packet["benchmark_returns"].iloc[:] = [-.6, 0., 0., 0.]
    changed = preview_relative_risk(**packet, account_drawdown_reference=reference)
    for result in (original, changed):
        assert result["drawdown_rule_previews"]["benchmark_anchored_account"]["limit_fraction"] == pytest.approx(.17)
        assert_unarmed(result)
    assert original["input_sha256"] != changed["input_sha256"]


def test_reference_hash_changes_for_nonbinding_source_or_freeze_changes():
    packet, reference = reference_inputs()
    original = preview_relative_risk(**packet, account_drawdown_reference=reference)
    for field, value in (("reference_id", "another-artificial-reference"),
                         ("frozen_at_utc", "2000-01-05T00:00:00Z")):
        changed = dict(reference, **{field: value})
        out = preview_relative_risk(**packet, account_drawdown_reference=changed)
        assert out["input_sha256"] != original["input_sha256"]


@pytest.mark.parametrize("fault", ["missing_source", "scope", "currency", "price_return", "wrong_symbol",
    "unselected", "role", "naive_time", "future_freeze", "future_availability", "same_day_availability",
    "duplicate_date", "nan", "annual_return", "missing_dates"])
def test_unknown_or_incomparable_reference_cannot_issue_a_ceiling(fault):
    packet, reference = reference_inputs()
    if fault == "missing_source": reference["context"]["source_sha256"] = None
    elif fault == "scope": reference["context"]["account_scope"] = "OTHER"
    elif fault == "currency": reference["context"]["currency"] = "HKD"
    elif fault == "price_return": reference["context"]["return_basis"] = "PRICE_RETURN"
    elif fault == "wrong_symbol": reference["benchmark_symbol"] = "buy_hold_SPY"
    elif fault == "unselected": del packet["strategy_context"]["account_risk_benchmark"]
    elif fault == "role": reference["context"]["wealth_scope"] = "FULL_STRATEGY"
    elif fault == "naive_time": reference["frozen_at_utc"] = "2000-01-04"
    elif fault == "future_freeze": reference["frozen_at_utc"] = "2000-01-11T00:00:00Z"
    elif fault == "future_availability": reference["available_at_utc"] = "2000-01-05T00:00:00Z"
    elif fault == "same_day_availability": reference["available_at_utc"] = "1999-12-31T23:59:59Z"
    elif fault == "duplicate_date": reference["dates"][1] = reference["dates"][0]
    elif fault == "nan": reference["net_returns"][1] = float("nan")
    elif fault == "annual_return": reference["net_returns"] = [-.17]
    else: del reference["dates"]
    with pytest.raises(RelativeRiskPreviewError):
        preview_relative_risk(**packet, account_drawdown_reference=reference)


def test_zero_drawdown_reference_is_an_observation_not_a_future_risk_guarantee():
    packet, reference = reference_inputs()
    reference["net_returns"] = [.01, .01, .01]
    out = preview_relative_risk(**packet, account_drawdown_reference=reference)
    rule = out["drawdown_rule_previews"]["benchmark_anchored_account"]
    assert rule["limit_fraction"] == 0 and rule["status"] == "EXCEEDS"
    assert rule["reference_qualified"] is False
    json.dumps(out, allow_nan=False)
    assert_unarmed(out)
