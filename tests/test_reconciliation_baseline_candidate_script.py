from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

from scripts.build_reconciliation_baseline_candidate import (
    evaluate_receipts,
    evaluate_initial_receipt,
    extract_reconciliation_evidence,
    initial_facts_from_private_report,
)
from quant_platform_kit.common.broker_reconciliation import build_broker_reconciliation_evidence


def _digest(character: str) -> str:
    return character * 64


def _payload(*, observed_at: datetime, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "platform_id": "ibkr",
        "strategy_profile": "soxl_soxx_trend_income",
        "account_scope_sha256": _digest("a"),
        "baseline_id": "ibkr-soxl-lkg-20260830",
        "baseline_target_sha256": _digest("b"),
        "runtime_target_sha256": _digest("b"),
        "observed_at": observed_at,
        "broker_connected": True,
        "account_identity_match": True,
        "positions_match": False,
        "cash_match": False,
        "open_orders_match": False,
        "recent_executions_match": False,
        "local_execution_ledger_match": False,
        "positions_sha256": _digest("c"),
        "cash_sha256": _digest("d"),
        "open_orders_sha256": _digest("e"),
        "recent_executions_sha256": _digest("f"),
        "local_execution_ledger_sha256": _digest("0"),
    }
    values.update(overrides)
    return {
        "schema_version": "ibkr_reconciliation_candidate.v1",
        "evidence": build_broker_reconciliation_evidence(**values).to_dict(),
    }



def _sources_for_evidences(evidences):
    """Synthetic independently designated records, never broker observations."""
    from dataclasses import asdict
    from application.broker_reconciliation_candidate import SourceReceiptExpectation, SOURCE_RECEIPT_RECORD_SCHEMA_VERSION
    expectations = []
    records = []
    for index, evidence in enumerate(evidences, start=1):
        # Independent synthetic scheduler identity, not inferred from a record
        # under validation. Match the real producer's job-name hashing rule.
        scheduler_job_name = f"ibkr-reconcile-{evidence.strategy_profile}-{index}-1"
        expectation = SourceReceiptExpectation(
            strategy_profile=evidence.strategy_profile,
            repository="QuantStrategyLab/InteractiveBrokersPlatform",
            workflow_path=".github/workflows/collect-reconciliation-evidence.yml",
            workflow_ref="refs/heads/main", workflow_conclusion="success",
            workflow_run_id=str(index), workflow_run_attempt="1", workflow_head_sha="c" * 40,
            artifact_id=str(index), artifact_name=f"ibkr-reconciliation-{evidence.strategy_profile}-{index}",
            artifact_sha256="d" * 64, evidence_sha256=evidence.evidence_sha256,
            service_name="synthetic-service", service_revision="synthetic-revision",
            service_revision_commit_sha="e" * 40, service_deploy_run_id="100",
            scheduler_job_sha256=hashlib.sha256(scheduler_job_name.encode()).hexdigest(),
        )
        expectations.append(expectation)
        record = asdict(expectation)
        for key in ("strategy_profile", "workflow_ref", "workflow_conclusion"):
            record.pop(key)
        record["scheduler_job_sha256"] = hashlib.sha256(scheduler_job_name.encode()).hexdigest()
        records.append({"schema_version": SOURCE_RECEIPT_RECORD_SCHEMA_VERSION, **record})
    return {"source_receipt_records": records, "expectations": tuple(expectations)}


