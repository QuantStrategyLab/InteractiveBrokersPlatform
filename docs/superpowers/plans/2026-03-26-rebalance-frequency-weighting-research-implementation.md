# Rebalance Frequency and Weighting Research Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为统一总池 `VOO + XLK + SMH` 研究版增加“调仓频率 + Top N + 仓位分配”实验，并跑出可比较的回测结果。

**Architecture:** 继续复用 `research/backtest_qqq_variants.py` 的下载、对齐、回测和报表框架，只在策略配置、调仓日期生成、选股数量和权重分配上增加可选参数。实验结果和现有主对比分组打印，不影响实盘代码。

**Tech Stack:** Python 3.9, pandas, numpy, yfinance, pytest

---

### Task 1: 先用测试锁定新配置和加权行为

**Files:**
- Modify: `tests/test_research_configs.py`
- Test: `tests/test_research_configs.py`

- [ ] **Step 1: Write the failing tests**

新增这些测试：

```python
def test_build_configs_includes_rebalance_and_weighting_experiments():
    ...

def test_compute_rotation_weights_uses_top_n_override():
    ...

def test_compute_rotation_weights_supports_momentum_weighting():
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests/test_research_configs.py`
Expected: FAIL，因为当前脚本还没有这些配置和行为

- [ ] **Step 3: Write minimal implementation**

在 `research/backtest_qqq_variants.py` 里补：

1. `StrategyConfig` 的新字段
2. `Top N` 覆盖
3. 动量加权逻辑
4. 实验配置

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests/test_research_configs.py`
Expected: PASS

### Task 2: 支持不同调仓频率和执行指标

**Files:**
- Modify: `research/backtest_qqq_variants.py`

- [ ] **Step 1: Support rebalance frequency overrides**

让配置可以覆盖：

1. 月度
2. 季度
3. 半年

- [ ] **Step 2: Add turnover and rebalance-count metrics**

在回测中统计：

1. 调仓次数
2. 近似换手率

- [ ] **Step 3: Group experiment output**

新增一个研究分组，例如：

`Rebalance and Weighting Experiments`

- [ ] **Step 4: Verify script help still works**

Run: `.venv/bin/python research/backtest_qqq_variants.py --help`
Expected: 正常输出

### Task 3: 跑完整回测并给出结论

**Files:**
- Modify: `research/backtest_qqq_variants.py`
- Test: `tests/test_research_configs.py`, `tests/test_event_loop.py`

- [ ] **Step 1: Run full backtest**

Run: `.venv/bin/python research/backtest_qqq_variants.py`
Expected: 输出新的频率和权重实验分组

- [ ] **Step 2: Run regression checks**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests/test_research_configs.py tests/test_event_loop.py`
Expected: PASS

Run: `.venv/bin/python -m py_compile research/backtest_qqq_variants.py tests/test_research_configs.py`
Expected: PASS

- [ ] **Step 3: Summarize stop/go decision**

明确回答：

1. 月度、季度、半年哪个更好
2. `Top 1 / 2 / 3` 哪个更合适
3. 等权和动量加权哪种更合理
4. 是否值得继续进入“分步加仓/减仓”的下一轮研究

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-03-26-rebalance-frequency-weighting-research-design.md \
        docs/superpowers/plans/2026-03-26-rebalance-frequency-weighting-research-implementation.md \
        research/backtest_qqq_variants.py \
        tests/test_research_configs.py
git commit -m "Add rebalance frequency and weighting research experiments"
```
