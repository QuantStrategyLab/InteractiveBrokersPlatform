"""Strategy plugin signal loading, report attachment, and alert dispatch."""


# ruff: noqa: F403, F405
from __future__ import annotations


import runtime_wiring as rw
from runtime_wiring import *  # noqa: F403, F401

def load_strategy_plugin_signals():
    return build_strategy_adapters().load_strategy_plugin_signals(
        getattr(RUNTIME_SETTINGS, "strategy_plugin_mounts_json", None)
    )
def attach_strategy_plugin_report(report, *, signals, error: str | None = None):
    build_strategy_adapters().attach_strategy_plugin_report(report, signals=signals, error=error)
def build_strategy_plugin_notification_lines(signals) -> tuple[str, ...]:
    return build_strategy_adapters().build_strategy_plugin_notification_lines(signals)
def build_strategy_plugin_error_notification_lines(error) -> tuple[str, ...]:
    return build_strategy_adapters().build_strategy_plugin_error_notification_lines(error)
def build_strategy_plugin_alert_messages(signals):
    return build_strategy_adapters().build_strategy_plugin_alert_messages(signals)
def build_strategy_plugin_alert_state_settings():
    return StrategyPluginAlertStateSettings.from_env(
        project_id=PROJECT_ID,
    )
def build_strategy_plugin_alert_context_label() -> str:
    return build_alert_context_label(
        platform_id="ibkr",
        strategy_profile=STRATEGY_PROFILE,
        account_scope=ACCOUNT_GROUP,
        service_name=SERVICE_NAME,
        runtime_target=RUNTIME_SETTINGS.runtime_target,
    )
def publish_strategy_plugin_alerts(signals, *, report=None):
    from quant_platform_kit.notifications.digest_routing import immediate_plugin_signals
    result = dispatch_strategy_plugin_alerts(
        immediate_plugin_signals(signals, mode="daily_digest"),
        notification_settings=RUNTIME_SETTINGS,
        translator=t,
        strategy_label=STRATEGY_PROFILE,
        context_label=build_strategy_plugin_alert_context_label(),
        state_settings=build_strategy_plugin_alert_state_settings(),
        log_message=print,
    )
    if report is not None:
        result.attach_to_report(report)
    return result
