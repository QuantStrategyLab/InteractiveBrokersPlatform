"""Artificial supplied returns only; no strategy execution or real research."""

from copy import deepcopy
import json
import socket

import pandas as pd
import pytest

from application.relative_risk_preview import RelativeRiskPreviewError, preview_relative_risk
from quant_platform_kit.strategy_lifecycle.benchmark_catalog import StrategyBenchmarkBinding


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("offline relative-risk validation prohibits network")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


def inputs(profile="tqqq_growth_income", symbol="QQQ"):
    dates = pd.date_range("2000-01-03", periods=4, freq="B")
    context = {"currency": "USD", "calendar_id": "ARTIFICIAL_EQUITY_SESSIONS",
        "window_id": "ARTIFICIAL_FIXED_WINDOW", "frequency": "daily",
        "return_basis": "NET_SIMPLE_TOTAL_RETURN", "cash_flow_basis": "EXTERNAL_FLOWS_REMOVED",
        "cost_treatment": "APPLICABLE_COSTS_INCLUDED",
        "cash_income_treatment": "INCLUDED_IN_NET_WEALTH",
        "periods_per_year": 252, "volatility_ddof": 1}
    return dict(strategy_returns=pd.Series([.1, -.1, .02, -.02], index=dates),
        benchmark_returns=pd.Series([.02, -.06, .01, -.01], index=dates),
        binding=StrategyBenchmarkBinding(profile, "buy_hold_" + symbol, "unleveraged_underlying"),
        strategy_context={**context, "series_id": profile, "source_sha256": "a" * 64},
        benchmark_context={**context, "series_id": "buy_hold_" + symbol, "source_sha256": "b" * 64})


def assert_unarmed(out):
    assert out["preview_only"] is True
    for key in ("settings_applied", "execution_authorized", "financial_risk_certified", "policy_selected"):
        assert out[key] is False
    assert out["source_authority"] == "CALLER_DECLARED_NOT_VERIFIED"
    json.dumps(out, allow_nan=False)


@pytest.mark.parametrize("profile,symbol", [("tqqq_growth_income", "QQQ"), ("soxl_soxx_trend_income", "SOXX")])
def test_both_rules_are_visible_without_selecting_policy_or_inventing_numbers(profile, symbol):
    out = preview_relative_risk(**inputs(profile, symbol))
    # The later partial recovery and decline compound to a deeper drawdown
    # than either path's largest negative daily return.
    assert out["max_drawdown_loss_fraction"]["strategy"] == pytest.approx(.10036)
    assert out["max_drawdown_loss_fraction"]["benchmark"] == pytest.approx(.060094)
    assert out["drawdown_rule_previews"]["strict_no_excess"]["status"] == "EXCEEDS"
    assert out["drawdown_rule_previews"]["bounded_excess"]["status"] == "UNDETERMINED"
    assert out["drawdown_rule_previews"]["bounded_excess"]["limit_fraction"] is None
    assert "protected_capital" in out["remaining_policy_fields"]
    assert "drawdown_excess_method_and_value" in out["remaining_policy_fields"]
    assert_unarmed(out)


@pytest.mark.parametrize("rule,limit", [
    ({"method": "difference_fraction", "value": .05}, .110094),
    ({"method": "benchmark_multiple", "value": 2.}, .120188),
])
def test_bounded_rule_uses_only_explicit_artificial_method_and_value(rule, limit):
    out = preview_relative_risk(**inputs(), allowed_excess=rule)
    assert out["drawdown_rule_previews"]["bounded_excess"]["limit_fraction"] == pytest.approx(limit)
    assert out["drawdown_rule_previews"]["bounded_excess"]["status"] == "WITHIN"
    assert out["drawdown_rule_previews"]["strict_no_excess"]["status"] == "EXCEEDS"
    assert_unarmed(out)


