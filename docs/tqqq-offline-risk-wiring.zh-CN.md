# TQQQ 风险政策、资金合同与状态账本的离线接线

`application.tqqq_offline_risk.run_tqqq_offline_risk` 把一个 TQQQ 人工账户合同接入已有 UES 风险与配置草稿链。它只提供 `initialize`、`read`、`persist`、`preflight` 四个动作，不注册进 main、composer、生产策略分派或下单入口。

这是一条明确的离线适配路径。原生产 `_build_runtime_risk_capabilities` 的 SOXL 分派保持原样；本包不把它改成 TQQQ 已获准实盘的路径，也不复制 SOXL 的现金保留比例或退出缓冲。生产 QPK/UES 依赖、部署文件和实际配置保持原版本。

## 复用哪些能力

- 平台原 `build_ibkr_account_facts` 生成冻结快照事实；`LoadedStrategyRuntime._build_capital_base_capabilities` 核账户净清算价值和资金分母。现金仍由原适配器区分余额、已结算和可支用金额。
- UES 原现金、费用、价格、数量与三档资金政策适配器处理人工输入；原 `preview_capital_adaptive_target` 核冻结资金政策，原 Kelly QP 产生候选。不新增求解器，不拟合模型、请求行情或计算历史收益。
- 原 `RiskEngine`、止损/熔断/恢复政策入口处理风险事件；`PortfolioEventJournal` 和 QPK create-only 存储保存状态；原 settings-draft IO 保存、读回并预检完整草稿。
- 单个策略也可调用同一研究候选入口；空策略集仍拒绝。该支持不把防守证券或现金伪装成第二个策略。

## 调用方必须明确提供

1. `runtime`：一个明确 dry-run、cash-only、options disabled 的人工 `LoadedStrategyRuntime`；profile 固定为 `tqqq_growth_income`，target、账户范围、账户列表、有效执行现金保留比例必须一致。非零固定现金保留额目前未在这条薄适配器中表示，交缺口，不忽略。
2. `portfolio_snapshot`：人工原生形状的账户快照。`packet.facts` 必须与当前平台生产函数的投影完全相同；原生期权库存必须明确为空，缺失或非空都不能被 stocks-only 投影隐去。现金证据中每个账户/币种/标签还须与该快照的原生 cash_balances 对齐，尤其单独核 `SettledCash`，不能由 `CashBalance` 代替。
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

## 状态与草稿

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

原事实投影对未知/空库存会省略持仓字段，本接口保留这一语义，不能自动填成已知空仓。真实全现金场景若缺完整库存合同，目前继续返回缺口。费用也维持原支持范围：明确固定非负 BPS、全部成本包含、无最低佣金；不兼容的实际券商费用须补模型和适用证据，不能降级为该人工费率。

## 验证与本包终点

独立 CI job `tqqq-offline-risk` 只装固定研究 overlay 和固定人工 fixture 源；实际版本不同、依赖缺失、测试跳过都失败。默认 CI 另验证生产入口未注册该适配器。没有云身份、Secret、券商、部署或真实数据步骤。完整 setup 与精确 SHA 见 `.github/workflows/ci.yml`。

离线验收命令：

```bash
IBKR_REQUIRE_TQQQ_OFFLINE=1 IBKR_OFFLINE_UES_SOURCE=/path/to/frozen/ues \
PYTHONPATH=. PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv-offline/bin/python -m pytest -q \
  tests/offline/test_tqqq_risk_wiring.py
```

测试仅用人工资金与场景，覆盖三档两模式、账户/版本/政策错配、未结算现金冲突、费用数量缺项、未知 pending、初始化身份、空链、OPEN、重放、陈旧草稿和独立进程读回。fixture 中的所有数值只用于证明程序行为，均不是账户预算或建议阈值。

本包到“离线 TQQQ 接线和拒绝路径可验证，缺项不授予配置权限”结束。仍需真实账户有效政策与批准来源、资金/费用/数量适用证据、可信初态/checkpoint 发行方、实际存储资格，以及未来获准消费者与生效读回；这些不由本次合成验证替代。B5 的原不采用决定保持。
