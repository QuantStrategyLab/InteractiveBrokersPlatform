# InteractiveBrokersPlatform

InteractiveBrokersPlatform 是 QuantStrategyLab 的 Interactive Brokers 多市场执行运行时，通过 IBKR 运行服务执行美股和港股 profile，支持 dry-run、paper 和 live 控制。它属于执行层，不是策略研究仓库：策略逻辑来自 `UsEquityStrategies` / `HkEquityStrategies`，如果 profile 依赖 snapshot，验证和产物来自 `UsEquitySnapshotPipelines` / `HkEquitySnapshotPipelines`。

[English README](README.md)

> 投资有风险。本项目不构成投资建议，仅用于学习、研究和工程审阅。

## 这个仓库是什么

本仓库在 `src/interactive_brokers_platform/` 下提供可安装的 `interactive_brokers_platform` 平台包，包含 WSGI 服务入口（`interactive_brokers_platform.main:app`）、券商与组合适配器、订单执行与对账服务、策略运行时加载器以及通知投递模块。策略规则和 snapshot 生成由上游策略及 pipeline 仓库维护，不包含在该运行时包内。

## 运行边界

- 只加载策略包暴露的 runtime-enabled profile。
- 负责券商/API 连接、dry-run 检查、通知和部署配置。
- 凭据必须放在 GitHub Secrets、云密钥系统或券商专用密钥系统中，不能提交到 Git。
- 任何 live 下单路径启用前，都应先从 dry-run 或 paper mode 开始。

## 普通 profile 与 snapshot-backed profile

普通 runtime profile 通常可以直接基于 market history 或 portfolio state 执行。Snapshot-backed profile 需要先从对应 snapshot pipeline 获取当前 artifact bundle，平台才应该执行。平台不应该自行判断策略资格，而应消费策略仓和 snapshot 仓发布的状态与产物。

## 港股策略政策

固定依赖中的港股策略政策已无可选策略。`hk_global_etf_tactical_rotation` 和 `hk_low_vol_dividend_quality_snapshot` 均拒绝新选择，包括 dry-run 切换计划；历史策略元数据和适配器仍保留，美股策略版本及现有部署配置继续保持有效。

## 运行时报告中的账户风险事实

当账户事实通过既有物理账户选择器校验后，运行时报告可包含 schema 为 `ibkr.account_risk_fact_readiness.v1` 的 `summary.account_facts.account_risk_fact_readiness`。该回执仅描述快照报告本身：`scope` 为 `SNAPSHOT_REPORT_ONLY`，`admission` 为 `NOT_EVALUATED`，`status` 为 `NOT_READY`；它不是执行决策或下单授权。`snapshot_observed_at` 仅表示抓取时间，而非经过验证的券商新鲜度水位。手续费、数量权限、账户域与批准预算均为 `UNVERIFIED`；订单观察水位为 `MISSING`。

`settled_cash` 仅限 USD 与原生 `SettledCash` 字段，状态为 `MISSING`、`INVALID`、`AMBIGUOUS`、`UNVERIFIED_SCOPE` 或 `OBSERVED`。仅当存在唯一有效的 account-values-cache 行且无 model code 时，才包含数值、来源方法、model scope 与快照观察时间；不会使用 `CashBalance` 或交收计划替代 `SettledCash`，观察本身也不证明可消费资金或数据新鲜度。

若账户事实无法构建（包括现金行缺失、无效或物理账户选择器不匹配），该回执缺席；旧报告也可能不包含该字段。消费方必须将缺席状态视为 `NOT_REPORTED` 且未就绪；缺席本身不证明缺失原因。该报告状态不改变独立的执行门禁。

## 安全部署顺序

1. 在 Git 之外配置 secrets 和 runtime variables。
2. 先以 dry-run 模式运行 workflow 或服务。
3. 检查生成订单、日志、通知和 reconciliation 输出。
4. 确认回滚步骤和 artifact 版本。
5. 上述检查清楚后，再启用定时任务或 live 执行。

## 仓库结构

- `src/interactive_brokers_platform/`：可安装的平台包（包含 `main.py`、`application/`、`entrypoints/`、`notifications/`、`strategy/` 及运行时支持模块）。
- `config/`：公开非敏感 runtime-target manifest。
- `tests/`：单元测试、契约测试和回归测试。
- `docs/`：运行手册、设计说明、证据和集成契约。
- `.github/workflows/`：CI、定时任务、发布或部署 workflow。
- `scripts/`：运维脚本和本地辅助工具。
- `research/`：研究配置和非 live 候选产物。

## QSL 架构角色

- **层级**：`执行平台`。
- **职责**：Interactive Brokers 多市场执行运行时。
- **事实源/归属**：IBKR 连接、paper/live 控制、账户/runtime 集成。
- **消费对象**：UsEquityStrategies、HkEquityStrategies、snapshot artifacts、QuantPlatformKit、QuantRuntimeSettings。
- **禁止事项**：承载策略研究逻辑或绕过 dry-run checks。

## 快速开始

```bash
uv sync --frozen --extra test
uv run --no-sync ruff check --exclude external .
uv run --no-sync python scripts/check_qpk_pin_consistency.py
```

## 延伸文档

- [`docs/hk-equity-runtime.md`](docs/hk-equity-runtime.md)
- [`docs/ibkr-paper-top50-switch.md`](docs/ibkr-paper-top50-switch.md)
- [`docs/ibkr-runtime-rollout.md`](docs/ibkr-runtime-rollout.md)
- [`docs/runtime-target-manifest.md`](docs/runtime-target-manifest.md)：公开 runtime-target manifest 契约、增减目标步骤与下一阶段 workflow wiring 边界
- [`docs/display-names.md`](docs/display-names.md)：展示与通知命名配置

## 社区和安全

- 贡献前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)，确认 PR 范围、本地校验和文档要求。
- 讨论、issue 和 review 请遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
- 涉及密钥、自动化、券商/交易所或云资源的漏洞请按 [SECURITY.md](SECURITY.md) 私密报告；不要为 secret 或实盘风险开公开 issue。

## 参与贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。AI 助手与维护者遵循 [AGENTS.md](AGENTS.md)。

## 许可证

详见 [LICENSE](LICENSE)。