def test_strict_rule_still_has_a_normal_within_case_and_bounded_rejection():
    packet = inputs()
    packet["strategy_returns"] = packet["strategy_returns"] * .5
    out = preview_relative_risk(**packet)
    assert out["drawdown_rule_previews"]["strict_no_excess"]["status"] == "WITHIN"
    out = preview_relative_risk(**inputs(), allowed_excess={"method": "difference_fraction", "value": .01})
    assert out["drawdown_rule_previews"]["bounded_excess"]["status"] == "EXCEEDS"
    assert_unarmed(out)


def test_auxiliary_volatility_and_tail_keep_their_units_and_no_policy_role():
    out = preview_relative_risk(**inputs())
    assert out["annualized_volatility"]["exceeds_benchmark"] is True
    assert out["annualized_volatility"]["ddof"] == 1
    tail = out["tail_diagnostic"]
    assert tail["strategy"]["value"] == pytest.approx(-.1)
    assert tail["benchmark"]["value"] == pytest.approx(-.06)
    assert tail["worse_than_benchmark"] is True
    assert tail["policy_role"] is None
    assert_unarmed(out)


def test_explicit_daily_close_is_retained_without_inferring_it_for_legacy_inputs():
    packet = inputs()
    legacy = preview_relative_risk(**packet)
    assert legacy.get("valuation_sampling") is None
    for key in ("strategy_context", "benchmark_context"):
        packet[key]["valuation_sampling"] = "DAILY_CLOSE"
    explicit = preview_relative_risk(**packet)
    assert explicit["valuation_sampling"] == "DAILY_CLOSE"
    assert explicit["input_sha256"] != legacy["input_sha256"]
    assert explicit["max_drawdown_loss_fraction"] == legacy["max_drawdown_loss_fraction"]
    assert_unarmed(explicit)


@pytest.mark.parametrize("strategy,benchmark", [
    ("DAILY_CLOSE", "INTRADAY"), ("INTRADAY", "DAILY_CLOSE"),
    ("INTRADAY", "INTRADAY"), ("DAILY_CLOSE", None), (None, "DAILY_CLOSE"),
    (None, None), (True, True),
])
def test_explicit_sampling_must_be_supported_and_match_on_both_paths(strategy, benchmark):
    packet = inputs()
    packet["strategy_context"]["valuation_sampling"] = strategy
    packet["benchmark_context"]["valuation_sampling"] = benchmark
    with pytest.raises(RelativeRiskPreviewError, match="^COMPARISON_SAMPLING_MISMATCH$"):
        preview_relative_risk(**packet)


@pytest.mark.parametrize("leg", ["strategy_context", "benchmark_context"])
def test_one_sided_sampling_declaration_cannot_certify_the_other_path(leg):
    packet = inputs()
    packet[leg]["valuation_sampling"] = "DAILY_CLOSE"
    with pytest.raises(RelativeRiskPreviewError, match="^COMPARISON_SAMPLING_MISMATCH$"):
        preview_relative_risk(**packet)


@pytest.mark.parametrize("leg", ["strategy_returns", "benchmark_returns"])
@pytest.mark.parametrize("kind", ["missing_date", "duplicate", "leading_nan", "middle_nan", "inf",
    "bankruptcy", "below_bankruptcy", "boolean", "complex", "unsorted", "intraday", "timezone"])
def test_invalid_path_never_drops_rows_or_changes_window(leg, kind):
    packet = inputs(); path = packet[leg].copy()
    if kind == "missing_date":
        path = path.iloc[1:]
    elif kind == "duplicate":
        path.index = [path.index[0], path.index[0], *path.index[2:]]
    elif kind in ("leading_nan", "middle_nan", "inf", "bankruptcy", "below_bankruptcy"):
        value = {"leading_nan": float("nan"), "middle_nan": float("nan"), "inf": float("inf"),
                 "bankruptcy": -1., "below_bankruptcy": -1.1}[kind]
        path.iloc[0 if kind == "leading_nan" else 1] = value
    elif kind == "boolean":
        path = pd.Series([True, False, True, False], index=path.index)
    elif kind == "complex":
        path = path.astype(complex) + 1j
    elif kind == "unsorted":
        path = path.iloc[::-1]
    elif kind == "intraday":
        path.index = path.index + pd.Timedelta(hours=12)
    elif kind == "timezone":
        path.index = path.index.tz_localize("UTC")
    packet[leg] = path
    with pytest.raises(RelativeRiskPreviewError):
        preview_relative_risk(**packet)


