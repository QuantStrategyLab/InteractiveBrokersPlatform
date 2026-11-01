#!/usr/bin/env python3
"""Render or send ONE InteractiveBrokersPlatform PAPER Telegram notification preview.

The preview renders a single consolidated synthetic message in the unified
daily-digest format (the QuantSentinel daily digest contract) by reusing the
pinned QRS digest renderer snapshot in ``tests/fixtures/qrs_notification_contract``.
With ``--send`` the workflow delivers that one message through the existing
Telegram sender; without ``--send`` it prints the rendered message to the job
log and sends nothing.

Does not trade, connect IB Gateway/TWS, read accounts/positions/quotes, import
or call broker/order/Cloud Run production interfaces, or change production
configuration.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from notifications.telegram import send_telegram_message  # noqa: E402

_QRS_SNAPSHOT = ROOT / "tests" / "fixtures" / "qrs_notification_contract"
_PREVIEW_PLATFORM_ID = "ibkr"
_PREVIEW_STRATEGY_PROFILE = "us_equity_combo"
_PREVIEW_EXTRA_LINES = (
    "🧪 【PREVIEW】PAPER notification preview",
    "synthetic / 合成样例 · 不会下单 · No order will be placed",
)
_SHANGHAI = ZoneInfo("Asia/Shanghai")


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


def _digest_renderer():
    """Load the pinned QRS daily-digest renderer snapshot (a pure formatter)."""
    snapshot = str(_QRS_SNAPSHOT)
    if snapshot not in sys.path:
        sys.path.insert(0, snapshot)
    import daily_digest_notify

    return daily_digest_notify


def _default_business_day() -> str:
    return datetime.now(_SHANGHAI).date().isoformat()


def build_preview_message(*, locale: str | None = None, business_day: str | None = None) -> str:
    """Render ONE synthetic message in the unified daily-digest format."""

    resolved_locale = _resolve_locale(locale)
    digest = _digest_renderer()
    payload = digest.DailyDigestInput(
        business_day=business_day or _default_business_day(),
        locale=resolved_locale,
        runs=(
            digest.DigestRunEntry(
                platform_id=_PREVIEW_PLATFORM_ID,
                strategy_profile=_PREVIEW_STRATEGY_PROFILE,
                status="ok",
                order_count=0,
                fill_count=0,
                cycle_count=1,
            ),
        ),
    )
    body = digest.render_daily_digest(payload)
    return "\n".join(("[PAPER]", body, *_PREVIEW_EXTRA_LINES))


def send_preview(*, locale: str | None = None, requests_module=None) -> bool:
    message = build_preview_message(locale=locale)
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
            "Render (or send with --send) one InteractiveBrokersPlatform PAPER "
            "notification preview in the unified daily-digest format."
        )
    )
    parser.add_argument(
        "--locale",
        default=os.environ.get("NOTIFY_LANG"),
        help="Optional notification locale override (zh/en). Defaults to NOTIFY_LANG.",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        default=(os.environ.get("PREVIEW_SEND") or "").strip().lower()
        in {"1", "true", "yes", "on"},
        help="Send the rendered preview message to Telegram. Default: print only.",
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

    message = build_preview_message(locale=args.locale)
    if not args.send:
        print("Rendered PAPER notification preview (send=false; nothing sent):")
        print(message)
        print(
            "Preview rendered only: one consolidated unified daily-digest "
            "message; no Telegram delivery."
        )
        return 0

    delivered = send_preview(locale=args.locale)
    if not delivered:
        return 1
    print(
        "Notification preview delivered: one consolidated synthetic unified "
        "daily-digest message (no orders)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
