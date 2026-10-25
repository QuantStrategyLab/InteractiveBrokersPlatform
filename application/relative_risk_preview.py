"""Offline comparison of supplied net paths; never a policy or runtime permit.

Reuse QPK metrics and the existing benchmark binding. No prices are fetched,
strategies fitted, windows selected, settings applied or accounts accessed.
Source declarations identify caller inputs; they do not attest real evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping

import numpy as np
import pandas as pd

from quant_platform_kit.strategy_lifecycle.benchmark_catalog import StrategyBenchmarkBinding
from quant_platform_kit.strategy_lifecycle.performance_metrics import compute_window_metrics

_COMPARISON_FIELDS = ("currency", "calendar_id", "window_id", "frequency", "return_basis",
    "cash_flow_basis", "cost_treatment", "cash_income_treatment", "periods_per_year", "volatility_ddof")


class RelativeRiskPreviewError(ValueError):
    """Invalid comparison inputs, with fixed messages that omit source values."""


def _require(condition, code):
    if not condition:
        raise RelativeRiskPreviewError(code)


def _validate_path(returns, context):
    _require(isinstance(returns, pd.Series) and len(returns) >= 2, "RETURN_PATH_REQUIRED")
    index = returns.index
    _require(isinstance(index, pd.DatetimeIndex), "DAILY_SESSION_INDEX_REQUIRED")
    _require(index.tz is None and not index.hasnans and not index.has_duplicates
             and index.is_monotonic_increasing and index.equals(index.normalize()),
             "INVALID_DAILY_SESSION_INDEX")
    _require(not pd.api.types.is_bool_dtype(returns.dtype)
             and not any(isinstance(v, (bool, np.bool_, complex, np.complexfloating))
                         for v in returns), "INVALID_NET_RETURNS")
    try:
        values = pd.to_numeric(returns, errors="raise").astype(float)
    except (TypeError, ValueError):
        raise RelativeRiskPreviewError("INVALID_NET_RETURNS") from None
    _require(np.isfinite(values.to_numpy()).all() and (values > -1).all(), "INVALID_NET_RETURNS")
    _require(isinstance(context, Mapping), "COMPARISON_CONTEXT_REQUIRED")
    for key in ("currency", "calendar_id", "window_id", "series_id"):
        _require(isinstance(context.get(key), str) and bool(context[key].strip()),
                 "COMPARISON_CONTEXT_INCOMPLETE")
    _require(context.get("frequency") == "daily", "DAILY_RETURN_BASIS_REQUIRED")
    _require(context.get("return_basis") == "NET_SIMPLE_TOTAL_RETURN"
             and context.get("cash_flow_basis") == "EXTERNAL_FLOWS_REMOVED"
             and context.get("cost_treatment") == "APPLICABLE_COSTS_INCLUDED"
             and context.get("cash_income_treatment") == "INCLUDED_IN_NET_WEALTH",
             "NET_WEALTH_BASIS_REQUIRED")
    annualization = context.get("periods_per_year")
    _require(type(annualization) in (int, float) and math.isfinite(annualization)
             and annualization > 0, "ANNUALIZATION_REQUIRED")
    _require(type(context.get("volatility_ddof")) is int and context["volatility_ddof"] in (0, 1),
             "VOLATILITY_DDOF_REQUIRED")
    _require(isinstance(context.get("source_sha256"), str)
             and bool(re.fullmatch(r"[0-9a-f]{64}", context["source_sha256"])),
             "SOURCE_IDENTITY_REQUIRED")
    return values


def _drawdown_rules(strategy_loss, benchmark_loss, allowed_excess):
    strict = {"status": "WITHIN" if strategy_loss <= benchmark_loss else "EXCEEDS",
              "limit_fraction": benchmark_loss}
    bounded = {"status": "UNDETERMINED", "reason": "EXCESS_METHOD_AND_VALUE_UNSET",
               "limit_fraction": None}
    if allowed_excess is not None:
        _require(isinstance(allowed_excess, Mapping) and set(allowed_excess) == {"method", "value"},
                 "INVALID_EXCESS_RULE")
        method, value = allowed_excess["method"], allowed_excess["value"]
        _require(type(value) in (int, float) and math.isfinite(value), "INVALID_EXCESS_VALUE")
        if method == "difference_fraction":
            _require(0 <= value <= 1, "INVALID_EXCESS_VALUE")
            limit = benchmark_loss + value
        elif method == "benchmark_multiple":
            _require(value >= 1, "INVALID_EXCESS_VALUE")
            limit = benchmark_loss * value
        else:
            raise RelativeRiskPreviewError("INVALID_EXCESS_METHOD")
        _require(math.isfinite(limit), "INVALID_EXCESS_VALUE")
        bounded = {"status": "WITHIN" if strategy_loss <= limit else "EXCEEDS",
                   "reason": None,
                   "method": method, "value": value, "limit_fraction": limit}
    return {"strict_no_excess": strict, "bounded_excess": bounded}


def _benchmark_drawdown_rule(strategy_loss, reference, context):
    """A caller-selected, frozen account reference, not a live moving stop.

    The reference window can differ from the comparison window. Its known
    availability and freeze must precede the declared decision cutoff. These
    declarations and content hashes do not verify the issuer or data quality.
    """
    if reference is None:
        return dict(status="UNDETERMINED", reason="BENCHMARK_DRAWDOWN_REFERENCE_REQUIRED", limit_fraction=None)
    if isinstance(reference, Mapping) and "schema_version" in reference:
        _require(isinstance(context.get("account_scope"), str) and bool(context["account_scope"].strip())
            and context.get("wealth_scope") == "ALLOCATED_ACCOUNT_NET_WEALTH", "ACCOUNT_REFERENCE_SCOPE_REQUIRED")
        try:
            from quant_platform_kit.strategy_lifecycle.security_drawdown_reference import (
                SecurityDrawdownReferenceError, describe_security_drawdown_reference,
            )
        except ImportError:
            raise RelativeRiskPreviewError("SECURITY_REFERENCE_DEPENDENCY_UNAVAILABLE") from None
        try:
            result = describe_security_drawdown_reference(reference,
                selected_benchmark=context.get("account_risk_benchmark"), currency=context["currency"],
                calendar_id=context["calendar_id"], valuation_sampling=context.get("valuation_sampling"),
                decision_cutoff_utc=context.get("decision_cutoff_utc"))
        except SecurityDrawdownReferenceError as exc:
            raise RelativeRiskPreviewError(str(exc)) from None
        loss = result["max_drawdown_loss_fraction"]
        return dict(result, status="WITHIN" if strategy_loss <= loss else "EXCEEDS", reason=None,
            limit_fraction=loss, model="FROZEN_SECURITY_DRAWDOWN_PROXY")
    fields = {"benchmark_symbol", "reference_id", "available_at_utc", "frozen_at_utc", "dates", "net_returns", "context"}
    _require(isinstance(reference, Mapping) and set(reference) == fields, "ACCOUNT_REFERENCE_INCOMPLETE")
    _require(all(isinstance(reference[key], str) and bool(reference[key].strip())
                 for key in ("benchmark_symbol", "reference_id")), "ACCOUNT_REFERENCE_IDENTITY_REQUIRED")

    def timestamp(raw):
        _require(isinstance(raw, str) and bool(raw.strip()), "ACCOUNT_REFERENCE_TIME_REQUIRED")
        try:
            value = pd.Timestamp(raw)
            _require(not pd.isna(value) and value.tzinfo is not None, "ACCOUNT_REFERENCE_TIME_REQUIRED")
            return value.tz_convert("UTC")
        except (TypeError, ValueError, OverflowError):
            raise RelativeRiskPreviewError("ACCOUNT_REFERENCE_TIME_INVALID") from None

    available, frozen, cutoff = (timestamp(reference["available_at_utc"]),
        timestamp(reference["frozen_at_utc"]), timestamp(context.get("decision_cutoff_utc")))
    _require(available <= frozen <= cutoff, "ACCOUNT_REFERENCE_NOT_AVAILABLE_AT_FREEZE")
    raw_dates, raw_returns = reference["dates"], reference["net_returns"]
    _require(type(raw_dates) is list and type(raw_returns) is list
        and all(isinstance(day, str) and len(day) == 10 for day in raw_dates), "ACCOUNT_REFERENCE_DAILY_PATH_REQUIRED")
    try:
        dates = pd.DatetimeIndex(pd.to_datetime(raw_dates, format="%Y-%m-%d", errors="raise"))
        path = pd.Series(raw_returns, index=dates)
    except (TypeError, ValueError, OverflowError):
        raise RelativeRiskPreviewError("ACCOUNT_REFERENCE_DAILY_PATH_INVALID") from None
    source = reference["context"]
    path = _validate_path(path, source)
    _require(all(day.date() < available.date() for day in dates), "ACCOUNT_REFERENCE_UNFINISHED_DAILY_SESSION")
    _require(source["series_id"] == reference["benchmark_symbol"] == context.get("account_risk_benchmark"),
        "ACCOUNT_REFERENCE_SELECTION_MISMATCH")
    _require(isinstance(context.get("account_scope"), str) and bool(context["account_scope"].strip())
        and source.get("account_scope") == context["account_scope"]
        and source.get("wealth_scope") == "ACCOUNT_RISK_REFERENCE_NET_WEALTH"
        and context.get("wealth_scope") == "ALLOCATED_ACCOUNT_NET_WEALTH", "ACCOUNT_REFERENCE_SCOPE_REQUIRED")
    _require(all(source[key] == context[key] for key in _COMPARISON_FIELDS if key != "window_id"),
        "ACCOUNT_REFERENCE_BASIS_MISMATCH")
    try:
        metric = compute_window_metrics(path, **{key: source[key]
            for key in ("periods_per_year", "calendar_id", "volatility_ddof")})
    except (ArithmeticError, ValueError):
        raise RelativeRiskPreviewError("ACCOUNT_REFERENCE_METRIC_UNCOMPUTABLE") from None
    loss = -metric.max_drawdown
    _require(math.isfinite(loss) and 0 <= loss < 1, "ACCOUNT_REFERENCE_METRIC_UNCOMPUTABLE")
    return dict(status="WITHIN" if strategy_loss <= loss else "EXCEEDS", reason=None,
        limit_fraction=loss, model="FROZEN_BENCHMARK_DRAWDOWN_REFERENCE",
        benchmark_symbol=reference["benchmark_symbol"], reference_id=reference["reference_id"],
        reference_source_sha256=source["source_sha256"], frozen_at_utc=reference["frozen_at_utc"],
        reference_window=dict(start_date=dates[0].date().isoformat(), end_date=dates[-1].date().isoformat(),
            observations=len(path), window_id=source["window_id"]),
        reference_qualified=False, model_calibrated=False, enforcement_eligible=False)


def _dynamic_drawdown_rule(strategy_loss, benchmark_loss, policy, binding, context, benchmark_context,
                           reference_ceiling=None):
    """Hypothetical, independently supplied DD policy; never reuse a CVaR budget.

    Scale explicit account and relative allowances with the existing capital
    curve, then apply BOTH limits. No candidate/return statistic sets its own
    allowance. The curve's use for drawdown is a proposed policy, not a theorem
    or a calibrated risk model. Prior limits remain unverified caller state.
    """
    if policy is None:
        return dict(status="UNDETERMINED", reason="DYNAMIC_DRAWDOWN_POLICY_REQUIRED", limit_fraction=None)
    keys = {"synthetic", "loss_measure", "policy_id", "source_sha256", "binding",
        "comparison_context", "account_scope", "level", "nav", "curve", "levels",
        "account_max_drawdown_fraction", "previous_limits"}
    _require(isinstance(policy, Mapping) and set(policy) == keys, "DYNAMIC_DRAWDOWN_POLICY_INCOMPLETE")
    _require(policy["synthetic"] is True
        and policy["loss_measure"] == "MAX_DRAWDOWN_LOSS_FRACTION", "DYNAMIC_DRAWDOWN_BASIS_REQUIRED")
    _require(isinstance(policy["policy_id"], str) and bool(policy["policy_id"].strip())
        and isinstance(policy["source_sha256"], str)
        and re.fullmatch(r"[0-9a-f]{64}", policy["source_sha256"]), "DYNAMIC_DRAWDOWN_SOURCE_REQUIRED")
    _require(policy["binding"] == vars(binding)
        and policy["comparison_context"] == {key: context[key] for key in _COMPARISON_FIELDS},
        "DYNAMIC_DRAWDOWN_CONTEXT_MISMATCH")
    # A full-investment strategy/security path is not the allocated account.
    # These declarations bind the hypothetical path roles, not real provenance.
    _require(isinstance(policy["account_scope"], str) and bool(policy["account_scope"].strip())
        and context.get("account_scope") == benchmark_context.get("account_scope") == policy["account_scope"]
        and context.get("wealth_scope") == "ALLOCATED_ACCOUNT_NET_WEALTH"
        and benchmark_context.get("wealth_scope") == "ACCOUNT_RISK_REFERENCE_NET_WEALTH",
        "DYNAMIC_ACCOUNT_WEALTH_PATH_REQUIRED")

    def number(value, *, fraction=False):
        _require(type(value) in (int, float), "DYNAMIC_DRAWDOWN_NUMBER_INVALID")
        try:
            value = float(value)
        except OverflowError:
            raise RelativeRiskPreviewError("DYNAMIC_DRAWDOWN_NUMBER_INVALID") from None
        _require(math.isfinite(value) and value >= 0 and (not fraction or value <= 1),
            "DYNAMIC_DRAWDOWN_NUMBER_INVALID")
        return value

    levels = ("conservative", "balanced", "growth")
    _require(policy["level"] in levels and isinstance(policy["levels"], Mapping)
        and set(policy["levels"]) == set(levels), "DYNAMIC_DRAWDOWN_LEVELS_REQUIRED")
    ratios = []
    for level in levels:
        row = policy["levels"][level]
        _require(isinstance(row, Mapping) and set(row) == {"drawdown_fraction", "excess_fraction"},
            "DYNAMIC_DRAWDOWN_LEVELS_REQUIRED")
        ratios.append(tuple(number(row[key], fraction=True)
            for key in ("drawdown_fraction", "excess_fraction")))
    _require(all(all(a <= b for a, b in zip(column, column[1:])) for column in zip(*ratios)),
        "DYNAMIC_DRAWDOWN_LEVELS_UNORDERED")
    nav = number(policy["nav"])
    _require(nav > 0, "DYNAMIC_DRAWDOWN_NAV_REQUIRED")
    raw_ceiling = policy["account_max_drawdown_fraction"]
    if raw_ceiling is None:
        _require(reference_ceiling is not None, "DYNAMIC_ACCOUNT_CEILING_SOURCE_REQUIRED")
        account_ceiling = reference_ceiling
    else:
        account_ceiling = number(raw_ceiling, fraction=True)
        if reference_ceiling is not None:
            account_ceiling = min(account_ceiling, reference_ceiling)
    try:
        from us_equity_strategies.research.capital_adaptive_risk import (
            CapitalProtectionCurve, capital_protection_factor,
        )
    except ImportError:
        raise RelativeRiskPreviewError("DYNAMIC_CURVE_DEPENDENCY_UNAVAILABLE") from None
    try:
        curve = CapitalProtectionCurve(**policy["curve"])
        factor = capital_protection_factor(curve)
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise RelativeRiskPreviewError("DYNAMIC_CURVE_INVALID") from None
    _require(curve.currency == context["currency"] and curve.protected_capital >= nav,
        "DYNAMIC_CURVE_CAPITAL_OR_CURRENCY_MISMATCH")
    drawdown, extra = ratios[levels.index(policy["level"])]
    account_limit = min(drawdown * factor, account_ceiling)
    excess = extra * factor
    previous = policy["previous_limits"]
    if previous is not None:
        _require(isinstance(previous, Mapping)
            and set(previous) == {"account_limit_fraction", "excess_fraction"},
            "DYNAMIC_PREVIOUS_LIMITS_INCOMPLETE")
        account_limit = min(account_limit, number(previous["account_limit_fraction"], fraction=True))
        excess = min(excess, number(previous["excess_fraction"], fraction=True))
    relative_limit = benchmark_loss + excess
    combined = min(account_limit, relative_limit)
    return dict(status="WITHIN" if strategy_loss <= combined else "EXCEEDS", reason=None,
        model="CAPITAL_SCALED_DRAWDOWN_ENVELOPE_V1", level=policy["level"],
        capital_factor=factor, account_limit_fraction=account_limit, excess_fraction=excess,
        benchmark_relative_limit_fraction=relative_limit, combined_limit_fraction=combined,
        limit_fraction=combined, within_account_limit=strategy_loss <= account_limit,
        within_relative_limit=strategy_loss <= relative_limit,
        policy_source_sha256=policy["source_sha256"], model_calibrated=False, retained_state_verified=False)


def preview_relative_risk(
    strategy_returns, benchmark_returns, *, binding, strategy_context, benchmark_context,
    allowed_excess=None, dynamic_policy=None, account_drawdown_reference=None,
):
    """Describe hypothetical rules on an exact supplied comparison window.

    ``difference_fraction`` adds return-fraction units (0.01 = one percentage
    point); ``benchmark_multiple`` multiplies the benchmark loss magnitude.
    Neither method nor number has a default. CVaR95 is QPK's existing worst-5%
    empirical *signed daily return*, not an approved tail horizon or threshold.
    ``dynamic_policy`` optionally supplies independent, artificial drawdown
    policy inputs to the existing capital curve. Its account ceiling remains
    separate from its dynamic relative allowance. A caller-selected frozen
    ``account_drawdown_reference`` may supply the independent account ceiling
    from a separately declared prior window. Its explicit security-reference
    schema describes a daily adjusted-price proxy without claiming account
    net wealth; the account path still requires net returns. No number has a default.
    Every result remains offline, unapproved and without execution authority.
    """
    _require(type(binding) is StrategyBenchmarkBinding and binding.relative_drawdown_required,
             "BENCHMARK_BINDING_REQUIRED")
    # Keep the user's named underlying references; reporting QQQM is not QQQ.
    required = {"tqqq_growth_income": "QQQ", "soxl_soxx_trend_income": "SOXX"}
    if binding.strategy_profile in required:
        symbol = required[binding.strategy_profile]
        _require(binding.benchmark_kind == "unleveraged_underlying"
                 and binding.benchmark_symbol in (symbol, "buy_hold_" + symbol),
                 "UNDERLYING_REFERENCE_MISMATCH")
    else:
        _require(binding.benchmark_symbol in ("SPY", "QQQ", "buy_hold_SPY", "buy_hold_QQQ"),
                 "PREDECLARED_MARKET_REFERENCE_REQUIRED")
    strategy = _validate_path(strategy_returns, strategy_context)
    benchmark = _validate_path(benchmark_returns, benchmark_context)
    _require(strategy_context["series_id"] == binding.strategy_profile
             and benchmark_context["series_id"] == binding.benchmark_symbol,
             "RETURN_SOURCE_BINDING_MISMATCH")
    _require(strategy.index.equals(benchmark.index), "EXACT_SESSION_MATCH_REQUIRED")
    _require(all(strategy_context[k] == benchmark_context[k] for k in _COMPARISON_FIELDS),
             "COMPARISON_BASIS_MISMATCH")
    kwargs = {k: strategy_context[k] for k in ("periods_per_year", "calendar_id", "volatility_ddof")}
    # Lazy, read-only research statistics; this module has no runtime caller.
    from quant_platform_kit.research_stats.unified_report import build_unified_net_report

    try:
        metrics = [compute_window_metrics(path, **kwargs) for path in (strategy, benchmark)]
        tail = [build_unified_net_report(path).metric("cvar95_return_per_period")
                for path in (strategy, benchmark)]
    except (ArithmeticError, ValueError):
        raise RelativeRiskPreviewError("RISK_METRIC_UNCOMPUTABLE") from None
    losses = [-metric.max_drawdown for metric in metrics]
    _require(all(math.isfinite(v) for v in (*losses, *(m.volatility for m in metrics))),
             "RISK_METRIC_UNCOMPUTABLE")
    rules = _drawdown_rules(*losses, allowed_excess)
    rules["benchmark_anchored_account"] = _benchmark_drawdown_rule(
        losses[0], account_drawdown_reference, strategy_context)
    rules["capital_adaptive"] = _dynamic_drawdown_rule(
        *losses, dynamic_policy, binding, strategy_context, benchmark_context,
        reference_ceiling=rules["benchmark_anchored_account"]["limit_fraction"])
    digest_input = {
        "binding": vars(binding), "contexts": [dict(strategy_context), dict(benchmark_context)],
        "dates": [d.date().isoformat() for d in strategy.index],
        "returns": [strategy.tolist(), benchmark.tolist()], "allowed_excess": allowed_excess,
    }
    if dynamic_policy is not None:
        digest_input["dynamic_policy"] = dict(dynamic_policy)
    if account_drawdown_reference is not None:
        digest_input["account_drawdown_reference"] = dict(account_drawdown_reference)
    try:
        input_digest = hashlib.sha256(json.dumps(digest_input, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    except (TypeError, ValueError):
        raise RelativeRiskPreviewError("INVALID_COMPARISON_CONTEXT") from None
    return {
        "schema_version": "qsl.relative_risk_preview.v1", "preview_only": True,
        "settings_applied": False, "execution_authorized": False, "financial_risk_certified": False,
        "policy_selected": False, "source_authority": "CALLER_DECLARED_NOT_VERIFIED",
        "input_sha256": input_digest,
        "binding": vars(binding).copy(), "observations": len(strategy),
        "currency": strategy_context["currency"], "calendar_id": kwargs["calendar_id"],
        "start_date": strategy.index[0].date().isoformat(),
        "end_date": strategy.index[-1].date().isoformat(),
        "max_drawdown_loss_fraction": {"strategy": losses[0], "benchmark": losses[1]},
        "drawdown_rule_previews": rules,
        "annualized_volatility": {"strategy": metrics[0].volatility,
            "benchmark": metrics[1].volatility,
            "exceeds_benchmark": metrics[0].volatility > metrics[1].volatility,
            "periods_per_year": kwargs["periods_per_year"], "ddof": kwargs["volatility_ddof"],
            "policy_role": None},
        "tail_diagnostic": {"name": "cvar95_signed_daily_net_return",
            "strategy": tail[0].to_dict(), "benchmark": tail[1].to_dict(),
            "worse_than_benchmark": (tail[0].value < tail[1].value
                                     if all(t.computed for t in tail) else None),
            "policy_role": None},
        "remaining_policy_fields": ["selected_drawdown_rule", "comparison_horizon",
            "volatility_policy_role_and_limit", "tail_policy_role_horizon_and_limit",
            "protected_capital", "account_policy_approval"]
            + (["absolute_loss_budget"] if account_drawdown_reference is None else ["account_reference_source_qualification"])
            + (["drawdown_excess_method_and_value"] if allowed_excess is None and dynamic_policy is None else [])
            + (["dynamic_drawdown_calibration_and_source_approval"] if dynamic_policy is not None else []),
        "zero_benchmark_drawdown": losses[1] == 0,
    }
