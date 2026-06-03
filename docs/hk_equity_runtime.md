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

本仓库只做券商运行时能力，不把港股策略逻辑硬编码进平台。当前已接入 `HkEquityStrategies` 的港股 profile 元数据，平台可选港股 profile 只暴露 `runtime_enabled` 的 `hk_global_etf_tactical_rotation`。`hk_blue_chip_leader_rotation` 是 snapshot 架构占位，`hk_index_mean_reversion`、`hk_etf_regime_rotation` 是 `market_history` 研究候选，均留在研究/快照仓库，不进入平台可选列表。Cloud Run 通过 `RUNTIME_TARGET_JSON` / `STRATEGY_PROFILE` 选择当前运行策略。整体沿用美股策略的架构：

1. [`HkEquityStrategies`](https://github.com/QuantStrategyLab/HkEquityStrategies) 提供非 snapshot `hk_equity` 策略 profile、运行入口和 IBKR runtime adapter。
2. [`HkEquitySnapshotPipelines`](https://github.com/QuantStrategyLab/HkEquitySnapshotPipelines) 发布 snapshot-backed profile 的 `<profile>_feature_snapshot_latest.csv`、manifest、ranking 和 release summary。
3. 非 snapshot profile 使用平台 market-data feed 提供的 `market_history`，不需要 snapshot artifact。
4. 平台仓库通过 `RUNTIME_TARGET_JSON`、snapshot/config 路径和平台 market scope 读取策略输入。
5. IBKR 运行时根据 market scope 选择 SEHK/HKD 合约、HKD 账户口径、XHKG 日历和通知/日志字段。

这样可以复用现有 US snapshot 的 artifact contract，同时保持平台仓只负责执行、账户、通知和运行报告。

## 港股 profile 当前状态

| Profile | Domain | Inputs | Target mode | Snapshot manifest | Status |
| --- | --- | --- | --- | --- | --- |
| `hk_global_etf_tactical_rotation` | `hk_equity` | `market_history` | `weight` | not required | runtime-enabled; platform-selectable |
| `hk_blue_chip_leader_rotation` | `hk_equity` | `feature_snapshot` | `weight` | required | snapshot scaffold; not platform-selectable |
| `hk_index_mean_reversion` | `hk_equity` | `market_history` | `weight` | not required | research/backtest only; not platform-selectable |
| `hk_etf_regime_rotation` | `hk_equity` | `market_history` | `weight` | not required | research/backtest only; not platform-selectable |

`scripts/print_strategy_profile_status.py` 只显示平台可选 profile，因此只会列出 `hk_global_etf_tactical_rotation` 这一条港股 profile。其他港股候选继续保留在研究文档和 snapshot pipeline，不应该出现在 Cloud Run switch plan 里。

未来启用 snapshot-backed profile 后的最小策略配置示例；这些 profile 晋级为 `runtime_enabled` 前不会出现在平台可选列表：

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

## Dry-run 切换计划

可用以下命令生成 HK dry-run 环境计划，复核当前 Cloud Run 配置或准备重新同步：

```bash
python scripts/print_strategy_switch_env_plan.py \
  --profile hk_global_etf_tactical_rotation \
  --dry-run-only \
  --deployment-selector hk-verify \
  --account-scope hk-verify \
  --account-group hk-verify \
  --service-name interactive-brokers-hk-verify-service \
  --json
```

这个命令只打印计划。输出会显式包含：

- `RUNTIME_TARGET_JSON`：`strategy_profile=hk_global_etf_tactical_rotation`、`dry_run_only=true`、`execution_mode=paper`。
- `IBKR_DRY_RUN_ONLY=true` 和 `IBKR_MARKET=HK` / `XHKG` / `SEHK` / `HKD` / `.HK`。
- `remove_if_present`：清理 snapshot/config 相关环境变量，因为该 profile 直接使用 `market_history`。
- `dry_run_plan`：检查 HK 行情权限、SEHK/HKD 映射、整数股和 lot-size、HKD 现金口径、通知和 runtime report。

打印计划不会直接修改服务配置；只有执行 Cloud Run env 更新/部署命令才会改变服务。

## 部署或同步 HK Cloud Run

仓库的 `Deploy Cloud Run` workflow 支持手动 `workflow_dispatch` 目标 `hk-verify`。这个目标会设置或更新独立港股 dry-run 服务：

- `CLOUD_RUN_SERVICE=interactive-brokers-hk-verify-service`（可通过输入改名）
- `STRATEGY_PROFILE=hk_global_etf_tactical_rotation`
- `ACCOUNT_GROUP=hk-verify`
- `RUNTIME_TARGET_JSON.execution_mode=paper`、`dry_run_only=true`
- `IBKR_DRY_RUN_ONLY=true`
- `IBKR_MARKET=HK`、`IBKR_MARKET_EXCHANGE=SEHK`、`IBKR_MARKET_CURRENCY=HKD`、`IBKR_MARKET_DATA_SYMBOL_SUFFIX=.HK`

手动部署示例：

```bash
gh workflow run sync-cloud-run-env.yml \
  --repo QuantStrategyLab/InteractiveBrokersPlatform \
  -f target=hk-verify \
  -f cloud_run_region=<gcp-region> \
  -f cloud_run_service=interactive-brokers-hk-verify-service \
  -f account_group=hk-verify \
  -f account_group_config_secret_name=ibkr-account-groups \
  -f deploy_image=true \
  -f sync_env=true
```

如果只想同步环境变量、不重新部署镜像，可以设置 `-f deploy_image=false -f sync_env=true`；workflow 会跳过 commit wait，避免等待一个并未部署的新 revision。

执行前确认：

- 目标 Cloud Run service 是独立 HK service；不要和其他 IBKR 服务共用同一个 service 名。
- GitHub 变量或输入里有 `CLOUD_RUN_REGION`、`GLOBAL_TELEGRAM_CHAT_ID`、`NOTIFY_LANG`、`IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME`。
- `TELEGRAM_TOKEN_SECRET_NAME` 或 `TELEGRAM_TOKEN` 可用。
- `IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME` 指向的账号组里存在目标 HK account group，且只绑定预期的 HK paper/verify/live 账号。
- GCP deploy service account 仍只负责部署；IBKR 登录、账号、Gateway 地址等私密配置继续放在 Secret Manager 的 account-group payload。

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
- `hk_global_etf_tactical_rotation` 已在策略包 `runtime_enabled`，可由 IBKR HK Cloud Run 通过运行时环境选择；`hk_blue_chip_leader_rotation`、`hk_index_mean_reversion`、`hk_etf_regime_rotation` 仍不进入平台可选列表。
- 港股 `market_history` profile 运行后，需要持续用 IBKR HK 行情 feed 对 `02800`、`03033`、`02822`、`02840`、`03110`、`03188`、`02834`、`03175` 做行情、价差、lot-size 和订单预览/执行结果复核。
