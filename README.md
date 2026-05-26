# InteractiveBrokersPlatform

![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![Platform](https://img.shields.io/badge/Broker-Interactive%20Brokers-red)
![Strategy](https://img.shields.io/badge/Strategy-US%20Equity%20Profiles-green)
![GCP](https://img.shields.io/badge/GCP-Cloud%20Run%20%2B%20GCE-4285F4)

[English](#english) | [中文](#中文)

---

<a id="english"></a>
## English

IBKR runtime for shared `us_equity` strategy profiles from `UsEquityStrategies`. Strategy logic, cadence, asset universes, parameters, and research/backtest notes live there.
The runtime carries a structured `RuntimeTarget` / `RUNTIME_TARGET_JSON` for the running service identity. Strategy-owned defaults come from `UsEquityStrategies`; platform variables are only explicit overrides.

Full strategy documentation now lives in [`UsEquityStrategies`](https://github.com/QuantStrategyLab/UsEquityStrategies). This README focuses on IBKR runtime behavior, profile enablement, deployment, and credentials, and this profile matrix remains the authoritative IBKR enablement source.

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
- `mega_cap_leader_rotation_top50_balanced`
- `nasdaq_sp500_smart_dca`


**IBKR profile status**

| Canonical profile | Display name | Eligible | Enabled | Domain | Runtime note |
| --- | --- | --- | --- | --- | --- |
| `global_etf_rotation` | Global ETF Rotation | Yes | Yes | `us_equity` | enabled weight-mode rotation line |
| `russell_1000_multi_factor_defensive` | Russell 1000 Multi-Factor | Yes | Yes | `us_equity` | defensive stock baseline |
| `tqqq_growth_income` | TQQQ Growth Income | Yes | Yes | `us_equity` | enabled value-mode alternative |
| `soxl_soxx_trend_income` | SOXL/SOXX Semiconductor Trend Income | Yes | Yes | `us_equity` | current IBKR live line |
| `tech_communication_pullback_enhancement` | Tech/Communication Pullback Enhancement | Yes | Yes | `us_equity` | enabled feature-snapshot alternative |
| `mega_cap_leader_rotation_top50_balanced` | Mega Cap Leader Rotation Top50 Balanced | Yes | Yes | `us_equity` | enabled balanced Top50 leader rotation |
| `nasdaq_sp500_smart_dca` | Nasdaq/S&P 500 Smart DCA | Yes | Yes | `us_equity` | buy-only cash-deployment profile |

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
For IBKR, keep `paper` as a single account-group entry. If you later add live accounts, split them into separate live groups; do not mix paper and live in one account-group entry.

| Variable | Required | Description |
|----------|----------|-------------|
| `IB_GATEWAY_ZONE` | Optional fallback | GCE zone (for example `us-central1-a`). Recommended to keep in the selected account-group entry; this env var is only a transition fallback. |
| `IB_GATEWAY_IP_MODE` | Optional fallback | `internal` (default) or `external`. Recommended to keep in the selected account-group entry; this env var is only a transition fallback. |
| `IBKR_CONNECT_TIMEOUT_SECONDS` | No | IB API handshake timeout in seconds. Defaults to `60`; raise only if Gateway remote API startup is consistently slow. |
| `IBKR_CONNECT_ATTEMPTS` | No | Number of IBKR connection attempts before failing the cycle. Defaults to `3`. |
| `IBKR_CONNECT_RETRY_DELAY_SECONDS` | No | Delay between failed IBKR connection attempts. Defaults to `5`. |
| `IBKR_CLIENT_ID_RETRY_OFFSET` | No | Offset added to the configured `ib_client_id` on each retry, so a timed-out API handshake can retry with a fresh client id. Defaults to `100`. |
| `STRATEGY_PROFILE` | Yes | Strategy profile selector. Supported `us_equity` values: `global_etf_rotation`, `russell_1000_multi_factor_defensive`, `tqqq_growth_income`, `soxl_soxx_trend_income`, `tech_communication_pullback_enhancement`, `mega_cap_leader_rotation_top50_balanced`, `nasdaq_sp500_smart_dca` |
| `ACCOUNT_GROUP` | Yes | Account-group selector. Set explicitly for each deployment. |
| `IBKR_FEATURE_SNAPSHOT_PATH` | Conditionally required | Required for snapshot-backed profiles such as `russell_1000_multi_factor_defensive`, `tech_communication_pullback_enhancement`, and `mega_cap_leader_rotation_top50_balanced`. Path to the latest feature snapshot file (`.csv`, `.json`, `.jsonl`, `.parquet`). |
| `IBKR_STRATEGY_PLUGIN_MOUNTS_JSON` | No | Optional IBKR-side strategy plugin mount JSON. The plugin artifact controls mode; platform config must not set `mode`. |
| `IBKR_MIN_ORDER_NOTIONAL_USD` | No | Minimum buy notional for limit buys; defaults to `50.0`. |
| `IBKR_MIN_RESERVED_CASH_USD` | No | Platform-level minimum cash reserve in USD. Defaults to `0`; the effective reserve is the max of this floor and the effective cash reserve ratio. |
| `IBKR_RESERVED_CASH_RATIO` | No | Platform-level minimum cash reserve ratio in `[0,1]`. When unset, IBKR keeps the strategy/runtime `execution_cash_reserve_ratio`; when set, it can raise but not lower that strategy ratio. |
| `IBKR_SAFE_HAVEN_CASH_SUBSTITUTE_THRESHOLD_USD` | No | Safe-haven target values below this USD amount are kept as cash instead of buying BOXX/BIL. Defaults to `1000.0`. |
| `IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME` | Yes for Cloud Run | Secret Manager secret name for account-group config JSON. Recommended production source. |
| `IB_ACCOUNT_GROUP_CONFIG_JSON` | No | Local/dev JSON fallback for account-group config. Not recommended for production Cloud Run. |
| `TELEGRAM_TOKEN` | Yes | Telegram bot token. For Cloud Run, prefer a Secret Manager reference instead of a literal env var. |
| `GLOBAL_TELEGRAM_CHAT_ID` | Yes | Telegram chat ID used by this service. |
| `NOTIFY_LANG` | No | `en` (default) or `zh` |
| `CRISIS_ALERT_CHANNELS` | No | Optional crisis alert channel list: `email`, `sms`, `push`, and/or `telegram`. |
| `CRISIS_ALERT_EMAIL_RECIPIENTS` | No | Comma/semicolon/newline-separated email-form recipients. Use a normal mailbox for email-only delivery, or a Google Voice-associated mailbox/address to also trigger Google Voice prompts. |
| `CRISIS_ALERT_EMAIL_SENDER_EMAIL` | No | Sender email address used for crisis alert email. Gmail is the default transport, but the sender naming is provider-neutral. |
| `CRISIS_ALERT_EMAIL_SENDER_PASSWORD` | No | Sender SMTP password or app password. For Cloud Run, prefer `CRISIS_ALERT_EMAIL_SENDER_PASSWORD_SECRET_NAME` in env sync. |
| `CRISIS_ALERT_EMAIL_SMTP_HOST` | No | Optional SMTP host override. Defaults to Gmail SMTP when unset. |
| `CRISIS_ALERT_EMAIL_SMTP_PORT` | No | Optional SMTP port override. Defaults to `465` when unset. |
| `CRISIS_ALERT_EMAIL_SMTP_SECURITY` | No | Optional SMTP security override: `ssl`, `starttls`, or `none`. Defaults to `ssl` when unset. |
| `CRISIS_ALERT_TELEGRAM_CHAT_IDS` | No | Dedicated crisis-alert Telegram chat IDs. Separate from the strategy-cycle Telegram chat. |
| `CRISIS_ALERT_TELEGRAM_BOT_TOKEN` | No | Dedicated crisis-alert Telegram bot token. For Cloud Run, prefer `CRISIS_ALERT_TELEGRAM_BOT_TOKEN_SECRET_NAME` in env sync. |

For the default Gateway execution backend, the selected account-group entry must provide at least:

- `execution_backend` (optional; defaults to `gateway`)
- `ib_gateway_instance_name`
- `ib_gateway_mode`
- `ib_client_id`

For the recommended Cloud Run deployment, also include:

- `ib_gateway_zone`
- `ib_gateway_port` when a VM hosts more than one Gateway; omit it to use `4001` for live and `4002` for paper.
- `ib_gateway_ip_mode` (or let it default to `internal`)

If you use instance-name resolution with `ib_gateway_zone`, the Cloud Run runtime service account needs `roles/compute.viewer`. If you load the payload from Secret Manager, the same runtime service account also needs `roles/secretmanager.secretAccessor` on `ibkr-account-groups`.

The account-group entry may also set `execution_backend` to `quantconnect`. In that mode, Gateway host/port/client-id fields are no longer required and this Cloud Run service will fail fast instead of accidentally connecting to Gateway. The QuantConnect path is a deployment/algorithm backend: real account mapping, QuantConnect project/node/compile identifiers, API token, and IBKR brokerage credentials must stay in private runtime configuration, and a QuantConnect algorithm must consume the same strategy or target configuration before live orders can be placed.

**Recommended shared-config mode**

For the current first rollout, keep GitHub / Cloud Run focused on service-level values:

```bash
STRATEGY_PROFILE=global_etf_rotation
ACCOUNT_GROUP=paper
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
ACCOUNT_GROUP=paper
IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME=ibkr-account-groups
IBKR_FEATURE_SNAPSHOT_PATH=/var/data/r1000_feature_snapshot.csv
GLOBAL_TELEGRAM_CHAT_ID=<telegram-chat-id>
NOTIFY_LANG=zh
```

```bash
STRATEGY_PROFILE=tech_communication_pullback_enhancement
ACCOUNT_GROUP=paper
IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME=ibkr-account-groups
IBKR_FEATURE_SNAPSHOT_PATH=/var/data/tech_communication_pullback_enhancement_feature_snapshot_latest.csv
IBKR_FEATURE_SNAPSHOT_MANIFEST_PATH=/var/manifests/tech_communication_pullback_enhancement_feature_snapshot_latest.csv.manifest.json
# IBKR_STRATEGY_CONFIG_PATH is optional; the bundled canonical default is used when unset.
IBKR_DRY_RUN_ONLY=true
# IBKR orders run on whole shares only.
GLOBAL_TELEGRAM_CHAT_ID=<telegram-chat-id>
NOTIFY_LANG=zh
```

```bash
STRATEGY_PROFILE=mega_cap_leader_rotation_top50_balanced
ACCOUNT_GROUP=paper
IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME=ibkr-account-groups
IBKR_FEATURE_SNAPSHOT_PATH=/var/data/mega_cap_leader_rotation_top50_balanced_feature_snapshot_latest.csv
IBKR_FEATURE_SNAPSHOT_MANIFEST_PATH=/var/manifests/mega_cap_leader_rotation_top50_balanced_feature_snapshot_latest.csv.manifest.json
GLOBAL_TELEGRAM_CHAT_ID=<telegram-chat-id>
NOTIFY_LANG=zh
```

This shared-config mode is only for the **IBKR pair** (`InteractiveBrokersPlatform` + `IBKRGatewayManager`). It is not meant to become a global secret bundle for unrelated quant repos. Across multiple quant projects, broadly reusable runtime settings are usually limited to `GLOBAL_TELEGRAM_CHAT_ID`, `NOTIFY_LANG`, `CRISIS_ALERT_CHANNELS`, and generic `CRISIS_ALERT_EMAIL_*`/`CRISIS_ALERT_PUSH_*` alert policy when the same alert policy applies.

Recommended account-group config payload:

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

For live multi-account rollout, keep one Cloud Run service per live account group. Each live group should carry exactly one `account_ids` value so portfolio reads, pending/fill guards, and submitted IBKR orders are all routed to that account.

If the IB Gateway username can access multiple linked IBKR accounts, keep those broader login credentials in the Gateway layer and restrict each Cloud Run service with its selected account-group `account_ids` value. At connect time the service validates that the configured account is visible in IBKR `managedAccounts`; if it is not visible, the cycle fails before portfolio reads or order submission. This keeps open-source configuration examples generic while allowing private deployments to map separate services to separate live accounts.

If each IBKR username can only access one linked account, run one Gateway session per username and give each account group its own `ib_gateway_port` and `ib_client_id`. The `ib_gateway_instance_name` can still point to the same VM when the Gateway containers expose different host ports.

See [`docs/examples/ibkr-account-groups.paper.json`](docs/examples/ibkr-account-groups.paper.json) for a ready-to-edit starter example, and [`docs/ibkr_runtime_rollout.md`](docs/ibkr_runtime_rollout.md) for the exact rollout steps to get `ACCOUNT_GROUP=paper` running.

Current behavior is fail-fast:

- missing `STRATEGY_PROFILE` → startup error
- missing `ACCOUNT_GROUP` → startup error
- missing account-group config source → startup error
- missing Gateway key fields in a `gateway` group (`ib_gateway_instance_name`, `ib_gateway_mode`, `ib_client_id`) → startup error
- direct Gateway execution requested while `execution_backend=quantconnect` → startup error before any Gateway connection attempt

When `IBKR_STRATEGY_PLUGIN_MOUNTS_JSON` includes the `crisis_response_shadow` plugin, the normal strategy-cycle Telegram message still includes the compact plugin line. If the plugin signal escalates beyond `no_action` (for example `canonical_route=true_crisis`, `suggested_action=defend`/`blocked`, or `would_trade_if_enabled=true`), the service also sends independent crisis alerts through configured `CRISIS_ALERT_CHANNELS` channels.
Alert results are written into the runtime report. Duplicate suppression uses stable plugin alert keys and stores markers under `STRATEGY_PLUGIN_ALERT_STATE_GCS_URI` when set, otherwise `EXECUTION_REPORT_GCS_URI`, with a local `/tmp` marker fallback.

### GitHub-managed Cloud Run env sync

If code deployment still uses Google Cloud Trigger, but you want GitHub to be the single source of truth for runtime env vars, this repo now includes `.github/workflows/sync-cloud-run-env.yml`. The workflow now also emits `RUNTIME_TARGET_JSON`, so the control plane carries a structured runtime target alongside the legacy `STRATEGY_PROFILE` selector.

Recommended setup:

- **Repository Variables**
  - `ENABLE_GITHUB_ENV_SYNC` = `true`
  - `CLOUD_RUN_REGION`
  - `CLOUD_RUN_SERVICES` (comma-, semicolon-, or newline-separated Cloud Run service names; preferred for slot deployments)
  - `CLOUD_RUN_SERVICE` (single-service fallback when `CLOUD_RUN_SERVICES` is not set)
  - `CLOUD_RUN_SERVICE_TARGETS_JSON` (preferred for full slot sync; see below)
  - Optional: `CLOUD_RUN_ENV_SYNC_WAIT_FOR_COMMIT=false` if the target services are managed by a deployment flow that does not update the `commit-sha` label before this sync runs
  - `TELEGRAM_TOKEN_SECRET_NAME` (recommended when Cloud Run already uses Secret Manager for `TELEGRAM_TOKEN`)
  - `STRATEGY_PROFILE` (set explicitly to one enabled profile, such as `soxl_soxx_trend_income`)
  - `ACCOUNT_GROUP` (recommended: `paper`)
  - `IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME`
  - Optional: `IBKR_STRATEGY_PLUGIN_MOUNTS_JSON`, `IBKR_MIN_RESERVED_CASH_USD`, `IBKR_RESERVED_CASH_RATIO`, `IBKR_SAFE_HAVEN_CASH_SUBSTITUTE_THRESHOLD_USD`
  - Optional: `CRISIS_ALERT_EMAIL_RECIPIENTS`, `CRISIS_ALERT_EMAIL_SENDER_EMAIL`, `CRISIS_ALERT_EMAIL_SENDER_PASSWORD_SECRET_NAME`
  - Optional: `CRISIS_ALERT_EMAIL_SMTP_HOST`, `CRISIS_ALERT_EMAIL_SMTP_PORT`, `CRISIS_ALERT_EMAIL_SMTP_SECURITY`
  - `GLOBAL_TELEGRAM_CHAT_ID`
  - `NOTIFY_LANG`
- **Repository Secrets**
  - `TELEGRAM_TOKEN` (fallback only when `TELEGRAM_TOKEN_SECRET_NAME` is not set)
  - `CRISIS_ALERT_EMAIL_SENDER_PASSWORD` (fallback only when `CRISIS_ALERT_EMAIL_SENDER_PASSWORD_SECRET_NAME` is not set)
- **Optional transition Variables**
  - `IB_GATEWAY_ZONE`
  - `IB_GATEWAY_IP_MODE`

On every push to `main`, the workflow builds a Cloud Run sync plan, updates the configured Cloud Run service or services, and removes legacy env vars that should now live in the account-group config (`IB_CLIENT_ID`, `IB_GATEWAY_INSTANCE_NAME`, `IB_GATEWAY_MODE`) plus the older transport vars (`IB_GATEWAY_HOST`, `IB_GATEWAY_PORT`, `TELEGRAM_CHAT_ID`). If `IB_GATEWAY_ZONE` or `IB_GATEWAY_IP_MODE` are blank in the selected sync target, the workflow also removes them from Cloud Run to avoid drift.

`STRATEGY_PROFILE` is resolved from a platform capability matrix plus a rollout allowlist derived from `runtime_enabled` strategy metadata. The current strategy domain is `us_equity`: `eligible` means the platform can run the strategy in theory, while `enabled` means the current rollout really allows it. `ACCOUNT_GROUP` selects one account-group config entry, and the service fails fast if that runtime identity is incomplete. `RUNTIME_TARGET_JSON` carries the structured runtime identity; strategy defaults continue to come from `UsEquityStrategies`.

For slot deployments, use `CLOUD_RUN_SERVICE_TARGETS_JSON` instead of a shared `RUNTIME_TARGET_JSON`. This keeps shared alert policy in one place while each Cloud Run service owns its runtime identity:

```json
{
  "defaults": {
    "GLOBAL_TELEGRAM_CHAT_ID": "5992562050",
    "NOTIFY_LANG": "zh",
    "IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME": "ibkr-account-groups",
    "IB_GATEWAY_ZONE": "us-central1-c",
    "IB_GATEWAY_IP_MODE": "internal",
    "EXECUTION_REPORT_GCS_URI": "gs://qsl-runtime-logs-interactivebrokersquant/execution-reports"
  },
  "targets": [
    {
      "service": "interactive-brokers-live-slot-a-service",
      "account_group": "live-slot-a",
      "runtime_target": {
        "platform_id": "ibkr",
        "strategy_profile": "tqqq_growth_income",
        "dry_run_only": false,
        "deployment_selector": "live-slot-a",
        "account_selector": ["U18308207"],
        "account_scope": "live-slot-a",
        "service_name": "interactive-brokers-live-slot-a-service",
        "execution_mode": "live"
      },
      "ibkr_strategy_plugin_mounts_json": {
        "strategy_plugins": [
          {
            "strategy": "tqqq_growth_income",
            "plugin": "crisis_response_shadow",
            "signal_path": "gs://qsl-runtime-logs-interactivebrokersquant/strategy-artifacts/us_equity/tqqq_growth_income/plugins/crisis_response_shadow/latest_signal.json",
            "enabled": true,
            "expected_mode": "shadow"
          }
        ]
      }
    },
    {
      "service": "interactive-brokers-live-slot-b-service",
      "account_group": "live-slot-b",
      "runtime_target": {
        "platform_id": "ibkr",
        "strategy_profile": "mega_cap_leader_rotation_top50_balanced",
        "dry_run_only": false,
        "deployment_selector": "live-slot-b",
        "account_selector": ["U15998061"],
        "account_scope": "live-slot-b",
        "service_name": "interactive-brokers-live-slot-b-service",
        "execution_mode": "live"
      },
      "ibkr_feature_snapshot_path": "gs://qsl-runtime-logs-interactivebrokersquant/strategy-artifacts/us_equity/mega_cap_leader_rotation_top50_balanced_staging/mega_cap_leader_rotation_top50_balanced_feature_snapshot_latest.csv",
      "ibkr_feature_snapshot_manifest_path": "gs://qsl-runtime-logs-interactivebrokersquant/strategy-artifacts/us_equity/mega_cap_leader_rotation_top50_balanced_staging/mega_cap_leader_rotation_top50_balanced_feature_snapshot_latest.csv.manifest.json"
    }
  ]
}
```

Important:

- The workflow only becomes strict when `ENABLE_GITHUB_ENV_SYNC=true`. If this variable is unset, the sync job is skipped. When enabled, it builds the per-service plan with `scripts/build_cloud_run_env_sync_plan.py` and resolves each selected profile's snapshot/config requirements from the strategy status matrix instead of a hard-coded strategy-name list.
- For full slot sync, configure `CLOUD_RUN_SERVICE_TARGETS_JSON`. `CLOUD_RUN_SERVICES` is only for the legacy mode where every listed service intentionally receives the same runtime env.
- Here "shared config" still only means the **IBKR pair** (`InteractiveBrokersPlatform` + `IBKRGatewayManager`). `TELEGRAM_TOKEN` and `TELEGRAM_TOKEN_SECRET_NAME` remain repository-specific. If crisis alert recipients and sender policy are shared across platforms, manage `CRISIS_ALERT_CHANNELS`, `CRISIS_ALERT_EMAIL_*`, and `CRISIS_ALERT_PUSH_*` with GitHub Organization Variables/Secrets or GCP Secret Manager.
- If `IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME` is set, the Cloud Run runtime needs Secret Manager access to that secret.
- GitHub now authenticates to Google Cloud with OIDC + Workload Identity Federation, so `GCP_SA_KEY` is no longer required for this workflow.
- If you deploy with `gcloud run deploy --source` or a Cloud Run source trigger, the staging bucket `gs://run-sources-<project>-<region>` also needs `roles/storage.objectViewer` for the build service account, the deploy service account, and the default compute service account. Missing this binding fails the deploy before Cloud Build starts with `storage.objects.get` denied.

### Deployment unit and naming

- `QuantPlatformKit` is only a shared dependency; Cloud Run now deploys `InteractiveBrokersPlatform`.
- Recommended single-service Cloud Run name: `interactive-brokers-quant-service`. For slot deployments, keep explicit service names in `CLOUD_RUN_SERVICES`.
- For future multi-account rollout, keep one Cloud Run service per `ACCOUNT_GROUP`, and let each service select its account-group config at runtime.
- If you later rename or move this repository, reselect the GitHub source in Cloud Build / Cloud Run trigger instead of assuming the existing source binding will update itself.
- For the shared deployment model and trigger migration checklist, see [`QuantPlatformKit/docs/deployment_model.md`](../QuantPlatformKit/docs/deployment_model.md).

### Deployment

1. **GCE**: Set up IB Gateway (paper or live) on a GCE instance. Ensure API access is enabled, remote clients are allowed when needed, and use `4001` for `live` or `4002` for `paper`.
2. **VPC / Subnet**: Put Cloud Run and GCE in the same VPC. For cleaner firewall rules, reserve a dedicated subnet for Cloud Run Direct VPC egress.
3. **Cloud Run**: Deploy or update this Flask app with Direct VPC egress. Set `STRATEGY_PROFILE`, `ACCOUNT_GROUP`, and `IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME`. Keep `IB_GATEWAY_ZONE` / `IB_GATEWAY_IP_MODE` only as transition fallbacks if the selected account-group payload does not already contain them. The workflow emits `RUNTIME_TARGET_JSON` to describe the structured deployment target. The runtime service account needs `roles/secretmanager.secretAccessor` and, for instance-name resolution, `roles/compute.viewer`.
   - For Cloud Run source deploy, also grant `roles/storage.objectViewer` on `gs://run-sources-${PROJECT_ID}-${REGION}` to the build service account, the deploy service account, and `${PROJECT_NUMBER}-compute@developer.gserviceaccount.com`.
4. **Firewall**: Allow TCP `4001` (`live`) or `4002` (`paper`) from the Cloud Run egress subnet CIDR to the GCE instance.
5. **Cloud Scheduler**: Create two jobs that POST to the Cloud Run URL. Use `"/precheck"` after the open window and `"/"` near the close window. Choose both crons from the strategy-layer cadence in `UsEquityStrategies`; daily profiles can still use a near-close weekday schedule such as `45 15 * * 1-5` in `America/New_York`.
6. **Optional public-IP mode**: Only if you cannot use VPC, set `IB_GATEWAY_IP_MODE=external`, expose the GCE public IP deliberately, and restrict source ranges tightly. This is not the default path.

Example deploy/update command:

```bash
gcloud run deploy interactive-brokers-quant-service \
  --source . \
  --region us-central1 \
  --service-account ibkr-platform-runtime@PROJECT_ID.iam.gserviceaccount.com \
  --concurrency 1 \
  --max-instances 1 \
  --network default \
  --subnet cloudrun-direct-egress \
  --vpc-egress private-ranges-only \
  --set-env-vars STRATEGY_PROFILE=global_etf_rotation,ACCOUNT_GROUP=paper,IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME=ibkr-account-groups,GLOBAL_TELEGRAM_CHAT_ID=123456789,NOTIFY_LANG=zh
```

If the service already exists and your CI only updates source/image, you can patch networking separately:

```bash
gcloud run services update ibkr-quant \
  --region us-central1 \
  --concurrency 1 \
  --max-instances 1 \
  --network default \
  --subnet cloudrun-direct-egress \
  --vpc-egress private-ranges-only \
  --update-env-vars IB_GATEWAY_IP_MODE=internal
```

---

<a id="中文"></a>
## 中文

IBKR runtime 负责把共享的 `us_equity` 策略档位部署到 GCP Cloud Run，并连接 GCE 上的 IB Gateway 执行。策略逻辑、策略频率、标的池、参数和研究/回测说明都放在 `UsEquityStrategies`；这个仓库只维护 IBKR 运行时、账号组、Gateway 连接、下单和通知。

完整策略说明放在 [`UsEquityStrategies`](https://github.com/QuantStrategyLab/UsEquityStrategies)。这个 README 只保留 IBKR 运行时、profile 启用状态、部署和凭据说明。

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
| `IBKR_CONNECT_ATTEMPTS` | 否 | IBKR 连接失败前最多尝试次数。默认 `3`。 |
| `IBKR_CONNECT_RETRY_DELAY_SECONDS` | 否 | IBKR 连接重试间隔，单位秒。默认 `5`。 |
| `IBKR_CLIENT_ID_RETRY_OFFSET` | 否 | 每次重试时加到 `ib_client_id` 上的偏移量，用新的 client id 避开超时握手留下的卡住会话。默认 `100`。 |
| `STRATEGY_PROFILE` | 是 | 策略档位选择。当前可用的 `us_equity` 值：`global_etf_rotation`、`russell_1000_multi_factor_defensive`、`tqqq_growth_income`、`soxl_soxx_trend_income`、`tech_communication_pullback_enhancement`、`mega_cap_leader_rotation_top50_balanced`、`nasdaq_sp500_smart_dca` |
| `ACCOUNT_GROUP` | 是 | 账号组选择器，每个部署都要显式设置。 |
| `IBKR_FEATURE_SNAPSHOT_PATH` | 条件必填 | `russell_1000_multi_factor_defensive`、`tech_communication_pullback_enhancement`、`mega_cap_leader_rotation_top50_balanced` 等快照策略需要。指向最新特征快照文件（`.csv`、`.json`、`.jsonl`、`.parquet`）。 |
| `IBKR_STRATEGY_PLUGIN_MOUNTS_JSON` | 否 | 可选的 IBKR 侧策略插件挂载 JSON。插件 artifact 自带模式；平台配置不要设置 `mode`。 |
| `IBKR_MIN_ORDER_NOTIONAL_USD` | 否 | 限价买入的最小名义金额；默认 `50.0`。 |
| `IBKR_MIN_RESERVED_CASH_USD` | 否 | 平台级最低预留现金 USD。默认 `0`；实际预留取该下限和有效预留现金比例中的最大值。 |
| `IBKR_RESERVED_CASH_RATIO` | 否 | 平台级最低预留现金比例，取值 `[0,1]`。不设置时沿用策略/运行配置里的 `execution_cash_reserve_ratio`；设置后只会抬高，不会降低策略比例。 |
| `IBKR_SAFE_HAVEN_CASH_SUBSTITUTE_THRESHOLD_USD` | 否 | `BOXX`/`BIL` 等避险标的目标金额低于该 USD 门槛时保留现金，不买入。默认 `1000.0`。 |
| `IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME` | Cloud Run 建议必填 | 账号组配置 JSON 在 Secret Manager 里的密钥名。生产环境推荐使用。 |
| `IB_ACCOUNT_GROUP_CONFIG_JSON` | 否 | 本地开发用的账号组配置 JSON fallback。不建议在生产 Cloud Run 直接使用。 |
| `TELEGRAM_TOKEN` | 是 | Telegram 机器人 Token。Cloud Run 上更推荐走 Secret Manager 引用，不要直接写成明文 env。 |
| `GLOBAL_TELEGRAM_CHAT_ID` | 是 | 这个服务使用的 Telegram Chat ID。 |
| `NOTIFY_LANG` | 否 | `en`（默认）或 `zh` |
| `CRISIS_ALERT_CHANNELS` | 否 | 可选危机告警通道列表：`email`、`sms`、`push` 和/或 `telegram`。 |
| `CRISIS_ALERT_EMAIL_RECIPIENTS` | 否 | 通知收件邮箱。普通邮箱只收邮件；关联 Google Voice 的邮箱/地址会额外触发 Google Voice 提醒。支持逗号、分号或换行分隔。 |
| `CRISIS_ALERT_EMAIL_SENDER_EMAIL` | 否 | 邮件通知的发送方邮箱。默认传输走 Gmail SMTP，但命名不绑定 Gmail。 |
| `CRISIS_ALERT_EMAIL_SENDER_PASSWORD` | 否 | 发送方 SMTP 密码或 app password。Cloud Run env sync 建议配置 `CRISIS_ALERT_EMAIL_SENDER_PASSWORD_SECRET_NAME`。 |
| `CRISIS_ALERT_EMAIL_SMTP_HOST` | 否 | 可选 SMTP host 覆盖。不设置时默认 Gmail SMTP。 |
| `CRISIS_ALERT_EMAIL_SMTP_PORT` | 否 | 可选 SMTP port 覆盖。不设置时默认 `465`。 |
| `CRISIS_ALERT_EMAIL_SMTP_SECURITY` | 否 | 可选 SMTP 加密方式：`ssl`、`starttls` 或 `none`。不设置时默认 `ssl`。 |
| `CRISIS_ALERT_TELEGRAM_CHAT_IDS` | 否 | 危机告警专用 Telegram chat ID，和常规策略周期 Telegram 分开。 |
| `CRISIS_ALERT_TELEGRAM_BOT_TOKEN` | 否 | 危机告警专用 Telegram bot token。Cloud Run env sync 建议配置 `CRISIS_ALERT_TELEGRAM_BOT_TOKEN_SECRET_NAME`。 |

默认 Gateway 执行后端下，选中的账号组配置里至少要有：

- `execution_backend`（可选；默认 `gateway`）
- `ib_gateway_instance_name`
- `ib_gateway_mode`
- `ib_client_id`

按当前推荐的 Cloud Run 部署方式，最好再一起放上：

- `ib_gateway_zone`
- `ib_gateway_port`（同一台 VM 上有多个 Gateway 时填写；不填则 live 默认 `4001`，paper 默认 `4002`）
- `ib_gateway_ip_mode`（或者直接走默认 `internal`）

如果你配置了 `ib_gateway_zone` 让程序通过实例名解析内网 IP，Cloud Run runtime service account 需要 `roles/compute.viewer`。如果账号组配置来源是 Secret Manager，同一个 runtime service account 还需要对 `ibkr-account-groups` 具备 `roles/secretmanager.secretAccessor`。

账号组也可以把 `execution_backend` 设为 `quantconnect`。这个模式不再要求 Gateway 主机、端口和 client id，当前 Cloud Run 服务也会 fail-fast，避免误连 Gateway。QuantConnect 路径是部署/算法后端：真实账号映射、QuantConnect project/node/compile 标识、API token 和 IBKR 券商凭证都必须留在私有运行配置里，并且需要 QuantConnect 算法项目消费同一套策略或目标配置后才能实盘下单。

**推荐的共享配置模式**

当前第一步，建议让 GitHub / Cloud Run 只维护服务级变量：

```bash
STRATEGY_PROFILE=soxl_soxx_trend_income
ACCOUNT_GROUP=paper
IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME=ibkr-account-groups
GLOBAL_TELEGRAM_CHAT_ID=<telegram-chat-id>
NOTIFY_LANG=zh

# 仅作为过渡 fallback：
IB_GATEWAY_ZONE=us-central1-c
IB_GATEWAY_IP_MODE=internal
```

这里说的“共享配置”只针对 **IBKR 这一组系统**，也就是 `InteractiveBrokersPlatform` 和 `IBKRGatewayManager` 之间共享。它不是让所有 quant 仓库都共用一套 secrets。对多个量化仓库来说，`GLOBAL_TELEGRAM_CHAT_ID`、`NOTIFY_LANG`、`CRISIS_ALERT_CHANNELS`，以及同一套危机告警策略下的 `CRISIS_ALERT_EMAIL_*`/`CRISIS_ALERT_PUSH_*` 适合提升到组织级配置；告警 token 和密码仍应放在 GitHub Secret 或 GCP Secret Manager。

推荐的账号组配置 JSON：

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

仓库里也提供了一个可以直接改的起始样例：[`docs/examples/ibkr-account-groups.paper.json`](docs/examples/ibkr-account-groups.paper.json)。如果你要按 `ACCOUNT_GROUP=paper` 先落地，直接看 [`docs/ibkr_runtime_rollout.md`](docs/ibkr_runtime_rollout.md)。

实盘多账户建议一个 UID 对应一个 Cloud Run 服务和一个账号组。每个实盘账号组只放一个 `account_ids` 值；运行时会用它过滤持仓、pending/fill 检查，并把同一个 UID 写进 IBKR 订单的 `order.account`。

如果 IB Gateway 登录用户名本身能访问多个 linked IBKR 账户，仍然建议把这种更宽的登录权限留在 Gateway 层，每个 Cloud Run 服务只通过自己选中的账号组 `account_ids` 限定一个交易账户。服务连接成功后会校验该账号是否出现在 IBKR `managedAccounts` 里；如果不可见，会在读取组合或提交订单之前失败。开源仓库只保留通用示例，私有实盘映射放在 Secret Manager 等运行配置里。

如果 IBKR 的每个副用户名只能看到一个 linked 账户，就按“一个用户名一个 Gateway session”部署；每个账号组配置自己的 `ib_gateway_port` 和 `ib_client_id`。多个 Gateway 可以在同一台 VM 上运行，只要 Gateway 容器暴露到不同 host port。

当前行为改成了 fail-fast：

- 没有 `STRATEGY_PROFILE` → 启动直接报错
- 没有 `ACCOUNT_GROUP` → 启动直接报错
- 没有账号组配置来源 → 启动直接报错
- `gateway` 账号组缺少关键字段（`ib_gateway_instance_name`、`ib_gateway_mode`、`ib_client_id`）→ 启动直接报错
- `execution_backend=quantconnect` 时直接请求 Gateway 执行 → 启动/执行前直接报错，不会尝试连接 Gateway

如果 `IBKR_STRATEGY_PLUGIN_MOUNTS_JSON` 挂载了 `crisis_response_shadow` 插件，常规策略周期 Telegram 仍会包含插件摘要行。当插件信号升级到非 `no_action`（例如 `canonical_route=true_crisis`、`suggested_action=defend`/`blocked`，或 `would_trade_if_enabled=true`）时，服务还会按 `CRISIS_ALERT_CHANNELS` 配置额外发送独立危机通知。
告警结果会写入 runtime report。重复发送抑制使用稳定的插件告警 key；如配置了 `STRATEGY_PLUGIN_ALERT_STATE_GCS_URI` 则写入该前缀，否则复用 `EXECUTION_REPORT_GCS_URI`，并有本地 `/tmp` marker fallback。

### GitHub 统一管理 Cloud Run 环境变量

如果代码部署继续走 Google Cloud Trigger，但你想把运行时环境变量统一放在 GitHub 管理，这个仓库现在提供了 `.github/workflows/sync-cloud-run-env.yml`。

推荐配置方式：

- **仓库级 Variables**
  - `ENABLE_GITHUB_ENV_SYNC` = `true`
  - `CLOUD_RUN_REGION`
  - `CLOUD_RUN_SERVICES`（逗号、分号或换行分隔的 Cloud Run 服务名；slot 部署优先用这个）
  - `CLOUD_RUN_SERVICE`（没设置 `CLOUD_RUN_SERVICES` 时的单服务兼容入口）
  - `CLOUD_RUN_SERVICE_TARGETS_JSON`（全量同步 slot 时优先用这个，见下面示例）
  - 可选：`CLOUD_RUN_ENV_SYNC_WAIT_FOR_COMMIT=false`（当目标服务由另一个部署链路管理、不会在同步前更新 `commit-sha` label 时使用）
  - `TELEGRAM_TOKEN_SECRET_NAME`（如果 Cloud Run 上的 `TELEGRAM_TOKEN` 已经改成 Secret Manager，建议配置）
  - `STRATEGY_PROFILE`（显式设置为任一已启用 profile，例如 `soxl_soxx_trend_income`）
  - `ACCOUNT_GROUP`（建议设为 `paper`）
  - `IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME`
  - 可选：`IBKR_STRATEGY_PLUGIN_MOUNTS_JSON`、`IBKR_MIN_RESERVED_CASH_USD`、`IBKR_RESERVED_CASH_RATIO`、`IBKR_SAFE_HAVEN_CASH_SUBSTITUTE_THRESHOLD_USD`
  - 可选：`CRISIS_ALERT_EMAIL_RECIPIENTS`、`CRISIS_ALERT_EMAIL_SENDER_EMAIL`、`CRISIS_ALERT_EMAIL_SENDER_PASSWORD_SECRET_NAME`
  - 可选：`CRISIS_ALERT_EMAIL_SMTP_HOST`、`CRISIS_ALERT_EMAIL_SMTP_PORT`、`CRISIS_ALERT_EMAIL_SMTP_SECURITY`
  - `GLOBAL_TELEGRAM_CHAT_ID`
  - `NOTIFY_LANG`
- **仓库级 Secrets**
  - `TELEGRAM_TOKEN`（仅在没设置 `TELEGRAM_TOKEN_SECRET_NAME` 时作为 fallback）
  - `CRISIS_ALERT_EMAIL_SENDER_PASSWORD`（仅在没设置 `CRISIS_ALERT_EMAIL_SENDER_PASSWORD_SECRET_NAME` 时作为 fallback）
- **可选过渡 Variables**
  - `IB_GATEWAY_ZONE`
  - `IB_GATEWAY_IP_MODE`

每次 push 到 `main` 时，这个 workflow 会先生成 Cloud Run sync plan，再把目标值同步到配置的一个或多个 Cloud Run 服务里，并清掉已经转移到账号组配置里的旧 env（`IB_CLIENT_ID`、`IB_GATEWAY_INSTANCE_NAME`、`IB_GATEWAY_MODE`）以及更早的传输层 env（`IB_GATEWAY_HOST`、`IB_GATEWAY_PORT`、`TELEGRAM_CHAT_ID`）。如果目标 sync 配置里没有 `IB_GATEWAY_ZONE` 或 `IB_GATEWAY_IP_MODE`，workflow 也会把 Cloud Run 上这两个旧值一起删除，避免双配置源漂移。

`STRATEGY_PROFILE` 由平台能力矩阵和从 `runtime_enabled` 策略元数据派生的 rollout allowlist 一起决定。当前策略域仍是 `us_equity`：`eligible` 表示平台理论上能跑，`enabled` 表示当前 rollout 真正放开。`ACCOUNT_GROUP` 是严格必填项，并会选中一份账号组配置。运行身份不完整时，服务会直接失败，不再静默回退。

注意：

- 只有在 `ENABLE_GITHUB_ENV_SYNC=true` 时，这个 workflow 才会严格校验并执行同步。没打开时会直接跳过。打开后，它会用 `scripts/build_cloud_run_env_sync_plan.py` 生成 per-service plan，并从策略状态矩阵动态解析每个目标策略需要的 snapshot/config 输入，不再维护硬编码策略名列表。
- 全量同步 slot 时应配置 `CLOUD_RUN_SERVICE_TARGETS_JSON`。`CLOUD_RUN_SERVICES` 只适合旧模式，也就是多个服务确实要收到同一份 runtime env。
- 这里说的“共享配置”仍然只针对 **IBKR 这一组系统**。`TELEGRAM_TOKEN` 和 `TELEGRAM_TOKEN_SECRET_NAME` 都还是这个仓库自己的配置，不建议提升成所有 quant 共用的全局配置。危机告警如果确实跨平台共用同一套收件人和发送方，可以用 GitHub Organization Variables/Secrets 管理 `CRISIS_ALERT_CHANNELS`、`CRISIS_ALERT_EMAIL_*` 和 `CRISIS_ALERT_PUSH_*`。
- 如果设置了 `IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME`，Cloud Run 运行时还需要有对应 Secret 的访问权限。
- GitHub 现在通过 OIDC + Workload Identity Federation 登录 Google Cloud，这个 workflow 不再需要 `GCP_SA_KEY`。
- 如果你用 `gcloud run deploy --source` 或 Cloud Run source trigger 部署，还要给 staging bucket `gs://run-sources-<project>-<region>` 补 `roles/storage.objectViewer`，对象包括 build service account、deploy service account、默认 compute service account。少了这层权限，会在 Cloud Build 启动前直接报 `storage.objects.get denied`。

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
   - 如果使用 Cloud Run source deploy，还要给 `gs://run-sources-${PROJECT_ID}-${REGION}` 这个 bucket 授权 `roles/storage.objectViewer`，对象是 build service account、deploy service account，以及 `${PROJECT_NUMBER}-compute@developer.gserviceaccount.com`。
4. **防火墙**: 只允许 Cloud Run 出口子网访问 GCE 的 `TCP 4001`（`live`）或 `TCP 4002`（`paper`）。
5. **Cloud Scheduler**: 创建定时任务，POST 到 Cloud Run URL。cron 频率以 `UsEquityStrategies` 里的策略层 cadence 为准；日频 profile 仍可使用美股临近收盘的工作日计划，例如 `45 15 * * 1-5`（America/New_York 时区）。
6. **可选公网模式**: 只有在不能走 VPC 时，才设置 `IB_GATEWAY_IP_MODE=external`，并且要明确开放 GCE 公网 IP，同时严格限制来源 IP 和防火墙规则。

示例部署命令：

```bash
gcloud run deploy interactive-brokers-quant-service \
  --source . \
  --region us-central1 \
  --service-account ibkr-platform-runtime@PROJECT_ID.iam.gserviceaccount.com \
  --concurrency 1 \
  --max-instances 1 \
  --network default \
  --subnet cloudrun-direct-egress \
  --vpc-egress private-ranges-only \
  --set-env-vars STRATEGY_PROFILE=global_etf_rotation,ACCOUNT_GROUP=paper,IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME=ibkr-account-groups,GLOBAL_TELEGRAM_CHAT_ID=123456789,NOTIFY_LANG=zh
```

如果服务已经存在，而你们的 CI 只是更新代码/镜像，可以单独补一次网络配置：

```bash
gcloud run services update ibkr-quant \
  --region us-central1 \
  --concurrency 1 \
  --max-instances 1 \
  --network default \
  --subnet cloudrun-direct-egress \
  --vpc-egress private-ranges-only \
  --update-env-vars IB_GATEWAY_IP_MODE=internal
```
