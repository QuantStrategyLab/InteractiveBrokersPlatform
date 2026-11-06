"""Source-only presentation regressions; no notification delivery or broker calls."""
from copy import deepcopy

import pytest

from notifications import unified_adapter as adapter


@pytest.fixture(autouse=True)
def isolated_identity(monkeypatch):
    monkeypatch.setenv("QSL_NOTIFY_LANG", "zh")
    for key in ("DIGEST_OPAQUE_ACCOUNT_UID", "IBKR_DIGEST_OPAQUE_ACCOUNT_UID", "DIGEST_TARGET_ID", "IBKR_DIGEST_TARGET_ID", "RUNTIME_TARGET_JSON", "DIGEST_TARGET_BINDINGS_JSON"):
        monkeypatch.delenv(key, raising=False)


def render(execution=None, report=None, diagnostics=""):
    return adapter._cycle_attention_notification("blocked_run", execution or {}, report or {}, diagnostics).detailed_text


@pytest.mark.parametrize("code,description", [
    ("blocked_run", "保护规则阻断"),
    ("empty_position_decision", "空持仓决策"),
    ("strategy_risk_rejected", "策略风控拒绝"),
    ("account_brake_source_or_submit_unavailable", "账户保护数据或提交服务不可用"),
    ("account_brake_submission_not_confirmed", "账户保护提交未确认"),
    ("rejected:too_many_positions", "策略风控拒绝"),
])
def test_known_block_explanation(code, description):
    body = render({"execution_blocked_reason": code})
    assert code in body and description in body
    assert "无法解释此原因码" not in body


def test_unknown_structured_code_remains_visible():
    body = render({"execution_blocked_reason": "fake_unknown_guard"})
    assert "fake_unknown_guard" in body and "查看风控记录" in body


def test_authoritative_bound_identity(monkeypatch):
    monkeypatch.setenv("DIGEST_OPAQUE_ACCOUNT_UID", "fake-account-42")
    monkeypatch.setenv("DIGEST_TARGET_ID", "fake-target")
    body = render(report={"strategy_profile": "global_etf_rotation"})
    assert "••42" in body and "账户未知" not in body
    assert "fake-account-42" not in body


def test_missing_identity_names_field():
    body = render()
    assert "账户未绑定" in body and "DIGEST_OPAQUE_ACCOUNT_UID" in body


def test_continue_snapshot_does_not_override_block():
    execution = {"execution_status": "blocked", "snapshot_guard_decision": "proceed", "execution_blocked_reason": "empty_position_decision", "positions": []}
    original = deepcopy(execution)
    body = render(execution, diagnostics="无需调仓 · 快照决策=继续")
    assert "operational_uncertain" not in body
    assert "快照决策不代表执行获准" in body
    assert adapter._kind(execution) == "blocked_run" and execution == original


@pytest.mark.parametrize("field", ["console_run_url", "actions_run_url", "cloud_run_url", "strategy_cycle_url"])
def test_explicit_source_run_link(field):
    body = render(report={field: "https://runs.example.invalid/fake-cycle?token=fake-secret#private"})
    assert "https://runs.example.invalid/fake-cycle" in body
    assert "fake-secret" not in body and "#private" not in body


@pytest.mark.parametrize("url", [None, "javascript:fake", "https://user:pass@runs.example.invalid/fake"])
def test_no_link_is_explicitly_unavailable(url):
    body = render(report={"console_run_url": url, "run_id": "fake-run"})
    assert "运行详情不可用" in body
    assert "https://" not in body


def test_unknown_code_and_diagnostics_redacted():
    body = render({"execution_blocked_reason": "rejected:U00000001"}, diagnostics="account_id=U00000001 token=fake-secret")
    assert "U00000001" not in body and "fake-secret" not in body
    assert "rejected:" in body


@pytest.mark.parametrize("invalid", ["unknown", "none", " "])
def test_placeholder_identity_stays_unbound(monkeypatch, invalid):
    monkeypatch.setenv("DIGEST_OPAQUE_ACCOUNT_UID", invalid)
    monkeypatch.setenv("DIGEST_TARGET_ID", "fake-target")
    assert "DIGEST_OPAQUE_ACCOUNT_UID" in render()


def test_other_targets_identity_is_never_displayed(monkeypatch):
    import json
    monkeypatch.setenv("DIGEST_OPAQUE_ACCOUNT_UID", "fake-other-99")
    monkeypatch.setenv("DIGEST_TARGET_ID", "fake-other-target")
    monkeypatch.setenv("RUNTIME_TARGET_JSON", json.dumps({"service_name": "fake-other", "strategy_profile": "global_etf_rotation", "account_scope": "fake-other"}))
    body = render(report={"runtime_target": {"service_name": "fake-current", "strategy_profile": "global_etf_rotation", "account_scope": "fake-current"}})
    assert "账户未绑定" in body and "••99" not in body


def test_invalid_first_link_uses_later_explicit_evidence():
    body = render(report={"console_run_url": "javascript:fake", "actions_run_url": "https://runs.example.invalid/fake"})
    assert "https://runs.example.invalid/fake" in body and "RUN_LINK_MISSING" not in body
