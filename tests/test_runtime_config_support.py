import pytest

from runtime_config_support import (
    DEFAULT_STRATEGY_PROFILE,
    load_platform_runtime_settings,
    parse_account_group_configs,
)
from strategy_registry import IBKR_PLATFORM, US_EQUITY_DOMAIN, get_platform_profile_matrix, get_supported_profiles_for_platform


MINIMAL_GROUP_JSON = (
    '{"groups":{"default":{"ib_gateway_instance_name":"ib-gateway",'
    '"ib_gateway_mode":"paper","ib_client_id":1}}}'
)


def test_load_platform_runtime_settings_requires_strategy_profile(monkeypatch):
    monkeypatch.setenv("ACCOUNT_GROUP", "default")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)
    monkeypatch.delenv("STRATEGY_PROFILE", raising=False)

    with pytest.raises(EnvironmentError, match="STRATEGY_PROFILE is required"):
        load_platform_runtime_settings(project_id_resolver=lambda: "project-1")



def test_load_platform_runtime_settings_requires_account_group(monkeypatch):
    monkeypatch.setenv("STRATEGY_PROFILE", DEFAULT_STRATEGY_PROFILE)
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)
    monkeypatch.delenv("ACCOUNT_GROUP", raising=False)

    with pytest.raises(EnvironmentError, match="ACCOUNT_GROUP is required"):
        load_platform_runtime_settings(project_id_resolver=lambda: "project-1")



def test_load_platform_runtime_settings_requires_account_group_config_source(monkeypatch):
    monkeypatch.setenv("STRATEGY_PROFILE", DEFAULT_STRATEGY_PROFILE)
    monkeypatch.setenv("ACCOUNT_GROUP", "default")
    monkeypatch.delenv("IB_ACCOUNT_GROUP_CONFIG_JSON", raising=False)
    monkeypatch.delenv("IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME", raising=False)

    with pytest.raises(
        EnvironmentError,
        match="IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME or IB_ACCOUNT_GROUP_CONFIG_JSON is required",
    ):
        load_platform_runtime_settings(project_id_resolver=lambda: "project-1")



def test_load_platform_runtime_settings_uses_minimal_group_config(monkeypatch):
    monkeypatch.setenv("STRATEGY_PROFILE", DEFAULT_STRATEGY_PROFILE)
    monkeypatch.setenv("ACCOUNT_GROUP", "default")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)
    monkeypatch.delenv("IB_GATEWAY_ZONE", raising=False)
    monkeypatch.delenv("IB_GATEWAY_IP_MODE", raising=False)
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("GLOBAL_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("NOTIFY_LANG", raising=False)

    settings = load_platform_runtime_settings(project_id_resolver=lambda: "project-1")

    assert settings.project_id == "project-1"
    assert settings.ib_gateway_instance_name == "ib-gateway"
    assert settings.ib_gateway_zone == ""
    assert settings.ib_gateway_mode == "paper"
    assert settings.ib_gateway_ip_mode == "internal"
    assert settings.ib_client_id == 1
    assert settings.strategy_profile == DEFAULT_STRATEGY_PROFILE
    assert settings.strategy_domain == US_EQUITY_DOMAIN
    assert settings.feature_snapshot_path is None
    assert settings.feature_snapshot_manifest_path is None
    assert settings.strategy_config_path is None
    assert settings.strategy_config_source is None
    assert settings.reconciliation_output_path is None
    assert settings.dry_run_only is False
    assert settings.account_group == "default"
    assert settings.service_name is None
    assert settings.account_ids == ()
    assert settings.notify_lang == "en"
    assert settings.tg_token is None
    assert settings.tg_chat_id is None



def test_load_platform_runtime_settings_supports_explicit_group_config_values(monkeypatch):
    monkeypatch.setenv("STRATEGY_PROFILE", DEFAULT_STRATEGY_PROFILE)
    monkeypatch.setenv("ACCOUNT_GROUP", "taxable_main")
    monkeypatch.setenv(
        "IB_ACCOUNT_GROUP_CONFIG_JSON",
        '{"groups":{"taxable_main":{"ib_gateway_instance_name":"ib-gateway-main",'
        '"ib_gateway_zone":"us-central1-a","ib_gateway_mode":"live",'
        '"ib_gateway_ip_mode":"external","ib_client_id":7,'
        '"service_name":"interactive-brokers-quant-global-etf-rotation-taxable-main",'
        '"account_ids":["U1234567"]}}}',
    )
    monkeypatch.setenv("TELEGRAM_TOKEN", "token-1")
    monkeypatch.setenv("GLOBAL_TELEGRAM_CHAT_ID", "chat-1")
    monkeypatch.setenv("NOTIFY_LANG", "zh")

    settings = load_platform_runtime_settings(project_id_resolver=lambda: None)

    assert settings.ib_gateway_instance_name == "ib-gateway-main"
    assert settings.ib_gateway_zone == "us-central1-a"
    assert settings.ib_gateway_mode == "live"
    assert settings.ib_gateway_ip_mode == "external"
    assert settings.ib_client_id == 7
    assert settings.strategy_profile == DEFAULT_STRATEGY_PROFILE
    assert settings.strategy_domain == US_EQUITY_DOMAIN
    assert settings.feature_snapshot_path is None
    assert settings.feature_snapshot_manifest_path is None
    assert settings.account_group == "taxable_main"
    assert settings.service_name == "interactive-brokers-quant-global-etf-rotation-taxable-main"
    assert settings.account_ids == ("U1234567",)
    assert settings.tg_token == "token-1"
    assert settings.tg_chat_id == "chat-1"
    assert settings.notify_lang == "zh"



def test_load_platform_runtime_settings_rejects_unknown_strategy_profile(monkeypatch):
    monkeypatch.setenv("STRATEGY_PROFILE", "balanced_income")
    monkeypatch.setenv("ACCOUNT_GROUP", "default")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)

    with pytest.raises(ValueError, match="Unsupported STRATEGY_PROFILE"):
        load_platform_runtime_settings(project_id_resolver=lambda: None)


