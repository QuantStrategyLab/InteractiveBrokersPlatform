# IBKR Global ETF Rotation

![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![Platform](https://img.shields.io/badge/Broker-Interactive%20Brokers-red)
![Strategy](https://img.shields.io/badge/Strategy-Global%20ETF%20Rotation-green)
![GCP](https://img.shields.io/badge/GCP-Cloud%20Run%20%2B%20GCE-4285F4)

[English](#english) | [中文](#中文)

---

<a id="english"></a>
## English

Quarterly momentum rotation across 22 global ETFs (international markets, commodities, US sectors, US broad market, tech, and semiconductors) with daily canary emergency check. Designed to stay more stable than high-beta tech strategies while still allowing major tech leadership to enter the rotation. Deployed on GCP Cloud Run, connecting to IB Gateway on GCE.

### Strategy

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

### Architecture

```
Cloud Scheduler (daily, 15:45 ET on weekdays)
    ↓ HTTP POST
Cloud Run (Flask: strategy + orders)
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

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `IB_GATEWAY_HOST` | Conditional | Legacy explicit host or IP. If not set, the app falls back to `IB_GATEWAY_INSTANCE_NAME`. |
| `IB_GATEWAY_INSTANCE_NAME` | Conditional | Recommended shared config name for the GCE instance. Useful when Gateway and Quant share the same GitHub-managed config. |
| `IB_GATEWAY_ZONE` | Yes | GCE zone (e.g. `us-central1-a`) |
| `IB_GATEWAY_IP_MODE` | No | `internal` (default) or `external`; for Cloud Run, `internal` with Direct VPC egress is recommended |
| `IB_GATEWAY_MODE` | No | Recommended shared mode flag. `live` maps to port `4001`, `paper` maps to port `4002`. |
| `IB_GATEWAY_PORT` | No | Legacy explicit port override. If set together with `IB_GATEWAY_MODE`, it must match (`live`=`4001`, `paper`=`4002`). |
| `IB_CLIENT_ID` | No | IB client ID (default: 1) |
| `TELEGRAM_TOKEN` | Yes | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Conditional | Per-service Telegram chat ID. If not set, the app falls back to `GLOBAL_TELEGRAM_CHAT_ID`. |
| `GLOBAL_TELEGRAM_CHAT_ID` | No | Optional shared Telegram chat ID for teams that keep one common destination across multiple quant services. |
| `NOTIFY_LANG` | No | `en` (default) or `zh` |

`IB_GATEWAY_HOST` and `IB_GATEWAY_INSTANCE_NAME` are backward-compatible alternatives; one of them must be set. If you use instance-name resolution with `IB_GATEWAY_ZONE`, the service account needs `roles/compute.viewer`. The recommended deployment is Cloud Run with Direct VPC egress to the GCE private IP. Set `IB_GATEWAY_IP_MODE=external` only if you intentionally expose the gateway over a public IP and have locked down API access and firewall rules.

**Recommended shared-config mode**

If you deploy both `IBKRQuant` and `IBKRGatewayManager` from the same GitHub-managed config, keep these shared values aligned:

```bash
IB_GATEWAY_INSTANCE_NAME=interactive-brokers-quant-instance
IB_GATEWAY_ZONE=us-central1-c
IB_GATEWAY_MODE=paper
IB_GATEWAY_IP_MODE=internal
IB_CLIENT_ID=1
NOTIFY_LANG=zh
```

In this mode, you do not need to set `IB_GATEWAY_PORT` manually; the app derives it from `IB_GATEWAY_MODE`.

### Deployment

1. **GCE**: Set up IB Gateway (paper or live) on a GCE instance. Ensure API access is enabled, remote clients are allowed when needed, and use `4001` for `live` or `4002` for `paper`.
2. **VPC / Subnet**: Put Cloud Run and GCE in the same VPC. For cleaner firewall rules, reserve a dedicated subnet for Cloud Run Direct VPC egress.
3. **Cloud Run**: Deploy or update this Flask app with Direct VPC egress. You can either keep the legacy pair `IB_GATEWAY_HOST + IB_GATEWAY_PORT`, or use the shared-config pair `IB_GATEWAY_INSTANCE_NAME + IB_GATEWAY_MODE`. In both cases keep `IB_GATEWAY_ZONE` and `IB_GATEWAY_IP_MODE=internal`.
4. **Firewall**: Allow TCP `4001` (`live`) or `4002` (`paper`) from the Cloud Run egress subnet CIDR to the GCE instance.
5. **Cloud Scheduler**: Create a job: `45 15 * * 1-5` (America/New_York), POST to the Cloud Run URL. The code handles market calendar checks internally.
6. **Optional public-IP mode**: Only if you cannot use VPC, set `IB_GATEWAY_IP_MODE=external`, expose the GCE public IP deliberately, and restrict source ranges tightly. This is not the default path.

Example deploy/update command:

```bash
gcloud run deploy ibkr-quant \
  --source . \
  --region us-central1 \
  --network default \
  --subnet cloudrun-direct-egress \
  --vpc-egress private-ranges-only \
  --set-env-vars IB_GATEWAY_INSTANCE_NAME=ib-gateway,IB_GATEWAY_ZONE=us-central1-a,IB_GATEWAY_MODE=paper,IB_GATEWAY_IP_MODE=internal
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
Cloud Run (Flask: 策略计算 + 下单)
    ↓ ib_insync TCP
GCE (IB Gateway 常驻)
    ↓
IBKR 账户
```

### 环境变量

| 变量 | 必需 | 说明 |
|------|------|------|
| `IB_GATEWAY_HOST` | 条件必需 | 旧写法，直接填主机名或 IP。不填时会回退到 `IB_GATEWAY_INSTANCE_NAME`。 |
| `IB_GATEWAY_INSTANCE_NAME` | 条件必需 | 推荐的共享配置写法，填 GCE 实例名。适合和 Gateway 共用一套 GitHub 配置。 |
| `IB_GATEWAY_ZONE` | 是 | GCE zone (如 `us-central1-a`) |
| `IB_GATEWAY_IP_MODE` | 否 | `internal`（默认）或 `external`；Cloud Run 推荐配合 Direct VPC egress 使用 `internal` |
| `IB_GATEWAY_MODE` | 否 | 推荐的共享模式变量。`live` 会映射到 `4001`，`paper` 会映射到 `4002`。 |
| `IB_GATEWAY_PORT` | 否 | 旧写法，直接指定端口。如果和 `IB_GATEWAY_MODE` 同时配置，必须一致。 |
| `IB_CLIENT_ID` | 否 | IB 连接客户端 ID (默认: 1) |
| `TELEGRAM_TOKEN` | 是 | Telegram 机器人 Token |
| `TELEGRAM_CHAT_ID` | 条件必需 | 当前服务自己的 Telegram Chat ID。不填时会回退到 `GLOBAL_TELEGRAM_CHAT_ID`。 |
| `GLOBAL_TELEGRAM_CHAT_ID` | 否 | 可选的共享 Telegram Chat ID。适合多个 quant 服务共用一个接收目标。 |
| `NOTIFY_LANG` | 否 | `en`(默认) 或 `zh` |

`IB_GATEWAY_HOST` 和 `IB_GATEWAY_INSTANCE_NAME` 是兼容关系，二选一即可。如果你配了 `IB_GATEWAY_ZONE` 让程序通过实例名解析内网 IP，Cloud Run service account 需要 `roles/compute.viewer` 权限。推荐做法是 Cloud Run 通过 Direct VPC egress 访问 GCE 内网地址。只有在你明确要走公网暴露的 GCE 时，才设置 `IB_GATEWAY_IP_MODE=external`。

**推荐的共享配置模式**

如果你和现在这套部署一样，希望 `IBKRQuant` 和 `IBKRGatewayManager` 共用一套 GitHub 配置，建议统一维护下面这些值：

```bash
IB_GATEWAY_INSTANCE_NAME=interactive-brokers-quant-instance
IB_GATEWAY_ZONE=us-central1-c
IB_GATEWAY_MODE=paper
IB_GATEWAY_IP_MODE=internal
IB_CLIENT_ID=1
NOTIFY_LANG=zh
```

这种写法下，不需要再手工维护 `IB_GATEWAY_PORT`，程序会按 `IB_GATEWAY_MODE` 自动推导。

### 部署

1. **GCE**: 部署 IB Gateway（模拟或实盘），确认 API 已开启、需要远程连接时已允许非 localhost 客户端，并确认 `live` 使用 `4001`、`paper` 使用 `4002`。
2. **VPC / 子网**: 让 Cloud Run 和 GCE 处于同一个 VPC。为了让防火墙规则更干净，建议给 Cloud Run Direct VPC egress 单独准备一个子网。
3. **Cloud Run**: 部署此 Flask 应用时启用 Direct VPC egress。你可以继续用旧写法 `IB_GATEWAY_HOST + IB_GATEWAY_PORT`，也可以改成推荐的共享写法 `IB_GATEWAY_INSTANCE_NAME + IB_GATEWAY_MODE`。两种方式都要保留 `IB_GATEWAY_ZONE` 和 `IB_GATEWAY_IP_MODE=internal`。Service account 需要 `roles/compute.viewer` 权限。
4. **防火墙**: 只允许 Cloud Run 出口子网访问 GCE 的 `TCP 4001`（`live`）或 `TCP 4002`（`paper`）。
5. **Cloud Scheduler**: 创建定时任务 `45 15 * * 1-5`（America/New_York 时区），POST 到 Cloud Run URL。代码内部处理交易日判断。
6. **可选公网模式**: 只有在不能走 VPC 时，才设置 `IB_GATEWAY_IP_MODE=external`，并且要明确开放 GCE 公网 IP，同时严格限制来源 IP 和防火墙规则。

示例部署命令：

```bash
gcloud run deploy ibkr-quant \
  --source . \
  --region us-central1 \
  --network default \
  --subnet cloudrun-direct-egress \
  --vpc-egress private-ranges-only \
  --set-env-vars IB_GATEWAY_INSTANCE_NAME=ib-gateway,IB_GATEWAY_ZONE=us-central1-a,IB_GATEWAY_MODE=paper,IB_GATEWAY_IP_MODE=internal
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
