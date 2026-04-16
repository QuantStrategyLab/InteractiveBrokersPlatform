"""Telegram notification and i18n helpers for InteractiveBrokersPlatform."""

from __future__ import annotations


I18N = {
    "zh": {
        "rebalance_title": "🔔 【调仓指令】",
        "heartbeat_title": "💓 【心跳检测】",
        "error_title": "🚨 【策略异常】",
        "canary_title": "🐤 【金丝雀检查】",
        "strategy_label": "🧭 策略: {name}",
        "equity": "净值",
        "buying_power": "购买力",
        "empty_positions": "  (空仓)",
        "empty_target_weights": "  (无目标持仓)",
        "target_weights_title": "目标持仓",
        "strategy_profile_detail": "策略={profile}",
        "execution_profile_detail": "执行配置={profile}",
        "regime_detail": "市场阶段={value}",
        "breadth_detail": "宽度={value}",
        "target_stock_detail": "目标股票仓位={value}",
        "realized_stock_detail": "实际股票仓位={value}",
        "safe_haven_target_detail": "目标避险仓位={value}",
        "snapshot_decision_detail": "快照决策={value}",
        "snapshot_as_of_detail": "快照日期={value}",
        "snapshot_age_days_detail": "快照账龄={value}",
        "snapshot_file_ts_detail": "快照文件时间={value}",
        "snapshot_path_detail": "快照路径={value}",
        "config_source_detail": "配置来源={value}",
        "dry_run_snapshot_prices": "🧪 模拟运行估价: 使用快照收盘价 {count}个标的 ({symbols})",
        "target_diff_summary": "调仓变化: {details}",
        "trade_date_detail": "交易日={value}",
        "target_diff": "目标差异 {symbol}: 当前={current} 目标={target} 变化={delta}",
        "pending_orders_detected": "检测到未完成订单: 策略={profile} 标的={symbols}",
        "same_day_fills_detected": "检测到当日成交: 策略={profile} 模式={mode} 标的={symbols} 交易日={trade_date}",
        "same_day_execution_locked": "当日执行锁已存在: 策略={profile} 模式={mode} 交易日={trade_date} 快照日期={snapshot_date} 目标哈希={target_hash} 锁文件={lock_path}",
        "execution_lock_acquired": "已获取执行锁: 模式={mode} 交易日={trade_date} 快照日期={snapshot_date} 锁文件={lock_path}",
        "same_day_execution_locked_notice": "当日已执行过: 模式={mode} | 交易日={trade_date} | 快照日期={snapshot_date}",
        "dry_run_buy_batch": "🧪 模拟买入 {count}个标的: {details}",
        "dry_run_sell_batch": "🧪 模拟卖出 {count}个标的: {details}",
        "submitted_buy_batch": "📈 已提交买单 {count}个标的: {details}",
        "submitted_sell_batch": "📉 已提交卖单 {count}个标的: {details}",
        "filled_buy_batch": "✅ 买单成交 {count}个标的: {details}",
        "filled_sell_batch": "✅ 卖单成交 {count}个标的: {details}",
        "partial_buy_batch": "⚠️ 买单部分成交 {count}个标的: {details}",
        "partial_sell_batch": "⚠️ 卖单部分成交 {count}个标的: {details}",
        "no_equity": "❌ 无净值",
        "signal_label": "信号",
        "market_status_risk_on": "🚀 风险开启（{asset}）",
        "market_status_delever": "🛡️ 降杠杆（{asset}）",
        "signal_risk_on": "SOXL 站上 {window} 日均线，持有 SOXL，交易层风险仓位 {ratio}",
        "signal_delever": "SOXL 跌破 {window} 日均线，切换至 SOXX，交易层风险仓位 {ratio}",
        "no_trades": "✅ 无需调仓",
        "emergency": "🛡️ 金丝雀应急: {n_bad}/4 坏, 全部转入 {safe}",
        "quarterly": "📊 季度调仓: 前 {n} 名轮动",
        "daily_check": "📋 每日检查: 金丝雀正常, 持仓不变",
        "hold": "💎 持仓不变",
        "market_sell": "📉 [市价卖出] {symbol}: {qty}股",
        "limit_buy": "📈 [限价买入] {symbol}: {qty}股 @ ${price}",
        "submitted": "已下发（订单号: {order_id}）",
        "failed": "失败: {reason}",
        "order_filled": "✅ 订单成交 | {symbol} {side} {qty}股 均价 ${price}（订单号: {order_id}）",
        "order_partial": "⚠️ 部分成交 | {symbol} {side} {executed}/{qty}股 均价 ${price}（订单号: {order_id}）",
        "order_rejected": "❌ 订单异常 | {symbol} {side} {qty}股 状态: {status}（订单号: {order_id}）",
        "strategy_name_global_etf_rotation": "全球 ETF 轮动",
        "strategy_name_russell_1000_multi_factor_defensive": "罗素1000多因子",
        "strategy_name_tech_communication_pullback_enhancement": "科技通信回调增强",
        "strategy_name_qqq_tech_enhancement": "科技通信回调增强",
        "strategy_name_mega_cap_leader_rotation_dynamic_top20": "Mega Cap 动态 Top20 龙头轮动",
        "strategy_name_dynamic_mega_leveraged_pullback": "Mega Cap 2x 回调策略",
        "strategy_name_tqqq_growth_income": "TQQQ 增长收益",
        "strategy_name_soxl_soxx_trend_income": "SOXL/SOXX 半导体趋势收益",
    },
    "en": {
        "rebalance_title": "🔔 【Trade Execution Report】",
        "heartbeat_title": "💓 【Heartbeat】",
        "error_title": "🚨 【Strategy Error】",
        "canary_title": "🐤 【Canary Check】",
        "strategy_label": "🧭 Strategy: {name}",
        "equity": "Equity",
        "buying_power": "Buying Power",
        "empty_positions": "  (No positions)",
        "empty_target_weights": "  (No target positions)",
        "target_weights_title": "Target Weights",
        "strategy_profile_detail": "strategy_profile={profile}",
        "execution_profile_detail": "profile={profile}",
        "regime_detail": "regime={value}",
        "breadth_detail": "breadth={value}",
        "target_stock_detail": "target_stock={value}",
        "realized_stock_detail": "realized_stock={value}",
        "safe_haven_target_detail": "safe_haven_target={value}",
        "snapshot_decision_detail": "snapshot_decision={value}",
        "snapshot_as_of_detail": "snapshot_as_of={value}",
        "snapshot_age_days_detail": "snapshot_age_days={value}",
        "snapshot_file_ts_detail": "snapshot_file_ts={value}",
        "snapshot_path_detail": "snapshot_path={value}",
        "config_source_detail": "config_source={value}",
        "dry_run_snapshot_prices": "🧪 dry-run pricing: snapshot close for {count} symbols ({symbols})",
        "target_diff_summary": "Target changes: {details}",
        "trade_date_detail": "trade_date={value}",
        "target_diff": "target_diff {symbol}: current={current} target={target} delta={delta}",
        "pending_orders_detected": "pending_orders_detected profile={profile} symbols={symbols}",
        "same_day_fills_detected": "same_day_fills_detected profile={profile} mode={mode} symbols={symbols} trade_date={trade_date}",
        "same_day_execution_locked": "same_day_execution_locked profile={profile} mode={mode} trade_date={trade_date} snapshot_date={snapshot_date} target_hash={target_hash} lock_path={lock_path}",
        "execution_lock_acquired": "execution_lock_acquired mode={mode} trade_date={trade_date} snapshot_date={snapshot_date} lock_path={lock_path}",
        "same_day_execution_locked_notice": "same-day execution already exists: mode={mode} trade_date={trade_date} snapshot_date={snapshot_date}",
        "dry_run_buy_batch": "🧪 dry-run buys for {count} symbols: {details}",
        "dry_run_sell_batch": "🧪 dry-run sells for {count} symbols: {details}",
        "submitted_buy_batch": "📈 Submitted buy orders for {count} symbols: {details}",
        "submitted_sell_batch": "📉 Submitted sell orders for {count} symbols: {details}",
        "filled_buy_batch": "✅ Filled buy orders for {count} symbols: {details}",
        "filled_sell_batch": "✅ Filled sell orders for {count} symbols: {details}",
        "partial_buy_batch": "⚠️ Partial buy fills for {count} symbols: {details}",
        "partial_sell_batch": "⚠️ Partial sell fills for {count} symbols: {details}",
        "no_equity": "❌ No equity",
        "signal_label": "Signal",
        "market_status_risk_on": "🚀 RISK-ON ({asset})",
        "market_status_delever": "🛡️ DE-LEVER ({asset})",
        "signal_risk_on": "SOXL above {window}d MA, hold SOXL, risk {ratio}",
        "signal_delever": "SOXL below {window}d MA, switch to SOXX, risk {ratio}",
        "no_trades": "✅ No rebalance needed",
        "emergency": "🛡️ Canary Emergency: {n_bad}/4 bad, rotating to {safe}",
        "quarterly": "📊 Quarterly Rebalance: Top {n} rotation",
        "daily_check": "📋 Daily Check: canary OK, holding",
        "hold": "💎 Hold positions",
        "market_sell": "📉 [Market sell] {symbol}: {qty} shares",
        "limit_buy": "📈 [Limit buy] {symbol}: {qty} shares @ ${price}",
        "submitted": "submitted (ID: {order_id})",
        "failed": "failed: {reason}",
        "order_filled": "✅ Filled | {symbol} {side} {qty} shares avg ${price} (ID: {order_id})",
        "order_partial": "⚠️ Partial | {symbol} {side} {executed}/{qty} shares avg ${price} (ID: {order_id})",
        "order_rejected": "❌ Rejected | {symbol} {side} {qty} shares status: {status} (ID: {order_id})",
        "strategy_name_global_etf_rotation": "Global ETF Rotation",
        "strategy_name_russell_1000_multi_factor_defensive": "Russell 1000 Multi-Factor",
        "strategy_name_tech_communication_pullback_enhancement": "Tech/Communication Pullback Enhancement",
        "strategy_name_qqq_tech_enhancement": "Tech/Communication Pullback Enhancement",
        "strategy_name_mega_cap_leader_rotation_dynamic_top20": "Mega Cap Leader Rotation Dynamic Top20",
        "strategy_name_dynamic_mega_leveraged_pullback": "Dynamic Mega Leveraged Pullback",
        "strategy_name_tqqq_growth_income": "TQQQ Growth Income",
        "strategy_name_soxl_soxx_trend_income": "SOXL/SOXX Semiconductor Trend Income",
    },
}


def build_translator(lang):
    def translate(key, **kwargs):
        active_lang = lang if lang in I18N else "en"
        template = I18N[active_lang].get(key, key)
        return template.format(**kwargs) if kwargs else template

    return translate


def build_strategy_display_name(translate_fn):
    def strategy_display_name(profile: str, *, fallback_name: str | None = None) -> str:
        key = f"strategy_name_{str(profile or '').strip()}"
        translated = translate_fn(key)
        if translated != key:
            return translated
        fallback = str(fallback_name or "").strip()
        if fallback:
            return fallback
        return str(profile or "").strip()

    return strategy_display_name


def send_telegram_message(
    message,
    *,
    token,
    chat_id,
    requests_module,
    printer=print,
):
    if not token or not chat_id:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        printer(f"TG:\n{message}", flush=True)
        response = requests_module.post(
            url,
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
        if not 200 <= response.status_code < 300:
            printer(
                f"Telegram send failed with status {response.status_code}: {response.text}",
                flush=True,
            )
    except Exception as exc:
        printer(f"Telegram send failed: {exc}", flush=True)
