from __future__ import annotations

import json
from datetime import datetime, timezone

import scripts.publish_account_facts_from_report as publisher
from scripts.publish_account_facts_from_report import (
    project_ibkr_account_facts_history,
)


def _report() -> dict[str, object]:
    return {
        "schema_version": "runtime_report.v1",
        "platform": "interactive_brokers",
        "deploy_target": "cloud_run",
        "project_id": "qsl-prod",
        "service_name": "interactive-brokers-quant-live-u16608560-service",
        "account_scope": "live-u16608560",
        "runtime_target": {
            "account_selector": ["U16608560"],
            "deployment_selector": "live-u16608560",
        },
        "diagnostics": {"runtime_revision": "service-00369-88c"},
        "runtime_release_receipt": {"attestation_state": "legacy_unattested"},
        "started_at": "2026-09-30T01:00:00Z",
        "finished_at": "2026-09-30T01:00:02Z",
        "summary": {
            "account_facts": {
                "schema_version": "ibkr_account_snapshot.v1",
                "account_ids": ["U16608560"],
                "currency": "USD",
                "observed_at": "2026-09-30T01:00:01Z",
                "net_assets": "12345.6700",
                "cash": [
                    {"currency": "USD", "cash_balance": "100.00", "source_tag": "$LEDGER-CashBalance"},
                    {"currency": "HKD", "cash_balance": "-0.01", "source_tag": "CashBalance"},
                ],
            }
        },
    }


def _project(report: dict[str, object]) -> dict[str, object]:
    return project_ibkr_account_facts_history(
        report,
        target_id="ibkr-u16608560",
        expected_report_prefix="gs://qsl-runtime-reports/ibkr",
        source_report_uri="gs://qsl-runtime-reports/ibkr/report-1.json",
        expected_project_id="qsl-prod",
        expected_service_name="interactive-brokers-quant-live-u16608560-service",
        expected_runtime_revision="service-00369-88c",
        expected_account_scope="live-u16608560",
        expected_account_selector=["U16608560"],
        expected_deployment_selector="live-u16608560",
    )


def test_projects_bound_ibkr_facts_and_keeps_legacy_receipt_unchanged():
    report = _report()
    history = _project(report)

    assert history["schema_version"] == "ibkr_account_snapshot_history.v1"
    assert history["snapshot_schema_version"] == "ibkr_account_snapshot.v1"
    assert history["account_ids"] == ["U16608560"]
    assert history["broker_reported_balances"] == [
        {"currency": "USD", "net_assets": "12345.6700"}
    ]
    assert history["cash"] == [
        {"currency": "USD", "cash_balance": "100.00", "source_tag": "$LEDGER-CashBalance"},
        {"currency": "HKD", "cash_balance": "-0.01", "source_tag": "CashBalance"},
    ]
    assert history["source_binding"]["kind"] == "deployment_runtime_account"
    assert "source_report_uri" not in history
    assert set(history) == {
        "schema_version", "snapshot_schema_version", "account_scope", "target_id",
        "source_binding", "observed_started_at", "observed_finished_at", "snapshot_atomic",
        "observation_date", "broker_reported_balances", "cash", "account_ids",
    }
    assert history["observed_started_at"] == "2026-09-30T01:00:01+00:00"
    assert history["observed_finished_at"] == "2026-09-30T01:00:01+00:00"
    assert report["runtime_release_receipt"] == {"attestation_state": "legacy_unattested"}


def test_binding_is_stable_for_amount_changes_and_changes_with_revision():
    first = _project(_report())
    changed_amounts = _report()
    changed_amounts["summary"]["account_facts"]["net_assets"] = "999"  # type: ignore[index]
    second = _project(changed_amounts)
    assert first["source_binding"] == second["source_binding"]

    changed_revision = _report()
    changed_revision["diagnostics"]["runtime_revision"] = "service-00370-1ab"  # type: ignore[index]
    third = _project(changed_revision)
    assert third["status"] == "skipped"
    assert third["reason"] == "runtime_target_mismatch"

    receiptless = _report()
    receiptless.pop("runtime_release_receipt")
    assert _project(receiptless)["schema_version"] == "ibkr_account_snapshot_history.v1"


