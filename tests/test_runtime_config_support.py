import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from runtime_config_support import (
    DEFAULT_RESERVED_CASH_FLOOR_USD,
    DEFAULT_SAFE_HAVEN_CASH_SUBSTITUTE_THRESHOLD_USD,
    EXECUTION_BACKEND_GATEWAY,
    EXECUTION_BACKEND_QUANTCONNECT,
    load_platform_runtime_settings,
    parse_account_group_configs,
    resolve_execution_backend,
    resolve_non_negative_float_env,
    resolve_optional_ratio_env,
)
from strategy_registry import (
    IBKR_PLATFORM,
    US_EQUITY_DOMAIN,
    get_eligible_profiles_for_platform,
    get_platform_profile_matrix,
    get_platform_profile_status_matrix,
    get_supported_profiles_for_platform,
)


MINIMAL_GROUP_JSON = (
    '{"groups":{"paper":{"ib_gateway_instance_name":"ib-gateway",'
    '"ib_gateway_mode":"paper","ib_client_id":1}}}'
)
SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "print_strategy_profile_status.py"
SWITCH_PLAN_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "print_strategy_switch_env_plan.py"
SYNC_PLAN_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "build_cloud_run_env_sync_plan.py"
SAMPLE_STRATEGY_PROFILE = "global_etf_rotation"


def runtime_target_json(
    strategy_profile: str,
    *,
    dry_run_only: bool = False,
    platform_id: str = "ibkr",
    deployment_selector: str = "default",
    account_selector: list[str] | tuple[str, ...] | None = None,
    account_scope: str = "default",
    service_name: str | None = None,
) -> str:
    payload: dict[str, object] = {
        "platform_id": platform_id,
        "strategy_profile": strategy_profile,
        "dry_run_only": dry_run_only,
        "deployment_selector": deployment_selector,
        "account_scope": account_scope,
    }
    if account_selector is not None:
        payload["account_selector"] = list(account_selector)
    if service_name is not None:
        payload["service_name"] = service_name
    payload["execution_mode"] = "paper" if dry_run_only else "live"
    return json.dumps(payload, separators=(",", ":"))


def test_load_platform_runtime_settings_requires_strategy_profile(monkeypatch):
    monkeypatch.setenv("ACCOUNT_GROUP", "paper")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)
    monkeypatch.delenv("RUNTIME_TARGET_JSON", raising=False)

    with pytest.raises(EnvironmentError, match="RUNTIME_TARGET_JSON is required"):
        load_platform_runtime_settings(project_id_resolver=lambda: "project-1")



def test_load_platform_runtime_settings_requires_account_group(monkeypatch):
    monkeypatch.setenv("RUNTIME_TARGET_JSON", runtime_target_json(SAMPLE_STRATEGY_PROFILE))
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)
    monkeypatch.delenv("ACCOUNT_GROUP", raising=False)

    with pytest.raises(EnvironmentError, match="ACCOUNT_GROUP is required"):
        load_platform_runtime_settings(project_id_resolver=lambda: "project-1")



def test_load_platform_runtime_settings_requires_account_group_config_source(monkeypatch):
    monkeypatch.setenv("RUNTIME_TARGET_JSON", runtime_target_json(SAMPLE_STRATEGY_PROFILE))
    monkeypatch.setenv("ACCOUNT_GROUP", "paper")
    monkeypatch.delenv("IB_ACCOUNT_GROUP_CONFIG_JSON", raising=False)
    monkeypatch.delenv("IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME", raising=False)

    with pytest.raises(
        EnvironmentError,
        match="IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME or IB_ACCOUNT_GROUP_CONFIG_JSON is required",
    ):
        load_platform_runtime_settings(project_id_resolver=lambda: "project-1")



def test_load_platform_runtime_settings_uses_minimal_group_config(monkeypatch):
    monkeypatch.setenv("RUNTIME_TARGET_JSON", runtime_target_json(SAMPLE_STRATEGY_PROFILE))
    monkeypatch.setenv("ACCOUNT_GROUP", "paper")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)
    monkeypatch.delenv("IB_GATEWAY_ZONE", raising=False)
    monkeypatch.delenv("IB_GATEWAY_IP_MODE", raising=False)
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("GLOBAL_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("NOTIFY_LANG", raising=False)

    settings = load_platform_runtime_settings(project_id_resolver=lambda: "project-1")

    assert settings.project_id == "project-1"
    assert settings.execution_backend == EXECUTION_BACKEND_GATEWAY
    assert settings.ib_gateway_instance_name == "ib-gateway"
    assert settings.ib_gateway_zone == ""
    assert settings.ib_gateway_mode == "paper"
    assert settings.ib_gateway_ip_mode == "internal"
    assert settings.ib_client_id == 1
    assert settings.strategy_profile == SAMPLE_STRATEGY_PROFILE
    assert settings.strategy_display_name == "Global ETF Rotation"
    assert settings.strategy_domain == US_EQUITY_DOMAIN
    assert settings.runtime_target.platform_id == "ibkr"
    assert settings.runtime_target.execution_mode == "live"
    assert settings.strategy_target_mode == "weight"
    assert settings.strategy_artifact_root is None
    assert settings.strategy_artifact_dir is None
    assert settings.feature_snapshot_path is None
    assert settings.feature_snapshot_manifest_path is None
    assert settings.strategy_config_path is None
    assert settings.strategy_config_source is None
    assert settings.reconciliation_output_path is None
    assert settings.dry_run_only is False
    assert settings.quantity_step == 1.0
    assert settings.min_order_notional == 50.0
    assert settings.reserved_cash_floor_usd == DEFAULT_RESERVED_CASH_FLOOR_USD
    assert settings.reserved_cash_ratio is None
    assert settings.safe_haven_cash_substitute_threshold_usd == DEFAULT_SAFE_HAVEN_CASH_SUBSTITUTE_THRESHOLD_USD
    assert settings.account_group == "paper"
    assert settings.service_name is None
    assert settings.account_ids == ()
    assert settings.notify_lang == "en"
    assert settings.tg_token is None
    assert settings.tg_chat_id is None
    assert settings.strategy_plugin_mounts_json is None
    assert settings.quantconnect_project_id is None
    assert settings.quantconnect_node_id is None
    assert settings.quantconnect_compile_id is None
    assert settings.quantconnect_version_id is None
    assert settings.quantconnect_credentials_secret_name is None
    assert settings.quantconnect_brokerage_secret_name is None
    assert settings.crisis_alert_channels == ()
    assert settings.crisis_alert_email_recipients == ()
    assert settings.crisis_alert_email_sender_email is None
    assert settings.crisis_alert_email_sender_password is None
    assert settings.crisis_alert_email_smtp_host is None
    assert settings.crisis_alert_email_smtp_port is None
    assert settings.crisis_alert_email_smtp_security is None
    assert settings.crisis_alert_sms_recipients == ()
    assert settings.crisis_alert_sms_provider is None
    assert settings.crisis_alert_sms_account_id is None
    assert settings.crisis_alert_sms_auth_token is None
    assert settings.crisis_alert_sms_sender is None
    assert settings.crisis_alert_sms_messaging_service_id is None
    assert settings.crisis_alert_sms_api_base_url is None
    assert settings.crisis_alert_sms_body_max_chars is None
    assert settings.crisis_alert_push_recipients == ()
    assert settings.crisis_alert_push_provider is None
    assert settings.crisis_alert_push_app_token is None
    assert settings.crisis_alert_push_access_token is None
    assert settings.crisis_alert_push_api_base_url is None
    assert settings.crisis_alert_push_device is None
    assert settings.crisis_alert_push_priority is None
    assert settings.crisis_alert_push_tags is None
    assert settings.crisis_alert_push_body_max_chars is None
    assert settings.crisis_alert_telegram_chat_ids == ()
    assert settings.crisis_alert_telegram_bot_token is None
    assert settings.crisis_alert_telegram_api_base_url is None
    assert settings.crisis_alert_telegram_parse_mode is None
    assert settings.crisis_alert_telegram_disable_web_page_preview is None
    assert settings.crisis_alert_telegram_body_max_chars is None


def test_load_platform_runtime_settings_prefers_runtime_target_json(monkeypatch):
    monkeypatch.setenv("RUNTIME_TARGET_JSON", runtime_target_json(SAMPLE_STRATEGY_PROFILE))
    monkeypatch.setenv("ACCOUNT_GROUP", "paper")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)
    monkeypatch.setenv(
        "RUNTIME_TARGET_JSON",
        runtime_target_json(
            "tech_communication_pullback_enhancement",
            dry_run_only=True,
            account_selector=["U999"],
            service_name="interactive-brokers-paper-service",
        ),
    )

    settings = load_platform_runtime_settings(project_id_resolver=lambda: "project-1")

    assert settings.strategy_profile == "tech_communication_pullback_enhancement"
    assert settings.runtime_target.strategy_profile == "tech_communication_pullback_enhancement"
    assert settings.runtime_target.platform_id == "ibkr"
    assert settings.runtime_target.dry_run_only is True
    assert settings.runtime_target.execution_mode == "paper"
    assert settings.runtime_target.deployment_selector == "default"
    assert settings.runtime_target.account_selector == ("U999",)
    assert settings.runtime_target.account_scope == "default"



