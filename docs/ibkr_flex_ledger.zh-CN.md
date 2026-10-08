# IBKR Activity Flex 本地账本解析

`application.ibkr_flex_ledger` 把一份已经取回的 IBKR **XML Activity Flex 报表**
转换成最小的、保留来源的账本事实。`application.ibkr_flex_source.import_activity_flex_ledger`
把现有一次性 fetch、账户范围复核和解析器串成实际可调用的导入入口。完整账本含真实金额、
账户标识与原生 TWR，只能留在获批云端内存或已有私有存储；不能写入本机日志。

`diagnose_activity_flex_ledger` 是 source 侧报告入口：它调用上面的 importer，然后只返回
`build_flex_ledger_diagnostic` 状态报告，保留状态、方法、期间、币种、缺项和安全 warning
code，不含账户标识、估值、费用、出入金金额或 TWR 数值。当前仓库尚无 Flex 专用调度/发布
调用点；此报告投影不等于收益页面已消费或恢复。它不接入 QRS 余额快照接口、旧 QPK USDT
区间合同或任何交易链路。

`scripts/diagnose_ibkr_flex_ledger.py` 是可执行的状态诊断入口。它默认关闭，只有环境变量
`IBKR_FLEX_DIAGNOSTIC_ENABLED=true` 才会尝试一次 fetch。启用时需由获批云端 Secret/受保护
配置注入 `IBKR_FLEX_TOKEN`、`IBKR_FLEX_QUERY_ID` 和
`IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON`；未启用或缺项时不联网，只输出固定缺项状态。异常也只
输出固定错误码，不打印 SDK 异常正文。此任务未配置 Flex Secret/Query/账户范围，也未创建或
调用 workflow；禁止把 Gateway 凭据当 Flex token。诊断脚本 stdout 仅是脱敏状态报告。

## 输入与账户合同

```python
from application.ibkr_flex_source import diagnose_activity_flex_ledger

safe_status = diagnose_activity_flex_ledger(
    token=..., query_id=..., expected_account_ids=("UXXXXXXX",)
)
```

需要在批准云端内存里继续消费账本事实时，调用 `import_activity_flex_ledger` 并只将完整
结果传给受控的私有处理函数，不调用状态报告路径替代收益消费者。此处 TWR 是账户级原生
期间收益，不是策略归因；若页面要显示期间收益，消费者合同必须区分券商原生TWR和本地计算
收益，不能用净值差额或入金代替。

`ledger` 是私有完整账本；需要写入时只能由获批云端调用者使用现有私有报告位置。当前没有
已批准的 Flex 专用持久化目标或云端 workflow，本地实现不会新建存储、打印完整账本或模拟
真实导入。

`build_flex_ledger` 先复用 `ibkr_flex_source.verify_report_accounts` 复核账户范围，
再解析。任何账户缺失、多余、重复或整个报表不含唯一 `FlexStatement` 都会抛
`FlexSourceError`，不返回部分事实。`type` 存在但不是 `AF` 时同样拒绝。

本合同针对**独立 USD 账户**：`currency` 只有在报表自身的基础币种（
`AccountInformation.currency`）就是 USD 时才声明为 `"USD"`，否则为 `None` 并记
`account_base_currency_not_usd` 警告。非 USD 金额只按原币种报告，不做换算、不改写
币种、不与 USD 静默合并。

## 输出事实

返回字典 `schema_version="ibkr_flex_ledger.v1"`：

| 字段 | 含义 | 来源 |
|---|---|---|
| `account_ids` | 唯一账户 | `FlexStatement.accountId` |
| `account_base_currency` / `currency` | 观察币种 / 声明币种 | `AccountInformation.currency` |
| `period` | 期间起止与 `period` 标签 | `FlexStatement.fromDate/toDate/period` |
| `ending_valuation` | 期末原币种估值（仅取报告日=期间结束日的那一行） | `EquitySummaryByReportDateInBase.total` |
| `external_flows` / `net_external_flow` | 外部出入金明细与合计 | `StmtFunds` 中 `DEP`(正) / `WITH`(负) |
| `fee_lines` / `fee_total` | 费用明细与合计 | `StmtFunds` 中 `MFEE/OFEE/FRTAX/STAX/TTAX`，以及交易行上的 `tradeCommission` |
| `fx_rates` | 可用汇率 | `ConversionRate.fromCurrency/toCurrency/rate` |
| `native_returns` | 券商原生 TWR 文本，`unit="percent"`，如存在且期间有效匹配 | `ChangeInNAV.twr` |
| `return_assessment` | 收益是否可给、来源与缺项（`value` 为 `unit="ratio"`） | 本地判定 |
| `warnings` | 逐项数据问题/合同提示 | 本地判定 |