def test_platform_supported_profiles_are_filtered_by_registry():
    assert get_supported_profiles_for_platform(IBKR_PLATFORM) == frozenset(
        {
            "tech_pullback_cash_buffer",
            "global_etf_rotation",
            "russell_1000_multi_factor_defensive",
        }
    )


def test_load_platform_runtime_settings_accepts_tech_pullback_cash_buffer(monkeypatch):
    monkeypatch.setenv("STRATEGY_PROFILE", "tech_pullback_cash_buffer")
    monkeypatch.setenv("ACCOUNT_GROUP", "default")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)
    monkeypatch.setenv("IBKR_FEATURE_SNAPSHOT_PATH", "/tmp/cash-buffer.csv")

    settings = load_platform_runtime_settings(project_id_resolver=lambda: "project-1")

    assert settings.strategy_profile == "tech_pullback_cash_buffer"


def test_platform_profile_matrix_marks_default_and_rollback():
    rows = get_platform_profile_matrix()
    by_profile = {row["canonical_profile"]: row for row in rows}
    assert by_profile["global_etf_rotation"]["is_default"] is True
    assert by_profile["global_etf_rotation"]["is_rollback"] is True
    assert by_profile["tech_pullback_cash_buffer"]["display_name"] == "Tech Pullback Cash Buffer"



def test_load_platform_runtime_settings_reads_feature_snapshot_path(monkeypatch):
    monkeypatch.setenv("STRATEGY_PROFILE", "russell_1000_multi_factor_defensive")
    monkeypatch.setenv("ACCOUNT_GROUP", "default")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)
    monkeypatch.setenv("IBKR_FEATURE_SNAPSHOT_PATH", "/tmp/r1000-latest.csv")

    settings = load_platform_runtime_settings(project_id_resolver=lambda: "project-1")

    assert settings.feature_snapshot_path == "/tmp/r1000-latest.csv"


def test_load_platform_runtime_settings_reads_tech_pullback_runtime_config(monkeypatch):
    monkeypatch.setenv("STRATEGY_PROFILE", "tech_pullback_cash_buffer")
    monkeypatch.setenv("ACCOUNT_GROUP", "default")
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
    monkeypatch.setenv("STRATEGY_PROFILE", "tech_pullback_cash_buffer")
    monkeypatch.setenv("ACCOUNT_GROUP", "default")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)
    monkeypatch.setenv("IBKR_FEATURE_SNAPSHOT_PATH", "/tmp/cash-buffer.csv")
    monkeypatch.delenv("IBKR_STRATEGY_CONFIG_PATH", raising=False)
    monkeypatch.delenv("STRATEGY_CONFIG_PATH", raising=False)

    settings = load_platform_runtime_settings(project_id_resolver=lambda: "project-1")

    assert settings.strategy_config_path is not None
    assert settings.strategy_config_path.endswith("growth_pullback_tech_pullback_cash_buffer.json")
    assert settings.strategy_config_source == "bundled_canonical_default"



