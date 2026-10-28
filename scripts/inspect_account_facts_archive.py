"""Inspect one protected runtime-report day without publishing or broker access."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
import subprocess

from scripts import diagnose_account_facts_identity as identity
from scripts import diagnose_ibkr_flex_runtime_binding as binding
from scripts import publish_account_facts_from_report as publisher


class ArchiveError(ValueError):
    pass


def archive_day(value: str, *, now: datetime, allow_today: bool = False) -> date:
    try:
        day = date.fromisoformat(value)
    except (ValueError, TypeError):
        raise ArchiveError("archive_date_invalid") from None
    today = now.astimezone(timezone.utc).date()
    if day.isoformat() != value or day < today - timedelta(days=365) or day > today or (day == today and not allow_today):
        raise ArchiveError("archive_date_outside_retention")
    return day


def report_uris(*, day: date, expected: dict, now: datetime, whole_month: bool = False) -> list[str]:
    prefix = expected["expected_report_prefix"].rstrip("/")
    filename_prefix = day.strftime("%Y%m" if whole_month else "%Y%m%dT")
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
    if len(result.stdout) > 1024 * 1024 or len(lines) > (2048 if whole_month else 64):
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



def resolve_profile_slot(env: dict) -> str:
    """Select one existing slot by its protected runtime identity before storage I/O."""
    slots, targets, project, _ = binding.configuration(env)
    if len(targets) != 1:
        raise ArchiveError("archive_target_invalid")
    _, target = next(iter(targets.values()))
    matches = [slot for slot, row in slots.items() if row["project_id"] == project and all(
        tuple(row[field]) == binding.account_selector(target.get(field)) if field == "account_selector"
        else row[field] == target.get(field) for field in binding.IDENTITY_FIELDS)]
    if len(matches) != 1:
        raise ArchiveError("archive_target_not_unique")
    if matches[0] == "primary":
        return publisher.IBKR_ACCOUNT_FACTS_PRIMARY_TARGET
    if matches[0] not in publisher.IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS:
        raise ArchiveError("archive_target_not_supported")
    return matches[0]

def projection_metadata(projected: dict) -> dict:
    payload = publisher.prepare_sync_payload(projected)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return {"observation_date": projected["observation_date"],
            "source_binding_id_sha256": hashlib.sha256(projected["source_binding"]["id"].encode()).hexdigest(),
            "projection_sha256": hashlib.sha256(encoded).hexdigest(),
            "net_assets_available": projected["broker_reported_balances"][0]["net_assets"] is not None,
            "cash_record_count": len(projected["cash"]),
            "usd_cash_source_tags": sorted({row["source_tag"] for row in projected["cash"]
                                             if row["currency"] == "USD"}),
            "observed_at": projected["observed_started_at"]}


def inspect_report(report, *, uri: str, expected: dict, day: date) -> dict:
    projected = publisher.project_ibkr_account_facts_history(
        report, source_report_uri=uri, **expected,
    )
    safe = {"report_name": uri.rsplit("/", 1)[-1],
            "snapshot_present": (isinstance(report, dict) and isinstance(report.get("summary"), dict)
                                 and isinstance(report["summary"].get("account_facts"), dict))}
    if projected.get("status") == "skipped":
        safe.update(projected)
        declared_revision = publisher._exact_text(report.get("diagnostics", {}).get("runtime_revision")) \
            if isinstance(report, dict) and isinstance(report.get("diagnostics"), dict) else ""
        if (projected.get("reason") == "runtime_target_mismatch"
                and projected.get("mismatched_fields") == ["runtime_revision"] and declared_revision):
            # Preview only: establish whether the original report has otherwise
            # valid same-account facts under its declared revision. Preserve the
            # configured-source rejection; a hash does not admit this source.
            candidate = publisher.project_ibkr_account_facts_history(
                report, source_report_uri=uri,
                **{**expected, "expected_runtime_revision": declared_revision},
            )
            if candidate.get("status") != "skipped" and candidate["observation_date"] == day.isoformat():
                safe["historical_candidate"] = {**projection_metadata(candidate),
                                                "receiver_admission_verified": False}
            else:
                safe["historical_candidate_rejection"] = candidate.get("reason", "archive_observation_date_mismatch")
        return safe
    if projected["observation_date"] != day.isoformat():
        return {**safe, "status": "skipped", "reason": "archive_observation_date_mismatch"}
    # The original pure projector enforces the full report/runtime/account
    # identity and timestamp interval. No expected revision or clock is replaced.
    return {**safe, "status": "verified", **projection_metadata(projected)}


def main() -> int:
    stage = "context"
    report_name = None
    try:
        if (os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
                or os.environ.get("GITHUB_REF") != "refs/heads/main"):
            raise ArchiveError("manual_main_required")
        requested = os.environ.get("IBKR_ACCOUNT_FACTS_TARGET")
        profile_mode = requested in binding.PROFILES
        slot = resolve_profile_slot(os.environ) if profile_mode else requested
        if slot not in publisher.IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS and not (
                profile_mode and slot == publisher.IBKR_ACCOUNT_FACTS_PRIMARY_TARGET):
            raise ArchiveError("archive_target_invalid")
        now = datetime.now(timezone.utc)
        day = archive_day(os.environ.get("IBKR_ACCOUNT_FACTS_ARCHIVE_DATE", ""), now=now, allow_today=profile_mode)
        os.environ["IBKR_ACCOUNT_FACTS_TARGET"] = slot
        expected = identity.resolve_expected()
        stage = "listing"
        mode = os.environ.get("IBKR_ACCOUNT_FACTS_ARCHIVE_MODE", "day")
        if mode not in {"day", "month-metadata", "month-snapshots"} or (profile_mode and mode != "day"):
            raise ArchiveError("archive_mode_invalid")
        uris = report_uris(day=day, expected=expected, now=now, whole_month=mode != "day")
        if mode == "month-metadata":
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
        if len(uris) > 64:
            raise ArchiveError("archive_read_limit")
        rows = []
        for uri in uris:
            report_name = uri.rsplit("/", 1)[-1]
            report_day = datetime.strptime(uri.rsplit("/", 1)[-1], "%Y%m%dT%H%M%SZ.json").date()
            try:
                archive_day(report_day.isoformat(), now=now, allow_today=profile_mode)
            except ArchiveError:
                rows.append({"report_name": uri.rsplit("/", 1)[-1], "status": "skipped",
                             "reason": "archive_date_outside_retention"})
                continue
            stage = "report_read"
            report = publisher._load_gcs_report(uri, project_id=expected["expected_project_id"])
            stage = "projection"
            rows.append(inspect_report(report, uri=uri, expected=expected, day=report_day if mode == "month-snapshots" else day))
        result = {"status": "inspected", **({"profile": requested} if profile_mode else {"slot": slot}),
                  **({"month": day.strftime("%Y-%m")} if mode == "month-snapshots" else {"date": day.isoformat()}),
                  "report_count": len(rows), "records": rows,
                  "broker_requests": 0, "business_writes": 0}
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ArchiveError, publisher._ProjectionError, binding.BindingError) as exc:
        reason = exc.reason if isinstance(exc, publisher._ProjectionError) else str(exc)
        if isinstance(exc, binding.BindingError) and reason not in {
            "configuration_invalid", "target_not_supported", "target_not_unique",
        }:
            reason = "configuration_invalid"
        print(json.dumps({"status": "stopped", "stage": stage, "reason": reason}))
        return 1
    except Exception as exc:
        error_type = type(exc).__name__
        print(json.dumps({"status": "stopped", "stage": stage, "reason": "archive_inspection_failed",
                          "error_type": error_type if error_type in {
                              "KeyError", "TypeError", "ValueError", "AttributeError", "OverflowError",
                          } else "other",
                          "report_name": report_name if publisher._REPORT_RUN_ID.fullmatch(report_name or "") else None}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
