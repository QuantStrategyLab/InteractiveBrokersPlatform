"""Platform-to-research wiring with artificial money, policies and events."""
from copy import deepcopy
from dataclasses import fields, is_dataclass, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

if not os.environ.get("IBKR_OFFLINE_UES_SOURCE"):
    pytest.skip("requires isolated TQQQ research environment", allow_module_level=True)

from test_portfolio_intraday_state_io import intraday_fixture, move_observation
from test_portfolio_settings_preview import request_fixture
from quant_platform_kit.common.models import PortfolioSnapshot, Position
from quant_platform_kit.common.runtime_target import build_runtime_target
from quant_platform_kit.common.strategy_contracts import StrategyRuntimeAdapter
from us_equity_strategies.research.auto_portfolio_account import digest
from us_equity_strategies.research.auto_portfolio_preview import _hash
from us_equity_strategies.research.portfolio_event_journal import PortfolioEventJournal
from us_equity_strategies.research.portfolio_risk_events_preview import _portfolio_id

from application.account_facts import build_ibkr_account_facts
from application.tqqq_offline_risk import UES_REVISION, run_tqqq_offline_risk
from strategy_runtime import LoadedStrategyRuntime

PROFILE = "tqqq_growth_income"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("offline tests must not connect to any network")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def rename(value):
    names = {"A": "TQQQ", "B": "BOXX", "STRATEGY_A": PROFILE}
    if is_dataclass(value):
        return type(value)(**{f.name: rename(getattr(value, f.name)) for f in fields(value)})
    if isinstance(value, dict):
        return {names.get(k, k): rename(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(rename(v) for v in value)
    return names.get(value, value) if isinstance(value, str) else value


def fixture(opened=False):
    p, c, b, proof = rename(intraday_fixture(opened=opened, held=True))
    p["moments"].update(strategy_ids=[PROFILE], strategy_versions={PROFILE: "v1"},
        eligible_strategies={PROFILE: "v1"}, mu_excess=[.04], covariance=[[.04]])
    p["composition"].update(strategy_versions={PROFILE: "v1"}, weights={PROFILE: {"TQQQ": 1.}})
    snapshot = PortfolioSnapshot(
        as_of=datetime(2000, 1, 4, 12, tzinfo=timezone.utc), total_equity=100.,
        positions=(Position(symbol="TQQQ", quantity=1., market_value=1., currency="USD"),),
        metadata={"account_ids": ["ARTIFICIAL_ACCOUNT"], "account_hash": "ARTIFICIAL_HASH",
            "option_positions": (),
            "total_equity_source": "broker_net_liquidation", "broker_net_liquidation": 100.,
            "source_digest_sha256": "e" * 64,
            "cash_balances": [{"account_id": "ARTIFICIAL_ACCOUNT", "currency": "USD",
                "CashBalance": "99", "SettledCash": "99", "NetLiquidation": "100"}]})
    p["account_contract"]["settled_cash"] = "99"
    for row in p["cash_evidence"]["account_values"]:
        row["value"] = "99"
    c["observations"]["sleeves"][0].update(fee_adjusted_entry_value=1., market_value=1.,
        net_liquidation_value=.999, held_cycle_peak_net_value=.999, security_market_values={"TQQQ": 1.})
    p["facts"] = build_ibkr_account_facts(snapshot)
    c["observations"]["account_snapshot_sha256"] = digest(p["facts"])
    c["observations"]["fee_evidence_sha256"] = digest(p["execution_rules"]["fees"])
    if c.get("reduction_plan"):
        c["reduction_plan"]["account_snapshot_sha256"] = digest(p["facts"])
    b["identity"]["candidate_id"] = _portfolio_id(p)
    proof["identity"] = deepcopy(b["identity"])
    config = {"execution_cash_reserve_ratio": 0., "option_overlay_enabled": False,
        "option_growth_overlay_enabled": False, "option_income_overlay_enabled": False}
    risk_hash = digest(dict(event_policy=c["policy"], funding_inputs_sha256=_hash(p["risk_inputs"])))
    release = dict(release_id="artificial-offline-release", manifest_sha256="a" * 64,
        strategy_revision=UES_REVISION, config_sha256=digest(config), risk_policy_sha256=risk_hash,
        evidence_sha256="b" * 64, plugin_bundle_sha256="c" * 64, effective_session="2000-01-04")
    target = build_runtime_target(platform_id="ibkr", strategy_profile=PROFILE, dry_run_only=True,
        account_scope="ARTIFICIAL_SCOPE", service_name="artificial-offline-service", strategy_release=release)
    settings = SimpleNamespace(strategy_profile=PROFILE, runtime_target=target, account_ids=("ARTIFICIAL_ACCOUNT",),
        trusted_runtime_risk_policy=deepcopy(c["policy"]), cash_only_execution=True,
        reserved_cash_ratio=0., reserved_cash_floor_usd=0., dry_run_only=True)
    runtime = LoadedStrategyRuntime(entrypoint=SimpleNamespace(manifest=SimpleNamespace(profile=PROFILE)),
        runtime_settings=settings, runtime_adapter=StrategyRuntimeAdapter(), merged_runtime_config=config,
        cash_reserve_ratio=0., logger=lambda _: None)
    return runtime, snapshot, p, c, b, proof


@pytest.fixture
def packet():
    return fixture()


def call(root, args, action="persist", request=None, **kwargs):
    runtime, snapshot, p, c, b, proof = args
    return run_tqqq_offline_risk(request or request_fixture(), runtime=runtime, portfolio_snapshot=snapshot,
        packet=p, event_context=c, journal=PortfolioEventJournal(root / "journal", offline_root=root),
        binding=b, action=action, **kwargs)


def seed(root, args=None):
    args = fixture() if args is None else args
    initialized = call(root, args, "initialize", initialization=args[-1])
    assert initialized["readback_verified"], initialized
    args = (*args[:4], initialized["next_binding"], args[-1])
    return args


def assert_unarmed(out):
    for key in ("execution_authorized", "settings_applied", "breaker_reset", "account_policy_bound", "financial_risk_certified"):
        assert out[key] is False


def attach_relative_comparison(args, request):
    from tests.offline.test_dynamic_relative_risk import inputs, policy
    from us_equity_strategies.research.portfolio_intraday_state_io import HEADER

    _runtime, snapshot, packet, context, _binding, _proof = args
    source = inputs()
    scope = packet["account_contract"]["scope_ref"]
    for key in ("strategy_context", "benchmark_context"):
        source[key]["account_scope"] = scope
    proposed = policy(source, level=request["level"], capital=float(snapshot.total_equity))
    proposed["account_scope"] = scope
    context["relative_risk_comparison"] = {
        **{key: deepcopy(context["policy"][key]) for key in HEADER},
        "portfolio_id": _portfolio_id(packet), "request_sha256": digest(request),
        "returns": {"dates": ["1999-12-27", "1999-12-28", "1999-12-29", "1999-12-30"],
            "strategy": source["strategy_returns"].tolist(),
            "benchmark": source["benchmark_returns"].tolist()},
        "binding": vars(source["binding"]), "strategy_context": source["strategy_context"],
        "benchmark_context": source["benchmark_context"], "allowed_excess": None,
        "dynamic_policy": proposed,
    }


def attach_account_reference(args, request, *, security_proxy=False):
    from application.relative_risk_preview import _COMPARISON_FIELDS

    attach_relative_comparison(args, request)
    evidence = args[3]["relative_risk_comparison"]
    evidence["strategy_context"].update(account_risk_benchmark=evidence["binding"]["benchmark_symbol"],
        decision_cutoff_utc=args[2]["account_contract"]["cutoff_utc"])
    source = deepcopy(evidence["benchmark_context"])
    source.update(window_id="ARTIFICIAL_FROZEN_REFERENCE", source_sha256="d" * 64)
    evidence["account_drawdown_reference"] = dict(
        benchmark_symbol=evidence["binding"]["benchmark_symbol"], reference_id="artificial-frozen-reference",
        available_at_utc="2000-01-03T00:00:00Z", frozen_at_utc="2000-01-03T01:00:00Z",
        dates=["1999-12-29", "1999-12-30", "1999-12-31"], net_returns=[.04, -.17, .03], context=source)
    if security_proxy:
        from tests.test_benchmark_drawdown_reference import as_security_reference

        evidence["strategy_context"]["valuation_sampling"] = "DAILY_CLOSE"
        evidence["account_drawdown_reference"] = as_security_reference(evidence["account_drawdown_reference"])
    evidence["dynamic_policy"]["account_max_drawdown_fraction"] = None
    evidence["dynamic_policy"]["comparison_context"] = {
        key: evidence["strategy_context"][key] for key in _COMPARISON_FIELDS}


@pytest.mark.parametrize("mode", ["simple", "advanced"])
@pytest.mark.parametrize("level", ["conservative", "balanced", "growth"])
@pytest.mark.parametrize("security_proxy", [False, True])
def test_benchmark_account_reference_survives_original_draft_save_and_preflight(tmp_path, mode, level, security_proxy):
    args = seed(tmp_path); request = request_fixture(mode); request["level"] = level
    attach_account_reference(args, request, security_proxy=security_proxy)
    saved = call(tmp_path, args, request=request, event_id="reference-allocation-1")
    assert saved["status"] == "SETTINGS_DRAFT_PERSISTED", saved
    reference = saved["relative_risk_preview"]["drawdown_rule_previews"]["benchmark_anchored_account"]
    assert reference["limit_fraction"] == pytest.approx(.17) and reference["reference_qualified"] is False
    assert reference["enforcement_eligible"] is False
    if security_proxy:
        assert reference["reference_return_basis"] == "PROVIDER_ADJUSTED_CLOSE_RATIO"
        assert reference["account_costs_included"] is False
    args = (*args[:4], saved["next_binding"], args[-1])
    read = call(tmp_path, args, "preflight", request=request,
        expected_artifact_sha256=saved["artifact_receipt"]["artifact_sha256"],
        consumption_cutoff_utc="2000-01-04T12:01:30Z")
    assert read["status"] == "READY_OFFLINE_SETTINGS_PREFLIGHT", read
    assert read["relative_risk_input_bound_to_draft"]
    assert read["relative_risk_preview"] == saved["relative_risk_preview"]
    assert read["relative_risk_enforced"] is False
    assert_unarmed(read)


@pytest.mark.parametrize("change", ["omitted", "source", "freeze", "reference_path", "cutoff"])
@pytest.mark.parametrize("security_proxy", [False, True])
def test_frozen_reference_cannot_be_changed_or_omitted_to_reuse_an_old_draft(tmp_path, change, security_proxy):
    args = seed(tmp_path); request = request_fixture()
    attach_account_reference(args, request, security_proxy=security_proxy)
    saved = call(tmp_path, args, request=request, event_id="reference-allocation-1")
    assert saved["status"] == "SETTINGS_DRAFT_PERSISTED", saved
    args = (*args[:4], saved["next_binding"], args[-1])
    evidence = args[3]["relative_risk_comparison"]
    reference = evidence["account_drawdown_reference"]
    if change == "omitted": del evidence["account_drawdown_reference"]
    elif change == "source": reference["context"]["source_sha256"] = "f" * 64
    elif change == "freeze": reference["frozen_at_utc"] = "2000-01-03T02:00:00Z"
    elif change == "cutoff": evidence["strategy_context"]["decision_cutoff_utc"] = "2000-01-04T13:00:00Z"
    else: reference["returns" if security_proxy else "net_returns"][1] = -.19
    read = call(tmp_path, args, "preflight", request=request,
        expected_artifact_sha256=saved["artifact_receipt"]["artifact_sha256"],
        consumption_cutoff_utc="2000-01-04T12:01:30Z")
    assert read["draft_settings"] is None and read["gaps"]
    assert read["relative_risk_input_bound_to_draft"] is False
    assert_unarmed(read)


def test_relative_comparison_source_expires_at_consumption_without_refreshing_the_draft(tmp_path):
    args = seed(tmp_path); request = request_fixture()
    attach_relative_comparison(args, request)
    args[3]["relative_risk_comparison"]["max_age_seconds"] = 60
    saved = call(tmp_path, args, request=request, event_id="relative-comparison-1")
    assert saved["status"] == "SETTINGS_DRAFT_PERSISTED", saved
    args = (*args[:4], saved["next_binding"], args[-1])
    read = call(tmp_path, args, "preflight", request=request,
        expected_artifact_sha256=saved["artifact_receipt"]["artifact_sha256"],
        consumption_cutoff_utc="2000-01-04T12:01:30Z")
    assert read["gaps"] == ["SETTINGS_CURRENT_SOURCE_EVIDENCE_EXPIRED_OR_UNBOUND"]
    assert read["draft_settings"] is None and not read["relative_risk_input_bound_to_draft"]
    assert_unarmed(read)


def test_relative_comparison_roundtrip_keeps_open_and_reduction_only(tmp_path):
    args = seed(tmp_path, fixture(opened=True)); request = request_fixture()
    attach_relative_comparison(args, request)
    saved = call(tmp_path, args, request=request, event_id="relative-reduction-1")
    assert saved["status"] == "SETTINGS_DRAFT_PERSISTED", saved
    assert saved["breaker_state"] == "OPEN" and saved["new_risk_prohibited"]
    assert all(value == 0 for value in saved["draft_settings"]["security_target_weights"].values())
    args = (*args[:4], saved["next_binding"], args[-1])
    read = call(tmp_path, args, "preflight", request=request,
        expected_artifact_sha256=saved["artifact_receipt"]["artifact_sha256"],
        consumption_cutoff_utc="2000-01-04T12:01:30Z")
    assert read["relative_risk_input_bound_to_draft"] and read["breaker_state"] == "OPEN"
    assert read["consumer_action"] == "REDUCTION_PREVIEW_ONLY"
    assert_unarmed(read)


@pytest.mark.parametrize("mode", ["simple", "advanced"])
@pytest.mark.parametrize("level", ["conservative", "balanced", "growth"])
def test_relative_comparison_bound_to_original_draft_receipt_for_both_modes(tmp_path, mode, level):
    args = seed(tmp_path)
    request = request_fixture(mode); request["level"] = level
    attach_relative_comparison(args, request)
    saved = call(tmp_path, args, request=request, event_id="relative-comparison-1")
    assert saved["status"] == "SETTINGS_DRAFT_PERSISTED", saved
    assert saved["relative_risk_input_bound_to_draft"] is True
    args = (*args[:4], saved["next_binding"], args[-1])
    read = call(tmp_path, args, "preflight", request=request,
        expected_artifact_sha256=saved["artifact_receipt"]["artifact_sha256"],
        consumption_cutoff_utc="2000-01-04T12:01:30Z")
    assert read["status"] == "READY_OFFLINE_SETTINGS_PREFLIGHT", read
    assert read["relative_risk_input_bound_to_draft"] is True
    assert read["relative_risk_preview"] == saved["relative_risk_preview"]
    assert read["relative_risk_preview"]["drawdown_rule_previews"]["capital_adaptive"]["model_calibrated"] is False
    assert read["relative_risk_enforced"] is False
    assert_unarmed(saved); assert_unarmed(read)


@pytest.mark.parametrize("change", ["omit", "source", "nonbinding_limit", "path"])
def test_changed_or_omitted_relative_inputs_cannot_reuse_old_draft(tmp_path, change):
    args = seed(tmp_path); request = request_fixture()
    attach_relative_comparison(args, request)
    saved = call(tmp_path, args, request=request, event_id="relative-comparison-1")
    assert saved["status"] == "SETTINGS_DRAFT_PERSISTED", saved
    args = (*args[:4], saved["next_binding"], args[-1])
    evidence = args[3]["relative_risk_comparison"]
    if change == "omit": del args[3]["relative_risk_comparison"]
    elif change == "source": evidence["source_version"] = "different-artificial-source"
    elif change == "nonbinding_limit": evidence["dynamic_policy"]["account_max_drawdown_fraction"] = .9
    else: evidence["returns"]["strategy"][0] *= .5
    read = call(tmp_path, args, "preflight", request=request,
        expected_artifact_sha256=saved["artifact_receipt"]["artifact_sha256"],
        consumption_cutoff_utc="2000-01-04T12:01:30Z")
    assert read["draft_settings"] is None and read["gaps"]
    assert read["relative_risk_input_bound_to_draft"] is False
    assert_unarmed(read)


@pytest.mark.parametrize("change", ["real", "account", "portfolio", "request", "level", "nav", "future", "scope", "currency"])
def test_unbound_relative_input_never_produces_a_draft_or_resets_state(tmp_path, change):
    args = seed(tmp_path); request = request_fixture()
    attach_relative_comparison(args, request)
    evidence = args[3]["relative_risk_comparison"]
    if change == "real": evidence["synthetic"] = False
    elif change == "account": evidence["account_id"] = "OTHER"
    elif change == "portfolio": evidence["portfolio_id"] = "OTHER"
    elif change == "request": evidence["request_sha256"] = "f" * 64
    elif change == "level": evidence["dynamic_policy"]["level"] = "growth"
    elif change == "nav": evidence["dynamic_policy"]["nav"] += 1
    elif change == "future": evidence["returns"]["dates"][-1] = "2000-01-04"
    elif change == "scope": evidence["benchmark_context"]["account_scope"] = "OTHER"
    else: evidence["benchmark_context"]["currency"] = "HKD"
    before = deepcopy(args[4])
    result = call(tmp_path, args, request=request, event_id="relative-comparison-1")
    assert result["draft_settings"] is None and result["gaps"]
    assert result["state_persisted"] is False and args[4] == before
    assert_unarmed(result)


@pytest.mark.parametrize("mode", ["simple", "advanced"])
@pytest.mark.parametrize("level", ["conservative", "balanced", "growth"])
def test_tqqq_account_policy_cash_quantity_event_draft_round_trip(tmp_path, mode, level):
    args = seed(tmp_path)
    request = request_fixture(mode)
    request["level"] = level
    if mode == "advanced":
        request["overrides"] = {"stop_loss_ratio": .08}
    saved = call(tmp_path, args, request=request, event_id="allocation-1")
    assert saved["status"] == "SETTINGS_DRAFT_PERSISTED", saved
    assert saved["artifact_readback_verified"]
    assert saved["draft_settings"] is not None
    args = (*args[:4], saved["next_binding"], args[-1])
    read = call(tmp_path, args, "preflight", request=request,
        expected_artifact_sha256=saved["artifact_receipt"]["artifact_sha256"],
        consumption_cutoff_utc="2000-01-04T12:01:30Z")
    assert read["status"] == "READY_OFFLINE_SETTINGS_PREFLIGHT", read
    assert_unarmed(read)
    assert_unarmed(saved)
    assert saved["platform_profile"] == PROFILE
    if mode == "advanced":
        assert read["confirmation_state"] == "REQUIRED"
        assert read["confirmation_granted"] is False
        assert saved["preview"]["requested_events"]["limits"]["stop_loss_ratio"] == .05


@pytest.mark.parametrize("change", ["policy", "budget", "threshold", "profile", "account", "revision", "config", "fee", "quantity", "pending", "real", "reserve", "options", "option_inventory"])
def test_missing_or_conflicting_contracts_never_write_or_return_a_draft(tmp_path, change):
    args = seed(tmp_path)
    runtime, snapshot, p, c, b, proof = args
    if change == "policy": runtime.runtime_settings.trusted_runtime_risk_policy = None
    elif change == "budget": p["risk_inputs"]["account_policy"] = None
    elif change == "threshold": del c["policy"]["thresholds_by_level"]["balanced"]["breaker_daily_loss"]
    elif change == "profile": runtime.runtime_settings.strategy_profile = "soxl_soxx_trend_income"
    elif change == "account": runtime.runtime_settings.account_ids = ("OTHER",)
    elif change == "revision": runtime.runtime_settings.runtime_target = replace(runtime.runtime_settings.runtime_target,
        strategy_release=replace(runtime.runtime_settings.runtime_target.strategy_release, strategy_revision="f" * 40))
    elif change == "config": runtime.merged_runtime_config["execution_cash_reserve_ratio"] = .2
    elif change == "fee": p["execution_rules"].pop("fees")
    elif change == "quantity": p["execution_rules"]["quantity"]["step_by_symbol"]["TQQQ"] = None
    elif change == "pending": p["cash_evidence"]["pending_state"] = "UNKNOWN"
    elif change == "real": p["account_contract"]["synthetic"] = False
    elif change == "reserve": runtime.runtime_settings.reserved_cash_ratio = .2
    elif change == "options": runtime.merged_runtime_config["option_overlay_enabled"] = True
    elif change == "option_inventory": snapshot.metadata["option_positions"] = ({"synthetic": True},)
    before = {str(f): f.read_bytes() for f in tmp_path.rglob("*.json")}
    out = call(tmp_path, args, event_id="rejected")
    assert out["gaps"], out
    assert out["new_risk_prohibited"] is True
    assert out["draft_settings"] is None
    assert before == {str(f): f.read_bytes() for f in tmp_path.rglob("*.json")}
    assert_unarmed(out)


def test_missing_policy_does_not_turn_verified_closed_state_into_permission(tmp_path):
    args = seed(tmp_path)
    args[0].runtime_settings.trusted_runtime_risk_policy = None
    out = call(tmp_path, args, "read")
    assert out["new_risk_prohibited"] is True
    assert out["draft_settings"] is None
    assert "TQQQ_EVENT_POLICY_MISSING" in out["gaps"]


@pytest.mark.parametrize("rebind_facts", [False, True])
def test_settled_cash_cannot_be_invented_behind_an_unchanged_cash_balance_projection(tmp_path, rebind_facts):
    args = fixture()
    args[1].metadata["cash_balances"][0]["SettledCash"] = "1"
    # The original selected balance stays unchanged; the separate native
    # attachment now exposes the change before the cash-evidence comparison.
    facts = build_ibkr_account_facts(args[1])
    assert facts["cash"] == args[2]["facts"]["cash"]
    assert facts != args[2]["facts"]
    if rebind_facts:
        args[2]["facts"] = facts
    out = call(tmp_path, args, "initialize", initialization=args[-1])
    assert out["gaps"] == ["TQQQ_NATIVE_CASH_CONFLICT" if rebind_facts else "TQQQ_NATIVE_ACCOUNT_FACTS_MISMATCH"]
    assert not out["readback_verified"]
    assert list(tmp_path.rglob("*.json")) == []


@pytest.mark.parametrize("change", ["held_symbol", "scenario_scope"])
def test_native_holdings_and_funding_model_cannot_silently_use_another_scope(tmp_path, change):
    args = fixture()
    runtime, snapshot, p, c, b, proof = args
    if change == "held_symbol":
        snapshot = replace(snapshot, positions=(Position(symbol="OTHER", quantity=1., market_value=1., currency="USD"),))
        p["facts"] = build_ibkr_account_facts(snapshot)
        args = (runtime, snapshot, p, c, b, proof)
        expected = "TQQQ_HELD_SECURITY_OUTSIDE_SCENARIO_SCOPE"
    else:
        risk = p["risk_inputs"]
        descriptor = replace(risk["scenario_set"].descriptor, scope="OTHER")
        risk["scenario_set"] = replace(risk["scenario_set"], descriptor=descriptor)
        risk["account_policy"] = replace(risk["account_policy"], descriptor=descriptor)
        risk["presets"] = {level: replace(policy, descriptor=descriptor) for level, policy in risk["presets"].items()}
        release = replace(runtime.runtime_settings.runtime_target.strategy_release,
            risk_policy_sha256=digest(dict(event_policy=c["policy"], funding_inputs_sha256=_hash(risk))))
        runtime.runtime_settings.runtime_target = replace(runtime.runtime_settings.runtime_target, strategy_release=release)
        expected = "TQQQ_SCENARIO_ACCOUNT_SCOPE_MISMATCH"
    out = call(tmp_path, args, "initialize", initialization=proof)
    assert out["gaps"] == [expected]
    assert list(tmp_path.rglob("*.json")) == []


def test_empty_store_never_assumes_closed_or_initializes_itself(tmp_path, packet):
    out = call(tmp_path, packet, event_id="event-1")
    assert out["breaker_state"] == "UNKNOWN"
    assert out["new_risk_prohibited"] is True
    assert out["draft_settings"] is None
    assert list(tmp_path.rglob("*.json")) == []


@pytest.mark.parametrize("field,value", [("candidate_id", "other-artificial-portfolio"), ("config_sha256", "f" * 64)])
def test_wrong_journal_identity_cannot_poison_initialization_or_be_read(tmp_path, field, value):
    args = fixture()
    args[4]["identity"][field] = value
    args[5]["identity"] = deepcopy(args[4]["identity"])
    for action in ("initialize", "read"):
        out = call(tmp_path, args, action, initialization=args[-1])
        assert out["gaps"] == ["TQQQ_JOURNAL_POLICY_OR_PORTFOLIO_MISMATCH"]
        assert not out["readback_verified"]
    assert list(tmp_path.rglob("*.json")) == []


def test_open_survives_restart_replay_and_qualified_reduction_only(tmp_path):
    args = seed(tmp_path, fixture(opened=True))
    out = call(tmp_path, args, event_id="reduce-1")
    assert out["status"] == "SETTINGS_DRAFT_PERSISTED", out
    assert out["breaker_state"] == "OPEN"
    assert out["new_risk_prohibited"] is True
    assert all(v == 0 for v in out["draft_settings"]["security_target_weights"].values())
    replay = call(tmp_path, args, event_id="reduce-1")
    assert replay["readback_verified"] is True
    assert replay["draft_settings"] is None
    args = (*args[:4], out["next_binding"], args[-1])
    read = call(tmp_path, args, "read")
    assert read["breaker_state"] == "OPEN"
    assert_unarmed(read)


def test_replay_read_and_preflight_do_not_generate_candidates_or_write(tmp_path, monkeypatch):
    args = seed(tmp_path)
    saved = call(tmp_path, args, event_id="allocation-1")
    assert saved["artifact_readback_verified"]
    def forbidden(*args, **kwargs):
        raise AssertionError("replay/reader must not regenerate or write")
    monkeypatch.setattr("us_equity_strategies.research.auto_portfolio_preview.generate_joint_candidate", forbidden)
    monkeypatch.setattr(PortfolioEventJournal, "append", forbidden)
    replay = call(tmp_path, args, event_id="allocation-1")
    assert replay["readback_verified"] and replay["draft_settings"] is None
    args = (*args[:4], saved["next_binding"], args[-1])
    out = call(tmp_path, args, "read")
    assert out["readback_verified"] and out["new_risk_prohibited"]
    out = call(tmp_path, args, "preflight", expected_artifact_sha256=saved["artifact_receipt"]["artifact_sha256"],
        consumption_cutoff_utc="2000-01-04T12:01:30Z")
    assert out["status"] == "READY_OFFLINE_SETTINGS_PREFLIGHT", out


def test_committed_event_with_lost_ack_is_read_back_without_append_retry(tmp_path, monkeypatch):
    args = seed(tmp_path)
    original = PortfolioEventJournal.append
    attempts = []
    def lost_ack(self, record):
        attempts.append(record["record_sha256"])
        original(self, record)
        raise OSError("artificial acknowledgement lost")
    monkeypatch.setattr(PortfolioEventJournal, "append", lost_ack)
    out = call(tmp_path, args, event_id="allocation-1")
    assert out["state_persisted"] and out["readback_verified"], out
    assert out["artifact_readback_verified"]
    assert len(attempts) == 1


def test_old_draft_rejected_after_new_open_and_policy_remains_frozen(tmp_path):
    args = seed(tmp_path)
    saved = call(tmp_path, args, event_id="allocation-1")
    p, c, b = move_observation(args[2], args[3], saved["next_binding"])
    snapshot = replace(args[1], as_of=datetime(2000, 1, 4, 12, 0, 30, tzinfo=timezone.utc))
    p["facts"] = build_ibkr_account_facts(snapshot)
    c["observations"]["account_snapshot_sha256"] = digest(p["facts"])
    c["observations"]["flow_adjusted_nav_index"] = 80.
    next_args = (args[0], snapshot, p, c, b, args[-1])
    opened = call(tmp_path, next_args, event_id="trip")
    assert opened["breaker_state"] == "OPEN", opened
    assert opened["state_persisted"] is True
    assert opened["draft_settings"] is None
    old = (*args[:4], opened["next_binding"], args[-1])
    read = call(tmp_path, old, "preflight", expected_artifact_sha256=saved["artifact_receipt"]["artifact_sha256"],
        consumption_cutoff_utc="2000-01-04T12:01:30Z")
    assert read["draft_settings"] is None
    assert read["new_risk_prohibited"] is True


def test_independent_processes_read_same_open_checkpoint(tmp_path):
    args = seed(tmp_path, fixture(opened=True))
    saved = call(tmp_path, args, event_id="reduce-1")
    checkpoint = tmp_path / "external-checkpoint.json"
    checkpoint.write_text(json.dumps(saved["next_binding"]))
    code = """
import json, sys
from pathlib import Path
from test_tqqq_risk_wiring import fixture, call
root=Path(sys.argv[1]); args=fixture(opened=True)
args=(*args[:4],json.loads((root/'external-checkpoint.json').read_text()),args[-1])
out=call(root,args,'read')
assert out['readback_verified'] and out['breaker_state']=='OPEN',out
assert out['new_risk_prohibited'] and not out['breaker_reset']
print('OPEN retained; no configuration or order authority')
"""
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(Path(__file__).parent),
        str(Path(os.environ["IBKR_OFFLINE_UES_SOURCE"]) / "tests" / "research"), str(Path.cwd())]))
    for _ in range(2):
        completed = subprocess.run([sys.executable, "-c", code, str(tmp_path)], env=env,
            capture_output=True, text=True, timeout=30)
        assert completed.returncode == 0, completed.stderr
        assert "OPEN retained" in completed.stdout


