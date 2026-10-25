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
2. `portfolio_snapshot`：人工原生形状的账户快照。`packet.facts` 必须与当前平台生产函数的投影完全相同；明确全现金的人工场景可另附 `inventory_evidence`，通过独立完整性证明后使用研究投影，详见[券商事实与接线](tqqq-broker-facts.zh-CN.md)。原生期权库存必须明确为空，缺失或非空都不能被 stocks-only 投影隐去。现金证据中每个账户/币种/标签还须与该快照的原生 cash_balances 对齐，尤其单独核 `SettledCash`，不能由 `CashBalance` 代替。
3. `packet`：原 UES facts/account_contract、cash_evidence、execution_rules、moments、composition、risk_inputs、definition_bytes、request_id、Kelly 数学合同。当前只装配一个 TQQQ 策略及其明确证券组成；不扩为多个平台或其他策略。
4. `event_context`：原三档完整事件政策、风险观察、日初基准、外部流量、高水位、费用后持仓周期，以及如适用的减仓和恢复证据。预算/期限/阈值、曲线或任何必需规则缺失时交 gaps。
5. `journal`、`binding`：明确离线根目录及原 `PortfolioEventJournal`，外部保存的精确 checkpoint；初始化另需完整显式初态证明。身份必须恰为当前账户范围、当前 TQQQ 组合和当前事件政策，不能拿同账户的其他链替代。

全部来源维持原 `synthetic=true` 要求。不得把真实账户数据仅改标志后送入研究入口。实际账户报告已核对来源这一事实，也不自动证明完整持仓、资金义务、结算现金、收费方案或风险政策合格。

`runtime.runtime_settings.trusted_runtime_risk_policy` 在**这条离线入口**中消费原 UES 事件政策形状，必须与 `event_context.policy` 完全一致；它不是把旧 SOXL `RuntimeRiskLimits` 字典改个 profile 就能使用的入口。事件止损/熔断政策与 `risk_inputs` 中的资金预算、三档曲线、原硬约束分别核对，不能相互替代。

离线 target 使用明确的冻结 release 引用：

- `strategy_revision` 等于该入口要求的研究 UES 版本；实际安装的 QPK/UES VCS revision 也必须一致。
- `config_sha256 = digest(runtime.merged_runtime_config)`。
- `risk_policy_sha256 = digest({"event_policy": policy, "funding_inputs_sha256": _hash(packet["risk_inputs"])})`；`digest` / `_hash` 复用原研究函数和其 dataclass 编码。

这里定义的是本离线输入的冻结引用，不签发真实 release、不改变现役 release 摘要口径，也不证明真实政策发行者。配置或资金/事件模型改变须提供新的明确引用；接口不会自己补摘要或更新 checkpoint。

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
