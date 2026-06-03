# InteractiveBrokersPlatform

<!-- qsl-doc-overview:start -->

> ⚠️ 投资有风险，不构成投资建议，仅供学习交流用途。
> ⚠️ Investing involves risk. This project does not provide investment advice and is for educational and research purposes only.

## Open-source overview / 开源项目入口

| Item | Description |
| --- | --- |
| Project type | execution platform |
| What it does | Interactive Brokers execution platform for QuantStrategyLab US/HK equity strategies with Cloud Run dispatch and dry-run controls. |
| 中文说明 | IBKR 执行平台，负责加载策略、处理账户/市场输入，并通过 Cloud Run/调度执行 dry-run 或实盘路径。 |
| Current status | Execution platform. Treat all live credentials, account IDs and order paths as production-sensitive. |

### Quick start

- `python -m pip install -e '.[test]'`
- `python -m pytest -q`

### Deploy / operate safely

Start from dry-run GitHub Actions/Cloud Run deployment, verify secrets and account group config, then enable live execution only per account.

### Strategy performance / evidence boundary

Strategy performance is not owned here; review UsEquityStrategies, HkEquityStrategies and their snapshot pipeline evidence before enabling a profile.

> Detailed runbooks, migration notes, workflow internals, and historical decisions are kept below. Start with this overview before using the lower-level operational sections.

<!-- qsl-doc-overview:end -->

> Risk warning: this project is not investment advice and is provided for study and engineering validation only.

