"""Read-only Actions receipt guard. No delivery, credentials lookup, or state writes."""
from __future__ import annotations

import io
import argparse
import json
import os
import re
import sys
import shlex
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping
from pathlib import Path
from zoneinfo import ZoneInfo


# Public diagnostics must never contain exception text, URLs, or credentials.
HISTORY_ERRORS = frozenset({
    "guard_response_too_large", "guard_history_incomplete", "guard_marker_invalid",
    "guard_marker_business_day_missing", "guard_configuration_invalid", "guard_future_business_day",
    "guard_run_invalid", "guard_prior_run_in_progress", "guard_job_history_missing",
    "guard_send_receipt_missing", "guard_send_receipt_expired", "guard_http_access_denied", "guard_http_request_failed",
    "guard_http_unavailable", "guard_transport_unavailable", "guard_history_read_failed",
})
RETRYABLE_HISTORY_ERRORS = frozenset({
    "guard_http_unavailable", "guard_transport_unavailable", "guard_history_incomplete",
    "guard_prior_run_in_progress",
})
MARKER_FIELDS = (
    "schema_version", "business_day", "report_date", "recipient_scope", "delivered", "dry_run", "status", "test_message",
)


def report_date() -> str:
    return datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Singapore")).date().isoformat()


def unified_mode(env: Mapping[str, str]) -> bool:
    from notification_mode import routine_mode
    return routine_mode(env) == "daily_digest"


