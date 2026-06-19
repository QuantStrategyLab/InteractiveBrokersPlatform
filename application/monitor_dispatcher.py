"""Dispatch shared monitor windows to configured Cloud Run targets."""

from __future__ import annotations

import datetime as dt
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token


MONITOR_TARGETS_ENV = "IBKR_MONITOR_DISPATCH_TARGETS_JSON"
DEFAULT_LOOKBACK_MINUTES = 4
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_MAX_WORKERS = 4


@dataclass(frozen=True)
class MonitorWindow:
    name: str
    path: str
    scheduler_key: str


MONITOR_WINDOWS = (
    MonitorWindow("probe", "/probe", "probe_time"),
    MonitorWindow("precheck", "/dry-run", "precheck_time"),
)


def load_monitor_targets(raw_json: str | None = None) -> list[dict[str, Any]]:
    text = str(raw_json if raw_json is not None else os.environ.get(MONITOR_TARGETS_ENV) or "").strip()
    if not text:
        return []
    payload = json.loads(text)
    targets = payload.get("targets") if isinstance(payload, Mapping) else payload
    if not isinstance(targets, list):
        raise ValueError(f"{MONITOR_TARGETS_ENV} must be a JSON array or object with targets")
    return [target for target in targets if isinstance(target, dict)]


def due_monitor_dispatches(
    targets: list[dict[str, Any]],
    *,
    now: dt.datetime | None = None,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
) -> list[dict[str, Any]]:
    now = _normalize_now(now)
    since = now - dt.timedelta(minutes=max(0, int(lookback_minutes)))
    dispatches: list[dict[str, Any]] = []
    for target in targets:
        if not _target_enabled(target):
            continue
        service_url = str(target.get("service_url") or "").strip().rstrip("/")
        service_name = str(target.get("service_name") or target.get("service") or "").strip()
        if not service_url:
            continue
        scheduler = target.get("scheduler") if isinstance(target.get("scheduler"), Mapping) else {}
        timezone = _target_timezone(scheduler)
        for window in MONITOR_WINDOWS:
            schedule = str(scheduler.get(window.scheduler_key) or "").strip()
            if not schedule:
                continue
            if _schedule_due_between(schedule, timezone=timezone, since=since, now=now):
                dispatches.append(
                    {
                        "window": window.name,
                        "path": window.path,
                        "service_name": service_name,
                        "service_url": service_url,
                        "url": f"{service_url}{window.path}",
                        "audience": service_url,
                        "schedule": schedule,
                        "timezone": getattr(timezone, "key", str(timezone)),
                        "strategy_profile": str(target.get("strategy_profile") or "").strip(),
                    }
                )
    return dispatches


