# Historical agent plans and research notes

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
