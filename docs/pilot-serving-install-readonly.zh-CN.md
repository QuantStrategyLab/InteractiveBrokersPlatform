# 单 TQQQ serving 镜像安装身份只读核验

在原仓库 main 首次运行 `Read IBKR pilot serving install identity`，指定唯一 TQQQ profile 及本次 main 的完整 SHA。沿用既有部署 gate、原 inventory/认证来源和部署 concurrency；不扫描其他 slot，不增加 IAM，也不修改版本声明。

受保护 runner 读取准确唯一 100% serving revision，核对六项账户/部署身份，再按 revision 的确切 immutable digest 拉取镜像。Docker 认证只放 runner 临时目录。沿用原安装身份探针：`-I -S`、无网络、只读、非 root、drop capabilities、禁止提权，无主机卷或运行环境注入，不导入业务包。读取完成后再次确认 serving、流量/tags、账户身份、target 和 digest 稳定。

公开回执只含固定状态、每个包的 metadata 状态、nullable equality 和独立 `ues_identity_verified`。actual serving release 来自真实 serving target；`deployment_inventory_release` 来自受保护部署 inventory；source lock 只来自本次准确源码的 uv.lock，不能当成已批准风险版本。原版本值、镜像地址、服务、账号、绑定、配置和异常不输出或保存。

plugin 缺失或缺少提交 metadata 时整体保留 partial，退出码仍为 1；只要 UES distribution 的提交与 stamp 一致，独立 UES 身份仍可核实。stamp 与 distribution 冲突则明确不可用，不把冲突升级为策略版本合格。读取失败或身份漂移保留 unknown/null，不用零值或主分支版本补造。

默认镜像模式没有 build、push、KEEP、deploy、IAM、Secret 内容、券商、策略或通知操作。镜像安装身份取证不授予交易、风险政策、恢复或发布权限。

可选 `runner_gateway_preflight=true` 与镜像读取互斥，单独核查原平台 GitHub Linux runner 的既有能力。它从准确 serving revision 解析原 ACCOUNT_GROUP 声明；如原配置引用 Secret，仅由同一受保护 runner 的原身份读取该准确配置，沿用现有账户组解析器和 Secret 优先级。账户、配置正文、token 和底层异常不进入日志或 artifact，不把凭据转移到 Gateway VM。

该模式沿用原入口的 backend/zone 回退和 project 默认值。配置含 zone 时读取原 Gateway 的 VM 元数据及 IAP `testIamPermissions`；无 zone、无显式 Gateway project 时保留原直连地址语义，只记录声明，不查 VM/IAP。显式 project 缺 zone 仍按原入口拒绝。账户范围、live 模式、地址与 client ID 等条件分别输出固定布尔值，原值不输出。原配置的 client ID 为零不阻止无券商连接的元数据检查；正 client ID 独立记录，后续隔离连接仍须满足它。

默认检查不打开隧道或券商 socket，不拉镜像，不导入策略或执行运行周期，不创建资源或改变 IAM/配置。返回权限未观察到不等于已实测网络拒绝；条件权限和真实隧道/端口连通性仍需各自验证。`network_path_verified` 和 `runtime_ready` 始终 false，不能据声明或静态权限结果宣称承载已就绪。失败只输出固定阶段和条件，当前来源在结束时再次核对稳定性。

`gateway_tcp_preflight=true` 默认关闭，仅能与 runner 模式一起使用，且与引用摘要模式互斥。原声明必须通过单账户/live 绑定、正 client ID 和端口检查，并采用原直连路线；否则不打开 socket，不尝试其他地址、VM/IAP、身份或端口。它仅对原配置 host/port 解析出的首个地址发起一次 TCP 连接，连接等待上限 5 秒，成功后立即关闭；不发送或读取任何券商协议，不认证、读取账户或调用 SDK。port 沿用账户组优先、`IB_GATEWAY_PORT` 回退和 live/paper 默认值。安全结果只新增 nullable `gateway_tcp_connected`、是否尝试及固定失败类别，不输出地址、端口或底层异常。结果只代表该次地址/端口尝试，仍不代表券商身份、其他解析地址、隔离运行时或完整周期已通过。

同时指定 `configuration_reference_only=true` 时只核来源摘要：要求 ACCOUNT_GROUP 和配置 Secret 名称均为明确字面声明，返回准确 `projects/.../secrets/...` 引用的 SHA256。它不重试已失败的 payload 读取，不取 Secret 内容、VM、token 或 IAP，不执行 IAM。只在云端解析原声明；用于将受保护现役引用与管理客户端已有定位作准确比较，不把引用名或配置正文放入产物。不支持的声明形状保持 unavailable。