def reserve_fixture(ratio, floor, reserved):
    args = fixture()
    runtime, _, p, _, _, _ = args
    runtime.runtime_settings.reserved_cash_ratio = ratio
    runtime.runtime_settings.reserved_cash_floor_usd = floor
    runtime = replace(runtime, cash_reserve_ratio=ratio, cash_reserve_floor_usd=floor)
    args = (runtime, *args[1:])
    p['account_contract']['reserved_cash'] = str(reserved)
    p['cash_evidence']['reservations'] = [dict(id='explicit-reserve', kind='RESERVE', amount=str(reserved)),
        dict(id='separate-obligation', kind='OTHER', amount='3')]
    p['account_contract']['other_cash_obligations'] = '3'
    return args


@pytest.mark.parametrize(('ratio', 'floor', 'reserved'), [(.1, 5., 10.), (.05, 10., 10.), (0., 10., 10.)])
def test_fixed_and_ratio_reserve_use_max_once_then_separate_obligations(tmp_path, ratio, floor, reserved):
    args = seed(tmp_path, reserve_fixture(ratio, floor, reserved))
    saved = call(tmp_path, args, event_id='reserve-funded')
    assert saved['status'] == 'SETTINGS_DRAFT_PERSISTED', saved
    quantity = saved['preview']['allocation_preview']['requested_preview']['quantity_preview']
    for scenario in quantity['scenarios']:
        assert scenario['spendable_cash'] == 99 - reserved - 3
        assert scenario['sale_proceeds_used_for_funding'] is False
    assert_unarmed(saved)


