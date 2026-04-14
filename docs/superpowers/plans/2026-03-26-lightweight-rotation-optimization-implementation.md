# Lightweight Rotation Optimization Implementation Plan

> Historical note: this file is an agent planning/research archive from March 2026. It is not the current source of truth for strategy logic, cadence, live runtime configuration, or deployment state. Use `docs/superpowers/README.md` for the archive index and current documentation pointers.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变统一总池结构的前提下，为 `VOO + XLK + SMH` 研究版增加少量轻规则实验，并比较是否能改善长期表现而不过拟合。

**Architecture:** 继续复用 `research/backtest_qqq_variants.py` 现有数据下载、指标计算和报表逻辑，只在策略配置和选股打分/替换逻辑上增加三个可选参数：`voo_bonus`、`switch_threshold`、`hold_bonus_override`。实验输出和现有主对比结果分组打印，不影响实盘代码。

**Tech Stack:** Python 3.9, pandas, numpy, yfinance, pytest

---

### Task 1: 先用测试锁定轻量实验行为

**Files:**
- Modify: `tests/test_research_configs.py`
- Test: `tests/test_research_configs.py`

- [ ] **Step 1: Write failing tests**

新增两类测试：

```python
def test_build_configs_includes_lightweight_experiments():
    ...

def test_compute_rotation_weights_applies_voo_bonus():
    ...

def test_compute_rotation_weights_respects_switch_threshold():
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests/test_research_configs.py`
Expected: FAIL，因为当前脚本还没有这些实验配置和逻辑

- [ ] **Step 3: Write minimal implementation**

在 `research/backtest_qqq_variants.py` 里：
- 扩展 `StrategyConfig`
- 抽出分数计算和选股逻辑
- 支持 `VOO` 额外加分、替换门槛、持仓 bonus 覆盖

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests/test_research_configs.py`
Expected: PASS

### Task 2: 把实验配置接入研究脚本输出

**Files:**
- Modify: `research/backtest_qqq_variants.py`

- [ ] **Step 1: Add lightweight experiment configs**

新增这些配置：
- `proposed_voo_xlk_smh`
- `voo_bonus_0_5`
- `voo_bonus_1_0`
- `switch_threshold_1_0`
- `hold_bonus_1_0`
- `hold_bonus_3_0`

- [ ] **Step 2: Group experiment output**

在报表里新增一个实验分组，避免和主对比、拆解对比混在一起。

- [ ] **Step 3: Verify script help and import still work**

Run: `.venv/bin/python research/backtest_qqq_variants.py --help`
Expected: 正常输出，脚本仍可运行

### Task 3: 运行回测并给出结论

**Files:**
- Modify: `research/backtest_qqq_variants.py`
- Test: `tests/test_research_configs.py`, `tests/test_event_loop.py`

- [ ] **Step 1: Run full backtest**

Run: `.venv/bin/python research/backtest_qqq_variants.py`
Expected: 输出主对比、拆解对比、QQQ core、轻量优化实验四组结果

- [ ] **Step 2: Run regression checks**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests/test_research_configs.py tests/test_event_loop.py`
Expected: PASS

Run: `.venv/bin/python -m py_compile research/backtest_qqq_variants.py tests/test_research_configs.py`
Expected: PASS

- [ ] **Step 3: Summarize stop/go decision**

明确回答：
- `VOO` 小加分是否真的有帮助
- 替换门槛是否改善稳定性
- 当前 `+2%` 持仓 bonus 是否值得保留
- 如果没有稳定提升，就停止继续细调

- [ ] **Step 4: Commit**

```bash
git add research/backtest_qqq_variants.py tests/test_research_configs.py
git commit -m "Add lightweight rotation optimization experiments"
```
