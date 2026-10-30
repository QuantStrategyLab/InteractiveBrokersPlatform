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

该模式还以 `configured_revision_source` 独立比较原受保护配置中的 revision：只允许同 service 下合法名称的一次 metadata GET；与 serving 相同时复用已读对象。只投影 `equals_serving`、四项 scope equality、固定 reason 及 scope 全部匹配后合法 40hex `commit-sha` 标签 `declared_source_sha`，不输出 revision 标识或账户。缺标签、配置修订不可读、目标不匹配或非法名称保持 null/固定原因，不替换原 expected revision，不改变 serving 的原准入和退出结果。此标签观察不证明构建字节、部署流程或批准；缺 serving 部署 locator 时仍可保留独立配置观察，整体部署 provenance 保持 unknown。

`serving_runtime_declarations` 仅从已读、scope 相等的 serving revision 投影 `RUNTIME_TARGET_ENABLED`、`IBKR_DRY_RUN_ONLY` 的明确 true/false literal，以及 target 内明确的 live/paper `execution_mode` 和布尔 `dry_run_only`。缺失、引用 Secret、重复或无法识别时为 null；不读取 Secret、不新增云请求，也不从 expected 或默认值推断。声明值不是运行时消费或执行资格证明，不改变原 provenance 准入和退出结果。

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

## 所选源码文件的静态镜像比对

默认关闭的 `static_source_only` 与两个 metadata inputs 互斥；默认 report 和两个 metadata 调用/输出保持。它复用原受保护映射、main/preflight/WIF，通过唯一100% serving、exact revision、immutable_image 及四物理身份等式。revision 的合法 `commit-sha` 仅作为候选；固定同repo GitHub API核该commit存在，再读取该准确ref的八个公开文件：main.py、runtime_strategy_adapters/runtime_composer/runtime_dependencies/runtime_broker_adapters/runtime_reporting_adapters、pyproject.toml、uv.lock。不读取main/tag作为替代。

原gcloud身份的access token只在云内存消费；静态registry GET只访问同次镜像exactdigest及其descriptor声明的config/child/layers。客户端禁止重定向；redirect/权限/读取失败即停止，不跟Location、换身份或扩IAM。无Docker、镜像/entrypoint/策略执行、broker、Secret Manager、报告、通知或生产写。manifest/config仅内存；压缩层仅runner私有临时文件，逐个处理并删除，无artifact上传或Mac下载。

按顺序核manifest/child/config/压缩层digest+size以及每层解压diff_id；支持唯一linux/amd64 index及tar/gzip层。只保留所选公开文件、原venv内UES/QPK的METADATA/direct_url，其他tar内容跳过。whiteout/opaque只遮挡lower layer，祖先目录被file/symlink/hardlink覆盖时旧child失效，链接不跟随，不调用extractall。未知/重复路径、unsupported层/type或缺文件保留unknown。UES/QPK仅把安装metadata的VCS声明与候选lock对比，不证明整个依赖包字节或内存加载；plugin不纳入此范围。

固定预算：64层，manifest/config各4MiB，压缩层256MiB，总下载1.5GiB，单层解压1GiB/总4GiB，entries每层100k/总250k，path1024bytes，源码单文件2MiB，METADATA256KiB/direct_url64KiB，所选内容累计32MiB，请求45秒/整体8分钟，临时剩余空间1GiB。超限不自动扩大，不安装zstd或新增依赖。实际registry需要redirect时固定失败，另行确认受限通道。

公开新schema `ibkr.static_source_diagnostic.v1` 仅固定alias的nullable文件等式、scope等式、依赖status/nullable等式、固定reason和稳定性。八文件字节全匹配并末尾serving稳定时输出 `matched_candidate_sha`；它只表示所选文件匹配该候选，不是历史actual SOURCE_COMMIT/full tree证明。`selected_source_sha=null`、loaded source/独立release批准恒unknown。任一文件缺失/不同、依赖未核对或末尾漂移非零。合法workflow source_sha仍只是此诊断版本。

```bash
gh workflow run account-facts-identity-diagnose.yml \
  --repo QuantStrategyLab/InteractiveBrokersPlatform --ref main \
  -f static_source_only=true -f serving_source_metadata_only=false \
  -f registry_source_metadata_only=false
```

