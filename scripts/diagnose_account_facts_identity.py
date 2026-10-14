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
_ACCOUNT_FACTS_STAGES = (
    "report_envelope", "source_report_provenance", "scope_identity", "snapshot_schema",
    "account_identity", "observation_interval", "balance_cash", "positions",
)
_SOURCE_REPOSITORY = "QuantStrategyLab/InteractiveBrokersPlatform"
_SOURCE_REASONS = _REASONS | frozenset({
    "source_metadata_context_invalid", "source_metadata_unavailable", "deployed_target_mismatch",
    "serving_traffic_unresolved", "revision_identity_invalid", "revision_metadata_invalid",
    "revision_metadata_unavailable", "immutable_image_unavailable", "immutable_image_mismatch",
    "source_labels_unavailable", "deployment_run_read_failed", "deployment_run_unverified",
    "deployment_job_unverified", "deployment_log_read_failed", "deployment_source_unverified",
    "deployment_target_unverified", "deployment_image_mismatch", "serving_changed_during_read",
    "deploy_provenance_verified",
})
_SOURCE_STEPS = (
    "Checkout selected image source", "Verify selected image source",
    "Build, push, and deploy Cloud Run image", "Verify no-traffic deployment readback",
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
        "account_facts_stages": dict.fromkeys(_ACCOUNT_FACTS_STAGES, "unknown"),
        "snapshot_issue": "unknown",
        "report_stop_reason": "unknown",
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


def _diagnose_account_facts_stages(
    report: Mapping[str, Any], *, expected: Mapping[str, Any], source_report_uri: str,
) -> dict[str, str]:
    """Inspect existing projection predicates without changing a revision expectation.

    These states describe structure and scope comparisons only.  They neither
    produce a history record nor establish release approval or economic truth.
    """
    stages = dict.fromkeys(_ACCOUNT_FACTS_STAGES, "unknown")
    try:
        if not isinstance(report, Mapping):
            stages["report_envelope"] = "invalid"
            return stages
        target, diagnostics, receipt = (
            report.get("runtime_target"), report.get("diagnostics"), report.get("runtime_release_receipt")
        )
        envelope_valid = (
            report.get("schema_version") == publisher.RUNTIME_REPORT_SCHEMA
            and report.get("platform") == "interactive_brokers"
            and report.get("deploy_target") == "cloud_run"
            and isinstance(target, Mapping) and isinstance(diagnostics, Mapping)
            and (receipt is None or isinstance(receipt, Mapping))
        )
        stages["report_envelope"] = "verified" if envelope_valid else "invalid"
        try:
            publisher._source_report_uri(
                publisher._text(source_report_uri), publisher._text(expected["expected_report_prefix"])
            )
        except publisher._ProjectionError:
            stages["source_report_provenance"] = "invalid"
            return stages
        stages["source_report_provenance"] = "verified"
        if not envelope_valid:
            return stages
        expected_fields = {
            field: publisher._text(expected[f"expected_{field}"])
            for field in ("project_id", "service_name", "runtime_revision", "account_scope", "deployment_selector")
        }
        selector = publisher._selector(expected["expected_account_selector"])
        target_id = publisher._text(expected["target_id"])
        if (
            not target_id or publisher._TARGET_ID.fullmatch(target_id) is None
            or any(not value for value in expected_fields.values())
            or len(selector) != 1 or selector[0].lower() == "default"
        ):
            return stages
        report_revision = publisher._exact_text(diagnostics.get("runtime_revision"))
        if not report_revision:
            return stages
        # Exact original scope predicates, excluding only the rejected revision.
        # The protected expected revision is never replaced with a report value.
        scope_matches = (
            all(publisher._exact_text(report.get(field)) == expected_fields[field]
                for field in ("project_id", "service_name", "account_scope"))
            and publisher._exact_text(target.get("deployment_selector")) == expected_fields["deployment_selector"]
            and publisher._selector(target.get("account_selector")) == selector
        )
        receipt_revision = publisher._text(receipt.get("runtime_revision")) if isinstance(receipt, Mapping) else ""
        receipt_consistent = not receipt_revision or receipt_revision == report_revision
        if not scope_matches or not receipt_consistent:
            stages["scope_identity"] = "invalid"
            return stages
        stages["scope_identity"] = "verified"
        summary = report.get("summary")
        facts = summary.get("account_facts") if isinstance(summary, Mapping) else None
        if not isinstance(facts, Mapping) or facts.get("schema_version") != publisher.SNAPSHOT_SCHEMA or facts.get("currency") != "USD":
            stages["snapshot_schema"] = "invalid"
            return stages
        stages["snapshot_schema"] = "verified"
        account_ids = publisher._selector(facts.get("account_ids"))
        if len(account_ids) != 1 or account_ids != selector or publisher._ACCOUNT_ID.fullmatch(account_ids[0]) is None:
            stages["account_identity"] = "invalid"
            return stages
        stages["account_identity"] = "verified"
        try:
            started = publisher._observed_timestamp(report.get("started_at"))
            finished = publisher._observed_timestamp(report.get("finished_at"))
            observed = publisher._observed_timestamp(facts.get("observed_at"))
            if finished < started or observed < started or observed > finished:
                raise publisher._ProjectionError("observation_invalid")
        except publisher._ProjectionError:
            stages["observation_interval"] = "invalid"
            return stages
        stages["observation_interval"] = "verified"
        try:
            publisher._decimal_text(facts.get("net_assets"), nullable=True)
            publisher._project_cash(facts.get("cash"))
        except publisher._ProjectionError:
            stages["balance_cash"] = "invalid"
        else:
            stages["balance_cash"] = "verified"
        if facts.get("broker_reported_positions_scope") == publisher.POSITIONS_SCOPE:
            stages["positions"] = "verified" if publisher._project_positions(facts) is not None else "invalid"
    except Exception:
        # Optional diagnostics cannot change the original guard or expose an
        # unexpected exception. Unexecuted/undetermined stages stay unknown.
        pass
    return stages


def _snapshot_issue(report: object, stages: Mapping[str, str]) -> str:
    """Classify only the existing snapshot predicate, after scope proof."""
    try:
        if stages.get("scope_identity") != "verified":
            return "unknown"
        if stages.get("snapshot_schema") == "verified":
            return "none"
        if stages.get("snapshot_schema") != "invalid" or not isinstance(report, Mapping):
            return "unknown"
        if "summary" not in report:
            return "summary_absent"
        summary = report["summary"]
        if not isinstance(summary, Mapping):
            return "summary_not_object"
        if "account_facts" not in summary:
            return "account_facts_absent"
        facts = summary["account_facts"]
        if not isinstance(facts, Mapping):
            return "account_facts_not_object"
        if "schema_version" not in facts:
            return "snapshot_schema_missing"
        if facts["schema_version"] != publisher.SNAPSHOT_SCHEMA:
            return "snapshot_schema_invalid"
        if "currency" not in facts:
            return "currency_missing"
        if facts["currency"] != "USD":
            return "currency_invalid"
    except Exception:
        # Optional classification must not change the original guard or expose
        # a value/exception from malformed input.
        pass
    return "unknown"


def _report_stop_reason(report: object, stages: Mapping[str, str]) -> str:
    """Expose a declared fixed category, never infer an omission's cause."""
    try:
        if any(stages.get(key) != "verified" for key in (
            "report_envelope", "source_report_provenance", "scope_identity",
        )) or not isinstance(report, Mapping):
            return "unknown"
        diagnostics = report.get("diagnostics")
        if not isinstance(diagnostics, Mapping):
            return "unknown"
        status = report.get("status")
        skip, failure = diagnostics.get("skip_reason"), diagnostics.get("failure_category")
        if type(status) is not str:
            return "unknown"
        if status == "skipped":
            if failure not in (None, ""):
                return "invalid_stop_context"
            allowed = {"already_running", "market_closed", "ibkr_fractional_equity_api_unsupported"}
            return skip if type(skip) is str and skip in allowed else "unclassified_skip"
        if status == "error":
            if skip not in (None, ""):
                return "invalid_stop_context"
            allowed = {
                "strategy_execution_blocked", "strategy_deadline", "ibkr_gateway_unavailable",
                "ibkr_trading_permission", "ibkr_portfolio_snapshot",
            }
            return failure if type(failure) is str and failure in allowed else "unclassified_error"
        if status == "ok":
            return "no_declared_stop" if skip in (None, "") and failure in (None, "") else "invalid_stop_context"
    except Exception:
        pass  # Malformed optional context cannot change the original guard.
    return "unknown"


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
    result["account_facts_stages"] = _diagnose_account_facts_stages(
        report, expected=expected, source_report_uri=source_report_uri,
    )
    result["snapshot_issue"] = _snapshot_issue(report, result["account_facts_stages"])
    result["report_stop_reason"] = _report_stop_reason(report, result["account_facts_stages"])
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


def _source_scope(target: object, expected: Mapping[str, Any]) -> dict[str, bool | None]:
    target = target if isinstance(target, Mapping) else {}
    return {
        "service_name": _equal(target.get("service_name"), expected["expected_service_name"]),
        "account_scope": _equal(target.get("account_scope"), expected["expected_account_scope"]),
        "deployment_selector": _equal(target.get("deployment_selector"), expected["expected_deployment_selector"]),
        "account_selector": publisher._selector(target.get("account_selector")) == expected["expected_account_selector"],
    }


def _source_step_env(logs: str, markers: Sequence[str]) -> dict[str, str]:
    """Read one precisely identified step's environment; never echo job logs."""
    groups = []
    current = None
    for raw in logs.splitlines():
        line = re.sub(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z ", "", raw)
        if line.startswith("##[group]"):
            current = []
        elif line == "##[endgroup]" and current is not None:
            group = "\n".join(current)
            if all(marker in group for marker in markers):
                groups.append(current)
            current = None
        elif current is not None:
            current.append(line)
    env_indices = [i for i, line in enumerate(groups[0]) if line.strip() == "env:"] if len(groups) == 1 else []
    if len(groups) != 1 or len(env_indices) != 1:
        raise publisher._ProjectionError("deployment_source_unverified")
    values = {}
    for line in groups[0][env_indices[0] + 1:]:
        match = re.fullmatch(r"\s+([A-Z][A-Z0-9_]*): (.*)", line)
        if match:
            if match[1] in values:
                raise publisher._ProjectionError("deployment_source_unverified")
            values[match[1]] = match[2]
    return values


def _source_deploy_receipt(image, *, run_id: str, selected_source: str, digest: str,
                           expected: Mapping[str, Any], region: str) -> None:
    root = f"repos/{_SOURCE_REPOSITORY}/actions"
    def api(path, reason):
        return image._json_command(("gh", "api", "--hostname", "github.com", path), reason)
    run = api(f"{root}/runs/{run_id}", "deployment_run_read_failed")
    if (not isinstance(run, Mapping) or type(run.get("id")) is not int or str(run["id"]) != run_id
        or not isinstance(run.get("repository"), Mapping)
        or run["repository"].get("full_name") != _SOURCE_REPOSITORY
        or run.get("path") != ".github/workflows/sync-cloud-run-env.yml"
        or run.get("head_branch") != "main" or run.get("event") != "workflow_dispatch"
        or run.get("status") != "completed" or run.get("conclusion") != "success"):
        raise publisher._ProjectionError("deployment_run_unverified")
    listing = api(f"{root}/runs/{run_id}/jobs?per_page=100", "deployment_run_read_failed")
    if (not isinstance(listing, Mapping) or type(listing.get("total_count")) is not int
        or not isinstance(listing.get("jobs"), list) or listing["total_count"] != len(listing["jobs"])):
        raise publisher._ProjectionError("deployment_job_unverified")
    jobs = [j for j in listing["jobs"] if isinstance(j, Mapping) and j.get("name") == "Deploy / Sync Cloud Run"]
    if len(jobs) != 1:
        raise publisher._ProjectionError("deployment_job_unverified")
    job = jobs[0]
    if (type(job.get("id")) is not int or job["id"] <= 0 or str(job.get("run_id")) != run_id
        or job.get("status") != "completed" or job.get("conclusion") != "success"
        or not isinstance(job.get("steps"), list)):
        raise publisher._ProjectionError("deployment_job_unverified")
    for name in _SOURCE_STEPS:
        steps = [s for s in job["steps"] if isinstance(s, Mapping) and s.get("name") == name]
        if len(steps) != 1 or steps[0].get("status") != "completed" or steps[0].get("conclusion") != "success":
            raise publisher._ProjectionError("deployment_job_unverified")
    logs = image._command(("gh", "api", "--hostname", "github.com", f"{root}/jobs/{job['id']}/logs"),
                          "deployment_log_read_failed")
    verify = _source_step_env(logs, ("Selected source must be a full commit SHA.",
        'checked_out_sha="$(git -C source rev-parse HEAD)"', "Checked-out image source does not match source_commit."))
    build = _source_step_env(logs, ('docker build --pull -t "${image}" source', 'gcloud run deploy "${cloud_run_service}"'))
    readback = _source_step_env(logs, ("scripts/verify_cloud_run_no_traffic_deploy.py verify",
                                     '--expected-image-digest="${EXPECTED_IMAGE_DIGEST}"'))
    del logs
    # The run head may differ from the explicitly selected image source. It is
    # never used as a substitute for this successful checkout/verification.
    if verify.get("SOURCE_COMMIT") != selected_source or build.get("SOURCE_COMMIT") != selected_source:
        raise publisher._ProjectionError("deployment_source_unverified")
    if readback.get("EXPECTED_IMAGE_DIGEST") != digest:
        raise publisher._ProjectionError("deployment_image_mismatch")
    if build.get("GCP_PROJECT_ID") != expected["expected_project_id"] or build.get("CLOUD_RUN_REGION") != region:
        raise publisher._ProjectionError("deployment_target_unverified")
    try:
        plan = json.loads(build.get("SYNC_PLAN_JSON", ""))
    except (ValueError, TypeError):
        raise publisher._ProjectionError("deployment_target_unverified") from None
    targets = plan.get("targets") if isinstance(plan, Mapping) else None
    if not isinstance(targets, list):
        raise publisher._ProjectionError("deployment_target_unverified")
    matches = [t for t in targets if isinstance(t, Mapping) and t.get("service_name") == expected["expected_service_name"]]
    if len(matches) != 1 or not isinstance(matches[0].get("env"), Mapping):
        raise publisher._ProjectionError("deployment_target_unverified")
    try:
        target = json.loads(matches[0]["env"].get("RUNTIME_TARGET_JSON", ""))
    except (ValueError, TypeError):
        raise publisher._ProjectionError("deployment_target_unverified") from None
    if any(value is not True for value in _source_scope(target, expected).values()):
        raise publisher._ProjectionError("deployment_target_unverified")


def _source_result(source_sha: object, reason: str = "source_metadata_unavailable") -> dict[str, Any]:
    return {
        "schema_version": SCHEMA, "source_sha": source_sha if isinstance(source_sha, str) and re.fullmatch(r"[0-9a-fA-F]{40}", source_sha) else None,
        "status": "metadata_only", "reason": reason if reason in _SOURCE_REASONS else "diagnostic_error",
        "scope_equalities": dict.fromkeys(("service_name", "account_scope", "deployment_selector", "account_selector")),
        "immutable_image_status": "unknown", "deployment_provenance_status": "unknown",
        "selected_source_sha": None, "serving_stable": None,
        "loaded_source_status": "unknown", "independent_release_approval": "unknown",
    }


def diagnose_serving_source_metadata(expected: Mapping[str, Any], *, source_sha: object = None) -> dict[str, Any]:
    result = _source_result(source_sha)
    try:
        if (os.environ.get("GITHUB_REF") != "refs/heads/main" or os.environ.get("GITHUB_REPOSITORY") != _SOURCE_REPOSITORY
            or not os.environ.get("GH_TOKEN")):
            raise publisher._ProjectionError("source_metadata_context_invalid")
        try:
            from scripts import diagnose_serving_image_identity as image
        except ModuleNotFoundError:
            import diagnose_serving_image_identity as image
        region = os.environ.get("CLOUD_RUN_REGION", "")
        service = _read_cloud_run_metadata(expected)
        if (not isinstance(service, Mapping) or not isinstance(service.get("metadata"), Mapping)
            or service["metadata"].get("name") != expected["expected_service_name"]):
            raise publisher._ProjectionError("source_metadata_unavailable")
        serving, _ = image._serving(service, expected)
        revision = image._json_command(("gcloud", "run", "revisions", "describe", serving,
            "--project", expected["expected_project_id"], "--region", region, "--format=json"), "revision_metadata_unavailable")
        metadata = revision.get("metadata") if isinstance(revision, Mapping) else None
        if not isinstance(metadata, Mapping) or metadata.get("name") != serving:
            raise publisher._ProjectionError("revision_identity_invalid")
        immutable, container = image.immutable_image(revision)
        result["immutable_image_status"] = "verified"
        env = container.get("env")
        rows = [r for r in env if isinstance(r, Mapping) and r.get("name") == "RUNTIME_TARGET_JSON"] if isinstance(env, list) else []
        if len(rows) != 1 or not isinstance(rows[0].get("value"), str):
            raise publisher._ProjectionError("deployed_target_mismatch")
        try:
            target = json.loads(rows[0]["value"])
        except (ValueError, TypeError):
            raise publisher._ProjectionError("deployed_target_mismatch") from None
        result["scope_equalities"] = _source_scope(target, expected)
        if any(v is not True for v in result["scope_equalities"].values()):
            raise publisher._ProjectionError("deployed_target_mismatch")
        labels = metadata.get("labels")
        selected = image._sha(labels.get("commit-sha")) if isinstance(labels, Mapping) else None
        run_id = labels.get("github-run-id") if isinstance(labels, Mapping) else None
        if not selected or not isinstance(run_id, str) or re.fullmatch(r"[1-9][0-9]{0,19}", run_id) is None:
            raise publisher._ProjectionError("source_labels_unavailable")
        _source_deploy_receipt(image, run_id=run_id, selected_source=selected,
                              digest=immutable.rsplit("@", 1)[1], expected=expected, region=region)
        after = _read_cloud_run_metadata(expected)
        if (not isinstance(after, Mapping) or not isinstance(after.get("metadata"), Mapping)
            or after["metadata"].get("name") != expected["expected_service_name"]
            or image._serving(after, expected)[0] != serving):
            result["serving_stable"] = False
            raise publisher._ProjectionError("serving_changed_during_read")
        result.update(reason="deploy_provenance_verified", deployment_provenance_status="verified",
                      selected_source_sha=selected, serving_stable=True)
    except ValueError as exc:
        result["reason"] = str(exc) if str(exc) in _SOURCE_REASONS else "diagnostic_error"
    except Exception:
        result["reason"] = "diagnostic_error"
    return result


def main(argv: Sequence[str] | None = None, *, now: datetime | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    source_sha = os.environ.get("GITHUB_SHA")
    if args == ["--serving-source-metadata-only"]:
        try:
            result = diagnose_serving_source_metadata(resolve_expected(), source_sha=source_sha)
        except publisher._ProjectionError as exc:
            result = _source_result(source_sha, exc.reason)
        except Exception:
            result = _source_result(source_sha, "diagnostic_error")
        print(json.dumps(result, sort_keys=True))
        return 0 if result["reason"] == "deploy_provenance_verified" else 1
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
