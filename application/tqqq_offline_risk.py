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
QPK_REVISION = "7840127862ea715e44c8f70b03155a399dc817de"


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _number(value):
    _require(type(value) in (int, float) and math.isfinite(value), "TQQQ_FINITE_CONFIG_REQUIRED")
    return value


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
    reserve = max(_number(config["execution_cash_reserve_ratio"]), _number(settings.reserved_cash_ratio))
    _require(0 <= reserve <= 1 and runtime.cash_reserve_ratio == reserve, "TQQQ_EFFECTIVE_RESERVE_MISMATCH")
    _require(all(config.get(key) is False for key in (
        "option_overlay_enabled", "option_growth_overlay_enabled", "option_income_overlay_enabled")),
        "TQQQ_OPTIONS_DISABLED_REQUIRED")
    floor = _number(settings.reserved_cash_floor_usd)
    _require(floor >= 0 and _number(runtime.cash_reserve_floor_usd) == floor,
        "TQQQ_EFFECTIVE_RESERVE_FLOOR_MISMATCH")

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
    required_reserve = max(Decimal(str(account["nav"])) * Decimal(str(reserve)), Decimal(str(floor)))
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


def run_tqqq_offline_risk(request, *, runtime, portfolio_snapshot, packet, event_context,
        journal, binding, action, event_id=None, initialization=None,
        expected_artifact_sha256=None, consumption_cutoff_utc=None, inventory_evidence=None):
    """Initialize/read/persist/preflight an explicit artificial TQQQ contract.

    Callers retain the external checkpoint and draft receipt. There is no reset,
    automatic initialization, current-directory discovery, broker or apply API.
    An existing CLOSED record never substitutes for missing effective policy.
    """
    out = dict(status="TQQQ_OFFLINE_RISK_GAPS", gaps=[], platform_profile=PROFILE,
        draft_settings=None, preview=None, next_binding=None, artifact_receipt=None,
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
