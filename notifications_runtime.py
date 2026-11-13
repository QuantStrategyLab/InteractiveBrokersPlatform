"""Telegram and unified notification helpers for runtime routes."""


# ruff: noqa: F403, F405
from __future__ import annotations


import runtime_wiring as rw
from runtime_wiring import *  # noqa: F403, F401

def _split_env_list(value: str | None) -> tuple[str, ...]:
    return tuple(
        item.strip()
        for item in str(value or "").replace(";", ",").split(",")
        if item.strip()
    )
def send_tg_message(message):
    from quant_platform_kit.notifications.cycle_channel import build_cycle_sender
    sender = build_cycle_sender(
        channel=_NOTIFICATION_CHANNEL,
        telegram_token=TG_TOKEN,
        telegram_chat_id=TG_CHAT_ID,
        webhook_url=_NOTIFICATION_WEBHOOK_URL,
    )
    return sender(_with_platform_notification_prefix(message))
def _platform_notification_prefix() -> str:
    runtime_target = getattr(RUNTIME_SETTINGS, "runtime_target", None)
    account_selector = getattr(runtime_target, "account_selector", None) or ()
    for selector in account_selector:
        label = _normalize_account_prefix_label(selector)
        if label:
            return f"[{label}]"
    raw_scope = (
        getattr(runtime_target, "account_scope", None)
        or ACCOUNT_GROUP
    )
    label = _normalize_account_prefix_label(raw_scope)
    if not label:
        return ""
    return f"[{label}]"
def _normalize_account_prefix_label(value: object) -> str:
    label = str(value or "").strip().upper()
    if not label or label in {"DEFAULT", "LIVE", "US"}:
        return ""
    if label.startswith("LIVE-U") and len(label) > len("LIVE-U"):
        return label.removeprefix("LIVE-")
    return label
def _with_platform_notification_prefix(message: str) -> str:
    prefix = _platform_notification_prefix()
    text = str(message or "")
    if not prefix or text.lstrip().startswith(prefix):
        return text
    return f"{prefix} {text}"
def publish_notification(*, detailed_text, compact_text):
    return build_composer().build_notification_adapters().publish_cycle_notification(
        detailed_text=detailed_text,
        compact_text=compact_text,
    )
def _runtime_error_notification_targets() -> tuple[tuple[str, str], ...]:
    targets: list[tuple[str, str]] = []
    if TG_TOKEN and TG_CHAT_ID:
        targets.append((TG_TOKEN, TG_CHAT_ID))

    seen: set[tuple[str, str]] = set()
    unique_targets: list[tuple[str, str]] = []
    for target in targets:
        if target in seen:
            continue
        seen.add(target)
        unique_targets.append(target)
    return tuple(unique_targets)
def _runtime_error_notification_message(exc: Exception, *, route_label: str | None = None) -> str:
    title = "runtime_failure_title"
    return "\n".join((
        t(title),
        t("strategy_label", name=strategy_display_name or STRATEGY_PROFILE),
        t("runtime_failure_context", context=ACCOUNT_GROUP),
        t("runtime_failure_result"),
        t("runtime_failure_action"),
    ))
def _notify_runtime_error(exc: Exception, *, route_label: str | None = None) -> bool:
    from notifications.unified_adapter import publish_runtime_failure
    return publish_runtime_failure(exc, lambda: _notify_runtime_error_legacy(exc, route_label=route_label))
def _notify_runtime_error_legacy(exc: Exception, *, route_label: str | None = None) -> bool:
    targets = _runtime_error_notification_targets()
    if not targets:
        print(t("runtime_notification_missing_target"), flush=True)
        return False
    message = _runtime_error_notification_message(exc, route_label=route_label)
    outcomes = []
    for token, chat_id in targets:
        outcomes.append(send_telegram_message(
            _with_platform_notification_prefix(message),
            token=token,
            chat_id=chat_id,
            requests_module=requests,
        ))
    return bool(outcomes) and all(outcomes)
def _publish_runtime_failure_notification(*, detailed_text: str, compact_text: str, exc: Exception) -> bool:
    from notifications.unified_adapter import enabled as unified_enabled, publish_runtime_failure
    if unified_enabled():
        return publish_runtime_failure(exc, lambda: publish_notification(detailed_text=detailed_text, compact_text=compact_text))
    try:
        if publish_notification(detailed_text=detailed_text, compact_text=compact_text):
            return True
        return _notify_runtime_error(exc)
    except Exception:
        print(t("runtime_notification_delivery_failed"), flush=True)
        return _notify_runtime_error(exc)
def _handle_route_runtime_error(exc: Exception, *, route_label: str | None = None):
    print(t("runtime_failure_log"), flush=True)
    _notify_runtime_error(exc, route_label=route_label)
    return "Error", 500
def _route_with_runtime_error_fallback(handler, *args, route_label: str | None = None, **kwargs):
    try:
        return handler(*args, **kwargs)
    except Exception as exc:
        return _handle_route_runtime_error(exc, route_label=route_label)
