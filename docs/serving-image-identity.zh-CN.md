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

## 精确 serving metadata 与部署来源

`account-facts-identity-diagnose.yml` 新增默认关闭的 `serving_source_metadata_only`。显式为 true 时，使用同一固定四 slot、原保护映射 preflight、main 与既有 WIF，仅读取准确 service 的唯一 100% serving revision，再读取该 revision metadata。原 report 诊断 step 不运行；不列出/读取 GCS 报告、不拉镜像、不运行 Docker、Secret Manager、券商、策略、通知或生产写入。默认 false 仍走原调用与原输出。

该模式复用 `immutable_image` 的 pinned/resolved digest 校验，要求 deployed literal `RUNTIME_TARGET_JSON` 的 service、scope、deployment selector 与原单账户 selector 全部匹配。revision 的合法 `commit-sha` 与数值 `github-run-id` 只用于定位同仓准确的 `sync-cloud-run-env.yml` main 手动成功运行及唯一部署 job；不接受任意 URL、别仓、其他分支/工作流或 skipped step。新 step 仅注入 `github.token`，增加最小 `actions: read`。

精确 job log 只在批准云 runner 内存消费，限既有 helper 的 2 MiB 输出上限，不打印或持久化。必须唯一匹配原 selected-source 校验 group、build/deploy group 和 no-traffic readback group；两处 `SOURCE_COMMIT` 与 revision source label 相等，原 checkout/校验/build/读回步骤均成功，原计划准确目标/物理字段匹配，readback 的实际 `EXPECTED_IMAGE_DIGEST` 与当前 immutable digest 相等。`head_sha` 不替代所选源码；缺失、masked、重复/含糊日志或前驱运行材料不可读均拒绝。最后重读同 service 确认 serving 未变；这是只读稳定性检查，不是生产 CAS 或运行锁。

输出仍使用现有 `ibkr.account_facts_identity_diagnostic.v1` 的显式 `status=metadata_only` 分支，仅含固定 reason、四项 nullable scope equality、image/provenance状态、serving稳定性及已证明的公开 `selected_source_sha`。revision/digest/run ID/私有配置/账户/日志与 token 不输出。`deploy_provenance_verified` / exit0只表示部署来源链被核对；`loaded_source_status` 和 `independent_release_approval` 始终 unknown，不代替正在运行的加载证明、source批准或 producer/receiver绑定修复。

```bash
gh workflow run account-facts-identity-diagnose.yml \
  --repo QuantStrategyLab/InteractiveBrokersPlatform --ref main \
  -f serving_source_metadata_only=true
```

该新读取补齐不同的 metadata 证据，不重跑旧失败的 image/plugin 安装探针。任何 target、流量、digest、来源或读取失败保持非零退出，停止受影响目标，不借最新 ready、tag、run head或旧记录代填。

## 精确 registry provenance 结构观察

同 workflow 提供默认关闭的 `registry_source_metadata_only`，与 `serving_source_metadata_only` 互斥；两个同时为 true 时整个 job 不运行。默认调用和原 report 输出保持，新模式仍使用原四 slot、受保护配置预检、main 与既有 WIF。通过唯一100% serving、exact revision、immutable_image 和四物理身份等式后，只对同次精确 digest 执行一次 `gcloud artifacts docker images describe --show-provenance --show-build-details --format=json`，最后确认 serving 未变。没有旧 job-log、image/build/service list、层下载、Docker、业务或生产写入；新 step 不注入 GitHub token，不增加权限。

公开结构按实际安装 Google Cloud SDK 的 `containeranalysis_util.py` 中 `ArtifactsDescribeView`、`ProvenanceSummary` 与 `BuildSummary` 核对：`provenance_summary.provenance` 和 `build_details_summary.build_details` 均为记录列表。新输出仅这两个列表的存在/结构四个布尔（读取未完成为 null），不会打印记录值、动态字段名、账户、digest、路径或异常。缺 provenance、畸形结构或读取失败非零；build details 可缺失。`registry_provenance_observed`/exit0 仅证明非空记录列表结构可读及 serving 稳定；未验证 subject/source/builder/签名/独立批准。`selected_source_sha=null`，deployment provenance、loaded source 和 independent release approval 始终 unknown。官方命令说明见 [Google Cloud SDK](https://docs.cloud.google.com/sdk/gcloud/reference/artifacts/docker/images/describe)。

```bash
gh workflow run account-facts-identity-diagnose.yml \
  --repo QuantStrategyLab/InteractiveBrokersPlatform --ref main \
  -f registry_source_metadata_only=true -f serving_source_metadata_only=false
```

该模式不重试原 HTTP410 日志入口、不轮换 producer revision 或 receiver binding；权限失败不新增 IAM，也不降级为标签/run head 证明。