@pytest.mark.parametrize("key,value", [("currency", "HKD"), ("calendar_id", "OTHER"),
    ("periods_per_year", 365), ("volatility_ddof", 0), ("window_id", "OTHER")])
def test_mismatched_context_is_rejected_instead_of_comparing_inconsistent_metrics(key, value):
    packet = inputs(); packet["benchmark_context"][key] = value
    with pytest.raises(RelativeRiskPreviewError, match="COMPARISON_BASIS_MISMATCH"):
        preview_relative_risk(**packet)


@pytest.mark.parametrize("key,value", [("return_basis", "GROSS"), ("cash_flow_basis", "UNKNOWN"),
    ("cost_treatment", None), ("cash_income_treatment", None), ("frequency", "weekly"),
    ("source_sha256", None), ("volatility_ddof", True), ("periods_per_year", float("nan")),
    ("series_id", "buy_hold_QQQM")])
def test_missing_or_conflicting_basis_is_not_guessed(key, value):
    packet = inputs(); packet["benchmark_context"][key] = value
    with pytest.raises(RelativeRiskPreviewError):
        preview_relative_risk(**packet)


@pytest.mark.parametrize("rule", [
    {}, {"method": "difference_fraction", "value": None},
    {"method": "difference_fraction", "value": True},
    {"method": "difference_fraction", "value": -.01},
    {"method": "difference_fraction", "value": float("inf")},
    {"method": "benchmark_multiple", "value": .9},
    {"method": "unsupported", "value": 1},
])
def test_invalid_optional_excess_rule_is_not_a_default_permission(rule):
    with pytest.raises(RelativeRiskPreviewError):
        preview_relative_risk(**inputs(), allowed_excess=rule)


@pytest.mark.parametrize("strategy_flat", [False, True])
def test_zero_benchmark_drawdown_has_zero_multiple_limit_and_no_risk_qualification(strategy_flat):
    packet = inputs(); packet["benchmark_returns"][:] = .01
    if strategy_flat:
        packet["strategy_returns"][:] = 0.
    out = preview_relative_risk(**packet, allowed_excess={"method": "benchmark_multiple", "value": 2})
    bounded = out["drawdown_rule_previews"]["bounded_excess"]
    assert bounded["status"] == ("WITHIN" if strategy_flat else "EXCEEDS")
    assert bounded["limit_fraction"] == 0
    assert out["zero_benchmark_drawdown"] is True
    assert_unarmed(out)


def test_qqqm_is_not_accepted_as_tqqq_underlying_and_other_reference_must_be_predeclared():
    with pytest.raises(RelativeRiskPreviewError, match="UNDERLYING_REFERENCE_MISMATCH"):
        preview_relative_risk(**inputs(symbol="QQQM"))
    with pytest.raises(RelativeRiskPreviewError, match="PREDECLARED_MARKET_REFERENCE_REQUIRED"):
        preview_relative_risk(**inputs("OTHER_STRATEGY", "SOXX"))


def test_input_hash_is_stable_changes_with_evidence_and_does_not_mutate_inputs():
    packet = inputs(); original = deepcopy(packet)
    first = preview_relative_risk(**packet); second = preview_relative_risk(**packet)
    assert first == second
    assert packet["strategy_context"] == original["strategy_context"]
    pd.testing.assert_series_equal(packet["strategy_returns"], original["strategy_returns"])
    packet["strategy_context"]["source_sha256"] = "c" * 64
    assert preview_relative_risk(**packet)["input_sha256"] != first["input_sha256"]
