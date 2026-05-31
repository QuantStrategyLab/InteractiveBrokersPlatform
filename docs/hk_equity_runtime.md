# IBKR 港股运行时接入说明

## 结论

QuantStrategyLab 现有平台仓库里，能接入港股股票交易的平台是 `InteractiveBrokersPlatform` 和 `LongBridgePlatform`。

| 平台仓库 | 港股交易接入判断 | 当前处理 |
| --- | --- | --- |
| `InteractiveBrokersPlatform` | 可接入。IBKR 支持 SEHK/HKD 合约，但账户必须开通港股交易和行情权限。 | 已加入 HK market scope、SEHK/HKD 合约参数、HKD portfolio 口径、通知和结构化日志字段。 |
| `LongBridgePlatform` | 可接入。LongBridge 支持港股账户、`.HK` 行情符号和 HKD 现金口径。 | 在对应仓库单独接入。 |
| `CharlesSchwabPlatform` | 不适合作为港股交易入口。 | 保持 US equity 边界，不改。 |
| `FirstradePlatform` | 不适合作为港股交易入口。 | 保持 US equity 边界，不改。 |
| `BinancePlatform` | 加密货币平台，不是港股股票交易入口。 | 不改。 |

## 运行时设计

本仓库只做券商运行时能力，不把港股策略逻辑硬编码进平台。当前已正式接入 `HkEquityStrategies` 的 `hk_blue_chip_leader_rotation` profile，并沿用美股 snapshot 策略的架构：

1. [`HkEquityStrategies`](https://github.com/QuantStrategyLab/HkEquityStrategies) 提供 `hk_equity` 策略 profile、运行入口和 IBKR runtime adapter。
2. [`HkEquitySnapshotPipelines`](https://github.com/QuantStrategyLab/HkEquitySnapshotPipelines) 发布 `<profile>_feature_snapshot_latest.csv`、manifest、ranking 和 release summary。
3. 平台仓库通过 `RUNTIME_TARGET_JSON`、`IBKR_FEATURE_SNAPSHOT_PATH`、`IBKR_FEATURE_SNAPSHOT_MANIFEST_PATH` 和可选 config 路径读取策略输入。
4. IBKR 运行时根据 market scope 选择 SEHK/HKD 合约、HKD 账户口径、XHKG 日历和通知/日志字段。

这样可以复用现有 US snapshot 的 artifact contract，同时保持平台仓只负责执行、账户、通知和运行报告。

## 已启用港股 profile

| Profile | Domain | Inputs | Target mode | Snapshot manifest |
| --- | --- | --- | --- | --- |
| `hk_blue_chip_leader_rotation` | `hk_equity` | `feature_snapshot` | `weight` | required |

最小策略配置示例：

```bash
STRATEGY_PROFILE=hk_blue_chip_leader_rotation
ACCOUNT_GROUP=hk-live
RUNTIME_TARGET_JSON={"platform_id":"ibkr","strategy_profile":"hk_blue_chip_leader_rotation","deployment_selector":"hk-live","account_scope":"hk-live","execution_mode":"live"}
IBKR_FEATURE_SNAPSHOT_PATH=gs://<bucket>/hk_blue_chip_leader_rotation_feature_snapshot_latest.csv
IBKR_FEATURE_SNAPSHOT_MANIFEST_PATH=gs://<bucket>/hk_blue_chip_leader_rotation_feature_snapshot_latest.csv.manifest.json
```

## 配置项

| 变量 | 默认值 | 港股建议值 | 说明 |
| --- | --- | --- | --- |
| `ACCOUNT_GROUP` | 必填 | 例如 `hk-live` | 包含 `hk` 时会推导港股默认 market scope。 |
| `IBKR_MARKET` | 从 `ACCOUNT_GROUP` 推导，默认 `US` | `HK` | 显式指定市场；优先级高于 `ACCOUNT_GROUP` 推导。 |
| `IBKR_MARKET_CALENDAR` | `NYSE` / 港股为 `XHKG` | `XHKG` | 市场日历。 |
| `IBKR_MARKET_TIMEZONE` | `America/New_York` / 港股为 `Asia/Hong_Kong` | `Asia/Hong_Kong` | 市场时区。 |
| `IBKR_MARKET_EXCHANGE` | `SMART` / 港股为 `SEHK` | `SEHK` | 股票合约交易所。 |
| `IBKR_MARKET_CURRENCY` | `USD` / 港股为 `HKD` | `HKD` | 合约币种、账户净值和购买力过滤口径。 |
| `IBKR_MARKET_DATA_SYMBOL_SUFFIX` | 空 / 港股为 `.HK` | `.HK` | yfinance fallback 行情符号后缀；IBKR 合约本身不附加该后缀。 |

最小港股配置：

```bash
ACCOUNT_GROUP=hk-live
# 可选显式覆盖：
IBKR_MARKET=HK
IBKR_MARKET_CALENDAR=XHKG
IBKR_MARKET_TIMEZONE=Asia/Hong_Kong
IBKR_MARKET_EXCHANGE=SEHK
IBKR_MARKET_CURRENCY=HKD
IBKR_MARKET_DATA_SYMBOL_SUFFIX=.HK
```

## 订单、组合和行情口径

- 股票订单通过 `Stock(symbol, IBKR_MARKET_EXCHANGE, IBKR_MARKET_CURRENCY)` 构造；港股默认是 `SEHK/HKD`。
- Portfolio snapshot 只汇总配置币种的 `NetLiquidation` 和 `AvailableFunds`；港股默认是 HKD。
- IBKR 历史行情和 quote snapshot 会使用配置的 exchange/currency。
- yfinance fallback 会给无后缀 symbol 追加 `IBKR_MARKET_DATA_SYMBOL_SUFFIX`，例如 `00700` -> `00700.HK`。

## 通知和日志

- Telegram 中英文模板新增市场行：市场、交易币种、交易所和日历。
- Runtime report / structured log context 新增：`market`、`market_calendar`、`market_currency`、`market_data_symbol_suffix`、`market_exchange`、`market_timezone`。
- 市场关闭跳过等事件会带上 market scope，便于区分 US/HK 服务。

## 风险和注意事项

- IBKR 港股实盘依赖账户权限、行情权限、Gateway 登录账户可见账号和交易许可；平台配置无法替代这些权限。
- 不同 IBKR 账户或区域对港股 symbol 格式可能有差异，首批上线前需要用 dry-run 和小范围 symbol 做实盘连接验证。
- `XHKG` 是否可用取决于部署环境里的 `pandas_market_calendars` 版本；如不可用，可用 `IBKR_MARKET_CALENDAR` 临时覆盖。
- `hk_blue_chip_leader_rotation` 已接入平台，但实盘前仍需要用最新 snapshot artifact、dry-run 和小范围 symbol / 小订单做连接验证。
