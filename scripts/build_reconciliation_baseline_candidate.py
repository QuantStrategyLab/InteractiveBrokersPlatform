#!/usr/bin/env python3
"""Build a review-ready legacy-baseline candidate from redacted IBKR receipts.

The CLI authenticates saved source ZIP bytes against independently designated
expectations before parsing digest-only evidence. The pure parsed-input functions
remain for trusted in-process callers; this tool never opens a
broker connection, changes Cloud Run configuration, writes an execution
marker, or submits an order.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import stat
import zipfile
import zlib
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from application.broker_reconciliation_candidate import (
    SourceReceiptExpectation, calculate_source_receipts_sha256, canonical_source_receipt_records_json,
    validate_reconciliation_candidate_sources,
)

from quant_platform_kit.common.broker_reconciliation import BrokerReconciliationEvidence
from quant_platform_kit.common.broker_reconciliation_enrollment import (
    evaluate_broker_reconciliation_baseline_enrollment,
)


def extract_reconciliation_evidence(payload: Mapping[str, Any]) -> BrokerReconciliationEvidence:
    """Extract one digest-only receipt from an endpoint or persisted report."""

    if not isinstance(payload, Mapping):
        raise ValueError("reconciliation receipt must be a JSON object")
    candidate = payload
    diagnostics = payload.get("diagnostics")
    if isinstance(diagnostics, Mapping):
        nested = diagnostics.get("broker_reconciliation")
        if isinstance(nested, Mapping):
            candidate = nested
    # The manual evidence workflow stores only this safe candidate beneath a
    # small artifact envelope.  Accept that envelope too, so enrollment never
    # needs the broader private execution report as an input.
    reconciliation = payload.get("reconciliation")
    if isinstance(reconciliation, Mapping):
        candidate = reconciliation
    evidence = candidate.get("evidence") if isinstance(candidate, Mapping) else None
    if not isinstance(evidence, Mapping):
        raise ValueError("receipt does not contain broker_reconciliation evidence")
    return BrokerReconciliationEvidence.from_dict(evidence)


def evaluate_receipts(
    payloads: Iterable[Mapping[str, Any]],
    *,
    source_receipt_records: Sequence[Mapping[str, object]],
    expectations: Sequence[SourceReceiptExpectation],
    now: datetime | None = None,
) -> dict[str, object]:
    """Return a redacted candidate or stable findings for a private controller."""

    evidences = [extract_reconciliation_evidence(payload) for payload in payloads]
    if not evidences:
        raise ValueError("at least one reconciliation receipt is required")
    root = calculate_source_receipts_sha256(
        source_receipt_records, strategy_profile=evidences[0].strategy_profile, expectations=expectations,
    )
    if sorted(record["evidence_sha256"] for record in source_receipt_records) != sorted(item.evidence_sha256 for item in evidences):
        raise ValueError("source records must match receipt evidence members exactly")
    evaluation = evaluate_broker_reconciliation_baseline_enrollment(evidences, now=now, source_receipts_sha256=root)
    result: dict[str, object] = {
        "schema_version": "ibkr_reconciliation_baseline_enrollment.v1",
        "ready_for_independent_review": evaluation.ready_for_independent_review,
        "findings": [finding.value for finding in evaluation.findings],
    }
    if evaluation.candidate is not None:
        result["candidate"] = validate_reconciliation_candidate_sources(
            evaluation.candidate, source_receipt_records=source_receipt_records, expectations=expectations,
        ).to_dict()
    return result


def _strict_json(data: str | bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("non-JSON constant")

    return json.loads(data, object_pairs_hook=pairs, parse_constant=reject_constant)


def _load_payload(path: Path) -> dict[str, Any]:
    try:
        value = _strict_json(path.read_bytes())
        if not isinstance(value, dict):
            raise ValueError("object required")
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("source_json_invalid") from exc
    return value


def load_source_inputs(
    records_path: Path, expectations_path: Path,
) -> tuple[list[Mapping[str, object]], tuple[SourceReceiptExpectation, ...]]:
    """Load existing private record lists, never infer trusted expectations."""
    try:
        records = _strict_json(records_path.read_bytes())
        raw_expectations = _strict_json(expectations_path.read_bytes())
        if not isinstance(records, list) or not isinstance(raw_expectations, list):
            raise ValueError("source inputs must be lists")
        expectations = tuple(SourceReceiptExpectation(**item) for item in raw_expectations)
    except (OSError, TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("private source inputs are invalid or unavailable") from exc
    return records, expectations


# Parsing limits for this digest-only producer artifact, not broker/risk limits.
_MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
_MAX_MEMBER_BYTES = 4 * 1024 * 1024
_ARTIFACT_FIELDS = frozenset({
    "schema_version", "strategy_profile", "reconciliation_scheduler_job_sha256",
    "report_status", "reconciliation", "errors",
})
_CANDIDATE_FIELDS = frozenset({
    "schema_version", "permits_active_lkg", "expected_digests_configured",
    "execution_ledger_records_count", "recovery_blockers", "evidence",
})


def _archive_payload(raw: bytes, expectation: SourceReceiptExpectation) -> dict[str, Any]:
    """Parse one already digest-authenticated producer ZIP, without extraction."""
    profile = expectation.strategy_profile
    if any(part in profile for part in ("/", "\\", "..")):
        raise ValueError("source_archive_profile_invalid")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        if len(members) != 1:
            raise ValueError("source_archive_members_invalid")
        member = members[0]
        file_type = stat.S_IFMT(member.external_attr >> 16)
        if (
            member.filename != f"{profile}.json" or member.orig_filename != member.filename or member.is_dir()
            or file_type not in (0, stat.S_IFREG) or member.flag_bits & 1
            or member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
            or member.file_size > _MAX_MEMBER_BYTES
        ):
            raise ValueError("source_archive_member_invalid")
        with archive.open(member) as stream:
            data = stream.read(_MAX_MEMBER_BYTES + 1)
        if len(data) > _MAX_MEMBER_BYTES:
            raise ValueError("source_archive_member_invalid")
        payload = _strict_json(data)
    if not isinstance(payload, dict) or set(payload) != _ARTIFACT_FIELDS:
        raise ValueError("source_envelope_fields_invalid")
    if (
        payload["schema_version"] != "ibkr_reconciliation_artifact.v1"
        or payload["strategy_profile"] != profile
        or payload["reconciliation_scheduler_job_sha256"] != expectation.scheduler_job_sha256
    ):
        raise ValueError("source_envelope_binding_mismatch")
    candidate = payload["reconciliation"]
    if not isinstance(candidate, dict) or set(candidate) != _CANDIDATE_FIELDS:
        raise ValueError("source_candidate_fields_invalid")
    if (
        candidate["schema_version"] != "ibkr_reconciliation_candidate.v1"
        or type(candidate["permits_active_lkg"]) is not bool
        or type(candidate["expected_digests_configured"]) is not bool
        or type(candidate["execution_ledger_records_count"]) is not int
        or candidate["execution_ledger_records_count"] < 0
        or not isinstance(candidate["recovery_blockers"], list)
        or any(not isinstance(item, str) for item in candidate["recovery_blockers"])
    ):
        raise ValueError("source_candidate_fields_invalid")
    if (
        payload["report_status"] is not None and not isinstance(payload["report_status"], str)
        or not isinstance(payload["errors"], list)
        or any(not isinstance(item, dict) or set(item) != {"stage", "error_type", "failure_category"}
               for item in payload["errors"])
    ):
        raise ValueError("source_envelope_fields_invalid")
    evidence = extract_reconciliation_evidence(payload)
    if (evidence.platform_id != "ibkr" or evidence.strategy_profile != profile
            or evidence.evidence_sha256 != expectation.evidence_sha256):
        raise ValueError("source_evidence_binding_mismatch")
    return payload


def load_authenticated_source_inputs(
    records_path: Path, expectations_path: Path, archive_paths: Sequence[Path],
) -> tuple[list[Mapping[str, object]], tuple[SourceReceiptExpectation, ...], list[dict[str, Any]]]:
    """Bind raw local artifact bytes to existing independent source designations.

    No provenance is inferred from the bytes or record. The operator/controller
    remains responsible for obtaining independent expectations from trusted
    workflow/run/artifact/service metadata. No downloader or new trust root.
    """
    try:
        records, expectations = load_source_inputs(records_path, expectations_path)
        canonical_source_receipt_records_json(
            records, strategy_profile=expectations[0].strategy_profile, expectations=expectations,
        )
        by_digest = {item.artifact_sha256: item for item in expectations}
        if len(by_digest) != len(expectations) or len(archive_paths) != len(expectations):
            raise ValueError("source_archive_cohort_invalid")
        authenticated = []
        seen = set()
        for path in archive_paths:
            with path.open("rb") as stream:
                raw = stream.read(_MAX_ARCHIVE_BYTES + 1)
            digest = hashlib.sha256(raw).hexdigest()
            if len(raw) > _MAX_ARCHIVE_BYTES or digest not in by_digest or digest in seen:
                raise ValueError("source_archive_digest_mismatch")
            seen.add(digest)
            authenticated.append((raw, by_digest[digest]))
        # No ZIP/JSON parsing occurs before the complete archive cohort matches.
        payloads = [_archive_payload(raw, expected) for raw, expected in authenticated]
    except (OSError, ValueError, TypeError, IndexError, KeyError, UnicodeError,
            zipfile.BadZipFile, zlib.error, NotImplementedError, RuntimeError, RecursionError) as exc:
        # Do not expose raw report bodies, ZIP member names, paths or parser details.
        raise ValueError("source_archive_authentication_failed") from exc
    return records, expectations, payloads


def validate_authenticated_candidate(
    candidate_payload: Mapping[str, Any], payloads: Sequence[Mapping[str, Any]], *,
    source_receipt_records: Sequence[Mapping[str, object]],
    expectations: Sequence[SourceReceiptExpectation], now: datetime | None = None,
) -> None:
    """Rebuild, rather than trusting a self-consistent candidate/content root."""
    rebuilt = evaluate_receipts(payloads, source_receipt_records=source_receipt_records,
                                expectations=expectations, now=now)
    if json.dumps(candidate_payload, sort_keys=True, allow_nan=False) != json.dumps(rebuilt, sort_keys=True, allow_nan=False):
        raise ValueError("source_candidate_not_derived_from_archives")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create a non-authorising legacy IBKR reconciliation baseline candidate."
    )
    parser.add_argument(
        "--receipt",
        action="append",
        type=Path,
        help="Optional parsed copies; must exactly match authenticated archive payloads.",
    )
    parser.add_argument("--source-archive", action="append", type=Path, required=True, help="Unmodified downloaded artifact ZIP; one per independent expectation")
    parser.add_argument("--source-records", type=Path, required=True, help="Private saved source record list")
    parser.add_argument("--source-expectations", type=Path, required=True, help="Independently verified private source expectations")
    parser.add_argument("--now", help="Optional ISO-8601 time used for deterministic validation.")
    args = parser.parse_args(argv)
    try:
        reference_now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else None
        records, expectations, payloads = load_authenticated_source_inputs(
            args.source_records, args.source_expectations, args.source_archive,
        )
        if args.receipt and [_load_payload(path) for path in args.receipt] != payloads:
            raise ValueError("source_parsed_copy_mismatch")
        result = evaluate_receipts(payloads, now=reference_now,
                                   source_receipt_records=records, expectations=expectations)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ready_for_independent_review"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
