# InteractiveBrokersPlatform

[English README](README.md)

> ⚠️ 投资有风险，不构成投资建议，仅供学习交流用途。

## 这个项目做什么

InteractiveBrokersPlatform 是 QuantStrategyLab 体系中的**执行平台**。通过 Interactive Brokers 执行 QuantStrategyLab 的美股和港股策略，包含账户组配置、区域处理、dry-run 控制以及港股/HKD 支持。

## 适合谁使用

- 希望阅读、复现或扩展 QuantStrategyLab 相关模块的工程师和研究人员。
- 在阅读详细 runbook 或 workflow 前，需要先理解项目入口的运维人员。
- 在启用自动化前，需要确认项目职责、安全边界和证据要求的 reviewer。

## 当前状态

生产型券商接入；真实交易前必须先完成 paper 或 dry-run 验证。

## 仓库结构

- `application/`, `entrypoints/`, `notifications/`, `strategy/`：Python 包代码。
- `tests/`：单元测试和契约测试。
- `docs/`：详细设计说明、运行手册和证据文档。
- `.github/workflows/`：CI、定时任务和部署 workflow。
- `scripts/`：运维脚本和本地辅助工具。

## 快速开始

从全新 clone 开始：

```bash
python -m pip install -r requirements.txt
python -m pytest -q
```

如果命令需要凭据，请先阅读相关 workflow 或 runbook，并把密钥配置在 Git 之外。

## 部署和运行

准备 IBKR 连接、账户标识、运行变量和策略来源。先以 dry-run 运行 workflow 或服务，确认订单和通知无误后再启用生产定时任务。

建议先手工运行或 dry-run。只有在日志、产物、权限和回滚步骤都检查过之后，才启用定时任务或 live 执行。

## 策略表现与证据边界

执行平台关注可靠性和订单正确性。策略表现需要在策略/快照仓库中确认后，才能通过 IBKR 路由订单。

README 不应该承诺固定收益或过期指标。实际使用前，请重新运行对应测试、回测或流水线任务。

## 安全注意事项

- 不要把 API key、券商凭据、OAuth token、Cookie 或账户标识提交到 Git。
- 新策略或平台变更在 live 前必须先跑 dry-run 或 paper 流程。
- 启用定时任务前，需要人工检查生成的订单、产物和日志。

## 参与贡献

请保持改动小、可复现，并用最小必要测试覆盖。涉及策略的改动，需要附上验证行为的证据产物或命令。

## 许可证

如仓库包含 [LICENSE](LICENSE)，请以该文件为准。