def test_initial_facts_must_match_authenticated_receipt_and_preserve_unknown() -> None:
    start = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
    receipt = _payload(observed_at=start)
    source = _sources_for_evidences([extract_reconciliation_evidence(receipt)])
    request_id = "853a2e08-9396-4fe8-89ee-59fb17e40a1d"
    scope = {"kind": "synthetic", "currency": None, "window_start": None, "window_end": None,
             "limit_per_namespace": None, "gateway_availability": None, "truncated": None}
    surfaces = {name: {"status": "unknown", "items": None, "scope": scope} for name in
                ("positions", "cash", "open_orders", "recent_executions", "local_execution_ledger")}
    facts = {"schema_version": "ibkr_initial_facts.v1", "account_scope_sha256": _digest("a"),
             "observed_at": "2026-08-30T08:00:00Z", "surfaces": surfaces}
    digest = hashlib.sha256(json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    report = {"diagnostics": {"broker_reconciliation": receipt,
                              "reconciliation_scheduler_job_sha256": source["source_receipt_records"][0]["scheduler_job_sha256"],
                              "reconciliation_request_id": request_id,
                              "initial_enrollment_private": {"facts": facts, "facts_sha256": digest}}}
    extracted = initial_facts_from_private_report(report, scheduler_job_sha256=source["source_receipt_records"][0]["scheduler_job_sha256"], request_id=request_id)
    result = evaluate_initial_receipt(receipt, extracted, now=start, private_source_sha256=_digest("1"))
    assert result["candidate"]["mode"] == "initial_enrollment"
    assert result["candidate"]["cash_sha256"] is None
    report["diagnostics"]["initial_enrollment_private"]["facts_sha256"] = _digest("f")
    with pytest.raises(ValueError, match="facts digest mismatch"):
        initial_facts_from_private_report(report, scheduler_job_sha256=source["source_receipt_records"][0]["scheduler_job_sha256"], request_id=request_id)


def test_builds_redacted_review_candidate_from_two_matching_receipts() -> None:
    start = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
    reconciled = dict.fromkeys(("positions_match", "cash_match", "open_orders_match", "recent_executions_match", "local_execution_ledger_match"), True)
    payloads = [_payload(observed_at=start, **reconciled), _payload(observed_at=start + timedelta(minutes=2), **reconciled)]
    result = evaluate_receipts(payloads, now=start + timedelta(minutes=3),
                              **_sources_for_evidences([extract_reconciliation_evidence(item) for item in payloads]))

    assert result["ready_for_independent_review"] is True
    assert result["findings"] == []
    candidate = result["candidate"]
    assert isinstance(candidate, dict)
    assert candidate["candidate_sha256"]
    assert "account_scope" not in candidate


def test_rejects_mismatched_receipts_without_authorisation() -> None:
    start = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
    payloads = [_payload(observed_at=start), _payload(observed_at=start + timedelta(minutes=2), cash_sha256=_digest("9"))]
    result = evaluate_receipts(payloads, now=start + timedelta(minutes=3),
                              **_sources_for_evidences([extract_reconciliation_evidence(item) for item in payloads]))

    assert result == {
        "schema_version": "ibkr_reconciliation_baseline_enrollment.v1",
        "ready_for_independent_review": False,
        "findings": ["broker_reconciliation_enrollment_observation_mismatch"],
    }


def test_accepts_persisted_runtime_report_shape() -> None:
    start = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
    payload = {"diagnostics": {"broker_reconciliation": _payload(observed_at=start)}}

    evidence = extract_reconciliation_evidence(payload)

    assert evidence.platform_id == "ibkr"


def test_accepts_redacted_workflow_artifact_shape() -> None:
    start = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
    payload = {
        "schema_version": "ibkr_reconciliation_artifact.v1",
        "strategy_profile": "soxl_soxx_trend_income",
        "reconciliation": _payload(observed_at=start),
    }

    evidence = extract_reconciliation_evidence(payload)

    assert evidence.platform_id == "ibkr"


def test_missing_redacted_evidence_is_rejected() -> None:
    with pytest.raises(ValueError, match="does not contain"):
        extract_reconciliation_evidence({"diagnostics": {}})



def _archive_inputs(directory, evidences):
    """Controlled raw bytes and independent synthetic run/service/job designations."""
    import io
    import json
    import zipfile
    from dataclasses import asdict, replace
    from application.broker_reconciliation import IBKRReconciliationCandidate

    directory.mkdir(parents=True, exist_ok=True)
    designated = _sources_for_evidences(evidences)
    archives, payloads, expectations, records = [], [], [], []
    for index, (evidence, expected) in enumerate(zip(evidences, designated["expectations"], strict=True)):
        # Same six-field safe envelope and actual to_safe_dict producer as the
        # protected workflow; no runtime reports, secrets or raw observations.
        payload = {
            "schema_version": "ibkr_reconciliation_artifact.v1",
            "strategy_profile": evidence.strategy_profile,
            "reconciliation_scheduler_job_sha256": expected.scheduler_job_sha256,
            "report_status": "success",
            "reconciliation": IBKRReconciliationCandidate(
                evidence=evidence, recovery_blockers=(), expected_digests_configured=True,
                execution_ledger_records_count=0,
            ).to_safe_dict(),
            "errors": [],
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(f"{evidence.strategy_profile}.json", json.dumps(payload))
        raw = buffer.getvalue()
        path = directory / f"{index}.zip"
        path.write_bytes(raw)
        # Expected bytes originate in the controlled fixture, never a record
        # being verified. Mutations below leave this designation independent.
        expected = replace(expected, artifact_sha256=hashlib.sha256(raw).hexdigest())
        record = dict(designated["source_receipt_records"][index], artifact_sha256=expected.artifact_sha256)
        archives.append(path)
        payloads.append(payload)
        expectations.append(expected)
        records.append(record)
    records_path = directory / "records.json"
    expectations_path = directory / "expectations.json"
    records_path.write_text(json.dumps(records))
    expectations_path.write_text(json.dumps([asdict(item) for item in expectations]))
    args = ["--source-records", str(records_path), "--source-expectations", str(expectations_path)]
    for archive in archives:
        args += ["--source-archive", str(archive)]
    return {"args": args, "archives": archives, "payloads": payloads, "records": records,
            "expectations": expectations, "records_path": records_path, "expectations_path": expectations_path}


def _rewrite_archive(material, payload=None, *, members=None, raw=None, designate=False):
    """Mutate bytes; optionally change only the independently specified ZIP digest."""
    import io
    import json
    import zipfile
    if raw is None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in (members if members is not None else [
                (f"{material['expectations'][0].strategy_profile}.json", json.dumps(payload)),
            ]):
                archive.writestr(name, data)
        raw = buffer.getvalue()
    material["archives"][0].write_bytes(raw)
    if designate:
        digest = hashlib.sha256(raw).hexdigest()
        for path in (material["records_path"], material["expectations_path"]):
            values = json.loads(path.read_text())
            values[0]["artifact_sha256"] = digest
            path.write_text(json.dumps(values))


def _healthy_evidence():
    start = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
    return extract_reconciliation_evidence(_payload(observed_at=start, **dict.fromkeys((
        "positions_match", "cash_match", "open_orders_match", "recent_executions_match", "local_execution_ledger_match",
    ), True)))


def _run_builder(material):
    from scripts import build_reconciliation_baseline_candidate as builder
    return builder.main(["--now", "2026-08-30T08:01:00Z", *material["args"]])


def test_cli_rejects_outer_scheduler_changed_under_unchanged_expectation(tmp_path, capsys):
    from copy import deepcopy
    material = _archive_inputs(tmp_path, [_healthy_evidence()])
    payload = deepcopy(material["payloads"][0])
    payload["reconciliation_scheduler_job_sha256"] = "f" * 64
    # Even a newly designated archive digest does not authorize another job.
    _rewrite_archive(material, payload, designate=True)
    with pytest.raises(SystemExit) as rejected:
        _run_builder(material)
    assert rejected.value.code == 2
    assert "source_archive_authentication_failed" in capsys.readouterr().err


def test_cli_requires_archive_no_parsed_fallback(tmp_path):
    import json
    from scripts import build_reconciliation_baseline_candidate as builder
    material = _archive_inputs(tmp_path, [_healthy_evidence()])
    parsed = tmp_path / "parsed.json"
    parsed.write_text(json.dumps(material["payloads"][0]))
    with pytest.raises(SystemExit) as rejected:
        builder.main(["--receipt", str(parsed), *material["args"][:4]])
    assert rejected.value.code == 2


def test_cli_authenticates_full_cohort_before_zip_parse(tmp_path, monkeypatch, capsys):
    from scripts import build_reconciliation_baseline_candidate as builder
    evidence = _healthy_evidence()
    # Use a distinct observation/digest and independent workflow/artifact member.
    second = extract_reconciliation_evidence(_payload(
        observed_at=evidence.observed_at + timedelta(seconds=30),
    ))
    material = _archive_inputs(tmp_path, [evidence, second])
    material["archives"][1].write_bytes(b"tampered ZIP bytes")
    calls = []
    monkeypatch.setattr(builder.zipfile, "ZipFile", lambda *_a, **_k: calls.append(1))
    with pytest.raises(SystemExit):
        _run_builder(material)
    assert not calls
    assert "tampered" not in capsys.readouterr().err


@pytest.mark.parametrize("field", [
    "workflow_run_id", "workflow_run_attempt", "workflow_head_sha", "artifact_id", "artifact_name",
    "artifact_sha256", "evidence_sha256", "service_name", "service_revision",
    "service_revision_commit_sha", "service_deploy_run_id", "scheduler_job_sha256",
])
def test_cli_rejects_record_not_matching_independent_designation(tmp_path, field):
    import json
    material = _archive_inputs(tmp_path, [_healthy_evidence()])
    records = material["records"]
    records[0][field] = ("0" if records[0][field][0] != "0" else "1") + records[0][field][1:]
    material["records_path"].write_text(json.dumps(records))
    with pytest.raises(SystemExit):
        _run_builder(material)


@pytest.mark.parametrize("mutation", [
    "profile", "scheduler_missing", "scheduler_type", "scheduler_format", "envelope_unknown",
    "candidate_unknown", "evidence_unknown", "evidence_missing", "scope_rehash", "inner_rehash",
    "inner_outer_swap", "duplicate_json", "invalid_json", "invalid_zip", "path", "backslash",
    "duplicate_member", "extra_member", "symlink", "empty_archive", "unknown_schema",
    "nul_member", "bad_crc", "bad_deflate",
])
def test_actual_builder_rejects_authenticated_but_invalid_archive_content(tmp_path, mutation):
    import json
    import stat
    import zipfile
    from copy import deepcopy
    from quant_platform_kit.common.broker_reconciliation import calculate_broker_reconciliation_evidence_sha256
    material = _archive_inputs(tmp_path, [_healthy_evidence()])
    payload = deepcopy(material["payloads"][0])
    evidence = payload["reconciliation"]["evidence"]
    members = raw = None
    name = f"{material['expectations'][0].strategy_profile}.json"
    if mutation == "profile":
        payload["strategy_profile"] = "other-profile"
    elif mutation == "scheduler_missing":
        payload.pop("reconciliation_scheduler_job_sha256")
    elif mutation == "scheduler_type":
        payload["reconciliation_scheduler_job_sha256"] = None
    elif mutation == "scheduler_format":
        payload["reconciliation_scheduler_job_sha256"] = "bad"
    elif mutation == "envelope_unknown":
        payload["caller_verified"] = True
    elif mutation == "candidate_unknown":
        payload["reconciliation"]["unknown"] = True
    elif mutation == "evidence_unknown":
        evidence["unknown"] = True
    elif mutation == "evidence_missing":
        evidence.pop("account_scope_sha256")
    elif mutation in ("scope_rehash", "inner_rehash"):
        evidence["account_scope_sha256" if mutation == "scope_rehash" else "positions_sha256"] = "1" * 64
        evidence["evidence_sha256"] = calculate_broker_reconciliation_evidence_sha256(evidence)
    elif mutation == "inner_outer_swap":
        evidence["evidence_sha256"] = material["expectations"][0].artifact_sha256
    elif mutation == "duplicate_json":
        members = [(name, json.dumps(payload)[:-1] + ',"strategy_profile":"other"}')]
    elif mutation == "invalid_json":
        members = [(name, "{invalid}")]
    elif mutation == "invalid_zip":
        raw = b"not a ZIP with a valid independently matched digest"
    elif mutation == "path":
        members = [("../" + name, json.dumps(payload))]
    elif mutation == "backslash":
        members = [("folder\\" + name, json.dumps(payload))]
    elif mutation == "duplicate_member":
        members = [(name, json.dumps(payload)), (name, json.dumps(payload))]
    elif mutation == "extra_member":
        members = [(name, json.dumps(payload)), ("other.json", "{}")]
    elif mutation == "symlink":
        info = zipfile.ZipInfo(name)
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        members = [(info, json.dumps(payload))]
    elif mutation == "empty_archive":
        members = []
    elif mutation == "nul_member":
        import io
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(name + "Xextra", json.dumps(payload))
        raw = buffer.getvalue().replace((name + "Xextra").encode(), (name + "\x00extra").encode())
    elif mutation in ("bad_crc", "bad_deflate"):
        raw = bytearray(material["archives"][0].read_bytes())
        if mutation == "bad_crc":
            offset = raw.index(b"PK\x01\x02") + 16
            raw[offset] ^= 1
        else:
            offset = 30 + int.from_bytes(raw[26:28], "little") + int.from_bytes(raw[28:30], "little")
            raw[offset] = 0xff  # Invalid DEFLATE block type.
        raw = bytes(raw)
    elif mutation == "unknown_schema":
        payload["schema_version"] = "ibkr_reconciliation_artifact.v2"
    _rewrite_archive(material, payload, members=members, raw=raw, designate=True)
    with pytest.raises(SystemExit) as rejected:
        _run_builder(material)
    assert rejected.value.code == 2


def test_builder_accepts_authenticated_bytes_but_rejects_different_parsed_copy(tmp_path, capsys):
    import json
    from scripts import build_reconciliation_baseline_candidate as builder
    material = _archive_inputs(tmp_path, [_healthy_evidence()])
    assert _run_builder(material) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ready_for_independent_review"]
    expected = material["expectations"][0]
    assert expected.artifact_sha256 != expected.evidence_sha256
    copy = tmp_path / "copy.json"
    copy.write_text(json.dumps(material["payloads"][0]))
    assert builder.main(["--now", "2026-08-30T08:01:00Z", "--receipt", str(copy), *material["args"]]) == 0
    capsys.readouterr()
    copy.write_text(json.dumps({"evidence": material["payloads"][0]["reconciliation"]["evidence"]}))
    with pytest.raises(SystemExit):
        builder.main(["--receipt", str(copy), *material["args"]])


@pytest.mark.parametrize("cohort", ["missing", "extra", "duplicate", "wrong_member", "ambiguous_designation"])
def test_cli_rejects_wrong_archive_cohort(tmp_path, cohort):
    import json
    material = _archive_inputs(tmp_path, [_healthy_evidence()])
    if cohort == "missing":
        args = material["args"][:4]
    elif cohort in ("extra", "duplicate"):
        args = material["args"] + ["--source-archive", str(material["archives"][0])]
    elif cohort == "wrong_member":
        material["archives"][0].write_bytes(b"another artifact")
        args = material["args"]
    else:
        # Two separately designated runs may not use one ambiguous ZIP digest.
        for path in (material["records_path"], material["expectations_path"]):
            values = json.loads(path.read_text())
            second = dict(values[0], workflow_run_id="2", artifact_id="2",
                          artifact_name="ibkr-reconciliation-soxl_soxx_trend_income-2")
            values.append(second)
            path.write_text(json.dumps(values))
        args = material["args"] + ["--source-archive", str(material["archives"][0])]
    from scripts import build_reconciliation_baseline_candidate as builder
    with pytest.raises(SystemExit):
        builder.main(args)


@pytest.mark.parametrize("mutation", ["missing", "unknown", "not_string", "short", "empty", "swapped_hashes"])
def test_source_record_wire_remains_strict_at_actual_cli(tmp_path, mutation):
    import json
    material = _archive_inputs(tmp_path, [_healthy_evidence()])
    record = material["records"][0]
    if mutation == "missing":
        record.pop("scheduler_job_sha256")
    elif mutation == "unknown":
        record["caller_verified"] = True
    elif mutation == "not_string":
        record["scheduler_job_sha256"] = 123
    elif mutation == "short":
        record["scheduler_job_sha256"] = "a" * 63
    elif mutation == "empty":
        record["scheduler_job_sha256"] = ""
    else:
        record["artifact_sha256"], record["evidence_sha256"] = record["evidence_sha256"], record["artifact_sha256"]
    material["records_path"].write_text(json.dumps(material["records"]))
    with pytest.raises(SystemExit):
        _run_builder(material)


@pytest.mark.parametrize("limit", ["archive", "member"])
def test_digest_only_archive_parsing_is_bounded(tmp_path, monkeypatch, limit):
    from scripts import build_reconciliation_baseline_candidate as builder
    material = _archive_inputs(tmp_path, [_healthy_evidence()])
    monkeypatch.setattr(builder, "_MAX_ARCHIVE_BYTES" if limit == "archive" else "_MAX_MEMBER_BYTES", 1)
    with pytest.raises(SystemExit):
        _run_builder(material)


def test_replay_is_idempotent_only_within_existing_freshness_not_new_authority(tmp_path, capsys):
    from scripts import build_reconciliation_baseline_candidate as builder
    material = _archive_inputs(tmp_path, [_healthy_evidence()])
    assert _run_builder(material) == 0
    original = capsys.readouterr().out
    assert _run_builder(material) == 0
    assert capsys.readouterr().out == original
    assert builder.main(["--now", "2026-08-31T08:00:00Z", *material["args"]]) == 2


def test_builder_authenticates_multiple_independent_archives_without_order_dependency(tmp_path, capsys):
    import json
    from scripts import build_reconciliation_baseline_candidate as builder
    from quant_platform_kit.common.broker_reconciliation import (
        BrokerReconciliationEvidence, calculate_broker_reconciliation_evidence_sha256,
    )
    first = _healthy_evidence()
    value = first.to_dict()
    value["observed_at"] = "2026-08-30T08:00:30Z"
    value["evidence_sha256"] = calculate_broker_reconciliation_evidence_sha256(value)
    second = BrokerReconciliationEvidence.from_dict(value)
    material = _archive_inputs(tmp_path, [first, second])
    assert _run_builder(material) == 0
    result = json.loads(capsys.readouterr().out)
    reversed_args = material["args"][:4]
    for path in reversed(material["archives"]):
        reversed_args += ["--source-archive", str(path)]
    assert builder.main(["--now", "2026-08-30T08:01:00Z", *reversed_args]) == 0
    assert json.loads(capsys.readouterr().out) == result
    assert len(result["candidate"]["source_evidence_sha256"]) == 2
