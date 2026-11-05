# InteractiveBrokersPlatform 配置落地：先跑通 `ACCOUNT_GROUP=paper`

这份文档只管当前这一步：**先把 `interactive-brokers-quant-service` 这一个 Cloud Run 服务，用 `ACCOUNT_GROUP=paper` 跑通。**
策略默认值仍然来自 `UsEquityStrategies`，这里仅描述 IBKR 平台壳子、账号组、Secret 和运行目标的落地方式。

不在这一步里做的事：

- 不在这一步里再次改仓库名
- 不在这一步里再次重绑 Cloud Build / Cloud Run 的 GitHub source
- 不按 `ACCOUNT_GROUP` 拆多个 Cloud Run service
- 不引入新的“全局量化仓库共享 secret”

## 1. 当前建议的配置边界

现在要把配置拆成两层：

### Cloud Run / GitHub 管服务级变量

这些变量只描述 IBKR 运行身份和平台覆盖项；策略默认值、策略参数和策略频率继续由 `UsEquityStrategies` 提供。

- `STRATEGY_PROFILE`
- `ACCOUNT_GROUP`
- `IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME`
- `GLOBAL_TELEGRAM_CHAT_ID`
- `NOTIFY_LANG`
- `TELEGRAM_TOKEN_SECRET_NAME`（推荐）
- `TELEGRAM_TOKEN`（fallback）
- `RUNTIME_TARGET_JSON`（由同步 workflow 自动生成，表示结构化运行目标；其中的 `strategy_profile` 只是策略选择器）

可选过渡变量：

- `IB_GATEWAY_ZONE`
- `IB_GATEWAY_IP_MODE`

### Secret Manager 里的 `ibkr-account-groups` 管 broker 运行身份

Gateway 后端至少放这些字段：

- `execution_backend`：默认是 `gateway`，建议显式写出来。
- `ib_gateway_instance_name`
- `ib_gateway_mode`
- `ib_client_id`

建议一起放进去：

- `ib_gateway_zone`
- `ib_gateway_project_id`：仅在 Gateway VM 位于另一个 GCP 项目时填写。
- `ib_gateway_port`：同一台 VM 上跑多个 Gateway 时填写；不填则 live 默认 `4001`，paper 默认 `4002`。
- `ib_gateway_ip_mode`

当前代码里，`execution_backend`、`ib_gateway_instance_name`、`ib_gateway_mode`、`ib_client_id` 已经不再从 Cloud Run env 读了，所以不要继续把它们放在 GitHub Repository Variables 里当主配置源。

如果账号组未来交给 QuantConnect Cloud 管理，可以把 `execution_backend` 设为 `quantconnect`。这时该账号组不再要求 Gateway 主机、端口和 client id，但要在私有配置里提供 QuantConnect 项目、节点、compile id 和 Secret 名。当前平台只识别并保护这个后端，不会把实盘账号配置写入仓库；真正下单仍需 QuantConnect 算法项目消费同一套策略/目标配置。

## 2. `ibkr-account-groups` 怎么配

仓库里已经放了一个可直接改的样例：

- `docs/examples/ibkr-account-groups.paper.json`

先复制一份出来改成你的真实值：

```bash
cd /path/to/InteractiveBrokersPlatform
cp docs/examples/ibkr-account-groups.paper.json /tmp/ibkr-account-groups.json
```

最小推荐结构：

```json
{
  "groups": {
    "paper": {
      "execution_backend": "gateway",
      "ib_gateway_instance_name": "interactive-brokers-quant-instance",
      "ib_gateway_zone": "us-central1-c",
      "ib_gateway_mode": "paper",
      "ib_gateway_port": 4002,
      "ib_gateway_ip_mode": "internal",
      "ib_client_id": 1,
      "service_name": "interactive-brokers-quant-service",
      "account_ids": ["DU1234567"]
    }
  }
}
```

说明：

