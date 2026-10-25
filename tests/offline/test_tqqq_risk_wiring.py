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
        reserved_cash_ratio=0., dry_run_only=True)
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


def test_settled_cash_cannot_be_invented_behind_an_unchanged_cash_balance_projection(tmp_path):
    args = fixture()
    args[1].metadata["cash_balances"][0]["SettledCash"] = "1"
    # Native facts intentionally select CashBalance; it still matches exactly.
    assert build_ibkr_account_facts(args[1]) == args[2]["facts"]
    out = call(tmp_path, args, "initialize", initialization=args[-1])
    assert out["gaps"] == ["TQQQ_NATIVE_CASH_CONFLICT"]
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
