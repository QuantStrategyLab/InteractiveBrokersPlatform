from notifications.telegram import build_strategy_display_name, build_translator, send_telegram_message
from strategy_registry import SUPPORTED_STRATEGY_PROFILES


def test_build_translator_supports_chinese():
    translate = build_translator("zh")
    assert translate("equity") == "净值"
    assert translate("account_summary_title") == "📊 账户摘要"
    assert translate("positions_title") == "💼 当前持仓"
    assert translate("execution_summary_title") == "🧾 执行摘要"
    assert translate("target_weights_title") == "目标持仓"
    assert translate("market_status_risk_on", asset="SOXL") == "🚀 风险开启（SOXL）"
    assert translate("signal_risk_on", window=150, ratio="40.2%") == "SOXL 站上 150 日均线，持有 SOXL，交易层风险仓位 40.2%"
    assert translate("paper_liquidation_only") == "IBKR 模拟账户清仓模式"
    assert translate("paper_liquidation_positions_seen", count=4) == "识别持仓=4"
    assert translate("market_status_blend_gate_risk_on", asset="SOXX+SOXL") == "🚀 风险开启（SOXX+SOXL）"
    assert translate("market_status_blend_gate_overlay_capped", asset="SOXX") == "🧯 过热降档（SOXX）"
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
    assert (
        translate(
            "blend_gate_reason_volatility_delever",
            symbol="SOXX",
            window=10,
            volatility="55.0%",
            threshold="50.0%",
            redirect_symbol="SOXX",
        )
        == "SOXX 10 日年化波动率 55.0% 高于 50.0%，SOXL 转向 SOXX"
    )
    assert (
        translate(
            "strategy_plugin_line",
            plugin=translate("strategy_plugin_name_crisis_response_shadow"),
            mode=translate("strategy_plugin_mode_shadow"),
            route=translate("strategy_plugin_route_no_action"),
            action=translate("strategy_plugin_action_watch_only"),
        )
        == "🧩 插件：危机观察通知 | 状态：未触发 | 提醒：仅通知"
    )
    assert (
        translate(
            "strategy_plugin_line",
            plugin=translate("strategy_plugin_name_taco_rebound_shadow"),
            mode=translate("strategy_plugin_mode_shadow"),
            route=translate("strategy_plugin_route_taco_rebound"),
            action=translate("strategy_plugin_action_notify_manual_review"),
        )
        == "🧩 插件：TACO 抄底观察通知 | 状态：TACO 反弹确认 | 提醒：通知人工复核"
    )
    assert translate("strategy_plugin_alert_guidance", guidance="小仓位博弈") == "处置建议：小仓位博弈"
    assert translate("strategy_plugin_alert_scope_note", scope_note="不会自动下单") == "执行范围：不会自动下单"
    assert "降低杠杆" in translate("strategy_plugin_guidance_crisis_response_shadow_true_crisis_defend")
    assert "小仓位" in translate("strategy_plugin_guidance_taco_rebound_shadow_taco_rebound_notify_manual_review")
    assert translate("account_ids_detail", account_ids="U1234567") == "🆔 账户: U1234567"
    assert (
        translate(
            "small_account_warning_note",
            portfolio_equity="$0",
            min_recommended_equity="$1,000",
            reason=translate(
                "small_account_warning_reason_integer_shares_min_position_value_may_prevent_backtest_replication"
            ),
        )
        == "小账户提示：净值 $0 低于建议 $1,000；整数股和最小仓位限制可能导致实盘无法完全复现回测"
    )


def test_strategy_display_name_translates_new_live_profiles():
    zh_name = build_strategy_display_name(build_translator("zh"))
    en_name = build_strategy_display_name(build_translator("en"))

    assert zh_name("global_etf_confidence_vol_gate") == "全球 ETF 置信波动门控"
    assert en_name("global_etf_confidence_vol_gate") == "Global ETF Confidence Vol Gate"
    assert zh_name("mega_cap_leader_rotation_top50_balanced") == "Mega Cap Top50 平衡龙头轮动"
    assert en_name("mega_cap_leader_rotation_top50_balanced") == "Mega Cap Leader Rotation Top50 Balanced"
    assert zh_name("nasdaq_sp500_smart_dca") == "纳斯达克 / 标普智能定投"
    assert en_name("nasdaq_sp500_smart_dca") == "Nasdaq/S&P 500 Smart DCA"
    assert zh_name("hk_listed_global_etf_rotation") == "港股上市全球 ETF 轮动"
    assert en_name("hk_listed_global_etf_rotation") == "HK-listed Global ETF Rotation"
    assert zh_name("hk_high_dividend_low_vol_trend") == "港股高股息低波趋势"
    assert en_name("hk_high_dividend_low_vol_trend") == "HK High Dividend Low-Volatility Trend"
    assert zh_name("hk_low_vol_dividend_quality") == "港股低波股息质量"
    assert en_name("hk_low_vol_dividend_quality") == "HK Low-Volatility Dividend Quality"


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
