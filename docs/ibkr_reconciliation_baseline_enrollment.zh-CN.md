# IBKR 旧实盘基线：审核材料生成

本流程仅适用于已明确进入 `RECONCILE_ONLY` 的冻结基线恢复。
对于仍是 `ACTIVE_LKG`、但 `RUNTIME_TARGET_ENABLED=false` 的目标，
缺少 `IBKR_RECONCILIATION_EXPECTED_DIGESTS_JSON` 本身不是正常 `/run` 的阻断条件，
也不能据此要求 Flex 令牌或人工导出账单。该目标应先核实当前账户、
未决订单与旧执行标记，再按现有禁止提交完整周期和启用条件判断；
不能为了套用本流程而先改写既有基线状态。

对这类停用中的既有实盘目标，可先手动运行 `Inspect Historical IBKR Execution`，
指定目标与旧标记对应的 UTC 日期。它只在云端读取该目标的私有运行报告，输出
日期、周期状态、提交数量和已存在的成交确认类别；不输出订单、账户金额或原始报告。
`submitted/not_observed`、缺失回执或缺失报告仍需进一步核对，摘要不能自动批准恢复。

历史检查从受保护的 `CLOUD_RUN_SERVICE_TARGETS_JSON` 解析服务身份；公开manifest
只提供目标目录与地区，不能把其中的示例服务名当成生产来源。必须恰好匹配一个
绑定一致的live目标，缺配置、重复或绑定不一致时停止读取。
外层入口沿用已有受保护workflow边界，将仓库变量或Secret作为调用方Secret传入，
在runner打印步骤环境前注册原始值；解析出的服务名另外屏蔽，不直接把私有变量放进步骤环境。

已有对账采集的执行账本汇总不依赖券商采样成功，只在所选目标、源码及云端认证
均就绪后执行。因此券商不可连时仍可查看旧claim；券商失败仍保留，账本读取成功
不代表订单已经核清或允许恢复交易。
历史检查也可显式启用 `inspect_execution_ledger`，不调用券商，仅独立输出同账户
有界账本计数；历史报告读取失败不会自动跳过已就绪的账本检查。

`Probe IBKR Live API Permission` 仅允许 `ACTIVE_LKG`、当前交易开关关闭、
`IBKR_FORCE_RUN=false` 的单一 live 目标。它以一次性 Scheduler 身份调用内部
`/live-permission-probe`，在私有云存储先原子认领本次请求，再使用当前实盘
Gateway 配置验证账户和券商 `what-if` 写入口，随后断开连接；不会运行策略或提交订单。
同一请求不重试；原始券商报错在探针期间不写入日志。失败、重复、连接释放失败、
目标配置或运行版本漂移均不得据此启用实盘。成功也只证明这个 API 写入口，
不证明每个标的的交易许可、资金足够或旧订单已结清。

## 当前挂单的只读观察口径

执行前检查与恢复对账共用 `load_read_only_open_trades`。它消费 ib_insync
阻塞式 `reqAllOpenOrders()` 完成后的返回值，覆盖当前 Gateway 关联账户中各客户端的
当时挂单，并保留 `openTrades()` 中尚未收到终态确认的记录；同一个 Trade 对象不重复计入。
不能只发送请求后读取缓存，也不能把缺少接口、空响应类型、读取错误或超时当成空订单簿。
单次请求等待上限为 10 秒，已有更短期限继续生效；读取后恢复原 SDK 超时和错误处理设置。

这是一次时点快照，不订阅其他客户端后续订单，不绑定、撤销或接管订单。
空快照不会自行清除旧的未决记录、执行 claim 或熔断；没有可信终态的缓存可能继续阻断新增，
须由原对账路径核实。它不保证快照后其他客户端不会新增订单，也不证明全部券商在途义务已齐。
同账户其他证券的活动订单在金额及现金净扣口径未证时继续禁止新增买入，不猜扣款；
原来合格的减仓仍受本证券挂单、数量、风控、claim 和提交权限约束。

## 独立券商报表来源（接入中，默认不启用）

`application.ibkr_flex_source` 仅提供 IBKR 官方 Flex Web Service v3 的一次性、只读
XML Activity 报表获取和账户范围校验。它固定访问 IBKR 的 SendRequest/GetStatement，
不跟随响应中的 URL 或 HTTP 重定向；令牌只从受限调用方的既有受保护存储传入，
不写日志、命令参数、公开文件或候选回执。一次生成后若遇到报表尚未就绪，
既有诊断只按同一句柄有限领取，不重新生成请求；其他错误立即停止。
报表原文只在批准的内存或私有存储中处理，不能保存到本机或公开 artifact。

