"""Carry existing safe evidence through the pinned shared attention renderer."""
from copy import deepcopy

import pytest

from notifications import unified_adapter as adapter


@pytest.mark.parametrize("field,code", [
    ("execution_blocked_reason", "empty_position_decision"),
    ("no_op_reason", "strategy_risk_rejected"),
    ("execution_blocked_reason", "account_brake_source_or_submit_unavailable"),
])
def test_blocked_attention_preserves_known_source_cause(monkeypatch, field, code):
    monkeypatch.setenv("NOTIFY_LANG", "en")
    execution = {"execution_status": "blocked", field: code}
    original = deepcopy(execution)
    body = adapter._cycle_attention_notification("blocked_run", execution, {}).detailed_text
    assert code in body
    assert execution == original


@pytest.mark.parametrize("value", ["private account detail", "private code with spaces", None, [], {}])
def test_unrecognized_source_reason_retains_fixed_fallback(monkeypatch, value):
    monkeypatch.setenv("NOTIFY_LANG", "en")
    body = adapter._cycle_attention_notification("blocked_run", {"no_op_reason": value}, {}).detailed_text
    assert "blocked_run" in body
    assert "private account detail" not in body and "synthetic_private_code" not in body


def test_pinned_compact_renderer_keeps_provided_run_url_once(monkeypatch):
    from quant_platform_kit.risk import attention
    monkeypatch.delattr(attention, "render_attention_notification", raising=False)
    monkeypatch.setenv("NOTIFY_LANG", "en")
    report = {"started_at": "2026-11-05T20:45:01Z",
              "console_run_url": "https://console.example.invalid/runs/synthetic?token=private"}
    original = deepcopy(report)
    body = adapter._cycle_attention_notification("blocked_run", {}, report).detailed_text
    assert "https://console.example.invalid/runs/synthetic" in body
    assert "private" not in body and "?token=" not in body
    assert body.count("Run time:") == 1
    assert report == original


@pytest.mark.parametrize("url", [None, "javascript:synthetic", "https://user:pass@console.example.invalid/run"])
def test_missing_or_unsafe_run_url_is_not_invented(monkeypatch, url):
    monkeypatch.setenv("NOTIFY_LANG", "en")
    body = adapter._cycle_attention_notification("blocked_run", {}, {"console_run_url": url}).detailed_text
    assert "https://" not in body and "javascript:" not in body and "user:pass" not in body
