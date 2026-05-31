# Research Notes


## 中文摘要

- 用途：本文档围绕 `Research Notes`，用于理解 `InteractiveBrokersPlatform` 的配置、运行、部署、研究或验收边界。
- 主要覆盖：`Research Notes`。
- 阅读顺序：先确认边界、输入输出和权限要求，再执行文档里的命令、CI、dry-run、发布或切换步骤。
- 风险提示：涉及实盘、密钥、权限、Cloud Run、交易所或券商 API 的变更，必须先在测试环境或 dry-run 验证；不要只凭示例直接修改生产。
- 英文正文保留更完整的命令、字段名和配置键；如果摘要和正文不一致，以正文中的实际命令和配置为准。
This directory keeps IBKR-side research artifacts that are still useful for
current live strategy review.

Live strategy behavior is owned by `UsEquityStrategies`, platform runtime
configuration, and the snapshot pipeline repositories. Historical IBKR-local
research files that used retired TQQQ, SOXL/SOXX, growth-pullback,
stock-alpha, or brokerless signal-notifier assumptions have been removed from
this repository so they are not reused as current IBKR performance references.

Current retained IBKR-local research: none.

Use `UsEquitySnapshotPipelines` outputs when reviewing snapshot-backed live
strategy performance.
