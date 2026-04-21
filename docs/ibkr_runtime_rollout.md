# InteractiveBrokersPlatform 配置落地：先跑通 `ACCOUNT_GROUP=default`

这份文档只管当前这一步：**先把 `interactive-brokers-quant-service` 这一个 Cloud Run 服务，用 `ACCOUNT_GROUP=default` 跑通。**

不在这一步里做的事：

- 不在这一步里再次改仓库名
- 不在这一步里再次重绑 Cloud Build / Cloud Run 的 GitHub source
- 不按 `ACCOUNT_GROUP` 拆多个 Cloud Run service
- 不引入新的“全局量化仓库共享 secret”

## 1. 当前建议的配置边界

现在要把配置拆成两层：

### Cloud Run / GitHub 管服务级变量

- `STRATEGY_PROFILE`
- `ACCOUNT_GROUP`
- `IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME`
- `GLOBAL_TELEGRAM_CHAT_ID`
- `NOTIFY_LANG`
- `TELEGRAM_TOKEN_SECRET_NAME`（推荐）
- `TELEGRAM_TOKEN`（fallback）

可选过渡变量：

- `IB_GATEWAY_ZONE`
- `IB_GATEWAY_IP_MODE`

### Secret Manager 里的 `ibkr-account-groups` 管 broker 运行身份

至少放这些字段：

- `ib_gateway_instance_name`
- `ib_gateway_mode`
- `ib_client_id`

建议一起放进去：

- `ib_gateway_zone`
- `ib_gateway_ip_mode`

当前代码里，`ib_gateway_instance_name`、`ib_gateway_mode`、`ib_client_id` 已经不再从 Cloud Run env 读了，所以不要继续把它们放在 GitHub Repository Variables 里当主配置源。

## 2. `ibkr-account-groups` 怎么配

仓库里已经放了一个可直接改的样例：

- `docs/examples/ibkr-account-groups.default.json`

先复制一份出来改成你的真实值：

```bash
cd /Users/lisiyi/Projects/InteractiveBrokersPlatform
cp docs/examples/ibkr-account-groups.default.json /tmp/ibkr-account-groups.json
```

最小推荐结构：

```json
{
  "groups": {
    "default": {
      "ib_gateway_instance_name": "interactive-brokers-quant-instance",
      "ib_gateway_zone": "us-central1-c",
      "ib_gateway_mode": "paper",
      "ib_gateway_ip_mode": "internal",
      "ib_client_id": 1,
      "service_name": "interactive-brokers-quant-service",
      "account_ids": ["DU1234567"]
    }
  }
}
```

说明：

- `default`：就是这次先跑通的账号组名，要和 Cloud Run env 里的 `ACCOUNT_GROUP=default` 一致。
- `ib_gateway_instance_name`：GCE 上 IB Gateway 实例名。
- `ib_gateway_zone`：建议现在就配上。当前推荐是按实例名解析内网 IP，这样 Cloud Run 不用手填固定私网 IP。
- `ib_gateway_mode`：`paper` 或 `live`。
- `ib_gateway_ip_mode`：推荐 `internal`。
- `ib_client_id`：这个账号组对应的 client id。
- `service_name`：当前只是预留元数据，建议先填成现有 Cloud Run 服务名，后面多账号拆服务时更顺。
- `account_ids`：当前主要是留档和后续扩展，不是启动必填。

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

上传后先看一眼 `default` 组：

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
- `TELEGRAM_TOKEN_SECRET_NAME=interactive-brokers-telegram-token`
- `STRATEGY_PROFILE=global_etf_rotation`
- `ACCOUNT_GROUP=default`
- `IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME=ibkr-account-groups`
- `GLOBAL_TELEGRAM_CHAT_ID`
- `NOTIFY_LANG`

可选过渡项：

- `IB_GATEWAY_ZONE`
- `IB_GATEWAY_IP_MODE`

说明：

- 如果 `ibkr-account-groups` 里的 `default` 已经写了 `ib_gateway_zone` / `ib_gateway_ip_mode`，这两个 GitHub vars 可以留空。
- 现在 workflow 会在它们留空时，顺手把 Cloud Run 上旧的 `IB_GATEWAY_ZONE` / `IB_GATEWAY_IP_MODE` env 清掉，避免双配置源漂移。

### Repository Secrets

- `TELEGRAM_TOKEN`（仅在没设置 `TELEGRAM_TOKEN_SECRET_NAME` 时作为 fallback）

说明：

- GitHub 现在通过 OIDC + Workload Identity Federation 登录 Google Cloud，这个 workflow 不再需要 `GCP_SA_KEY`。
- 如果你现在只用这个 workflow 做“同步已有 Cloud Run service 的 env”，那这个 GitHub Actions 账号只需要能更新目标 Cloud Run service，不需要现在就补 Cloud Build / Artifact Registry 那一套权限。

## 5. 先把 `ACCOUNT_GROUP=default` 跑通的顺序

建议按这个顺序来：

1. **先定真实值**
   - GCP project id
   - Cloud Run service 名
   - runtime service account 名
   - GCE instance 名 / zone
   - `paper` 还是 `live`
   - `ib_client_id`

2. **更新 Secret Manager**
   - 把 `docs/examples/ibkr-account-groups.default.json` 改成真实值
   - 上传成 `ibkr-account-groups` 最新版本

3. **给 runtime service account 授权**
   - `roles/secretmanager.secretAccessor`
   - `roles/compute.viewer`

4. **配 GitHub vars / secrets**
   - `ACCOUNT_GROUP=default`
   - `IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME=ibkr-account-groups`
   - 其他服务级变量按上面补齐

5. **触发 env sync**
   - push 到 `main`，或手动跑一次同等的 `gcloud run services update`

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
- secret 里没有 `default`
- `default` 组缺 `ib_gateway_instance_name` / `ib_gateway_mode` / `ib_client_id`

## 6. 哪些已经完成，哪些后续再做

### 当前已经完成

- `ibkr-account-groups` secret 建好，并至少把 `default` 组跑通
- Cloud Run runtime service account 权限补齐
- GitHub env sync 改成只管服务级变量
- 继续保持现有 Cloud Run service、现有 trigger、现有 `ACCOUNT_GROUP=default` 运行链路稳定

### 还可以后做

- 按 `ACCOUNT_GROUP` 拆多个 Cloud Run service
- 为每个账号组单独定义更细的 service name / 命名规范
- 把更多平台共性提取到后续平台级部署体系里
