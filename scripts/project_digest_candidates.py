"""Project IBKR runtime_report / account-facts evidence into QRS DIGEST_CANDIDATES JSON.

Pure adapter: no broker calls, no GCS, no QRS POST, no secret reads, no QPK import.
Missing fill/order counts stay null with explicit field_status — never invent 0.
Identity (opaque_account_uid / target_id) must be supplied by the caller from
protected configuration; this module does not mint them from thin air.

platform_id is fixed to ``ibkr`` (QRS short name), not ``interactive_brokers``.
"""

from __future__ import annotations

import argparse
import json
import sys
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

PLATFORM_ID = "ibkr"
SCHEMA_VERSION = "qsl.digest_candidates.v1"
RUNTIME_REPORT_SCHEMA = "runtime_report.v1"
EVIDENCE_PROVENANCE = "candidates"
# Documented sole-target fallback only when report omits strategy_profile.
DEFAULT_STRATEGY = "global_etf_rotation"

_RAN_ACTIVITIES = frozenset(
    {
        "no_signal",
        "no_rebalance",
        "no_submission",
        "no_action",
        "not_due",
        "submitted",
        "broker_acknowledged",
        "partially_filled",
        "filled",
        "previewed",
        "blocked",
        "failed",
        "unknown",
        "reconciliation_required",
    }
)
_ALERT_ACTIVITIES = frozenset(
    {"blocked", "failed", "unknown", "reconciliation_required"}
)
_ALERT_STATUSES = frozenset(
    {"error", "failed", "failure", "cancelled", "canceled", "timed_out", "conflict"}
)
_SIGNAL_BY_ACTIVITY = {
    "no_signal": "no_signal",
    "no_rebalance": "no_rebalance",
    "no_submission": "no_action",
    "no_action": "no_action",
    "not_due": "not_due",
    "submitted": "order_submitted",
    "broker_acknowledged": "order_acknowledged",
    "partially_filled": "partially_filled",
    "filled": "filled",
    "previewed": "previewed",
    "blocked": "blocked",
    "failed": "failed",
    "unknown": "unknown",
    "reconciliation_required": "reconciliation_required",
}
_REBALANCE_BY_ACTIVITY = {
    "no_signal": "no_rebalance",
    "no_rebalance": "no_rebalance",
    "no_submission": "no_order",
    "no_action": "no_order",
    "not_due": "no_order",
    "submitted": "rebalance",
    "broker_acknowledged": "rebalance",
    "partially_filled": "rebalance",
    "filled": "rebalance",
    "previewed": "pending",
    "blocked": "pending",
    "failed": "pending",
    "unknown": "pending",
    "reconciliation_required": "pending",
}


def _as_mapping(value: object) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None


def _as_str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


_BROKER_ACCOUNT_RE = re.compile(r"(?i)\b(D?U\d{5,})\b")


def _normalize_broker_account_id(raw: object) -> str:
    """Return canonical ``U######`` / ``DU######`` when present; else empty."""
    text = _as_str(raw)
    if not text:
        return ""
    compact = text.strip()
    m = re.fullmatch(r"(?i)(du|u)(\d{5,})", compact)
    if m:
        prefix = m.group(1).upper()
        digits = m.group(2)
        return ("DU" if prefix == "DU" else "U") + digits
    found = _BROKER_ACCOUNT_RE.search(text)
    if not found:
        return ""
    return _normalize_broker_account_id(found.group(1))


def _labels_from_runtime_target(target: Mapping[str, Any] | None) -> tuple[str, str]:
    """Return (account_hint, account_scope) from a runtime_target-like mapping."""
    if not isinstance(target, Mapping):
        return "", ""
    scope = _as_str(target.get("account_scope"))
    hint = ""
    selector = target.get("account_selector")
    if isinstance(selector, str):
        hint = _normalize_broker_account_id(selector)
    elif isinstance(selector, Sequence) and not isinstance(selector, (str, bytes)):
        for item in selector:
            hint = _normalize_broker_account_id(item)
            if hint:
                break
    if not hint:
        hint = _normalize_broker_account_id(scope)
    return hint, scope


def _labels_from_report(report: Mapping[str, Any]) -> tuple[str, str]:
    target = _as_mapping(report.get("runtime_target"))
    hint, scope = _labels_from_runtime_target(target)
    if not scope:
        scope = _as_str(report.get("account_scope"))
    if not hint:
        hint = _normalize_broker_account_id(scope)
    return hint, scope


