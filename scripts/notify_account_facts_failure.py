"""Send a redacted Telegram alert when scheduled IBKR account-facts sync fails."""
from __future__ import annotations

import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_REDACT_PATTERNS = (
    (re.compile(r"gs://[^\s'\"<>]+"), "[gcs-redacted]"),
    (re.compile(r"bot[0-9]{6,}:[A-Za-z0-9_-]+"), "bot[redacted]"),
    (re.compile(r"Bearer\s+\S+", re.IGNORECASE), "Bearer [redacted]"),
    (re.compile(r"\bU\d{5,}\b"), "[account-redacted]"),
)


def redact_text(value: str) -> str:
    text = str(value or "").strip()
    for pattern, replacement in _REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def build_failure_message(target: str, run_url: str, *, reason: str = "") -> str:
    safe_target = redact_text(target) or "unknown-target"
    safe_url = redact_text(run_url) or "unknown-run"
    lines = [
        "⚠️ IBKR account-facts 定时同步失败",
        f"Target: {safe_target}",
        f"Run: {safe_url}",
    ]
    if reason:
        lines.append(f"Detail: {redact_text(reason)[:240]}")
    return "\n".join(lines)


def send_telegram_alert(token: str, chat_id: str, text: str, *, timeout: float = 15.0) -> bool:
    if not token or not chat_id:
        print("telegram_alert_skipped: missing credentials", flush=True)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True}).encode("utf-8")
    req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except (HTTPError, URLError, OSError, TimeoutError) as exc:
        print(f"telegram_alert_failed: {type(exc).__name__}", flush=True)
        return False


def main() -> int:
    env = os.environ
    text = build_failure_message(
        env.get("TARGET_NAME", ""),
        env.get("RUN_URL", ""),
        reason=env.get("FAILURE_REASON", ""),
    )
    send_telegram_alert(
        env.get("TELEGRAM_TOKEN", "").strip(),
        env.get("GLOBAL_TELEGRAM_CHAT_ID", "").strip(),
        text,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
