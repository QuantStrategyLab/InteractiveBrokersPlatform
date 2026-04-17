from notifications.telegram import build_strategy_display_name, build_translator, send_telegram_message
from strategy_registry import SUPPORTED_STRATEGY_PROFILES


def test_build_translator_supports_chinese():
    translate = build_translator("zh")
    assert translate("equity") == "净值"
    assert translate("target_weights_title") == "目标持仓"
    assert translate("market_status_risk_on", asset="SOXL") == "🚀 风险开启（SOXL）"
    assert translate("signal_risk_on", window=150, ratio="40.2%") == "SOXL 站上 150 日均线，持有 SOXL，交易层风险仓位 40.2%"
    assert translate("market_status_blend_gate_risk_on", asset="SOXX+SOXL") == "🚀 风险开启（SOXX+SOXL）"
    assert (
        translate(
            "signal_blend_gate_risk_on",
            trend_symbol="SOXX",
            window=140,
            soxl_ratio="70.0%",
            soxx_ratio="20.0%",
        )
        == "SOXX 站上 140 日门槛线，持有 SOXL 70.0% + SOXX 20.0%"
    )


def test_strategy_display_name_translates_new_live_profiles():
    zh_name = build_strategy_display_name(build_translator("zh"))
    en_name = build_strategy_display_name(build_translator("en"))

    assert zh_name("mega_cap_leader_rotation_aggressive") == "Mega Cap 激进龙头轮动"
    assert zh_name("mega_cap_leader_rotation_dynamic_top20") == "Mega Cap 动态 Top20 龙头轮动"
    assert zh_name("mega_cap_leader_rotation_top50_balanced") == "Mega Cap Top50 平衡龙头轮动"
    assert zh_name("dynamic_mega_leveraged_pullback") == "Mega Cap 2x 回调策略"
    assert en_name("mega_cap_leader_rotation_aggressive") == "Mega Cap Leader Rotation Aggressive"
    assert en_name("mega_cap_leader_rotation_dynamic_top20") == "Mega Cap Leader Rotation Dynamic Top20"
    assert en_name("mega_cap_leader_rotation_top50_balanced") == "Mega Cap Leader Rotation Top50 Balanced"
    assert en_name("dynamic_mega_leveraged_pullback") == "Dynamic Mega Leveraged Pullback"


def test_supported_strategy_profiles_have_translated_names():
    zh_name = build_strategy_display_name(build_translator("zh"))
    en_name = build_strategy_display_name(build_translator("en"))

    for profile in SUPPORTED_STRATEGY_PROFILES:
        assert zh_name(profile) != profile
        assert en_name(profile) != profile


def test_send_telegram_message_logs_non_200_response(capsys):
    class FakeResponse:
        status_code = 401
        text = "unauthorized"

    class FakeRequests:
        @staticmethod
        def post(*args, **kwargs):
            return FakeResponse()

    send_telegram_message(
        "hello",
        token="token",
        chat_id="chat-id",
        requests_module=FakeRequests,
    )

    captured = capsys.readouterr()
    assert "Telegram send failed with status 401: unauthorized" in captured.out
