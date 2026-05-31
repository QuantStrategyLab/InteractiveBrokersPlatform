# Historical agent plans and research notes


## 中文摘要

- 用途：本文档围绕 `Historical agent plans and research notes`，用于理解 `InteractiveBrokersPlatform` 的配置、运行、部署、研究或验收边界。
- 主要覆盖：`Archived plans`、`Archived specs`。
- 阅读顺序：先确认边界、输入输出和权限要求，再执行文档里的命令、CI、dry-run、发布或切换步骤。
- 风险提示：涉及实盘、密钥、权限、Cloud Run、交易所或券商 API 的变更，必须先在测试环境或 dry-run 验证；不要只凭示例直接修改生产。
- 英文正文保留更完整的命令、字段名和配置键；如果摘要和正文不一致，以正文中的实际命令和配置为准。
This directory is an archive of March 2026 agent-generated implementation plans and research designs. These files are useful for understanding why earlier IBKR research and runtime changes were attempted, but they are **not** the current source of truth for strategy behavior or live deployment settings.

Current docs live here instead:

- Strategy logic, cadence, universes, and research/backtest notes: [`UsEquityStrategies`](https://github.com/QuantStrategyLab/UsEquityStrategies)
- IBKR runtime, Gateway connectivity, env vars, and deployment wiring: [`InteractiveBrokersPlatform` README](../../README.md)
- Cross-platform matrix and live-switch runbooks: [`QuantPlatformKit`](https://github.com/QuantStrategyLab/QuantPlatformKit/tree/main/docs)
- Snapshot artifact generation and publishing: [`UsEquitySnapshotPipelines`](https://github.com/QuantStrategyLab/UsEquitySnapshotPipelines)

Do not treat wording like "current default", "live", "production", "daily", "monthly", or "quarterly" inside the archived files as current state. Those phrases describe the state or assumption at the time the file was written.

## Archived plans

- [`plans/2026-03-26-qqq-variant-backtest-and-ib-loop-fix.md`](plans/2026-03-26-qqq-variant-backtest-and-ib-loop-fix.md): IB event-loop fix plus early QQQ variant backtest plan.
- [`plans/2026-03-26-voo-xlk-smh-rotation-research-implementation.md`](plans/2026-03-26-voo-xlk-smh-rotation-research-implementation.md): VOO / XLK / SMH rotation research implementation plan.
- [`plans/2026-03-26-rebalance-frequency-weighting-research-implementation.md`](plans/2026-03-26-rebalance-frequency-weighting-research-implementation.md): rebalance frequency and weighting research implementation plan.
- [`plans/2026-03-26-lightweight-rotation-optimization-implementation.md`](plans/2026-03-26-lightweight-rotation-optimization-implementation.md): lightweight rotation optimization research implementation plan.
- [`plans/2026-03-28-remove-execution-lock.md`](plans/2026-03-28-remove-execution-lock.md): historical plan for removing the GCS-backed execution lock.

## Archived specs

- [`specs/2026-03-26-qqq-variant-backtest-design.md`](specs/2026-03-26-qqq-variant-backtest-design.md): early QQQ variant backtest and IB event-loop design.
- [`specs/2026-03-26-voo-xlk-smh-rotation-research-design.md`](specs/2026-03-26-voo-xlk-smh-rotation-research-design.md): VOO / XLK / SMH rotation research design.
- [`specs/2026-03-26-rebalance-frequency-weighting-research-design.md`](specs/2026-03-26-rebalance-frequency-weighting-research-design.md): rebalance frequency and weighting research design.
