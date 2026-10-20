from pathlib import Path
import json
import re


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
