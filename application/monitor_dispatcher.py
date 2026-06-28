"""IBKR monitor dispatcher — wraps platform_runner with IBKR-specific names & env helpers."""
from __future__ import annotations

import json
import os
from typing import Any

from quant_platform_kit.common.platform_runner.monitor import (
    dispatch_due_monitors as _dispatch_due_monitors,
    load_monitor_targets as _load_targets_from_env,
)


# ── IBKR env var helpers (were part of the original IBKR monitor_dispatcher) ──

def lookback_minutes_from_env() -> int:
    return int(os.environ.get("IBKR_MONITOR_DISPATCH_LOOKBACK_MINUTES", "4"))

def timeout_seconds_from_env() -> int:
    return int(os.environ.get("IBKR_MONITOR_DISPATCH_TIMEOUT_SECONDS", "120"))

def max_workers_from_env() -> int:
    return int(os.environ.get("IBKR_MONITOR_DISPATCH_MAX_WORKERS", "4"))


# ── IBKR-compatible function names ──

def load_monitor_targets(raw_json: str | None = None) -> list[dict[str, Any]]:
    """Read target config from env or from a direct JSON string."""
    if raw_json:
        return json.loads(raw_json)
    env = os.environ if "IBKR_MONITOR_DISPATCH_TARGETS_JSON" in os.environ else None
    return _load_targets_from_env(env=env)


def dispatch_due_monitor_targets(
    targets: list[dict[str, Any]],
    *,
    now: str | None = None,
    lookback_minutes: int | None = None,
    timeout_seconds: int | None = None,
    max_workers: int | None = None,
) -> dict[str, Any]:
    from datetime import datetime, timezone

    return _dispatch_due_monitors(
        targets,
        now=datetime.fromisoformat(now) if now else None,
        lookback_minutes=lookback_minutes or lookback_minutes_from_env(),
        timeout_seconds=timeout_seconds or timeout_seconds_from_env(),
        max_workers=max_workers or max_workers_from_env(),
    )