def test_load_platform_runtime_settings_supports_explicit_group_config_values(monkeypatch):
    monkeypatch.setenv("RUNTIME_TARGET_JSON", runtime_target_json(SAMPLE_STRATEGY_PROFILE))
    monkeypatch.setenv("ACCOUNT_GROUP", "taxable_main")
    monkeypatch.setenv(
        "IB_ACCOUNT_GROUP_CONFIG_JSON",
        '{"groups":{"taxable_main":{"ib_gateway_instance_name":"ib-gateway-main",'
        '"ib_gateway_zone":"us-central1-a","ib_gateway_mode":"live",'
        '"ib_gateway_port":4011,"ib_gateway_ip_mode":"external","ib_client_id":7,'
        '"service_name":"interactive-brokers-quant-taxable-main-service",'
        '"account_ids":["U1234567"]}}}',
    )
    monkeypatch.setenv("TELEGRAM_TOKEN", "token-1")
    monkeypatch.setenv("GLOBAL_TELEGRAM_CHAT_ID", "chat-1")
    monkeypatch.setenv("NOTIFY_LANG", "zh")

    settings = load_platform_runtime_settings(project_id_resolver=lambda: None)

    assert settings.ib_gateway_instance_name == "ib-gateway-main"
    assert settings.ib_gateway_zone == "us-central1-a"
    assert settings.ib_gateway_mode == "live"
    assert settings.ib_gateway_port == 4011
    assert settings.ib_gateway_ip_mode == "external"
    assert settings.ib_client_id == 7
    assert settings.strategy_profile == SAMPLE_STRATEGY_PROFILE
    assert settings.strategy_display_name == "Global ETF Rotation"
    assert settings.strategy_domain == US_EQUITY_DOMAIN
    assert settings.strategy_target_mode == "weight"
    assert settings.feature_snapshot_path is None
    assert settings.feature_snapshot_manifest_path is None
    assert settings.account_group == "taxable_main"
    assert settings.service_name == "interactive-brokers-quant-taxable-main-service"
    assert settings.account_ids == ("U1234567",)
    assert settings.tg_token == "token-1"
    assert settings.tg_chat_id == "chat-1"
    assert settings.notify_lang == "zh"


def test_load_platform_runtime_settings_supports_quantconnect_backend_without_gateway(monkeypatch):
    monkeypatch.setenv("RUNTIME_TARGET_JSON", runtime_target_json(SAMPLE_STRATEGY_PROFILE))
    monkeypatch.setenv("ACCOUNT_GROUP", "qc_slot")
    monkeypatch.setenv(
        "IB_ACCOUNT_GROUP_CONFIG_JSON",
        json.dumps(
            {
                "groups": {
                    "qc_slot": {
                        "execution_backend": "quantconnect",
                        "service_name": "interactive-brokers-qc-slot-service",
                        "account_ids": ["U00000000"],
                        "quantconnect_project_id": 12345678,
                        "quantconnect_node_id": "LN-placeholder",
                        "quantconnect_compile_id": "compile-placeholder",
                        "quantconnect_version_id": "-1",
                        "quantconnect_credentials_secret_name": "qc-api-credentials",
                        "quantconnect_brokerage_secret_name": "qc-ibkr-slot",
                    }
                }
            }
        ),
    )

    settings = load_platform_runtime_settings(project_id_resolver=lambda: "project-1")

    assert settings.execution_backend == EXECUTION_BACKEND_QUANTCONNECT
    assert settings.ib_gateway_instance_name == ""
    assert settings.ib_gateway_mode == "live"
    assert settings.ib_gateway_port == 0
    assert settings.ib_client_id == 0
    assert settings.service_name == "interactive-brokers-qc-slot-service"
    assert settings.account_ids == ("U00000000",)
    assert settings.quantconnect_project_id == 12345678
    assert settings.quantconnect_node_id == "LN-placeholder"
    assert settings.quantconnect_compile_id == "compile-placeholder"
    assert settings.quantconnect_version_id == "-1"
    assert settings.quantconnect_credentials_secret_name == "qc-api-credentials"
    assert settings.quantconnect_brokerage_secret_name == "qc-ibkr-slot"