- `paper`：就是这次先跑通的账号组名，要和 Cloud Run env 里的 `ACCOUNT_GROUP=paper` 一致。
- `execution_backend`：`gateway` 表示继续通过自托管 IB Gateway 连接和下单；`quantconnect` 表示该账号组交给 QuantConnect 路径，当前服务不会误连 Gateway。
- `ib_gateway_instance_name`：GCE 上 IB Gateway 实例名。
- `ib_gateway_zone`：建议现在就配上。当前推荐是按实例名解析内网 IP，这样 Cloud Run 不用手填固定私网 IP。
- `ib_gateway_project_id`：可选，只改变 Gateway VM 的 Compute API 查询项目；不填时沿用 Cloud Run 所在项目。跨项目填写时必须同时提供 `ib_gateway_zone`。查询失败会阻断 Gateway 连接，不会把实例名当成可用地址。
- `ib_gateway_mode`：`paper` 或 `live`。
- `ib_gateway_port`：Cloud Run 连接 Gateway VM 的 host port。多个 IBKR 用户名分别跑 Gateway 时，每个 Gateway 要用不同 port。
- `ib_gateway_ip_mode`：推荐 `internal`。
- `ib_client_id`：这个账号组对应的 client id。
- `service_name`：当前只是预留元数据，建议先填成现有 Cloud Run 服务名，后面多账号拆服务时更顺。
- `account_ids`：实盘账号组建议只放一个 UID。运行时会用它过滤持仓、pending/fill 检查，并写入 IBKR 订单的 `order.account`；如果一个服务配置多个 UID，实盘下单会因为账户路由不明确而失败。

QuantConnect 后端占位结构示例：

```json
{
  "groups": {
    "qc-slot": {
      "execution_backend": "quantconnect",
      "service_name": "interactive-brokers-qc-slot-service",
      "account_ids": ["U00000000"],
      "quantconnect_project_id": 12345678,
      "quantconnect_node_id": "LN-placeholder",
      "quantconnect_compile_id": "compile-placeholder",
      "quantconnect_version_id": "-1",
      "quantconnect_credentials_secret_name": "qc-api-credentials",
      "quantconnect_brokerage_secret_name": "qc-ibkr-slot"
    }
  }
}
```

上面只展示字段形状；真实账号、用户名、密码、API token 和 TOTP 不应进入仓库。

把 secret 建起来：

```bash
PROJECT_ID="your-gcp-project-id"

gcloud secrets describe ibkr-account-groups \
  --project "${PROJECT_ID}" >/dev/null 2>&1 || \
gcloud secrets create ibkr-account-groups \
  --project "${PROJECT_ID}" \
  --replication-policy="automatic"

gcloud secrets versions add ibkr-account-groups \
  --project "${PROJECT_ID}" \
  --data-file=/tmp/ibkr-account-groups.json
```

上传后先看一眼 `paper` 组：

```bash
gcloud secrets versions access latest \
  --project "${PROJECT_ID}" \
  --secret="ibkr-account-groups"
```

## 3. Cloud Run runtime service account 需要哪些权限

只按当前仓库代码看，先给这两个：

### 必需：Secret Manager 读取权限

如果 Cloud Run 运行时通过 `IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME=ibkr-account-groups` 读 Secret Manager，runtime service account 需要：

- `roles/secretmanager.secretAccessor`

建议直接绑到这个 secret 上：

```bash
PROJECT_ID="your-gcp-project-id"
RUNTIME_SA="ibkr-platform-runtime@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud secrets add-iam-policy-binding ibkr-account-groups \
  --project "${PROJECT_ID}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/secretmanager.secretAccessor"
```

### 推荐：Compute Engine 实例查看权限

当前推荐路径是：

- Secret 里放 `ib_gateway_instance_name`
- 同时放 `ib_gateway_zone`
- 运行时通过 Compute API 把实例名解析成 GCE 内网 IP

这种情况下，runtime service account 还需要：

- `roles/compute.viewer`

```bash
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/compute.viewer"
```

若设置了 `ib_gateway_project_id`，须在 **Gateway 所在项目** 向原 Cloud Run runtime service account 授予 Compute 查看权限，至少允许只读 `compute.instances.get`；Secret 读取权限仍按 Secret 所在项目授予。使用 `internal` IP 时，还须单独核实 Cloud Run 到新项目 Gateway 私网的路由与防火墙，跨项目查询到 IP 不代表网络可达。权限、查询或连通性未核实时不要切换运行目标。

如果你后面改成直接配固定私网 host，而且代码不再走实例名解析，这个权限可以再收回。**但按现在这套推荐部署，先给上更稳。**

## 3.1 Cloud Run source deploy 的 staging bucket 权限

如果部署路径是 `gcloud run deploy --source`，或 Cloud Run 绑定 GitHub/source trigger，那么还要检查 staging bucket：