def dispatch_due_monitor_targets(
    targets: list[dict[str, Any]],
    *,
    now: dt.datetime | None = None,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_workers: int = DEFAULT_MAX_WORKERS,
    request_fn: Callable[..., Any] | None = None,
    token_fetcher: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    dispatches = due_monitor_dispatches(targets, now=now, lookback_minutes=lookback_minutes)
    request_fn = request_fn or requests.post
    token_fetcher = token_fetcher or fetch_identity_token
    token_cache: dict[str, str] = {}
    results = []
    if not dispatches:
        return {
            "dispatches_due": 0,
            "dispatches_sent": 0,
            "results": [],
        }
    for dispatch in dispatches:
        audience = str(dispatch["audience"])
        token = token_cache.get(audience)
        if token is None:
            token = token_fetcher(audience)
            token_cache[audience] = token

    def send(dispatch: Mapping[str, Any]) -> dict[str, Any]:
        token = token_cache[str(dispatch["audience"])]
        result = {
            **{key: dispatch[key] for key in ("window", "service_name", "url", "schedule", "timezone", "strategy_profile")},
        }
        try:
            response = request_fn(
                dispatch["url"],
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "ibkr-monitor-dispatcher",
                },
                timeout=timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            return {
                **result,
                "status_code": 0,
                "ok": False,
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:300],
            }
        status_code = int(getattr(response, "status_code", 0) or 0)
        body = str(getattr(response, "text", "") or "")
        return {
            **result,
            "status_code": status_code,
            "ok": 200 <= status_code < 300,
            "body_preview": body[:200],
        }

    worker_count = max(1, min(int(max_workers), len(dispatches)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(send, dispatch) for dispatch in dispatches]
        for future in as_completed(futures):
            results.append(future.result())
    return {
        "dispatches_due": len(dispatches),
        "dispatches_sent": len(results),
        "results": results,
    }


def fetch_identity_token(audience: str) -> str:
    return id_token.fetch_id_token(GoogleAuthRequest(), audience)


def lookback_minutes_from_env() -> int:
    raw_value = os.environ.get("IBKR_MONITOR_DISPATCH_LOOKBACK_MINUTES", str(DEFAULT_LOOKBACK_MINUTES))
    try:
        return max(0, int(raw_value))
    except (TypeError, ValueError):
        return DEFAULT_LOOKBACK_MINUTES


def timeout_seconds_from_env() -> int:
    raw_value = os.environ.get("IBKR_MONITOR_DISPATCH_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
    try:
        return max(1, int(raw_value))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS


def max_workers_from_env() -> int:
    raw_value = os.environ.get("IBKR_MONITOR_DISPATCH_MAX_WORKERS", str(DEFAULT_MAX_WORKERS))
    try:
        return max(1, int(raw_value))
    except (TypeError, ValueError):
        return DEFAULT_MAX_WORKERS


def _target_enabled(target: Mapping[str, Any]) -> bool:
    value = target.get("runtime_target_enabled")
    if value is None:
        return True
    return str(value).strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _normalize_now(now: dt.datetime | None) -> dt.datetime:
    value = now or dt.datetime.now(dt.timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _target_timezone(scheduler: Mapping[str, Any]) -> ZoneInfo | dt.tzinfo:
    try:
        return ZoneInfo(str(scheduler.get("timezone") or "UTC"))
    except Exception:  # noqa: BLE001
        return dt.timezone.utc


def _cron_token_value(token: str, *, names: dict[str, int] | None = None) -> int:
    normalized = token.strip().lower()
    if names and normalized in names:
        return names[normalized]
    return int(normalized)


def _cron_field_values(
    field: str,
    *,
    minimum: int,
    maximum: int,
    names: dict[str, int] | None = None,
) -> set[int] | None:
    text = str(field or "").strip().lower()
    if text in {"", "*"}:
        return None
    values: set[int] = set()
    for raw_part in text.split(","):
        part = raw_part.strip()
        if not part:
            continue
        base, raw_step = part, "1"
        if "/" in part:
            base, raw_step = part.split("/", 1)
        step = max(1, int(raw_step))
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            raw_start, raw_end = base.split("-", 1)
            start = _cron_token_value(raw_start, names=names)
            end = _cron_token_value(raw_end, names=names)
        else:
            start = end = _cron_token_value(base, names=names)
        for value in range(start, end + 1, step):
            if minimum <= value <= maximum:
                values.add(value)
            elif maximum == 6 and value == 7:
                values.add(0)
    return values


def _cron_matches(schedule: str, value: dt.datetime) -> bool:
    fields = str(schedule or "").split()
    if len(fields) != 5:
        return False
    minute, hour, day_of_month, month, day_of_week = fields
    dow_names = {
        "sun": 0,
        "mon": 1,
        "tue": 2,
        "wed": 3,
        "thu": 4,
        "fri": 5,
        "sat": 6,
    }
    minute_values = _cron_field_values(minute, minimum=0, maximum=59)
    hour_values = _cron_field_values(hour, minimum=0, maximum=23)
    dom_values = _cron_field_values(day_of_month, minimum=1, maximum=31)
    month_values = _cron_field_values(month, minimum=1, maximum=12)
    dow_values = _cron_field_values(day_of_week, minimum=0, maximum=6, names=dow_names)
    if minute_values is not None and value.minute not in minute_values:
        return False
    if hour_values is not None and value.hour not in hour_values:
        return False
    if month_values is not None and value.month not in month_values:
        return False

    dom_matches = dom_values is None or value.day in dom_values
    cron_weekday = value.isoweekday() % 7
    dow_matches = dow_values is None or cron_weekday in dow_values
    if dom_values is not None and dow_values is not None:
        return dom_matches or dow_matches
    return dom_matches and dow_matches


def _schedule_due_between(
    schedule: str,
    *,
    timezone: dt.tzinfo,
    since: dt.datetime,
    now: dt.datetime,
) -> bool:
    since_utc = since.astimezone(dt.timezone.utc)
    now_utc = now.astimezone(dt.timezone.utc)
    cursor = since_utc.replace(second=0, microsecond=0)
    if cursor < since_utc:
        cursor += dt.timedelta(minutes=1)
    while cursor <= now_utc:
        if _cron_matches(schedule, cursor.astimezone(timezone)):
            return True
        cursor += dt.timedelta(minutes=1)
    return False
