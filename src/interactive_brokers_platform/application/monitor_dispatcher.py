"""IBKR monitor dispatcher — wraps platform_runner with IBKR-specific names & env helpers."""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from datetime import timezone
from typing import Any

from quant_platform_kit.common.platform_runner.monitor import (
    _as_utc,
    _iter_due_dispatches,
    dispatch_due_monitors as _dispatch_due_monitors,
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
    raw = str(
        raw_json
        or os.environ.get("IBKR_MONITOR_DISPATCH_TARGETS_JSON")
        or os.environ.get("MONITOR_DISPATCH_TARGETS_JSON")
        or ""
    ).strip()
    if not raw:
        return []
    payload = json.loads(raw)
    targets = payload.get("targets") if isinstance(payload, dict) else payload
    if not isinstance(targets, list):
        raise ValueError("IBKR monitor dispatch targets must be a JSON array or an object with targets")
    for index, target in enumerate(targets):
        if not isinstance(target, dict):
            raise ValueError(f"IBKR monitor dispatch target at index {index} must be an object")
    return [dict(target) for target in targets]


def due_monitor_dispatches(
    targets: list[dict[str, Any]],
    *,
    now: datetime,
    lookback_minutes: int,
) -> list[dict[str, Any]]:
    """Expose shared due-window selection through the IBKR adapter."""
    return list(
        _iter_due_dispatches(
            targets,
            now_utc=_as_utc(now),
            lookback_minutes=lookback_minutes,
        )
    )


def dispatch_due_monitor_targets(
    targets: list[dict[str, Any]],
    *,
    now: str | datetime | None = None,
    lookback_minutes: int | None = None,
    timeout_seconds: int | None = None,
    max_workers: int | None = None,
    token_fetcher=None,
    request_fn=None,
    local_service_name: str | None = None,
    local_dispatch_fn=None,
) -> dict[str, Any]:
    resolved_now = datetime.fromisoformat(now) if isinstance(now, str) else now
    resolved_now = resolved_now or datetime.now(timezone.utc)
    resolved_lookback = lookback_minutes or lookback_minutes_from_env()
    local_name = str(local_service_name or "").strip()
    if not local_name or local_dispatch_fn is None:
        result = _dispatch_due_monitors(
            targets,
            now=resolved_now,
            lookback_minutes=resolved_lookback,
            timeout_seconds=timeout_seconds or timeout_seconds_from_env(),
            max_workers=max_workers or max_workers_from_env(),
            token_fetcher=token_fetcher,
            post_fn=request_fn,
        )
        results = list(result.get("results") or [])
        return {**result, "dispatches_sent": len(results)}

    local_targets = [target for target in targets if str(target.get("service_name") or "").strip() == local_name]
    remote_targets = [target for target in targets if str(target.get("service_name") or "").strip() != local_name]
    local_due = due_monitor_dispatches(
        local_targets,
        now=resolved_now,
        lookback_minutes=resolved_lookback,
    )

    # Run remote HTTP dispatches concurrently while the host service executes its
    # own due check in-process. This avoids a self-call deadlock on maxScale=1,
    # single-worker Cloud Run services without serially adding both timeout budgets.
    with ThreadPoolExecutor(max_workers=1) as executor:
        remote_future = executor.submit(
            _dispatch_due_monitors,
            remote_targets,
            now=resolved_now,
            lookback_minutes=resolved_lookback,
            timeout_seconds=timeout_seconds or timeout_seconds_from_env(),
            max_workers=max_workers or max_workers_from_env(),
            token_fetcher=token_fetcher,
            post_fn=request_fn,
        )
        local_results = [_dispatch_local(dispatch, local_dispatch_fn) for dispatch in local_due]
        remote_result = remote_future.result()

    results = [*local_results, *(remote_result.get("results") or [])]
    results.sort(key=lambda item: (str(item.get("service_name") or ""), str(item.get("window") or "")))
    return {
        "ok": all(bool(result.get("ok")) for result in results),
        "total_targets": len(targets),
        "dispatches_due": len(local_due) + int(remote_result.get("dispatches_due") or 0),
        "dispatches_sent": len(results),
        "results": results,
    }


def _dispatch_local(dispatch: dict[str, Any], local_dispatch_fn) -> dict[str, Any]:
    base_result = {
        key: dispatch.get(key)
        for key in ("service_name", "strategy_profile", "account_scope", "window", "url")
    }
    try:
        outcome = dict(local_dispatch_fn(dispatch) or {})
        status_code = int(outcome.get("status_code") or 0)
        return {
            **base_result,
            **outcome,
            "status_code": status_code,
            "ok": 200 <= status_code < 300,
            "dispatch_mode": "in_process",
        }
    except Exception as exc:
        return {
            **base_result,
            "status_code": 0,
            "ok": False,
            "dispatch_mode": "in_process",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