- bucket 一般是 `gs://run-sources-${PROJECT_ID}-${REGION}`
- 至少需要给下面这些身份授予 `roles/storage.objectViewer`
  - build service account
  - deploy service account
  - 默认 compute service account：`${PROJECT_NUMBER}-compute@developer.gserviceaccount.com`

少了这层权限，部署会在“源码上传完成后、Cloud Build 启动前”直接失败，报错类似：

```text
storage.objects.get denied on gs://run-sources-...
```

示例：

```bash
gcloud storage buckets add-iam-policy-binding "gs://run-sources-${PROJECT_ID}-${REGION}" \
  --member="serviceAccount:${BUILD_SA}" \
  --role="roles/storage.objectViewer"

gcloud storage buckets add-iam-policy-binding "gs://run-sources-${PROJECT_ID}-${REGION}" \
  --member="serviceAccount:${DEPLOY_SA}" \
  --role="roles/storage.objectViewer"

gcloud storage buckets add-iam-policy-binding "gs://run-sources-${PROJECT_ID}-${REGION}" \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/storage.objectViewer"
```

### 当前这一步不用先加的权限

- 不需要为了这个仓库再加 Storage 锁相关权限；当前代码已经不靠 `EXECUTION_LOCK_BUCKET` 那套 GCS 锁运行。
- 不需要现在就为“多账号多 service”预留额外 runtime IAM。

## 4. GitHub Repository Variables / Secrets 怎么设

如果继续沿用这个仓库里的 `.github/workflows/sync-cloud-run-env.yml`，建议这样配：

### Repository Variables

必填：

- `ENABLE_GITHUB_ENV_SYNC=true`
- `CLOUD_RUN_REGION`
- `CLOUD_RUN_SERVICE`
- `TELEGRAM_TOKEN_SECRET_NAME=quant-sentinel-telegram-bot-token`（统一 bot「QSL资产管家」合同名，见 QuantRuntimeSettings `docs/notifications-quant-sentinel.zh-CN.md`；旧名 `interactive-brokers-telegram-token` 为遗留，不再作为执行通知入口）
- `STRATEGY_PROFILE=global_etf_rotation`
- `ACCOUNT_GROUP=paper`
- `IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME=ibkr-account-groups`
- `GLOBAL_TELEGRAM_CHAT_ID`
- `NOTIFY_LANG`
- `RUNTIME_TARGET_JSON` 不需要手工填，workflow 会自动算出并同步

可选过渡项：

- `IB_GATEWAY_ZONE`
- `IB_GATEWAY_IP_MODE`

说明：

- 如果 `ibkr-account-groups` 里的 `paper` 已经写了 `ib_gateway_zone` / `ib_gateway_ip_mode`，这两个 GitHub vars 可以留空。
- 现在 workflow 会在它们留空时，顺手把 Cloud Run 上旧的 `IB_GATEWAY_ZONE` / `IB_GATEWAY_IP_MODE` env 清掉，避免双配置源漂移。

### Repository Secrets

- `TELEGRAM_TOKEN`（仅在没设置 `TELEGRAM_TOKEN_SECRET_NAME` 时作为 fallback）

说明：

- GitHub 现在通过 OIDC + Workload Identity Federation 登录 Google Cloud，这个 workflow 不再需要 `GCP_SA_KEY`。
- 如果你现在只用这个 workflow 做“同步已有 Cloud Run service 的 env”，那这个 GitHub Actions 账号只需要能更新目标 Cloud Run service，不需要现在就补 Cloud Build / Artifact Registry 那一套权限。

## 5. 先把 `ACCOUNT_GROUP=paper` 跑通的顺序

建议按这个顺序来：

1. **先定真实值**
   - GCP project id
   - Cloud Run service 名
   - runtime service account 名
   - GCE instance 名 / zone
   - `paper` 还是 `live`
   - `ib_client_id`

2. **更新 Secret Manager**
   - 把 `docs/examples/ibkr-account-groups.paper.json` 改成真实值
   - 上传成 `ibkr-account-groups` 最新版本

3. **给 runtime service account 授权**
   - `roles/secretmanager.secretAccessor`
   - `roles/compute.viewer`

4. **配 GitHub vars / secrets**
   - `ACCOUNT_GROUP=paper`
   - `IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME=ibkr-account-groups`
   - 其他服务级变量按上面补齐

