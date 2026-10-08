# IBKR Activity Flex 本地账本解析

`application.ibkr_flex_ledger` 把一份已经取回的 IBKR **XML Activity Flex 报表**
转换成最小的、保留来源的账本事实。`application.ibkr_flex_source.import_activity_flex_ledger`
把现有一次性 fetch、账户范围复核和解析器串成实际可调用的导入入口。完整账本含真实金额、
账户标识与原生 TWR，只能留在获批云端内存或已有私有存储；不能写入本机日志。

`diagnose_activity_flex_ledger` 是 source 侧报告入口：它调用上面的 importer，然后只返回
`build_flex_ledger_diagnostic` 状态报告，保留状态、方法、期间、币种、缺项和安全 warning
code，不含账户标识、估值、费用、出入金金额或 TWR 数值。此报告投影不等于收益页面已消费
或恢复。它不接入 QRS 余额快照接口、旧 QPK USDT
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

期间收益发布器另用 `account_base_currency` 保留报表自身的三字母基础币种，例如 EUR；
不会使用上述独立 USD 账本的 `currency=None` 重新标成 USD，也不会换算金额。

## 原生期间收益的一次发布

`application.ibkr_period_return.build_ibkr_period_return` 从已复核账户的账本提取
`ibkr_account_period_return.v1`。它只接受唯一受保护预期账户、有效且一致的报表/原生收益
期间、`return_assessment.computable=True`、`native_ibkr_twr` 方法与 `ChangeInNAV.twr`
来源；原生 `percent` 和 assessment 的 `ratio` 必须精确相差 100 倍。缺少原生 TWR 时
不改用净资产差额。投影仅含 QRS 合同字段，账户 ID 留在私有同步请求中。

`scripts/publish_ibkr_flex_period_return.py` 是默认关闭、仅从环境取配置的一次调用入口：

```sh
PYTHONPATH=. python -m scripts.publish_ibkr_flex_period_return
```

由批准云端 Secret/受保护配置注入以下字段，不通过命令行传值：

| 环境变量 | 用途 |
|---|---|
| `IBKR_PERIOD_RETURN_PUBLISH_ENABLED` | 只有精确 `true` 才启用 |
| `IBKR_PERIOD_RETURN_TARGET_ID` / `IBKR_PERIOD_RETURN_SOURCE_BINDING_ID` | 已批准 QRS 目标与来源绑定 |
| `IBKR_PERIOD_RETURN_ACCOUNT_SCOPE` / `IBKR_PERIOD_RETURN_ACCOUNT_KEY` | 已批准私有 scope 与回执预期 key |
| `IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON` | 仅含一个独立批准原生账户的 JSON 数组 |
| `IBKR_FLEX_TOKEN` / `IBKR_FLEX_QUERY_ID` | 原生 XML Activity Flex 来源配置 |
| `IBKR_ACCOUNT_FACTS_SYNC_TOKEN` | 复用已有 IBKR 私有同步令牌；不得等同 Flex 或其他来源/控制令牌 |

目标、绑定、scope、账户与回执 key 必须来自独立受保护配置，不能从导入报表反推授权。
全部配置有效后才调用已有 `import_activity_flex_ledger`：一次 SendRequest、一次 GetStatement，
没有自动重试或等待轮询。未启用或缺配置时不发 Flex HTTP；无合格原生 TWR 时不发同步 POST。

### 生成中与显式领取

