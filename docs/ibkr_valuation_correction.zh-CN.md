# IBKR 策略快照估值边界

策略快照对普通股票使用 IBKR 当前缓存 `portfolio()` 行中的 `marketValue`，并以 `account + conId` 与 `positions()` 的持仓严格配对；数量和币种也必须一致。缺失、非有限、身份不匹配或不同币种的市值会阻断快照，不以平均成本代替。

`Position.average_cost` 继续来自 `positions().avgCost`。策略权益按已选账户的原生现金与股票市值计算；原生现金保持账户返回值，cash-only 执行的 buying power 继续以该现金为限，不能把它描述为券商保证金购买力。USD `NetLiquidation` 作为单独的券商 NLV 证据记录，并保留与策略权益的差额，不据此改写现金。`BASE` 不被视为 USD。股票快照只处理与目标币种一致的 `STK`；期权仍单独列入元数据，其他衍生品类型不进入股票估值。

快照记录市值来源及本次读取时间。`portfolio()` 返回缓存数据时通常没有逐条报价时间，因此该时间只表示本次观察时点，不证明底层报价的新鲜度。这里不请求行情、不换汇，也不增加券商读取调用。

原锁定的 QuantPlatformKit（`bd8d06e`）策略范围投影保留正确的 `market_value`，但会重建 `Position` 并丢弃 `average_cost`、`currency` 和 `account_id`。QPK PR #643 已于 2026-10-03 合并；合并提交 `fc4cad977d637c31ff43b3ea5969665707d8e867` 修正了往返转换：唯一标的保留字段及空头，重复标的明确拒绝，缺失成本保持未知。

在 QPK 合并及 IBKR 本地更新固定版本之前，曾用 IBKR 实际投影入口、锁定 QPK/UES 环境叠加候选 QPK helper 做离线合成验证，覆盖多头、空头、缺失成本和重复标的拒绝；该结果当时只证明候选 helper 的行为，不代表锁定依赖已采用。

IBKR 本次工作区已将 QPK 固定到合并提交 `fc4cad977d637c31ff43b3ea5969665707d8e867`，UES `4a3943883cd6b5bbfe32a559e56a91b40a81b7ce` 与 HES `709e5e1cde7841aed538d94eb26b552b46cb7806` 保持原 pin。`tests/test_strategy_runtime.py` 中的四项投影回归现为无条件测试，覆盖成本已知/未知、非默认币种与合成账户身份保留、空头及显式零仓省略、重复标的拒绝。worktree 独立环境实际安装来源核验、与 CI 同命令的本地全测试均通过；这只证明本地依赖采用和合成测试，不代表 IBKR PR 已合并、生产已部署、券商多币种支持或真实账户恢复。
