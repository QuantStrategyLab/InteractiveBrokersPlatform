#!/usr/bin/env python3
"""Build or explicitly publish a redacted IBKR legacy-recovery source snapshot.

This bridge consumes a private QPK baseline candidate and the private result
of optional advisory ``reconciliation_baseline`` review, plus independently
designated private source records. By default it
only prints the minimal console snapshot. It never opens an IBKR session,
changes a runtime target, writes an execution marker, or submits an order.

``--publish-url`` is opt-in and sends only that minimal snapshot to QRS using
its dedicated bearer token. The ingress records an operator-facing recovery
intent; it is not a broker or execution endpoint.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from application.broker_reconciliation_candidate import (
    SourceReceiptExpectation, initial_private_source_sha256, validate_reconciliation_candidate_sources,
)
from scripts.build_reconciliation_baseline_candidate import (
    _load_payload, _strict_json, evaluate_initial_receipt, extract_reconciliation_evidence, initial_facts_from_private_report,
    load_authenticated_source_inputs, validate_authenticated_candidate,
)

from quant_platform_kit.common.broker_reconciliation_enrollment import (
    BrokerReconciliationBaselineCandidate,
)
from quant_platform_kit.common.reconciliation_recovery import (
    ReconciliationRecoveryDualReview,
    ReconciliationRecoverySourceSnapshot,
    build_reconciliation_recovery_record,
)


RECONCILIATION_RECOVERY_SYNC_TOKEN_ENV = "RECONCILIATION_RECOVERY_SYNC_TOKEN"
RECONCILIATION_RECOVERY_USER_AGENT = "QSL-IBKR-Reconciliation/1.0"
RECONCILIATION_RECOVERY_PRIVATE_EVIDENCE_SCHEMA_VERSION = "ibkr_reconciliation_recovery_private_evidence.v1"
_SHA256_LENGTH = 64


def read_initial_private_run(run_id: str, *, profile: str, service: str, project: str,
                             expected_revision: str | None = None) -> tuple[dict[str, object], dict[str, object]]:
    """Read one exact original or current private report without publishing it."""
    from scripts.diagnose_ibkr_flex_runtime_binding import _gh

    if not re.fullmatch(r"[0-9]{1,20}", run_id):
        raise ValueError("initial private run invalid")
    run = _strict_json(_gh(f"actions/runs/{run_id}"))
    if (not isinstance(run, dict) or run.get("id") != int(run_id) or run.get("status") != "completed"
            or run.get("conclusion") != "success" or run.get("head_branch") != "main"
            or run.get("run_attempt") != 1 or run.get("event") != "workflow_dispatch"
            or run.get("path") != ".github/workflows/collect-reconciliation-evidence.yml"
            or (run.get("repository") or {}).get("full_name") != "QuantStrategyLab/InteractiveBrokersPlatform"):
        raise ValueError("initial private run invalid")
    job_hash = hashlib.sha256(f"ibkr-reconcile-{profile}-{run_id}-1".encode()).hexdigest()
    query = (f'resource.type="cloud_run_revision" AND resource.labels.service_name="{service}" '
             f'AND textPayload:"reconciliation_receipt_ready scheduler_job_sha256={job_hash} "')
    result = subprocess.run(["gcloud", "logging", "read", query, "--project", project,
                             "--freshness=7d", "--limit=10", "--format=json"],
                            capture_output=True, timeout=30, check=False)
    if result.returncode:
        raise ValueError("initial private report unavailable")
    pattern = re.compile(r"^reconciliation_receipt_ready scheduler_job_sha256=" + re.escape(job_hash)
                         + r" report_uri=(gs://\S+/initial-enrollment/([0-9a-f-]{36})\.json#([0-9]+))$")
    entries = _strict_json(result.stdout)
    matches = [(entry, match) for entry in entries if isinstance(entry, dict)
               and (match := pattern.fullmatch(str(entry.get("textPayload", ""))))]
    if len(matches) != 1:
        raise ValueError("initial private report unavailable")
    entry, match = matches[0]
    revision = (entry.get("resource") or {}).get("labels", {}).get("revision_name")
    if not isinstance(revision, str) or not revision or (expected_revision and revision != expected_revision):
        raise ValueError("initial private revision mismatch")
    response = subprocess.run(["gcloud", "storage", "cat", match.group(1)], capture_output=True, timeout=30, check=False)
    if response.returncode or len(response.stdout) > 4 * 1024 * 1024:
        raise ValueError("initial private report unavailable")
    report = _strict_json(response.stdout)
    record = {"schema_version": "ibkr_initial_private_source.v1", "repository": run["repository"]["full_name"],
              "workflow_path": run["path"], "workflow_run_id": run_id, "workflow_run_attempt": "1",
              "workflow_head_sha": run.get("head_sha"), "service_name": service, "service_revision": revision,
              "scheduler_job_sha256": job_hash, "report_uri": match.group(1), "request_id": match.group(2),
              "report_generation": match.group(3)}
    initial_facts_from_private_report(report, scheduler_job_sha256=job_hash, request_id=match.group(2))
    return record, report


def build_trusted_initial_snapshot(env: Mapping[str, str], run_id: str, *, read_report=None, read_run=None,
                                   private_output: dict[str, object] | None = None) -> dict[str, object]:
    """Read the existing private report after verifying its exact protected source."""
    from scripts.diagnose_ibkr_flex_runtime_binding import configuration, observe, _gh
    from quant_platform_kit.common.live_continuity import build_live_continuity, runtime_target_fingerprint

    slots, targets, project, region = configuration(env)
    if len(targets) != 1:
        raise ValueError("initial enrollment requires one current target")
    profile, (service, target) = next(iter(targets.items()))
    private = {}
    if not isinstance(run_id, str) or not re.fullmatch(r"[0-9]{1,20}", run_id):
        raise ValueError("initial enrollment source run is invalid")
    try:
        run = _strict_json(_gh(f"actions/runs/{run_id}") if read_run is None else read_run(run_id))
    except (ValueError, RuntimeError, TypeError):
        raise ValueError("initial enrollment source run unavailable") from None
    if (not isinstance(run, dict) or run.get("id") != int(run_id) or run.get("status") != "completed"
            or run.get("conclusion") != "success" or run.get("head_branch") != "main" or run.get("run_attempt") != 1
            or run.get("event") != "workflow_dispatch" or run.get("path") != ".github/workflows/collect-reconciliation-evidence.yml"
            or (run.get("repository") or {}).get("full_name") != "QuantStrategyLab/InteractiveBrokersPlatform"):
        raise ValueError("initial enrollment source run is invalid")
    observed = observe(profile, service, target, slots, project, region, private_output=private, initial_private=True)
    if observed.get("status") != "initial_metadata_verified" or not private.get("target_id"):
        raise ValueError("initial enrollment protected source mismatch")
    job_hash = hashlib.sha256(f"ibkr-reconcile-{profile}-{run_id}-1".encode()).hexdigest()
    if read_report is None:
        query = (f'resource.type="cloud_run_revision" AND resource.labels.service_name="{service}" '
                 f'AND resource.labels.revision_name="{private["service_revision"]}" '
                 f'AND textPayload:"reconciliation_receipt_ready scheduler_job_sha256={job_hash} "')
        try:
            result = subprocess.run(["gcloud", "logging", "read", query, "--project", project,
                                     "--freshness=1h", "--limit=10", "--format=json"],
                                    capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError("initial private report lookup failed") from None
        if result.returncode:
            raise ValueError("initial private report lookup failed")
        try:
            entries = json.loads(result.stdout)
            pattern = re.compile(r"^reconciliation_receipt_ready scheduler_job_sha256=" + re.escape(job_hash) + r" report_uri=(gs://\S+)$")
            uris = [match.group(1) for entry in entries if isinstance(entry, dict)
                    and (match := pattern.fullmatch(str(entry.get("textPayload", ""))))]
            if len(uris) != 1:
                raise ValueError()
            response = subprocess.run(["gcloud", "storage", "cat", uris[0]], capture_output=True, timeout=30, check=False)
            if response.returncode or len(response.stdout) > 4 * 1024 * 1024:
                raise ValueError()
            report = _strict_json(response.stdout)
        except (TypeError, ValueError, KeyError, OSError, subprocess.TimeoutExpired):
            raise ValueError("initial private report unavailable") from None
    else:
        uris, report = read_report(job_hash)
    match = re.fullmatch(r"(gs://\S+/initial-enrollment/([0-9a-f-]{36})\.json#([0-9]+))", uris[0]) if len(uris) == 1 else None
    if not match:
        raise ValueError("initial private report generation unavailable")
    record = {"schema_version": "ibkr_initial_private_source.v1", "repository": run["repository"]["full_name"],
              "workflow_path": run["path"], "workflow_run_id": run_id, "workflow_run_attempt": "1",
              "workflow_head_sha": run.get("head_sha"), "service_name": service,
              "service_revision": private["service_revision"], "scheduler_job_sha256": job_hash,
              "report_uri": match.group(1), "request_id": match.group(2), "report_generation": match.group(3)}
    source_sha256 = initial_private_source_sha256(record)
    facts = initial_facts_from_private_report(report, scheduler_job_sha256=job_hash, request_id=record["request_id"])
    evidence = extract_reconciliation_evidence(report)
    account_digest = hashlib.sha256(json.dumps({"account_ids": sorted(private["account_selector"])}, ensure_ascii=True,
                                              sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if evidence.account_scope_sha256 != account_digest or evidence.strategy_profile != profile or evidence.platform_id != "ibkr":
        raise ValueError("initial enrollment account binding mismatch")
    rebuilt = evaluate_initial_receipt(report, facts, private_source_sha256=source_sha256)
    candidate = rebuilt["candidate"]
    runtime_target = private["runtime_target"]
    continuity = build_live_continuity(runtime_target.get("live_continuity"))
    continuity.assert_matches_target(runtime_target)
    if (continuity.state != "RECONCILE_ONLY" or continuity.baseline_id != candidate["baseline_id"]
            or runtime_target_fingerprint(runtime_target) != candidate["baseline_target_sha256"]):
        raise ValueError("initial enrollment current target mismatch")
    if private_output is not None:
        private_output.update(record=record, receipt=report,
                              runtime_target=runtime_target, facts=facts, candidate_payload=rebuilt)
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {"schema_version": "qsl_reconciliation_recovery_source_snapshot.v1",
            "source_id": f"ibkr.initial.{profile}", "generated_at": timestamp, "computed_at": timestamp,
            "data_status": "ready", "recoveries": [{"mode": "initial_enrollment",
            "recovery_id": f"initial-{candidate['candidate_sha256']}", "target_id": private["target_id"],
            "candidate": candidate, "facts": facts}], "errors": []}


class _NoRedirect(HTTPRedirectHandler):
    """Never forward the dedicated source token to an HTTP redirect target."""

    def redirect_request(self, request: Request, *_args: object, **_kwargs: object) -> None:
        return None


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"unable to read {label}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _sha256(value: object, *, field_name: str) -> str:
    normalized = str(value or "").strip().lower().removeprefix("sha256:")
    if len(normalized) != _SHA256_LENGTH or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError(f"{field_name} must be a SHA-256 digest")
    return normalized


def extract_baseline_candidate(payload: Mapping[str, Any], *,
                               source_receipt_records: Sequence[Mapping[str, object]],
                               expectations: Sequence[SourceReceiptExpectation]) -> BrokerReconciliationBaselineCandidate:
    """Load only a ready, QPK-validated candidate from the local receipt tool."""

    if payload.get("schema_version") != "ibkr_reconciliation_baseline_enrollment.v1":
        raise ValueError("baseline candidate has an unsupported schema_version")
    if payload.get("ready_for_independent_review") is not True:
        raise ValueError("baseline candidate is not ready for independent review")
    candidate = payload.get("candidate")
    if not isinstance(candidate, Mapping):
        raise ValueError("baseline candidate is missing its QPK candidate")
    return validate_reconciliation_candidate_sources(
        BrokerReconciliationBaselineCandidate.from_dict(candidate),
        source_receipt_records=source_receipt_records, expectations=expectations,
    )


def _secondary_reviewer_count(value: object) -> int:
    if not isinstance(value, Mapping):
        return 0
    # Count actual returned review records, not required model votes.
    # A legacy independent secondary reviewer still counts as one; primary is
    # counted separately by extract_bound_dual_review.
    if all(isinstance(value.get(name), Mapping) for name in ("gpt", "claude")):
        return 2
    return 1 if isinstance(value.get("legacy"), Mapping) else 0


def extract_bound_dual_review(
    payload: Mapping[str, Any] | None,
    *,
    candidate: BrokerReconciliationBaselineCandidate,
) -> ReconciliationRecoveryDualReview:
    """Preserve an optional advisory result and its candidate binding.

    Generic approval JSON is deliberately rejected. The later private
    controller independently rechecks the full receipt; this output is only a
    redacted, operator-facing source row.
    """

    if payload is None:
        return ReconciliationRecoveryDualReview("unavailable", 0, candidate.candidate_sha256)
    if str(payload.get("trigger") or "").strip() != "reconciliation_baseline":
        raise ValueError("dual review must use the reconciliation_baseline trigger")
    if str(payload.get("strategy_profile") or "").strip() != candidate.strategy_profile:
        raise ValueError("dual review strategy_profile does not match the candidate")
    if payload.get("requires_human_recovery_approval") is not True:
        raise ValueError("dual review is missing its human recovery approval requirement")
    authority = payload.get("recovery_authority")
    if not isinstance(authority, Mapping) or authority.get("human_review_required") is not True:
        raise ValueError("dual review authority does not require human review")
    if str(authority.get("final_action") or "").strip().lower() != "escalate":
        raise ValueError("dual review authority is not restricted to escalation")

    binding = _sha256(payload.get("evidence_binding_sha256"), field_name="dual review evidence_binding_sha256")
    if binding != candidate.candidate_sha256:
        raise ValueError("dual review evidence binding does not match the candidate")

    outcome_map = {
        "pass": "approved", "approve": "approved", "approved": "approved",
        "fail": "rejected", "reject": "rejected", "rejected": "rejected",
        "review_unavailable": "unavailable", "unavailable": "unavailable",
    }
    outcome = outcome_map.get(str(payload.get("outcome") or "").strip().lower())
    if outcome is None:
        raise ValueError("dual review has an unsupported outcome")
    reviewer_count = int(isinstance(payload.get("primary_review"), Mapping)) + _secondary_reviewer_count(
        payload.get("secondary_review")
    )
    return ReconciliationRecoveryDualReview(
        outcome=outcome,
        reviewer_count=reviewer_count,
        evidence_binding_sha256=binding,
    )


def build_recovery_source_snapshot(
    *,
    candidate_payload: Mapping[str, Any],
    source_receipt_records: Sequence[Mapping[str, object]],
    expectations: Sequence[SourceReceiptExpectation],
    dual_review_payload: Mapping[str, Any] | None = None,
    recovery_id: str,
    source_id: str = "ibkr.reconciliation_recovery",
    now: datetime | None = None,
) -> dict[str, object]:
    """Return the exact QRS ingress payload, without a network side effect."""

    candidate = extract_baseline_candidate(candidate_payload, source_receipt_records=source_receipt_records, expectations=expectations)
    dual_review = extract_bound_dual_review(dual_review_payload, candidate=candidate)
    reference_now = now or datetime.now(timezone.utc)
    record = build_reconciliation_recovery_record(
        recovery_id=recovery_id,
        console_platform="ibkr",
        candidate=candidate,
        dual_review=dual_review,
        now=reference_now,
    )
    return ReconciliationRecoverySourceSnapshot(
        source_id=source_id,
        generated_at=reference_now,
        computed_at=reference_now,
        records=(record,),
    ).to_dict()


def _parse_private_evidence_uri(value: str) -> tuple[str, str]:
    """Allow only an explicit, immutable IBKR source-artifact object path."""

    normalized = str(value or "").strip()
    if not normalized.startswith("gs://"):
        raise ValueError("private_evidence_uri must use gs://")
    remainder = normalized.removeprefix("gs://")
    bucket_name, separator, object_name = remainder.partition("/")
    required_prefix = "reconciliation-recovery/ibkr/source/"
    if not bucket_name or not separator or not object_name.startswith(required_prefix) or object_name.endswith("/"):
        raise ValueError("private_evidence_uri must use the IBKR recovery source prefix")
    return bucket_name, object_name


def build_private_evidence_package(
    *,
    snapshot: Mapping[str, object],
    candidate_payload: Mapping[str, Any],
    source_receipt_records: Sequence[Mapping[str, object]],
    expectations: Sequence[SourceReceiptExpectation],
    dual_review_payload: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """Package private inputs for the later verifier, never for QRS ingress."""

    candidate = extract_baseline_candidate(candidate_payload, source_receipt_records=source_receipt_records, expectations=expectations)
    extract_bound_dual_review(dual_review_payload, candidate=candidate)
    recoveries = snapshot.get("recoveries")
    if not isinstance(recoveries, list) or len(recoveries) != 1 or not isinstance(recoveries[0], Mapping):
        raise ValueError("recovery source snapshot must contain exactly one recovery")
    recovery_id = str(recoveries[0].get("recovery_id") or "").strip()
    if not recovery_id:
        raise ValueError("recovery source snapshot is missing recovery_id")
    return {
        "schema_version": RECONCILIATION_RECOVERY_PRIVATE_EVIDENCE_SCHEMA_VERSION,
        "recovery_id": recovery_id,
        "candidate_sha256": candidate.candidate_sha256,
        "source_snapshot": dict(snapshot),
        "baseline_candidate": dict(candidate_payload),
        "dual_review": dict(dual_review_payload) if dual_review_payload is not None else None,
    }


def archive_private_evidence_package(
    package: Mapping[str, object],
    *,
    private_evidence_uri: str,
    storage_client_factory: Any | None = None,
) -> dict[str, str]:
    """Create one immutable private package with a GCS generation precondition.

    The publisher role has create-only access to this prefix. A pre-existing
    artifact therefore fails instead of being replaced, and this helper never
    lists, reads, deletes, or rewrites an object.
    """

    bucket_name, object_name = _parse_private_evidence_uri(private_evidence_uri)
    if package.get("schema_version") != RECONCILIATION_RECOVERY_PRIVATE_EVIDENCE_SCHEMA_VERSION:
        raise ValueError("private evidence package has an unsupported schema_version")
    payload = json.dumps(dict(package), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if storage_client_factory is None:
        try:
            from google.cloud import storage
        except ImportError as exc:  # pragma: no cover - production dependency is installed in runtime images.
            raise RuntimeError("google-cloud-storage is required to archive private recovery evidence") from exc
        client = storage.Client()
    else:
        client = storage_client_factory()
    blob = client.bucket(bucket_name).blob(object_name)
    try:
        blob.upload_from_string(
            payload,
            content_type="application/json",
            if_generation_match=0,
        )
    except Exception as exc:
        raise RuntimeError("private recovery evidence archive was not created") from exc
    return {
        "uri": f"gs://{bucket_name}/{object_name}",
        "schema_version": RECONCILIATION_RECOVERY_PRIVATE_EVIDENCE_SCHEMA_VERSION,
        "candidate_sha256": str(package["candidate_sha256"]),
    }


def publish_recovery_source_snapshot(
    snapshot: Mapping[str, object],
    *,
    publish_url: str,
    token: str,
    post: Any | None = None,
) -> dict[str, object]:
    """Publish a redacted source snapshot to QRS; never contact a broker."""

    normalized_url = str(publish_url or "").strip()
    normalized_token = str(token or "").strip()
    if not normalized_url.startswith("https://"):
        raise ValueError("publish_url must use https")
    if not normalized_url.rstrip("/").endswith("/api/internal/sync-reconciliation-recovery-source"):
        raise ValueError("publish_url must target the reconciliation recovery source ingress")
    if not normalized_token:
        raise ValueError(f"{RECONCILIATION_RECOVERY_SYNC_TOKEN_ENV} is required for --publish-url")
    if post is None:
        body = json.dumps(dict(snapshot), ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        request = Request(
            normalized_url,
            data=body,
            headers={"Authorization": f"Bearer {normalized_token}", "Content-Type": "application/json",
                     "User-Agent": RECONCILIATION_RECOVERY_USER_AGENT},
            method="POST",
        )
        try:
            with build_opener(_NoRedirect).open(request, timeout=15) as response:  # noqa: S310 - URL is constrained above.
                status_code = int(response.status)
                raw_body = response.read().decode("utf-8")
        except HTTPError as exc:
            raise RuntimeError(f"reconciliation recovery source publish returned HTTP {exc.code}") from exc
        except URLError as exc:
            raise RuntimeError("reconciliation recovery source publish failed") from exc
        if status_code < 200 or status_code >= 300:
            raise RuntimeError(f"reconciliation recovery source publish returned HTTP {status_code}")
        try:
            result = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("reconciliation recovery source publish returned invalid JSON") from exc
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError("reconciliation recovery source publish was not acknowledged")
        return result
    try:
        response = post(
            normalized_url,
            json=dict(snapshot),
            headers={"Authorization": f"Bearer {normalized_token}", "User-Agent": RECONCILIATION_RECOVERY_USER_AGENT},
            timeout=15,
        )
    except Exception as exc:
        raise RuntimeError("reconciliation recovery source publish failed") from exc
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(f"reconciliation recovery source publish returned HTTP {response.status_code}")
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError("reconciliation recovery source publish returned invalid JSON") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("reconciliation recovery source publish was not acknowledged")
    return result


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("--now must be ISO-8601 with a timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--now must include a timezone")
    return parsed.astimezone(timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build or explicitly publish a redacted, non-authorising IBKR recovery source snapshot."
    )
    parser.add_argument("--candidate", type=Path, help="Private output from build_reconciliation_baseline_candidate.py")
    parser.add_argument("--dual-review", type=Path, help="Optional private advisory review result")
    parser.add_argument("--source-archive", action="append", type=Path)
    parser.add_argument("--source-records", type=Path)
    parser.add_argument("--source-expectations", type=Path)
    parser.add_argument("--recovery-id", help="Opaque legacy runtime recovery identifier")
    parser.add_argument("--trusted-initial-run", help="One completed existing initial evidence workflow run")
    parser.add_argument("--source-id", default="ibkr.reconciliation_recovery")
    parser.add_argument("--now", help="Optional ISO-8601 time used for deterministic validation")
    parser.add_argument(
        "--publish-url",
        help="Explicit QRS /api/internal/sync-reconciliation-recovery-source HTTPS URL; omitted means no network call",
    )
    parser.add_argument(
        "--archive-gcs-uri",
        help="Explicit private gs://.../reconciliation-recovery/ibkr/source/... object; uses create-only generation precondition",
    )
    args = parser.parse_args(argv)
    try:
        if args.trusted_initial_run:
            if (args.candidate or args.source_archive or args.source_records or args.source_expectations or args.recovery_id
                    or args.dual_review or args.archive_gcs_uri or args.now):
                raise ValueError("trusted initial source arguments are invalid")
            snapshot = build_trusted_initial_snapshot(os.environ, args.trusted_initial_run)
            if args.publish_url:
                publish_recovery_source_snapshot(snapshot, publish_url=args.publish_url,
                                                 token=os.environ.get(RECONCILIATION_RECOVERY_SYNC_TOKEN_ENV, ""))
            print(json.dumps({"status": "published" if args.publish_url else "prepared"}))
            return 0
        if not all((args.candidate, args.source_archive, args.source_records, args.source_expectations, args.recovery_id)):
            raise ValueError("source candidate and authenticated source inputs are required")
        records, expectations, payloads = load_authenticated_source_inputs(
            args.source_records, args.source_expectations, args.source_archive,
        )
        reference_now = _parse_time(args.now)
        candidate_payload = _load_payload(args.candidate)
        validate_authenticated_candidate(candidate_payload, payloads,
                                         source_receipt_records=records, expectations=expectations, now=reference_now)
        snapshot = build_recovery_source_snapshot(
            candidate_payload=candidate_payload,
            dual_review_payload=_load_json(args.dual_review, label="dual review") if args.dual_review else None,
            source_receipt_records=records, expectations=expectations,
            recovery_id=args.recovery_id,
            source_id=args.source_id,
            now=reference_now,
        )
        output: dict[str, object] = {"snapshot": snapshot}
        if args.archive_gcs_uri:
            output["private_evidence_archive"] = archive_private_evidence_package(
                build_private_evidence_package(
                    snapshot=snapshot,
                    candidate_payload=candidate_payload,
                    dual_review_payload=_load_json(args.dual_review, label="dual review") if args.dual_review else None,
                    source_receipt_records=records, expectations=expectations,
                ),
                private_evidence_uri=args.archive_gcs_uri,
            )
        if args.publish_url:
            result = publish_recovery_source_snapshot(
                snapshot,
                publish_url=args.publish_url,
                token=os.environ.get(RECONCILIATION_RECOVERY_SYNC_TOKEN_ENV, ""),
            )
            output["publish"] = result
        if args.publish_url or args.archive_gcs_uri:
            print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        else:
            print(json.dumps(snapshot, ensure_ascii=False, sort_keys=True))
    except (ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
