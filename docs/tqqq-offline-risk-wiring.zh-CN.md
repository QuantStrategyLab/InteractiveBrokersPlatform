# TQQQ 风险政策、资金合同与状态账本的离线接线

`application.tqqq_offline_risk.run_tqqq_offline_risk` 把一个 TQQQ 人工账户合同接入已有 UES 风险与配置草稿链。它只提供 `initialize`、`read`、`persist`、`preflight` 四个动作，不注册进 main、composer、生产策略分派或下单入口。

这是一条明确的离线适配路径。原生产 `_build_runtime_risk_capabilities` 的 SOXL 分派保持原样；本包不把它改成 TQQQ 已获准实盘的路径，也不复制 SOXL 的现金保留比例或退出缓冲。生产 QPK/UES 依赖、部署文件和实际配置保持原版本。

## 复用哪些能力

- 平台原 `build_ibkr_account_facts` 生成冻结快照事实；`LoadedStrategyRuntime._build_capital_base_capabilities` 核账户净清算价值和资金分母。现金仍由原适配器区分余额、已结算和可支用金额。
- UES 原现金、费用、价格、数量与三档资金政策适配器处理人工输入；原 `preview_capital_adaptive_target` 核冻结资金政策，原 Kelly QP 产生候选。不新增求解器，不拟合模型、请求行情或计算历史收益。
- 原 `RiskEngine`、止损/熔断/恢复政策入口处理风险事件；`PortfolioEventJournal` 和 QPK create-only 存储保存状态；原 settings-draft IO 保存、读回并预检完整草稿。
- 单个策略也可调用同一研究候选入口；空策略集仍拒绝。该支持不把防守证券或现金伪装成第二个策略。

## 调用方必须明确提供

1. `runtime`：一个明确 dry-run、cash-only、options disabled 的人工 `LoadedStrategyRuntime`；profile 固定为 `tqqq_growth_income`，target、账户范围、账户列表、有效执行现金保留比例必须一致。固定现金保留额须显式提供并与 runtime 一致；资金合同须覆盖 max(比例 × 净值, 固定底线)，保留额由原现金适配器扣除一次。未定值不按零补齐。
2. `portfolio_snapshot`：人工原生形状的账户快照。`packet.facts` 必须与当前平台生产函数的投影完全相同；明确全现金的人工场景可另附 `inventory_evidence`，通过独立完整性证明后使用研究投影，详见[券商事实与接线](tqqq-broker-facts.zh-CN.md)。原生期权库存必须明确为空，缺失或非空都不能被 stocks-only 投影隐去。原生现金证据的账户/币种/标签须与快照的 cash_balances 对齐；已结算金额须另有原生 `SettledCash` 或下文明确绑定的人工 Web ledger 来源，不能由 `CashBalance` 代替。
3. `packet`：原 UES facts/account_contract、cash_evidence、execution_rules、moments、composition、risk_inputs、definition_bytes、request_id、Kelly 数学合同。当前只装配一个 TQQQ 策略及其明确证券组成；不扩为多个平台或其他策略。
4. `event_context`：原三档完整事件政策、风险观察、日初基准、外部流量、高水位、费用后持仓周期，以及如适用的减仓和恢复证据。预算/期限/阈值、曲线或任何必需规则缺失时交 gaps。
5. `journal`、`binding`：明确离线根目录及原 `PortfolioEventJournal`，外部保存的精确 checkpoint；初始化另需完整显式初态证明。身份必须恰为当前账户范围、当前 TQQQ 组合和当前事件政策，不能拿同账户的其他链替代。

全部来源维持原 `synthetic=true` 要求。不得把真实账户数据仅改标志后送入研究入口。实际账户报告已核对来源这一事实，也不自动证明完整持仓、资金义务、结算现金、收费方案或风险政策合格。

### 原生现金字段的保存和只读核验

平台账户快照另外保存可选的`broker_cash_observations`（`ibkr.cash_observations.v1`），
保留原`cash_balances`里分别出现的CashBalance、TotalCashBalance、两个LEDGER余额字段、
SettledCash、AvailableFunds和FullAvailableFunds。原`cash`的选择优先级和执行购买力算法保持原样；
新增字段不把这些金额相加，也不将AvailableFunds或余额重命名为已结算、可支用现金。

附件继承同一个父快照的账户身份和`observed_at`，只写明确币种，排除BASE；零与负数按原值保留，
非法值仅记录固定字段名，缺字段保持缺失。空数组省略，以兼容原运行报告的保存归一化；
字段未出现不代表余额为零、义务为空或查询完整。

