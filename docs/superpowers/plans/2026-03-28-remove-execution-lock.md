# Remove Execution Lock Implementation Plan

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