def test_resolve_execution_backend_rejects_unknown_backend():
    with pytest.raises(EnvironmentError, match="IBKR_EXECUTION_BACKEND"):
        resolve_execution_backend("unsupported")


def test_load_platform_runtime_settings_reads_crisis_alert_email_config(monkeypatch):
    monkeypatch.setenv("RUNTIME_TARGET_JSON", runtime_target_json(SAMPLE_STRATEGY_PROFILE))
    monkeypatch.setenv("ACCOUNT_GROUP", "paper")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)
    monkeypatch.setenv("CRISIS_ALERT_EMAIL_RECIPIENTS", "alerts@example.com; voice@example.com")
    monkeypatch.setenv("CRISIS_ALERT_EMAIL_SENDER_EMAIL", "sender@example.com")
    monkeypatch.setenv("CRISIS_ALERT_EMAIL_SENDER_PASSWORD", "secret")
    monkeypatch.setenv("CRISIS_ALERT_EMAIL_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("CRISIS_ALERT_EMAIL_SMTP_PORT", "587")
    monkeypatch.setenv("CRISIS_ALERT_EMAIL_SMTP_SECURITY", "starttls")

    settings = load_platform_runtime_settings(project_id_resolver=lambda: "project-1")

    assert settings.crisis_alert_email_recipients == ("alerts@example.com", "voice@example.com")
    assert settings.crisis_alert_email_sender_email == "sender@example.com"
    assert settings.crisis_alert_email_sender_password == "secret"
    assert settings.crisis_alert_email_smtp_host == "smtp.example.com"
    assert settings.crisis_alert_email_smtp_port == "587"
    assert settings.crisis_alert_email_smtp_security == "starttls"


def test_load_platform_runtime_settings_reads_crisis_alert_sms_config(monkeypatch):
    monkeypatch.setenv("RUNTIME_TARGET_JSON", runtime_target_json(SAMPLE_STRATEGY_PROFILE))
    monkeypatch.setenv("ACCOUNT_GROUP", "paper")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)
    monkeypatch.setenv("CRISIS_ALERT_SMS_RECIPIENTS", "+15165480265;(516) 548-0265")
    monkeypatch.setenv("CRISIS_ALERT_SMS_PROVIDER", "twilio")
    monkeypatch.setenv("CRISIS_ALERT_SMS_ACCOUNT_ID", "AC123")
    monkeypatch.setenv("CRISIS_ALERT_SMS_AUTH_TOKEN", "secret")
    monkeypatch.setenv("CRISIS_ALERT_SMS_SENDER", "+15551234567")
    monkeypatch.setenv("CRISIS_ALERT_SMS_MESSAGING_SERVICE_ID", "MG123")
    monkeypatch.setenv("CRISIS_ALERT_SMS_API_BASE_URL", "https://twilio.example.test")
    monkeypatch.setenv("CRISIS_ALERT_SMS_BODY_MAX_CHARS", "160")

    settings = load_platform_runtime_settings(project_id_resolver=lambda: "project-1")

    assert settings.crisis_alert_sms_recipients == ("+15165480265", "(516) 548-0265")
    assert settings.crisis_alert_sms_provider == "twilio"
    assert settings.crisis_alert_sms_account_id == "AC123"
    assert settings.crisis_alert_sms_auth_token == "secret"
    assert settings.crisis_alert_sms_sender == "+15551234567"
    assert settings.crisis_alert_sms_messaging_service_id == "MG123"
    assert settings.crisis_alert_sms_api_base_url == "https://twilio.example.test"
    assert settings.crisis_alert_sms_body_max_chars == "160"


def test_load_platform_runtime_settings_reads_crisis_alert_channels_and_push_config(monkeypatch):
    monkeypatch.setenv("RUNTIME_TARGET_JSON", runtime_target_json(SAMPLE_STRATEGY_PROFILE))
    monkeypatch.setenv("ACCOUNT_GROUP", "paper")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)
    monkeypatch.setenv("CRISIS_ALERT_CHANNELS", "email;push;telegram")
    monkeypatch.setenv("CRISIS_ALERT_PUSH_RECIPIENTS", "risk-topic; backup-topic")
    monkeypatch.setenv("CRISIS_ALERT_PUSH_PROVIDER", "ntfy")
    monkeypatch.setenv("CRISIS_ALERT_PUSH_APP_TOKEN", "app-token")
    monkeypatch.setenv("CRISIS_ALERT_PUSH_ACCESS_TOKEN", "access-token")
    monkeypatch.setenv("CRISIS_ALERT_PUSH_API_BASE_URL", "https://ntfy.example.test")
    monkeypatch.setenv("CRISIS_ALERT_PUSH_DEVICE", "iphone")
    monkeypatch.setenv("CRISIS_ALERT_PUSH_PRIORITY", "5")
    monkeypatch.setenv("CRISIS_ALERT_PUSH_TAGS", "warning")
    monkeypatch.setenv("CRISIS_ALERT_PUSH_BODY_MAX_CHARS", "300")
    monkeypatch.setenv("CRISIS_ALERT_TELEGRAM_CHAT_IDS", "12345; @risk_channel")
    monkeypatch.setenv("CRISIS_ALERT_TELEGRAM_BOT_TOKEN", "telegram-token")
    monkeypatch.setenv("CRISIS_ALERT_TELEGRAM_API_BASE_URL", "https://telegram.example.test")
    monkeypatch.setenv("CRISIS_ALERT_TELEGRAM_PARSE_MODE", "HTML")
    monkeypatch.setenv("CRISIS_ALERT_TELEGRAM_DISABLE_WEB_PAGE_PREVIEW", "false")
    monkeypatch.setenv("CRISIS_ALERT_TELEGRAM_BODY_MAX_CHARS", "900")

    settings = load_platform_runtime_settings(project_id_resolver=lambda: "project-1")

    assert settings.crisis_alert_channels == ("email", "push", "telegram")
    assert settings.crisis_alert_push_recipients == ("risk-topic", "backup-topic")
    assert settings.crisis_alert_push_provider == "ntfy"
    assert settings.crisis_alert_push_app_token == "app-token"
    assert settings.crisis_alert_push_access_token == "access-token"
    assert settings.crisis_alert_push_api_base_url == "https://ntfy.example.test"
    assert settings.crisis_alert_push_device == "iphone"
    assert settings.crisis_alert_push_priority == "5"
    assert settings.crisis_alert_push_tags == "warning"
    assert settings.crisis_alert_push_body_max_chars == "300"
    assert settings.crisis_alert_telegram_chat_ids == ("12345", "@risk_channel")
    assert settings.crisis_alert_telegram_bot_token == "telegram-token"
    assert settings.crisis_alert_telegram_api_base_url == "https://telegram.example.test"
    assert settings.crisis_alert_telegram_parse_mode == "HTML"
    assert settings.crisis_alert_telegram_disable_web_page_preview == "false"
    assert settings.crisis_alert_telegram_body_max_chars == "900"


