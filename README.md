# InteractiveBrokersPlatform

![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![Platform](https://img.shields.io/badge/Broker-Interactive%20Brokers-red)
![Strategy](https://img.shields.io/badge/Strategy-US%20Equity%20Profiles-green)
![GCP](https://img.shields.io/badge/GCP-Cloud%20Run%20%2B%20GCE-4285F4)

[English](#english) | [中文](#中文)

---

<a id="english"></a>
## English

IBKR runtime for shared `us_equity` strategy profiles from `UsEquityStrategies`. It supports the `us_equity` profiles listed in the IBKR profile status table below. Strategy logic, cadence, asset universes, parameters, and research/backtest notes live in `UsEquityStrategies`.

Current strategy implementations are sourced from `UsEquityStrategies`.

Full strategy documentation now lives in [`UsEquityStrategies`](https://github.com/QuantStrategyLab/UsEquityStrategies). This README focuses on IBKR runtime behavior, profile enablement, deployment, and credentials.
This runtime matrix is the authoritative enablement source for IBKR. `UsEquityStrategies` carries strategy-layer logic, cadence, compatibility, and metadata.

### Execution boundary

The mainline runtime now follows one path only:

- `main.py` assembles platform inputs into `StrategyContext`
- `strategy_runtime.py` loads the unified strategy entrypoint
- `entrypoint.evaluate(ctx)` returns a shared `StrategyDecision`
- `decision_mapper.py` maps that decision into IBKR orders, notifications, and runtime updates

`main.py` no longer reads private strategy constants or platform-only fields from strategy return payloads.

### Strategy profile support

**Supported `STRATEGY_PROFILE` values**

- `global_etf_rotation`
- `russell_1000_multi_factor_defensive`
- `tqqq_growth_income`
- `soxl_soxx_trend_income`
- `tech_communication_pullback_enhancement`
- `mega_cap_leader_rotation_aggressive`
- `mega_cap_leader_rotation_dynamic_top20`
- `mega_cap_leader_rotation_top50_balanced`
- `dynamic_mega_leveraged_pullback`


**IBKR profile status**

| Canonical profile | Display name | Eligible | Enabled | Domain | Runtime note |
| --- | --- | --- | --- | --- | --- |
| `global_etf_rotation` | Global ETF Rotation | Yes | Yes | `us_equity` | enabled weight-mode rotation line |
| `russell_1000_multi_factor_defensive` | Russell 1000 Multi-Factor | Yes | Yes | `us_equity` | defensive stock baseline |
| `tqqq_growth_income` | TQQQ Growth Income | Yes | Yes | `us_equity` | enabled value-mode alternative |
| `soxl_soxx_trend_income` | SOXL/SOXX Semiconductor Trend Income | Yes | Yes | `us_equity` | current IBKR live line |
| `tech_communication_pullback_enhancement` | Tech/Communication Pullback Enhancement | Yes | Yes | `us_equity` | enabled feature-snapshot alternative |
| `mega_cap_leader_rotation_aggressive` | Mega Cap Leader Rotation Aggressive | Yes | Yes | `us_equity` | enabled aggressive leader rotation |
| `mega_cap_leader_rotation_dynamic_top20` | Mega Cap Leader Rotation Dynamic Top20 | Yes | Yes | `us_equity` | enabled concentrated leader rotation |
| `mega_cap_leader_rotation_top50_balanced` | Mega Cap Leader Rotation Top50 Balanced | Yes | Yes | `us_equity` | enabled balanced Top50 leader rotation |
| `dynamic_mega_leveraged_pullback` | Dynamic Mega Leveraged Pullback | Yes | Yes | `us_equity` | enabled 2x mega-cap pullback line |

Check the current matrix locally:

```bash
python3 scripts/print_strategy_profile_status.py
```

### Feature snapshot inputs

Snapshot-backed profiles use upstream artifacts from `UsEquitySnapshotPipelines`. This runtime only needs the artifact location, for example `IBKR_FEATURE_SNAPSHOT_PATH`; strategy logic, cadence, feature definitions, and snapshot schema details live in `UsEquityStrategies` / `UsEquitySnapshotPipelines`.

Example runtime pointer:

```bash
STRATEGY_PROFILE=russell_1000_multi_factor_defensive
IBKR_FEATURE_SNAPSHOT_PATH=/var/data/r1000_feature_snapshot.csv
```

### Architecture

```
Cloud Scheduler (cron chosen from the strategy-layer cadence in `UsEquityStrategies`)
    ↓ HTTP POST
Cloud Run (Flask: strategy + orchestration)
    ↓ shared adapter package
QuantPlatformKit (IBKR adapter)
    ↓ ib_insync TCP
GCE (IB Gateway, always-on)
    ↓
IBKR Account
```

### Notifications

Telegram alerts support English/Chinese execution and heartbeat messages. Strategy-specific signal/status fields come from the selected `UsEquityStrategies` profile; IBKR-specific fields cover order submission, order IDs, account-group context, and runtime state.

### Runtime env vars

The selected `ACCOUNT_GROUP` is now the runtime identity. Keep broker-specific identity in the account-group config payload, not in Cloud Run env vars.

| Variable | Required | Description |
|----------|----------|-------------|
| `IB_GATEWAY_ZONE` | Optional fallback | GCE zone (for example `us-central1-a`). Recommended to keep in the selected account-group entry; this env var is only a transition fallback. |
| `IB_GATEWAY_IP_MODE` | Optional fallback | `internal` (default) or `external`. Recommended to keep in the selected account-group entry; this env var is only a transition fallback. |
| `IBKR_CONNECT_TIMEOUT_SECONDS` | No | IB API handshake timeout in seconds. Defaults to `60`; raise only if Gateway remote API startup is consistently slow. |
| `STRATEGY_PROFILE` | Yes | Strategy profile selector. Supported `us_equity` values: `global_etf_rotation`, `russell_1000_multi_factor_defensive`, `tqqq_growth_income`, `soxl_soxx_trend_income`, `tech_communication_pullback_enhancement`, `mega_cap_leader_rotation_aggressive`, `mega_cap_leader_rotation_dynamic_top20`, `mega_cap_leader_rotation_top50_balanced`, `dynamic_mega_leveraged_pullback` |
| `ACCOUNT_GROUP` | Yes | Account-group selector. Set explicitly for each deployment. |
| `IBKR_FEATURE_SNAPSHOT_PATH` | Conditionally required | Required for snapshot-backed profiles such as `russell_1000_multi_factor_defensive`, `tech_communication_pullback_enhancement`, `mega_cap_leader_rotation_dynamic_top20`, and `mega_cap_leader_rotation_top50_balanced`. Path to the latest feature snapshot file (`.csv`, `.json`, `.jsonl`, `.parquet`). |
| `IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME` | Yes for Cloud Run | Secret Manager secret name for account-group config JSON. Recommended production source. |
| `IB_ACCOUNT_GROUP_CONFIG_JSON` | No | Local/dev JSON fallback for account-group config. Not recommended for production Cloud Run. |
| `TELEGRAM_TOKEN` | Yes | Telegram bot token. For Cloud Run, prefer a Secret Manager reference instead of a literal env var. |
| `GLOBAL_TELEGRAM_CHAT_ID` | Yes | Telegram chat ID used by this service. |
| `NOTIFY_LANG` | No | `en` (default) or `zh` |

The selected account-group entry must provide at least:

- `ib_gateway_instance_name`
- `ib_gateway_mode`
- `ib_client_id`

For the recommended Cloud Run deployment, also include:

- `ib_gateway_zone`
- `ib_gateway_ip_mode` (or let it default to `internal`)

If you use instance-name resolution with `ib_gateway_zone`, the Cloud Run runtime service account needs `roles/compute.viewer`. If you load the payload from Secret Manager, the same runtime service account also needs `roles/secretmanager.secretAccessor` on `ibkr-account-groups`.

**Recommended shared-config mode**

For the current first rollout, keep GitHub / Cloud Run focused on service-level values:

```bash
STRATEGY_PROFILE=global_etf_rotation
ACCOUNT_GROUP=default
IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME=ibkr-account-groups
GLOBAL_TELEGRAM_CHAT_ID=<telegram-chat-id>
NOTIFY_LANG=zh

# Optional transition fallback only:
IB_GATEWAY_ZONE=us-central1-c
IB_GATEWAY_IP_MODE=internal
```

For the snapshot-based stock profiles:

```bash
STRATEGY_PROFILE=russell_1000_multi_factor_defensive
ACCOUNT_GROUP=default
IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME=ibkr-account-groups
IBKR_FEATURE_SNAPSHOT_PATH=/var/data/r1000_feature_snapshot.csv
GLOBAL_TELEGRAM_CHAT_ID=<telegram-chat-id>
NOTIFY_LANG=zh
```

```bash
STRATEGY_PROFILE=tech_communication_pullback_enhancement
ACCOUNT_GROUP=default
IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME=ibkr-account-groups
IBKR_FEATURE_SNAPSHOT_PATH=/var/data/tech_communication_pullback_enhancement_feature_snapshot_latest.csv
IBKR_FEATURE_SNAPSHOT_MANIFEST_PATH=/var/manifests/tech_communication_pullback_enhancement_feature_snapshot_latest.csv.manifest.json
# IBKR_STRATEGY_CONFIG_PATH is optional; the bundled canonical default is used when unset.
IBKR_DRY_RUN_ONLY=true
GLOBAL_TELEGRAM_CHAT_ID=<telegram-chat-id>
NOTIFY_LANG=zh
```

```bash
STRATEGY_PROFILE=mega_cap_leader_rotation_dynamic_top20
ACCOUNT_GROUP=default
IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME=ibkr-account-groups
IBKR_FEATURE_SNAPSHOT_PATH=/var/data/mega_cap_leader_rotation_dynamic_top20_feature_snapshot_latest.csv
IBKR_FEATURE_SNAPSHOT_MANIFEST_PATH=/var/manifests/mega_cap_leader_rotation_dynamic_top20_feature_snapshot_latest.csv.manifest.json
GLOBAL_TELEGRAM_CHAT_ID=<telegram-chat-id>
NOTIFY_LANG=zh
```

This shared-config mode is only for the **IBKR pair** (`InteractiveBrokersPlatform` + `IBKRGatewayManager`). It is not meant to become a global secret bundle for unrelated quant repos. Across multiple quant projects, the only broadly reusable runtime settings are usually `GLOBAL_TELEGRAM_CHAT_ID` and `NOTIFY_LANG`.

Recommended account-group config payload:

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
    },
    "ira": {
      "ib_gateway_instance_name": "interactive-brokers-quant-instance",
      "ib_gateway_zone": "us-central1-c",
      "ib_gateway_mode": "paper",
      "ib_gateway_ip_mode": "internal",
      "ib_client_id": 7,
      "service_name": "interactive-brokers-quant-ira-service",
      "account_ids": ["U1234567"]
    }
  }
}
```

See [`docs/examples/ibkr-account-groups.default.json`](docs/examples/ibkr-account-groups.default.json) for a ready-to-edit starter example, and [`docs/ibkr_runtime_rollout.md`](docs/ibkr_runtime_rollout.md) for the exact rollout steps to get `ACCOUNT_GROUP=default` running.

Current behavior is fail-fast:

- missing `STRATEGY_PROFILE` → startup error
- missing `ACCOUNT_GROUP` → startup error
- missing account-group config source → startup error
- missing key fields in the selected group (`ib_gateway_instance_name`, `ib_gateway_mode`, `ib_client_id`) → startup error

### GitHub-managed Cloud Run env sync

If code deployment still uses Google Cloud Trigger, but you want GitHub to be the single source of truth for runtime env vars, this repo now includes `.github/workflows/sync-cloud-run-env.yml`.

Recommended setup:

- **Repository Variables**
  - `ENABLE_GITHUB_ENV_SYNC` = `true`
  - `CLOUD_RUN_REGION`
  - `CLOUD_RUN_SERVICE`
  - `TELEGRAM_TOKEN_SECRET_NAME` (recommended when Cloud Run already uses Secret Manager for `TELEGRAM_TOKEN`)
  - `STRATEGY_PROFILE` (set explicitly to one enabled profile, such as `soxl_soxx_trend_income`)
  - `ACCOUNT_GROUP` (recommended: `default`)
  - `IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME`
  - `GLOBAL_TELEGRAM_CHAT_ID`
  - `NOTIFY_LANG`
- **Repository Secrets**
  - `TELEGRAM_TOKEN` (fallback only when `TELEGRAM_TOKEN_SECRET_NAME` is not set)
- **Optional transition Variables**
  - `IB_GATEWAY_ZONE`
  - `IB_GATEWAY_IP_MODE`

On every push to `main`, the workflow updates the existing Cloud Run service with the values above and removes legacy env vars that should now live in the account-group config (`IB_CLIENT_ID`, `IB_GATEWAY_INSTANCE_NAME`, `IB_GATEWAY_MODE`) plus the older transport vars (`IB_GATEWAY_HOST`, `IB_GATEWAY_PORT`, `TELEGRAM_CHAT_ID`). If `IB_GATEWAY_ZONE` or `IB_GATEWAY_IP_MODE` are blank in GitHub, the workflow also removes them from Cloud Run to avoid drift.

`STRATEGY_PROFILE` is now resolved from a platform capability matrix plus a rollout allowlist derived from `runtime_enabled` strategy metadata. The current strategy domain is `us_equity`, and the repo keeps the runtime registry thin: `eligible` means the platform can run the strategy in theory, while `enabled` means the current rollout really allows it. `ACCOUNT_GROUP` now selects one account-group config entry, and the service fails fast if that runtime identity is incomplete.

Important:

- The workflow only becomes strict when `ENABLE_GITHUB_ENV_SYNC=true`. If this variable is unset, the sync job is skipped. When enabled, it resolves the selected profile's snapshot/config requirements from `scripts/print_strategy_profile_status.py --json` instead of a hard-coded strategy-name list.
- Here "shared config" still only means the **IBKR pair** (`InteractiveBrokersPlatform` + `IBKRGatewayManager`). `TELEGRAM_TOKEN` and `TELEGRAM_TOKEN_SECRET_NAME` remain repository-specific.
- If `IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME` is set, the Cloud Run runtime needs Secret Manager access to that secret.
- GitHub now authenticates to Google Cloud with OIDC + Workload Identity Federation, so `GCP_SA_KEY` is no longer required for this workflow.

### Deployment unit and naming

- `QuantPlatformKit` is only a shared dependency; Cloud Run now deploys `InteractiveBrokersPlatform`.
- Recommended Cloud Run service name: `interactive-brokers-quant-service`.
- For future multi-account rollout, keep one Cloud Run service per `ACCOUNT_GROUP`, and let each service select its account-group config at runtime.
- If you later rename or move this repository, reselect the GitHub source in Cloud Build / Cloud Run trigger instead of assuming the existing source binding will update itself.
- For the shared deployment model and trigger migration checklist, see [`QuantPlatformKit/docs/deployment_model.md`](../QuantPlatformKit/docs/deployment_model.md).

### Deployment

1. **GCE**: Set up IB Gateway (paper or live) on a GCE instance. Ensure API access is enabled, remote clients are allowed when needed, and use `4001` for `live` or `4002` for `paper`.
2. **VPC / Subnet**: Put Cloud Run and GCE in the same VPC. For cleaner firewall rules, reserve a dedicated subnet for Cloud Run Direct VPC egress.
3. **Cloud Run**: Deploy or update this Flask app with Direct VPC egress. Set `STRATEGY_PROFILE`, `ACCOUNT_GROUP`, and `IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME`. Keep `IB_GATEWAY_ZONE` / `IB_GATEWAY_IP_MODE` only as transition fallbacks if the selected account-group payload does not already contain them. The runtime service account needs `roles/secretmanager.secretAccessor` and, for instance-name resolution, `roles/compute.viewer`.
4. **Firewall**: Allow TCP `4001` (`live`) or `4002` (`paper`) from the Cloud Run egress subnet CIDR to the GCE instance.
5. **Cloud Scheduler**: Create a job that POSTs to the Cloud Run URL. Choose the cron from the strategy-layer cadence in `UsEquityStrategies`; daily profiles can still use a near-close weekday schedule such as `45 15 * * 1-5` in `America/New_York`.
6. **Optional public-IP mode**: Only if you cannot use VPC, set `IB_GATEWAY_IP_MODE=external`, expose the GCE public IP deliberately, and restrict source ranges tightly. This is not the default path.

Example deploy/update command:

```bash
gcloud run deploy interactive-brokers-quant-service \
  --source . \
  --region us-central1 \
  --service-account ibkr-platform-runtime@PROJECT_ID.iam.gserviceaccount.com \
  --network default \
  --subnet cloudrun-direct-egress \
  --vpc-egress private-ranges-only \
  --set-env-vars STRATEGY_PROFILE=global_etf_rotation,ACCOUNT_GROUP=default,IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME=ibkr-account-groups,GLOBAL_TELEGRAM_CHAT_ID=123456789,NOTIFY_LANG=zh
```

If the service already exists and your CI only updates source/image, you can patch networking separately:

```bash
gcloud run services update ibkr-quant \
  --region us-central1 \
  --network default \
  --subnet cloudrun-direct-egress \
  --vpc-egress private-ranges-only \
  --update-env-vars IB_GATEWAY_IP_MODE=internal
```

---

<a id="中文"></a>
## 中文

IBKR runtime 负责把共享的 `us_equity` 策略档位部署到 GCP Cloud Run，并连接 GCE 上的 IB Gateway 执行。策略逻辑、策略频率、标的池、参数和研究/回测说明都放在 `UsEquityStrategies`；这个仓库只维护 IBKR 运行时、账号组、Gateway 连接、下单和通知。

当前 `global_etf_rotation`、`russell_1000_multi_factor_defensive`、`tqqq_growth_income`、`soxl_soxx_trend_income`、`tech_communication_pullback_enhancement`、`mega_cap_leader_rotation_aggressive`、`mega_cap_leader_rotation_dynamic_top20`、`mega_cap_leader_rotation_top50_balanced` 和 `dynamic_mega_leveraged_pullback` 的策略实现都来自 `UsEquityStrategies`。

完整策略说明现在放在 [`UsEquityStrategies`](https://github.com/QuantStrategyLab/UsEquityStrategies)。这个 README 只保留 IBKR 运行时、profile 启用状态、部署和凭据说明。

### 执行边界

当前主线运行路径已经统一为：

- `main.py` 负责把平台输入组装成 `StrategyContext`
- `strategy_runtime.py` 负责加载统一策略入口
- `entrypoint.evaluate(ctx)` 返回共享的 `StrategyDecision`
- `decision_mapper.py` 再把决策映射成 IBKR 订单、通知和运行时更新

`main.py` 已经不再直接读取策略私有常量，也不再依赖策略返回里的平台专属字段。

### 策略输入边界

feature-snapshot 类策略使用 `UsEquitySnapshotPipelines` 发布的上游 artifact。这个运行时只需要 artifact 的位置，例如 `IBKR_FEATURE_SNAPSHOT_PATH`；策略逻辑、策略频率、特征定义和 snapshot schema 说明放在 `UsEquityStrategies` / `UsEquitySnapshotPipelines`。

### 架构

```
Cloud Scheduler（cron 以 `UsEquityStrategies` 的策略层频率为准）
    ↓ HTTP POST
Cloud Run (Flask: 策略计算 + 编排)
    ↓ 共享平台适配层
QuantPlatformKit (IBKR adapter)
    ↓ ib_insync TCP
GCE (IB Gateway 常驻)
    ↓
IBKR 账户
```

### 运行时环境变量

现在 `ACCOUNT_GROUP` 就是运行身份选择器。broker 侧身份信息应该放在账号组配置 JSON 里，不要继续把这部分主配置塞回 Cloud Run env。

| 变量 | 必需 | 说明 |
|------|------|------|
| `IB_GATEWAY_ZONE` | 可选过渡项 | GCE zone（如 `us-central1-a`）。推荐直接放进选中的账号组配置里；这里只保留过渡 fallback。 |
| `IB_GATEWAY_IP_MODE` | 可选过渡项 | `internal`（默认）或 `external`。推荐直接放进选中的账号组配置里；这里只保留过渡 fallback。 |
| `IBKR_CONNECT_TIMEOUT_SECONDS` | 否 | IB API 握手超时时间，单位秒。默认 `60`；只有 Gateway 远程 API 启动持续偏慢时才需要调高。 |
| `STRATEGY_PROFILE` | 是 | 策略档位选择。当前可用的 `us_equity` 值：`global_etf_rotation`、`russell_1000_multi_factor_defensive`、`tqqq_growth_income`、`soxl_soxx_trend_income`、`tech_communication_pullback_enhancement`、`mega_cap_leader_rotation_aggressive`、`mega_cap_leader_rotation_dynamic_top20`、`mega_cap_leader_rotation_top50_balanced`、`dynamic_mega_leveraged_pullback` |
| `ACCOUNT_GROUP` | 是 | 账号组选择器，每个部署都要显式设置。 |
| `IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME` | Cloud Run 建议必填 | 账号组配置 JSON 在 Secret Manager 里的密钥名。生产环境推荐使用。 |
| `IB_ACCOUNT_GROUP_CONFIG_JSON` | 否 | 本地开发用的账号组配置 JSON fallback。不建议在生产 Cloud Run 直接使用。 |
| `TELEGRAM_TOKEN` | 是 | Telegram 机器人 Token。Cloud Run 上更推荐走 Secret Manager 引用，不要直接写成明文 env。 |
| `GLOBAL_TELEGRAM_CHAT_ID` | 是 | 这个服务使用的 Telegram Chat ID。 |
| `NOTIFY_LANG` | 否 | `en`（默认）或 `zh` |

选中的账号组配置里，至少要有：

- `ib_gateway_instance_name`
- `ib_gateway_mode`
- `ib_client_id`

按当前推荐的 Cloud Run 部署方式，最好再一起放上：

- `ib_gateway_zone`
- `ib_gateway_ip_mode`（或者直接走默认 `internal`）

如果你配置了 `ib_gateway_zone` 让程序通过实例名解析内网 IP，Cloud Run runtime service account 需要 `roles/compute.viewer`。如果账号组配置来源是 Secret Manager，同一个 runtime service account 还需要对 `ibkr-account-groups` 具备 `roles/secretmanager.secretAccessor`。

**推荐的共享配置模式**

当前第一步，建议让 GitHub / Cloud Run 只维护服务级变量：

```bash
STRATEGY_PROFILE=soxl_soxx_trend_income
ACCOUNT_GROUP=default
IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME=ibkr-account-groups
GLOBAL_TELEGRAM_CHAT_ID=<telegram-chat-id>
NOTIFY_LANG=zh

# 仅作为过渡 fallback：
IB_GATEWAY_ZONE=us-central1-c
IB_GATEWAY_IP_MODE=internal
```

这里说的“共享配置”只针对 **IBKR 这一组系统**，也就是 `InteractiveBrokersPlatform` 和 `IBKRGatewayManager` 之间共享。它不是让所有 quant 仓库都共用一套 secrets。对多个量化仓库来说，通常只有 `GLOBAL_TELEGRAM_CHAT_ID` 和 `NOTIFY_LANG` 适合做跨项目共享。

推荐的账号组配置 JSON：

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
    },
    "ira": {
      "ib_gateway_instance_name": "interactive-brokers-quant-instance",
      "ib_gateway_zone": "us-central1-c",
      "ib_gateway_mode": "paper",
      "ib_gateway_ip_mode": "internal",
      "ib_client_id": 7,
      "service_name": "interactive-brokers-quant-ira-service",
      "account_ids": ["U1234567"]
    }
  }
}
```

仓库里也提供了一个可以直接改的默认样例：[`docs/examples/ibkr-account-groups.default.json`](docs/examples/ibkr-account-groups.default.json)。如果你要按 `ACCOUNT_GROUP=default` 先落地，直接看 [`docs/ibkr_runtime_rollout.md`](docs/ibkr_runtime_rollout.md)。

当前行为改成了 fail-fast：

- 没有 `STRATEGY_PROFILE` → 启动直接报错
- 没有 `ACCOUNT_GROUP` → 启动直接报错
- 没有账号组配置来源 → 启动直接报错
- 选中的账号组缺少关键字段（`ib_gateway_instance_name`、`ib_gateway_mode`、`ib_client_id`）→ 启动直接报错

### GitHub 统一管理 Cloud Run 环境变量

如果代码部署继续走 Google Cloud Trigger，但你想把运行时环境变量统一放在 GitHub 管理，这个仓库现在提供了 `.github/workflows/sync-cloud-run-env.yml`。

推荐配置方式：

- **仓库级 Variables**
  - `ENABLE_GITHUB_ENV_SYNC` = `true`
  - `CLOUD_RUN_REGION`
  - `CLOUD_RUN_SERVICE`
  - `TELEGRAM_TOKEN_SECRET_NAME`（如果 Cloud Run 上的 `TELEGRAM_TOKEN` 已经改成 Secret Manager，建议配置）
  - `STRATEGY_PROFILE`（显式设置为任一已启用 profile，例如 `soxl_soxx_trend_income`）
  - `ACCOUNT_GROUP`（建议设为 `default`）
  - `IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME`
  - `GLOBAL_TELEGRAM_CHAT_ID`
  - `NOTIFY_LANG`
- **仓库级 Secrets**
  - `TELEGRAM_TOKEN`（仅在没设置 `TELEGRAM_TOKEN_SECRET_NAME` 时作为 fallback）
- **可选过渡 Variables**
  - `IB_GATEWAY_ZONE`
  - `IB_GATEWAY_IP_MODE`

每次 push 到 `main` 时，这个 workflow 会把上面这些值同步到现有 Cloud Run 服务里，并清掉已经转移到账号组配置里的旧 env（`IB_CLIENT_ID`、`IB_GATEWAY_INSTANCE_NAME`、`IB_GATEWAY_MODE`）以及更早的传输层 env（`IB_GATEWAY_HOST`、`IB_GATEWAY_PORT`、`TELEGRAM_CHAT_ID`）。如果 GitHub 里没有配置 `IB_GATEWAY_ZONE` 或 `IB_GATEWAY_IP_MODE`，workflow 也会把 Cloud Run 上这两个旧值一起删除，避免双配置源漂移。

`STRATEGY_PROFILE` 现在由平台能力矩阵和从 `runtime_enabled` 策略元数据派生的 rollout allowlist 一起决定。当前策略域仍是 `us_equity`：`eligible` 表示平台理论上能跑，`enabled` 表示当前 rollout 真正放开。`ACCOUNT_GROUP` 是严格必填项，并会选中一份账号组配置。运行身份不完整时，服务会直接失败，不再静默回退。

注意：

- 只有在 `ENABLE_GITHUB_ENV_SYNC=true` 时，这个 workflow 才会严格校验并执行同步。没打开时会直接跳过。打开后，它会通过 `scripts/print_strategy_profile_status.py --json` 动态解析目标策略需要的 snapshot/config 输入，不再维护硬编码策略名列表。
- 这里说的“共享配置”仍然只针对 **IBKR 这一组系统**。`TELEGRAM_TOKEN` 和 `TELEGRAM_TOKEN_SECRET_NAME` 都还是这个仓库自己的配置，不建议提升成所有 quant 共用的全局配置。
- 如果设置了 `IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME`，Cloud Run 运行时还需要有对应 Secret 的访问权限。
- GitHub 现在通过 OIDC + Workload Identity Federation 登录 Google Cloud，这个 workflow 不再需要 `GCP_SA_KEY`。

### 部署单元和命名建议

- `QuantPlatformKit` 只是共享依赖，不单独部署；Cloud Run 现在部署的是 `InteractiveBrokersPlatform`。
- 推荐 Cloud Run 服务名：`interactive-brokers-quant-service`。
- 后续如果扩到多账户，建议按 `ACCOUNT_GROUP` 拆成多个 Cloud Run 服务，并让每个服务在运行时选中自己的账号组配置。
- 如果后面改 GitHub 仓库名或再次迁组织，Cloud Build / Cloud Run 里的 GitHub 来源需要重新选择，不要假设旧绑定会自动跟过去。
- 统一部署模型和触发器迁移清单见 [`QuantPlatformKit/docs/deployment_model.md`](../QuantPlatformKit/docs/deployment_model.md)。

### 部署

1. **GCE**: 部署 IB Gateway（模拟或实盘），确认 API 已开启、需要远程连接时已允许非 localhost 客户端，并确认 `live` 使用 `4001`、`paper` 使用 `4002`。
2. **VPC / 子网**: 让 Cloud Run 和 GCE 处于同一个 VPC。为了让防火墙规则更干净，建议给 Cloud Run Direct VPC egress 单独准备一个子网。
3. **Cloud Run**: 部署此 Flask 应用时启用 Direct VPC egress。设置 `STRATEGY_PROFILE`、`ACCOUNT_GROUP`、`IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME`；只有在账号组配置里还没放 `ib_gateway_zone` / `ib_gateway_ip_mode` 时，才临时保留 `IB_GATEWAY_ZONE` / `IB_GATEWAY_IP_MODE` 作为过渡 fallback。runtime service account 需要 `roles/secretmanager.secretAccessor`，若走实例名解析，还需要 `roles/compute.viewer`。
4. **防火墙**: 只允许 Cloud Run 出口子网访问 GCE 的 `TCP 4001`（`live`）或 `TCP 4002`（`paper`）。
5. **Cloud Scheduler**: 创建定时任务，POST 到 Cloud Run URL。cron 频率以 `UsEquityStrategies` 里的策略层 cadence 为准；日频 profile 仍可使用美股临近收盘的工作日计划，例如 `45 15 * * 1-5`（America/New_York 时区）。
6. **可选公网模式**: 只有在不能走 VPC 时，才设置 `IB_GATEWAY_IP_MODE=external`，并且要明确开放 GCE 公网 IP，同时严格限制来源 IP 和防火墙规则。

示例部署命令：

```bash
gcloud run deploy interactive-brokers-quant-service \
  --source . \
  --region us-central1 \
  --service-account ibkr-platform-runtime@PROJECT_ID.iam.gserviceaccount.com \
  --network default \
  --subnet cloudrun-direct-egress \
  --vpc-egress private-ranges-only \
  --set-env-vars STRATEGY_PROFILE=global_etf_rotation,ACCOUNT_GROUP=default,IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME=ibkr-account-groups,GLOBAL_TELEGRAM_CHAT_ID=123456789,NOTIFY_LANG=zh
```

如果服务已经存在，而你们的 CI 只是更新代码/镜像，可以单独补一次网络配置：

```bash
gcloud run services update ibkr-quant \
  --region us-central1 \
  --network default \
  --subnet cloudrun-direct-egress \
  --vpc-egress private-ranges-only \
  --update-env-vars IB_GATEWAY_IP_MODE=internal
```