5. **通过受保护的部署 workflow 触发同步或镜像发布**
   - 标准路径是 `Deploy Cloud Run` workflow；`configured` 必须提供精确的 `configured_service`，禁止默认扫全部 inventory。
   - `workflow_dispatch` 必须从 `refs/heads/main` 调用可信 workflow。可选 `source_commit` 仅接受本仓库已存在的完整 40 位 SHA；未传时沿用 workflow SHA。镜像构建使用单独检出的源码，并以实际源码 SHA 标记镜像、revision 与部署读回。workflow 自身仍由 GitHub 的 main ref admission 约束，不更换 WIF provider。
   - 镜像发布与 env 同步默认 `--no-traffic`：只准备候选 revision，不改当前流量。
   - `configured` 镜像发布仅为精确选中的候选 revision 设置 `--min-instances=1`，不修改 service 全局 scaling；单次 deploy 上限 360 秒。切流前最多等待 300 秒，必须同时读到 `Ready=True` 和 `ContainerHealthy=True`；Retired 不代替启动健康。等待期间 revision、commit、image digest、配置、原流量和 latest candidate 漂移，以及查询失败、显式不健康或超时都会停止。单次查询最多 30 秒。
   - 预热实例费用和每次准确目标的重新部署需另行获得 owner 授权。超时不会自动重试、切流或清除 revision minimum；候选最小实例配置的后续处置也需 owner 决定。离线测试不证明云端一定会启动零流量候选。
   - 只更新已批准目标的镜像时，明确设置 `deploy_image=true`、`sync_env=false`；只同步配置则设置 `deploy_image=false`、`sync_env=true`。两项仍受仓库级部署/同步开关约束，且必须指定精确 `configured_service`。这样通知等纯代码发布不会顺带改 Secret、运行开关或 Scheduler。
   - `approve_traffic_shift` / `approve_scheduler_sync` 默认 `false`；显式打开后才切流量或启停 Scheduler，并做读回失败停止。
   - Scheduler 同步时，主执行与预热任务按 `standard_execution_enabled` 启停；独立 precheck 仅在 `drill_precheck_enabled=true` 时启用。主执行开启、drill 关闭时，precheck 保持暂停或被暂停。
   - 流量切换前会验证已部署目标与待发布策略均仍在准入目录。
   - 无交易心跳中的可用现金和账户总权益只来自本轮券商账户快照；后者要求已验证的 broker `NetLiquidation`，不使用策略子集权益代替。缺项分别标为「未核实」，只用于展示，不改变风控和下单口径。
   - 不要把临时的 `gcloud run services update --image ...` 当作常规发布方式：它会绕过策略准入、运行身份与 Paper/Shadow/Live 语义校验。
   - 历史 revision 与镜像默认保留；只有显式设置 `CLOUD_RUN_CLEANUP_ENABLED=true` 才允许自动清理，以保证有可回滚版本。

6. **检查 Cloud Run 当前 env**

```bash
gcloud run services describe interactive-brokers-quant-service \
  --project "${PROJECT_ID}" \
  --region "us-central1" \
  --format="yaml(spec.template.spec.serviceAccountName,spec.template.spec.containers[0].env)"
```

7. **看启动日志**

```bash
gcloud run services logs read interactive-brokers-quant-service \
  --project "${PROJECT_ID}" \
  --region "us-central1" \
  --limit=100
```

如果配置没对齐，当前代码会直接 fail-fast，最常见的报错就是：

- 缺 `STRATEGY_PROFILE`
- 缺 `ACCOUNT_GROUP`
- 缺 `IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME`
- 缺 `RUNTIME_TARGET_JSON`（通常说明 workflow 没跑到或输出没接上）
- secret 里没有 `paper`
- `paper` 组缺 `ib_gateway_instance_name` / `ib_gateway_mode` / `ib_client_id`

## 6. 哪些已经完成，哪些后续再做

### 当前已经完成

- `ibkr-account-groups` secret 建好，并至少把 `paper` 组跑通
- Cloud Run runtime service account 权限补齐
- GitHub env sync 改成只管服务级变量
- 继续保持现有 Cloud Run service、现有 trigger、现有 `ACCOUNT_GROUP=paper` 运行链路稳定

### 还可以后做

