# IBKR serving image 只读安装身份诊断

此通道接续 #632/#634 的 D1 证据。`serving-image-identity-diagnose.yml` 只允许 main 手动运行，固定四个现有 slot；配置预检先于既有 WIF/SA 鉴权。原 publisher、身份 guard、依赖 pin、目标、预算、账户与通知配置不变。

脚本从受保护 service 的实际 `status.traffic` 选择唯一承接完整 100% 流量的 revision。分流、缺失/非法流量、缺 revision 身份或多容器返回 unknown；不会以 latestReady、service template 或零流量 tag 替代 serving。只读取所选 revision；仅使用其明确 pinned image digest，或同 revision 中可核对 repository 的 resolved digest，拒绝 mutable tag 单独作为拉取依据。registry 限于既有 Google Artifact Registry/GCR 域名。pull 后还须核对 Docker RepoDigests；探测后再只读确认 serving 未变。

Docker credential 配置仅在本次 runner 私有临时目录。所有 gcloud/Docker stdout/stderr 捕获，不上传 artifact；失败只打印固定原因。image URI、digest、revision、真实 source/installed commit/version、账户、配置、秘密与异常原文不进入公开诊断。

安装 probe 使用 digest-pinned image，覆盖为 `/usr/local/bin/python -I -S`；network none、read-only、nonroot、drop all capabilities、no-new-privileges、资源限制，无环境/host volume 注入。`-I -S` 关闭 Python startup hooks/.pth/sitecustomize；只通过标准库 `importlib.metadata.distributions` 读取明确 site-packages 路径及 UES stamp，不启动服务、不 import 业务/策略/插件模块、不运行原 entrypoint。此源码验证批只使用 fake cloud/Docker；真实 main 只读执行由主智能体完成。

公开结果仅含固定 schema/status/reason、dependency alias 的 observed/partial/missing/invalid 状态、nullable equality 与当前 workflow 合法 40hex source SHA。固定发行包为 UES、QPK、quant-strategy-plugins；plugin 发行元数据 missing 不代表外部挂载信号不存在。wheel 缺 git commit 或畸形元数据保持 partial/invalid，不能把 stamp 替代真实 direct_url。缺任一发行完整身份时整体 partial/exit1，不冒称已核验全部安装。

UES equality 只对照所选 deployed revision 中已有 `strategy_release.strategy_revision` 与 `runtime_risk_limits.binding.ues_revision`；缺 literal target/期望时保留 null。QPK/plugin 没有既定 expected revision 就只报告已读取元数据状态，不拿本仓 main/lock/workflow SHA 充当批准版本。revision 的 source label 与 image OCI source label 也只比较有效 SHA；缺 label 为 null。`independent_release_approval` 始终 unknown，本通道不替代 owner/CI/发布批准、运行中加载事实或 producer 自然恢复，不自动旋转 broker revision 或中央 source binding。

只读执行命令（最终 PR/CI 合并后）：

```bash
gh workflow run serving-image-identity-diagnose.yml \
  --repo QuantStrategyLab/InteractiveBrokersPlatform --ref main
```

metadata 完整可读取时 exit0 只说明本次 image 证据已取得。读取不可用、身份/来源不完整或漂移保留 unknown/partial 与 exit1；没有 POST、GCS 写入、业务请求、通知、部署、IAM/生产变量变更。
