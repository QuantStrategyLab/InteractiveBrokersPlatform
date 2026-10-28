from pathlib import Path
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import subprocess
import textwrap
import zipfile

import pytest


WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "collect-reconciliation-evidence-protected.yml"
)


def test_reconciliation_evidence_matrix_comes_from_private_inventory() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "resolve-matrix:" in workflow
    assert "render_runtime_target_matrix.py --profile reconciliation --private-config --github-output" in workflow
    assert "needs: resolve-matrix" in workflow
    assert "matrix: ${{ fromJSON(needs.resolve-matrix.outputs.matrix) }}" in workflow
    # Neither public examples nor real account bindings belong in the workflow.
    assert "interactive-brokers-quant-live-u00000001-service" not in workflow
    assert "interactive-brokers-quant-live-u00000002-service" not in workflow
    assert "interactive-brokers-quant-live-u00000003-service" not in workflow
    assert "interactive-brokers-quant-live-u00000004-service" not in workflow
    assert "- profile: soxl_soxx_trend_income\n            service:" not in workflow


def test_reconciliation_evidence_uses_internal_one_shot_scheduler_and_cleans_up() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert '"${SERVICE_URL}/reconcile"' in workflow
    scheduler_command = workflow.split("gcloud scheduler jobs create http", 1)[1].split("--quiet", 1)[0]
    assert "--max-retry-attempts=0" in scheduler_command
    assert "--max-retry-duration=0s" in scheduler_command
    assert 'schedule="$(date -u -d "+${delay_minutes} minutes"' in workflow
    assert "gcloud scheduler jobs delete" in workflow
    assert "gcloud storage cat \"$report_uri\"" in workflow
    assert "gcloud scheduler jobs run" not in workflow
    assert "/run" not in workflow
    assert "curl " not in workflow


def test_reconciliation_evidence_records_receipt_source_without_self_reference() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    required_markers = (
        'gcloud logging read',
        '--format=json',
        'log_entry=',
        'serving_revision',
        'gcloud run revisions describe "$serving_revision"',
        'id: receipt_artifact',
        'steps.receipt_artifact.outputs.artifact-id',
        'steps.receipt_artifact.outputs.artifact-digest',
        'Generate reconciliation source record',
        'Upload reconciliation source record',
        'ibkr_reconciliation_source_receipt_record.v1',
        'schema_version',
        'service_revision_commit_sha',
        'service_deploy_run_id',
        'does not prove scheduler correlation or that this workflow run created the revision',
    )
    for marker in required_markers:
        assert marker in workflow

    record_fields = (
        'schema_version',
        'repository',
        'workflow_path',
        'workflow_run_id',
        'workflow_run_attempt',
        'workflow_head_sha',
        'artifact_id',
        'artifact_name',
        'artifact_sha256',
        'evidence_sha256',
        'service_name',
        'service_revision',
        'service_revision_commit_sha',
        'service_deploy_run_id',
        'scheduler_job_sha256',
    )
    for field in record_fields:
        assert f'--arg {field}' in workflow

    receipt_upload = workflow.index('id: receipt_artifact')
    source_record = workflow.index('Generate reconciliation source record')
    source_upload = workflow.index('Upload reconciliation source record')
    assert workflow.index('Delete temporary internal reconciliation job') < receipt_upload
    assert receipt_upload < source_record < source_upload
    assert 'steps.source_record_artifact.outputs.artifact-digest' not in workflow


def test_reconciliation_evidence_preserves_sanitized_log_query_failure_class() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert 'logging_read_failures=0' in workflow
    assert 'receipt_marker_seen=false' in workflow
    assert 'reconciliation_log_query_failed class=logging_read_nonzero_exit' in workflow
    assert 'reconciliation_log_query_timeout class=receipt_scheduler_job_mismatch' in workflow
    assert 'reconciliation_log_query_timeout class=no_matching_candidate' in workflow
    assert 'gcloud logging read' in workflow
    assert '--format=json 2>/dev/null' in workflow