def test_load_platform_runtime_settings_uses_whole_share_quantity_step(monkeypatch):
    monkeypatch.setenv("RUNTIME_TARGET_JSON", runtime_target_json(SAMPLE_STRATEGY_PROFILE))
    monkeypatch.setenv("ACCOUNT_GROUP", "paper")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)
    monkeypatch.setenv("IBKR_MIN_ORDER_NOTIONAL_USD", "5")
    monkeypatch.setenv("IBKR_SAFE_HAVEN_CASH_SUBSTITUTE_THRESHOLD_USD", "750")

    settings = load_platform_runtime_settings(project_id_resolver=lambda: "project-1")

    assert settings.quantity_step == 1.0
    assert settings.min_order_notional == 5.0
    assert settings.safe_haven_cash_substitute_threshold_usd == 750.0


def test_load_platform_runtime_settings_reads_reserved_cash_policy(monkeypatch):
    monkeypatch.setenv("RUNTIME_TARGET_JSON", runtime_target_json(SAMPLE_STRATEGY_PROFILE))
    monkeypatch.setenv("ACCOUNT_GROUP", "paper")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)
    monkeypatch.setenv("IBKR_MIN_RESERVED_CASH_USD", "250")
    monkeypatch.setenv("IBKR_RESERVED_CASH_RATIO", "0.025")

    settings = load_platform_runtime_settings(project_id_resolver=lambda: "project-1")

    assert settings.reserved_cash_floor_usd == 250.0
    assert settings.reserved_cash_ratio == 0.025


def test_load_platform_runtime_settings_rejects_invalid_reserved_cash_ratio(monkeypatch):
    monkeypatch.setenv("IBKR_RESERVED_CASH_RATIO", "1.25")

    with pytest.raises(ValueError, match="IBKR_RESERVED_CASH_RATIO"):
        resolve_optional_ratio_env("IBKR_RESERVED_CASH_RATIO")


@pytest.mark.parametrize("raw_value", ["nan", "inf", "-inf"])
def test_load_platform_runtime_settings_rejects_non_finite_reserved_cash_floor(
    monkeypatch,
    raw_value,
):
    monkeypatch.setenv("IBKR_MIN_RESERVED_CASH_USD", raw_value)

    with pytest.raises(ValueError, match="IBKR_MIN_RESERVED_CASH_USD must be finite"):
        resolve_non_negative_float_env("IBKR_MIN_RESERVED_CASH_USD", default=0.0)


@pytest.mark.parametrize("raw_value", ["nan", "inf", "-inf"])
def test_load_platform_runtime_settings_rejects_non_finite_reserved_cash_ratio(
    monkeypatch,
    raw_value,
):
    monkeypatch.setenv("IBKR_RESERVED_CASH_RATIO", raw_value)

    with pytest.raises(ValueError, match="IBKR_RESERVED_CASH_RATIO must be finite"):
        resolve_optional_ratio_env("IBKR_RESERVED_CASH_RATIO")


def test_load_platform_runtime_settings_reads_ibkr_strategy_plugin_mounts(monkeypatch):
    mount_config = '{"strategy_plugins":[{"strategy":"soxl_soxx_trend_income","plugin":"crisis_response_shadow","signal_path":"gs://bucket/latest_signal.json"}]}'
    monkeypatch.setenv("RUNTIME_TARGET_JSON", runtime_target_json("soxl_soxx_trend_income"))
    monkeypatch.setenv("ACCOUNT_GROUP", "paper")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)
    monkeypatch.setenv("STRATEGY_PLUGIN_MOUNTS_JSON", '{"strategy_plugins":[{"plugin":"global"}]}')
    monkeypatch.setenv("IBKR_STRATEGY_PLUGIN_MOUNTS_JSON", mount_config)

    settings = load_platform_runtime_settings(project_id_resolver=lambda: "project-1")

    assert settings.strategy_plugin_mounts_json == mount_config



def test_load_platform_runtime_settings_rejects_unknown_strategy_profile(monkeypatch):
    monkeypatch.setenv("RUNTIME_TARGET_JSON", runtime_target_json("balanced_income"))
    monkeypatch.setenv("ACCOUNT_GROUP", "paper")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)

    with pytest.raises(ValueError, match="Unsupported STRATEGY_PROFILE"):
        load_platform_runtime_settings(project_id_resolver=lambda: None)


def test_platform_supported_profiles_are_filtered_by_registry():
    assert get_supported_profiles_for_platform(IBKR_PLATFORM) == frozenset(
        {
            "soxl_soxx_trend_income",
            "tqqq_growth_income",
            "tech_communication_pullback_enhancement",
            "global_etf_rotation",
            "mega_cap_leader_rotation_top50_balanced",
            "nasdaq_sp500_smart_dca",
            "russell_1000_multi_factor_defensive",
        }
    )


def test_platform_eligible_profiles_are_exposed_by_capability_matrix():
    assert get_eligible_profiles_for_platform(IBKR_PLATFORM) == frozenset(
        {
            "soxl_soxx_trend_income",
            "tqqq_growth_income",
            "tech_communication_pullback_enhancement",
            "global_etf_rotation",
            "mega_cap_leader_rotation_top50_balanced",
            "nasdaq_sp500_smart_dca",
            "russell_1000_multi_factor_defensive",
        }
    )


def test_load_platform_runtime_settings_accepts_tech_communication_pullback_enhancement(monkeypatch):
    monkeypatch.setenv(
        "RUNTIME_TARGET_JSON",
        runtime_target_json("tech_communication_pullback_enhancement"),
    )
    monkeypatch.setenv("ACCOUNT_GROUP", "paper")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)
    monkeypatch.setenv("IBKR_FEATURE_SNAPSHOT_PATH", "/tmp/cash-buffer.csv")

    settings = load_platform_runtime_settings(project_id_resolver=lambda: "project-1")

    assert settings.strategy_profile == "tech_communication_pullback_enhancement"
    assert settings.strategy_display_name == "Tech/Communication Pullback Enhancement"
    assert settings.strategy_target_mode == "weight"