@pytest.mark.parametrize(('ratio', 'floor', 'reserved'), [(.1, 5., 9.), (.05, 10., 9.)])
def test_underfunded_effective_reserve_rejects_before_store_write(tmp_path, ratio, floor, reserved):
    args = reserve_fixture(ratio, floor, reserved)
    out = call(tmp_path, args, 'initialize', initialization=args[-1])
    assert out['gaps'] == ['TQQQ_RESERVE_NOT_FUNDED'], out
    assert not list(tmp_path.rglob('*.json'))
    assert_unarmed(out)


@pytest.mark.parametrize('floor', [None, True, -1., float('nan'), float('inf'), 10.])
def test_missing_invalid_or_runtime_mismatched_floor_never_writes(tmp_path, floor):
    args = fixture()
    args[0].runtime_settings.reserved_cash_floor_usd = floor
    out = call(tmp_path, args, 'initialize', initialization=args[-1])
    assert out['gaps']
    assert not list(tmp_path.rglob('*.json'))
    assert_unarmed(out)


def cash_only_fixture():
    from application.tqqq_offline_risk import build_tqqq_offline_account_facts
    args = fixture()
    runtime, snapshot, p, c, b, proof = args
    snapshot = replace(snapshot, positions=())
    snapshot.metadata['cash_balances'][0].update(CashBalance='100', SettledCash='100')
    for row in p['cash_evidence']['account_values']:
        row['value'] = '100'
    p['account_contract']['settled_cash'] = '100'
    evidence = {key: deepcopy(p['cash_evidence'][key]) for key in (
        'synthetic', 'account_id', 'scope_ref', 'currency', 'source', 'source_version',
        'observed_at', 'available_at', 'max_age_seconds', 'valid_from', 'valid_until')}
    evidence.update(positions_complete=True, asset_scope='STOCKS_AND_CASH_ONLY',
        account_snapshot_sha256=digest(build_ibkr_account_facts(snapshot)))
    p['facts'] = build_tqqq_offline_account_facts(snapshot, account_contract=p['account_contract'],
        inventory_evidence=evidence)
    c['observations'].update(account_snapshot_sha256=digest(p['facts']), sleeves=[])
    return (runtime, snapshot, p, c, b, proof), evidence