def test_reconciliation_evidence_binds_scheduler_job_to_exact_receipt() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    required_markers = (
        "scheduler_job_config=\"$(gcloud scheduler jobs describe",
        "scheduler_job_sha256=\"$(printf '%s' \"$job_name\" | sha256sum",
        "SCHEDULER_JOB_SHA256: ${{ steps.scheduler.outputs.scheduler_job_sha256 }}",
        "scheduler_job_sha256=\" + $scheduler_job_sha256 + \" report_uri=gs://",
        "--arg scheduler_job_sha256 \"$SCHEDULER_JOB_SHA256\"",
        ".reconciliation_scheduler_job_sha256 == $scheduler_job_sha256",
    )
    for marker in required_markers:
        assert marker in workflow

    assert 'textPayload:\\"reconciliation_receipt_ready\\"' in workflow


def _run_receipt_wait(tmp_path, *, mode, ready_after=0):
    workflow = WORKFLOW.read_text(encoding="utf-8")
    step = workflow.split("      - name: Read private no-order reconciliation candidate", 1)[1].split("      - name:", 1)[0]
    # Execute the actual wait and error classification, replacing only clock/cloud I/O.
    fragment = "log_entry=''" + textwrap.dedent(step.split("          log_entry=''", 1)[1].split('          receipt="', 1)[0])
    marker = json.dumps([{
        "textPayload": "reconciliation_receipt_ready scheduler_job_sha256=" + "a" * 64 + " report_uri=gs://synthetic/report.json",
        "resource": {"labels": {"revision_name": "synthetic-revision"}},
    }])
    script = '''set -euo pipefail
unset SECONDS
SECONDS=0
sleep() { SECONDS=$((SECONDS + $1)); }
timeout() {
  test "$1" = '--kill-after=5s'
  test "$2" = '30s'
  shift 2
  "$@"
}
gcloud() {
  test "$1 $2" = 'logging read'
  case "$MODE" in
    error) return 7 ;;
    invalid) printf '%s' 'invalid-json'; return ;;
  esac
  if [ "$MODE" = late ] && [ "$SECONDS" -ge "$READY_AFTER" ]; then
    printf '%s' "$MARKER"
  else
    printf '%s' '[]'
  fi
}
''' + fragment + '\nprintf "matched_after=%s\\n" "$SECONDS"\n'
    return subprocess.run(
        ["/bin/bash", "-c", script], cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin:/sbin", "LC_ALL": "C", "MODE": mode, "READY_AFTER": str(ready_after),
             "MARKER": marker, "SCHEDULER_JOB_SHA256": "a" * 64, "SERVICE": "synthetic-service",
             "REQUESTED_AT": "2026-10-28T00:00:00Z", "GCP_PROJECT_ID": "synthetic-project"},
        capture_output=True, text=True, timeout=10, check=False,
    )


@pytest.mark.parametrize("ready_after", [550, 900])
def test_real_receipt_wait_accepts_late_russell_receipt(tmp_path, ready_after):
    result = _run_receipt_wait(tmp_path, mode="late", ready_after=ready_after)
    assert result.returncode == 0, result.stderr
    assert f"matched_after={ready_after}" in result.stdout


@pytest.mark.parametrize(("mode", "expected"), [
    ("empty", "reconciliation_log_query_timeout class=no_matching_candidate"),
    ("error", "reconciliation_log_query_failed class=logging_read_nonzero_exit"),
    ("invalid", "reconciliation_log_query_failed class=invalid_logging_json"),
])
def test_real_receipt_wait_preserves_bounded_failure_class(tmp_path, mode, expected):
    result = _run_receipt_wait(tmp_path, mode=mode)
    assert result.returncode != 0
    assert expected in result.stderr
    assert 'gs://' not in result.stdout + result.stderr


def test_receipt_wait_budget_covers_scheduler_request_logs_and_cleanup():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    delays = [int(value) for value in re.findall(r"delay_minutes=(\d+)", workflow)]
    request_deadline = int(re.search(r"--attempt-deadline '(\d+)s'", workflow).group(1))
    wait = re.search(r"log_wait_seconds=\$\(\((\d+) \* 60 \+ (\d+) \+ (\d+)\)\)", workflow)
    delay_minutes, request_seconds, log_margin = map(int, wait.groups())
    assert delay_minutes >= max(delays)
    assert request_seconds >= request_deadline
    assert log_margin >= 120
    wait_seconds = delay_minutes * 60 + request_seconds + log_margin
    collect = workflow.split('  collect:', 1)[1]
    job_minutes = int(re.search(r'timeout-minutes: (\d+)', collect).group(1))
    assert job_minutes * 60 >= wait_seconds + 30 + 5 + 300
    freshness_minutes = int(re.search(r'--freshness=(\d+)m', collect).group(1))
    assert freshness_minutes * 60 >= wait_seconds + 35