def test_projection_fails_closed_on_scope_identity_and_selector_mismatch():
    mismatched_identity = _report()
    mismatched_identity["summary"]["account_facts"]["account_ids"] = ["U999"]  # type: ignore[index]
    assert _project(mismatched_identity) == {"status": "skipped", "reason": "account_identity_mismatch"}

    default_selector = _report()
    default_selector["runtime_target"]["account_selector"] = ["default"]  # type: ignore[index]
    result = project_ibkr_account_facts_history(
        default_selector,
        target_id="ibkr-u16608560",
        expected_report_prefix="gs://qsl-runtime-reports/ibkr",
        source_report_uri="gs://qsl-runtime-reports/ibkr/report-1.json",
        expected_project_id="qsl-prod",
        expected_service_name="interactive-brokers-quant-live-u16608560-service",
        expected_runtime_revision="service-00369-88c",
        expected_account_scope="live-u16608560",
        expected_account_selector=["default"],
        expected_deployment_selector="live-u16608560",
    )
    assert result == {"status": "skipped", "reason": "expected_target_invalid"}


def test_projection_rejects_invalid_observation_provenance_and_duplicate_cash():
    naive_time = _report()
    naive_time["started_at"] = "2026-09-30T01:00:00"
    assert _project(naive_time) == {"status": "skipped", "reason": "observation_invalid"}

    out_of_prefix = project_ibkr_account_facts_history(
        _report(),
        target_id="ibkr-u16608560",
        expected_report_prefix="gs://qsl-runtime-reports/ibkr",
        source_report_uri="gs://other-bucket/ibkr/report-1.json",
        expected_project_id="qsl-prod",
        expected_service_name="interactive-brokers-quant-live-u16608560-service",
        expected_runtime_revision="service-00369-88c",
        expected_account_scope="live-u16608560",
        expected_account_selector=["U16608560"],
        expected_deployment_selector="live-u16608560",
    )
    assert out_of_prefix == {"status": "skipped", "reason": "report_provenance_invalid"}

    duplicates = _report()
    duplicates["summary"]["account_facts"]["cash"].append(  # type: ignore[index]
        {"currency": "USD", "cash_balance": "1", "source_tag": "CashBalance"}
    )
    assert _project(duplicates) == {"status": "skipped", "reason": "account_facts_invalid"}


def test_projection_requires_revision_and_preserves_null_assets():
    missing_revision = _report()
    missing_revision["diagnostics"]["runtime_revision"] = None  # type: ignore[index]
    assert _project(missing_revision) == {"status": "skipped", "reason": "runtime_target_mismatch"}

    unavailable_assets = _report()
    unavailable_assets["summary"]["account_facts"]["net_assets"] = None  # type: ignore[index]
    result = _project(unavailable_assets)
    assert result["broker_reported_balances"] == [{"currency": "USD", "net_assets": None}]


def test_projection_enforces_native_account_cash_tags_and_amount_precision():
    invalid_id = _report()
    invalid_id["summary"]["account_facts"]["account_ids"] = ["ACCT1"]  # type: ignore[index]
    assert _project(invalid_id) == {"status": "skipped", "reason": "account_identity_mismatch"}

    invalid_tag = _report()
    invalid_tag["summary"]["account_facts"]["cash"][0]["source_tag"] = "TotalCashValue"  # type: ignore[index]
    assert _project(invalid_tag) == {"status": "skipped", "reason": "account_facts_invalid"}

    excessive_scale = _report()
    excessive_scale["summary"]["account_facts"]["cash"][0]["cash_balance"] = "0.000000001"  # type: ignore[index]
    assert _project(excessive_scale) == {"status": "skipped", "reason": "account_facts_invalid"}


def test_projection_rejects_snapshot_observation_outside_report_interval():
    report = _report()
    report["summary"]["account_facts"]["observed_at"] = "2026-09-30T00:59:59Z"  # type: ignore[index]
    assert _project(report) == {"status": "skipped", "reason": "observation_invalid"}

    padded_selector = _report()
    padded_selector["runtime_target"]["account_selector"] = [" U16608560"]  # type: ignore[index]
    assert _project(padded_selector) == {"status": "skipped", "reason": "runtime_target_mismatch"}


