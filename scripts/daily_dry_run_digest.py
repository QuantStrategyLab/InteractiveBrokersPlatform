#!/usr/bin/env python3
"""Send a daily summary for privately configured read-only IBKR drills."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
from dataclasses import dataclass
from zoneinfo import ZoneInfo

try:
    from scripts.execution_report_heartbeat import _send_telegram
except ModuleNotFoundError:
    from execution_report_heartbeat import _send_telegram


@dataclass(frozen=True)
class DrillTarget:
    label: str
    profile: str
    scope: str
    service: str
    schedule: str
    timezone: str

    @property
    def precheck_job(self) -> str:
        return f"{self.service.removesuffix('-service')}-precheck-scheduler"

    @property
    def live_job(self) -> str:
        return f"{self.service.removesuffix('-service')}-scheduler"


def _setting(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise ValueError(f"{name} is required for daily drill reporting")
    return value


def _targets() -> list[DrillTarget]:
    payload = json.loads(_setting("CLOUD_RUN_SERVICE_TARGETS_JSON"))
    entries = payload.get("targets") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise ValueError("drill target inventory must be a list")
    result: list[DrillTarget] = []
    for item in entries:
        if not isinstance(item, dict) or item.get("drill_precheck_enabled") is not True:
            continue
        runtime = item.get("runtime_target") or item.get("runtime_target_json") or {}
        if isinstance(runtime, str):
            runtime = json.loads(runtime)
        if not isinstance(runtime, dict):
            raise ValueError("drill runtime target must be an object")
        scheduler = runtime.get("scheduler") or {}
        service = str(item.get("service") or item.get("service_name") or "").strip()
        profile = str(runtime.get("strategy_profile") or "").strip()
        scope = str(runtime.get("account_scope") or "").strip()
        schedule = str(scheduler.get("precheck_time") or "").strip()
        timezone = str(scheduler.get("timezone") or "").strip()
        if (
            not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", service)
            or runtime.get("service_name") != service
            or not re.fullmatch(r"[a-z][a-z0-9_]*", profile)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", scope)
            or not timezone
        ):
            raise ValueError("drill target identity is incomplete or invalid")
        fields = schedule.split()
        if len(fields) != 5 or fields[2:] != ["*", "*", "*"]:
            raise ValueError("drill precheck must run every day")
        int(fields[0]), int(fields[1])
        result.append(DrillTarget(
            label=str(item.get("drill_label") or f"演练目标 {len(result) + 1}"),
            profile=profile,
            scope=scope,
            service=service,
            schedule=schedule,
            timezone=timezone,
        ))
    return result


def _gcloud(*args: str) -> str:
    result = subprocess.run(("gcloud", *args), capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"gcloud {args[0]} {args[1]} failed ({result.returncode})")
    return result.stdout


def _job(job_name: str) -> dict:
    return json.loads(_gcloud(
        "scheduler", "jobs", "describe", job_name,
        "--project", _setting("GCP_PROJECT_ID"),
        "--location", _setting("RUNTIME_HEARTBEAT_SCHEDULER_LOCATION"), "--format=json",
    ))


def _timestamp(raw_value: object) -> dt.datetime | None:
    try:
        value = dt.datetime.fromisoformat(str(raw_value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else None


def _report_time(report: dict) -> dt.datetime | None:
    return _timestamp(report.get("started_at"))


def _today_reports(target: DrillTarget, day: dt.date) -> list[dict]:
    timezone = ZoneInfo(target.timezone)
    start = dt.datetime.combine(day, dt.time.min, timezone).astimezone(dt.timezone.utc)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, timezone).astimezone(dt.timezone.utc)
    months = {start.strftime("%Y-%m"), end.strftime("%Y-%m")}
    utc_dates = {start.strftime("%Y%m%d"), end.strftime("%Y%m%d")}
    report_root = _setting("RUNTIME_HEARTBEAT_GCS_URIS").split(",", 1)[0].rstrip("/")
    if not report_root.startswith("gs://"):
        raise ValueError("drill report root must be a GCS URI")
    prefix = f"{report_root}/interactive_brokers/{target.profile}/{target.scope}"
    reports: list[dict] = []
    for month in sorted(months):
        listing = subprocess.run(
            ("gcloud", "storage", "ls", f"{prefix}/{month}/*", "--project", _setting("GCP_PROJECT_ID")),
            capture_output=True, text=True, check=False,
        )
        if listing.returncode != 0:
            if "matched no objects" in listing.stderr.lower() or "not found" in listing.stderr.lower():
                continue
            raise RuntimeError("GCS drill report listing failed")
        for uri in listing.stdout.splitlines():
            if not uri.startswith(prefix + "/") or not uri.endswith(".json"):
                continue
            if uri.rsplit("/", 1)[-1][:8] not in utc_dates:
                continue
            report = json.loads(_gcloud("storage", "cat", uri, "--project", _setting("GCP_PROJECT_ID")))
            when = _report_time(report)
            if when is None or when.astimezone(timezone).date() != day:
                continue
            if (
                report.get("service_name") == target.service
                and report.get("strategy_profile") == target.profile
                and report.get("account_scope") == target.scope
            ):
                reports.append(report)
    return sorted(reports, key=lambda item: _report_time(item) or start, reverse=True)


def _service_is_drill_only(target: DrillTarget) -> tuple[bool, str]:
    service = json.loads(_gcloud(
        "run", "services", "describe", target.service,
        "--project", _setting("GCP_PROJECT_ID"),
        "--region", _setting("RUNTIME_HEARTBEAT_SCHEDULER_LOCATION"), "--format=json",
    ))
    containers = service.get("spec", {}).get("template", {}).get("spec", {}).get("containers") or []
    env = {item.get("name"): item.get("value") for item in (containers[0].get("env") or [])} if containers else {}
    runtime = json.loads(env.get("RUNTIME_TARGET_JSON") or "{}")
    safe = (
        env.get("RUNTIME_TARGET_ENABLED") == "false"
        and env.get("IBKR_DRY_RUN_ONLY") == "false"
        and runtime.get("dry_run_only") is False
        and runtime.get("live_continuity", {}).get("state") == "RECONCILE_ONLY"
    )
    return safe, str(service.get("status", {}).get("url") or "")


def _target_status(target: DrillTarget, now: dt.datetime) -> tuple[dt.date, str]:
    timezone = ZoneInfo(target.timezone)
    day = now.astimezone(timezone).date()
    try:
        safe, service_url = _service_is_drill_only(target)
        if not safe:
            return day, "⚠️ 云端禁单配置不符；需立即检查"
        precheck, live = _job(target.precheck_job), _job(target.live_job)
        if live.get("state") != "PAUSED":
            return day, "⚠️ 实盘任务未暂停；需立即检查"
        if precheck.get("state") != "ENABLED":
            return day, "⚠️ 演练任务未启用"
        if precheck.get("schedule") != target.schedule or precheck.get("timeZone") != target.timezone:
            return day, "⚠️ 每日演练时间配置不符"
        http_target = precheck.get("httpTarget") or {}
        oidc = http_target.get("oidcToken") or {}
        if (
            http_target.get("uri") != f"{service_url}/dry-run"
            or http_target.get("httpMethod") != "POST"
            or not oidc.get("serviceAccountEmail")
            or oidc.get("audience") != service_url
        ):
            return day, "⚠️ 演练任务路由或认证不符"
        attempted_at = _timestamp(precheck.get("lastAttemptTime"))
        minute, hour = (int(value) for value in target.schedule.split()[:2])
        scheduled_at = dt.datetime.combine(day, dt.time(hour=hour, minute=minute), timezone)
        if attempted_at is None or attempted_at < scheduled_at.astimezone(dt.timezone.utc) - dt.timedelta(minutes=5):
            return day, "⚠️ 今日定时演练尚未触发"
        if attempted_at.astimezone(timezone).date() != day:
            return day, "⚠️ 今日定时演练尚未触发"
        if (precheck.get("status") or {}).get("code") not in (None, 0):
            return day, "⚠️ 今日定时演练请求失败"
        reports = _today_reports(target, day)
        report = next((item for item in reports if (started := _report_time(item)) is not None
                       and abs((started - attempted_at).total_seconds()) <= 600), None)
        if report is None:
            return day, "⚠️ 定时请求与演练报告无法对应，结果未验证"
        if report.get("dry_run") is not True:
            return day, "⚠️ 最新报告不是 dry run，结果未验证"
        status = str(report.get("status") or "").lower()
        diagnostics = report.get("diagnostics") or {}
        if status == "skipped" and diagnostics.get("skip_reason") == "market_closed":
            return day, "🗓️ 休市，演练按规则跳过；未下单"
        summary = report.get("summary") or {}
        if status == "ok" and summary.get("execution_status") and summary.get("orders_submitted_count") == 0:
            return day, "✅ 今日模拟周期完成；实际下单 0 笔"
        return day, f"⚠️ 演练未通过完整零下单核验（状态：{status or '未知'}）"
    except (RuntimeError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return day, "⚠️ 无法读取演练证据，结果未验证"


def main(now: dt.datetime | None = None) -> int:
    now = now or dt.datetime.now(dt.timezone.utc)
    targets = _targets()
    if not targets:
        print("No daily drill targets configured")
        return 0
    lines = ["🧪 IBKR 每日模拟演练", "仅检查已配置的只读目标；实盘下单任务应保持关闭。"]
    statuses = []
    for target in targets:
        day, status = _target_status(target, now)
        statuses.append(status)
        lines.append(f"{target.label} · {day.isoformat()}：{status}")
    message = "\n".join(lines)
    alerts = sum(not value.startswith(("✅", "🗓️")) for value in statuses)
    if os.environ.get("DRILL_DIGEST_PREVIEW") == "true":
        print(f"Daily drill preview: targets={len(targets)}, alerts={alerts}")
        return 0
    sent = _send_telegram(message)
    print(f"Daily drill digest sent={sent}; targets={len(targets)}; alerts={alerts}")
    return 0 if sent and alerts == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