@pytest.mark.parametrize('level', ['conservative', 'balanced', 'growth'])
def test_explicit_artificial_empty_inventory_can_preview_and_read_back(tmp_path, level):
    args, evidence = cash_only_fixture()
    assert 'broker_reported_positions' not in build_ibkr_account_facts(args[1])
    assert args[2]['facts']['broker_reported_positions'] == []
    initialized = call(tmp_path, args, 'initialize', initialization=args[-1], inventory_evidence=evidence)
    assert initialized['readback_verified'], initialized
    args = (*args[:4], initialized['next_binding'], args[-1])
    request = request_fixture(); request['level'] = level
    saved = call(tmp_path, args, request=request, event_id='cash-allocation', inventory_evidence=evidence)
    assert saved['status'] == 'SETTINGS_DRAFT_PERSISTED', saved
    args = (*args[:4], saved['next_binding'], args[-1])
    read = call(tmp_path, args, 'preflight', request=request, inventory_evidence=evidence,
        expected_artifact_sha256=saved['artifact_receipt']['artifact_sha256'],
        consumption_cutoff_utc='2000-01-04T12:01:30Z')
    assert read['status'] == 'READY_OFFLINE_SETTINGS_PREFLIGHT', read
    assert_unarmed(read)


@pytest.mark.parametrize('change', ['missing', 'incomplete', 'scope', 'snapshot', 'stale', 'real', 'options', 'nonempty', 'unknown'])
def test_empty_native_projection_alone_or_invalid_attestation_never_means_flat(tmp_path, change):
    args, evidence = cash_only_fixture()
    if change == 'missing': evidence = None
    elif change == 'incomplete': evidence['positions_complete'] = False
    elif change == 'scope': evidence['account_id'] = 'OTHER'
    elif change == 'snapshot': evidence['account_snapshot_sha256'] = 'f' * 64
    elif change == 'stale': evidence['observed_at'] = '1999-01-01T00:00:00Z'
    elif change == 'real': evidence['synthetic'] = False
    elif change == 'options': args[1].metadata['option_positions'] = ({'unknown': True},)
    elif change == 'nonempty': args = (args[0], replace(args[1], positions=fixture()[1].positions), *args[2:])
    elif change == 'unknown': args = (args[0], replace(args[1], positions=None), *args[2:])
    out = call(tmp_path, args, 'initialize', initialization=args[-1], inventory_evidence=evidence)
    assert out['gaps']
    assert out['draft_settings'] is None
    assert not list(tmp_path.rglob('*.json'))
    assert_unarmed(out)