正常快照和paper对账快照在原`accountValues()`返回后、浮点转换与同名键覆盖前，另外捕获白名单字段。
原生有效十进制值保留精度，非法原文仅转换为无效标记，不保存其正文；重复的同账户、币种和标签
即使金额相同也记为歧义，不采用最后一条。单字段序列化限4096位，超出时标无效而不截断，
这是报告大小保护，不是资金或风险上限。原执行使用的数值缓存与现金选择保持原样。
旧快照没有逐项捕获附件时，只能从原已转换的`cash_balances`投影，不能恢复被丢弃的非法值、
被覆盖的重复项或已损失的精度。捕获的账户身份与父快照不符时拒绝该账户事实。

正常快照和paper对账快照另将同次`accountValues()`中的`SettledCashByDate`、
`SettledCashByDate-C`、`SettledCashByDate-S`原文保存为可选的
`broker_cash_schedule_observations`（`ibkr.cash_schedule_observations.v1`）。
该附件与七个标量字段分开，继承父快照身份和时间，沿原运行报告保存；不新增查询或存储。
USD等明确币种与BASE分别保留，重复行不覆盖。每条原文限4096个UTF-8字节；
空白、非字符串、无法编码或超长的原文不截断保存，而保留该字段行并省略`raw_value`。
旧快照缺附件时不从数值缓存重建，空附件也不表示余额为零或来源查询已完整。

只读来源诊断对该附件只输出OBSERVED_RAW、RAW_NOT_PRESERVED、AMBIGUOUS_ROWS或MISSING等固定状态；
日期、金额和任意原文不进入诊断输出。原文只能保存在运行报告的受保护存储中，不能复制到公开日志。
此附件不解释日期键、时区、当前或未来项、累计口径及费用/转出净含关系，不选择可支用金额。
它不补齐原生SettledCash，也不改变任何资金、执行或三档研究准入。

三档离线现金预检同时核原数值缓存和存在的逐项捕获来源；两者有冲突、原值无效或同字段重复时，
即使重建了facts使其匹配，也不能保存初态或草稿。旧缓存不能覆盖这份更完整来源的否定证据。

原受保护`--broker-contract-source-evidence`入口在账户、版本和时效检查后核这个附件，
只输出字段的OBSERVED／INVALID／MISSING状态，不输出金额。原字段被另一种现金标签遮住时，
可以准确说明SettledCash是否实际出现；附件时间不符、重复字段、同一原生字段的两份投影冲突时不认证。
未含附件的旧报告仍可读取，新增明细状态为NOT_REPORTED，不据此反推旧原始数据。

这些是已报告字段的观察，不证明原生缓存独立时效、完整结算查询、全部渠道义务或可花现金。
读到SettledCash只关闭“未观察到该标签”一项；费用、数量权限和完整义务仍须独立核实，
`spendable_cash_verified=false`、`complete_cash_obligations_verified=false`及所有执行权限保持原语义。
接口不新增券商请求、凭据读取、配置应用、存储或风险数值。

