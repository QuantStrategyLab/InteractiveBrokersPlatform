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

## 已知受保护来源的同报告核查

原 `primary-account-facts-readonly.yml` 手动入口增加显式
`--broker-contract-source-evidence` 模式。账户与报告前缀仍由原 Secrets 注入，main 和认证前 TQQQ 范围检查、原 WIF 身份及权限不变。只读取同一次定位的既有 runtime report 和原服务身份元数据；不增加 Secret 取值、Flex 请求、券商连接、权限探针、账本目录扫描、发布或上传原始 artifact。

`broker_contract_source_evidence` 仅输出当前生产者确实保存的固定路径形状、现金来源标签及允许的负面类别。`SOURCE_INSPECTED_CONTRACT_GAPS` 表示同一份来源报告已检查、金融合同仍有缺口；进程退出 0 只表示读取诊断成功。原读取/身份/时效/版本条件失败时输出 `NOT_EVALUATED`，不能解释为字段缺失或账户事实不存在。默认无参数诊断保持原行为。

该模式只补只读证据接线，不把真实材料改标为人工输入，不替代 `run_tqqq_offline_risk` 的人工来源、完整预算和政策准入。

| 已定位来源 | 证据边界 |
|---|---|
| runtime report 的 `summary.account_facts` | 当前 schema 没有账户收费计划、证券/碎股授权或完整义务清单；现金投影保留的标签单独核对，不从 CashBalance 或 buying power 补 SettledCash。 |
| `diagnostics.failure_category`、`diagnostics.skip_reason` | 已记录的 API 权限准备失败与程序碎股能力阻断可以保留；没有失败记录不等于具体证券交易获准。原 what-if 探针只验证订单 API 路径，不能当作 TQQQ/碎股资格；本包不运行该探针。 |
| `summary.managed_symbols`、运行数量与经济下单设置 | 属于程序配置；不能成为券商授予的证券白名单、数量权限或适用佣金表。 |
| 执行 pending / execution receipt | 属于当次执行及受管标的观察；零订单或 no_action 不代表全账户无挂单和资金义务。 |
| broker reconciliation | `open_orders_match=true` 只表示与基线匹配，不表示零挂单；持久化摘要不包含全范围现金义务金额。 |
| `account_submission.v1` | 同账户、同存储内参与协议的提交仲裁记录；没有现金预留金额，也不涵盖手工、其他客户端或其他存储订单。 |
| execution marker 的 `bounded_complete` | 只表示指定 claims/outcomes 前缀枚举完成，不是全账户资金义务完整性。 |
| Flex importer / ledger | 解析已有期间的费用和佣金不等于核定当前收费计划；本仓未给出可确认的稳定完整 Flex 私有归档位置，不猜 URI 或重新请求报表。 |

若本次已知来源仍缺证明，下一步所需证据限定为：

- 与账户、券商实体和生效期间绑定的实际收费计划及完整费用规则；历史成交佣金或程序默认最低费用不能代替。
- 适用市场、证券与订单方式的交易许可，碎股资格、数量步长及最小订单规则的来源和版本。
- 与账户、币种、截止时间绑定的已结算资金、全部未完成订单/未结算与其他待扣义务、保留额清单，以及完整性和不重复计入证明。

这些是事实证据，不是让用户猜金额。预算、损失底线和三档曲线仍按原草案留空；缺证据不扩大到其他账户、服务、区域或未知归档位置。

## 单次临时 Job 现金读取

原手动工作流可显式选择 `maintenance_job=true`，同时指定
`scripts/observe_maintenance_cash_job.py` 中已核实的维护镜像 source SHA。
默认仍只读既有报告。Job 模式要求 main、首次 dispatch，不接受外部命令、
账户、镜像、项目、服务或预算参数；单次创建、单任务、并行数 1、零重试、
任务最多 300 秒，执行命令不重试。使用前须已获一次计费及该账户读取授权。

控制端先核对维护候选与实际 serving 的完整非镜像配置及账户身份，再从候选
保留原 env、Secret 引用、运行身份、资源和 VPC 配置。只去掉 HTTP 端口、
健康探针与服务并发设置，使用已评审的 Python 入口文本调用原只读连接、
快照和保存接口，不运行策略、不下单、不检查下单权限、不发送业务通知。
创建后的模板读回必须一致，原服务、流量、IAM、Scheduler 必须保持不变。

报告仍经原 writer 保存到原私有前缀的 `maintenance-cash/` 子目录。
身份使用真实 `CLOUD_RUN_JOB`、`CLOUD_RUN_EXECUTION` 及 task 信息；
不伪造 `K_REVISION`，不作为正式服务 publisher 的来源或改写其绑定。
只有确切执行完成、同账户同时间范围的报告保存读回成立，才输出
`OBSERVED_JOB_ONLY`。退出 0 还要求临时 Job 删除读回及原基线检查通过。
创建或执行结果不明时只核查已有资源；清理只针对本次准确身份，活动任务
先取消并确认结束，不能再次执行来猜测成功。原始报告只在保护环境和私有
存储中读取；公开产物仅有固定状态、时间及来源摘要。

已观察现金字段、可支用资金和完整资金义务分别判断。成功读取并不自动证明
所有现金均可分配，也不启用任何策略或风险配置。
[Job YAML](https://docs.cloud.google.com/run/docs/reference/yaml/v1#job)、
[Job 执行身份](https://docs.cloud.google.com/run/docs/container-contract#env-vars)。
