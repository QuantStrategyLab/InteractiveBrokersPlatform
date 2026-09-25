#!/usr/bin/env python3
"""Send one daily, read-only summary for the two IBKR drill targets."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from dataclasses import dataclass
from zoneinfo import ZoneInfo

try:
    from scripts.execution_report_heartbeat import _send_telegram
except ModuleNotFoundError:
    from execution_report_heartbeat import _send_telegram


PROJECT = "interactivebrokersquant"
LOCATION = "us-central1"
REPORT_ROOT = "gs://qsl-runtime-logs-shared/execution-reports/interactive_brokers"
TIMEZONE = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class DrillTarget:
    account: str
    profile: str
    scope: str
    service: str

    @property
    def precheck_job(self) -> str:
        return f"{self.service.removesuffix('-service')}-precheck-scheduler"

    @property
    def live_job(self) -> str:
        return f"{self.service.removesuffix('-service')}-scheduler"

    @property
    def report_prefix(self) -> str:
        return f"{REPORT_ROOT}/{self.profile}/{self.scope}"


TARGETS = (
    DrillTarget("U18308207", "global_etf_rotation", "live-u18308207", "interactive-brokers-quant-live-u18308207-service"),
    DrillTarget("U18336562", "russell_top50_leader_rotation", "live-u18336562", "interactive-brokers-quant-live-u18336562-service"),
)


def _gcloud(*args: str) -> str:
    result = subprocess.run(("gcloud", *args), capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"gcloud {args[0]} {args[1]} failed ({result.returncode})")
    return result.stdout


def _job(job_name: str) -> dict:
    output = _gcloud(
        "scheduler", "jobs", "describe", job_name,
        "--project", PROJECT, "--location", LOCATION, "--format=json",
    )
    return json.loads(output)


def _report_time(report: dict) -> dt.datetime | None:
    return _timestamp(report.get("started_at"))


def _timestamp(raw_value: object) -> dt.datetime | None:
    raw = str(raw_value or "").strip()
    try:
        value = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else None


def _today_reports(target: DrillTarget, day: dt.date) -> list[dict]:
    # A New York calendar day can span two UTC dates and, at a month boundary,
    # two UTC month directories. List only those exact month prefixes.
    start = dt.datetime.combine(day, dt.time.min, TIMEZONE).astimezone(dt.timezone.utc)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, TIMEZONE).astimezone(dt.timezone.utc)
    months = {start.strftime("%Y-%m"), end.strftime("%Y-%m")}
    utc_dates = {start.strftime("%Y%m%d"), end.strftime("%Y%m%d")}
    reports: list[dict] = []
    for month in sorted(months):
        pattern = f"{target.report_prefix}/{month}/*"
        listing = subprocess.run(
            ("gcloud", "storage", "ls", pattern, "--project", PROJECT),
            capture_output=True, text=True, check=False,
        )
        if listing.returncode != 0:
            # An empty month is normal, but an access failure must be visible.
            if "matched no objects" in listing.stderr.lower() or "not found" in listing.stderr.lower():
                continue
            raise RuntimeError(f"GCS report listing failed for {target.account}")
        for uri in listing.stdout.splitlines():
            if not uri.endswith(".json") or not uri.startswith(target.report_prefix + "/"):
                continue
            if uri.rsplit("/", 1)[-1][:8] not in utc_dates:
                continue
            report = json.loads(_gcloud("storage", "cat", uri, "--project", PROJECT))
            when = _report_time(report)
            if when is None or when.astimezone(TIMEZONE).date() != day:
                continue
            if (
                report.get("service_name") != target.service
                or report.get("strategy_profile") != target.profile
                or report.get("account_scope") != target.scope
            ):
                continue
            reports.append(report)
    return sorted(reports, key=lambda item: _report_time(item) or start, reverse=True)


def _service_is_drill_only(target: DrillTarget) -> bool:
    service = json.loads(_gcloud(
        "run", "services", "describe", target.service,
        "--project", PROJECT, "--region", LOCATION, "--format=json",
    ))
    containers = service.get("spec", {}).get("template", {}).get("spec", {}).get("containers") or []
    env = {item.get("name"): item.get("value") for item in (containers[0].get("env") or [])} if containers else {}
    try:
        runtime_target = json.loads(env.get("RUNTIME_TARGET_JSON") or "{}")
    except json.JSONDecodeError:
        return False
    return (
        env.get("RUNTIME_TARGET_ENABLED") == "false"
        and env.get("IBKR_DRY_RUN_ONLY") == "false"
        and runtime_target.get("dry_run_only") is False
        and runtime_target.get("live_continuity", {}).get("state") == "RECONCILE_ONLY"
    )


def _target_status(target: DrillTarget, day: dt.date) -> str:
    try:
        if not _service_is_drill_only(target):
            return "⚠️ 云端禁单配置不符；需立即检查"
        precheck = _job(target.precheck_job)
        live = _job(target.live_job)
        if live.get("state") != "PAUSED":
            return "⚠️ 实盘任务未暂停；需立即检查"
        if precheck.get("state") != "ENABLED":
            return "⚠️ 演练任务未启用"
        if precheck.get("schedule") != "45 9 * * *" or precheck.get("timeZone") != "America/New_York":
            return "⚠️ 每日演练时间配置不符"
        http_target = precheck.get("httpTarget") or {}
        if (
            not str(http_target.get("uri") or "").endswith("/dry-run")
            or http_target.get("httpMethod") != "POST"
            or http_target.get("oidcToken", {}).get("serviceAccountEmail")
            != "ibkr-platform-scheduler@interactivebrokersquant.iam.gserviceaccount.com"
        ):
            return "⚠️ 演练任务未指向 /dry-run"
        attempted_at = _timestamp(precheck.get("lastAttemptTime"))
        if attempted_at is None or attempted_at.astimezone(TIMEZONE).date() != day:
            return "⚠️ 今日定时演练尚未触发"
        if (precheck.get("status") or {}).get("code") not in (None, 0):
            return "⚠️ 今日定时演练请求失败"
        reports = _today_reports(target, day)
        if not reports:
            return "⚠️ 今日未找到演练报告，结果未验证"
        report = next(
            (
                item for item in reports
                if (started := _report_time(item)) is not None
                and abs((started - attempted_at).total_seconds()) <= 600
            ),
            None,
        )
        if report is None:
            return "⚠️ 定时请求与演练报告无法对应，结果未验证"
        if report.get("dry_run") is not True:
            return "⚠️ 最新报告不是 dry run，结果未验证"
        status = str(report.get("status") or "").lower()
        diagnostics = report.get("diagnostics") or {}
        if status == "skipped" and diagnostics.get("skip_reason") == "market_closed":
            return "🗓️ 休市，演练按规则跳过；未下单"
        summary = report.get("summary") or {}
        if status == "ok" and summary.get("execution_status") and summary.get("orders_submitted_count") == 0:
            return "✅ 今日模拟周期完成；实际下单 0 笔"
        return f"⚠️ 演练未通过完整零下单核验（状态：{status or '未知'}）"
    except (RuntimeError, ValueError, TypeError, json.JSONDecodeError):
        return "⚠️ 无法读取演练证据，结果未验证"


def main(now: dt.datetime | None = None) -> int:
    now = now or dt.datetime.now(dt.timezone.utc)
    day = now.astimezone(TIMEZONE).date()
    lines = [f"🧪 IBKR 每日模拟演练 · {day.isoformat()}", "仅检查 U183 两账户；实盘下单任务保持关闭。"]
    statuses = []
    for target in TARGETS:
        status = _target_status(target, day)
        statuses.append(status)
        lines.append(f"{target.account}：{status}")
    message = "\n".join(lines)
    print(message)
    if os.environ.get("DRILL_DIGEST_PREVIEW") == "true":
        return 0
    if not _send_telegram(message):
        return 1
    return 0 if all(value.startswith(("✅", "🗓️")) for value in statuses) else 1


if __name__ == "__main__":
    raise SystemExit(main())
