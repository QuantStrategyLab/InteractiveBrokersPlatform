# 单账户试点镜像的零流量发布

本入口只为 TQQQ profile 构建并创建一个候选 revision。它保留原环境、风险政策、资源、网络、IAM、Scheduler 及实际服务流量，不启用交易、不解除熔断，也不调用券商。

在原仓库 `main` 首次运行 `Deploy IBKR pilot without traffic`，输入 `profile=tqqq_growth_income` 和本次 dispatch 的准确 main SHA。既有 `ENABLE_GITHUB_CLOUD_RUN_DEPLOY` 必须已经为 true。入口不接受账户、项目或服务名；这些绑定沿用原部署来源，作为 reusable workflow 的具名 secrets 在受保护 runner 内解析。WIF provider 只从原通用 protected workflow 的唯一固定声明读取，先遮罩再提供给认证步骤，不复制实际映射或新增配置变量。不会要求在 Mac 读取 Secret 内容。

入口精确选择唯一 TQQQ 单账户，核对当前 template 的完整非镜像配置与实际 serving 基线相同，并在构建及 KEEP 保护后再次核对基线。仅 `commit-sha`、`github-run-id` 两个构建标签可与 serving 不同；更新前后仍保持当前 template 的全部标签原样。旧的失败或未投流镜像候选保留，不因其存在或 Service 整体 Ready 受其影响而阻断；实际承接全部流量的 revision 必须 Ready。沿用 Docker build、UES release binding、推送后 immutable digest 和 Artifact Registry KEEP 保护；KEEP 可能补充保留规则，保留原 cleanup 模式，不执行删除。

Cloud Run 仅执行 `services update --image=<immutable image> --no-traffic`。inventory 只核对 service、profile、platform、account_scope、deployment_selector、account_selector 六个身份字段；其他金融期望不会写入。UES guard 使用实际 serving target 的显式 release/binding；无声明时保留原 `not_covered` 合同，不冒充版本已核实。新代码身份由准确 source checkout、镜像 OCI revision 标签、推送后的 RepoDigests 与注册表 digest 关联；镜像替换保留旧 runtime 标签，因此不会把旧 revision 的 commit 标签冒充本次代码身份。

发布前后比较实际 serving revision、解析后的流量和 tags、全部非镜像 template 配置、服务全局配置、IAM 及指向该服务的 Scheduler 配置。新候选必须 Ready、使用准确镜像、没有正流量或新增 tag。普通 env 的值及 Secret 引用都在 runner 内比较，公开产物只有固定状态与代码/镜像身份，不包含配置、金额哈希或原始日志。

template 与 Revision 的配置比较只去除平台派生的 location 标签及 Service 全局 ingress 投影；出现 ingress 投影时必须与 Service 全局值一致，Service 全局值仍完整参与前后比较。只有已知 network-interfaces 注解按严格 JSON 比较，允许对象键顺序和空白不同，不归一化资源名称，也不忽略真实网络或 env 变化。[ingress 层级](https://docs.cloud.google.com/run/docs/securing/ingress)、[Direct VPC 注解合同](https://docs.cloud.google.com/run/docs/configuring/vpc-direct-vpc)

回执保留镜像推送、KEEP 和 Service update 各自的 attempted/completed，以及独立的 readback_verified。变更前先记录 attempted；只有成功确认后才记录 completed。失败时这些进度不会清空，因此 update 后读回失败与 update 结果不明可区分，不能仅凭 source_read_failed 宣称未发生生产写入。

`--no-traffic` 可能把原先指向 LATEST 的流量改为明确绑定原 revision，所以验收比较实际解析结果，不要求路由表达方式相同。[gcloud update 合同](https://docs.cloud.google.com/sdk/gcloud/reference/run/services/update)

非零最小实例数本身不是阻断条件：revision 最小实例取决于流量引用或 tag，服务最小实例按服务流量分配。本入口保留原最小实例字段，并要求候选零流量、无新增 tag。手动固定实例或无法辨认的 scaling 合同需要单独核实，不自动修改。[Cloud Run 最小实例合同](https://docs.cloud.google.com/run/docs/configuring/min-instances)

以下任一情况就结束：部署 gate 未开、非 main 或重跑、source SHA 不符、目标非唯一单账户、inventory 与 serving 不符、template 不是 serving 基线、已有未知漂移、手动或未知 scaling、来源读取不完整、UES/镜像/KEEP 未通过，或 CLI 结果不明。更新命令不重试；不明结果须另行只读核查，不能再次派发来猜测成功。

完成只证明候选镜像准备与隔离读回通过。风险政策有效性、真实资金证据、策略采用、切流量、调度启用和熔断恢复仍须分别成立。
