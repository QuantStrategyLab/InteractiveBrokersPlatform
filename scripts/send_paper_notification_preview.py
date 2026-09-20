#!/usr/bin/env python3
"""Send a bounded InteractiveBrokersPlatform PAPER Telegram notification preview pack.

Renders synthetic compact messages via existing notification renderers,
translator, and Telegram sender. Does not trade, connect IB Gateway/TWS,
read accounts/positions/quotes, import or call broker/order/Cloud Run
production interfaces, or change production configuration.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from notifications.renderers import render_heartbeat_notification, render_trade_notification
from notifications.telegram import (
    build_strategy_display_name,
    build_translator,
    send_telegram_message,
)

_MAX_PREVIEW_MESSAGES = 6
_PREVIEW_STRATEGY_PROFILE = "us_equity_combo"
_PREVIEW_EXTRA_LINES = (
    "🧪 【PREVIEW】PAPER notification preview",
    "synthetic / 合成样例 · 不会下单 · No order will be placed",
)
_SYNTHETIC_SEPARATOR = "━━━━━━━━━━━━━━━━━━"
_SYNTHETIC_SYMBOL = "PREVIEW"
_SYNTHETIC_DASHBOARD = "📌 PAPER PREVIEW\n  - synthetic positions only"


def _resolve_locale(raw: str | None = None) -> str:
    value = str(raw or os.environ.get("NOTIFY_LANG") or "zh").strip().lower()
    return "en" if value.startswith("en") else "zh"


def _split_chat_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [
        part.strip()
        for part in str(raw).replace(";", ",").replace("\n", ",").split(",")
        if part.strip()
    ]


def _telegram_secret_project() -> str | None:
    return (
        os.environ.get("GCP_PROJECT_ID")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or None
    )


def resolve_telegram_token() -> str:
    direct_token = (os.environ.get("TELEGRAM_TOKEN") or os.environ.get("TG_TOKEN") or "").strip()
    if direct_token:
        return direct_token
    secret_name = (os.environ.get("TELEGRAM_TOKEN_SECRET_NAME") or "").strip()
    if not secret_name:
        return ""
    command = [
        "gcloud",
        "secrets",
        "versions",
        "access",
        "latest",
        "--secret",
        secret_name,
    ]
    project = _telegram_secret_project()
    if project:
        command.extend(["--project", project])
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def resolve_telegram_chat_id() -> str:
    chats = _split_chat_ids(os.environ.get("GLOBAL_TELEGRAM_CHAT_ID"))
    return chats[0] if chats else ""


def _with_preview_markers(body: str) -> str:
    return "\n".join(("[PAPER]", body, *_PREVIEW_EXTRA_LINES))


def _synthetic_order(*, side: str = "buy", quantity: float = 1.0, status: str = "", reason: str = "") -> dict:
    order = {
        "symbol": _SYNTHETIC_SYMBOL,
        "side": side,
        "quantity": quantity,
    }
    if status:
        order["status"] = status
    if reason:
        order["reason"] = reason
    return order


def build_preview_messages(*, locale: str | None = None) -> list[str]:
    """Build at most six synthetic compact PAPER preview messages."""

    resolved_locale = _resolve_locale(locale)
    translator = build_translator(resolved_locale)
    strategy_name = build_strategy_display_name(translator, resolved_locale)(
        _PREVIEW_STRATEGY_PROFILE,
        fallback_name="US Equity Combo",
    )
    common = dict(
        dashboard="",
        strategy_dashboard=_SYNTHETIC_DASHBOARD,
        signal_desc="hold",
        status_desc="hold",
        status_icon="🐤",
        translator=translator,
        separator=_SYNTHETIC_SEPARATOR,
        strategy_display_name=strategy_name,
        extra_notification_lines=(),
    )

    heartbeat = render_heartbeat_notification(
        no_op_text=translator("no_trades"),
        **common,
    ).compact_text

    dry_run = render_trade_notification(
        trade_logs=(),
        execution_summary={
            "mode": "dry_run",
            "orders_submitted": [_synthetic_order()],
            "target_vs_current": [{"symbol": _SYNTHETIC_SYMBOL, "delta_weight": 0.10}],
        },
        **common,
    ).compact_text

    pending = render_trade_notification(
        trade_logs=(),
        execution_summary={
            "mode": "paper",
            "orders_pending": [_synthetic_order()],
        },
        **common,
    ).compact_text

    filled = render_trade_notification(
        trade_logs=(),
        execution_summary={
            "mode": "paper",
            "orders_filled": [_synthetic_order()],
        },
        **common,
    ).compact_text

    rejected = render_trade_notification(
        trade_logs=(),
        execution_summary={
            "mode": "paper",
            "orders_skipped": [
                _synthetic_order(status="Rejected", reason="rejected"),
            ],
        },
        **common,
    ).compact_text

    unknown_status = "\n".join(
        (
            translator("error_title"),
            strategy_name,
            f"status={translator('strategy_plugin_route_unknown_route')}",
            "synthetic PREVIEW unknown status / 未知状态",
        )
    )

    messages = [
        _with_preview_markers(heartbeat),
        _with_preview_markers(dry_run),
        _with_preview_markers(pending),
        _with_preview_markers(filled),
        _with_preview_markers(rejected),
        _with_preview_markers(unknown_status),
    ]
    if len(messages) > _MAX_PREVIEW_MESSAGES:
        raise RuntimeError(
            f"preview message count {len(messages)} exceeds cap {_MAX_PREVIEW_MESSAGES}"
        )
    return messages


def send_preview(*, locale: str | None = None, requests_module=None) -> bool:
    messages = build_preview_messages(locale=locale)
    token = resolve_telegram_token()
    chat_id = resolve_telegram_chat_id()
    if not token or not chat_id:
        print(
            "Notification preview not sent: Telegram target is not configured.",
            file=sys.stderr,
        )
        return False

    if requests_module is None:
        import requests as requests_module

    for message in messages:
        if not send_telegram_message(
            message,
            token=token,
            chat_id=chat_id,
            requests_module=requests_module,
        ):
            print("Notification preview delivery failed.", file=sys.stderr)
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Send a bounded InteractiveBrokersPlatform PAPER Telegram "
            "notification preview pack."
        )
    )
    parser.add_argument(
        "--locale",
        default=os.environ.get("NOTIFY_LANG"),
        help="Optional notification locale override (zh/en). Defaults to NOTIFY_LANG.",
    )
    args = parser.parse_args(argv)

    # Fail closed: this path never enables a production runtime target.
    if (os.environ.get("RUNTIME_TARGET_ENABLED") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }:
        print(
            "Notification preview refused: RUNTIME_TARGET_ENABLED must stay disabled.",
            file=sys.stderr,
        )
        return 1

    delivered = send_preview(locale=args.locale)
    if not delivered:
        return 1
    print(
        "Notification preview delivered bounded synthetic PAPER pack "
        f"(at most {_MAX_PREVIEW_MESSAGES} messages; no orders)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