def test_expired_empty_inventory_evidence_cannot_refresh_a_saved_draft(tmp_path):
    args, evidence = cash_only_fixture()
    evidence['max_age_seconds'] = 60
    initialized = call(tmp_path, args, 'initialize', initialization=args[-1], inventory_evidence=evidence)
    assert initialized['readback_verified'], initialized
    args = (*args[:4], initialized['next_binding'], args[-1])
    saved = call(tmp_path, args, event_id='short-inventory', inventory_evidence=evidence)
    assert saved['status'] == 'SETTINGS_DRAFT_PERSISTED', saved
    args = (*args[:4], saved['next_binding'], args[-1])
    before = {str(f): f.read_bytes() for f in tmp_path.rglob('*.json')}
    read = call(tmp_path, args, 'preflight', inventory_evidence=evidence,
        expected_artifact_sha256=saved['artifact_receipt']['artifact_sha256'],
        consumption_cutoff_utc='2000-01-04T12:01:30Z')
    assert read['gaps'] and read['draft_settings'] is None
    assert before == {str(f): f.read_bytes() for f in tmp_path.rglob('*.json')}
    assert_unarmed(read)



def test_execution_equity_cannot_use_a_different_reserve_denominator(tmp_path):
    args = reserve_fixture(.1, 0., 10.)
    args = (args[0], replace(args[1], total_equity=200.), *args[2:])
    # Broker NLV, cash facts and their capital adapter still say 100.
    out = call(tmp_path, args, 'initialize', initialization=args[-1])
    assert out['gaps'] == ['TQQQ_EXECUTION_EQUITY_DENOMINATOR_MISMATCH'], out
    assert not list(tmp_path.rglob('*.json'))
    assert_unarmed(out)


