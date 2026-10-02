# IBKR 策略快照估值边界

策略快照对普通股票使用 IBKR 当前缓存 `portfolio()` 行中的 `marketValue`，并以 `account + conId` 与 `positions()` 的持仓严格配对；数量和币种也必须一致。缺失、非有限、身份不匹配或不同币种的市值会阻断快照，不以平均成本代替。

`Position.average_cost` 继续来自 `positions().avgCost`。策略权益按已选账户的原生现金与股票市值计算；原生现金保持账户返回值，cash-only 执行的 buying power 继续以该现金为限，不能把它描述为券商保证金购买力。USD `NetLiquidation` 作为单独的券商 NLV 证据记录，并保留与策略权益的差额，不据此改写现金。`BASE` 不被视为 USD。股票快照只处理与目标币种一致的 `STK`；期权仍单独列入元数据，其他衍生品类型不进入股票估值。

快照记录市值来源及本次读取时间。`portfolio()` 返回缓存数据时通常没有逐条报价时间，因此该时间只表示本次观察时点，不证明底层报价的新鲜度。这里不请求行情、不换汇，也不增加券商读取调用。

锁定的 QuantPlatformKit 策略范围投影保留正确的 `market_value`，但会重建 `Position` 并丢弃 `average_cost`、`currency` 和 `account_id`。本次修复不更改共享投影契约；因此平均成本只在投影前的 IBKR 快照可用，不能据此宣称下游所有 PnL 诊断都已恢复。
