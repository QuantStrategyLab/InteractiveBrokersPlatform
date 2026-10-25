"""Explicit TQQQ artificial-policy/journal wiring; never registered in runtime.

The existing UES funding, event and draft consumers perform the calculations
and storage. This adapter binds their frozen inputs to one IBKR runtime target.
It issues no policy, checkpoint, order authority or configuration application.
Research imports stay lazy so production dependency pins remain unchanged.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal
from importlib.metadata import distribution
import json
import math

from application.account_facts import build_ibkr_account_facts

PROFILE = "tqqq_growth_income"
UES_REVISION = "3dfa547bccd73a562667a975dd91ffd65d47850a"
QPK_REVISION = "3ea9f47d67914c4e39143ed4348c7bc248b759ce"


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _number(value):
    _require(type(value) in (int, float) and math.isfinite(value), "TQQQ_FINITE_CONFIG_REQUIRED")
    return value


def _effective_cash_policy(runtime):
    from quant_platform_kit.common.runtime_control import CashPolicy

    settings = runtime.runtime_settings
    ratio = _number(settings.reserved_cash_ratio)
    floor = _number(settings.reserved_cash_floor_usd)
    strategy_ratio = _number(runtime.merged_runtime_config["execution_cash_reserve_ratio"])
    _require(0 <= ratio <= 1 and 0 <= strategy_ratio <= 1
        and _number(runtime.cash_reserve_ratio) == max(ratio, strategy_ratio), "TQQQ_EFFECTIVE_RESERVE_MISMATCH")
    _require(floor >= 0 and _number(runtime.cash_reserve_floor_usd) == floor,
        "TQQQ_EFFECTIVE_RESERVE_FLOOR_MISMATCH")
    _require(settings.cash_only_execution is True, "TQQQ_CASH_ONLY_REQUIRED")
    return CashPolicy(cash_only=True, reserved_cash_ratio=ratio, reserved_cash_floor_usd=floor)


def _cash_policy_packet(runtime, snapshot, packet, evidence):
    """Project one explicitly bound CashPolicy into the original reservations.

    A reservation ID identifies the SAME reserve on retries; an unrelated
    RESERVE cannot be guessed to overlap it. Native balances and obligations
    remain untouched. Both settings modes consume this same frozen packet.
    """
    from us_equity_strategies.research.auto_portfolio_account import adapt_ibkr_account_input, digest
    from us_equity_strategies.research.funded_portfolio_preview import _amount, _bound, adapt_cash_evidence

    contract = packet["account_contract"]
    _require(contract.get("synthetic") is True and runtime.runtime_settings.dry_run_only is True
        and runtime.runtime_settings.runtime_target.dry_run_only is True, "TQQQ_OFFLINE_RUNTIME_REQUIRED")
    _require(contract.get("currency") == "USD", "TQQQ_CASH_POLICY_USD_REQUIRED")
    try:
        _bound(evidence, contract)
    except (ValueError, TypeError, AttributeError, OverflowError) as exc:
        raise ValueError("TQQQ_CASH_POLICY_SOURCE_UNBOUND") from exc
    _require(evidence.get("read_status") == "RESOLVED", "TQQQ_CASH_POLICY_SOURCE_UNRESOLVED")
    policy = _effective_cash_policy(runtime)
    supplied = evidence.get("resolved_cash_policy")
    _require(isinstance(supplied, Mapping) and supplied == policy.to_dict()
        and supplied.get("cash_only") is True, "TQQQ_CASH_POLICY_SOURCE_MISMATCH")
    for key in ("reserved_cash_ratio", "reserved_cash_floor_usd"):
        _number(supplied[key])
    _require(evidence.get("config_sha256") == digest(runtime.merged_runtime_config),
        "TQQQ_CASH_POLICY_CONFIG_MISMATCH")
    identity = evidence.get("reservation_id")
    _require(isinstance(identity, str) and bool(identity.strip()), "TQQQ_CASH_POLICY_RESERVATION_ID_REQUIRED")
    _native_cash(snapshot, packet["cash_evidence"])
    nav = _amount(packet["facts"]["net_assets"], positive=True)
    _require(Decimal(str(_number(snapshot.total_equity))) == nav,
        "TQQQ_EXECUTION_EQUITY_DENOMINATOR_MISMATCH")
    strategy_ratio = runtime.merged_runtime_config["execution_cash_reserve_ratio"]
    # Use the deployed helper, including its number semantics, for both the
    # generated reservation and the later minimum-reserve admission check.
    reserve = _amount(policy.reserved_cash_usd(float(nav), strategy_ratio))
    prepared = deepcopy(packet)
    rows = prepared["cash_evidence"]["reservations"]
    _require(isinstance(rows, list) and all(isinstance(row, Mapping) for row in rows),
        "TQQQ_CASH_POLICY_RESERVATIONS_INVALID")
    matched = [row for row in rows if row.get("id") == identity]
    _require(len(matched) <= 1 and all(row.get("id") == identity
        for row in rows if row.get("kind") == "RESERVE"), "TQQQ_CASH_POLICY_RESERVE_AMBIGUOUS")
    if matched:
        row = matched[0]
        _require(row.get("kind") == "RESERVE" and _amount(row.get("amount")) == reserve,
            "TQQQ_CASH_POLICY_RESERVATION_CONFLICT")
        _require(row.get("cash_policy_evidence", evidence) == evidence,
            "TQQQ_CASH_POLICY_RESERVATION_CONFLICT")
        row["cash_policy_evidence"] = deepcopy(evidence)
    else:
        rows.append(dict(id=identity, kind="RESERVE", amount=str(reserve), cash_policy_evidence=deepcopy(evidence)))
    cash = adapt_cash_evidence(prepared["facts"], contract, prepared["cash_evidence"])
    account = adapt_ibkr_account_input(prepared["facts"], cash["account_contract"])
    gaps = sorted(set(cash["gaps"] + account["gaps"]))
    if not gaps:
        prepared["account_contract"] = cash["account_contract"]
    preview = dict(status="CASH_POLICY_PREVIEW_GAPS" if gaps else "READY_SYNTHETIC_CASH_POLICY_PREVIEW",
        gaps=gaps, currency=contract["currency"], resolved_cash_policy=policy.to_dict(),
        effective_reserve_ratio=policy.effective_reserve_ratio(strategy_ratio),
        required_reserve_usd=str(reserve), settled_cash=cash["account_contract"].get("settled_cash"),
        other_cash_obligations=cash["account_contract"].get("other_cash_obligations"),
        spendable_cash=account["spendable_cash"], source=evidence["source"],
        source_version=evidence["source_version"], observed_at=evidence["observed_at"],
        evidence_sha256=digest(evidence), reservation_id=identity,
        frozen_cutoff_utc=contract["cutoff_utc"], readback_scope="FROZEN_INPUT_NOT_SERVING_CONFIG",
        source_attestation_only=True, execution_authorized=False, settings_applied=False)
    return prepared, preview


def _research_dependencies():
    """Check the actual isolated distributions; no fallback to weaker contracts."""
    for name, revision in (("us-equity-strategies", UES_REVISION), ("quant-platform-kit", QPK_REVISION)):
        direct = json.loads(distribution(name).read_text("direct_url.json") or "{}")
        _require(direct.get("vcs_info", {}).get("commit_id") == revision,
            "TQQQ_ISOLATED_RESEARCH_REVISION_REQUIRED")


def _native_cash(snapshot, evidence):
    """CashBalance projection does not attest the separately consumed SettledCash."""
    native = snapshot.metadata.get("cash_balances")
    _require(isinstance(native, (tuple, list)), "TQQQ_NATIVE_CASH_MISSING")
    for row in evidence["account_values"]:
        matches = [item for item in native if item.get("account_id") == row["account_id"]
            and item.get("currency") == row["currency"] and row["tag"] in item]
        _require(len(matches) == 1, "TQQQ_NATIVE_CASH_TAG_MISSING_OR_AMBIGUOUS")
        supplied, actual = row["value"], matches[0][row["tag"]]
        _require(not isinstance(supplied, bool) and not isinstance(actual, bool), "TQQQ_NATIVE_CASH_INVALID")
        supplied, actual = Decimal(str(supplied)), Decimal(str(actual))
        _require(supplied.is_finite() and actual.is_finite() and supplied == actual,
            "TQQQ_NATIVE_CASH_CONFLICT")


def build_tqqq_offline_account_facts(snapshot, *, account_contract, inventory_evidence=None):
    """Keep native facts; explicit artificial completeness may attest an empty set.

    The production producer intentionally omits empty or unknown inventory. An
    omitted field alone is never enough. This separate research projection uses
    the existing scoped/time-bound evidence contract and never publishes facts.
    """
    from us_equity_strategies.research.auto_portfolio_account import digest
    from us_equity_strategies.research.funded_portfolio_preview import _bound

    facts = build_ibkr_account_facts(snapshot)
    if inventory_evidence is None:
        return facts
    _bound(inventory_evidence, account_contract)
    _require(account_contract.get("synthetic") is True
        and account_contract.get("positions_complete") is True
        and account_contract.get("asset_scope") == "STOCKS_AND_CASH_ONLY",
        "TQQQ_EMPTY_INVENTORY_CONTRACT_REQUIRED")
    _require(facts.get("account_ids") == [account_contract["account_id"]]
        and facts.get("currency") == account_contract["currency"]
        and inventory_evidence.get("account_snapshot_sha256") == digest(facts),
        "TQQQ_EMPTY_INVENTORY_SNAPSHOT_MISMATCH")
    _require(isinstance(snapshot.positions, (tuple, list)) and not snapshot.positions
        and isinstance(snapshot.metadata.get("option_positions"), (tuple, list))
        and not snapshot.metadata["option_positions"]
        and inventory_evidence.get("positions_complete") is True
        and inventory_evidence.get("asset_scope") == "STOCKS_AND_CASH_ONLY",
        "TQQQ_COMPLETE_EMPTY_INVENTORY_REQUIRED")
    return dict(facts, broker_reported_positions=[], broker_reported_positions_scope="stocks_only")


def _admit(runtime, snapshot, packet, context, inventory_evidence):
    from us_equity_strategies.research.auto_portfolio_account import digest
    from us_equity_strategies.research.auto_portfolio_preview import _hash
    from us_equity_strategies.research.funded_portfolio_preview import (
        _bound, _execution_rules, adapt_cash_evidence,
    )
    from us_equity_strategies.research.auto_portfolio_account import adapt_ibkr_account_input
    from us_equity_strategies.research.capital_adaptive_risk import preview_capital_adaptive_target
    from us_equity_strategies.research.cvar_exposure_constraint import Action, DecisionBook
    from us_equity_strategies.research.portfolio_risk_events_preview import BASES, LEVELS, _limits

    settings = runtime.runtime_settings
    target = settings.runtime_target
    _require(runtime.profile == settings.strategy_profile == target.strategy_profile == PROFILE,
        "TQQQ_PROFILE_MISMATCH")
    _require(settings.dry_run_only is True and target.dry_run_only is True,
        "TQQQ_OFFLINE_RUNTIME_REQUIRED")
    _require(settings.cash_only_execution is True, "TQQQ_CASH_ONLY_REQUIRED")
    contract = packet["account_contract"]
    _require(contract.get("synthetic") is True, "TQQQ_ARTIFICIAL_INPUT_REQUIRED")
    _require(tuple(settings.account_ids) == (contract["account_id"],)
        and target.account_scope == contract["scope_ref"], "TQQQ_ACCOUNT_SCOPE_MISMATCH")
    _require(bool(target.service_name or target.deployment_selector), "TQQQ_RUNTIME_SCOPE_REQUIRED")
    _require(packet["moments"]["strategy_ids"] == [PROFILE]
        and set(packet["composition"]["weights"]) == {PROFILE}, "TQQQ_STRATEGY_COMPOSITION_MISMATCH")
    # Empty inventory requires separate bound artificial evidence; native
    # omission remains unknown when that evidence is absent.
    _require(build_tqqq_offline_account_facts(snapshot, account_contract=contract,
        inventory_evidence=inventory_evidence) == packet["facts"], "TQQQ_NATIVE_ACCOUNT_FACTS_MISMATCH")
    options = snapshot.metadata.get("option_positions")
    _require(isinstance(options, (tuple, list)) and not options, "TQQQ_COMPLETE_EMPTY_OPTION_INVENTORY_REQUIRED")
    _native_cash(snapshot, packet["cash_evidence"])
    capital, status = runtime._build_capital_base_capabilities(snapshot)
    _require(status.startswith("verified:") and capital.get("capital_base") is not None,
        "TQQQ_CAPITAL_EVIDENCE_UNBOUND")

    policy = settings.trusted_runtime_risk_policy
    _require(isinstance(policy, Mapping), "TQQQ_EVENT_POLICY_MISSING")
    _require(policy == context.get("policy"), "TQQQ_EVENT_POLICY_MISMATCH")
    _bound(policy, contract)
    _require(policy.get("approved") is True and bool(policy.get("policy_id"))
        and all(policy.get(k) == v for k, v in BASES.items()), "TQQQ_EVENT_POLICY_UNBOUND")
    _require(set(policy["thresholds_by_level"]) == set(LEVELS), "TQQQ_ALL_LEVEL_POLICIES_REQUIRED")
    for level in LEVELS:
        _limits(policy["thresholds_by_level"][level])

    release = target.strategy_release
    _require(release is not None and release.strategy_revision == UES_REVISION,
        "TQQQ_STRATEGY_RELEASE_MISMATCH")
    _require(release.config_sha256 == digest(runtime.merged_runtime_config), "TQQQ_FROZEN_CONFIG_MISMATCH")
    funding_hash = _hash(packet["risk_inputs"])
    _require(funding_hash is not None and release.risk_policy_sha256 == digest(
        dict(event_policy=policy, funding_inputs_sha256=funding_hash)), "TQQQ_FROZEN_POLICY_MISMATCH")

    # Bind the execution reserve actually used by LoadedStrategyRuntime, rather
    # than borrowing SOXL's reserve or exit-buffer constants.
    config = runtime.merged_runtime_config
    cash_policy = _effective_cash_policy(runtime)
    _require(all(config.get(key) is False for key in (
        "option_overlay_enabled", "option_growth_overlay_enabled", "option_income_overlay_enabled")),
        "TQQQ_OPTIONS_DISABLED_REQUIRED")

    # Reuse original admission for budgets, curves, costs, quantities and cash.
    # HOLD validates the frozen risk contract without generating a candidate.
    # A breached holding still permits reading/retaining its protective state.
    cash = adapt_cash_evidence(packet["facts"], contract, packet["cash_evidence"])
    account = adapt_ibkr_account_input(packet["facts"], cash["account_contract"])
    _, rule_gaps = _execution_rules(packet["execution_rules"], contract, packet["risk_inputs"])
    gaps = sorted(set(cash["gaps"] + account["gaps"] + rule_gaps))
    if gaps:
        return gaps
    _require(account["nav"] == capital["capital_base"].target_equity, "TQQQ_CAPITAL_DENOMINATOR_MISMATCH")
    _require(_number(snapshot.total_equity) == account["nav"],
        "TQQQ_EXECUTION_EQUITY_DENOMINATOR_MISMATCH")
    # Same max(ratio * equity, fixed floor) as the existing execution service.
    # This reserve is already deducted by adapt_cash_evidence, exactly once.
    required_reserve = Decimal(str(cash_policy.reserved_cash_usd(
        account["nav"], config["execution_cash_reserve_ratio"])))
    _require(Decimal(str(cash["account_contract"]["reserved_cash"])) >= required_reserve,
        "TQQQ_RESERVE_NOT_FUNDED")
    risk = {key: value for key, value in packet["risk_inputs"].items() if key != "scenario_available_at"}
    scenario = risk["scenario_set"]
    _require(scenario.descriptor.scope == contract["scope_ref"]
        and scenario.descriptor.currency == contract["currency"], "TQQQ_SCENARIO_ACCOUNT_SCOPE_MISMATCH")
    _require(set(account["current_weights"]) <= set(scenario.symbols), "TQQQ_HELD_SECURITY_OUTSIDE_SCENARIO_SCOPE")
    book = DecisionBook({symbol: account["current_weights"].get(symbol, 0.)
        for symbol in scenario.symbols}, account["nav"], contract["currency"], 0.)
    for level in LEVELS:
        checked = preview_capital_adaptive_target(level=level, proposal=Action("HOLD_POSITIONS"),
            decision_book=book, **risk)
        _require(checked["status"] in {"PROPOSAL_FEASIBLE", "MANDATORY_TAIL_REDUCTION",
            "TAIL_REDUCTION_REQUIRED_NO_CERTIFIED_TARGET"}, "TQQQ_FUNDING_POLICY_UNBOUND")
    return []


def _relative_comparison(context, packet, request, nav):
    """Bind a declared diagnostic to the existing draft fingerprint, not a limit.

    The original UES receipt hashes the entire event context. Neither a new
    journal nor a second artifact is needed; omission or any input change is
    therefore checked by its existing read-only consumer preflight.
    """
    if "relative_risk_comparison" not in context:
        return None
    import pandas as pd

    from quant_platform_kit.strategy_lifecycle.benchmark_catalog import StrategyBenchmarkBinding
    from us_equity_strategies.research.auto_portfolio_account import digest, utc
    from us_equity_strategies.research.funded_portfolio_preview import _bound
    from us_equity_strategies.research.portfolio_intraday_state_io import HEADER
    from us_equity_strategies.research.portfolio_risk_events_preview import _portfolio_id
    from application.relative_risk_preview import preview_relative_risk

    try:
        evidence = context["relative_risk_comparison"]
        fields = {"portfolio_id", "request_sha256", "returns", "binding", "strategy_context",
                  "benchmark_context", "allowed_excess", "dynamic_policy"}
        _require(isinstance(evidence, Mapping) and (set(evidence) == HEADER | fields
            or set(evidence) == HEADER | fields | {"account_drawdown_reference"}),
            "TQQQ_RELATIVE_COMPARISON_INCOMPLETE")
        _bound(evidence, packet["account_contract"])
        _require(evidence["portfolio_id"] == _portfolio_id(packet)
            and evidence["request_sha256"] == digest(request), "TQQQ_RELATIVE_REQUEST_MISMATCH")
        contract = packet["account_contract"]
        reference = evidence.get("account_drawdown_reference")
        if reference is not None:
            _require(utc(evidence["strategy_context"].get("decision_cutoff_utc")) == utc(contract["cutoff_utc"]),
                "TQQQ_ACCOUNT_REFERENCE_CUTOFF_MISMATCH")
        for key, role in (("strategy_context", "ALLOCATED_ACCOUNT_NET_WEALTH"),
                          ("benchmark_context", "ACCOUNT_RISK_REFERENCE_NET_WEALTH")):
            _require(evidence[key].get("account_scope") == contract["scope_ref"]
                and evidence[key].get("currency") == contract["currency"]
                and evidence[key].get("wealth_scope") == role,
                "TQQQ_RELATIVE_ACCOUNT_PATH_MISMATCH")
        policy = evidence["dynamic_policy"]
        if policy is not None:
            _require(policy["level"] == request["level"] and policy["nav"] == nav,
                "TQQQ_RELATIVE_LEVEL_OR_CAPITAL_MISMATCH")
        paths = evidence["returns"]
        _require(isinstance(paths, Mapping) and set(paths) == {"dates", "strategy", "benchmark"}
            and all(type(paths[key]) is list for key in paths)
            and all(isinstance(value, str) and len(value) == 10 for value in paths["dates"]),
            "TQQQ_RELATIVE_PATHS_REQUIRED")
        dates = pd.DatetimeIndex(pd.to_datetime(paths["dates"], format="%Y-%m-%d", errors="raise"))
        _require(len(dates) >= 2 and all(day.date() < utc(contract["cutoff_utc"]).date() for day in dates),
            "TQQQ_RELATIVE_PRIOR_DAILY_SESSIONS_REQUIRED")
        return preview_relative_risk(
            pd.Series(paths["strategy"], index=dates), pd.Series(paths["benchmark"], index=dates),
            binding=StrategyBenchmarkBinding(**evidence["binding"]),
            strategy_context=evidence["strategy_context"], benchmark_context=evidence["benchmark_context"],
            allowed_excess=evidence["allowed_excess"], dynamic_policy=policy,
            account_drawdown_reference=reference)
    except Exception as exc:
        # Never expose a supplied path, identity, exception body or policy value.
        reason = str(exc)
        raise ValueError(reason if reason.startswith("TQQQ_RELATIVE_")
                         else "TQQQ_RELATIVE_COMPARISON_INVALID") from None


def run_tqqq_offline_risk(request, *, runtime, portfolio_snapshot, packet, event_context,
        journal, binding, action, event_id=None, initialization=None,
        expected_artifact_sha256=None, consumption_cutoff_utc=None, inventory_evidence=None,
        cash_policy_evidence=None):
    """Initialize/read/persist/preflight an explicit artificial TQQQ contract.

    Callers retain the external checkpoint and draft receipt. There is no reset,
    automatic initialization, current-directory discovery, broker or apply API.
    An existing CLOSED record never substitutes for missing effective policy.
    """
    out = dict(status="TQQQ_OFFLINE_RISK_GAPS", gaps=[], platform_profile=PROFILE,
        draft_settings=None, preview=None, cash_policy_preview=None, next_binding=None, artifact_receipt=None,
        relative_risk_preview=None, relative_risk_input_bound_to_draft=False, relative_risk_enforced=False,
        readback_verified=False, artifact_readback_verified=False, state_persisted=False,
        breaker_state="UNKNOWN", new_risk_prohibited=True, application_status="NOT_APPLIED",
        confirmation_granted=False, execution_authorized=False, settings_applied=False,
        breaker_reset=False, account_policy_bound=False, financial_risk_certified=False,
        authority_classification="ARTIFICIAL_SOURCE_ATTESTATIONS_ONLY")
    try:
        _require(action in {"initialize", "read", "persist", "preflight"}, "TQQQ_ACTION_UNSUPPORTED")
        _research_dependencies()
        from us_equity_strategies.research.portfolio_intraday_state_io import (
            initialize_portfolio_event_state, read_portfolio_event_state,
        )
        from us_equity_strategies.research.portfolio_settings_draft_io import (
            persist_portfolio_settings_draft, preflight_portfolio_settings_draft,
        )

        # Reusing a prepared packet must not bypass policy revalidation merely
        # by omitting the optional convenience argument on a later read.
        reservations = packet["cash_evidence"].get("reservations")
        embedded = [row for row in reservations if isinstance(row, Mapping)
            and "cash_policy_evidence" in row] if isinstance(reservations, list) else []
        if embedded:
            _require(len(embedded) == 1 and embedded[0].get("kind") == "RESERVE"
                and isinstance(embedded[0]["cash_policy_evidence"], Mapping),
                "TQQQ_CASH_POLICY_RESERVE_AMBIGUOUS")
            source = embedded[0]["cash_policy_evidence"]
            _require(cash_policy_evidence is None or cash_policy_evidence == source,
                "TQQQ_CASH_POLICY_RESERVATION_CONFLICT")
            cash_policy_evidence = source
        if cash_policy_evidence is not None:
            packet, out["cash_policy_preview"] = _cash_policy_packet(
                runtime, portfolio_snapshot, packet, cash_policy_evidence)
            if out["cash_policy_preview"]["gaps"]:
                out["gaps"] = out["cash_policy_preview"]["gaps"]
                return out
        if action in {"persist", "preflight"}:
            out["relative_risk_preview"] = _relative_comparison(
                event_context, packet, request, portfolio_snapshot.total_equity)
        gaps = _admit(runtime, portfolio_snapshot, packet, event_context, inventory_evidence)
        if gaps:
            out["gaps"] = gaps
            return out
        if action == "preflight" and inventory_evidence is not None:
            from us_equity_strategies.research.funded_portfolio_preview import _bound
            _bound(inventory_evidence, dict(packet["account_contract"], cutoff_utc=consumption_cutoff_utc))
        from quant_platform_kit.common.strategy_risk_state import StrategyRiskStateIdentity
        from us_equity_strategies.research.auto_portfolio_account import digest
        from us_equity_strategies.research.portfolio_risk_events_preview import _portfolio_id
        expected = StrategyRiskStateIdentity("portfolio_risk_events", packet["account_contract"]["scope_ref"],
            _portfolio_id(packet), digest(event_context["policy"])).to_dict()
        _require(binding.get("identity") == expected, "TQQQ_JOURNAL_POLICY_OR_PORTFOLIO_MISMATCH")
        if action == "initialize":
            result = initialize_portfolio_event_state(journal=journal, binding=binding,
                account_contract=packet["account_contract"], initialization=initialization)
        elif action == "read":
            result = read_portfolio_event_state(journal=journal, binding=binding,
                account_contract=packet["account_contract"])
        elif action == "persist":
            result = persist_portfolio_settings_draft(request, journal=journal, binding=binding,
                event_id=event_id, event_context=event_context, **packet)
        else:
            result = preflight_portfolio_settings_draft(request, journal=journal, binding=binding,
                expected_artifact_sha256=expected_artifact_sha256,
                consumption_cutoff_utc=consumption_cutoff_utc, event_context=event_context, **packet)
        out.update(deepcopy(result))
        out["relative_risk_input_bound_to_draft"] = (
            out["relative_risk_preview"] is not None and out["artifact_readback_verified"])
        # Initialization and state-only reads grant no candidate permission.
        # A failed candidate can preserve a successfully committed OPEN event.
        if action in {"initialize", "read"} or out["gaps"]:
            out.update(new_risk_prohibited=True, draft_settings=None)
        return out
    except Exception as exc:
        reason = str(exc)
        out["gaps"] = [reason if reason.startswith(("TQQQ_", "EVENT_")) else "TQQQ_INPUT_OR_DEPENDENCY_UNAVAILABLE"]
        out["error_type"] = type(exc).__name__
        return out
