import copy

import pytest

from notifications.renderers import render_trade_notification
from notifications.telegram import build_translator


def _render(summary, *, locale, trade_logs=()):
    return render_trade_notification(
        dashboard="detailed synthetic dashboard",
        strategy_dashboard="",
        trade_logs=trade_logs,
        execution_summary=summary,
        signal_desc="",
        status_desc="",
        status_icon="",
        translator=build_translator(locale),
        separator="---",
        strategy_display_name="Synthetic strategy",
    )


@pytest.mark.parametrize("locale", ["zh", "en"])
@pytest.mark.parametrize(
    "summary,trade_logs",
    [
        (
            {"execution_status": "no_op", "no_op_reason": "quantity_zero:TQQQ"},
            ["profile=tqqq_growth_income"],
        ),
        (
            {
                "execution_status": "blocked",
                "orders_skipped": [{"symbol": "TQQQ", "side": "buy", "reason": "quantity_zero"}],
            },
            [],
        ),
        (
            {
                "execution_status": "unknown",
                "no_op_reason": "outcome_unknown",
            },
            ["execution observation unavailable"],
        ),
        (
            {"orders_submitted": [{"symbol": "TQQQ", "side": "buy", "quantity": 1, "status": "dry_run"}]},
            [],
        ),
        (
            {"option_orders_submitted": [{"symbol": "TEST-OPTION", "action": "buy", "quantity": 1, "status": "dry_run"}]},
            [],
        ),
    ],
)
def test_unsubmitted_notification_uses_neutral_result_title(locale, summary, trade_logs):
    original = copy.deepcopy(summary)
    rendered = _render(summary, locale=locale, trade_logs=trade_logs)
    expected = "📋 【执行结果】" if locale == "zh" else "📋 【Execution Result】"
    assert rendered.compact_text.splitlines()[0] == expected
    assert rendered.detailed_text.splitlines()[0] == expected
    assert summary == original
    if summary.get("no_op_reason") == "quantity_zero:TQQQ":
        assert "TQQQ" in rendered.compact_text
        assert "整数股数量为0" in rendered.compact_text if locale == "zh" else "whole-share quantity is zero" in rendered.compact_text


@pytest.mark.parametrize("locale", ["zh", "en"])
@pytest.mark.parametrize(
    "field",
    [
        "orders_submitted", "orders_pending", "orders_filled", "orders_partially_filled",
        "option_orders_submitted", "option_orders_pending", "option_orders_filled",
        "option_orders_partially_filled",
    ],
)
def test_broker_order_evidence_with_skips_keeps_trade_title(locale, field):
    summary = {
        field: [{"symbol": "TQQQ", "side": "buy", "action": "buy", "quantity": 1}],
        "orders_skipped": [{"symbol": "QQQM", "side": "buy", "reason": "quantity_zero"}],
    }
    original = copy.deepcopy(summary)
    rendered = _render(summary, locale=locale)
    expected = build_translator(locale)("rebalance_title")
    assert rendered.compact_text.splitlines()[0] == expected
    assert rendered.detailed_text.splitlines()[0] == expected
    assert "QQQM" in rendered.compact_text
    assert summary == original


@pytest.mark.parametrize("locale,expected", [
    ("zh", "未下单：低于最小订单金额（TQQQ）"),
    ("en", "No order: below minimum order amount (TQQQ)"),
])
@pytest.mark.parametrize("flag", [None, "true"])
@pytest.mark.parametrize("logs", [(), ("reason=min_notional:TQQQ",)])
def test_a1_minimum_amount_reason_has_no_nested_label(monkeypatch, locale, expected, flag, logs):
    if flag is None:
        monkeypatch.delenv("IBKR_UNIFIED_NOTIFICATION_ENABLED", raising=False)
    else:
        monkeypatch.setenv("IBKR_UNIFIED_NOTIFICATION_ENABLED", flag)
    summary = {"execution_status": "no_op", "no_op_reason": "min_notional:TQQQ"}
    original = copy.deepcopy(summary)
    rendered = _render(summary, locale=locale, trade_logs=logs)
    for text in (rendered.compact_text, rendered.detailed_text):
        assert expected in text
        assert "min_notional" not in text
        assert "reason=" not in text
        assert "原因=" not in text
        assert not text.startswith(build_translator(locale)("rebalance_title"))
    assert summary == original