@dataclass(frozen=True)
class Decision:
    decision: str
    reason: str
    test_message: bool = False
    previous_failures: int = 0
    history_error: str = ""
    history_attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def log(self, day: str, event: str) -> str:
        return (f"digest_resend_guard decision={self.decision} reason={self.reason} "
                f"business_day={day} event={event} test={str(self.test_message).lower()} "
                f"history_error={self.history_error or 'none'} history_attempts={self.history_attempts}")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and urllib.parse.urlsplit(req.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            redirected.remove_header("Authorization")
        return redirected


def api_get(url: str, token: str, *, binary: bool = False) -> Any:
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.build_opener(_SafeRedirect()).open(request, timeout=20) as response:
        data = response.read(1_048_577)
    if len(data) > 1_048_576:
        raise ValueError("guard_response_too_large")
    return data if binary else json.loads(data)


def _pages(url: str, token: str, key: str, max_pages: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        payload = api_get(f"{url}{'&' if '?' in url else '?'}per_page=100&page={page}", token)
        total = payload.get("total_count")
        batch = payload.get(key)
        if (type(total) is not int or total < 0 or total > max_pages * 100
                or not isinstance(batch, list) or not all(isinstance(x, dict) for x in batch)):
            raise ValueError("guard_history_incomplete")
        items.extend(batch)
        if len(items) == total:
            return items
        if not batch or len(batch) < 100 or len(items) > total:
            raise ValueError("guard_history_incomplete")
    raise ValueError("guard_history_incomplete")


def _read_marker(data: bytes, *, legacy_filename: str = "receipt.json") -> dict[str, Any]:
    # Never extract files or retain private fields from legacy full receipts.
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        allowed = {"receipt-public.json"}
        if legacy_filename and re.fullmatch(r"[\w.-]+\.json", legacy_filename):
            allowed.add(legacy_filename)
        if len(entries) != 1 or entries[0].filename not in allowed or entries[0].file_size > 65536:
            raise ValueError("guard_marker_invalid")
        receipt = json.loads(archive.read(entries[0]))
    if (not isinstance(receipt, dict) or type(receipt.get("delivered")) is not bool
            or type(receipt.get("dry_run")) is not bool or not isinstance(receipt.get("status"), str)):
        raise ValueError("guard_marker_invalid")
    if receipt.get("business_day") is not None:
        date.fromisoformat(receipt["business_day"])
    elif receipt.get("delivered") is True or receipt.get("status") == "send_failed":
        raise ValueError("guard_marker_business_day_missing")
    if "test_message" in receipt and type(receipt["test_message"]) is not bool:
        raise ValueError("guard_marker_invalid")
    if entries[0].filename != "receipt-public.json" and receipt.get("schema_version") not in {
        "qsl.daily_digest_receipt.v1", "qsl.daily_digest_delivery_receipt.v1",
    }:
        raise ValueError("guard_marker_invalid")
    return {key: receipt[key] for key in MARKER_FIELDS if key in receipt}


def read_history(business_day: str, env: Mapping[str, str]) -> list[tuple[int, dict[str, Any]]]:
    repo = env.get("GITHUB_REPOSITORY", "")
    api = env.get("GITHUB_API_URL", "").rstrip("/")
    token = env.get("DIGEST_GUARD_TOKEN") or env.get("GITHUB_TOKEN", "")
    workflow = env.get("DIGEST_GUARD_WORKFLOW") or env.get("GITHUB_WORKFLOW_REF", "").split("@", 1)[0].rsplit("/", 1)[-1]
    current = int(env["GITHUB_RUN_ID"])
    attempt = int(env.get("GITHUB_RUN_ATTEMPT", "1"))
    max_pages = int(env.get("DIGEST_GUARD_MAX_PAGES", "10"))
    lookback = int(env.get("DIGEST_GUARD_LOOKBACK_DAYS", "7"))
    if (not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or not api.startswith("https://")
            or not token or not workflow or not 1 <= max_pages <= 100 or not 1 <= lookback <= 90):
        raise ValueError("guard_configuration_invalid")
    day = date.fromisoformat(business_day)
    # Reject future dates: a send cannot predate the date covered by this scan.
    if day > datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York")).date():
        raise ValueError("guard_future_business_day")
    start = (date.fromisoformat(report_date()) if unified_mode(env) else day) - timedelta(days=lookback)
    root = f"{api}/repos/{repo}/actions"
    query = urllib.parse.urlencode({"created": f">={start.isoformat()}"})
    runs = _pages(f"{root}/workflows/{urllib.parse.quote(workflow, safe='')}/runs?{query}", token, "workflow_runs", max_pages)
    history = []
    seen = set()
    for run in runs:
        run_id = int(run["id"])
        if run_id in seen or (run_id == current and attempt == 1):
            continue
        if not isinstance(run.get("event"), str):
            raise ValueError("guard_run_invalid")
        if run["event"] not in {"schedule", "workflow_dispatch"}:
            continue
        seen.add(run_id)
        if run.get("status") == "queued":
            continue
        if run.get("status") != "completed" and run_id != current:
            raise ValueError("guard_prior_run_in_progress")
        artifacts = _pages(f"{root}/runs/{run_id}/artifacts", token, "artifacts", max_pages)
        markers = [a for a in artifacts if a.get("name") == env.get("DIGEST_GUARD_ARTIFACT", "daily-digest-receipt")]
        if not markers:
            stamp = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
            old = (stamp.astimezone(ZoneInfo("Asia/Singapore")).date() < date.fromisoformat(report_date())
                   if unified_mode(env) else stamp.astimezone(ZoneInfo("America/New_York")).date() < day)
            if old:
                continue
            # Maintenance-only or failed-before-send jobs cannot have delivered.
            jobs = _pages(f"{root}/runs/{run_id}/jobs", token, "jobs", max_pages)
            if not jobs:
                raise ValueError("guard_job_history_missing")
            send_step = env.get("DIGEST_GUARD_SEND_STEP", "Aggregate, render, and send daily digest")
            if any(step.get("name") == send_step and step.get("conclusion") != "skipped"
                   for job in jobs for step in job.get("steps", [])):
                raise ValueError("guard_send_receipt_missing")
            continue
        for marker in markers:
            if marker.get("expired") is not False:
                raise ValueError("guard_send_receipt_expired")
            receipt = _read_marker(api_get(f"{root}/artifacts/{int(marker['id'])}/zip", token, binary=True),
                                   legacy_filename=env.get("DIGEST_GUARD_LEGACY_RECEIPT", "receipt.json"))
            if unified_mode(env):
                receipt.setdefault("report_date", datetime.fromisoformat(run["created_at"].replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Singapore")).date().isoformat())
                receipt.setdefault("recipient_scope", "default")
            history.append((run_id, receipt))
    return history


def history_error(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in {401, 403}:
            return "guard_http_access_denied"
        return "guard_http_unavailable" if exc.code == 429 or exc.code >= 500 else "guard_http_request_failed"
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return "guard_transport_unavailable"
    if isinstance(exc, ValueError) and str(exc) in HISTORY_ERRORS:
        return str(exc)
    return "guard_history_read_failed"


def recovery_notice(day: str, env: Mapping[str, str]) -> str:
    date.fromisoformat(day)
    repo = env.get("GITHUB_REPOSITORY", "OWNER/REPO")
    workflow = env.get("DIGEST_GUARD_WORKFLOW") or env.get("GITHUB_WORKFLOW_REF", "").split("@", 1)[0].rsplit("/", 1)[-1]
    command = shlex.join([
        "gh", "workflow", "run", workflow or "daily-digest-notify.yml", "--repo", repo, "--ref", "main",
        "-f", "dry_run=false", "-f", "force_resend=false", "-f", f"business_day={day}",
    ])
    return ("URGENT: Daily digest was not sent because history could not be verified. "
            "Restore readable history, then retry the same business day with the guard enabled. "
            "Do not force resend or retry uncertain delivery.\n" + command)


def evaluate(business_day: str, env: Mapping[str, str], *, dry_run: bool = False) -> Decision:
    if dry_run:
        return Decision("preview", "DRY_RUN")
    event = env.get("GITHUB_EVENT_NAME", "local")
    enabled = env.get("DIGEST_RESEND_GUARD", "true" if env.get("GITHUB_ACTIONS") == "true" else "false")
    force = env.get("DIGEST_FORCE_RESEND", "false")
    if enabled not in {"true", "false"} or force not in {"true", "false"}:
        return Decision("blocked", "CONFIGURATION_INVALID")
    if enabled == "false" and not unified_mode(env):
        return Decision("send", "LOCAL_EXECUTION")
    if event not in {"schedule", "workflow_dispatch"}:
        return Decision("blocked", "UNSUPPORTED_EVENT")
    if event == "workflow_dispatch" and force == "true" and not unified_mode(env):
        return Decision("send", "EXPLICIT_TEST_RESEND", test_message=True)
    try:
        attempts = int(env.get("DIGEST_GUARD_READ_ATTEMPTS", "3"))
        delay = int(env.get("DIGEST_GUARD_RETRY_DELAY_SECONDS", "2"))
        if not 1 <= attempts <= 5 or not 0 <= delay <= 60:
            raise ValueError("guard_configuration_invalid")
    except ValueError:
        return Decision("blocked", "CONFIGURATION_INVALID")
    for attempt in range(1, attempts + 1):
        try:
            history = read_history(business_day, env)
            break
        except Exception as exc:
            code = history_error(exc)
            if attempt == attempts or code not in RETRYABLE_HISTORY_ERRORS:
                return Decision("blocked", "HISTORY_UNAVAILABLE", history_error=code, history_attempts=attempt)
            # Only repeat read operations, never Telegram delivery or state writes.
            time.sleep(min(60, delay * 2 ** (attempt - 1)))
    try:
        deliveries = [run for run, receipt in history if receipt.get("delivered") is True
                      and receipt.get("dry_run") is False and receipt.get("status") == "delivered"
                      and receipt.get("test_message", False) is False]
        failures = [run for run, receipt in history if receipt.get("status") == "blocked_resend_guard"]
        previous_failures = sum(run > max(deliveries, default=0) for run in failures)
        if unified_mode(env):
            scope = env.get("DIGEST_RECIPIENT_SCOPE", "default")
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", scope):
                return Decision("blocked", "CONFIGURATION_INVALID")
            same_report_day = [receipt for _, receipt in history
                               if receipt.get("report_date") == report_date()
                               and receipt.get("recipient_scope", "default") == scope
                               and receipt.get("dry_run") is False]
            if any(receipt.get("delivered") is True for receipt in same_report_day):
                return Decision("skip", "ALREADY_DELIVERED", history_attempts=attempt)
            if any(receipt.get("status") in {"send_failed", "business_digest_failed", "sending"} for receipt in same_report_day):
                return Decision("blocked", "DELIVERY_UNCERTAIN", history_attempts=attempt)
            from notification_mode import consolidation_enabled
            if consolidation_enabled(env):
                no_attempt = {"blocked_missing_secrets", "skipped_duplicate", "blocked_resend_guard", "skipped_empty"}
                if any(receipt.get("status") not in no_attempt for receipt in same_report_day):
                    return Decision("blocked", "DELIVERY_UNCERTAIN", history_attempts=attempt)
                same_window = [receipt for _, receipt in history
                               if receipt.get("business_day") == business_day
                               and receipt.get("recipient_scope", "default") == scope
                               and receipt.get("dry_run") is False]
                if any(receipt.get("delivered") is True for receipt in same_window):
                    return Decision("skip", "ALREADY_DELIVERED", history_attempts=attempt)
                if any(receipt.get("status") in {"send_failed", "business_digest_failed", "sending"} for receipt in same_window):
                    return Decision("blocked", "DELIVERY_UNCERTAIN", history_attempts=attempt)
                if any(receipt.get("status") not in no_attempt for receipt in same_window):
                    return Decision("blocked", "DELIVERY_UNCERTAIN", history_attempts=attempt)
                # Historical previews remain available via dry-run. A manual
                # date override cannot create a second summary for an old window
                # after its receipts have fallen outside the history lookback.
                from notification_snapshot.unified_contract import sgt_business_window
                current = sgt_business_window(datetime.now(timezone.utc)).business_day - timedelta(days=1)
                if business_day != current.isoformat():
                    return Decision("blocked", "BUSINESS_WINDOW_NOT_CURRENT", history_attempts=attempt)
            return Decision("send", "FIRST_SEND", history_attempts=attempt)
        same_day = [receipt for _, receipt in history if receipt.get("business_day") == business_day
                    and receipt.get("dry_run") is False and not receipt.get("test_message", False)]
        if any(receipt.get("delivered") is True and receipt.get("status") == "delivered" for receipt in same_day):
            return Decision("skip", "ALREADY_DELIVERED", previous_failures=previous_failures, history_attempts=attempt)
        if any(receipt.get("status") == "send_failed" for receipt in same_day):
            return Decision("blocked", "DELIVERY_UNCERTAIN", previous_failures=previous_failures, history_attempts=attempt)
        return Decision("send", "FIRST_SEND", previous_failures=previous_failures, history_attempts=attempt)
    except Exception as exc:
        # Never echo HTTP errors, URLs, tokens, or artifact contents.
        return Decision("blocked", "HISTORY_UNAVAILABLE", history_error=history_error(exc), history_attempts=attempt)


def skip_receipt(day: str, locale: str, decision: Decision) -> dict[str, Any]:
    return {
        "schema_version": "qsl.daily_digest_delivery_receipt.v1", "business_day": day,
        "locale": "en" if locale.lower().startswith("en") else "zh", "dry_run": False, "delivered": False,
        "status": "skipped_duplicate" if decision.decision == "skip" else "blocked_resend_guard",
        "resend_guard": decision.to_dict(), "test_message": False,
        "report_date": report_date(), "recipient_scope": os.environ.get("DIGEST_RECIPIENT_SCOPE", "default"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    day = os.environ.get("DIGEST_BUSINESS_DAY") or datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York")).date().isoformat()
    date.fromisoformat(day)
    decision = evaluate(day, os.environ, dry_run=os.environ.get("DIGEST_DRY_RUN") == "true")
    print(decision.log(day, os.environ.get("GITHUB_EVENT_NAME", "local")))
    if decision.decision in {"skip", "blocked"}:
        args.write_receipt.parent.mkdir(parents=True, exist_ok=True)
        locale = os.environ.get("DIGEST_LOCALE") or os.environ.get("QSL_NOTIFY_LANG") or os.environ.get("NOTIFY_LANG", "zh")
        args.write_receipt.write_text(json.dumps(skip_receipt(day, locale, decision)) + "\n")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write(f"proceed={str(decision.decision in {'send', 'preview'}).lower()}\n")
    if decision.decision == "blocked":
        print(f"::error::Daily digest not sent: resend guard {decision.reason}.", file=sys.stderr)
        if decision.reason == "HISTORY_UNAVAILABLE":
            print("::error::" + recovery_notice(day, os.environ).replace("\n", " "), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
