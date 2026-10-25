# TQQQ / IBKR：已有券商事实与适用性边界

核对日期：2026-10-25。源码基线：IBKR `ddbcdd92d5e3247b1a11a38ee2a52be8659aac13`。本次使用已有受保护读取回执、当前源码和 IBKR 官方规则，没有重新请求账户、行情或回测，也没有把私有金融报告下载到本机。

## 已有来源能证明什么

| 来源 | 已知事实 | 仍不能推出 |
|---|---|---|
| [已有单账户只读核验](https://github.com/QuantStrategyLab/InteractiveBrokersPlatform/actions/runs/40119565098) | 当次既有报告的账户、范围和 serving revision 一致，原现金/股票字段结构合格 | 回执时间为 2026-10-24 18:12:34 UTC；当时符合 36 小时检查，不等于本次新鲜账户读取。未证明净值非空、已结算现金、义务完整或收费方案 |
| [`account_facts.py`](../application/account_facts.py) | 净资产仅在原 USD NetLiquidation 来源已核时输出；现金保留原标签优先级；证券范围为 stocks_only | CashBalance、AvailableFunds、SettledCash 不可互换；省略库存不等于已知空仓 |
| [`ibkr_portfolio.py`](../application/ibkr_portfolio.py) | 当前快照保留原账户现金标签和独立期权库存；股票数量与原缓存 portfolio 数量核对 | 缓存枚举成功本身不构成全部资产、pending 义务或可信完整库存证明；实际部署安装态未在本次重核 |
| [`execution_service.py`](../application/execution_service.py) 与 [`strategy_runtime.py`](../strategy_runtime.py) | 有效保留比例取策略与平台较大值，保留金额取“比例 × 权益”和固定现金底线较大值 | 现有默认或 SOXL 配置不能代替此账户批准的预算；此处只是源码算法 |
| [IBKR 账户说明](https://www.interactivebrokers.com/en/accounts/configuring-your-account.php) | 现金账户交易涉及已结算资金要求，须按适用账户类型核实 | 不证明当前账户属于哪种类型或有多少可用现金 |
| [IBKR 美股/ETF 佣金表](https://www.interactivebrokers.com/en/pricing/commissions-stocks.php) | 费用有不同计划，可含最低佣金、每股计费及其他费用 | 不能确定当前账户适用计划；不能一律折成研究样例的固定 BPS |
| [IBKR 碎股说明](https://www.ibkrguides.com/orgportal/trade-in-fractions.htm)、[交易权限说明](https://www.ibkrguides.com/orgportal/tradingpermissions.htm) | 碎股需要启用账户权限；交易权限限定产品与市场 | 股票持仓数量或程序默认步长不证明账户碎股资格、TQQQ 可用性或下单规则 |

## 本包接入的纯工程规则

1. 非零固定现金保留额接入原现金合同：要求有效 runtime 与显式 settings 一致，执行权益须与此股票现金账户的净值分母一致，合同保留额不少于两种底线的较大值；原现金适配器只扣一次保留额，再扣其他明确义务。资金不足、缺值、非有限值、负数及错配均拒绝，不回填预算。
2. 已知全现金的人工离线场景有独立入口：`build_tqqq_offline_account_facts` 保留原事实生产器，在明确人工库存完整性证明合格时才输出空股票集合。证明复用原来源、版本、账户、币种、时效合同，绑定原事实摘要；原生股票和期权列表均须明确为空，账户与证明均声明股票现金范围和完整性。没有该证明时仍保留原省略语义，不修改生产报告。
3. `run_tqqq_offline_risk(..., inventory_evidence=...)` 在初始化、保存、读回和预检均核该证明；预检另按消费时间检查其有效性。原 UES 现金、费用、数量、风险、状态与草稿存储继续复用；没有第二套风控或状态存储。

库存证明仅支持人工测试输入，既不是券商签发证明，也不赋予真实数据改标为人工数据的权限。实际全现金来源完整性仍留缺口。真实非线性费用仍不兼容当前固定 BPS 研究模型，继续报缺口，未新增费用近似。

## 哪些还需要补

- **账户事实**：适用账户类型与收费计划及生效版本、实际证券/碎股权限和数量步长、已结算可支用资金、全部资产与未完成资金义务。这些要查现成受保护账户来源，不能从公开网页或用户猜测补齐。
- **个人政策**：见[简短确认草案](tqqq-risk-policy-draft.zh-CN.md)。资金预算、期限、损失底线及三档映射仍留空。
- **真实运行证据**：可信初态、日初基准、外部资金流、高水位、检查点发行方，以及实际存储资格和未来消费者的生效读回。当前离线状态重启读回已具备；本包没有连接生产账本、注册执行入口或启用配置。

本包到离线接线和拒绝路径验证、政策草案交付及获准的 Git 集成结束。不重开 B5，不新增回测，不解除熔断；原 B5 不采用结论保持。
