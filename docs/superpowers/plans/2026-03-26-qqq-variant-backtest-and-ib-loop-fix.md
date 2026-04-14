# QQQ Variant Backtest And IB Loop Fix Implementation Plan

> Historical note: this file is an agent planning/research archive from March 2026. It is not the current source of truth for strategy logic, cadence, live runtime configuration, or deployment state. Use `docs/superpowers/README.md` for the archive index and current documentation pointers.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 Python 3.13 在线程中缺少 event loop 导致的 IB 连接失败，并新增独立回测脚本比较原策略与两类引入 QQQ 的方案。

**Architecture:** 保持现有 Flask + `main.py` 实盘结构不变，只在连接 IB 前补一个线程级 event loop 保护。回测代码单独放在 `research/`，复用现有策略参数和规则，避免把研究代码和下单代码耦合在一起。

**Tech Stack:** Python 3.13, Flask, ib_insync, pandas, numpy, pytest, yfinance

---

### Task 1: 补 event loop 回归测试

**Files:**
- Create: `tests/test_event_loop.py`
- Test: `tests/test_event_loop.py`

- [ ] **Step 1: 写失败测试，覆盖线程里没有默认 event loop 的场景**

```python
def test_ensure_event_loop_creates_loop_in_worker_thread():
    # 在线程中先验证 get_event_loop 会报错
    # 再调用 ensure_event_loop，最后确认可以拿到 loop
```

- [ ] **Step 2: 跑测试确认先失败**

Run: `pytest tests/test_event_loop.py -q`
Expected: FAIL，因为 `main.py` 里还没有 `ensure_event_loop`

- [ ] **Step 3: 再补一个 connect_ib 调用顺序测试**

```python
def test_connect_ib_prepares_event_loop_before_connect(monkeypatch):
    # stub 掉 IB 类，确认 connect_ib 会先准备 loop 再调用 connect
```

- [ ] **Step 4: 再跑一次测试，确认仍然是预期失败**

Run: `pytest tests/test_event_loop.py -q`
Expected: FAIL，提示缺少实现

### Task 2: 最小修复 IB 连接问题

**Files:**
- Modify: `main.py`
- Test: `tests/test_event_loop.py`

- [ ] **Step 1: 在 `main.py` 增加 `asyncio` 导入和 `ensure_event_loop()`**

```python
def ensure_event_loop():
    try:
        return asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop
```

- [ ] **Step 2: 在 `connect_ib()` 中先调用 `ensure_event_loop()` 再连接**

```python
def connect_ib():
    ensure_event_loop()
    ib = IB()
    ib.connect(...)
    return ib
```

- [ ] **Step 3: 跑相关测试确认通过**

Run: `pytest tests/test_event_loop.py -q`
Expected: PASS

### Task 3: 新增独立回测脚本

**Files:**
- Create: `research/backtest_qqq_variants.py`
- Modify: `README.md`

- [ ] **Step 1: 写最小数据加载与参数定义**

```python
BASE_POOL = [...]
CANARY_ASSETS = [...]
SAFE_HAVEN = "BIL"
```

- [ ] **Step 2: 实现价格下载与整理**

```python
def download_prices(symbols, start, end):
    ...
```

- [ ] **Step 3: 实现与实盘一致的信号计算**

```python
def compute_13612w_momentum(...)
def check_sma(...)
def is_quarterly_rebalance_day(...)
def compute_target_weights(...)
```

- [ ] **Step 4: 实现三类策略模拟**

```python
def run_baseline(...)
def run_rotation_with_qqq(...)
def run_core_satellite(...)
```

- [ ] **Step 5: 实现指标统计和子周期汇总**

```python
def summarize_period(...)
def build_report(...)
```

- [ ] **Step 6: 在 README 追加研究脚本运行方式**

Run: `python3 research/backtest_qqq_variants.py --help`
Expected: 能看到参数说明

### Task 4: 执行验证并整理结论

**Files:**
- Modify: `README.md`
- Test: `tests/test_event_loop.py`

- [ ] **Step 1: 跑测试**

Run: `pytest tests/test_event_loop.py -q`
Expected: PASS

- [ ] **Step 2: 跑回测脚本**

Run: `python3 research/backtest_qqq_variants.py`
Expected: 输出三类策略和多个子周期的表现

- [ ] **Step 3: 检查结果是否足够回答问题**

Expected:
- 能看出原策略为何排除 `QQQ`
- 能看出加 `QQQ` 后哪些阶段更强、哪些风险更高

- [ ] **Step 4: 汇总结论与剩余风险**

Expected:
- 明确说明数据口径限制
- 明确说明如果要上实盘，研究脚本结果需要和 IB 数据再交叉验证
