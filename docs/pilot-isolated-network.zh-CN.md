# 原 Cloud Run 网络的临时隔离预检

## 临时 Job 模式

在同一受保护入口指定 `isolated_job_network_preflight=true`，需要用户明确批准临时 Job 及按量费用。此模式默认关闭，与旧 revision 模式及其清理输入互斥。它使用相同 main/首次 attempt、单账户准入、原 WIF 身份及部署锁；不会修改原服务模板、入口、流量、IAM 或 Scheduler。旧 revision 模式保留，但长服务名可能无法创建任何非空 tag，不应重复尝试该路径。

受保护 runner 从准确现役版本读取原身份、Direct VPC/connector、egress 和已有执行/加密设置，仅带入原 host/port；镜像仍是已批准的原仓库与固定 digest。Job 为 1 CPU/512Mi、1 task、parallelism=1、60 秒、0 重试，不包含应用配置、Secret、报告路径或挂载。标准库命令 `python -I -S -c` 仅解析首个地址、TCP connect 一次（5 秒），关闭连接并输出六个固定状态字段后退出。不发送券商协议，不导入应用或 SDK。

创建前确认无遗留 `ibkr-p4-` Job，使用 `jobs.create?dryRun=all` 验证服务端配置，并重新核对生产基线。只创建和执行一次；写入结果未知时不重发。按准确 Job/Execution 核身份、配置、终止及成功/重试计数，再读取准确 Execution 的唯一固定 schema 日志。完整操作受 15 分钟工作流限制；执行器预留清理时间。失败时仅取消本次已核实的未结束 Execution，确认结束后删除准确本次 Job，使用 GET 404 确认删除；清理身份或权限不获证就停止，不扩 IAM 或切换身份。

执行前后比较正常 serving 镜像和配置、完整服务模板、流量、IAM、Scheduler 和全局设置。网络观察、任务完成、清理和生产基线分别报告；TCP 成功不代表应用隔离或完整策略周期通过。只保留安全状态 artifact，私有配置和原始云响应不下载。

接口依据：[创建 Job 与 dryRun](https://docs.cloud.google.com/run/docs/reference/rest/v1/namespaces.jobs/create)、[执行 Job](https://docs.cloud.google.com/run/docs/reference/rest/v1/namespaces.jobs/run)、[删除 Job](https://docs.cloud.google.com/run/docs/reference/rest/v1/namespaces.jobs/delete)。

## 原 revision 模式

此入口会修改原服务模板并创建临时版本，需要用户对部署和本次按量费用的明确授权。它不是原镜像安装读取器，也不运行策略或完整禁提交周期。

在既有 `Deploy IBKR pilot without traffic` 指定 `isolated_network_preflight=true`。默认关闭；沿用 main/首次 attempt、唯一 TQQQ 身份准入、原 WIF 身份和部署并发锁。先从准确 serving 配置读取原账户组，核单账户/live、正 client ID 和原直连 host/port；未通过时不部署。探针在既有镜像仓库内明确使用已批准的固定原 digest，不随正常服务发布采用新镜像；临时版本移除继承的 build source labels。正常服务即使已更新，也按执行前的实际配置与流量完整保留，不回退它。

复用获批的固定原镜像，仅覆盖容器命令为 Python `-I -S` 的标准库程序。程序不导入应用、策略或 SDK；环境只传原 host/port，不转入账户配置、报告路径或凭据，不写文件或金融状态。原执行身份、网络及服务级设置保留，临时版本 min=0/max=1、并发 1、请求上限 60 秒；仅对临时版本显式设置 `run.googleapis.com/health-check-disabled=false`，与 gcloud 的 `--deploy-health-check` 对应。临时版本附一个本次唯一的 0% 流量 tag，作为平台部署启动检查的路由目标，不发外部 HTTP 请求；保留原 internal ingress，正常流量固定在原版本，原 tags、IAM 与 Scheduler 保持原样。只在运行中允许这一准确版本/tag/零流量行，正流量、其他 tag 或重复行仍拒绝；结束后恢复完整原流量表并移除临时 tag。

部署前分别使用 Cloud Run `dryRun=all` 校验原模板恢复请求和临时模板，实际写入携带准确 resourceVersion；原流量必须已明确绑定 revision，不能依赖动态 latest；否则无法安全确认恢复时的路由，不执行部署。遇拒绝或来源变化不执行部署。不重试结果未知的写入。

标准库程序在监听健康端口前，只对原 host/port 解析出的首个地址 connect 一次，等待上限 5 秒，关闭后向 stdout 写入固定状态/失败类别，随后仅提供 readiness 响应。控制面核准确 revision 的 Ready，而非服务的 latestReady 指针，再按该 revision 和固定 schema 读取日志；观察不唯一时不声称单次通过。没有外部调用、券商协议、认证、账户读取、镜像 build/push 或策略计算。TCP 成功也不证明应用隔离和完整周期通过。

最后先确认仍是本次模板及原生产基线，按原模板/原 revision name 恢复配置与流量，回读原 IAM/Scheduler/serving/configuration；还须确认临时版本已不再是 latestCreated/latestReady，才删除它并以 404 回读确认。Cloud Run 不允许删除最新版本；若平台未释放该关系，保留零流量/零最小实例，明确报告清理未完成，不另建恢复版本。云端 dry-run 接受只是执行前校验；实际恢复和删除各自必须读回。发现其他写者的变化时不覆盖。失败、安全结果和清理状态分别记录；只有基线恢复且临时版本删除获证才正常结束。安全 artifact 不包含资源名、host/port、token、账户或底层异常。

接口依据：[Cloud Run replaceService 和 dryRun](https://docs.cloud.google.com/run/docs/reference/rest/v1/namespaces.services/replaceService)、[零流量与标签](https://docs.cloud.google.com/run/docs/rollouts-rollbacks-traffic-migration)。

前次运行已结束但清理未完成时，可在相同入口同时指定 `isolated_network_preflight=true` 和 `isolated_cleanup_run_id=<前次 run ID>`。该模式不部署、不恢复模板或读取账户组 Secret，只定位该次派生名称，并核准确原镜像、标准库代码摘要、命令、仅 host/port 的环境和 min=0/max=1。只有当前模板、latestCreated/latestReady、全部流量和 tags 均不引用它，且删除前基线再次稳定，才删除这一版本并读取 404。当前模板与其命名的原 revision 是否一致单独报告；它不是保留已证无用临时资源的前置。原执行身份和正常配置不替换。

恢复时比较现役版本的不可变运行配置及镜像、完整模板、流量、IAM、Scheduler 和服务全局设置；不把 revision 的 resourceVersion、条件更新时间及生成的 URL 投影作为业务配置。固定布尔项分别记录这些比较，实际配置或身份变化仍拒绝。若宽范围恢复复核失败，资源清理仍按上面的直接条件独立尝试一次；不重试未知删除结果，也不将资源删除成功写成原配置复核通过。平台回读会省略表示默认启用的 `health-check-disabled=false`；仅在临时探针比较中将这两种表示等价，显式 true 和所有其他差异仍拒绝，生产基线比较不作此转换。部署启动检查见[官方文档](https://docs.cloud.google.com/run/docs/deploying)。
