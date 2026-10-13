#!/usr/bin/env python3
"""Read protected report identity and Cloud Run metadata without publishing."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    from scripts import publish_account_facts_from_report as publisher
except ModuleNotFoundError:
    import publish_account_facts_from_report as publisher  # type: ignore[no-redef]


SCHEMA = "ibkr.account_facts_identity_diagnostic.v1"
_REASONS = frozenset({
    "target_disabled", "target_config_unavailable", "target_config_invalid",
    "target_config_valid", "report_listing_failed", "report_unavailable",
    "report_read_failed", "runtime_report_invalid", "expected_target_invalid",
    "report_provenance_invalid", "runtime_target_mismatch", "account_facts_invalid",
    "account_identity_mismatch", "observation_invalid", "identity_verified",
    "diagnostic_error", "arguments_invalid",
})
_EQUALITY_FIELDS = (
    "report_equals_configured", "configured_equals_ready", "report_equals_ready",
    "ready_equals_latest_created", "receipt_equals_report",
)


def _traffic_result(status: str = "absent") -> dict[str, Any]:
    return {
        "status": status,
        "report_has_positive_traffic": None,
        "configured_has_positive_traffic": None,
        "report_is_only_serving_revision": None,
    }


def _serving_traffic(rows: object, *, report_revision: object, configured_revision: object) -> dict[str, Any]:
    """Project effective traffic only; latestReady and tagged URLs are not fallbacks."""
    if rows is None or isinstance(rows, list) and not rows:
        return _traffic_result()
    if not isinstance(rows, list):
        return _traffic_result("invalid")
    total = 0
    positive: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            return _traffic_result("invalid")
        revision = publisher._single_line_text(row.get("revisionName"))
        percent = row.get("percent")
        if not revision or type(percent) is not int or not 0 <= percent <= 100:
            return _traffic_result("invalid")
        total += percent
        if percent > 0:
            positive.add(revision)
    if total != 100:
        return _traffic_result("invalid")
    result = _traffic_result("single" if len(positive) == 1 else "split")
    report = publisher._single_line_text(report_revision)
    configured = publisher._single_line_text(configured_revision)
    if report:
        result["report_has_positive_traffic"] = report in positive
        result["report_is_only_serving_revision"] = positive == {report}
    if configured:
        result["configured_has_positive_traffic"] = configured in positive
    return result


def _result(*, source_sha: object = None, reason: str, status: str = "unavailable") -> dict[str, Any]:
    return {
        "schema_version": SCHEMA,
        "source_sha": source_sha if isinstance(source_sha, str) and re.fullmatch(r"[0-9a-fA-F]{40}", source_sha) else None,
        "status": status,
        "reason": reason if isinstance(reason, str) and reason in _REASONS else "diagnostic_error",
        "mismatched_fields": [],
        "projection_passed": None,
        "freshness_status": "unknown",
        "cloud_run_metadata_status": "unavailable",
        "revision_equalities": dict.fromkeys(_EQUALITY_FIELDS),
        "serving_traffic": _traffic_result(),
    }


def resolve_expected() -> dict[str, Any]:
    """Validate a fixed slot before any cloud authentication or report read."""
    target = os.environ.get("IBKR_ACCOUNT_FACTS_TARGET", "")
    if target in publisher.IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS:
        selected = publisher.additional_target_environment(target)
    elif target == publisher.IBKR_ACCOUNT_FACTS_PRIMARY_TARGET:
        selected = os.environ
    else:
        raise publisher._ProjectionError("target_disabled")

    def setting(name: str) -> str:
        value = publisher._required_private_setting(name) if selected is os.environ else selected[name]
        if not publisher._single_line_text(value):
            raise publisher._ProjectionError("target_config_invalid")
        return value

    try:
        selector = publisher._selector(json.loads(setting("IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON")))
    except (json.JSONDecodeError, TypeError):
        raise publisher._ProjectionError("target_config_invalid") from None
    if len(selector) != 1 or publisher._ACCOUNT_ID.fullmatch(selector[0]) is None:
        raise publisher._ProjectionError("target_config_invalid")
    expected = {
        "target_id": setting("IBKR_ACCOUNT_FACTS_TARGET_ID"),
        "expected_report_prefix": setting("IBKR_ACCOUNT_FACTS_REPORT_PREFIX"),
        "expected_project_id": setting("IBKR_ACCOUNT_FACTS_PROJECT_ID"),
        "expected_service_name": setting("IBKR_ACCOUNT_FACTS_SERVICE_NAME"),
        "expected_runtime_revision": setting("IBKR_ACCOUNT_FACTS_RUNTIME_REVISION"),
        "expected_account_scope": setting("IBKR_ACCOUNT_FACTS_ACCOUNT_SCOPE"),
        "expected_account_selector": selector,
        "expected_deployment_selector": setting("IBKR_ACCOUNT_FACTS_DEPLOYMENT_SELECTOR"),
    }
    if publisher._TARGET_ID.fullmatch(expected["target_id"]) is None:
        raise publisher._ProjectionError("target_config_invalid")
    try:
        prefix = expected["expected_report_prefix"]
        publisher._source_report_uri(prefix.rstrip("/") + "/preflight.json", prefix)
    except publisher._ProjectionError:
        raise publisher._ProjectionError("target_config_invalid") from None
    return expected


def _equal(left: object, right: object) -> bool | None:
    first, second = publisher._exact_text(left), publisher._exact_text(right)
    return first == second if first and second else None


def diagnose_identity(
    report: Mapping[str, Any], *, expected: Mapping[str, Any], source_report_uri: str,
    now: datetime, cloud_run_metadata: object = None, source_sha: object = None,
) -> dict[str, Any]:
    """Pure diagnostic: preserve the projection guard and discard all private values."""
    projected = publisher.project_ibkr_account_facts_history(
        report, source_report_uri=source_report_uri, **expected,
    )
    passed = projected.get("schema_version") == publisher.HISTORY_SCHEMA
    result = _result(source_sha=source_sha, status="diagnosed",
        reason="identity_verified" if passed else projected.get("reason", "diagnostic_error"))
    result["projection_passed"] = passed
    if result["reason"] == "runtime_target_mismatch":
        result["mismatched_fields"] = publisher._safe_mismatched_fields(projected.get("mismatched_fields"))

    summary = report.get("summary") if isinstance(report, Mapping) else None
    facts = summary.get("account_facts") if isinstance(summary, Mapping) else None
    try:
        observed = publisher._observed_timestamp(facts.get("observed_at") if isinstance(facts, Mapping) else None)
        if now.tzinfo is None or now.utcoffset() is None:
            raise publisher._ProjectionError("observation_invalid")
        current = now.astimezone(timezone.utc)
        stale = observed < current - publisher.IBKR_ACCOUNT_FACTS_MAX_AGE or observed > current + timedelta(minutes=5)
        result["freshness_status"] = "stale" if stale else "fresh"
    except publisher._ProjectionError:
        pass

    diagnostics = report.get("diagnostics") if isinstance(report, Mapping) else None
    receipt = report.get("runtime_release_receipt") if isinstance(report, Mapping) else None
    revision = diagnostics.get("runtime_revision") if isinstance(diagnostics, Mapping) else None
    receipt_revision = receipt.get("runtime_revision") if isinstance(receipt, Mapping) else None
    equalities = result["revision_equalities"]
    equalities["report_equals_configured"] = _equal(revision, expected["expected_runtime_revision"])
    equalities["receipt_equals_report"] = _equal(receipt_revision, revision)
    if cloud_run_metadata is not None:
        metadata = cloud_run_metadata.get("metadata") if isinstance(cloud_run_metadata, Mapping) else None
        status = cloud_run_metadata.get("status") if isinstance(cloud_run_metadata, Mapping) else None
        if (
            not isinstance(metadata, Mapping) or not isinstance(status, Mapping)
            or _equal(metadata.get("name"), expected["expected_service_name"]) is not True
        ):
            result["cloud_run_metadata_status"] = "invalid"
            result["serving_traffic"] = _traffic_result("invalid")
        else:
            result["cloud_run_metadata_status"] = "read"
            ready = status.get("latestReadyRevisionName")
            equalities["configured_equals_ready"] = _equal(expected["expected_runtime_revision"], ready)
            equalities["report_equals_ready"] = _equal(revision, ready)
            equalities["ready_equals_latest_created"] = _equal(ready, status.get("latestCreatedRevisionName"))
            result["serving_traffic"] = _serving_traffic(status.get("traffic"),
                report_revision=revision, configured_revision=expected["expected_runtime_revision"])
    return result


def _read_cloud_run_metadata(expected: Mapping[str, Any]) -> object:
    # The explicit existing regional contract is independent of the report.
    # Missing region remains unknown; never search other services or regions.
    region = os.environ.get("CLOUD_RUN_REGION", "")
    if re.fullmatch(r"[a-z]+-[a-z]+[0-9]+", region) is None:
        return None
    try:
        result = subprocess.run(
            ("gcloud", "run", "services", "describe", expected["expected_service_name"],
             "--project", expected["expected_project_id"], "--region", region, "--format=json"),
            capture_output=True, text=True, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return None


def main(argv: Sequence[str] | None = None, *, now: datetime | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    source_sha = os.environ.get("GITHUB_SHA")
    if args not in ([], ["--preflight"]):
        print(json.dumps(_result(source_sha=source_sha, reason="arguments_invalid"), sort_keys=True))
        return 1
    try:
        expected = resolve_expected()
        if args:
            print(json.dumps(_result(source_sha=source_sha, status="preflight", reason="target_config_valid"), sort_keys=True))
            return 0
        current = now if now is not None else datetime.now(timezone.utc)
        uri = publisher._latest_report_uri(prefix=expected["expected_report_prefix"],
            project_id=expected["expected_project_id"], now=current)
        report = publisher._load_gcs_report(uri, project_id=expected["expected_project_id"])
        metadata = _read_cloud_run_metadata(expected)
        result = diagnose_identity(report, expected=expected, source_report_uri=uri,
            now=current, cloud_run_metadata=metadata, source_sha=source_sha)
    except publisher._ProjectionError as exc:
        result = _result(source_sha=source_sha, reason=exc.reason)
    except Exception:
        # No exception type, subprocess stderr, URI, or private values are emitted.
        result = _result(source_sha=source_sha, reason="diagnostic_error")
    print(json.dumps(result, sort_keys=True))
    return 0 if (
        result["projection_passed"] is True and result["freshness_status"] == "fresh"
        and result["cloud_run_metadata_status"] == "read"
        and result["revision_equalities"]["configured_equals_ready"] is True
        and result["revision_equalities"]["report_equals_ready"] is True
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
