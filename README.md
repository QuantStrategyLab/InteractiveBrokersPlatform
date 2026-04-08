# InteractiveBrokersPlatform

![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![Platform](https://img.shields.io/badge/Broker-Interactive%20Brokers-red)
![Strategy](https://img.shields.io/badge/Strategy-US%20Equity%20Profiles-green)
![GCP](https://img.shields.io/badge/GCP-Cloud%20Run%20%2B%20GCE-4285F4)

[English](#english) | [中文](#中文)

---

<a id="english"></a>
## English

IBKR runtime for shared `us_equity` strategy profiles from `UsEquityStrategies`. Today it supports:

- `global_etf_rotation` (`Global ETF Rotation Defense`): quarterly ETF momentum rotation with daily canary defense
- `russell_1000_multi_factor_defensive` (`Russell 1000 Multi-Factor Defensive`): monthly stock-selection strategy that consumes a precomputed feature snapshot
- `tech_pullback_cash_buffer` (`Tech Pullback Cash Buffer`): monthly tech-heavy stock-selection branch with explicit BOXX cash buffer

Current strategy implementations are sourced from `UsEquityStrategies`.

Full strategy documentation now lives in [`UsEquityStrategies`](https://github.com/QuantStrategyLab/UsEquityStrategies). The strategy section below is kept as an execution-side summary.
This runtime matrix is the authoritative enablement source for IBKR. `UsEquityStrategies` only describes strategy-layer compatibility and human-readable metadata.

### Execution boundary

The mainline runtime now follows one path only:

- `main.py` assembles platform inputs into `StrategyContext`
- `strategy_runtime.py` loads the unified strategy entrypoint
- `entrypoint.evaluate(ctx)` returns a shared `StrategyDecision`
- `decision_mapper.py` maps that decision into IBKR orders, notifications, and runtime updates

`main.py` no longer reads private strategy constants or platform-only fields from strategy return payloads.

### Strategy

**Supported `STRATEGY_PROFILE` values**

- `global_etf_rotation`
- `russell_1000_multi_factor_defensive`
- `tech_pullback_cash_buffer`


**IBKR profile status**

| Canonical profile | Display name | Eligible | Enabled | Default | Rollback | Domain | Runtime note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `global_etf_rotation` | Global ETF Rotation Defense | Yes | Yes | Yes | Yes | `us_equity` | current rollback line |
| `russell_1000_multi_factor_defensive` | Russell 1000 Multi-Factor Defensive | Yes | Yes | No | No | `us_equity` | defensive stock baseline |
| `tech_pullback_cash_buffer` | Tech Pullback Cash Buffer | Yes | Yes | No | No | `us_equity` | current IBKR paper dry-run candidate |

Check the current matrix locally:

```bash
python3 scripts/print_strategy_profile_status.py
```

**Pool (22 ETFs + 1 safe haven):**

| Category | Tickers |
|----------|---------|
| Asia | EWY (Korea), EWT (Taiwan), INDA (India), FXI (China), EWJ (Japan) |
| Europe | VGK |
| US Broad Market | VOO (S&P 500) |
| US Tech | XLK (Technology Select Sector) |
| Semiconductors | SMH (Semiconductor ETF) |
| Commodities | GLD (Gold), SLV (Silver), USO (Oil), DBA (Agriculture) |
| US Cyclical | XLE (Energy), XLF (Financials), ITA (Aerospace/Defense) |
| US Defensive | XLP (Consumer Staples), XLU (Utilities), XLV (Healthcare), IHI (Medical Devices) |
| Real Estate / Banks | VNQ (REITs), KRE (Regional Banks) |
| Safe Haven | BIL (Short-term Treasury) |

**Rules:**
- **Momentum**: 13612W formula (Keller): `(12×R1M + 4×R3M + 2×R6M + R12M) / 19`
- **Trend filter**: Price > 200-day SMA
- **Hold bonus**: Existing holdings get +2% momentum bonus (reduces turnover)
- **Selection**: Top 2 by momentum, equal weight (50/50)
- **Safe haven**: Positions not filled → BIL
- **Rebalance**: Quarterly (last trading day of Mar, Jun, Sep, Dec)
- **Canary emergency**: Daily check of SPY/EFA/EEM/AGG — if all 4 have negative momentum → 100% BIL immediately

**Current default backtest (aligned window: 2012-02-03 to 2026-03-25, `VOO + XLK + SMH` included):**
- CAGR: 11.6% | Max Drawdown: 23.3%
- Sharpe: 0.70
- 2022: +3.1%
- 2023+ CAGR: 29.2% | Max Drawdown: 20.9%
- Legacy non-tech baseline and prior `QQQ` default remain available in the research script for comparison

### Snapshot-based stock profile (`russell_1000_multi_factor_defensive`)

- Signal source: precomputed feature snapshot file
- Default benchmark: `SPY`
- Default safe haven: `BOXX`
- Runtime expectation:
  - the feature snapshot is produced upstream
  - Cloud Run / local runtime reads the latest file from `IBKR_FEATURE_SNAPSHOT_PATH`
- Required snapshot columns:
  - `symbol`, `sector`, `mom_6_1`, `mom_12_1`, `sma200_gap`, `vol_63`, `maxdd_126`
  - optional passthrough columns such as `as_of`, `close`, `volume`, `adv20_usd`, `history_days`, `eligible`

Recommended upstream task:

```bash
PYTHONPATH=src:. python3 scripts/run_russell_1000_snapshot_task.py
```

Then point this runtime at the generated file:

```bash
STRATEGY_PROFILE=russell_1000_multi_factor_defensive
IBKR_FEATURE_SNAPSHOT_PATH=/var/data/r1000_feature_snapshot.csv
```

### Architecture

```
Cloud Scheduler (daily, 15:45 ET on weekdays)
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

Telegram alerts with i18n support (en/zh).

**Rebalance:**
```
🔔 【Trade Execution Report】
Equity: $2,000.00 | Buying Power: $1,950.00
━━━━━━━━━━━━━━━━━━
  EWY: 10股 $500.00
  SLV: 15股 $450.00
━━━━━━━━━━━━━━━━━━
🐤 SPY:✅(0.05), EFA:✅(0.03), EEM:❌(-0.01), AGG:✅(0.02)
🎯 📊 Quarterly Rebalance: Top 2 rotation
  Top: GLD(0.045), XLE(0.038)
━━━━━━━━━━━━━━━━━━
📉 [Market sell] EWY: 10 shares ✅ submitted (ID: 123)
📉 [Market sell] SLV: 15 shares ✅ submitted (ID: 124)
📈 [Limit buy] GLD: 3 shares @ $198.50 ✅ submitted (ID: 125)
📈 [Limit buy] XLE: 5 shares @ $95.20 ✅ submitted (ID: 126)
```

**Heartbeat (daily, canary OK):**
```
💓 【Heartbeat】
Equity: $2,100.00 | Buying Power: $50.00
━━━━━━━━━━━━━━━━━━
  GLD: 3股 $595.50
  XLE: 5股 $476.00
━━━━━━━━━━━━━━━━━━
🐤 SPY:✅(0.04), EFA:✅(0.02), EEM:✅(0.01), AGG:✅(0.03)
🎯 📋 Daily Check: canary OK, holding
━━━━━━━━━━━━━━━━━━
✅ No rebalance needed
```

### Runtime env vars

The selected `ACCOUNT_GROUP` is now the runtime identity. Keep broker-specific identity in the account-group config payload, not in Cloud Run env vars.

| Variable | Required | Description |
|----------|----------|-------------|
| `IB_GATEWAY_ZONE` | Optional fallback | GCE zone (for example `us-central1-a`). Recommended to keep in the selected account-group entry; this env var is only a transition fallback. |
| `IB_GATEWAY_IP_MODE` | Optional fallback | `internal` (default) or `external`. Recommended to keep in the selected account-group entry; this env var is only a transition fallback. |
| `STRATEGY_PROFILE` | Yes | Strategy profile selector. Supported `us_equity` values: `global_etf_rotation`, `russell_1000_multi_factor_defensive`, `tech_pullback_cash_buffer` |
| `ACCOUNT_GROUP` | Yes | Account-group selector. No default fallback. |
| `IBKR_FEATURE_SNAPSHOT_PATH` | Conditionally required | Required when `STRATEGY_PROFILE=russell_1000_multi_factor_defensive`. Path to the latest feature snapshot file (`.csv`, `.json`, `.jsonl`, `.parquet`). |
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
STRATEGY_PROFILE=tech_pullback_cash_buffer
ACCOUNT_GROUP=default
IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME=ibkr-account-groups
IBKR_FEATURE_SNAPSHOT_PATH=/var/data/tech_pullback_cash_buffer_feature_snapshot_latest.csv
IBKR_FEATURE_SNAPSHOT_MANIFEST_PATH=/var/manifests/tech_pullback_cash_buffer_feature_snapshot_latest.csv.manifest.json
IBKR_DRY_RUN_ONLY=true
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
      "service_name": "interactive-brokers-quant-global-etf-rotation",
      "account_ids": ["DU1234567"]
    },
    "ira": {
      "ib_gateway_instance_name": "interactive-brokers-quant-instance",
      "ib_gateway_zone": "us-central1-c",
      "ib_gateway_mode": "paper",
      "ib_gateway_ip_mode": "internal",
      "ib_client_id": 7,
      "service_name": "interactive-brokers-quant-global-etf-rotation-ira",
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
  - `STRATEGY_PROFILE` (recommended: `global_etf_rotation`)
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

`STRATEGY_PROFILE` is now resolved from a platform capability matrix plus a rollout allowlist. The current strategy domain is `us_equity`, and the repo keeps the runtime registry thin: `eligible` means the platform can run the strategy in theory, while `enabled` means the current rollout really allows it. `ACCOUNT_GROUP` now selects one account-group config entry, and the service fails fast if that runtime identity is incomplete.

Important:

- The workflow only becomes strict when `ENABLE_GITHUB_ENV_SYNC=true`. If this variable is unset, the sync job is skipped.
- Here "shared config" still only means the **IBKR pair** (`InteractiveBrokersPlatform` + `IBKRGatewayManager`). `TELEGRAM_TOKEN` and `TELEGRAM_TOKEN_SECRET_NAME` remain repository-specific.
- If `IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME` is set, the Cloud Run runtime needs Secret Manager access to that secret.
- GitHub now authenticates to Google Cloud with OIDC + Workload Identity Federation, so `GCP_SA_KEY` is no longer required for this workflow.

### Deployment unit and naming

- `QuantPlatformKit` is only a shared dependency; Cloud Run now deploys `InteractiveBrokersPlatform`.
- Recommended Cloud Run service name: `interactive-brokers-quant-global-etf-rotation-service`.
- For future multi-account rollout, keep one Cloud Run service per `ACCOUNT_GROUP`, and let each service select its account-group config at runtime.
- If you later rename or move this repository, reselect the GitHub source in Cloud Build / Cloud Run trigger instead of assuming the existing source binding will update itself.
- For the shared deployment model and trigger migration checklist, see [`QuantPlatformKit/docs/deployment_model.md`](../QuantPlatformKit/docs/deployment_model.md).

### Deployment

1. **GCE**: Set up IB Gateway (paper or live) on a GCE instance. Ensure API access is enabled, remote clients are allowed when needed, and use `4001` for `live` or `4002` for `paper`.
2. **VPC / Subnet**: Put Cloud Run and GCE in the same VPC. For cleaner firewall rules, reserve a dedicated subnet for Cloud Run Direct VPC egress.
3. **Cloud Run**: Deploy or update this Flask app with Direct VPC egress. Set `STRATEGY_PROFILE`, `ACCOUNT_GROUP`, and `IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME`. Keep `IB_GATEWAY_ZONE` / `IB_GATEWAY_IP_MODE` only as transition fallbacks if the selected account-group payload does not already contain them. The runtime service account needs `roles/secretmanager.secretAccessor` and, for instance-name resolution, `roles/compute.viewer`.
4. **Firewall**: Allow TCP `4001` (`live`) or `4002` (`paper`) from the Cloud Run egress subnet CIDR to the GCE instance.
5. **Cloud Scheduler**: Create a job: `45 15 * * 1-5` (America/New_York), POST to the Cloud Run URL. The code handles market calendar checks internally.
6. **Optional public-IP mode**: Only if you cannot use VPC, set `IB_GATEWAY_IP_MODE=external`, expose the GCE public IP deliberately, and restrict source ranges tightly. This is not the default path.

Example deploy/update command:

```bash
gcloud run deploy interactive-brokers-quant-global-etf-rotation-service \
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

基于 IBKR 的全球 ETF 季度轮动策略（国际市场、商品、美股行业、美股宽基、科技和半导体），含每日金丝雀应急机制。定位上比 `TQQQ`、`SOXL` 这类高弹性科技策略更稳健，但不再把科技完全排除在外。部署在 GCP Cloud Run，连接 GCE 上的 IB Gateway。

当前 `global_etf_rotation`、`russell_1000_multi_factor_defensive` 和 `tech_pullback_cash_buffer` 的策略实现都来自 `UsEquityStrategies`。

完整策略说明现在放在 [`UsEquityStrategies`](https://github.com/QuantStrategyLab/UsEquityStrategies#global_etf_rotation)。下面的策略章节主要保留执行侧摘要。

### 执行边界

当前主线运行路径已经统一为：

- `main.py` 负责把平台输入组装成 `StrategyContext`
- `strategy_runtime.py` 负责加载统一策略入口
- `entrypoint.evaluate(ctx)` 返回共享的 `StrategyDecision`
- `decision_mapper.py` 再把决策映射成 IBKR 订单、通知和运行时更新

`main.py` 已经不再直接读取策略私有常量，也不再依赖策略返回里的平台专属字段。

### 策略

**选池 (22只 + 1只避险):**

| 类别 | 代码 |
|------|------|
| 亚洲 | EWY(韩国), EWT(台湾), INDA(印度), FXI(中国), EWJ(日本) |
| 欧洲 | VGK |
| 美股宽基 | VOO(S&P 500) |
| 美股科技 | XLK(科技板块) |
| 半导体 | SMH(半导体 ETF) |
| 商品 | GLD(黄金), SLV(白银), USO(石油), DBA(农产品) |
| 美股周期 | XLE(能源), XLF(金融), ITA(国防航空) |
| 美股防御 | XLP(必需消费), XLU(公用事业), XLV(医疗), IHI(医疗器械) |
| 地产/银行 | VNQ(REITs), KRE(区域银行) |
| 避险 | BIL(超短期国债) |

**规则:**
- **动量**: 13612W 公式: `(12×R1M + 4×R3M + 2×R6M + R12M) / 19`
- **趋势过滤**: 价格 > SMA200
- **持仓惯性**: 已持有标的 +2% 动量加分
- **选股**: Top 2，各 50%
- **避险**: 不足2只通过 → 空位转 BIL
- **调仓**: 季度（3/6/9/12月最后一个交易日）
- **金丝雀应急**: 每日检查 SPY/EFA/EEM/AGG — 4个全部动量为负 → 立即 100% BIL

**当前默认版本回测 (`VOO + XLK + SMH` 已纳入，公共区间: 2012-02-03 到 2026-03-25):**
- CAGR: 11.6% | 最大回撤: 23.3%
- Sharpe: 0.70
- 2022: +3.1%
- 2023+ CAGR: 29.2% | 最大回撤: 20.9%
- 如需对比旧版“非科技基线”和之前的 `QQQ` 默认版，可以直接运行研究脚本

### 架构

```
Cloud Scheduler (每个交易日 15:45 ET)
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
| `STRATEGY_PROFILE` | 是 | 策略档位选择。当前可用的 `us_equity` 值：`global_etf_rotation`、`russell_1000_multi_factor_defensive`、`tech_pullback_cash_buffer` |
| `ACCOUNT_GROUP` | 是 | 账号组选择器，不再提供默认回退。 |
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
STRATEGY_PROFILE=global_etf_rotation
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
      "service_name": "interactive-brokers-quant-global-etf-rotation",
      "account_ids": ["DU1234567"]
    },
    "ira": {
      "ib_gateway_instance_name": "interactive-brokers-quant-instance",
      "ib_gateway_zone": "us-central1-c",
      "ib_gateway_mode": "paper",
      "ib_gateway_ip_mode": "internal",
      "ib_client_id": 7,
      "service_name": "interactive-brokers-quant-global-etf-rotation-ira",
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
  - `STRATEGY_PROFILE`（建议设为 `global_etf_rotation`）
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

`STRATEGY_PROFILE` 现在由平台能力矩阵和 rollout allowlist 一起决定。当前策略域仍是 `us_equity`：`eligible` 表示平台理论上能跑，`enabled` 表示当前 rollout 真正放开。`ACCOUNT_GROUP` 是严格必填项，并会选中一份账号组配置。运行身份不完整时，服务会直接失败，不再静默回退。

注意：

- 只有在 `ENABLE_GITHUB_ENV_SYNC=true` 时，这个 workflow 才会严格校验并执行同步。没打开时会直接跳过。
- 这里说的“共享配置”仍然只针对 **IBKR 这一组系统**。`TELEGRAM_TOKEN` 和 `TELEGRAM_TOKEN_SECRET_NAME` 都还是这个仓库自己的配置，不建议提升成所有 quant 共用的全局配置。
- 如果设置了 `IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME`，Cloud Run 运行时还需要有对应 Secret 的访问权限。
- GitHub 现在通过 OIDC + Workload Identity Federation 登录 Google Cloud，这个 workflow 不再需要 `GCP_SA_KEY`。

### 部署单元和命名建议

- `QuantPlatformKit` 只是共享依赖，不单独部署；Cloud Run 现在部署的是 `InteractiveBrokersPlatform`。
- 推荐 Cloud Run 服务名：`interactive-brokers-quant-global-etf-rotation-service`。
- 后续如果扩到多账户，建议按 `ACCOUNT_GROUP` 拆成多个 Cloud Run 服务，并让每个服务在运行时选中自己的账号组配置。
- 如果后面改 GitHub 仓库名或再次迁组织，Cloud Build / Cloud Run 里的 GitHub 来源需要重新选择，不要假设旧绑定会自动跟过去。
- 统一部署模型和触发器迁移清单见 [`QuantPlatformKit/docs/deployment_model.md`](../QuantPlatformKit/docs/deployment_model.md)。

### 部署

1. **GCE**: 部署 IB Gateway（模拟或实盘），确认 API 已开启、需要远程连接时已允许非 localhost 客户端，并确认 `live` 使用 `4001`、`paper` 使用 `4002`。
2. **VPC / 子网**: 让 Cloud Run 和 GCE 处于同一个 VPC。为了让防火墙规则更干净，建议给 Cloud Run Direct VPC egress 单独准备一个子网。
3. **Cloud Run**: 部署此 Flask 应用时启用 Direct VPC egress。设置 `STRATEGY_PROFILE`、`ACCOUNT_GROUP`、`IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME`；只有在账号组配置里还没放 `ib_gateway_zone` / `ib_gateway_ip_mode` 时，才临时保留 `IB_GATEWAY_ZONE` / `IB_GATEWAY_IP_MODE` 作为过渡 fallback。runtime service account 需要 `roles/secretmanager.secretAccessor`，若走实例名解析，还需要 `roles/compute.viewer`。
4. **防火墙**: 只允许 Cloud Run 出口子网访问 GCE 的 `TCP 4001`（`live`）或 `TCP 4002`（`paper`）。
5. **Cloud Scheduler**: 创建定时任务 `45 15 * * 1-5`（America/New_York 时区），POST 到 Cloud Run URL。代码内部处理交易日判断。
6. **可选公网模式**: 只有在不能走 VPC 时，才设置 `IB_GATEWAY_IP_MODE=external`，并且要明确开放 GCE 公网 IP，同时严格限制来源 IP 和防火墙规则。

示例部署命令：

```bash
gcloud run deploy interactive-brokers-quant-global-etf-rotation-service \
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

### Research / 回测

可以用独立脚本对比旧版非科技基线、当前默认策略，以及 `QQQ` 和 `VOO/XLK/SMH` 的研究方案：

```bash
python3 research/backtest_qqq_variants.py
```

默认会比较：

- 旧版非科技轮动
- 当前默认策略：`VOO + XLK + SMH` 加入统一轮动池参与 `Top 2`
- `QQQ` 默认版、`VOO` 替换版，以及逐步加入 `XLK / SMH` 的拆解对比
- 固定 `20% / 30% / 40%` 的 `QQQ` 核心仓位参考方案

脚本使用 `yfinance` 的复权收盘价，并自动把回测起点对齐到所有标的都有历史数据的最早公共日期。