def test_publisher_posts_once_with_fresh_projected_payload_and_dedicated_token(monkeypatch):
    observed = {}

    class Response:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class Opener:
        def open(self, request, *, timeout):
            observed["request"] = request
            observed["timeout"] = timeout
            return Response()

    monkeypatch.setattr(publisher, "build_opener", lambda *_args: Opener())
    result = publisher.publish_ibkr_account_facts_history(
        _report(),
        now=datetime(2026, 9, 30, 1, 1, 30, tzinfo=timezone.utc),
        source_report_uri="gs://qsl-runtime-reports/ibkr/report-1.json",
        sync_url=publisher.IBKR_ACCOUNT_FACTS_SYNC_URL,
        sync_token="dedicated-test-token",
        target_id="ibkr-u16608560",
        expected_report_prefix="gs://qsl-runtime-reports/ibkr",
        expected_project_id="qsl-prod",
        expected_service_name="interactive-brokers-quant-live-u16608560-service",
        expected_runtime_revision="service-00369-88c",
        expected_account_scope="live-u16608560",
        expected_account_selector=["U16608560"],
        expected_deployment_selector="live-u16608560",
    )
    assert result == {"status": "published"}
    request = observed["request"]
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer dedicated-test-token"
    assert json.loads(request.data)["account_ids"] == ["U16608560"]
    assert observed["timeout"] == 15


def test_publisher_rejects_stale_or_missing_facts_without_http(monkeypatch):
    monkeypatch.setattr(
        publisher,
        "build_opener",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not POST")),
    )
    args = {
        "source_report_uri": "gs://qsl-runtime-reports/ibkr/report-1.json",
        "sync_url": publisher.IBKR_ACCOUNT_FACTS_SYNC_URL,
        "sync_token": "dedicated-test-token",
        "target_id": "ibkr-u16608560",
        "expected_report_prefix": "gs://qsl-runtime-reports/ibkr",
        "expected_project_id": "qsl-prod",
        "expected_service_name": "interactive-brokers-quant-live-u16608560-service",
        "expected_runtime_revision": "service-00369-88c",
        "expected_account_scope": "live-u16608560",
        "expected_account_selector": ["U16608560"],
        "expected_deployment_selector": "live-u16608560",
    }
    recent_wrapper = _report()
    recent_wrapper["finished_at"] = "2026-09-30T01:20:00Z"
    stale = publisher.publish_ibkr_account_facts_history(
        recent_wrapper, now=datetime(2026, 9, 30, 1, 20, tzinfo=timezone.utc), **args
    )
    assert stale == {"status": "skipped", "reason": "observation_stale"}

    missing_facts = _report()
    missing_facts["summary"].pop("account_facts")  # type: ignore[union-attr]
    missing = publisher.publish_ibkr_account_facts_history(
        missing_facts, now=datetime(2026, 9, 30, 1, 1, 30, tzinfo=timezone.utc), **args
    )
    assert missing == {"status": "skipped", "reason": "account_facts_invalid"}

    wrong_endpoint = publisher.publish_ibkr_account_facts_history(
        _report(),
        now=datetime(2026, 9, 30, 1, 1, 30, tzinfo=timezone.utc),
        **{**args, "sync_url": "https://attacker.example/api/account-facts/sync"},
    )
    assert wrong_endpoint == {"status": "skipped", "reason": "publish_target_invalid"}


def test_latest_report_listing_is_confined_to_exact_prefix(monkeypatch):
    seen = []

    class Result:
        returncode = 0
        stderr = ""

        def __init__(self, stdout):
            self.stdout = stdout

    def run(argv, **_kwargs):
        seen.append(argv[3])
        return Result(
            "gs://bucket/root/interactive_brokers/tqqq_growth_income/live-u16608560/2026-09/"
            "20260930T010000Z.json\n"
            "gs://bucket/root/interactive_brokers/tqqq_growth_income/live-u16608560-other/2026-09/"
            "20260930T010500Z.json\n"
        )

    monkeypatch.setattr(publisher.subprocess, "run", run)
    uri = publisher._latest_report_uri(
        prefix="gs://bucket/root/interactive_brokers/tqqq_growth_income/live-u16608560",
        project_id="project",
        now=datetime(2026, 9, 30, 1, 10, tzinfo=timezone.utc),
    )
    assert uri.endswith("/2026-09/20260930T010000Z.json")
    assert all("live-u16608560/" in value for value in seen)
