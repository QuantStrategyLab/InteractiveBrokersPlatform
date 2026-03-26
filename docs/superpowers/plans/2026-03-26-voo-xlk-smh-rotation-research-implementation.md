# VOO XLK SMH Rotation Research Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 扩展研究脚本，比较旧版非科技池、当前 `QQQ` 版，以及 `VOO + XLK + SMH` 结构版，并输出可直接用于决策的回测结果。

**Architecture:** 继续复用现有 `research/backtest_qqq_variants.py` 的下载、指标、回测和报表逻辑，只扩展策略配置矩阵，不改动核心策略规则。先用测试锁定配置生成结果，再做最小代码修改，最后跑脚本拿结果。

**Tech Stack:** Python 3.9, pandas, numpy, yfinance, pytest

---

### Task 1: 给研究脚本补配置构建测试

**Files:**
- Create: `tests/test_research_configs.py`
- Test: `tests/test_research_configs.py`

- [ ] **Step 1: Write the failing test**

```python
def test_build_configs_includes_voo_xlk_smh_variants():
    configs = build_configs([0.2, 0.3, 0.4])
    names = {config.name for config in configs}

    assert "baseline_non_tech" in names
    assert "current_default_qqq" in names
    assert "proposed_voo_xlk_smh" in names
    assert "replace_qqq_with_voo" in names
    assert "voo_plus_xlk" in names
    assert "voo_plus_xlk_plus_smh" in names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests/test_research_configs.py`
Expected: FAIL，因为现有脚本还没生成这些配置

- [ ] **Step 3: Write minimal implementation**

修改 `research/backtest_qqq_variants.py`：
- 提取当前默认 `QQQ` 池
- 新增 `VOO` / `XLK` / `SMH` 结构版配置
- 保留旧版非科技池和 `QQQ core` 变体

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests/test_research_configs.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_research_configs.py research/backtest_qqq_variants.py
git commit -m "Expand research configurations for VOO XLK SMH study"
```

### Task 2: 更新研究脚本输出文案

**Files:**
- Modify: `research/backtest_qqq_variants.py`

- [ ] **Step 1: Update script description and labels**

把脚本头部描述和打印标签从“只研究 QQQ 方案”改成“比较旧版、当前默认版、以及 VOO/XLK/SMH 结构版”。

- [ ] **Step 2: Verify script help output**

Run: `.venv/bin/python research/backtest_qqq_variants.py --help`
Expected: 输出能反映脚本已不只是 QQQ 研究

- [ ] **Step 3: Commit**

```bash
git add research/backtest_qqq_variants.py
git commit -m "Clarify research script scope and labels"
```

### Task 3: 运行研究回测并记录结果

**Files:**
- Modify: `README.md` (only if user later decides to publish results)
- Reference: `research/backtest_qqq_variants.py`

- [ ] **Step 1: Run full backtest**

Run: `.venv/bin/python research/backtest_qqq_variants.py`
Expected: 成功输出主对比、拆解对比、关键区间和年度收益

- [ ] **Step 2: Verify key questions are answered**

检查输出里至少能直接读出：
- `proposed_voo_xlk_smh` vs `current_default_qqq`
- `replace_qqq_with_voo`
- `voo_plus_xlk`
- `voo_plus_xlk_plus_smh`

- [ ] **Step 3: Run regression checks**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests/test_event_loop.py tests/test_research_configs.py`
Expected: PASS

Run: `.venv/bin/python -m py_compile research/backtest_qqq_variants.py tests/test_research_configs.py`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add research/backtest_qqq_variants.py tests/test_research_configs.py
git commit -m "Add VOO XLK SMH rotation research outputs"
```

### Task 4: 整理结论并准备后续方向

**Files:**
- Reference: `docs/superpowers/specs/2026-03-26-voo-xlk-smh-rotation-research-design.md`

- [ ] **Step 1: Summarize results for decision making**

整理四个问题：
- 新总池是否优于当前默认 `QQQ` 版
- 提升主要来自谁
- 是否值得继续进入实盘候选
- 是否值得进入“全球科技 / 全球半导体代理研究”

- [ ] **Step 2: Only if user approves, update docs or live strategy**

本任务默认不改 `README.md` 和实盘代码，除非用户基于结果明确要求。