![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![Platform](https://img.shields.io/badge/Broker-Interactive%20Brokers-red)
![Strategy](https://img.shields.io/badge/Strategy-US%2FHK%20Equity%20Profiles-green)
![GCP](https://img.shields.io/badge/GCP-Cloud%20Run%20%2B%20GCE-4285F4)

Language: [English](README.md) | [中文](README.zh-CN.md)

---

IBKR runtime for shared `us_equity` profiles from `UsEquityStrategies` and `hk_equity` profiles from `HkEquityStrategies`. Strategy logic, cadence, asset universes, parameters, and research/backtest notes live in the strategy repositories.
The runtime carries a structured `RuntimeTarget` / `RUNTIME_TARGET_JSON` for the running service identity. Strategy-owned defaults come from `UsEquityStrategies` and `HkEquityStrategies`; platform variables are only explicit overrides.

Strategy documentation lives in [`UsEquityStrategies`](https://github.com/QuantStrategyLab/UsEquityStrategies) and [`HkEquityStrategies`](https://github.com/QuantStrategyLab/HkEquityStrategies). HK snapshot artifact contracts are produced by [`HkEquitySnapshotPipelines`](https://github.com/QuantStrategyLab/HkEquitySnapshotPipelines). This README focuses on IBKR runtime behavior, profile enablement, deployment, and credentials, and this profile matrix remains the authoritative IBKR enablement source.

### Execution boundary

The mainline runtime now follows one path only:

- `main.py` assembles platform inputs into `StrategyContext`
- `strategy_runtime.py` loads the unified strategy entrypoint
- `entrypoint.evaluate(ctx)` returns a shared `StrategyDecision`
- `decision_mapper.py` maps that decision into IBKR orders, notifications, and runtime updates

`main.py` no longer reads private strategy constants or platform-only fields from strategy return payloads.

### Execution safety

IBKR order routing expects weight targets. Value-target decisions are translated
to weights using `portfolio_total_equity` from runtime metadata. If a new or
empty account reports non-positive total equity, the mapper marks the decision
as `no_execute` and omits the allocation payload instead of attempting
translation.

### Strategy profile support

**Supported `STRATEGY_PROFILE` values**

- `global_etf_rotation`
- `russell_1000_multi_factor_defensive`
- `tqqq_growth_income`
- `soxl_soxx_trend_income`
- `tech_communication_pullback_enhancement`
- `mega_cap_leader_rotation_top50_balanced`
- `nasdaq_sp500_smart_dca`
- `hk_blue_chip_leader_rotation` (architecture scaffold only; eligible but disabled)
- `hk_index_mean_reversion` (market-history research candidate; eligible but disabled)
- `hk_etf_regime_rotation` (market-history research candidate; eligible but disabled)
- `hk_listed_global_etf_rotation` (volatility-targeted market-history strategy; runtime-enabled and selectable by Cloud Run runtime config)


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
| `hk_blue_chip_leader_rotation` | HK Blue Chip Leader Rotation | Yes | No | `hk_equity` | architecture scaffold only; not runtime-enabled |
| `hk_index_mean_reversion` | HK Index Mean Reversion | Yes | No | `hk_equity` | market-history research candidate; not runtime-enabled |
| `hk_etf_regime_rotation` | HK ETF Regime Rotation | Yes | No | `hk_equity` | market-history research candidate; not runtime-enabled |
| `hk_listed_global_etf_rotation` | HK-listed Global ETF Rotation | Yes | Yes | `hk_equity` | runtime-enabled; selectable by HK Cloud Run runtime config |

Check the current matrix locally:

```bash
python3 scripts/print_strategy_profile_status.py
```

### Feature snapshot inputs

Snapshot-backed profiles use upstream artifacts from `UsEquitySnapshotPipelines` or `HkEquitySnapshotPipelines`. This runtime only needs the artifact location, for example `IBKR_FEATURE_SNAPSHOT_PATH`; strategy logic, cadence, feature definitions, and snapshot schema details live in the strategy/snapshot repositories.

For the HK-equity runtime scope, platform matrix, and env defaults, see [`docs/hk_equity_runtime.md`](docs/hk_equity_runtime.md).

For HK Cloud Run deployment or env review, print the switch plan first. To deploy or resync an isolated HK dry-run service, manually trigger the `Deploy Cloud Run` workflow with `target=hk-verify`:

```bash
python scripts/print_strategy_switch_env_plan.py --profile hk_listed_global_etf_rotation --dry-run-only --deployment-selector hk-verify --account-scope hk-verify --account-group hk-verify --service-name interactive-brokers-hk-verify-service --json
gh workflow run sync-cloud-run-env.yml --repo QuantStrategyLab/InteractiveBrokersPlatform -f target=hk-verify -f cloud_run_region=<gcp-region> -f cloud_run_service=interactive-brokers-hk-verify-service -f account_group=hk-verify -f account_group_config_secret_name=ibkr-account-groups -f deploy_image=true -f sync_env=true
```

Example runtime pointer:

```bash
STRATEGY_PROFILE=russell_1000_multi_factor_defensive
IBKR_FEATURE_SNAPSHOT_PATH=/var/data/r1000_feature_snapshot.csv
```

### Architecture

```
Cloud Scheduler (cron chosen from the selected strategy-layer cadence)
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

Telegram alerts support English/Chinese execution and heartbeat messages. Strategy-specific signal/status fields come from the selected strategy package profile; IBKR-specific fields cover order submission, order IDs, account-group context, market scope, and runtime state.

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
| `STRATEGY_PROFILE` | Yes | Strategy profile selector. Enabled values: `global_etf_rotation`, `russell_1000_multi_factor_defensive`, `tqqq_growth_income`, `soxl_soxx_trend_income`, `tech_communication_pullback_enhancement`, `mega_cap_leader_rotation_top50_balanced`, `nasdaq_sp500_smart_dca`, `hk_listed_global_etf_rotation`. `hk_blue_chip_leader_rotation`, `hk_index_mean_reversion`, and `hk_etf_regime_rotation` are not runtime-enabled and are not selectable by the platform status/switch tooling. Cloud Run uses the values configured on the selected service. |
| `ACCOUNT_GROUP` | Yes | Account-group selector. Set explicitly for each deployment. |
| `IBKR_MARKET` | No | Market scope. Defaults to `HK` when `ACCOUNT_GROUP` contains `hk`, otherwise `US`. |
| `IBKR_MARKET_CALENDAR` | No | Market calendar. Defaults to `XHKG` for HK and `NYSE` for US. |
| `IBKR_MARKET_TIMEZONE` | No | Market timezone. Defaults to `Asia/Hong_Kong` for HK and `America/New_York` for US. |
| `IBKR_MARKET_EXCHANGE` | No | Stock contract exchange. Defaults to `SEHK` for HK and `SMART` for US. |
| `IBKR_MARKET_CURRENCY` | No | Stock contract currency and portfolio currency scope. Defaults to `HKD` for HK and `USD` for US. |
| `IBKR_MARKET_DATA_SYMBOL_SUFFIX` | No | Suffix used only for yfinance fallback symbols. Defaults to `.HK` for HK and empty for US. |
| `IBKR_FEATURE_SNAPSHOT_PATH` | Conditionally required | Required for enabled snapshot-backed profiles such as `russell_1000_multi_factor_defensive`, `tech_communication_pullback_enhancement`, and `mega_cap_leader_rotation_top50_balanced`. The HK scaffold will also require this after promotion. Path to the latest feature snapshot file (`.csv`, `.json`, `.jsonl`, `.parquet`). |
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

### GitHub-managed Cloud Run deploy and env sync

This repo includes `.github/workflows/sync-cloud-run-env.yml` for GitHub-managed Cloud Run automation. Set `ENABLE_GITHUB_CLOUD_RUN_DEPLOY=true` to build and deploy the container image from GitHub Actions; set `ENABLE_GITHUB_ENV_SYNC=true` to sync runtime env vars. You can enable either flag independently during migration from a Google Cloud Trigger. The workflow also emits `RUNTIME_TARGET_JSON`, so the control plane carries a structured runtime target alongside the legacy `STRATEGY_PROFILE` selector.

Pushes to `main` use the `ENABLE_MAIN_PUSH_CLOUD_RUN_AUTOMATION` automation switch. Set it to `true` when main-branch pushes should also run Cloud Run automation; manual `workflow_dispatch` runs still follow the deploy/env-sync flags above.

Recommended setup:

- **Repository Variables**
  - `ENABLE_GITHUB_CLOUD_RUN_DEPLOY` = `true` to let GitHub Actions build/push/deploy the Cloud Run image
  - `ENABLE_GITHUB_ENV_SYNC` = `true`
  - `CLOUD_RUN_REGION`
  - `CLOUD_RUN_SERVICES` (comma-, semicolon-, or newline-separated Cloud Run service names; preferred for slot deployments)
  - `CLOUD_RUN_SERVICE` (single-service fallback when `CLOUD_RUN_SERVICES` is not set)
  - `CLOUD_RUN_SERVICE_TARGETS_JSON` (preferred for full slot sync; see below)
  - Optional: `GCP_ARTIFACT_REGISTRY_HOSTNAME` when Artifact Registry is not in the Cloud Run region (default: `<CLOUD_RUN_REGION>-docker.pkg.dev`)
  - Optional: `CLOUD_RUN_ENV_SYNC_WAIT_FOR_COMMIT=false` if the target services are managed by a deployment flow that does not update the `commit-sha` label before this sync runs
  - `TELEGRAM_TOKEN_SECRET_NAME` (recommended when Cloud Run already uses Secret Manager for `TELEGRAM_TOKEN`)
  - `STRATEGY_PROFILE` (set explicitly to one enabled profile, such as `soxl_soxx_trend_income`)
  - `ACCOUNT_GROUP` (recommended: `paper`)
  - `IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME`
  - Optional: `IBKR_MARKET`, `IBKR_MARKET_CALENDAR`, `IBKR_MARKET_CURRENCY`, `IBKR_MARKET_DATA_SYMBOL_SUFFIX`, `IBKR_MARKET_EXCHANGE`, `IBKR_MARKET_TIMEZONE`, `IBKR_STRATEGY_PLUGIN_MOUNTS_JSON`, `IBKR_MIN_RESERVED_CASH_USD`, `IBKR_RESERVED_CASH_RATIO`, `IBKR_SAFE_HAVEN_CASH_SUBSTITUTE_THRESHOLD_USD`
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

On every push to `main`, the workflow can build one container image, deploy it to one or more configured Cloud Run services, build a Cloud Run sync plan, update the configured Cloud Run service env vars, and remove legacy env vars that should now live in the account-group config (`IB_CLIENT_ID`, `IB_GATEWAY_INSTANCE_NAME`, `IB_GATEWAY_MODE`) plus the older transport vars (`IB_GATEWAY_HOST`, `IB_GATEWAY_PORT`, `TELEGRAM_CHAT_ID`). If `IB_GATEWAY_ZONE` or `IB_GATEWAY_IP_MODE` are blank in the selected sync target, the workflow also removes them from Cloud Run to avoid drift.

`STRATEGY_PROFILE` is resolved from a platform capability matrix plus a rollout allowlist derived from `runtime_enabled` strategy metadata. The current strategy domains are `us_equity` and `hk_equity`: `eligible` means the platform can run the strategy in theory, while `enabled` means the current rollout really allows it. `ACCOUNT_GROUP` selects one account-group config entry, and the service fails fast if that runtime identity is incomplete. `RUNTIME_TARGET_JSON` carries the structured runtime identity; strategy defaults continue to come from the strategy packages.

For slot deployments, use `CLOUD_RUN_SERVICE_TARGETS_JSON` instead of a shared `RUNTIME_TARGET_JSON`. This keeps shared alert policy in one place while each Cloud Run service owns its runtime identity:

```json
{
  "defaults": {
    "GLOBAL_TELEGRAM_CHAT_ID": "<telegram-chat-id>",
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
        "account_selector": ["U1234567"],
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
      "service": "interactive-brokers-live-u7654-mega-service",
      "account_group": "live-u7654-mega",
      "runtime_target": {
        "platform_id": "ibkr",
        "strategy_profile": "mega_cap_leader_rotation_top50_balanced",
        "dry_run_only": false,
        "deployment_selector": "live-u7654-mega",
        "account_selector": ["U7654321"],
        "account_scope": "live-u7654-mega",
        "service_name": "interactive-brokers-live-u7654-mega-service",
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
- The deploy path only becomes active when `ENABLE_GITHUB_CLOUD_RUN_DEPLOY=true`. If it is unset, an existing Cloud Build trigger can keep owning code deployment while this workflow only syncs env.
- For full slot sync, configure `CLOUD_RUN_SERVICE_TARGETS_JSON`. `CLOUD_RUN_SERVICES` is only for the legacy mode where every listed service intentionally receives the same runtime env.
- Here "shared config" still only means the **IBKR pair** (`InteractiveBrokersPlatform` + `IBKRGatewayManager`). `TELEGRAM_TOKEN` and `TELEGRAM_TOKEN_SECRET_NAME` remain repository-specific. If crisis alert recipients and sender policy are shared across platforms, manage `CRISIS_ALERT_CHANNELS`, `CRISIS_ALERT_EMAIL_*`, and `CRISIS_ALERT_PUSH_*` with GitHub Organization Variables/Secrets or GCP Secret Manager.
- If `IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME` is set, the Cloud Run runtime needs Secret Manager access to that secret.
- GitHub now authenticates to Google Cloud with OIDC + Workload Identity Federation, so `GCP_SA_KEY` is no longer required for this workflow.
- GitHub deploy uses the repository Dockerfile and Artifact Registry. The deploy service account needs Artifact Registry writer, Cloud Run admin, and service-account user permissions for the runtime service account.

### Runtime guard alerting

`.github/workflows/runtime-guard.yml` is a second notification layer for failures
outside the IBKR Flask handler. It reads Cloud Logging for recent Cloud Scheduler
errors and Cloud Run request/runtime failures, then sends Telegram directly
through `CRISIS_ALERT_TELEGRAM_BOT_TOKEN` + `CRISIS_ALERT_TELEGRAM_CHAT_IDS` or
the fallback `TELEGRAM_TOKEN` + `GLOBAL_TELEGRAM_CHAT_ID`.

The guard does not invoke Cloud Run trading routes. It is meant to catch cases
where Scheduler cannot reach the service, OIDC/IAM/audience is wrong, Cloud Run
returns 4xx/5xx, or the container fails before app-level Telegram fallback code
can run.

Required setup:

- keep `CLOUD_RUN_SERVICES`, `CLOUD_RUN_SERVICE`, `CLOUD_RUN_SERVICE_TARGETS_JSON`,
  or `RUNTIME_GUARD_CLOUD_RUN_SERVICES` populated with the service names to monitor
- grant the GitHub deploy service account `roles/logging.viewer` on
  `interactivebrokersquant`
- keep Telegram chat/token variables or secrets configured in GitHub
- optionally set `RUNTIME_GUARD_SCHEDULER_JOB_PATTERN` to a regex that limits
  Scheduler log checks to this deployment's jobs

The scheduled guard runs every 30 minutes. For a missed-run heartbeat, set
`RUNTIME_GUARD_REQUIRE_SUCCESS=true` and choose
`RUNTIME_GUARD_LOOKBACK_MINUTES` so the window covers the expected Scheduler run.
The default leaves the heartbeat check off to avoid false alerts outside the
active trading window.

`Execution Report Heartbeat` (`.github/workflows/execution-report-heartbeat.yml`)
is the stricter completion check. It runs on weekdays after the expected market
window and verifies that a recent runtime report exists under
`EXECUTION_REPORT_GCS_URI`. It reads the latest report JSON and alerts if no
recent report exists or the recent reports have rejected statuses such as
`error`. The deploy service account needs object read/list access on the report
bucket.
For slot deployments, `CLOUD_RUN_SERVICE_TARGETS_JSON` is used to require a
recent acceptable report for each configured service; set
`RUNTIME_HEARTBEAT_REQUIRED_SERVICES` when only a subset should be monitored.

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
5. **Cloud Scheduler**: Create two jobs that POST to the Cloud Run URL. Use `"/precheck"` after the open window and `"/"` near the close window. Choose both crons from the selected strategy-layer cadence; US daily profiles can still use a near-close weekday schedule such as `45 15 * * 1-5` in `America/New_York`, while HK profiles should use an `Asia/Hong_Kong` schedule aligned with XHKG.
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
