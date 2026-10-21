"""Notification-only projection; no holdings or account-facts export."""
from __future__ import annotations
from collections.abc import Mapping, Sequence
from typing import Any
SCHEMA_VERSION = "qsl.digest_candidates.v1"


def _as_mapping(value: object) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None



def _as_str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""



def _parse_timestamp(raw: str):
    """Parse an ISO-8601 timestamp; naive values are treated as UTC."""

    import datetime as _dt

    text = raw.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        value = _dt.datetime.fromisoformat(text)
    except ValueError:
        if len(text) >= 10 and text[4] == "-" and text[7] == "-":
            try:
                return _dt.datetime.combine(_dt.date.fromisoformat(text[:10]), _dt.time(12), tzinfo=_dt.timezone.utc)
            except ValueError:
                return None
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=_dt.timezone.utc)
    return value



def _latest_notification_block_reason(reports, validate_receipt) -> dict[str, Any]:
    """Annotate the latest admitted run; never change daily execution counts.

    Validation belongs to the caller's existing QPK installation. Without it,
    this standalone adapter retains its previous dependency-free behavior.
    """
    if validate_receipt is None or not reports:
        return {}
    identities = [report.get("run_id") for report in reports]
    if (any(not isinstance(value, str) or not value for value in identities)
            or len(set(identities)) != len(identities)):
        return {}
    target = reports[0].get("runtime_target")
    if not isinstance(target, Mapping) or any(report.get("runtime_target") != target for report in reports):
        return {}
    stamps = [_parse_timestamp(_as_str(report.get("started_at"))) for report in reports]
    if any(stamp is None for stamp in stamps):
        return {}
    latest = max(stamps)
    selected = [report for report, stamp in zip(reports, stamps) if stamp == latest]
    if len(selected) != 1:
        return {}
    report = selected[0]
    if (report.get("dry_run") is not False or report.get("status") not in {"ok", "completed", "success"}
            or report.get("errors") != [] or (_as_mapping(report.get("error_summary")) or {}).get("errors")):
        return {}
    # The existing validator binds platform/profile/revision/mode and checks
    # the receipt's content digest. Never reconstruct its schema here.
    try:
        receipt = validate_receipt(report)
    except (ValueError, TypeError):
        return {}
    if (not isinstance(receipt, Mapping) or receipt.get("outcome") != "risk_blocked"
            or receipt.get("broker_confirmation") != "not_applicable"):
        return {}
    finished = _parse_timestamp(_as_str(report.get("finished_at")))
    observed = _parse_timestamp(_as_str(receipt.get("observed_at")))
    if finished is None or observed is None or not latest <= observed <= finished:
        return {}
    summary = _as_mapping(report.get("summary")) or {}
    # An old receipt cannot override contradictory activity in the same run.
    arrays = ("orders_submitted", "option_orders_submitted", "orders_pending", "option_orders_pending",
              "orders_filled", "option_orders_filled", "orders_partially_filled", "option_orders_partially_filled")
    counters = ("orders_submitted_count", "orders_pending_count", "orders_filled_count",
                "orders_partially_filled_count", "orders_previewed_count", "order_count", "fill_count")
    if (any(key in summary and summary[key] not in ([], ()) for key in arrays)
            or any(key in summary and (type(summary[key]) is not int or summary[key] != 0) for key in counters)
            or any(key in summary and summary[key] is not False for key in ("broker_submission_done", "action_done"))):
        return {}
    facts = _as_mapping(summary.get("notification_digest")) or {}
    no_submission = (facts.get("schema_version") == "qsl.notification_cycle.v1"
                     and facts.get("dry_run") is False
                     and all(type(facts.get(key)) is int and facts[key] == 0 for key in ("order_count", "fill_count")))
    return {"reason_code": "strategy_risk_rejected", "observed_value": None, "limit": None,
            "order_submitted_this_run": False if no_submission else None}



def project_notification_events(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Export one row per source run, with only that run's counts and events.

    Monitor observations share the observed report's source_run_id. Incidents
    without a business receipt stay on the immediate path outside this export.
    """
    if not records:
        raise ValueError("notification_events_missing")
    first = records[0]["event"]
    identity = ("platform_id", "opaque_account_uid", "target_id", "strategy_profile", "business_day")
    if any(not first.get(k) for k in identity):
        raise ValueError("notification_candidate_identity_unknown")
    if any(any(item["event"].get(k) != first[k] for k in identity) for item in records):
        raise ValueError("notification_candidate_identity_conflict")
    runs: dict[str, list[Mapping[str, Any]]] = {}
    for item in records:
        event = item["event"]
        if event.get("coverage_status") != "known" or not event.get("source_run_id"):
            raise ValueError("notification_candidate_identity_unknown")
        if item.get("actually_ran") is not True:
            raise ValueError("notification_candidate_business_run_required")
        runs.setdefault(event["source_run_id"], []).append(item)
    rows = []
    for run_id, items in sorted(runs.items()):
        business = [item for item in items if item.get("actually_ran") is True]
        alert = any(item.get("conflict") is True or item["event"]["event_kind"] not in {
            "routine_cycle", "healthy_heartbeat", "order_submitted", "fill"} for item in items)
        row = {k: first[k] for k in identity}
        row.update(source_run_id=run_id, actually_ran=bool(business), cycle_count=int(bool(business)),
                   status="alert" if alert else "ok", evidence_provenance="candidates",
                   persistence_status="persisted", delivery_status="pending_central",
                   as_of=max((item["event"]["occurred_at"] for item in items), key=_parse_timestamp),
                   notification_events=list({item["event"]["dedupe_key"]: dict(item["event"]) for item in items}.values()))
        field_status = {"cycle_count": "known"}
        real = [item for item in business if item.get("dry_run") is False]
        count_conflict = False
        for field in ("order_count", "fill_count"):
            known = {item["event"][field] for item in real if type(item["event"].get(field)) is int}
            conflict = len(known) > 1 or any(item.get("conflict") for item in real)
            value = next(iter(known)) if len(known) == 1 and not conflict else None
            row[field] = value
            field_status[field] = "known" if value is not None else "unknown"
            if conflict:
                count_conflict = True
                alert = True
                row["status"] = "alert"
        row["field_status"] = field_status
        annotations = [item["block_reason"] for item in items if item.get("block_reason")]
        if annotations and not count_conflict and all(value == annotations[0] for value in annotations):
            row["block_reason"] = dict(annotations[0])
        for name in ("account_alias", "routine_decisions"):
            observations = [item[name] for item in items if item.get(name)]
            if (observations and not any(name in item.get("_display_conflicts", ()) for item in items)
                    and all(value == observations[0] for value in observations)):
                row[name] = observations[0]
        if alert:
            row.update(reason_code="notification_incident_observed", execution_status="execution_alert")
        else:
            row["execution_status"] = "executed" if row["order_count"] else "no_action"
        if business and all(item.get("dry_run") is True for item in business):
            row["execution_mode"] = "dry_run"
        rows.append(row)
    return {"schema_version": SCHEMA_VERSION, "producer_status": "projected", "runs": rows}