@pytest.mark.parametrize('field', ['minimum_fee_applies', 'minimum_fee_amount', 'per_share_fee', 'per_order_fee'])
def test_account_fee_features_are_not_approximated_by_fixed_bps(tmp_path, field):
    args = fixture()
    args[2]['execution_rules']['fees'][field] = True if field == 'minimum_fee_applies' else '1'
    out = call(tmp_path, args, 'initialize', initialization=args[-1])
    assert 'UNSUPPORTED_FEE_RULE' in out['gaps'], out
    assert not list(tmp_path.rglob('*.json'))
    assert_unarmed(out)


def cash_policy_fixture(ratio=.1, floor=5., strategy_ratio=0.):
    args = fixture()
    runtime, _, p, _, _, _ = args
    runtime.runtime_settings.reserved_cash_ratio = ratio
    runtime.runtime_settings.reserved_cash_floor_usd = floor
    runtime.merged_runtime_config['execution_cash_reserve_ratio'] = strategy_ratio
    runtime.runtime_settings.runtime_target = replace(runtime.runtime_settings.runtime_target,
        strategy_release=replace(runtime.runtime_settings.runtime_target.strategy_release,
            config_sha256=digest(runtime.merged_runtime_config)))
    runtime = replace(runtime, cash_reserve_ratio=max(ratio, strategy_ratio), cash_reserve_floor_usd=floor)
    p['account_contract'].pop('reserved_cash')  # Derived by the existing cash bridge.
    p['account_contract']['other_cash_obligations'] = '3'
    p['cash_evidence']['reservations'] = [dict(id='separate-obligation', kind='OTHER', amount='3')]
    evidence = {key: deepcopy(p['cash_evidence'][key]) for key in (
        'synthetic', 'account_id', 'scope_ref', 'currency', 'source', 'source_version',
        'observed_at', 'available_at', 'max_age_seconds', 'valid_from', 'valid_until')}
    evidence.update(read_status='RESOLVED', config_sha256=digest(runtime.merged_runtime_config),
        resolved_cash_policy=dict(cash_only=True, reserved_cash_ratio=ratio, reserved_cash_floor_usd=floor),
        reservation_id='account-cash-policy')
    return (runtime, *args[1:]), evidence


def seed_cash_policy(root, args, evidence):
    initialized = call(root, args, 'initialize', initialization=args[-1], cash_policy_evidence=evidence)
    assert initialized['readback_verified'], initialized
    return (*args[:4], initialized['next_binding'], args[-1])


@pytest.mark.parametrize('mode', ['simple', 'advanced'])
@pytest.mark.parametrize('level', ['conservative', 'balanced', 'growth'])
@pytest.mark.parametrize(('ratio', 'floor', 'strategy_ratio', 'expected'), [
    (.1, 0., 0., 10.), (0., 10., 0., 10.), (.1, 5., 0., 10.),
    (.05, 10., 0., 10.), (.1, 10., 0., 10.), (0., 0., 0., 0.), (.02, 0., .1, 10.),
])
def test_cash_policy_generates_one_reserve_for_all_levels_modes_and_preflight(
        tmp_path, mode, level, ratio, floor, strategy_ratio, expected):
    args, evidence = cash_policy_fixture(ratio, floor, strategy_ratio)
    original = deepcopy(args[2])
    args = seed_cash_policy(tmp_path, args, evidence)
    request = request_fixture(mode); request['level'] = level
    saved = call(tmp_path, args, request=request, event_id='cash-policy', cash_policy_evidence=evidence)
    assert saved['status'] == 'SETTINGS_DRAFT_PERSISTED', saved
    preview = saved['cash_policy_preview']
    assert float(preview['required_reserve_usd']) == expected
    assert preview['spendable_cash'] == 99 - expected - 3
    assert preview['source_attestation_only'] is True
    quantity = saved['preview']['allocation_preview']['requested_preview']['quantity_preview']
    for scenario in quantity['scenarios']:
        assert scenario['spendable_cash'] == 99 - expected - 3
        assert scenario['gross_buy_plus_fees'] <= scenario['spendable_cash']
        assert scenario['sale_proceeds_used_for_funding'] is False
    args = (*args[:4], saved['next_binding'], args[-1])
    read = call(tmp_path, args, 'preflight', request=request, cash_policy_evidence=evidence,
        expected_artifact_sha256=saved['artifact_receipt']['artifact_sha256'],
        consumption_cutoff_utc='2000-01-04T12:01:30Z')
    assert read['status'] == 'READY_OFFLINE_SETTINGS_PREFLIGHT', read
    assert read['cash_policy_preview'] == preview
    assert args[2] == original
    assert_unarmed(saved); assert_unarmed(read)


def test_prepared_packet_and_explicit_same_reserve_are_idempotent_not_added(tmp_path):
    from application.tqqq_offline_risk import _cash_policy_packet
    args, evidence = cash_policy_fixture()
    p = args[2]
    p['account_contract']['reserved_cash'] = '10'
    p['cash_evidence']['reservations'].append(dict(id=evidence['reservation_id'], kind='RESERVE', amount='10'))
    prepared, first = _cash_policy_packet(args[0], args[1], p, evidence)
    repeated, second = _cash_policy_packet(args[0], args[1], prepared, evidence)
    assert first == second and prepared == repeated
    assert len(repeated['cash_evidence']['reservations']) == 2
    assert repeated['account_contract']['reserved_cash'] == '10'
    assert repeated['account_contract']['settled_cash'] == '99'
    assert first['spendable_cash'] == 86.
    args = seed_cash_policy(tmp_path, (args[0], args[1], prepared, *args[3:]), evidence)
    saved = call(tmp_path, args, event_id='once', cash_policy_evidence=evidence)
    assert saved['status'] == 'SETTINGS_DRAFT_PERSISTED', saved