- 按 `ACCOUNT_GROUP` 拆多个 Cloud Run service
- 为每个账号组单独定义更细的 service name / 命名规范
- 把更多平台共性提取到后续平台级部署体系里

## Configuration

Set these non-secret repository variables before running the affected workflows. For standalone Python commands, export the same names as environment variables. Workflows reject missing values before cloud access. The Cloud Run deploy preflight also checks the resolved `GCP_*` aliases used by authentication and deployment, in addition to the `IBKR_*` inputs. The signal-prefix allowlist is deployment validation configuration; setting it does not require syncing the service environment.

| Variable | Purpose | Required | Default |
|---|---|---|---|
| `IBKR_GCP_PROJECT_ID` | Project for platform cloud commands | Workflows using the project | none |
| `IBKR_DEPLOY_SERVICE_ACCOUNT` | Existing deploy or read identity | Workflows using this identity | none |
| `IBKR_RUNTIME_SERVICE_ACCOUNT` | Runtime identity | Environment synchronization and drift checks | none |
| `IBKR_SCHEDULER_SERVICE_ACCOUNT` | Scheduler identity | Workflows using Scheduler | none |
| `QSL_CONSOLE_BASE_URL` | HTTPS console origin without an API path | Publisher workflows | `https://example.invalid` for Python scripts |
| `QSL_DIGEST_BUCKET` | Digest and parity evidence bucket | Store and parity workflows | none |
| `STRATEGY_PLUGIN_ALLOWED_SIGNAL_PREFIXES` | Comma-separated permitted GCS signal prefixes | Plugin mount verification | none |

### Private sync bindings

The protected sync workflow accepts the required infrastructure Secret names in
`config/cloud_run_sync_secret_names.json`. The dispatch caller must forward these
same-name repository Secrets; defining their names does not create their values.
Missing bindings stop preflight before authentication. Runtime and Scheduler
identities use the existing `IBKR_RUNTIME_SERVICE_ACCOUNT` and
`IBKR_SCHEDULER_SERVICE_ACCOUNT` Secrets. No Variable fallback is used for these
infrastructure bindings. Every sync plan stays in a mode-0600 runner-private file;
subsequent steps receive its path rather than raw JSON outputs or step headers.

Candidate warmup now requires two fresh healthy revision observations separated by
a five-second poll, followed by a final fresh identity and startup-health check.
The candidate generation must be a positive integer (or its canonical decimal
string), match observedGeneration, and remain unchanged. Missing or inconsistent
generation evidence fails closed. Health-only initial and polling reads use
fixed diagnostics and a maximum 30-second timeout per read. These observations
do not make traffic promotion an atomic cloud compare-and-set operation.

### Exact-target Russell candidate warm-up

Baseline capture and verification bound each service, IAM, Scheduler and revision
read to 30 seconds. A timeout fails with a fixed diagnostic that omits command
arguments and provider output. During candidate warm-up, that failure blocks
traffic promotion and enters the existing tag-removal (when tagged) and
minimum-zero cleanup; it does not retry the read or accept an incomplete baseline.

Immediately before each traffic shift, the lifecycle rereads the exact revision
and rechecks its identity, specification and startup health, including after
baseline validation. Missing, unknown, unhealthy or unreadable revision evidence
blocks promotion and enters the existing cleanup path.

The configured Russell image path uses `scripts/cloud_run_candidate_warmup.py`
for deployment, health qualification, traffic promotion and `finally` cleanup in
one process. Other profiles keep their existing image deployment path. Warm-up
requires one exact admitted configured service, image deployment with environment
sync disabled, and explicit traffic-shift approval. It changes no Scheduler,
environment, Secret, IAM or runtime execution switch.

The candidate is deployed with `--no-traffic --tag=cand-<source-sha8>
--min-instances=1` when the service name plus the candidate tag fits Cloud Run's
46-character combined limit. Longer names omit `--tag` entirely; promotion still
uses the exact qualified revision name, and cleanup skips tag removal. Other
profiles and supported short Russell services retain their existing path. This
is a **revision-level** minimum, never service-level `--min`. Google Cloud's
**Revisions and minimum instances** section says revision minimums start when referenced in a traffic split or assigned a tag; an untagged
zero-percent revision alone does not establish startup. The untagged path retains
`--deploy-health-check` and all existing fresh health and baseline checks; missing
startup evidence stops promotion rather than routing traffic to warm the revision.
One minimum instance is billed during this warm-up window, including idle time. Missing ContainerHealthy
remains a hard denial, with bounded fresh readbacks. Raw condition readbacks
separate absent startup evidence from an observed ContainerHealthy=False probe
failure. Conditions alone do not establish log absence; the lifecycle readback
checks for instance log records when startup is unobserved and Active=False.