def test_producer_source_record_field_shape_reaches_strict_consumer():
    from application.broker_reconciliation_candidate import canonical_source_receipt_records_json
    from tests.test_broker_reconciliation_candidate import _expectations, _records

    workflow = WORKFLOW.read_text(encoding="utf-8")
    source_object = workflow.split("'{\n              schema_version:", 1)[1].split("}'", 1)[0]
    producer_fields = {"schema_version"} | set(re.findall(r"^\s+([a-z_0-9]+):\s+\$", source_object, re.M))
    assert len(producer_fields) == 15
    assert "scheduler_job_sha256" in producer_fields
    # Field shape comes from the real producer, not the consumer's field set.
    # Expectations are independently specified synthetic run/revision bindings.
    records = [{field: record[field] for field in producer_fields} for record in _records()]
    canonical = json.loads(canonical_source_receipt_records_json(
        records, strategy_profile="global_etf_rotation", expectations=_expectations(),
    ))
    assert all(set(record) == producer_fields for record in canonical)
    assert [record["scheduler_job_sha256"] for record in canonical] == ["e" * 64, "f" * 64]


def _producer_materials(tmp_path, *, receipt_fault=None):
    from application.broker_reconciliation_candidate import SourceReceiptExpectation
    from tests.test_reconciliation_recovery_verify_only import _evidence

    start = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    evidence = _evidence(observed_at=start)
    profile = evidence.strategy_profile
    job_name = f"ibkr-reconcile-{profile}-45-1"
    scheduler_hash = hashlib.sha256(job_name.encode()).hexdigest()
    runtime_report = {
        "status": "ok", "errors": [],
        "diagnostics": {
            "reconciliation_scheduler_job_sha256": scheduler_hash,
            "broker_reconciliation": {
                "schema_version": "ibkr_reconciliation_candidate.v1",
                "evidence": evidence.to_dict(), "recovery_blockers": [],
                "permits_active_lkg": True, "expected_digests_configured": True,
            },
        },
    }
    if receipt_fault == "scheduler":
        runtime_report["diagnostics"]["reconciliation_scheduler_job_sha256"] = "0" * 64
    elif receipt_fault is not None:
        runtime_report["diagnostics"]["broker_reconciliation"]["evidence"]["evidence_sha256"] = receipt_fault

    workflow = WORKFLOW.read_text(encoding="utf-8")
    receipt_step = workflow.split("      - name: Read private no-order reconciliation candidate", 1)[1].split("      - name:", 1)[0]
    # Execute the exact jq transformation, guards and digest extraction from
    # the real producer. Replace only its cloud input with synthetic stdin.
    receipt_fragment = receipt_step.split('gcloud storage cat "$report_uri" | ', 1)[1].split("          jq -r '[.reconciliation.permits_active_lkg", 1)[0]
    receipt_script = "set -euo pipefail\nmkdir -p reports\n" + textwrap.dedent(receipt_fragment)
    output = tmp_path / "receipt-output"
    clean_env = {
        "PATH": "/usr/bin:/bin:/sbin", "LC_ALL": "C", "PROFILE": profile,
        "SCHEDULER_JOB_SHA256": scheduler_hash, "GITHUB_OUTPUT": str(output),
        "serving_revision": "synthetic-revision", "service_revision_commit_sha": "e" * 40,
        "service_deploy_run_id": "100",
    }
    assert not any(command in receipt_script for command in ("gcloud ", "gh ", "curl ", "wget "))
    receipt_run = subprocess.run(
        ["/bin/bash", "-c", receipt_script], cwd=tmp_path, env=clean_env,
        input=json.dumps(runtime_report), capture_output=True, text=True, check=False,
    )
    if receipt_fault is not None:
        return {"receipt_run": receipt_run}
    assert receipt_run.returncode == 0, receipt_run.stderr
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    report_path = tmp_path / "reports" / f"{profile}.json"
    artifact_path = tmp_path / "synthetic-upload-artifact.zip"
    with zipfile.ZipFile(artifact_path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.write(report_path, arcname=f"{profile}.json")
    # upload-artifact's outer archive digest is independently supplied to the
    # record step. It is never replaced by the nested canonical evidence hash.
    artifact_hash = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    record_step = workflow.split("      - name: Generate reconciliation source record", 1)[1].split("      - name:", 1)[0]
    record_script = textwrap.dedent(record_step.split("        run: |\n", 1)[1])
    assert not any(command in record_script for command in ("gcloud ", "gh ", "curl ", "wget "))
    record_env = {
        "PATH": "/usr/bin:/bin:/sbin", "LC_ALL": "C",
        "ARTIFACT_ID": "45", "ARTIFACT_SHA256": artifact_hash,
        "EVIDENCE_SHA256": outputs["evidence_sha256"],
        "REPOSITORY": "QuantStrategyLab/InteractiveBrokersPlatform",
        "WORKFLOW_REF": "QuantStrategyLab/InteractiveBrokersPlatform/.github/workflows/collect-reconciliation-evidence.yml@refs/heads/main",
        "WORKFLOW_SHA": "c" * 40, "WORKFLOW_HEAD_SHA": "c" * 40,
        "WORKFLOW_RUN_ID": "45", "WORKFLOW_RUN_ATTEMPT": "1", "PROFILE": profile,
        "SERVICE": "synthetic-service", "SERVICE_REVISION": "synthetic-revision",
        "SERVICE_REVISION_COMMIT_SHA": "e" * 40, "SERVICE_DEPLOY_RUN_ID": "100",
        "SCHEDULER_JOB_SHA256": scheduler_hash,
    }
    record_run = subprocess.run(
        ["/bin/bash", "-c", record_script], cwd=tmp_path, env=record_env,
        capture_output=True, text=True, check=False,
    )
    assert record_run.returncode == 0, record_run.stderr
    record = json.loads((tmp_path / "source-records" / f"{profile}.json").read_text())
    # Designation is assembled from known synthetic job/run/revision metadata
    # and the actual archive bytes, never from the untrusted generated record.
    expectation = SourceReceiptExpectation(
        strategy_profile=profile, repository="QuantStrategyLab/InteractiveBrokersPlatform",
        workflow_path=".github/workflows/collect-reconciliation-evidence.yml",
        workflow_ref="refs/heads/main", workflow_conclusion="success", workflow_run_id="45",
        workflow_run_attempt="1", workflow_head_sha="c" * 40, artifact_id="45",
        artifact_name=f"ibkr-reconciliation-{profile}-45", artifact_sha256=artifact_hash,
        evidence_sha256=evidence.evidence_sha256, service_name="synthetic-service",
        service_revision="synthetic-revision", service_revision_commit_sha="e" * 40,
        service_deploy_run_id="100", scheduler_job_sha256=hashlib.sha256(job_name.encode()).hexdigest(),
    )
    return {
        "record": record, "expectation": expectation, "payload": json.loads(report_path.read_text()),
        "evidence": evidence, "artifact_path": artifact_path, "report_path": report_path, "start": start,
    }


def test_real_producer_materials_reach_builder_and_verify_only(tmp_path):
    from scripts.build_reconciliation_baseline_candidate import evaluate_receipts
    from scripts.verify_reconciliation_recovery import verify_reconciliation_recovery
    from tests.test_reconciliation_recovery_verify_only import _console_response, _evidence, _runtime_target

    material = _producer_materials(tmp_path)
    source = {"source_receipt_records": [material["record"]], "expectations": [material["expectation"]]}
    candidate = evaluate_receipts([material["payload"]], now=material["start"], **source)
    record = material["record"]
    assert record["artifact_sha256"] == hashlib.sha256(material["artifact_path"].read_bytes()).hexdigest()
    assert record["evidence_sha256"] == material["evidence"].evidence_sha256
    assert record["artifact_sha256"] != record["evidence_sha256"]
    assert record["scheduler_job_sha256"] == material["payload"]["reconciliation_scheduler_job_sha256"]
    assert candidate["ready_for_independent_review"] is True
    confirmed_at = material["start"] + timedelta(minutes=1)
    confirmation = _console_response(candidate["candidate"]["candidate_sha256"], confirmed_at=confirmed_at)
    confirmation["recovery"]["evidence_sample_count"] = 1
    result = verify_reconciliation_recovery(
        candidate_payload=candidate, **source, confirmation_payload=confirmation,
        current_receipt_payload={"evidence": _evidence(observed_at=confirmed_at + timedelta(minutes=1)).to_dict()},
        runtime_target_payload=_runtime_target(), recovery_id="ibkr-soxl-live-recovery",
        now=confirmed_at + timedelta(minutes=2),
    )
    assert result["ready_for_atomic_state_transition"] is True
    assert result["policy"] == {
        "controller_mode": "verify_only", "no_order": True,
        "execution_authority_granted": False, "state_write_attempted": False,
    }


@pytest.mark.parametrize("fault", [
    "scheduler", "hash_swap", "outer_file_evidence_hash", "artifact_tamper", "evidence_tamper",
    "rehashed_evidence_tamper", "unknown", "missing", "malformed", "wrong_run",
])
def test_real_producer_materials_reject_tampering(tmp_path, fault):
    from quant_platform_kit.common.broker_reconciliation import calculate_broker_reconciliation_evidence_sha256
    from scripts.build_reconciliation_baseline_candidate import evaluate_receipts

    material = _producer_materials(tmp_path)
    record, payload = material["record"], material["payload"]
    if fault == "scheduler":
        record["scheduler_job_sha256"] = "0" * 64
    elif fault == "hash_swap":
        record["artifact_sha256"], record["evidence_sha256"] = record["evidence_sha256"], record["artifact_sha256"]
    elif fault == "outer_file_evidence_hash":
        record["evidence_sha256"] = hashlib.sha256(material["report_path"].read_bytes()).hexdigest()
    elif fault == "artifact_tamper":
        record["artifact_sha256"] = "0" * 64
    elif fault in {"evidence_tamper", "rehashed_evidence_tamper"}:
        evidence = payload["reconciliation"]["evidence"]
        evidence["cash_sha256"] = "9" * 64
        if fault == "rehashed_evidence_tamper":
            evidence["evidence_sha256"] = calculate_broker_reconciliation_evidence_sha256(evidence)
    elif fault == "unknown":
        record["untrusted_extra"] = "not-allowed"
    elif fault == "missing":
        record.pop("scheduler_job_sha256")
    elif fault == "malformed":
        record["scheduler_job_sha256"] = "Z" * 64
    elif fault == "wrong_run":
        record["workflow_run_id"] = "99"
    with pytest.raises(ValueError):
        evaluate_receipts(
            [payload], now=material["start"], source_receipt_records=[record],
            expectations=[material["expectation"]],
        )


@pytest.mark.parametrize("fault", ["scheduler", "", None, 1, "g" * 64, "A" * 64, "a" * 63])
def test_real_receipt_fragment_rejects_scheduler_or_malformed_inner_hash(tmp_path, fault):
    # None is also a malformed JSON digest, not a request to skip this control.
    if fault is None:
        fault = False
    material = _producer_materials(tmp_path, receipt_fault=fault)
    assert material["receipt_run"].returncode != 0


def test_shared_source_fixture_does_not_infer_expectation_from_tampered_record():
    from scripts.build_reconciliation_baseline_candidate import evaluate_receipts
    from tests.test_reconciliation_baseline_candidate_script import _sources_for_evidences
    from tests.test_reconciliation_recovery_verify_only import _evidence

    evidence = _evidence(observed_at=datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc))
    sources = _sources_for_evidences([evidence])
    original_designation = asdict(sources["expectations"][0])
    sources["source_receipt_records"][0]["scheduler_job_sha256"] = "0" * 64
    assert asdict(sources["expectations"][0]) == original_designation
    with pytest.raises(ValueError, match="provenance does not match"):
        evaluate_receipts([{"evidence": evidence.to_dict()}], now=evidence.observed_at, **sources)