仅root在审查/CI/main发布后做单次云只读验收。此比较不旋转producer revision或receiver绑定，也不恢复业务。层语义见 [OCI layer规范](https://github.com/opencontainers/image-spec/blob/main/layer.md)，原身份认证见 [Artifact Registry官方文档](https://docs.cloud.google.com/artifact-registry/docs/docker/authentication)。

redirect失败现在附带固定 `redirect_metadata`：准确请求阶段manifest/config/layer，HTTP301/302/303/307/308，Location存在、HTTPS默认443与query分隔符存在的nullable布尔，以及 `same_registry_origin` / `exact_google_storage_hostname` / `other_or_invalid`。exact Google项只分类 `storage.googleapis.com`，不接受suffix/wildcard。未观察到redirect或阶段/形状未知时保留null。

HTTPError仅在内存读取Location；只分类单值且至多8192 ASCII字符，不读body、error text、其他private headers或输出原URL。新增 `location_single_value` 表示是否恰好一个值，缺失/重复为false，读取或集合形状未知为null；`location_over_limit` 仅在单个字符串时给出是否超过8192，其他情况为null，不公布具体长度或数量。`location_validation_reason` 只输出下表固定值；无已分类redirect时为null。多种缺陷同时存在时，仅返回最先检查失败的原因。

| 固定原因 | 观察含义 |
| --- | --- |
| `header_read_failed` / `header_shape_invalid` | Location读取失败 / 返回集合形状无法判断 |
| `missing` / `multiple_values` / `value_type_invalid` | 无值 / 多于一个值 / 单值不是字符串 |
| `over_limit` / `non_ascii` / `whitespace_or_control` | 超过8192 / 非ASCII / 含空白或control字符 |
| `url_parse_failed` / `port_parse_failed` | URL或hostname解析失败 / port解析失败 |
| `missing_scheme_or_host` / `unsupported_scheme` / `hostname_invalid` | Location 自身缺 scheme 或 host / 非 HTTP(S) / host 形状不满足原分类条件。`missing_scheme_or_host` 不是非法或 relative 的单一证明 |
| `userinfo` / `fragment` | 含userinfo / 非空fragment |
| `accepted_shape` | 通过原形状分类条件；不代表允许访问 |

例如 Location 存在、`other_or_invalid` 且 HTTPS/query 两个布尔为 null，只说明绝对形状分类在赋值前停止。若同时给出 `location_shape` 与两个布尔，才能区分该引用是哪一种缺 scheme/host 的形状，以及它在内存中是否落在同一次请求上。原因表仍保留原 nullable 语义，不输出 URL、长度或 query。

解析成功的单值 Location 额外给出闭集 `location_shape`：`absolute`、`scheme_without_host`、`network_path`、`path_absolute`、`path_relative`、`query_relative`、`fragment_relative`、`empty`。超限、非 ASCII、空白/control、解析失败、重复值或非字符串时形状为 null，不猜测。`missing_scheme_or_host` 覆盖除 `absolute` 以外的这些形状，所以必须和 `location_shape` 一起读。

`same_origin` 与 `accurate_descriptor_path` 只对同一次准确请求 URI 做内存规范化。请求 URI 必须是 https、默认 443、host 符合既有字符类、无 userinfo、无 query/fragment，且 path 非空；否则两个布尔为 null。无 scheme 的引用用标准库 `urljoin` 解析到该请求 URI，并去掉 `.` / `..`。已有 scheme 的引用保留自己的 authority 和 path，不用 `urljoin`，避免 `https:///path` 被补上请求 host。`same_origin` 只比较 https、host 和默认 443；`accurate_descriptor_path` 只表示解析后的 path 与本次请求 path 相等。userinfo 不进入 origin 比较，但 `userinfo` 原因仍拒绝该形状。

两个布尔即使都为 true，也不授予跟跳：`redirect_followed` 与 `authorization_forwarded` 仍为 false，`NoRedirect` 不产生下一请求。相对引用解析后同源时，`destination_class` 仍是 `other_or_invalid`；该字段只描述已通过绝对形状检查的 Location 自身 host。digest、size、预算、不转发认证和末尾 serving 稳定检查不变。分类不证明 host 真实性、对象许可、签名或允许访问。

本分类不能填上仍需授权取证的字段：redirect 停止时不读 layer 字节，`image_content_status` 仍为 unknown；`loaded_source_status` 与 `independent_release_approval` 仍为 unknown；`selected_source_sha` 仍为 null；predecessor 未知。已经记录的诊断只有 `missing_scheme_or_host`，这里不重放那次 registry 响应，也不把新布尔回填成当时的 image、source 或前驱身份。

当前停车保护实际边界：本客户端将registry Authorization放在请求headers中；标准urllib重定向会复制该header到新请求。未经核验的跨origin跳转可能泄露凭据或读取未批准对象，事后digest校验不能撤回这些副作用。HTTPS及host形状分类本身也不足以证明对象属于本次exact digest。

合法registry blob转交并非永久不可读。后续若确认仍是原批准镜像及descriptor对象、目标精确HTTPS origin/443与受限读取通道可信且权限/费用范围不变，可另行实施有界显式读取：跨origin不转发registry Authorization/cookie，签名仅云内存消费；禁止任意host、suffix/wildcard、自动链式跳转，并保留原size/digest/diff_ids、预算与末尾serving稳定检查。相对URL还需在已验证registry基址内解析并绑定准确对象，不能据same-origin访问任意路径。本批只补拒绝原因，不实现这些跳转；新身份/IAM、未确认对象或超出原预算仍须停车。