**Restoring zero creates another revision.** Google Cloud's **Configure
revision-level minimum instances** and `gcloud run services update
--min-instances` documentation state that this setting is immutable on a
revision. Tag removal does not change traffic percentages or the serving
revision's minimum. The script therefore removes the temporary tag when present,
updates the service template with `--min-instances=0 --no-traffic --deploy-health-check`, and
asserts that the replacement has the same image and container specification,
minimum zero and unchanged traffic. After a successful initial promotion, this
replacement must independently pass strict health and continuity checks before
an explicit **second** guarded traffic migration. The final serving revision is
the minimum-zero replacement. This is two revisions within one workflow run;
acceptance must acknowledge that extra revision and traffic effect before
execution. It is also an M08 deployment prerequisite.

The warm revision remains immutable at configured minimum one. Once untagged and
non-serving it no longer has the tag/traffic reference that provisions its
minimum. Do not claim its revision configuration was reset to zero. A later
rollback to, or tag on, this retained revision would reactivate minimum-one cost;
include that fact in any separately approved rollback. Keep existing artifact
KEEP protection and the old serving revision for rollback; this path deletes
nothing.

Any deploy/health/traffic failure or catchable interrupt enters the same cleanup
path. Before initial promotion, cleanup preserves the old traffic and creates
only the minimum-zero replacement. After promotion, a failed replacement create,
health check, tag removal or readback makes cleanup fail loudly, prints redacted
raw service/revision readbacks and records `cleanup_failed` in the runner-private
result. It never routes to an unqualified revision, silently rolls back, retries
a deployment or reports the template value as a successful serving reset.
Minimum-one billing **may remain on cleanup failure** and needs owner response;
normal success limits it to the warm-up window. SIGKILL, lost runner and provider
outage cannot be guaranteed recoverable by a process-local `finally`. Retained
state blocks automatic replay after restart. Preserve the failed attempt and
reconcile it through the existing authorized operator path rather than rerunning
it. Cloud read/compare guards are not an atomic Cloud Run compare-and-set.

Treat deployment as successful only when all six have actual evidence:

1. Production logs no longer show the `cash_reserve_ratio` TypeError.
2. The final revision emitted a real heartbeat or snapshot; startup alone is insufficient.
3. The final minimum-zero replacement carries 100% of service traffic.
4. The prior revision no longer serves traffic and remains retained under KEEP for rollback.
5. The temporary candidate tag is absent.
6. Service-template and final serving-revision readbacks show minimum instances 0.

These are execution acceptance criteria, not claims established by offline tests.
Official references: [Minimum instances](https://cloud.google.com/run/docs/configuring/min-instances),
[service update flags](https://cloud.google.com/sdk/gcloud/reference/run/services/update),
[traffic tags](https://cloud.google.com/sdk/gcloud/reference/run/services/update-traffic),
and [revision conditions](https://cloud.google.com/run/docs/reference/rest/v2/Condition).

### Protected deployment dry-run

The manual deploy workflow defaults `dry_run` to `true`. The deployment job
selects its GitHub Environment through the repository variable
`IBKR_DEPLOY_ENVIRONMENT`, resolving its WIF credentials from Environment Secrets.
The selector must name an existing owner-protected Environment; an empty selector
fails before authentication. Set it through a separately authorized configuration
change after confirming approval and branch restrictions. Existing Environment approval and
branch restrictions must be configured independently; this workflow does not
create or modify them.

Select one exact configured service and the intended deployment controls. A
dry-run authenticates, resolves the admitted target and validates the deployment
inputs, but skips image builds, revision preparation, cloud writes, traffic,
Scheduler operations and runtime-switch result persistence. It is plan validation,
not evidence that a cloud write will succeed or that a serving revision adopted
the candidate. Missing controls or invalid targets fail validation. Notification,
runtime-switch and initial-state recovery modes require explicit `dry_run=false`.
Only a separately authorized execution may set `dry_run=false`; all existing
main-ref, admission and approval gates still apply.