费用只从 `StmtFunds` 这一个账本读取，**不**叠加 `CashReport.commissions` 或
`Trade.ibCommission`，避免同一笔费用被重复计入。`tradeCommission` 只取交易行上
的佣金字段，与费用活动码不相交。

## 期间与收益

期间只有 `fromDate`、`toDate` 都存在且 `from <= to` 时才算有效有序区间。仅在有效区间
且 `ChangeInNAV.fromDate/toDate` 与报表期间完全一致时，才保留原生 TWR：
`native_returns.twr` 是券商原生**百分比**文本，并标 `unit="percent"`（官方文档：
[Change in NAV](https://www.ibkrguides.com/reportingreference/reportguide/changeinnav_fq.htm)，
其 TWR 定义即 “measures the percent return”）。缺开始/结束或逆序时记
`native_twr_period_unavailable`，日期不一致记 `native_twr_period_mismatch`，均不保留原生
收益。

`return_assessment.computable=True` 时，`method="native_ibkr_twr"`，`value` 为把该百分比
用 `Decimal` 除以 100 得到的比例，并标 `unit="ratio"`（例如原生 `twr="1.25"` →
`value="0.0125"`）。否则为 `False`，`missing` 列出具体缺项，例如
`native_twr_unavailable`、`return_method_not_wired`、`ending_valuation_unavailable`、
`external_flows_unavailable`、`external_flows_incomplete`、
`external_flows_mixed_currency`。本模块**不**新建 TWR 算法、不杜撰现金流日期，
也不把净资产涨幅当作收益率。

期末估值只接受报告日等于期间结束日的那一行；期间无效或找不到该行时
`ending_valuation=None` 并记 `ending_valuation_unavailable`，不把其他日期的跨期估值
当作期末。

## 多币种与坏数据

- 外部流/费用只有全部同币种时才给出合计；出现多币种时合计为 `None` 并记
  `external_flows_mixed_currency` / `fees_mixed_currency`，明细仍按原币种保留。
- 金额无法解析为非有限十进制、缺少币种或缺少估值时，对应明细进入 `warnings`；
  外部流一旦有坏行，`net_external_flow` 置 `None` 并记 `external_flows_incomplete`。

## 最小目标账本（下一步真实数据核验条件）

本模块定义的目标产物只是上面这些**事实字段**，不是完整的对账/收益账本。用真实
报表核验前需满足：

1. 在券商门户启用 XML Activity Flex Query，并勾选期末估值（Equity Summary）、
   StmtFunds（含 DEP/WITH 与费用活动码）、ConversionRates；需要原生收益时勾选
   Change in NAV。
2. 令牌只存该目标的私有 Secret Manager，Query ID 走目标配置；报表原文与解析结果
   只在批准云端内存/私有存储处理，不落 Mac。
3. 用真实报表确认：基础币种确为 USD、期间与 `ChangeInNAV` 期间一致、StmtFunds
   活动码覆盖实际出入金、费用无跨区重复、期末估值与 Gateway 视图可解释的差异。
4. 只有在上述事实齐全、且已有下游方法被显式接线时，才由下游计算期间收益；本模块
   不代替该接线。

## 来源与测试

XML 元素/属性名与 `StmtFunds` 活动码（`DEP`/`WITH`/`MFEE`/`OFEE`/`FRTAX`/`STAX`/
`TTAX`）依据 IBKR Reporting Reference 的
[Statement of Funds](https://www.ibkrguides.com/reportingreference/reportguide/statement%20of%20fundsfq.htm)、
[Cash Report](https://www.ibkrguides.com/reportingreference/reportguide/cash%20reportfq.htm)
与 [Change in NAV](https://www.ibkrguides.com/reportingreference/reportguide/changeinnav_fq.htm)；
并用公开解析器 ibflex 的类型定义交叉核对字段名与来源。
`tests/test_ibkr_flex_ledger.py` 仅使用不可关联真实账户的合成占位符 XML，覆盖正负
现金流、费用不重复计入、多币种不合并、坏金额、缺估值、缺期间结束、期间逆序、跨期
估值、期间不匹配、账户归属保护与缺项结果；不验证任何真实账户或真实收益。