@pytest.mark.parametrize(
    "archived_profile",
    (
        "mega_cap_leader_rotation_dynamic_top20",
        "mega_cap_leader_rotation_aggressive",
        "dynamic_mega_leveraged_pullback",
    ),
)
def test_load_platform_runtime_settings_rejects_removed_research_profiles(monkeypatch, archived_profile):
    monkeypatch.setenv("RUNTIME_TARGET_JSON", runtime_target_json(archived_profile))
    monkeypatch.setenv("ACCOUNT_GROUP", "paper")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)
    monkeypatch.setenv("IBKR_FEATURE_SNAPSHOT_PATH", "/tmp/archive.csv")
    monkeypatch.setenv("IBKR_FEATURE_SNAPSHOT_MANIFEST_PATH", "/tmp/archive.csv.manifest.json")

    with pytest.raises(ValueError, match="Unsupported STRATEGY_PROFILE"):
        load_platform_runtime_settings(project_id_resolver=lambda: "project-1")


def test_load_platform_runtime_settings_accepts_tqqq_growth_income(monkeypatch):
    monkeypatch.setenv("RUNTIME_TARGET_JSON", runtime_target_json("tqqq_growth_income"))
    monkeypatch.setenv("ACCOUNT_GROUP", "paper")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)

    settings = load_platform_runtime_settings(project_id_resolver=lambda: "project-1")

    assert settings.strategy_profile == "tqqq_growth_income"
    assert settings.strategy_display_name == "TQQQ Growth Income"
    assert settings.strategy_target_mode == "value"


def test_load_platform_runtime_settings_accepts_nasdaq_sp500_smart_dca(monkeypatch):
    monkeypatch.setenv("RUNTIME_TARGET_JSON", runtime_target_json("nasdaq_sp500_smart_dca"))
    monkeypatch.setenv("ACCOUNT_GROUP", "paper")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)

    settings = load_platform_runtime_settings(project_id_resolver=lambda: "project-1")

    assert settings.strategy_profile == "nasdaq_sp500_smart_dca"
    assert settings.strategy_display_name == "Nasdaq/S&P 500 Smart DCA"
    assert settings.strategy_target_mode == "value"


def test_load_platform_runtime_settings_rejects_legacy_qqq_tech_alias(monkeypatch):
    monkeypatch.setenv("RUNTIME_TARGET_JSON", runtime_target_json("tech_pullback_cash_buffer"))
    monkeypatch.setenv("ACCOUNT_GROUP", "paper")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)
    monkeypatch.setenv("IBKR_FEATURE_SNAPSHOT_PATH", "/tmp/cash-buffer.csv")

    with pytest.raises(ValueError, match="Unsupported STRATEGY_PROFILE"):
        load_platform_runtime_settings(project_id_resolver=lambda: "project-1")


def test_platform_profile_matrix_exposes_profiles_without_selection_roles():
    rows = get_platform_profile_matrix()
    by_profile = {row["canonical_profile"]: row for row in rows}
    assert "is_default" not in by_profile["global_etf_rotation"]
    assert "is_rollback" not in by_profile["global_etf_rotation"]
    assert by_profile["tech_communication_pullback_enhancement"]["display_name"] == "Tech/Communication Pullback Enhancement"


def test_platform_profile_status_matrix_matches_current_ibkr_rollout():
    rows = get_platform_profile_status_matrix()
    by_profile = {row["canonical_profile"]: row for row in rows}

    assert set(by_profile) == {
        "global_etf_rotation",
        "russell_1000_multi_factor_defensive",
        "soxl_soxx_trend_income",
        "tqqq_growth_income",
        "tech_communication_pullback_enhancement",
        "mega_cap_leader_rotation_top50_balanced",
        "nasdaq_sp500_smart_dca",
    }
    assert by_profile["global_etf_rotation"] == {
        "canonical_profile": "global_etf_rotation",
        "display_name": "Global ETF Rotation",
        "domain": "us_equity",
        "eligible": True,
        "enabled": True,
        "platform": "ibkr",
    }
    assert by_profile["soxl_soxx_trend_income"]["display_name"] == "SOXL/SOXX Semiconductor Trend Income"
    assert by_profile["soxl_soxx_trend_income"]["eligible"] is True
    assert by_profile["soxl_soxx_trend_income"]["enabled"] is True
    assert by_profile["tqqq_growth_income"]["display_name"] == "TQQQ Growth Income"
    assert by_profile["tqqq_growth_income"]["eligible"] is True
    assert by_profile["tqqq_growth_income"]["enabled"] is True
    assert by_profile["nasdaq_sp500_smart_dca"]["display_name"] == "Nasdaq/S&P 500 Smart DCA"
    assert by_profile["nasdaq_sp500_smart_dca"]["eligible"] is True
    assert by_profile["nasdaq_sp500_smart_dca"]["enabled"] is True


def test_print_strategy_profile_status_json_matches_registry():
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--json"],
        check=True,
        capture_output=True,
        text=True,
    )

    rows = json.loads(result.stdout)
    assert [
        {
            key: row[key]
            for key in (
                "canonical_profile",
                "display_name",
                "domain",
                "eligible",
                "enabled",
                "platform",
            )
        }
        for row in rows
    ] == get_platform_profile_status_matrix()
    by_profile = {row["canonical_profile"]: row for row in rows}
    assert by_profile["global_etf_rotation"]["profile_group"] == "direct_runtime_inputs"
    assert by_profile["global_etf_rotation"]["input_mode"] == "market_history"
    assert by_profile["global_etf_rotation"]["requires_snapshot_artifacts"] is False
    assert by_profile["global_etf_rotation"]["requires_strategy_config_path"] is False
    assert by_profile["nasdaq_sp500_smart_dca"]["profile_group"] == "direct_runtime_inputs"
    assert by_profile["nasdaq_sp500_smart_dca"]["input_mode"] == "market_history+portfolio_snapshot"
    assert by_profile["nasdaq_sp500_smart_dca"]["requires_snapshot_artifacts"] is False
    assert by_profile["tech_communication_pullback_enhancement"]["profile_group"] == "snapshot_backed"
    assert by_profile["tech_communication_pullback_enhancement"]["input_mode"] == "feature_snapshot"
    assert by_profile["tech_communication_pullback_enhancement"]["requires_snapshot_artifacts"] is True
    assert by_profile["tech_communication_pullback_enhancement"]["requires_strategy_config_path"] is True
    assert by_profile["mega_cap_leader_rotation_top50_balanced"]["profile_group"] == "snapshot_backed"
    assert by_profile["mega_cap_leader_rotation_top50_balanced"]["input_mode"] == "feature_snapshot"
    assert by_profile["mega_cap_leader_rotation_top50_balanced"]["requires_snapshot_artifacts"] is True
    assert by_profile["mega_cap_leader_rotation_top50_balanced"]["requires_strategy_config_path"] is False
    assert by_profile["russell_1000_multi_factor_defensive"]["requires_strategy_config_path"] is False


