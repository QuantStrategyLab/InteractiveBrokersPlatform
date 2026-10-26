"""Inspect one protected runtime-report day without publishing or broker access."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
import subprocess

from scripts import diagnose_account_facts_identity as identity
from scripts import publish_account_facts_from_report as publisher


class ArchiveError(ValueError):
    pass


def archive_day(value: str, *, now: datetime) -> date:
    try:
        day = date.fromisoformat(value)
    except (ValueError, TypeError):
        raise ArchiveError("archive_date_invalid") from None
    today = now.astimezone(timezone.utc).date()
    if day.isoformat() != value or not today - timedelta(days=365) <= day < today:
        raise ArchiveError("archive_date_outside_retention")
    return day


def report_uris(*, day: date, expected: dict, now: datetime, metadata_only: bool = False) -> list[str]:
    prefix = expected["expected_report_prefix"].rstrip("/")
    filename_prefix = day.strftime("%Y%m" if metadata_only else "%Y%m%dT")
    match = f"{prefix}/{day:%Y-%m}/{filename_prefix}*.json"
    result = subprocess.run(
        ["gcloud", "storage", "ls", match, "--project", expected["expected_project_id"]],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if result.returncode != 0:
        if "matched no objects" in result.stderr.lower():
            return []
        raise ArchiveError("archive_listing_failed")
    lines = result.stdout.splitlines()
    if len(result.stdout) > 1024 * 1024 or len(lines) > (2048 if metadata_only else 64):
        raise ArchiveError("archive_listing_limit")
    uris = set()
    for uri in lines:
        name = uri.rsplit("/", 1)[-1]
        try:
            valid = publisher._named_report_uri(prefix=prefix, report_name=name, now=now)
        except publisher._ProjectionError:
            raise ArchiveError("archive_listing_invalid") from None
        if uri != valid or not name.startswith(filename_prefix):
            raise ArchiveError("archive_listing_invalid")
        uris.add(uri)
    return sorted(uris, reverse=True)


def projection_metadata(projected: dict) -> dict:
    payload = publisher.prepare_sync_payload(projected)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return {"observation_date": projected["observation_date"],
            "source_binding_id_sha256": hashlib.sha256(projected["source_binding"]["id"].encode()).hexdigest(),
            "projection_sha256": hashlib.sha256(encoded).hexdigest(),
            "net_assets_available": projected["broker_reported_balances"][0]["net_assets"] is not None}


def inspect_report(report, *, uri: str, expected: dict, day: date) -> dict:
    projected = publisher.project_ibkr_account_facts_history(
        report, source_report_uri=uri, **expected,
    )
    safe = {"report_name": uri.rsplit("/", 1)[-1]}
    if projected.get("status") == "skipped":
        safe.update(projected)
        if (projected.get("reason") == "runtime_target_mismatch"
                and projected.get("mismatched_fields") == ["runtime_revision"]):
            # Preview only: establish whether the original report has otherwise
            # valid same-account facts under its declared revision. Preserve the
            # configured-source rejection; a hash does not admit this source.
            candidate = publisher.project_ibkr_account_facts_history(
                report, source_report_uri=uri,
                **{**expected, "expected_runtime_revision": report["diagnostics"]["runtime_revision"]},
            )
            if candidate.get("status") != "skipped" and candidate["observation_date"] == day.isoformat():
                safe["historical_candidate"] = {**projection_metadata(candidate),
                                                "receiver_admission_verified": False}
        return safe
    if projected["observation_date"] != day.isoformat():
        return {**safe, "status": "skipped", "reason": "archive_observation_date_mismatch"}
    # The original pure projector enforces the full report/runtime/account
    # identity and timestamp interval. No expected revision or clock is replaced.
    return {**safe, "status": "verified", **projection_metadata(projected)}


def main() -> int:
    stage = "context"
    try:
        if (os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
                or os.environ.get("GITHUB_REF") != "refs/heads/main"):
            raise ArchiveError("manual_main_required")
        slot = os.environ.get("IBKR_ACCOUNT_FACTS_TARGET")
        if slot not in publisher.IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS:
            raise ArchiveError("archive_target_invalid")
        now = datetime.now(timezone.utc)
        day = archive_day(os.environ.get("IBKR_ACCOUNT_FACTS_ARCHIVE_DATE", ""), now=now)
        expected = identity.resolve_expected()
        stage = "listing"
        mode = os.environ.get("IBKR_ACCOUNT_FACTS_ARCHIVE_METADATA_ONLY", "false")
        if mode not in {"true", "false"}:
            raise ArchiveError("archive_mode_invalid")
        uris = report_uris(day=day, expected=expected, now=now, metadata_only=mode == "true")
        if mode == "true":
            counts = {}
            for uri in uris:
                stamp = datetime.strptime(uri.rsplit("/", 1)[-1], "%Y%m%dT%H%M%SZ.json")
                key = stamp.date().isoformat()
                counts[key] = counts.get(key, 0) + 1
            print(json.dumps({"status": "inspected", "mode": "metadata_inventory", "slot": slot,
                              "month": day.strftime("%Y-%m"), "report_count": len(uris),
                              "report_counts_by_filename_date": counts,
                              "financial_bodies_read": False, "broker_requests": 0, "business_writes": 0},
                             sort_keys=True))
            return 0
        rows = []
        for uri in uris:
            stage = "report_read"
            report = publisher._load_gcs_report(uri, project_id=expected["expected_project_id"])
            stage = "projection"
            rows.append(inspect_report(report, uri=uri, expected=expected, day=day))
        result = {"status": "inspected", "slot": slot, "date": day.isoformat(),
                  "report_count": len(rows), "records": rows,
                  "broker_requests": 0, "business_writes": 0}
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ArchiveError, publisher._ProjectionError) as exc:
        reason = exc.reason if isinstance(exc, publisher._ProjectionError) else str(exc)
        print(json.dumps({"status": "stopped", "stage": stage, "reason": reason}))
        return 1
    except Exception:
        print(json.dumps({"status": "stopped", "stage": stage, "reason": "archive_inspection_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