字段语义参见[IBKR TWS账户字段](https://www.interactivebrokers.com/docs/tws-api/doc/account-portfolio-data/account-updates/account-value-keys)
及[Account Summary](https://interactivebrokers.github.io/tws-api/account_summary.html)。原生字段含义与适用账户合同须分开核对。

`runtime.runtime_settings.trusted_runtime_risk_policy` 在**这条离线入口**中消费原 UES 事件政策形状，必须与 `event_context.policy` 完全一致；它不是把旧 SOXL `RuntimeRiskLimits` 字典改个 profile 就能使用的入口。事件止损/熔断政策与 `risk_inputs` 中的资金预算、三档曲线、原硬约束分别核对，不能相互替代。

离线 target 使用明确的冻结 release 引用：

- `strategy_revision` 等于该入口要求的研究 UES 版本；实际安装的 QPK/UES VCS revision 也必须一致。
- `config_sha256 = digest(runtime.merged_runtime_config)`。
- `risk_policy_sha256 = digest({"event_policy": policy, "funding_inputs_sha256": _hash(packet["risk_inputs"])})`；`digest` / `_hash` 复用原研究函数和其 dataclass 编码。

这里定义的是本离线输入的冻结引用，不签发真实 release、不改变现役 release 摘要口径，也不证明真实政策发行者。配置或资金/事件模型改变须提供新的明确引用；接口不会自己补摘要或更新 checkpoint。

## 资金规则与结算来源

券商账户能力、策略资金规则和实际可支用额是三件事。账户为 Margin 不授予借款权限，
也不自动切换资金规则。原生产 cash-only 路径用原生币种现金限制购买力；
QPK `CashPolicy` 的三字段是 cash_only、reserved_cash_ratio、reserved_cash_floor_usd，
它没有单独的结算规则字段。该旧行为保持原样，不能据此宣称已证明结算期间不会融资。

本研究链继续采用冻结 UES 的
`SETTLED_MINUS_DISJOINT_RESERVATIONS_AND_OBLIGATIONS_NO_SELL_BRIDGE`：
只用已结算现金，扣除不重叠的保留额与其他义务，不加入预计卖出收入，不批准借款或卖出款衔接。
`cash_policy_preview` 明确输出 `cash_rule`、`borrowing_allowed=false`、
`sale_proceeds_bridge_allowed=false` 和 `settled_cash_source_field`。
这是人工合同的规则说明，不是实际账户的无融资认证。

`packet.cash_evidence` 可附 `web_api_ledger`，使人工 Web 来源无需冒充 TWS 原生回调。
它沿用原11字段来源 header，额外要求：

| 字段 | 合同 |
|---|---|
| `read_status` | `RESOLVED`；未知或读取失败拒绝 |
| `endpoint` | 精确 `/portfolio/{accountId}/ledger` 模板，不含真实账户地址 |
| `account_snapshot_sha256` | 原 `digest(packet.facts)`，绑定本次快照 |
| `response` | 官方逐币种响应形状；必须有合同币种行，不从 BASE、其他币种或其他账户补值 |

选定行必须有匹配的 `acctcode`、`currency`，`timestamp` 为 Unix 秒整数且与来源
`observed_at` 相同；来源在冻结截止时刻须已可见、新鲜且有效。
`cashbalance` 须等于本次原生现金事实，`settledcash` 则单独校验。
缺失、null、非法、负值、冲突或错误时间单位都不能生成可支用额。
若原生快照或其逐项捕获附件也提供 SettledCash，该观察仍须有效、唯一且与 Web 来源一致。
来源不能覆盖已知冲突。

适配器仅在内部补入冻结 UES 所需的 `SettledCash` 键，并标记
`source_field=IBKR_WEB_API_PORTFOLIO_LEDGER.settledcash`；完整人工 Web 证据保留在同一 packet。
该内部键不是新增的 TWS 回调，也不回写原快照或调用方输入。未绑定 Web 来源的来源标记不能跳过原生检查。
缺少的合同 settled_cash 由原 UES 现金适配器从该证据派生；显式冲突保留并拒绝，不覆盖。
旧原生路径仍输出 `TWS.SettledCash` 来源标识。公式、研究版本、费用与数量规则均复用原消费者。

Web 来源不证明挂单、待转出和费用义务完整；原 pending_state、obligations_complete、
reservations_disjoint、显式 reservations 和保留政策检查全部保留。
完整来源进入原草稿指纹及来源时间集合；消费时原 UES 递归时效检查也核这份子证据，
改变来源版本不能复用旧草稿，过期不能靠重新读取草稿刷新。

本入口不请求网络、不持有认证、不注册生产消费。所有来源仍须 synthetic=true，
人工就绪不授予配置采用或执行权限。真实 Web 采集需先满足官方 accounts/subaccounts 前置和有效会话，
再按受保护存储流程独立验收，不能将真实响应改标人工后送入本入口。
字段定义见 [IBKR Portfolio Ledger](https://www.interactivebrokers.com/docs/web-api/v1/endpoints/portfolio/portfolio-ledger)。

一次性真实来源采集的准备和操作要求见[Web ledger只读采集](web-ledger-capture.md)。
该独立工具只保存受保护的真实来源投影，不进入本人工研究入口，也不自动取得资金或采用资格。

### 按阶段使用检查

| 阶段 | 必要输入与结果边界 |
|---|---|
| 原状态 `read` | 可信账户/政策身份、检查点和账链；不读取当前 TWS/Web 现金、费用或候选输入；读到 OPEN 仍禁止新增风险 |
| 原基线信号与禁提交诊断 | 沿其自身输入逐阶段检查；本研究链的已结算来源不成为全局查询前置；阶段完成不等于完整资金批准 |
| 本研究 `initialize`、`persist`、`preflight` | 继续完整严格资金与风险准入；保存/预检只产生人工草稿和读回结果 |
| 真实增险或配置采用 | 本入口不提供资格；当前资金、完整相关义务、有效政策和真实消费者另行验收 |

原 `broker_contract_source_evidence` 的“本报告未证明”仅描述该报告证据范围，
不能将一个字段缺失扩成所有阶段不可执行，也不能因较早阶段成功就宣布完整试点通过。

## 复用原现金政策生成保留要求

原手工 reservations 入口保留。调用方还可以给上述四个动作传入 `cash_policy_evidence`，
让同一 QPK `CashPolicy` 从已加载的 runtime 设置生成保留要求，再交给原现金桥：

```text
CashPolicy(settings.reserved_cash_ratio, settings.reserved_cash_floor_usd)
  + config.execution_cash_reserve_ratio
  → max(有效比例 × 原账户净值, 固定底线)
  → 一条原 RESERVE
  → SettledCash − RESERVE − 不重叠的 OTHER
```

该证据复用原人工来源 header：`synthetic`、`account_id`、`scope_ref`、`currency`、
`source`、`source_version`、`observed_at`、`available_at`、`max_age_seconds`、`valid_from`、`valid_until`，另有：

| 字段 | 要求 |
|---|---|
| `read_status` | 明确 `RESOLVED`；未知、读取失败都不能按零处理 |
| `resolved_cash_policy` | 原 `CashPolicy.to_dict()` 三字段，须与已加载设置完全一致 |
| `config_sha256` | 原 `digest(runtime.merged_runtime_config)` |
| `reservation_id` | 调用方明确标识这份现金保留要求；不能靠金额猜同一项 |

按比例模式不需要用户填固定额；这里读取原配置解析后的完整值，包括有明确默认规则的零值，
不把原始缺失、草稿移除覆盖或读取失败自行解析成零。当前金额仅支持原 USD 合同。

packet 可省略待派生的 `account_contract.reserved_cash`；若已提供则必须相符，不覆盖冲突的显式零或旧金额。
已有同 `reservation_id` 的 RESERVE 仅校验并附来源，不追加第二条；不同 ID 的 RESERVE 不能猜成同一要求。
明确不重叠的其他义务保持原样；相应完整性证据缺失仍拒绝。原 packet 不被修改。

完整政策证据随该 RESERVE 进入原草稿 fingerprint、保存、读回和消费时效复核。
重新使用 prepared packet 时，即使省略可选参数，已嵌入的唯一来源也会与当前 runtime 重验；
修改不主导当前金额的固定底线同样使旧草稿失效。无新来源的原手工路径不被标记为已接通配置来源。

`cash_policy_preview` 读回原政策、有效比例、保留金额、SettledCash、其他义务、可花现金、
来源和冻结时间。它明确属于 `FROZEN_INPUT_NOT_SERVING_CONFIG`，不证明线上已消费该配置；
即使人工现金合同完整，缺少风险政策仍不得生成草稿。消费预检失败后，冻结计算说明也不能当成当前采用资格。

三档与智能/高级模式共享该资金计算；高级 `cash_floor_ratio` 仍为原费用后组合约束，不另扣一笔余额。
保留不足不自动借款、转入外部资金或卖出。OPEN 仍由原账本保留，资金到账不解除它。
真实盘中资金流调整指数的来源绑定仍属独立缺口，不能由本入口或期末流量公式反推。

## 状态与草稿

隔离研究依赖包含`ORDER_COMPONENTS_V1`人工订单费用合同，数量舍入后按股、每单、最低/最高佣金
及明确附加费进入同一现金与原风险约束复核。原固定BPS仅供连续候选生成，不宣称非线性费用下最优。
事件清算使用该模型时须显式绑定`PRO_RATA_MARKET_VALUE_SINGLE_ACCOUNT_ORDER`，
按账户每证券一张卖单计算后在持仓周期间分摊，不重复收最低佣金。智能与高级模式走同一保存/预检接口。
预计费用与实际成交佣金分开；券商实体、阶梯/路由、部分成交及费用舍入尚未获证的条款不能冒充完整实盘收费计划。
生产依赖锁及真实运行注册保持原样；相关预览、保存和读回通过不赋予执行或恢复资格。

`read`仅核原可信账户/策略/政策身份、显式检查点、来源时间和完整账链；不消费当前现金、
费用、价格、候选曲线、阈值或现金预览附件。缺少这些当前分配输入仍可读回原OPEN/CLOSED，
但始终`new_risk_prohibited=true`且不返回配置、初始化、恢复或执行资格。缺可信政策身份、
错账户/检查点、坏链仍拒绝，不能从缺项推断CLOSED。原初始化、保存和消费预检保持完整准入。

### 将相对风险比较绑定到同一份离线草稿

`event_context.relative_risk_comparison` 可附显式人工比较输入。它复用上述11字段来源
header，另含 `portfolio_id`、`request_sha256`、`returns`、`binding`、`strategy_context`、
`benchmark_context`、`allowed_excess`、`dynamic_policy`；字段须完整，未指定规则继续为 null，
不补风险数值。`returns` 只含等长的 `dates`、`strategy`、`benchmark` JSON 数组，日期为
ISO 日日期且必须早于冻结截止日，不接受当前未完成日或未来日。

沿用原 `preview_relative_risk` 数学合同，同时绑定原组合ID、精确请求摘要、账户范围、
币种、两侧账户净财富角色；动态政策的档位与 NAV 必须与请求及人工快照一致。两种设置模式
走同一接缝。初始化及纯状态读取不消费该可选附件；保存及预检有附件就验证。

附件还可提供 `account_drawdown_reference`，将调用方预先选定的账户回撤参照绑定到同一草稿。
此对象完整包含 `benchmark_symbol`、`reference_id`、`available_at_utc`、`frozen_at_utc`、
`dates`、`net_returns`、`context`。参照可用不同历史窗口；日期必须为严格递增、不重复的
完整日序列，均早于声明可用时刻的 UTC 日期；可用时刻不晚于冻结时刻，冻结不晚于决策截止。
`strategy_context.decision_cutoff_utc` 须与原账户合同的 `cutoff_utc` 相同。
参照符号须与 `strategy_context.account_risk_benchmark` 的事先选择相同，并绑定同一账户范围、
币种、频率、成本、现金收益与外部资金流口径；其角色为账户风险参照净财富。

也可明确提供 `schema_version="qsl.security_drawdown_reference.v1"`，将上述`net_returns`
替换为`returns`。该类型是独立证券的提供商调整收盘价代理，`context`不含账户scope；
其分红、成本及资金流字段必须按`tqqq-risk-policy-draft.zh-CN.md`的证券口径声明。
源和账户均须`valuation_sampling=DAILY_CLOSE`，币种与日历匹配，账户侧仍为费用后净财富。
策略与基准比较路径也须同时明确`valuation_sampling=DAILY_CLOSE`；只声明一侧、null、盘中
或不同采样均拒绝，不能用日日期标签推定收盘。原账户净财富参照若声明采样，同样与账户
比较路径一致。两侧均未声明的旧预览仍可读算，但输出`valuation_sampling=null`，不升级为
日收盘证据；新增声明进入原完整输入指纹，改变或省略后不能复用旧草稿。
两类来源不通过改标签互换，证券代理不会声称包含账户交易成本或盘中采样。
原保存及预检直接绑定该完整对象，无新存储或生产消费入口；两模式、三档均保持
`enforcement_eligible=false`、`reference_qualified=false`和`relative_risk_enforced=false`。

账户风险参照与策略的无杠杆标的映射分别声明，不自动互相替代。公共接口不预设参照证券、
年份或百分比，不以个人配置产生全局默认。它复用 QPK 计算参照路径的最大回撤，
不接受用单个年度收益代替回撤。动态政策的 `account_max_drawdown_fraction` 可为 null，
由这份冻结参照提供独立上限；同时提供数值上限时取较小者。二者都缺失则拒绝动态计算。
参照不会推导允许超出的幅度、选择活动档位或补齐三档曲线参数。

完整参照和冻结声明随原输入 fingerprint 保存、读回和预检，变更或省略均使旧草稿失效。
`reference_qualified=false`、`model_calibrated=false` 保持不变：调用方声明和内容哈希
不能证明来源真实、完整覆盖、历史可获得性或未来风险；观察到零回撤也不构成零风险保证。

原 UES 草稿和事件 fingerprint 已包含完整 `event_context`，因此直接复用原 journal、
外部 checkpoint 和 artifact receipt，不另建存储或签发第二份回执。以后省略附件、改来源、
改路径或改当前没有主导限额的参数，原消费预检均拒绝复用旧草稿；其原来源有效期和年龄
在消费时继续复核，不延长冻结时间。

`relative_risk_preview` 是从同一冻结输入重新得到的确定性诊断，不重新生成 Kelly 候选。
只有原 artifact 的完整输入指纹和读回通过，才有 `relative_risk_input_bound_to_draft=true`。
这证明输入绑定，不声称比较结果已经作为策略约束执行：`relative_risk_enforced=false`，
模型校准和连续状态核实仍未获证，结果也不证明这条人工财富路径就是所生成目标的实际
费用后财富路径。观察到 WITHIN/EXCEEDS 均不产生真实风险政策、运行准入、配置应用或恢复权限。
该附件不能替代事件政策；原 OPEN、减仓和不自动解除的语义继续保留。

```python
initialized = run_tqqq_offline_risk(request, action="initialize", initialization=proof, **inputs)
# 显式保留 initialized["next_binding"]，不从目录猜最新头部。
saved = run_tqqq_offline_risk(request, action="persist", event_id=event_id, **inputs_with_checkpoint)
# 另存外部 checkpoint 和 saved["artifact_receipt"]。
read = run_tqqq_offline_risk(request, action="preflight",
    expected_artifact_sha256=receipt["artifact_sha256"],
    consumption_cutoff_utc=current_cutoff, **original_inputs_with_current_checkpoint)
```

智能和高级模式都消费原请求合同：`mode=simple|advanced`，三档为 `conservative|balanced|growth`。高级设置仍经过同一原约束；需要确认的字段保持 `REQUIRED`，预检不授予确认。任何输出都没有执行、配置启用或熔断解除权限。

资金政策准入使用原内核对当前 HOLD 账本作有限核对，不生成 Kelly 候选；持仓当前超限可以被保留和读回，不被解释为允许新增风险。实际新事件的候选生成仍只由原 UES 保存链执行一次。初始化和单纯状态读回始终 `new_risk_prohibited=true`；CLOSED 只描述明确读到的历史状态。

新事件按原合同追加并完整读回；重放只恢复状态，不重新计算候选或补发草稿。OPEN 跨重启保留；合格减仓也不清除 OPEN。止损、熔断事件可以成功保存但没有合格草稿，结果分别记录；不会为了恢复草稿回滚已保存状态。读取/预检不创建文件、不更新原冻结时间、不重新生成候选。过期来源、新 OPEN、旧头部、损坏记录或政策错配拒绝旧草稿。

原事实投影对未知/空库存会省略持仓字段。原报告语义保持；只有明确原生空股票/期权集合及来源、范围、摘要、时效均绑定的人工完整性证明，才能通过独立研究投影生成空持仓；消费预检继续核证明时效。真实全现金场景的来源完整性仍缺，不自动填成已知空仓。费用也维持原支持范围：明确固定非负 BPS、全部成本包含、无最低佣金；不兼容的实际券商费用须补模型和适用证据，不能降级为该人工费率。

## 验证与本包终点

独立 CI job `tqqq-offline-risk` 只装固定研究 overlay 和固定人工 fixture 源；实际版本不同、依赖缺失、测试跳过都失败。默认 CI 另验证生产入口未注册该适配器。没有云身份、Secret、券商、部署或真实数据步骤。完整 setup 与精确 SHA 见 `.github/workflows/ci.yml`。

离线验收命令：

```bash
IBKR_REQUIRE_TQQQ_OFFLINE=1 IBKR_OFFLINE_UES_SOURCE=/path/to/frozen/ues \
PYTHONPATH=. PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv-offline/bin/python -m pytest -q \
  tests/offline/test_tqqq_risk_wiring.py
```

测试仅用人工资金与场景，覆盖三档两模式、三种现金模式、策略保留底线、同一保留不重复扣、非主导固定额的指纹和可选入口重验、来源过期/冲突、资金不足、账户/版本/政策错配、未结算现金冲突、费用数量缺项、未知 pending、初始化身份、空链、OPEN、重放、陈旧草稿和独立进程读回。到账用例同步新原生 NAV/余额/SettledCash，显式累计净流入和调整后指数；重复事件不重算候选或重记本金。fixture 中的所有数值只用于证明程序行为，均不是账户预算或建议阈值。

本包到“离线 TQQQ 接线和拒绝路径可验证，缺项不授予配置权限”结束。仍需真实账户有效政策与批准来源、资金/费用/数量适用证据、可信初态/checkpoint 发行方、实际存储资格，以及未来获准消费者与生效读回；这些不由本次合成验证替代。B5 的原不采用决定保持。

资金取舍待填写[风险政策确认草案](tqqq-risk-policy-draft.zh-CN.md)；事实来源与缺口见[券商事实](tqqq-broker-facts.zh-CN.md)。草案不是批准或启用凭据。