def _apply_account_labels(
    row: dict[str, Any],
    *,
    account_hint: str = "",
    account_scope: str = "",
) -> None:
    normalized = _normalize_broker_account_id(account_hint)
    if normalized:
        row["account_hint"] = normalized
    scope = _as_str(account_scope)
    if scope:
        row["account_scope"] = scope


def _money_to_float(value: object) -> float | None:
    """Parse owner-confirmed decimal text; reject non-finite / non-numeric."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        amount = float(value)
        return amount if amount >= 0 and amount == amount else None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        amount = Decimal(value.strip())
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return float(amount)


def _unknown_counts(*, reason_code: str) -> dict[str, Any]:
    return {
        "fill_count": None,
        "order_count": None,
        "field_status": {
            "fill_count": "counts_unknown",
            "order_count": "counts_unknown",
        },
        "reason_code": reason_code,
    }


def _pick_signal_and_rebalance(
    activities: Sequence[str],
) -> tuple[str, str, str]:
    """Return (signal_summary, rebalance_kind, rebalance_conclusion)."""
    if not activities:
        return "", "", ""
    priority = (
        "failed",
        "blocked",
        "reconciliation_required",
        "unknown",
        "filled",
        "partially_filled",
        "broker_acknowledged",
        "submitted",
        "previewed",
        "no_rebalance",
        "no_signal",
        "no_submission",
        "no_action",
        "not_due",
    )
    chosen = next((name for name in priority if name in activities), activities[0])
    signal = _SIGNAL_BY_ACTIVITY.get(chosen, chosen)
    kind = _REBALANCE_BY_ACTIVITY.get(chosen, "")
    conclusion = ""
    if kind == "no_rebalance":
        conclusion = "no_rebalance"
    elif kind == "no_order":
        conclusion = "no_order"
    elif kind in {"rebalance", "pending"}:
        conclusion = chosen
    return signal, kind, conclusion


def _equity_from_account_facts(
    facts: Mapping[str, Any] | None,
) -> tuple[float | None, str, str]:
    """Return (equity, currency, reason_if_missing).

    Accepts IBKR history (``ibkr_account_snapshot_history.v1``) balances without
    requiring Schwab-only ``currency_source=owner_confirmed``.
    """
    if facts is None:
        return None, "USD", "account_facts_absent"
    if facts.get("status") == "skipped":
        return None, "USD", str(facts.get("reason") or "account_facts_skipped")
    balances = facts.get("broker_reported_balances")
    if isinstance(balances, list) and balances:
        for item in balances:
            if not isinstance(item, Mapping):
                continue
            if item.get("currency") != "USD":
                continue
            equity = _money_to_float(item.get("net_assets"))
            if equity is not None:
                return equity, "USD", ""
        return None, "USD", "account_facts_equity_absent"
    direct = facts.get("net_assets")
    equity = _money_to_float(direct)
    if equity is not None and (
        facts.get("currency") in {None, "USD"}
        or facts.get("net_assets_currency") == "USD"
    ):
        return equity, "USD", ""
    return None, "USD", "account_facts_equity_absent"


def _activity_from_report(report: Mapping[str, Any]) -> str | None:
    """Derive one covering activity; None means not a covering strategy cycle."""
    receipt = _as_mapping(report.get("execution_receipt")) or {}
    outcome = _as_str(receipt.get("outcome")).lower()
    if outcome in _RAN_ACTIVITIES:
        return outcome

    summary = _as_mapping(report.get("summary")) or {}
    for scope in (report, summary, _as_mapping(report.get("diagnostics")) or {}):
        stage = _as_str(scope.get("stage")).upper()
        if stage in {"NO_ACTION", "DRY_RUN_COMPLETED"}:
            return "no_action"
        if stage in {"FAILED", "FAILURE", "ERROR", "EXECUTION_BLOCKED"}:
            return "failed"
        if stage in {"SUBMITTED", "PARTIAL_SUBMITTED", "ORDERS_PLANNED"}:
            return "submitted"
        if stage in {"COMPLETED", "RECONCILED"}:
            return "no_rebalance"

    status = _as_str(report.get("status")).lower()
    if status in _ALERT_STATUSES:
        return "failed"
    if status in {"ok", "success", "completed", "no_action"}:
        return "no_action"
    if status == "skipped":
        return "no_signal"
    # Account-facts-only reports (no status / receipt) are not strategy cycles.
    return None


def _strategy_from_report(report: Mapping[str, Any]) -> str:
    runtime_target = _as_mapping(report.get("runtime_target")) or {}
    return (
        _as_str(report.get("strategy_profile"))
        or _as_str(runtime_target.get("strategy_profile"))
    )


def _business_day_from_report(report: Mapping[str, Any]) -> str:
    started = _as_str(report.get("started_at"))
    if len(started) >= 10 and started[4] == "-" and started[7] == "-":
        return started[:10]
    summary = _as_mapping(report.get("summary")) or {}
    facts = _as_mapping(summary.get("account_facts")) or {}
    observed = _as_str(facts.get("observed_at"))
    if len(observed) >= 10 and observed[4] == "-" and observed[7] == "-":
        return observed[:10]
    return ""


def _normalize_reports(payload: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(payload, Mapping):
        if payload.get("schema_version") == RUNTIME_REPORT_SCHEMA:
            return [dict(payload)]
        reports = payload.get("reports")
        if isinstance(reports, list):
            return [dict(item) for item in reports if isinstance(item, Mapping)]
        # Schwab-shaped daily projection compatibility (records[].runs).
        records = payload.get("records")
        if isinstance(records, list):
            return []  # handled separately
        return []
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        return [dict(item) for item in payload if isinstance(item, Mapping)]
    return []


def _project_from_schwab_shaped_daily(
    daily_projection: Mapping[str, Any],
    *,
    opaque_account_uid: str,
    target_id: str,
    account_facts: Mapping[str, Any] | None,
    business_day: str | None,
    account_hint: str = "",
    account_scope: str = "",
) -> dict[str, Any] | None:
    """Optional adapter when a daily-like envelope is supplied (tests / future)."""
    records = daily_projection.get("records")
    if not isinstance(records, list):
        return None
    uid = _as_str(opaque_account_uid)
    tid = _as_str(target_id)
    equity, equity_currency, equity_reason = _equity_from_account_facts(account_facts)
    runs_out: list[dict[str, Any]] = []
    for record in records:
        mapping = _as_mapping(record)
        if mapping is None:
            continue
        if mapping.get("platform") not in {None, PLATFORM_ID, "interactive_brokers"}:
            continue
        day = _as_str(mapping.get("business_date"))
        if business_day and day and day != business_day:
            continue
        target = _as_mapping(mapping.get("target")) or {}
        strategy = (
            _as_str(target.get("strategy_profile"))
            or _as_str(mapping.get("strategy_profile"))
            or DEFAULT_STRATEGY
        )
        raw_runs = mapping.get("runs")
        run_list = raw_runs if isinstance(raw_runs, list) else []
        activities: list[str] = []
        for item in run_list:
            if not isinstance(item, Mapping):
                continue
            activity = _as_str(item.get("activity"))
            if activity in _RAN_ACTIVITIES:
                activities.append(activity)
        if not activities:
            continue
        counts = _unknown_counts(reason_code="ibkr_fills_not_projected")
        signal, rebalance_kind, rebalance_conclusion = _pick_signal_and_rebalance(
            activities
        )
        status = "alert" if any(item in _ALERT_ACTIVITIES for item in activities) else "ok"
        if _as_str(mapping.get("status")).lower() in _ALERT_STATUSES:
            status = "alert"
        identity_reasons: list[str] = []
        if not uid:
            identity_reasons.append("opaque_account_uid_absent")
        if not tid:
            identity_reasons.append("target_id_absent")
        reason_parts = [counts["reason_code"], *identity_reasons]
        if equity is None and equity_reason:
            reason_parts.append(equity_reason)
        field_status = dict(counts["field_status"])
        field_status["cycle_count"] = "known"
        row: dict[str, Any] = {
            "platform_id": PLATFORM_ID,
            "strategy_profile": strategy,
            "opaque_account_uid": uid,
            "target_id": tid,
            "actually_ran": True,
            "fill_count": None,
            "order_count": None,
            "cycle_count": len(activities),
            "field_status": field_status,
            "evidence_provenance": EVIDENCE_PROVENANCE,
            "reason_code": "+".join(reason_parts),
            "status": status,
            "business_day": day or business_day or "",
        }
        if signal:
            row["signal_summary"] = signal
        if rebalance_kind:
            row["rebalance_kind"] = rebalance_kind
        if rebalance_conclusion:
            row["rebalance_conclusion"] = rebalance_conclusion
        if equity is not None:
            row["equity"] = equity
            row["equity_currency"] = equity_currency
            row["currency"] = equity_currency
        _apply_account_labels(
            row,
            account_hint=account_hint,
            account_scope=account_scope,
        )
        runs_out.append(row)
    return {
        "schema_version": SCHEMA_VERSION,
        "runs": runs_out,
        "producer_status": "projected" if runs_out else "empty",
        "producer_reason": "" if runs_out else "no_covering_runs",
    }


def project_digest_candidates(
    *,
    runtime_reports: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    daily_projection: Mapping[str, Any] | None = None,
    opaque_account_uid: str = "",
    target_id: str = "",
    account_facts: Mapping[str, Any] | None = None,
    business_day: str | None = None,
    account_hint: str = "",
    account_scope: str = "",
) -> dict[str, Any]:
    """Build ``{"schema_version", "runs": [...]}`` from IBKR runtime reports.

    Fill/order counts are **not** taken from ``summary.orders_*_count`` today:
    those are not a dedicated fills ledger and dry-run quiet zeros must not be
    promoted to central 「已验证零成交」. Counts stay null + ``counts_unknown``.
    """
    if (
        daily_projection is not None
        and isinstance(daily_projection, Mapping)
        and isinstance(daily_projection.get("records"), list)
    ):
        shaped = _project_from_schwab_shaped_daily(
            daily_projection,
            opaque_account_uid=opaque_account_uid,
            target_id=target_id,
            account_facts=account_facts,
            business_day=business_day,
            account_hint=account_hint,
            account_scope=account_scope,
        )
        if shaped is not None:
            return shaped

    if runtime_reports is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "runs": [],
            "producer_status": "skipped",
            "producer_reason": "runtime_reports_missing",
        }

    reports = _normalize_reports(runtime_reports)
    if not reports:
        return {
            "schema_version": SCHEMA_VERSION,
            "runs": [],
            "producer_status": "skipped",
            "producer_reason": "runtime_reports_invalid",
        }

    uid = _as_str(opaque_account_uid)
    tid = _as_str(target_id)
    equity, equity_currency, equity_reason = _equity_from_account_facts(account_facts)

    # Aggregate covering activities by (strategy, business_day).
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for report in reports:
        if report.get("schema_version") not in {None, RUNTIME_REPORT_SCHEMA}:
            continue
        platform = report.get("platform")
        if platform not in {None, PLATFORM_ID, "interactive_brokers"}:
            continue
        activity = _activity_from_report(report)
        if activity is None:
            continue
        explicit_strategy = _strategy_from_report(report)
        strategy = explicit_strategy or DEFAULT_STRATEGY
        day = _business_day_from_report(report)
        if business_day and day and day != business_day:
            continue
        key = (strategy, day or business_day or "")
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {
                "strategy": strategy,
                "day": day or business_day or "",
                "activities": [],
                "statuses": [],
                "strategy_defaulted": False,
                "account_hint": "",
                "account_scope": "",
            }
            buckets[key] = bucket
        bucket["activities"].append(activity)
        bucket["statuses"].append(_as_str(report.get("status")).lower())
        if not explicit_strategy:
            bucket["strategy_defaulted"] = True
        rep_hint, rep_scope = _labels_from_report(report)
        if rep_hint and not bucket["account_hint"]:
            bucket["account_hint"] = rep_hint
        if rep_scope and not bucket["account_scope"]:
            bucket["account_scope"] = rep_scope

    runs_out: list[dict[str, Any]] = []
    for bucket in buckets.values():
        activities: list[str] = bucket["activities"]
        counts = _unknown_counts(reason_code="ibkr_fills_not_projected")
        signal, rebalance_kind, rebalance_conclusion = _pick_signal_and_rebalance(
            activities
        )
        status = "alert" if any(item in _ALERT_ACTIVITIES for item in activities) else "ok"
        if any(item in _ALERT_STATUSES for item in bucket["statuses"]):
            status = "alert"
        identity_reasons: list[str] = []
        if not uid:
            identity_reasons.append("opaque_account_uid_absent")
        if not tid:
            identity_reasons.append("target_id_absent")
        reason_parts = [counts["reason_code"], *identity_reasons]
        if equity is None and equity_reason:
            reason_parts.append(equity_reason)
        if bucket["strategy_defaulted"]:
            reason_parts.append("strategy_profile_defaulted")
        field_status = dict(counts["field_status"])
        field_status["cycle_count"] = "known"
        row: dict[str, Any] = {
            "platform_id": PLATFORM_ID,
            "strategy_profile": bucket["strategy"],
            "opaque_account_uid": uid,
            "target_id": tid,
            "actually_ran": True,
            "fill_count": None,
            "order_count": None,
            "cycle_count": len(activities),
            "field_status": field_status,
            "evidence_provenance": EVIDENCE_PROVENANCE,
            "reason_code": "+".join(reason_parts),
            "status": status,
            "business_day": bucket["day"],
        }
        if signal:
            row["signal_summary"] = signal
        if rebalance_kind:
            row["rebalance_kind"] = rebalance_kind
        if rebalance_conclusion:
            row["rebalance_conclusion"] = rebalance_conclusion
        if equity is not None:
            row["equity"] = equity
            row["equity_currency"] = equity_currency
            row["currency"] = equity_currency
        # Explicit caller labels win; else use labels collected from reports.
        _apply_account_labels(
            row,
            account_hint=account_hint or bucket.get("account_hint") or "",
            account_scope=account_scope or bucket.get("account_scope") or "",
        )
        runs_out.append(row)

    return {
        "schema_version": SCHEMA_VERSION,
        "runs": runs_out,
        "producer_status": "projected" if runs_out else "empty",
        "producer_reason": "" if runs_out else "no_covering_runs",
    }


def load_json_object(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("json_root_must_be_object")
    return raw


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Project IBKR runtime_report evidence to QRS DIGEST_CANDIDATES JSON."
    )
    parser.add_argument(
        "--runtime-report",
        type=Path,
        action="append",
        default=None,
        help="Path to one runtime_report.v1 JSON (repeatable).",
    )
    parser.add_argument(
        "--runtime-reports",
        type=Path,
        default=None,
        help="Path to JSON object with a reports[] array, or a single runtime_report.v1.",
    )
    parser.add_argument(
        "--daily-projection",
        type=Path,
        default=None,
        help="Optional Schwab-shaped daily projection (records[].runs) for tests/compat.",
    )
    parser.add_argument(
        "--account-facts",
        type=Path,
        default=None,
        help="Optional account-facts history/snapshot JSON for equity only.",
    )
    parser.add_argument(
        "--opaque-account-uid",
        default="",
        help="Opaque account identity from protected config (not a raw account number).",
    )
    parser.add_argument(
        "--target-id",
        default="",
        help="Console/target identity from protected config (e.g. ibkr/...).",
    )
    parser.add_argument(
        "--business-day",
        default=None,
        help="Optional YYYY-MM-DD filter.",
    )
    parser.add_argument(
        "--account-hint",
        default="",
        help="Human broker account label (e.g. U15998061) for digest [ibkr U…] tags.",
    )
    parser.add_argument(
        "--account-scope",
        default="",
        help="Runtime account_scope (e.g. live-u15998061).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Where to write candidates JSON (keep ephemeral / private).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        reports_payload: Any = None
        if args.runtime_reports is not None:
            reports_payload = load_json_object(args.runtime_reports)
        elif args.runtime_report:
            loaded = [load_json_object(path) for path in args.runtime_report]
            reports_payload = {"reports": loaded} if len(loaded) > 1 else loaded[0]
        daily = load_json_object(args.daily_projection) if args.daily_projection else None
        facts = load_json_object(args.account_facts) if args.account_facts else None
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {
                    "status": "skipped",
                    "reason": "input_unreadable",
                    "detail": type(exc).__name__,
                }
            )
        )
        return 2
    if reports_payload is None and daily is None:
        print(json.dumps({"status": "skipped", "reason": "input_missing"}))
        return 2
    payload = project_digest_candidates(
        runtime_reports=reports_payload,
        daily_projection=daily,
        opaque_account_uid=args.opaque_account_uid,
        target_id=args.target_id,
        account_facts=facts,
        business_day=args.business_day,
        account_hint=args.account_hint,
        account_scope=args.account_scope,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": payload.get("producer_status"),
                "reason": payload.get("producer_reason") or "ok",
                "runs": len(payload.get("runs") or []),
                "output_written": True,
            },
            sort_keys=True,
        )
    )
    return 0 if payload.get("producer_status") in {"projected", "empty"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