def test_print_strategy_profile_status_table_contains_expected_headers():
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "canonical_profile" in result.stdout
    assert "display_name" in result.stdout
    assert "profile_group" in result.stdout
    assert "input_mode" in result.stdout
    assert "requires_snapshot_artifacts" in result.stdout
    assert "global_etf_rotation" in result.stdout
    assert "Tech/Communication Pullback Enhancement" in result.stdout
    assert "TQQQ Growth Income" in result.stdout


def test_print_strategy_switch_env_plan_for_tqqq_growth_income():
    result = subprocess.run(
        [sys.executable, str(SWITCH_PLAN_SCRIPT_PATH), "--profile", "tqqq_growth_income", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )

    plan = json.loads(result.stdout)
    assert plan["platform"] == "ibkr"
    assert plan["canonical_profile"] == "tqqq_growth_income"
    assert plan["eligible"] is True
    assert plan["enabled"] is True
    assert plan["runtime_target"]["platform_id"] == "ibkr"
    assert plan["runtime_target"]["strategy_profile"] == "tqqq_growth_income"
    assert plan["runtime_target"]["service_name"] == "interactive-brokers-quant-service"
    assert plan["runtime_target"]["execution_mode"] == "live"
    assert plan["profile_group"] == "direct_runtime_inputs"
    assert plan["input_mode"] == "benchmark_history+portfolio_snapshot"
    assert plan["requires_snapshot_artifacts"] is False
    assert plan["requires_strategy_config_path"] is False
    assert json.loads(plan["set_env"]["RUNTIME_TARGET_JSON"])["strategy_profile"] == "tqqq_growth_income"
    assert "ACCOUNT_GROUP" in plan["keep_env"]
    assert "IBKR_MIN_RESERVED_CASH_USD" in plan["optional_env"]
    assert "IBKR_RESERVED_CASH_RATIO" in plan["optional_env"]
    assert "IBKR_SAFE_HAVEN_CASH_SUBSTITUTE_THRESHOLD_USD" in plan["optional_env"]
    assert "IBKR_FEATURE_SNAPSHOT_PATH" in plan["remove_if_present"]


def test_build_cloud_run_env_sync_plan_supports_per_service_targets():
    payload = {
        "defaults": {
            "GLOBAL_TELEGRAM_CHAT_ID": "5992562050",
            "NOTIFY_LANG": "zh",
            "IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME": "ibkr-account-groups",
            "IB_GATEWAY_ZONE": "us-central1-c",
            "IB_GATEWAY_IP_MODE": "internal",
            "EXECUTION_REPORT_GCS_URI": "gs://runtime/execution-reports",
        },
        "targets": [
            {
                "service": "interactive-brokers-live-slot-a-service",
                "account_group": "live-slot-a",
                "runtime_target": json.loads(
                    runtime_target_json(
                        "tqqq_growth_income",
                        deployment_selector="live-slot-a",
                        account_selector=["U18308207"],
                        account_scope="live-slot-a",
                        service_name="interactive-brokers-live-slot-a-service",
                    )
                ),
                "ibkr_strategy_plugin_mounts_json": {
                    "strategy_plugins": [
                        {
                            "strategy": "tqqq_growth_income",
                            "plugin": "crisis_response_shadow",
                            "signal_path": "gs://runtime/tqqq/latest_signal.json",
                            "enabled": True,
                            "expected_mode": "shadow",
                        }
                    ]
                },
            },
            {
                "service": "interactive-brokers-live-slot-b-service",
                "account_group": "live-slot-b",
                "runtime_target": json.loads(
                    runtime_target_json(
                        "mega_cap_leader_rotation_top50_balanced",
                        deployment_selector="live-slot-b",
                        account_selector=["U15998061"],
                        account_scope="live-slot-b",
                        service_name="interactive-brokers-live-slot-b-service",
                    )
                ),
                "ibkr_feature_snapshot_path": "gs://runtime/mega/snapshot.csv",
                "ibkr_feature_snapshot_manifest_path": "gs://runtime/mega/snapshot.csv.manifest.json",
            },
        ],
    }
    env = {
        **os.environ,
        "CLOUD_RUN_SERVICE_TARGETS_JSON": json.dumps(payload),
        "IBKR_FEATURE_SNAPSHOT_PATH": "gs://stale-paper/snapshot.csv",
    }

    result = subprocess.run(
        [sys.executable, str(SYNC_PLAN_SCRIPT_PATH), "--json"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    plan = json.loads(result.stdout)
    assert plan["mode"] == "per_service"
    by_service = {target["service_name"]: target for target in plan["targets"]}
    slot_a = by_service["interactive-brokers-live-slot-a-service"]
    slot_b = by_service["interactive-brokers-live-slot-b-service"]

    assert slot_a["env"]["ACCOUNT_GROUP"] == "live-slot-a"
    assert slot_a["env"]["STRATEGY_PROFILE"] == "tqqq_growth_income"
    assert "IBKR_FEATURE_SNAPSHOT_PATH" not in slot_a["env"]
    assert "IBKR_FEATURE_SNAPSHOT_PATH" in slot_a["remove_env_vars"]
    assert "gs://stale-paper/snapshot.csv" not in json.dumps(slot_a)
    assert json.loads(slot_a["env"]["IBKR_STRATEGY_PLUGIN_MOUNTS_JSON"])["strategy_plugins"][0][
        "strategy"
    ] == "tqqq_growth_income"

    assert slot_b["env"]["ACCOUNT_GROUP"] == "live-slot-b"
    assert slot_b["env"]["STRATEGY_PROFILE"] == "mega_cap_leader_rotation_top50_balanced"
    assert slot_b["env"]["IBKR_FEATURE_SNAPSHOT_PATH"] == "gs://runtime/mega/snapshot.csv"
    assert (
        slot_b["env"]["IBKR_FEATURE_SNAPSHOT_MANIFEST_PATH"]
        == "gs://runtime/mega/snapshot.csv.manifest.json"
    )


def test_build_cloud_run_env_sync_plan_requires_target_snapshot_in_per_service_mode():
    payload = {
        "defaults": {
            "GLOBAL_TELEGRAM_CHAT_ID": "5992562050",
            "NOTIFY_LANG": "zh",
            "IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME": "ibkr-account-groups",
        },
        "targets": [
            {
                "service": "interactive-brokers-live-slot-b-service",
                "account_group": "live-slot-b",
                "runtime_target": json.loads(
                    runtime_target_json(
                        "mega_cap_leader_rotation_top50_balanced",
                        deployment_selector="live-slot-b",
                        account_selector=["U15998061"],
                        account_scope="live-slot-b",
                        service_name="interactive-brokers-live-slot-b-service",
                    )
                ),
            }
        ],
    }
    env = {
        **os.environ,
        "CLOUD_RUN_SERVICE_TARGETS_JSON": json.dumps(payload),
        "IBKR_FEATURE_SNAPSHOT_PATH": "gs://stale-paper/snapshot.csv",
    }

    result = subprocess.run(
        [sys.executable, str(SYNC_PLAN_SCRIPT_PATH), "--json"],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode != 0
    assert "interactive-brokers-live-slot-b-service:IBKR_FEATURE_SNAPSHOT_PATH" in result.stderr
    assert "gs://stale-paper/snapshot.csv" not in result.stderr


def test_print_strategy_switch_env_plan_rejects_removed_research_profile():
    result = subprocess.run(
        [sys.executable, str(SWITCH_PLAN_SCRIPT_PATH), "--profile", "mega_cap_leader_rotation_dynamic_top20", "--json"],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Unsupported STRATEGY_PROFILE" in result.stderr


def test_print_strategy_switch_env_plan_for_mega_cap_top50_balanced_profile():
    result = subprocess.run(
        [sys.executable, str(SWITCH_PLAN_SCRIPT_PATH), "--profile", "mega_cap_leader_rotation_top50_balanced", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )

    plan = json.loads(result.stdout)
    assert plan["canonical_profile"] == "mega_cap_leader_rotation_top50_balanced"
    assert plan["profile_group"] == "snapshot_backed"
    assert plan["input_mode"] == "feature_snapshot"
    assert plan["requires_snapshot_artifacts"] is True
    assert plan["requires_strategy_config_path"] is False
    assert plan["set_env"]["IBKR_FEATURE_SNAPSHOT_PATH"] == "<required>"
    assert plan["set_env"]["IBKR_FEATURE_SNAPSHOT_MANIFEST_PATH"] == "<required>"
    assert plan["hints"]["feature_snapshot_filename"] == "mega_cap_leader_rotation_top50_balanced_feature_snapshot_latest.csv"


def test_print_strategy_switch_env_plan_for_feature_snapshot_profile():
    result = subprocess.run(
        [sys.executable, str(SWITCH_PLAN_SCRIPT_PATH), "--profile", "tech_communication_pullback_enhancement", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )

    plan = json.loads(result.stdout)
    assert plan["canonical_profile"] == "tech_communication_pullback_enhancement"
    assert plan["profile_group"] == "snapshot_backed"
    assert plan["input_mode"] == "feature_snapshot"
    assert plan["requires_snapshot_artifacts"] is True
    assert plan["requires_strategy_config_path"] is True
    assert plan["config_source_policy"] == "bundled_or_env"
    assert plan["set_env"]["IBKR_FEATURE_SNAPSHOT_PATH"] == "<required>"
    assert plan["set_env"]["IBKR_FEATURE_SNAPSHOT_MANIFEST_PATH"] == "<required>"
    assert "IBKR_STRATEGY_CONFIG_PATH" not in plan["set_env"]
    assert "IBKR_STRATEGY_CONFIG_PATH" in plan["remove_if_present"]
    assert "IBKR_RECONCILIATION_OUTPUT_PATH" in plan["optional_env"]


def test_print_strategy_switch_env_plan_uses_manifest_contract_policy():
    result = subprocess.run(
        [
            sys.executable,
            str(SWITCH_PLAN_SCRIPT_PATH),
            "--profile",
            "russell_1000_multi_factor_defensive",
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    plan = json.loads(result.stdout)
    assert plan["canonical_profile"] == "russell_1000_multi_factor_defensive"
    assert plan["requires_snapshot_artifacts"] is True
    assert plan["requires_snapshot_manifest_path"] is False
    assert plan["set_env"]["IBKR_FEATURE_SNAPSHOT_PATH"] == "<required>"
    assert "IBKR_FEATURE_SNAPSHOT_MANIFEST_PATH" in plan["remove_if_present"]
    assert "IBKR_FEATURE_SNAPSHOT_MANIFEST_PATH" not in plan["set_env"]



def test_load_platform_runtime_settings_reads_feature_snapshot_path(monkeypatch):
    monkeypatch.setenv(
        "RUNTIME_TARGET_JSON",
        runtime_target_json("russell_1000_multi_factor_defensive"),
    )
    monkeypatch.setenv("ACCOUNT_GROUP", "paper")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)
    monkeypatch.setenv("IBKR_FEATURE_SNAPSHOT_PATH", "/tmp/r1000-latest.csv")

    settings = load_platform_runtime_settings(project_id_resolver=lambda: "project-1")

    assert settings.feature_snapshot_path == "/tmp/r1000-latest.csv"


def test_load_platform_runtime_settings_reads_tech_pullback_runtime_config(monkeypatch):
    monkeypatch.setenv(
        "RUNTIME_TARGET_JSON",
        runtime_target_json("tech_communication_pullback_enhancement"),
    )
    monkeypatch.setenv("ACCOUNT_GROUP", "paper")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)
    monkeypatch.setenv("IBKR_FEATURE_SNAPSHOT_PATH", "/tmp/cash-buffer.csv")
    monkeypatch.setenv("IBKR_FEATURE_SNAPSHOT_MANIFEST_PATH", "/tmp/cash-buffer.csv.manifest.json")
    monkeypatch.setenv("IBKR_STRATEGY_CONFIG_PATH", "/tmp/cash-buffer-config.json")
    monkeypatch.setenv("IBKR_RECONCILIATION_OUTPUT_PATH", "/tmp/reconciliation.json")
    monkeypatch.setenv("IBKR_DRY_RUN_ONLY", "true")

    settings = load_platform_runtime_settings(project_id_resolver=lambda: "project-1")

    assert settings.feature_snapshot_path == "/tmp/cash-buffer.csv"
    assert settings.feature_snapshot_manifest_path == "/tmp/cash-buffer.csv.manifest.json"
    assert settings.strategy_config_path == "/tmp/cash-buffer-config.json"
    assert settings.strategy_config_source == "env"
    assert settings.reconciliation_output_path == "/tmp/reconciliation.json"
    assert settings.dry_run_only is True


def test_load_platform_runtime_settings_uses_bundled_tech_pullback_config_when_env_missing(monkeypatch):
    monkeypatch.setenv(
        "RUNTIME_TARGET_JSON",
        runtime_target_json("tech_communication_pullback_enhancement"),
    )
    monkeypatch.setenv("ACCOUNT_GROUP", "paper")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)
    monkeypatch.setenv("IBKR_FEATURE_SNAPSHOT_PATH", "/tmp/cash-buffer.csv")
    monkeypatch.delenv("IBKR_STRATEGY_CONFIG_PATH", raising=False)
    monkeypatch.delenv("STRATEGY_CONFIG_PATH", raising=False)

    settings = load_platform_runtime_settings(project_id_resolver=lambda: "project-1")

    assert settings.strategy_config_path is not None
    assert settings.strategy_config_path.endswith("tech_communication_pullback_enhancement.json")
    assert settings.strategy_config_source == "bundled_canonical_default"


def test_load_platform_runtime_settings_derives_artifact_paths_from_root(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "RUNTIME_TARGET_JSON",
        runtime_target_json("tech_communication_pullback_enhancement"),
    )
    monkeypatch.setenv("ACCOUNT_GROUP", "paper")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)
    monkeypatch.setenv("IBKR_STRATEGY_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.delenv("IBKR_FEATURE_SNAPSHOT_PATH", raising=False)
    monkeypatch.delenv("IBKR_FEATURE_SNAPSHOT_MANIFEST_PATH", raising=False)
    monkeypatch.delenv("IBKR_RECONCILIATION_OUTPUT_PATH", raising=False)
    monkeypatch.delenv("IBKR_STRATEGY_CONFIG_PATH", raising=False)
    monkeypatch.delenv("STRATEGY_CONFIG_PATH", raising=False)

    settings = load_platform_runtime_settings(project_id_resolver=lambda: "project-1")

    assert settings.strategy_artifact_root == str(tmp_path)
    assert settings.strategy_artifact_dir == str(tmp_path / "tech_communication_pullback_enhancement")
    assert settings.feature_snapshot_path == str(
        tmp_path / "tech_communication_pullback_enhancement" / "tech_communication_pullback_enhancement_feature_snapshot_latest.csv"
    )
    assert settings.feature_snapshot_manifest_path == str(
        tmp_path
        / "tech_communication_pullback_enhancement"
        / "tech_communication_pullback_enhancement_feature_snapshot_latest.csv.manifest.json"
    )
    assert settings.reconciliation_output_path == str(
        tmp_path / "tech_communication_pullback_enhancement" / "reconciliation"
    )
    assert settings.strategy_config_path is not None
    assert settings.strategy_config_path.endswith("tech_communication_pullback_enhancement.json")
    assert settings.strategy_config_source == "bundled_canonical_default"



def test_load_platform_runtime_settings_uses_account_group_secret(monkeypatch):
    monkeypatch.setenv("RUNTIME_TARGET_JSON", runtime_target_json(SAMPLE_STRATEGY_PROFILE))
    monkeypatch.setenv("ACCOUNT_GROUP", "paper")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME", "ibkr-account-groups")

    payload = """
    {
      "groups": {
        "paper": {
          "ib_gateway_instance_name": "ib-gateway-paper",
          "ib_gateway_zone": "us-central1-a",
          "ib_gateway_mode": "live",
          "ib_gateway_port": 4011,
          "ib_gateway_ip_mode": "external",
          "ib_client_id": 9,
          "service_name": "interactive-brokers-paper-service",
          "account_ids": ["U1234567", "U7654321"]
        }
      }
    }
    """

    class FakeSecretClient:
        def access_secret_version(self, request):
            assert request["name"] == "projects/project-1/secrets/ibkr-account-groups/versions/latest"
            return type(
                "Resp",
                (),
                {"payload": type("Payload", (), {"data": payload.encode("utf-8")})()},
            )()

    settings = load_platform_runtime_settings(
        project_id_resolver=lambda: "project-1",
        secret_client_factory=FakeSecretClient,
    )

    assert settings.ib_gateway_instance_name == "ib-gateway-paper"
    assert settings.ib_gateway_zone == "us-central1-a"
    assert settings.ib_gateway_mode == "live"
    assert settings.ib_gateway_port == 4011
    assert settings.ib_gateway_ip_mode == "external"
    assert settings.ib_client_id == 9
    assert settings.account_group == "paper"
    assert settings.service_name == "interactive-brokers-paper-service"
    assert settings.account_ids == ("U1234567", "U7654321")



def test_load_platform_runtime_settings_requires_project_for_secret_source(monkeypatch):
    monkeypatch.setenv("RUNTIME_TARGET_JSON", runtime_target_json(SAMPLE_STRATEGY_PROFILE))
    monkeypatch.setenv("ACCOUNT_GROUP", "paper")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME", "ibkr-account-groups")

    with pytest.raises(
        EnvironmentError,
        match="GOOGLE_CLOUD_PROJECT is required when IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME is set",
    ):
        load_platform_runtime_settings(project_id_resolver=lambda: None)



def test_load_platform_runtime_settings_rejects_unknown_account_group(monkeypatch):
    monkeypatch.setenv("RUNTIME_TARGET_JSON", runtime_target_json(SAMPLE_STRATEGY_PROFILE))
    monkeypatch.setenv("ACCOUNT_GROUP", "missing")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)

    with pytest.raises(ValueError, match="ACCOUNT_GROUP='missing'"):
        load_platform_runtime_settings(project_id_resolver=lambda: "project-1")



def test_load_platform_runtime_settings_requires_key_group_fields(monkeypatch):
    monkeypatch.setenv("RUNTIME_TARGET_JSON", runtime_target_json(SAMPLE_STRATEGY_PROFILE))
    monkeypatch.setenv("ACCOUNT_GROUP", "paper")
    monkeypatch.setenv(
        "IB_ACCOUNT_GROUP_CONFIG_JSON",
        '{"groups":{"paper":{"ib_gateway_mode":"paper","ib_client_id":1}}}',
    )

    with pytest.raises(EnvironmentError, match="requires ib_gateway_instance_name"):
        load_platform_runtime_settings(project_id_resolver=lambda: "project-1")



def test_parse_account_group_configs_supports_top_level_mapping():
    configs = parse_account_group_configs(
        '{"paper": {"ib_gateway_instance_name":"ib-gateway","ib_gateway_mode":"paper",'
        '"ib_gateway_port":"4012","ib_client_id":"4","account_ids":["U1"],"service_name":"svc"}}'
    )

    assert configs["paper"].ib_gateway_port == 4012
    assert configs["paper"].ib_client_id == 4
    assert configs["paper"].account_ids == ("U1",)
    assert configs["paper"].service_name == "svc"


def test_load_platform_runtime_settings_rejects_legacy_cash_buffer_profile(monkeypatch):
    monkeypatch.setenv("RUNTIME_TARGET_JSON", runtime_target_json("cash_buffer_branch_default"))
    monkeypatch.setenv("ACCOUNT_GROUP", "paper")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)

    with pytest.raises(ValueError, match="Unsupported STRATEGY_PROFILE"):
        load_platform_runtime_settings(project_id_resolver=lambda: "project-1")
