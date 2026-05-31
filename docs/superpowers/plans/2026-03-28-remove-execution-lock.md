# Remove Execution Lock Implementation Plan


## 中文摘要

- 用途：本文档围绕 `Remove Execution Lock Implementation Plan`，用于理解 `InteractiveBrokersPlatform` 的配置、运行、部署、研究或验收边界。
- 主要覆盖：`Task 1: Replace lock tests with in-memory-only behavior`、`Task 2: Remove persistent lock code`、`Task 3: Clean docs and verify`。
- 阅读顺序：先确认边界、输入输出和权限要求，再执行文档里的命令、CI、dry-run、发布或切换步骤。
- 风险提示：涉及实盘、密钥、权限、Cloud Run、交易所或券商 API 的变更，必须先在测试环境或 dry-run 验证；不要只凭示例直接修改生产。
- 英文正文保留更完整的命令、字段名和配置键；如果摘要和正文不一致，以正文中的实际命令和配置为准。
> Historical note: this file is an agent planning/research archive from March 2026. It is not the current source of truth for strategy logic, cadence, live runtime configuration, or deployment state. Use `docs/superpowers/README.md` for the archive index and current documentation pointers.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the optional GCS-backed execution lock and keep only the existing in-memory daily guard.

**Architecture:** Delete the persistent lock helpers from `main.py`, simplify `try_acquire_execution_lock()` to use only the in-process date flag, and remove the now-invalid README/test coverage for `EXECUTION_LOCK_*`.

**Tech Stack:** Python, Flask, pytest

---

### Task 1: Replace lock tests with in-memory-only behavior

**Files:**
- Modify: `tests/test_request_handling.py`

- [ ] **Step 1: Write the failing test**
  Add a test that verifies `try_acquire_execution_lock()` returns `True` once and `False` on the second call without any persistent backend.

- [ ] **Step 2: Run test to verify it fails**
  Run: `./.venv/bin/pytest -q tests/test_request_handling.py -k execution_lock`

- [ ] **Step 3: Remove obsolete persistent-lock tests**
  Delete tests that mention `EXECUTION_LOCK_BUCKET`, `EXECUTION_LOCK_PREFIX`, or `try_acquire_persistent_execution_lock`.

- [ ] **Step 4: Run test to verify it passes**
  Run: `./.venv/bin/pytest -q tests/test_request_handling.py -k execution_lock`

### Task 2: Remove persistent lock code

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Delete persistent lock helpers**
  Remove `get_execution_lock_bucket`, `get_execution_lock_object_name`, `build_authorized_session`, and `try_acquire_persistent_execution_lock`.

- [ ] **Step 2: Simplify execution guard**
  Update `try_acquire_execution_lock()` to only check `_last_execution_date`.

- [ ] **Step 3: Remove dead imports**
  Delete imports used only by persistent locking.

- [ ] **Step 4: Run focused tests**
  Run: `./.venv/bin/pytest -q tests/test_request_handling.py`

### Task 3: Clean docs and verify

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Remove `EXECUTION_LOCK_*` from env docs**
- [ ] **Step 2: Remove bucket/IAM guidance tied to persistent lock**
- [ ] **Step 3: Run full verification**
  Run: `./.venv/bin/pytest -q`

- [ ] **Step 4: Commit**
  Commit message: `Remove execution lock`