def test_load_platform_runtime_settings_uses_account_group_secret(monkeypatch):
    monkeypatch.setenv("STRATEGY_PROFILE", DEFAULT_STRATEGY_PROFILE)
    monkeypatch.setenv("ACCOUNT_GROUP", "ira")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME", "ibkr-account-groups")

    payload = """
    {
      "groups": {
        "ira": {
          "ib_gateway_instance_name": "ib-gateway-ira",
          "ib_gateway_zone": "us-central1-a",
          "ib_gateway_mode": "live",
          "ib_gateway_ip_mode": "external",
          "ib_client_id": 9,
          "service_name": "interactive-brokers-quant-global-etf-rotation-ira",
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

    assert settings.ib_gateway_instance_name == "ib-gateway-ira"
    assert settings.ib_gateway_zone == "us-central1-a"
    assert settings.ib_gateway_mode == "live"
    assert settings.ib_gateway_ip_mode == "external"
    assert settings.ib_client_id == 9
    assert settings.account_group == "ira"
    assert settings.service_name == "interactive-brokers-quant-global-etf-rotation-ira"
    assert settings.account_ids == ("U1234567", "U7654321")



def test_load_platform_runtime_settings_requires_project_for_secret_source(monkeypatch):
    monkeypatch.setenv("STRATEGY_PROFILE", DEFAULT_STRATEGY_PROFILE)
    monkeypatch.setenv("ACCOUNT_GROUP", "default")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME", "ibkr-account-groups")

    with pytest.raises(
        EnvironmentError,
        match="GOOGLE_CLOUD_PROJECT is required when IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME is set",
    ):
        load_platform_runtime_settings(project_id_resolver=lambda: None)



def test_load_platform_runtime_settings_rejects_unknown_account_group(monkeypatch):
    monkeypatch.setenv("STRATEGY_PROFILE", DEFAULT_STRATEGY_PROFILE)
    monkeypatch.setenv("ACCOUNT_GROUP", "missing")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)

    with pytest.raises(ValueError, match="ACCOUNT_GROUP='missing'"):
        load_platform_runtime_settings(project_id_resolver=lambda: "project-1")



def test_load_platform_runtime_settings_requires_key_group_fields(monkeypatch):
    monkeypatch.setenv("STRATEGY_PROFILE", DEFAULT_STRATEGY_PROFILE)
    monkeypatch.setenv("ACCOUNT_GROUP", "default")
    monkeypatch.setenv(
        "IB_ACCOUNT_GROUP_CONFIG_JSON",
        '{"groups":{"default":{"ib_gateway_mode":"paper","ib_client_id":1}}}',
    )

    with pytest.raises(EnvironmentError, match="requires ib_gateway_instance_name"):
        load_platform_runtime_settings(project_id_resolver=lambda: "project-1")



def test_parse_account_group_configs_supports_top_level_mapping():
    configs = parse_account_group_configs(
        '{"default": {"ib_gateway_instance_name":"ib-gateway","ib_gateway_mode":"paper",'
        '"ib_client_id":"4","account_ids":["U1"],"service_name":"svc"}}'
    )

    assert configs["default"].ib_client_id == 4
    assert configs["default"].account_ids == ("U1",)
    assert configs["default"].service_name == "svc"


def test_load_platform_runtime_settings_rejects_legacy_cash_buffer_profile(monkeypatch):
    monkeypatch.setenv("STRATEGY_PROFILE", "cash_buffer_branch_default")
    monkeypatch.setenv("ACCOUNT_GROUP", "default")
    monkeypatch.setenv("IB_ACCOUNT_GROUP_CONFIG_JSON", MINIMAL_GROUP_JSON)

    with pytest.raises(ValueError, match="Unsupported STRATEGY_PROFILE"):
        load_platform_runtime_settings(project_id_resolver=lambda: "project-1")