既有账户事实和收益发布链路已经有 Flex Token、Query ID 与受保护账户选择配置。
`Check IBKR Flex Configuration` 只检查这些配置的形状；
`Diagnose IBKR Flex Without Publishing` 的 `configured_account_coverage_only`
只报告固定账户槽、日期和 section 计数。配置存在不代表报表已覆盖恢复需要的资料，
也不代表独立报表已接入 `/reconcile` 或可以启用交易。
`ACTIVE_LKG` 目标的正常运行不依赖这条冻结基线来源链路。

若 `RECONCILE_ONLY` 目标仍缺少必要 section，先在原 Query 所属用户名的
Client Portal 中检查既有 Activity Flex Query，按实际需要补充 Open Positions、
汇率或缺少的字段。保留原 period、XML 格式、日期格式、账户分组及网站收益/现金流
使用的 sections 和字段；现有消费者按需解析 XML，可兼容新增节点。
查询覆盖、账户范围和期间仍须与待核对基线一致，按下表核对字段：

| Section | 需要核对的内容 |
| --- | --- |
| Account Information | 原生账户身份、基础币种与报告期间，用于与已有受保护选择器核对。 |
| [Open Positions](https://www.ibkrguides.com/reportingreference/reportguide/open%20positionsfq.htm) | 报告日期、币种、资产类型、合约标识、数量及乘数；明确 Summary/Lot 层级，避免重复统计。 |
| Conversion Rates | 检查 General Configuration 的 Include Currency Rates；核对来源币种、目标币种、汇率及报告日期。 |
| [Cash Report](https://www.ibkrguides.com/reportingreference/reportguide/cash%20reportfq.htm) | 各币种 From/To Date、Ending Cash 和 Ending Settled Cash；两种余额分别核验。 |
| [Trades](https://www.ibkrguides.com/reportingreference/reportguide/tradesfq.htm) | Executions 明细、原生交易/订单标识、成交及目标结算日期、数量、现金与佣金币种；保留源提供的 Order Reference。 |
| [Cash Transactions](https://www.ibkrguides.com/reportingreference/reportguide/cash%20transactionsfq.htm) | 日期时间、币种、金额与类型，保留源提供的交易标识；未知标识或时间语义不能自行补齐。 |
| [Corporate Actions](https://www.ibkrguides.com/reportingreference/reportguide/corporate%20actionsfq.htm) | 报告/事件日期、合约、币种、类型、数量及原生 Transaction ID，用于解释非交易持仓和现金变化。 |

XML 节点缺失保留 unknown，不能证明 Portal 未勾选该 section，也不能直接解释为零持仓
或零成交；显式空 section 也须核对查询选项和报告覆盖。期末 Settled Cash 不能替代当前
可支用现金或全部挂单义务。调整完成后，先用受保护入口只读检查来源、账户及期间。

只有确实需要不同的 period、分组、格式、账户范围，或外部消费者要求固定模板时，
才考虑专用 Query。当前消费者只有一个 `IBKR_FLEX_QUERY_ID` Secret 槽，专用 Query
需要明确单独接入，不能直接替换网站正在使用的 Query。不要把 Query 的原始值或
访问令牌贴到聊天、PR 或日志。
继续复用已经批准的令牌存储及消费路径；只有实际需要新令牌时，才由所属用户名在门户
生成并存入指定私有 Secret Manager，不因增加报表 section 自动更换凭据。

Query 模板管理需要原所属用户名直接登录 Client Portal，Flex Web Service 的
SendRequest/GetStatement 不能完成模板编辑，参见
[IBKR 门户配置说明](https://www.interactivebrokers.com/docs/web-api/flex-web-service/client-portal-configuration)
和[创建查询说明](https://www.interactivebrokers.com/docs/web-api/flex-web-service/client-portal-configuration/create-a-flex-query)。

Flex 报表与 Gateway 当前状态是两份不同的券商视图，但报表摘要本身仍不是内部订单/现金
账本，也不包含可靠的全部当前挂单。首次需要核对报表覆盖期、账户、期初/期末持仓与
现金、成交、公司行动及内部订单结果，形成不可覆盖的人工确认基线；之后再用增量账本
计算预期。缺任一材料时保持 `RECONCILE_ONLY`/停用，不把 observed 自动复制为 expected。

`scripts/build_reconciliation_baseline_candidate.py` 处理一份或更多私有、已脱敏的
`/reconcile` 回应。调用方同时显式提供已保存的 source records 和独立核验的
`SourceReceiptExpectation`。现有 record/expectation 字段逐项一致、evidence 成员精确对应
后，才调用 QPK 生成既有 source-bound `broker_reconciliation_baseline_candidate.v2`。
收据必须新鲜、账户/runtime 匹配且各账务面已对账；不再要求第二份或固定最小间隔。

该工具不连接 IB Gateway、不访问原始账户资料、不写 Cloud Run 环境变量、不改
`RUNTIME_TARGET_JSON`，更不会下单。它的非零退出码表示候选尚不能进入审计；这不是
故障恢复授权。

模型审查仅为 advisory，省略时真实显示 unavailable/0；已有 rejected/unavailable 不
改写成 approved，不要求多模型票数或伪造 `dual_review_binding_reverified=True`。
人工确认、确认后的新观察、五摘要比较和 CAS/no-order 约束继续保留；本补丁不批准
真实账户接管、live、下单或扩大资金。

## 发布给统一管理站点的最小来源快照

`scripts/publish_reconciliation_recovery_source.py` 接受候选、独立来源文件及可选完整审查。
它重新核验 record/expectation、来源根及 candidate evidence 成员；已有审查仍须绑定同一
候选，并保持人工确认/escalate 边界。票数和模型结论不决定是否可以进入人工确认。

默认行为只是打印 `qsl_reconciliation_recovery_source_snapshot.v1`，其中只有不透明
恢复 ID、平台/策略、候选摘要、采样时间窗、审计绑定与稳定阻断码；不含账号、仓位、
现金、订单、成交明细或五项状态摘要。它不连接 IB Gateway，不修改运行时，也不会下单。

只有显式传入 QRS 的 HTTPS
`/api/internal/sync-reconciliation-recovery-source` 地址，且环境中存在专用的
`RECONCILIATION_RECOVERY_SYNC_TOKEN` 时，脚本才会把上述最小快照发给统一管理站点。
该写入仅供人工确认队列使用，不能恢复 `ACTIVE_LKG`。后续私有控制器仍须重新读取
券商、重验来源及候选绑定，并以原子比较并设置方式转换状态；任一失败都保持
`RECONCILE_ONLY`。

来源发布器还可在显式给出
`gs://.../reconciliation-recovery/ibkr/source/...` 时，把完整候选与可选审查回执写入
私有证据包（缺审查保存 null，不制造审核结果）。来源文件由调用方独立保留，不从
待验证包中自造受信 expectations。该包不发送给 QRS；写入固定使用 GCS `if_generation_match=0`，所以已存在
对象会失败而不会被覆盖、读取或删除。验证器只能在专用私有存储中读取它。

## 私有验证器（暂不写状态）

`scripts/verify_reconciliation_recovery.py` 是恢复链路的第二层。它使用一枚不同于
来源发布令牌的 `RECONCILIATION_RECOVERY_CONTROLLER_TOKEN`，从 QRS 只读取得已确认
条目；然后重新验证本地私有候选、独立指定的 source records/expectations 与可选审查，
要求控制台样本数等于候选真实成员数，读取一份**确认之后**新生成的
`/reconcile` 回执，并核对已部署 `RUNTIME_TARGET_JSON` 仍为同一
`RECONCILE_ONLY` 基线。

该验证器只输出 `ibkr_reconciliation_recovery_verification.v1` 和可能的 QPK 原子切换
计划。即使验证通过，输出也固定 `controller_mode=verify_only`、`no_order=true`、
`execution_authority_granted=false`、`state_write_attempted=false`；它没有 Cloud Run、
券商、执行标记或订单写入代码。下一层单独的最小权限控制器才可消费该计划，并且仍要
在同一目标上比较五项摘要后执行一次精确 CAS。

## 只读状态账本适配层（默认关闭）

为避免把一段旧的 `RUNTIME_TARGET_JSON` 直接写回 GitHub 变量，部署链路可选择读取一份
私有、不可覆盖的状态账本。账本的结构固定为
`ibkr_reconciliation_recovery_state_ledger.v1`，且只含四项：`recovery_id`、
`service_name` 和上述完整 QPK `transition_plan`（另加版本）。它**不**携带下一个运行
目标、账户、策略、仓位、订单或执行权限。

消费时系统从当前完整运行目标推导结果，并逐项验证：服务必须唯一匹配、平台必须为
IBKR、当前状态必须仍是 `RECONCILE_ONLY`、基线 ID 与目标指纹必须等于计划的冻结值，且
QPK 计划中的五项摘要、`no_order=true`、`execution_authority_granted=false` 和 CAS 标志
必须完整存在。验证成功后，唯一允许的差异是
`live_continuity.state: RECONCILE_ONLY -> ACTIVE_LKG`；五项摘要以
`IBKR_RECONCILIATION_EXPECTED_DIGESTS_JSON` 注入运行环境。任何字段缺失、账本重放、目标
漂移、服务不匹配或多服务误匹配都会失败关闭。

工作流只有在仓库变量 `IBKR_RECONCILIATION_RECOVERY_STATE_LEDGER_URI` 显式非空时才会读取
账本；URI 还必须落在专用私有桶的
`reconciliation-recovery/ibkr/state/*.json` 前缀。未设置时不下载账本、不改变同步计划，
既有实盘目标也不受影响。本阶段不写入该变量，也不创建账本对象；因此它只是经过测试的
兼容入口，而不是一次自动或隐式的实盘恢复。

`scripts/publish_reconciliation_recovery_state_ledger.py` 则是与消费端分离的发布适配器。
它只接受刚生成的 `ibkr_reconciliation_recovery_verification.v1`：验证结果必须无阻断、
完整携带 QPK 计划，并且仍声明 `verify_only`、无订单、无执行授权和未尝试状态写入。默认
只打印最小账本 JSON；只有显式提供状态前缀的 GCS URI 时才调用
`if_generation_match=0` 创建对象。该写入不读取、列举、覆盖或删除对象，也不会设置工作流
URI、部署 Cloud Run、连接券商或提交订单。实际启用仍需要单独的高风险控制面动作。

## 故障注入回归

恢复链路的回归测试会主动注入：控制台把不可执行策略篡改为可执行、五项摘要之一
漂移、人工确认过期、确认与新回执同秒、运行目标不再是 `RECONCILE_ONLY`、以及控制器
令牌被试图发送到普通管理站路径。每一种情况都必须抛出拒绝或返回没有
`transition_plan` 的结果；测试同时断言 `state_write_attempted=false`。这让后续接入
最小权限 CAS 时能持续证明“异常只能保持冻结，不能意外恢复实盘”。

## 收集候选收据

手动工作流可选择 `inspect_execution_ledger=true`。该选项在已有 GitHub
云端身份下读取所选服务对应的私有执行标记与 outcome，只在日志输出
`execution_claim.v1`、`execution_marker.v1`、`execution_outcome.v1` 的数量及
缺 outcome 的 claim 数量，不上传原始记录或对象路径。超过 256 条、读取失败或
对象身份不匹配时诊断失败；常规收据采集默认不运行它。旧
`execution_marker.v1` 表示本地记录已写入，单凭这个分类仍不能证明券商成交
或完成账户接管；`execution_claim.v1` 缺 outcome 也只表示需要进一步核对，
不能直接认定券商已收单。

`broker_reconciliation_completed` 的内部日志将预期摘要未配置标为
`comparison_status=not_configured`，并仅记录账户身份核验结果及持仓、现金、
挂单、近期成交的条数。此状态下操作日志不再列出五项无法比较的 `*_mismatch` 原因；原回执及恢复阻断保持不变。
基准存在时，日志用 `matched` 或 `mismatched` 表示五项比较结果；其他恢复阻断仍独立保留。
条数不能证明旧订单已结清。
五项摘要只用于这个恢复候选，不是正常 `/run` 每个周期的比较条件；
`ACTIVE_LKG` 与交易开关是另行判定的状态。
日志不包含账户明细、金额或订单身份。任何新起点接管仍需核实未决订单、
旧 claim 及策略所需状态，不能从本日志自动生成预期摘要或启用交易。

`Collect IBKR Reconciliation Evidence` 是显式手动工作流。由于这些 Cloud Run 服务只接受
内部入口，工作流会以部署身份创建一个名称绑定到本次运行、几分钟后只执行一次的 Cloud
Scheduler 任务，再由既有的最小权限 Scheduler 身份调用冻结服务的 `POST /reconcile`。它随后只从
私有运行报告提取脱敏 `ibkr_reconciliation_candidate.v1`，在 30 天内保留 artifact，并在
成功或失败时删除该一次性任务。它不调用 `/run`、不修改 GitHub 变量、不发布状态账本，也
不发送任何订单。

工作流 artifact 的外层是 `ibkr_reconciliation_artifact.v1`，内部仅保留同一份
`broker_reconciliation` 摘要；`build_reconciliation_baseline_candidate.py` 可直接消费它。
这样基线审核不必下载包含其他运行诊断的完整私有报告。

同一目标的一份完整、可信来源收据即可交给
`build_reconciliation_baseline_candidate.py`，不因凑样本重复触发采集。工作流的成功只说明读取和收据格式正常；
候选仍可能因为未配置预期摘要或账本差异而正确保持阻断。

其中现金摘要只选择结算/账面现金标签（例如 `CashBalance`），不会把随市价变化的
`NetLiquidation` 或保证金可用额计入。这样没有现金、订单或仓位变化的账户不会因为正常
估值波动而被误判为基线漂移；没有可靠现金标签时仍会失败关闭。

## v2 来源根（仅私有候选构造）

`application.broker_reconciliation_candidate` 校验**至少一份**已保存、已脱敏的 source
record。新 builder 直接生成
`broker_reconciliation_baseline_candidate.v2`。每条 record 固定且只允许
`schema_version`、`repository`、`workflow_path`、`workflow_run_id`、
`workflow_run_attempt`、`workflow_head_sha`、`artifact_id`、`artifact_name`、
`artifact_sha256`、`evidence_sha256`、`service_name`、`service_revision`、
`service_revision_commit_sha`、`service_deploy_run_id`。构造器要求来源数量一致、唯一
artifact/run；审计指定的 expectation 还将 candidate profile、`main` 成功 workflow、
artifact 命名，以及相同 repository/workflow/head 与 service/revision/commit 绑定到各条
record。随后将完整 canonical records 的单一根写入 `source_receipts_sha256`；任一缺失、
额外字段或不一致均失败关闭。

该模块是无 I/O 的 private consumer：不查询 GitHub/Cloud Run，不启动 workflow，不接触
broker/account/order，也不读取或写入 expected digest、`ACTIVE_LKG` 或 publisher。在线读取和
持久化来源记录属于后续受控 recorder，不在此范围内。

三个 CLI 均要求 `--source-records <私有JSON列表>` 与
`--source-expectations <独立核验的私有JSON列表>`；发布器和验证器的 `--dual-review` 可省略。
这些参数不触发来源下载或批准；验证器的既有确认读取、发布器的显式发布/存储副作用不变。

来源根只绑定内容，不证明账户身份、查询完整性、账务解释或权限。expectations 必须由
受信调用方独立核验，不能从待验证 records/candidate 自我生成。合成测试只证明这些
consumer 实际接线，不证明任何真实账户安全。历史无来源 v1 只读保留，publisher/verifier
拒绝新申请；旧显式 v1→v2 helper 仍要求完整独立来源校验及成员关联，不用于新单份路径。

## 利用既有对账回执核实账户绑定

在 `Collect IBKR Reconciliation Evidence` 的 `inspect_flex_runtime_binding_only` 模式中，
选一个目标并填入该目标已完成的 `reconciliation_run_id`，可以在受保护 runner 中只读
既有 artifacts，核实**当次**运行账户；留空则只检查现有配置，不读取 artifacts。
工作流读取权限仅用于同仓库 Actions，不发券商请求，也不提交订单。
`evidence.account_scope_sha256` 是运行进程读取的账户集合的摘要：按对账代码将
`account_ids` 去重、排序，构成 `{"account_ids": [...]}`，再使用
`calculate_broker_observation_sha256`。用同一函数计算对应 Flex 槽选择器的摘要，
只输出 `observed_group_account_match`；账号、摘要和原始回执不离开受保护环境。

诊断核对 workflow 来源、成功状态、run/attempt/head、artifact 摘要、目标及调度 job 哈希，
并将来源记录中原工作流日志绑定的 revision 和部署标签与现役 metadata 比较。若 revision、回执来源或
账户选择器不可核验，结果保持 unknown。相等仅证明该次运行的账户集合与该槽一致；
不证明当前 worker 的 Secret 版本、当前账户状态或现金已经对账。不同 revision、
后续进程重新加载配置、运行失败或来源不完整都不能沿用该次结果证明当前绑定。
不要为核对绑定重新触发券商请求，也不要把观测摘要写成批准基准。