def test_advanced_cash_ratio_constraint_does_not_deduct_the_reserve_again(tmp_path):
    args, evidence = cash_policy_fixture()
    args = seed_cash_policy(tmp_path, args, evidence)
    request = request_fixture('advanced', cash_floor_ratio=.9)
    saved = call(tmp_path, args, request=request, event_id='advanced-cash', cash_policy_evidence=evidence)
    assert saved['status'] == 'SETTINGS_DRAFT_PERSISTED', saved
    assert saved['cash_policy_preview']['spendable_cash'] == 86.
    rows = saved['preview']['allocation_preview']['requested_preview']['quantity_preview']['scenarios']
    for row in rows:
        assert row['spendable_cash'] == 86.
        assert sum(row['post_fee_security_weights'].values()) <= .1
    assert_unarmed(saved)


@pytest.mark.parametrize('change', ['read_failed', 'unknown', 'missing_floor', 'policy', 'config',
    'account', 'currency', 'stale', 'real', 'boolean', 'reservation_id', 'same_amount_other_id',
    'conflicting_amount', 'contract_zero', 'duplicate', 'other_collision', 'not_disjoint', 'pending'])
def test_cash_policy_source_or_overlap_gaps_never_write_or_override(tmp_path, change):
    args, evidence = cash_policy_fixture()
    p = args[2]
    if change in ('read_failed', 'unknown'): evidence['read_status'] = change.upper()
    elif change == 'missing_floor': evidence['resolved_cash_policy'].pop('reserved_cash_floor_usd')
    elif change == 'policy': evidence['resolved_cash_policy']['reserved_cash_ratio'] = .2
    elif change == 'config': evidence['config_sha256'] = 'f' * 64
    elif change == 'account': evidence['account_id'] = 'OTHER'
    elif change == 'currency': evidence['currency'] = 'HKD'
    elif change == 'stale': evidence['observed_at'] = '1999-01-01T00:00:00Z'
    elif change == 'real': evidence['synthetic'] = False
    elif change == 'boolean': evidence['resolved_cash_policy']['reserved_cash_floor_usd'] = True
    elif change == 'reservation_id': evidence['reservation_id'] = ''
    elif change in ('same_amount_other_id', 'conflicting_amount', 'duplicate', 'other_collision'):
        p['cash_evidence']['reservations'].append(dict(
            id='unmapped-reserve' if change == 'same_amount_other_id' else evidence['reservation_id'],
            kind='OTHER' if change == 'other_collision' else 'RESERVE',
            amount='9' if change == 'conflicting_amount' else '10'))
        if change == 'duplicate': p['cash_evidence']['reservations'].append(deepcopy(p['cash_evidence']['reservations'][-1]))
    elif change == 'contract_zero': p['account_contract']['reserved_cash'] = '0'
    elif change == 'not_disjoint': p['cash_evidence']['reservations_disjoint'] = False
    elif change == 'pending': p['cash_evidence']['pending_state'] = 'UNKNOWN'
    before = deepcopy(p)
    out = call(tmp_path, args, 'initialize', initialization=args[-1], cash_policy_evidence=evidence)
    assert out['gaps'], out
    assert out['draft_settings'] is None and not out['readback_verified']
    assert not list(tmp_path.rglob('*.json'))
    assert p == before
    assert_unarmed(out)


def test_cash_policy_insufficient_cash_is_a_gap_not_a_forced_sale(tmp_path):
    args, evidence = cash_policy_fixture(0., 100.)
    out = call(tmp_path, args, 'initialize', initialization=args[-1], cash_policy_evidence=evidence)
    assert 'CASH_CONTRACT_INCONSISTENT' in out['gaps'], out
    assert out['cash_policy_preview']['spendable_cash'] is None
    assert not list(tmp_path.rglob('*.json'))
    assert_unarmed(out)


def test_cash_preview_remains_diagnostic_when_risk_policy_is_missing(tmp_path):
    args, evidence = cash_policy_fixture()
    args[0].runtime_settings.trusted_runtime_risk_policy = None
    out = call(tmp_path, args, 'initialize', initialization=args[-1], cash_policy_evidence=evidence)
    assert out['gaps'] == ['TQQQ_EVENT_POLICY_MISSING'], out
    assert out['cash_policy_preview']['status'] == 'READY_SYNTHETIC_CASH_POLICY_PREVIEW'
    assert out['cash_policy_preview']['spendable_cash'] == 86.
    assert not list(tmp_path.rglob('*.json'))
    assert_unarmed(out)


def test_expired_cash_policy_evidence_cannot_refresh_a_saved_preview(tmp_path):
    args, evidence = cash_policy_fixture()
    evidence['max_age_seconds'] = 60
    args = seed_cash_policy(tmp_path, args, evidence)
    saved = call(tmp_path, args, event_id='cash-policy', cash_policy_evidence=evidence)
    assert saved['status'] == 'SETTINGS_DRAFT_PERSISTED', saved
    args = (*args[:4], saved['next_binding'], args[-1])
    before = {str(f): f.read_bytes() for f in tmp_path.rglob('*.json')}
    read = call(tmp_path, args, 'preflight', cash_policy_evidence=evidence,
        expected_artifact_sha256=saved['artifact_receipt']['artifact_sha256'],
        consumption_cutoff_utc='2000-01-04T12:01:30Z')
    assert read['gaps'] and read['draft_settings'] is None
    assert before == {str(f): f.read_bytes() for f in tmp_path.rglob('*.json')}
    assert_unarmed(read)


def test_cash_policy_fingerprint_binds_floor_even_when_ratio_dominates(tmp_path):
    args, evidence = cash_policy_fixture(.1, 5.)
    args = seed_cash_policy(tmp_path, args, evidence)
    saved = call(tmp_path, args, event_id='cash-policy', cash_policy_evidence=evidence)
    assert saved['status'] == 'SETTINGS_DRAFT_PERSISTED', saved
    args[0].runtime_settings.reserved_cash_floor_usd = 6.
    args = (replace(args[0], cash_reserve_floor_usd=6.), *args[1:4], saved['next_binding'], args[-1])
    evidence['resolved_cash_policy']['reserved_cash_floor_usd'] = 6.
    read = call(tmp_path, args, 'preflight', cash_policy_evidence=evidence,
        expected_artifact_sha256=saved['artifact_receipt']['artifact_sha256'],
        consumption_cutoff_utc='2000-01-04T12:01:30Z')
    assert read['gaps'] and read['draft_settings'] is None
    assert_unarmed(read)


