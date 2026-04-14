# InteractiveBrokersPlatform QQQ Variant Backtest And IB Event Loop Fix Design

> Historical note: this file is an agent planning/research archive from March 2026. It is not the current source of truth for strategy logic, cadence, live runtime configuration, or deployment state. Use `docs/superpowers/README.md` for the archive index and current documentation pointers.

## 背景

当前仓库实现的是“全球非科技板块季度轮动 + 每日金丝雀应急”，README 明确把它定义为科技仓位的互补策略，而不是覆盖科技牛市的主策略。

这次要同时解决两个问题：

1. 线上实盘在 Cloud Run / Gunicorn 线程中调用 `ib_insync` 时，因为 Python 3.13 在线程里不再自动提供 event loop，导致连接 IB Gateway 前直接报错。
2. 需要验证“排除 QQQ”是不是合理，比较原策略与两类引入 QQQ 的方案在全周期和科技牛市阶段的表现。

## 目标

- 用最小代码改动修复 `RuntimeError: There is no current event loop in thread ...`
- 新增可复用的研究脚本，回测三类策略：
  - 原始非科技轮动
  - `QQQ` 加入轮动池参与 `Top 2`
  - 原策略基础上固定保留一部分 `QQQ` 长持仓位
- 输出全周期与关键子周期表现，回答“加 QQQ 会不会更好”

## 非目标

- 不重写现有 Flask / Gunicorn 架构
- 不把实盘执行逻辑整体重构成异步服务
- 不引入新的三方依赖
- 不把研究代码和下单逻辑强耦合

## 方案

### 1. IB 连接报错修复

在 `connect_ib()` 调用 `ib.connect(...)` 之前，显式确保当前线程存在可用的 `asyncio` event loop。

设计要点：

- 新增一个很小的辅助函数，例如 `ensure_event_loop()`
- 优先复用当前线程已有 loop
- 如果当前线程没有 loop，则新建一个并设为当前线程 loop
- `connect_ib()` 在创建 `IB()` 后立即调用该辅助函数，再执行连接

这样可以兼容：

- 本地单线程直接运行
- Gunicorn `threads > 1`
- Python 3.13 对线程事件循环的变化

### 2. 测试策略

补一个最小回归测试，覆盖“在线程里第一次连接时会先准备 event loop”这个行为。

测试不直接连真实 IB，而是验证：

- 在线程里调用辅助函数前，`asyncio.get_event_loop_policy().get_event_loop()` 会抛错
- 调用辅助函数后，当前线程可以拿到可用 loop
- `connect_ib()` 会在实例化 `IB` 后先准备 loop，再调用 `connect`

## 3. 回测实现

新增独立研究脚本，不把回测揉进 `main.py`。脚本职责：

- 复用现有策略参数和规则
- 用 `yfinance` 拉取价格数据
- 在本地完成组合构建与指标统计

拟新增文件：

- `research/backtest_qqq_variants.py`
- `tests/test_event_loop.py`

回测规则与实盘保持一致的部分：

- 13612W 动量
- SMA200 过滤
- Top 2 等权
- 季末调仓
- 金丝雀应急
- 缺位进 `BIL`

本次研究新增的两类策略：

- `qqq_in_rotation`: 在原 `RANKING_POOL` 里加入 `QQQ`
- `qqq_core_satellite`: 固定 `QQQ` 仓位，剩余仓位跑原始非科技轮动

固定仓位方案先测：

- 20%
- 30%
- 40%

## 4. 输出结果

脚本输出：

- 全周期指标：CAGR、总收益、最大回撤、年化波动、Sharpe
- 与 `SPY` / `QQQ` 相关性
- 年度收益表
- 关键子周期对比：
  - 全周期
  - 2009-01-01 到 2021-11-30
  - 2022-01-01 到 2022-12-31
  - 2023-01-01 到最新

## 风险

- `yfinance` 的复权与 IBKR `ADJUSTED_LAST` 口径不完全一致，结果更适合做方向判断，不适合当作精确实盘审计
- `USO` 等商品 ETF 的长期历史口径可能带来噪音，需要在结论里明确说明
- 固定持有 `QQQ` 方案对权重很敏感，所以需要同时给出几档权重，而不是只看单点结果

## 验证标准

- 测试能证明线程内 event loop 问题被覆盖
- 应用逻辑不需要改部署架构即可避开当前报错
- 回测脚本可直接运行并输出三类策略、多个周期的对比结果
- 最终结论能回答：
  - 为什么原策略排除 `QQQ`
  - 在科技牛市里加 `QQQ` 是否更好
  - 这种“更好”是更高收益，还是以更高回撤/更高相关性换来的
