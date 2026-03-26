# IBKR Global Non-Tech Sector Rotation

![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![Platform](https://img.shields.io/badge/Broker-Interactive%20Brokers-red)
![Strategy](https://img.shields.io/badge/Strategy-Non--Tech%20Rotation-green)
![GCP](https://img.shields.io/badge/GCP-Cloud%20Run%20%2B%20GCE-4285F4)

[English](#english) | [中文](#中文)

---

<a id="english"></a>
## English

Quarterly momentum rotation across 19 global non-tech ETFs (international markets, commodities, and US non-tech sectors) with daily canary emergency check. Designed as a complement to tech-heavy strategies (TQQQ/SOXL). Deployed on GCP Cloud Run, connecting to IB Gateway on GCE.

### Strategy

**Pool (19 ETFs + 1 safe haven):**

| Category | Tickers |
|----------|---------|
| Asia | EWY (Korea), EWT (Taiwan), INDA (India), FXI (China), EWJ (Japan) |
| Europe | VGK |
| Commodities | GLD (Gold), SLV (Silver), USO (Oil), DBA (Agriculture) |
| US Offensive | XLE (Energy), XLF (Financials), ITA (Aerospace/Defense) |
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

**Backtest (25Y: 2001-2026):**
- CAGR: 11.9% | Max Drawdown: 33.1%
- Beats SPY in 13/25 years (52%)
- TQQQ correlation: 0.33 (low — complementary to tech-heavy strategies)
- 2008 crisis: -23.3% (vs SPY -36.8%, without canary would be -50%)
- 2022: +21.5% (vs SPY -18.2%)
- 2025: +46.5% (silver + Korea)

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
| `IB_GATEWAY_HOST` | Yes | GCE instance name (e.g. `ib-gateway`) |
| `IB_GATEWAY_ZONE` | Yes | GCE zone (e.g. `us-central1-a`) |
| `IB_GATEWAY_PORT` | No | IB Gateway port (default: 4001) |
| `IB_CLIENT_ID` | No | IB client ID (default: 1) |
| `TELEGRAM_TOKEN` | Yes | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Yes | Telegram chat ID |
| `NOTIFY_LANG` | No | `en` (default) or `zh` |

Instance name is resolved to internal IP via Compute API at startup. Service account needs `roles/compute.viewer`.

### Deployment

1. **GCE**: Set up IB Gateway (paper or live) on a GCE instance, ensure port 4001 is accessible from Cloud Run via VPC connector.
2. **Cloud Run**: Deploy this Flask app. Set `IB_GATEWAY_HOST` to GCE instance name and `IB_GATEWAY_ZONE` to its zone. Service account needs `roles/compute.viewer` for instance name resolution.
3. **VPC**: Create a Serverless VPC Access connector in the same network as GCE. Attach to Cloud Run deployment.
4. **Firewall**: Allow TCP 4001 from VPC connector subnet to GCE instance.
5. **Cloud Scheduler**: Create a job: `45 15 * * 1-5` (America/New_York), POST to Cloud Run URL. Code handles market calendar check internally.

---

<a id="中文"></a>
## 中文

基于 IBKR 的全球非科技板块季度轮动策略（国际市场、商品、美股非科技行业），含每日金丝雀应急机制。作为科技杠杆策略（TQQQ/SOXL）的互补。部署在 GCP Cloud Run，连接 GCE 上的 IB Gateway。

### 策略

**选池 (19只 + 1只避险):**

| 类别 | 代码 |
|------|------|
| 亚洲 | EWY(韩国), EWT(台湾), INDA(印度), FXI(中国), EWJ(日本) |
| 欧洲 | VGK |
| 商品 | GLD(黄金), SLV(白银), USO(石油), DBA(农产品) |
| 美股进攻 | XLE(能源), XLF(金融), ITA(国防航空) |
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

**回测 (25年: 2001-2026):**
- CAGR: 11.9% | 最大回撤: 33.1%
- 跑赢 SPY: 13/25年 (52%)
- 与 TQQQ 相关性: 0.33（低，与科技策略互补）
- 2008 金融危机: -23.3%（SPY -36.8%，无金丝雀为 -50%）
- 2022: +21.5%（SPY -18.2%）
- 2025: +46.5%（白银+韩国）

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
| `IB_GATEWAY_HOST` | 是 | GCE 实例名称 (如 `ib-gateway`) |
| `IB_GATEWAY_ZONE` | 是 | GCE zone (如 `us-central1-a`) |
| `IB_GATEWAY_PORT` | 否 | IB Gateway 端口 (默认: 4001) |
| `IB_CLIENT_ID` | 否 | IB 连接客户端 ID (默认: 1) |
| `TELEGRAM_TOKEN` | 是 | Telegram 机器人 Token |
| `TELEGRAM_CHAT_ID` | 是 | Telegram Chat ID |
| `NOTIFY_LANG` | 否 | `en`(默认) 或 `zh` |

实例名称启动时通过 Compute API 自动解析为内网 IP。Service account 需要 `roles/compute.viewer` 权限。

### 部署

1. **GCE**: 部署 IB Gateway（模拟或实盘），确保 4001 端口对 VPC 内部开放。
2. **Cloud Run**: 部署此 Flask 应用，`IB_GATEWAY_HOST` 设为 GCE 实例名，`IB_GATEWAY_ZONE` 设为对应 zone。Service account 需要 `roles/compute.viewer` 权限。
3. **VPC**: 创建 Serverless VPC Access connector，与 GCE 在同一网络。部署 Cloud Run 时绑定该 connector。
4. **防火墙**: 允许 VPC connector 子网访问 GCE 的 TCP 4001 端口。
5. **Cloud Scheduler**: 创建定时任务 `45 15 * * 1-5`（America/New_York 时区），POST 到 Cloud Run URL。代码内部处理交易日判断。

### Research / 回测

可以用独立脚本对比原始策略和两种 `QQQ` 方案：

```bash
python3 research/backtest_qqq_variants.py
```

默认会比较：

- 原始非科技轮动
- `QQQ` 加入轮动池参与 `Top 2`
- 固定 `20% / 30% / 40%` 的 `QQQ` 核心仓位，其余仓位继续跑原策略

脚本使用 `yfinance` 的复权收盘价，并自动把回测起点对齐到所有标的都有历史数据的最早公共日期。