def test_embedded_cash_policy_is_revalidated_when_optional_argument_is_omitted(tmp_path):
    from application.tqqq_offline_risk import _cash_policy_packet
    args, evidence = cash_policy_fixture(.1, 5.)
    prepared, _ = _cash_policy_packet(args[0], args[1], args[2], evidence)
    args = (args[0], args[1], prepared, *args[3:])
    args = seed_cash_policy(tmp_path, args, evidence)
    saved = call(tmp_path, args, event_id='cash-policy', cash_policy_evidence=evidence)
    assert saved['status'] == 'SETTINGS_DRAFT_PERSISTED', saved
    args = (*args[:4], saved['next_binding'], args[-1])
    unchanged = call(tmp_path, args, 'preflight',
        expected_artifact_sha256=saved['artifact_receipt']['artifact_sha256'],
        consumption_cutoff_utc='2000-01-04T12:01:30Z')
    assert unchanged['status'] == 'READY_OFFLINE_SETTINGS_PREFLIGHT', unchanged
    args[0].runtime_settings.reserved_cash_floor_usd = 6.
    args = (replace(args[0], cash_reserve_floor_usd=6.), *args[1:4], saved['next_binding'], args[-1])
    before = {str(f): f.read_bytes() for f in tmp_path.rglob('*.json')}
    read = call(tmp_path, args, 'preflight',
        expected_artifact_sha256=saved['artifact_receipt']['artifact_sha256'],
        consumption_cutoff_utc='2000-01-04T12:01:30Z')
    assert read['gaps'] and read['draft_settings'] is None
    assert before == {str(f): f.read_bytes() for f in tmp_path.rglob('*.json')}
    assert_unarmed(read)


def test_cash_policy_helper_number_semantics_match_reserve_admission(tmp_path):
    # 100 * .07 is 7.000000000000001 in the deployed float helper. The
    # projection and admission must not use subtly different reserve formulae.
    args, evidence = cash_policy_fixture(.07, 0.)
    args = seed_cash_policy(tmp_path, args, evidence)
    saved = call(tmp_path, args, event_id='fractional-reserve', cash_policy_evidence=evidence)
    assert saved['status'] == 'SETTINGS_DRAFT_PERSISTED', saved
    assert float(saved['cash_policy_preview']['required_reserve_usd']) == 100 * .07


def test_future_cash_metadata_does_not_increase_current_spendable_cash(tmp_path):
    args, evidence = cash_policy_fixture()
    args[1].metadata.update(planned_deposit='1000', outside_bank_cash='1000')
    args[2]['cash_evidence']['projected_sale_proceeds'] = '1000'
    args = seed_cash_policy(tmp_path, args, evidence)
    saved = call(tmp_path, args, event_id='not-arrived', cash_policy_evidence=evidence)
    assert saved['status'] == 'SETTINGS_DRAFT_PERSISTED', saved
    assert saved['cash_policy_preview']['settled_cash'] == '99'
    assert saved['cash_policy_preview']['spendable_cash'] == 86.


def test_confirmed_deposit_updates_cash_once_without_pnl_or_peak_reset(tmp_path):
    from quant_platform_kit.strategy_lifecycle.live_equity import (
        cash_flow_adjusted_return, extract_external_cash_flow,
    )
    args, evidence = cash_policy_fixture()
    # Explicit artificial capital reference, frozen BEFORE initialization;
    # neither an emergency-cash deduction nor an automatic real risk policy.
    args[2]['account_contract']['protected_capital_basis'] = '200'
    risk = args[2]['risk_inputs']
    risk['curve'] = replace(risk['curve'], protected_capital=200.)
    args[0].runtime_settings.runtime_target = replace(args[0].runtime_settings.runtime_target,
        strategy_release=replace(args[0].runtime_settings.runtime_target.strategy_release,
            risk_policy_sha256=digest(dict(event_policy=args[3]['policy'], funding_inputs_sha256=_hash(risk)))))
    args = seed_cash_policy(tmp_path, args, evidence)
    first = call(tmp_path, args, event_id='before-deposit', cash_policy_evidence=evidence)
    assert first['status'] == 'SETTINGS_DRAFT_PERSISTED', first
    p, c, b = move_observation(args[2], args[3], first['next_binding'])
    metadata = deepcopy(args[1].metadata)
    metadata['broker_net_liquidation'] = 120.
    metadata['cash_balances'][0].update(CashBalance='119', SettledCash='119', NetLiquidation='120')
    snapshot = replace(args[1], total_equity=120., metadata=metadata,
        as_of=datetime(2000, 1, 4, 12, 0, 30, tzinfo=timezone.utc))
    p['facts'] = build_ibkr_account_facts(snapshot)
    p['account_contract']['settled_cash'] = '119'
    for row in p['cash_evidence']['account_values']: row['value'] = '119'
    c['observations'].update(account_snapshot_sha256=digest(p['facts']), session_net_external_flow=20.,
        flow_adjusted_nav_index=100., retained_nav_index_peak=100.)
    evidence = dict(evidence, observed_at='2000-01-04T12:00:30Z', available_at='2000-01-04T12:00:30Z')
    arrived = (args[0], snapshot, p, c, b, args[-1])
    saved = call(tmp_path, arrived, event_id='deposit-observed', cash_policy_evidence=evidence)
    assert saved['status'] == 'SETTINGS_DRAFT_PERSISTED', saved
    preview = saved['cash_policy_preview']
    assert float(preview['required_reserve_usd']) == 12.
    assert preview['spendable_cash'] == 119 - 12 - 3
    assert saved['preview']['requested_events']['metrics']['daily_loss_ratio'] == 0.
    journal = PortfolioEventJournal(tmp_path / 'journal', offline_root=tmp_path)
    chain = journal.load_chain(b['identity'])
    assert chain[-1]['state']['session_start_nav'] == 100.
    assert chain[-1]['state']['session_net_external_flow'] == 20.
    assert chain[-1]['state']['risk_state']['nav_peak'] == 100.
    replay = call(tmp_path, arrived, event_id='deposit-observed', cash_policy_evidence=evidence)
    assert replay['readback_verified'] and replay['draft_settings'] is None
    assert journal.load_chain(b['identity']) == chain
    flow = extract_external_cash_flow({'net_external_cash_flow': 20.})
    # This existing helper has an END-OF-PERIOD convention only.
    assert cash_flow_adjusted_return(100., 120., net_external_cash_flow=flow) == 0.
    assert extract_external_cash_flow({}) is None
    assert cash_flow_adjusted_return(100., 120., net_external_cash_flow=None) is None
    assert_unarmed(saved); assert_unarmed(replay)


def test_generated_cash_reserve_preserves_open_state_and_reduction_path(tmp_path):
    args, evidence = cash_policy_fixture()
    opened = fixture(opened=True)
    args = (*args[:3], *opened[3:])
    args = seed_cash_policy(tmp_path, args, evidence)
    saved = call(tmp_path, args, event_id='open-cash-policy', cash_policy_evidence=evidence)
    assert saved['status'] == 'SETTINGS_DRAFT_PERSISTED', saved
    assert saved['breaker_state'] == 'OPEN' and saved['new_risk_prohibited']
    assert all(value == 0. for value in saved['draft_settings']['security_target_weights'].values())
    args = (*args[:4], saved['next_binding'], args[-1])
    read = call(tmp_path, args, 'read', cash_policy_evidence=evidence)
    assert read['readback_verified'] and read['breaker_state'] == 'OPEN'
    assert read['draft_settings'] is None
    assert_unarmed(saved); assert_unarmed(read)