IBKR [GetStatement 合同](https://www.interactivebrokers.com/docs/web-api/api-reference/get-statement)
要求 `q` 使用原先 `SendRequest` 返回的 ReferenceCode；
[1019 错误码](https://www.ibkrguides.com/orgportal/performanceandstatements/flex3error.htm)
表示该报表仍在生成，不表示应重新生成请求。source API 已分开：

- `request_activity_flex_report(token=..., query_id=...)` 只发一次 SendRequest，返回
  `FlexReportRequest`；它在批准进程内保存原 token/reference，`repr/str` 不含这些值。
- `collect_activity_flex_xml(request)` 只对这个句柄发一次 GetStatement，不发 SendRequest。
  1019 抛 `FlexReportPending`，其 `.request` 仍是原对象；其他错误保持失败，不自动重试。
- `import_activity_flex_ledger_from_request(request, expected_account_ids=...)` 显式领取一次后
  复用原账户校验及解析器。领取是否获准仍依本次采集授权；该 API 不授予重试权限。

已有 `fetch_activity_flex_xml` / importer 仍保持一次生成、一次领取的调用合同；1019 的安全
异常现在保留原句柄，批准调用者可在同一进程捕获并保留它。不要打印字段、序列化句柄或把它
写入日志、artifact、命令参数。发布 CLI 在 1019 输出固定 `flex_pending`、退出 1，零 POST；
诊断 CLI 输出 `pending/flex_generation_pending`。两者都不等待、不再次领取、不重新生成。

### 手动云端入口与尚未满足条件

`.github/workflows/publish-ibkr-flex-period-return.yml` 提供单账户手动 GitHub Actions 入口，
沿用现有 hosted runner、固定 uv action 与 frozen runtime lock。它只允许本仓 main 首次
attempt，必须同时由维护者设置 `IBKR_PERIOD_RETURN_PUBLISH_ENABLED=true` 变量并勾选
`publish_once`；两者默认关闭。目标及凭据使用上表同名 GitHub Secrets，且仅注入发布步骤。
不需要 GCP ADC/WIF/Gateway 凭据，不改交易任务、Scheduler、部署或权限，也不发通知。
现有 CI 的完整 `pytest tests` 和 Ruff 步骤已自动覆盖新 source/CLI/workflow 回归。

启用前须确认单一目标的独立 target/binding/scope/key/原生账户与专用 Flex token/query
及已有 IBKR sync token 真实对应，凭据与令牌权限符合原采集授权；本地实现未读取或创建这些
真实配置。该 workflow 禁止 rerun。pending 或未知结果时先停车调查，不能另起 dispatch
来替换原请求。当前没有已确认且获批的跨进程私有 reference 保存位置；GHA 进程结束后不能
从安全输出恢复原句柄，因此尚不能用下一次 job 完成 pending 的领取。没有新建 bucket/DB、
reference Secret 或自动调度；若需要跨进程续领，必须先核实已有私有位置及原请求状态。

同步地址固定为
`https://qsl-strategy-switch-console.pigbibi.workers.dev/api/account-facts/period-return/sync`。
发布器只发一次 POST，禁用重定向与环境代理；响应正文最多 8192 字节。只有 HTTP 200 且
ACK 恰好包含 `ok/stored/unchanged/account_key/period/currency/method`，布尔类型正确，
`ok/stored=true`，并且独立预期 key、期间、币种、方法全部精确匹配时才报告成功。
超时、5xx、超长或无效 ACK 返回 `sync_unknown`，立即停止，不自动重发；3xx/4xx 返回
`sync_rejected`。stdout 只输出固定 `status`，不输出账户、收益数值、令牌或原始异常。
`published/unchanged` 退出码为 0，`disabled/configuration_incomplete` 为 2，其余为 1。

此入口只更新每账户最新一个原生期间，不生成每日收益曲线、不触发交易或通知，不新增
存储。本地验证只使用合成 XML 和模拟 HTTP；真实 Flex token、Query 和获批
账户绑定尚未核验，不能据此声称真实收益已恢复。

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
4. 账本估值/现金流齐全后才能供依赖它们的下游计算；本次原生期间发布只消费券商 TWR，
   另须核验上述受保护 QRS 配置与精确 ACK，不自行计算收益。

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
`tests/test_ibkr_period_return.py` 与 `tests/test_publish_ibkr_flex_period_return.py` 补充纯投影
及真实 importer→合成 XML→固定请求→模拟 ACK 链，覆盖 EUR、单位精确匹配、默认关闭、
缺配置零 HTTP、缺原生收益零 POST、错身份/期间/币种回执、未知结果不重发与固定安全输出。
`tests/test_ibkr_flex_source.py` 复现原 1019 普通错误并覆盖 pending 原句柄、安全异常、显式
同 reference 领取、永久错误零重发、账户范围拒绝；`tests/test_ibkr_flex_period_workflow.py`
验证云端入口的默认关闭、main/首 attempt 边界和私有配置来源。
