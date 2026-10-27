# 原 Cloud Run 网络的临时隔离预检

此入口会修改原服务模板并创建临时版本，需要用户对部署和本次按量费用的明确授权。它不是原镜像安装读取器，也不运行策略或完整禁提交周期。

在既有 `Deploy IBKR pilot without traffic` 指定 `isolated_network_preflight=true`。默认关闭；沿用 main/首次 attempt、唯一 TQQQ 身份准入、原 WIF 身份和部署并发锁。先从准确 serving 配置读取原账户组，核单账户/live、正 client ID 和原直连 host/port；未通过或原固定 digest 已变化时不部署。

复用现役 immutable 镜像，仅覆盖容器命令为 Python `-I -S` 的标准库程序。程序不导入应用、策略或 SDK；环境只传原 host/port，不转入账户配置、报告路径或凭据，不写文件或金融状态。原执行身份、网络及服务级设置保留，临时版本 min=0/max=1、并发 1、请求上限 60 秒。不新增 tag 或调用 URL，正常流量固定在原版本，原 tags、IAM 与 Scheduler 不变。

部署前分别使用 Cloud Run `dryRun=all` 校验原模板恢复请求和临时模板，实际写入携带准确 resourceVersion；原流量必须已明确绑定 revision，不能依赖动态 latest；否则无法安全确认恢复时的路由，不执行部署。遇拒绝或来源变化不执行部署。不重试结果未知的写入。

标准库程序在监听健康端口前，只对原 host/port 解析出的首个地址 connect 一次，等待上限 5 秒，关闭后向 stdout 写入固定状态/失败类别，随后仅提供 readiness 响应。控制面按本次准确临时 revision 和固定 schema 读取日志；观察不唯一时不声称单次通过。没有外部调用、券商协议、认证、账户读取、镜像 build/push 或策略计算。TCP 成功也不证明应用隔离和完整周期通过。

最后先确认仍是本次模板及原生产基线，按原模板/原 revision name 恢复配置与流量，回读原 IAM/Scheduler/serving/configuration；还须确认临时版本已不再是 latestCreated/latestReady，才删除它并以 404 回读确认。Cloud Run 不允许删除最新版本；若平台未释放该关系，保留零流量/零最小实例，明确报告清理未完成，不另建恢复版本。云端 dry-run 接受只是执行前校验；实际恢复和删除各自必须读回。发现其他写者的变化时不覆盖。失败、安全结果和清理状态分别记录；只有基线恢复且临时版本删除获证才正常结束。安全 artifact 不包含资源名、host/port、token、账户或底层异常。

接口依据：[Cloud Run replaceService 和 dryRun](https://docs.cloud.google.com/run/docs/reference/rest/v1/namespaces.services/replaceService)、[零流量与标签](https://docs.cloud.google.com/run/docs/rollouts-rollbacks-traffic-migration)。

前次运行已结束但清理未完成时，可在相同入口同时指定 `isolated_network_preflight=true` 和 `isolated_cleanup_run_id=<前次 run ID>`。该模式不部署、不恢复模板或读取账户组 Secret，只定位该次派生名称，并核准确原镜像、标准库代码摘要、命令、仅 host/port 的环境和 min=0/max=1。只有当前模板、latestCreated/latestReady、全部流量和 tags 均不引用它，且删除前基线再次稳定，才删除这一版本并读取 404。当前模板与其命名的原 revision 是否一致单独报告；它不是保留已证无用临时资源的前置。原执行身份和正常配置不替换。
