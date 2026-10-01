from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

import scripts.publish_account_facts_from_report as publisher
from scripts.publish_account_facts_from_report import (
    project_ibkr_account_facts_history,
)


def _report() -> dict[str, object]:
    observed_at = datetime(2026, 9, 30, 1, 0, 1, tzinfo=timezone.utc)
    started_at = observed_at.replace(microsecond=0)
    finished_at = observed_at.replace(microsecond=0).replace(second=2)
    return {
        "schema_version": "runtime_report.v1",
        "platform": "interactive_brokers",
        "deploy_target": "cloud_run",
        "project_id": "example-project",
        "service_name": "ibkr-primary-service",
        "account_scope": "live-primary",
        "runtime_target": {
            "account_selector": ["U00000001"],
            "deployment_selector": "live-primary",
        },
        "diagnostics": {"runtime_revision": "runtime-revision-001"},
        "runtime_release_receipt": {"attestation_state": "legacy_unattested"},
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "finished_at": finished_at.isoformat().replace("+00:00", "Z"),
        "summary": {
            "account_facts": {
                "schema_version": "ibkr_account_snapshot.v1",
                "account_ids": ["U00000001"],
                "currency": "USD",
                "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
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
        target_id="ibkr-primary",
        expected_report_prefix="gs://qsl-runtime-reports/ibkr",
        source_report_uri="gs://qsl-runtime-reports/ibkr/report-1.json",
        expected_project_id="example-project",
        expected_service_name="ibkr-primary-service",
        expected_runtime_revision="runtime-revision-001",
        expected_account_scope="live-primary",
        expected_account_selector=["U00000001"],
        expected_deployment_selector="live-primary",
    )


def _additional_targets() -> dict[str, dict[str, object]]:
    return {
        f"additional-{index}": {
            "project_id": f"private-project-{index}",
            "report_prefix": f"gs://private-bucket/reports/target-{index}",
            "target_id": f"private-target-{index}",
            "service_name": f"private-service-{index}",
            "runtime_revision": f"private-revision-{index}",
            "account_scope": f"private-scope-{index}",
            "account_selector": [f"U0000001{index}"],
            "deployment_selector": f"private-deployment-{index}",
        }
        for index in range(1, 4)
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_slot",
        "extra_slot",
        "duplicate_identity",
        "duplicate_target",
        "duplicate_prefix",
        "duplicate_prefix_with_slash",
        "missing_field",
        "extra_field",
        "invalid_slot_identity",
    ],
)
def test_additional_target_config_is_strict_and_unique(mutation):
    config = _additional_targets()
    if mutation == "missing_slot":
        del config["additional-3"]
    elif mutation == "extra_slot":
        config["additional-4"] = config["additional-3"]
    elif mutation == "duplicate_identity":
        config["additional-2"]["account_selector"] = config["additional-1"]["account_selector"]
    elif mutation == "duplicate_target":
        config["additional-2"]["target_id"] = config["additional-1"]["target_id"]
    elif mutation == "duplicate_prefix":
        config["additional-2"]["report_prefix"] = config["additional-1"]["report_prefix"]
    elif mutation == "duplicate_prefix_with_slash":
        config["additional-2"]["report_prefix"] = config["additional-1"]["report_prefix"] + "/"
    elif mutation == "missing_field":
        del config["additional-1"]["runtime_revision"]
    elif mutation == "extra_field":
        config["additional-1"]["account_name"] = "must-not-be-accepted"
    else:
        config["additional-1"]["account_selector"] = ["default"]

    with pytest.raises(publisher._ProjectionError):
        publisher._additional_target_configs(json.dumps(config))


def test_additional_target_config_rejects_malformed_and_duplicate_json_keys():
    for raw in (
        "{broken",
        '{"additional-1":{},"additional-1":{},"additional-2":{},"additional-3":{}}',
    ):
        with pytest.raises(publisher._ProjectionError):
            publisher._additional_target_configs(raw)


def test_additional_target_preflight_rejects_primary_identity_collision(monkeypatch):
    config = _additional_targets()
    config["additional-1"]["account_selector"] = ["U00000001"]
    monkeypatch.setenv(
        publisher.IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON_ENV,
        json.dumps(config),
    )
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON", '["U00000001"]')
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_TARGET_ID", "ibkr-primary")
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_REPORT_PREFIX", "gs://example-private/ibkr/reports")

    with pytest.raises(publisher._ProjectionError):
        publisher.additional_target_environment("additional-2")


@pytest.mark.parametrize("failure", ["missing", "malformed", "duplicate_target", "invalid_slot"])
def test_additional_target_cli_fails_closed_before_gcs_or_post(monkeypatch, capsys, failure):
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_TARGET", "additional-1")
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_REPORT_NAME", "20261001T125959Z.json")
    config = _additional_targets()
    if failure == "missing":
        monkeypatch.delenv(publisher.IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON_ENV, raising=False)
    elif failure == "malformed":
        monkeypatch.setenv(publisher.IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON_ENV, "{broken")
    else:
        if failure == "duplicate_target":
            config["additional-2"]["target_id"] = config["additional-1"]["target_id"]
        monkeypatch.setenv(
            publisher.IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON_ENV,
            json.dumps(config),
        )
        if failure == "invalid_slot":
            monkeypatch.setenv("IBKR_ACCOUNT_FACTS_TARGET", "additional-4")
    monkeypatch.setattr(
        publisher,
        "_latest_report_uri",
        lambda **_kwargs: pytest.fail("invalid target config must stop before GCS listing"),
    )
    monkeypatch.setattr(
        publisher,
        "_named_report_uri",
        lambda **_kwargs: pytest.fail("invalid target config must stop before report selection"),
    )
    monkeypatch.setattr(
        publisher,
        "_load_gcs_report",
        lambda *_args, **_kwargs: pytest.fail("invalid target config must stop before GCS read"),
    )
    monkeypatch.setattr(
        publisher,
        "publish_ibkr_account_facts_history",
        lambda *_args, **_kwargs: pytest.fail("invalid target config must stop before POST"),
    )

    result = publisher.main()

    assert result == (0 if failure == "invalid_slot" else 1)
    output = capsys.readouterr().out.strip()
    assert output == (
        "skipped:target_disabled"
        if failure == "invalid_slot"
        else "skipped:target_config_unavailable"
        if failure == "missing"
        else "skipped:target_config_invalid"
    )
    assert "private-project" not in output


def test_projects_bound_ibkr_facts_and_keeps_legacy_receipt_unchanged():
    report = _report()
    history = _project(report)

    assert history["schema_version"] == "ibkr_account_snapshot_history.v1"
    assert history["snapshot_schema_version"] == "ibkr_account_snapshot.v1"
    assert history["account_ids"] == ["U00000001"]
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
        target_id="ibkr-primary",
        expected_report_prefix="gs://qsl-runtime-reports/ibkr",
        source_report_uri="gs://qsl-runtime-reports/ibkr/report-1.json",
        expected_project_id="example-project",
        expected_service_name="ibkr-primary-service",
        expected_runtime_revision="runtime-revision-001",
        expected_account_scope="live-primary",
        expected_account_selector=["default"],
        expected_deployment_selector="live-primary",
    )
    assert result == {"status": "skipped", "reason": "expected_target_invalid"}


def test_projection_rejects_invalid_observation_provenance_and_duplicate_cash():
    naive_time = _report()
    naive_time["started_at"] = "2026-09-30T01:00:00"
    assert _project(naive_time) == {"status": "skipped", "reason": "observation_invalid"}

    out_of_prefix = project_ibkr_account_facts_history(
        _report(),
        target_id="ibkr-primary",
        expected_report_prefix="gs://qsl-runtime-reports/ibkr",
        source_report_uri="gs://other-bucket/ibkr/report-1.json",
        expected_project_id="example-project",
        expected_service_name="ibkr-primary-service",
        expected_runtime_revision="runtime-revision-001",
        expected_account_scope="live-primary",
        expected_account_selector=["U00000001"],
        expected_deployment_selector="live-primary",
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
    padded_selector["runtime_target"]["account_selector"] = [" U00000001"]  # type: ignore[index]
    assert _project(padded_selector) == {"status": "skipped", "reason": "runtime_target_mismatch"}


def test_publisher_posts_once_with_fresh_projected_payload_and_dedicated_token(monkeypatch):
    observed = {}

    class Response:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return b'{"ok":true,"stored":true,"unchanged":false}'

    class Opener:
        def open(self, request, *, timeout):
            observed["request"] = request
            observed["timeout"] = timeout
            return Response()

    monkeypatch.setattr(publisher, "build_opener", lambda *_args: Opener())
    result = publisher.publish_ibkr_account_facts_history(
        _report(),
        now=datetime(2026, 9, 30, 1, 40, 1, tzinfo=timezone.utc),
        source_report_uri="gs://qsl-runtime-reports/ibkr/report-1.json",
        sync_url=publisher.IBKR_ACCOUNT_FACTS_SYNC_URL,
        sync_token="dedicated-test-token",
        target_id="ibkr-primary",
        expected_report_prefix="gs://qsl-runtime-reports/ibkr",
        expected_project_id="example-project",
        expected_service_name="ibkr-primary-service",
        expected_runtime_revision="runtime-revision-001",
        expected_account_scope="live-primary",
        expected_account_selector=["U00000001"],
        expected_deployment_selector="live-primary",
    )
    assert result == {"status": "published"}
    request = observed["request"]
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer dedicated-test-token"
    assert request.get_header("User-agent") == publisher.IBKR_ACCOUNT_FACTS_USER_AGENT
    assert json.loads(request.data)["account_ids"] == ["U00000001"]
    assert observed["timeout"] == 15


def test_publisher_accepts_36_hour_boundary_and_rejects_older_or_future_without_http(monkeypatch):
    observed_requests = []

    class Response:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return b'{"ok":true,"stored":true,"unchanged":false}'

    class Opener:
        def open(self, request, *, timeout):
            observed_requests.append((request, timeout))
            return Response()

    monkeypatch.setattr(publisher, "build_opener", lambda *_args: Opener())
    args = {
        "source_report_uri": "gs://qsl-runtime-reports/ibkr/report-1.json",
        "sync_url": publisher.IBKR_ACCOUNT_FACTS_SYNC_URL,
        "sync_token": "dedicated-test-token",
        "target_id": "ibkr-primary",
        "expected_report_prefix": "gs://qsl-runtime-reports/ibkr",
        "expected_project_id": "example-project",
        "expected_service_name": "ibkr-primary-service",
        "expected_runtime_revision": "runtime-revision-001",
        "expected_account_scope": "live-primary",
        "expected_account_selector": ["U00000001"],
        "expected_deployment_selector": "live-primary",
    }
    boundary = publisher.publish_ibkr_account_facts_history(
        _report(), now=datetime(2026, 10, 1, 13, 0, 1, tzinfo=timezone.utc), **args
    )
    assert boundary == {"status": "published"}
    assert len(observed_requests) == 1
    assert observed_requests[0][1] == 15

    older = publisher.publish_ibkr_account_facts_history(
        _report(), now=datetime(2026, 10, 1, 13, 0, 2, tzinfo=timezone.utc), **args
    )
    assert older == {"status": "skipped", "reason": "observation_stale"}

    future = _report()
    future_facts = future["summary"]["account_facts"]  # type: ignore[index]
    future_facts["observed_at"] = "2026-09-30T01:06:01Z"  # type: ignore[index]
    future["started_at"] = "2026-09-30T01:06:00Z"
    future["finished_at"] = "2026-09-30T01:06:02Z"
    future_observation = publisher.publish_ibkr_account_facts_history(
        future, now=datetime(2026, 9, 30, 1, 0, 1, tzinfo=timezone.utc), **args
    )
    assert future_observation == {"status": "skipped", "reason": "observation_stale"}

    missing_facts = _report()
    missing_facts["summary"].pop("account_facts")  # type: ignore[union-attr]
    missing = publisher.publish_ibkr_account_facts_history(
        missing_facts, now=datetime(2026, 9, 30, 1, 1, 30, tzinfo=timezone.utc), **args
    )
    assert missing == {"status": "skipped", "reason": "account_facts_invalid"}
    assert len(observed_requests) == 1

    wrong_endpoint = publisher.publish_ibkr_account_facts_history(
        _report(),
        now=datetime(2026, 9, 30, 1, 1, 30, tzinfo=timezone.utc),
        **{**args, "sync_url": "https://attacker.example/api/account-facts/sync"},
    )
    assert wrong_endpoint == {"status": "skipped", "reason": "publish_target_invalid"}


def test_publisher_reports_unchanged_observation_without_claiming_refresh(monkeypatch):
    observed = {"calls": 0}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return b'{"ok":true,"stored":true,"unchanged":true}'

    class Opener:
        def open(self, *_args, **_kwargs):
            observed["calls"] += 1
            return Response()

    monkeypatch.setattr(publisher, "build_opener", lambda *_args: Opener())
    result = publisher.publish_ibkr_account_facts_history(
        _report(),
        now=datetime(2026, 9, 30, 1, 1, 30, tzinfo=timezone.utc),
        source_report_uri="gs://example-private/ibkr/report-1.json",
        sync_url=publisher.IBKR_ACCOUNT_FACTS_SYNC_URL,
        sync_token="dedicated-test-token",
        target_id="ibkr-primary",
        expected_report_prefix="gs://example-private/ibkr",
        expected_project_id="example-project",
        expected_service_name="ibkr-primary-service",
        expected_runtime_revision="runtime-revision-001",
        expected_account_scope="live-primary",
        expected_account_selector=["U00000001"],
        expected_deployment_selector="live-primary",
    )
    assert result == {"status": "unchanged", "reason": "observation_unchanged"}
    assert observed["calls"] == 1


@pytest.mark.parametrize(
    ("status", "body_error", "expected_error"),
    [
        (401, "account_facts_sync_token_invalid", "account_facts_sync_token_invalid"),
        (409, "account_facts_bindings_missing", "account_facts_bindings_missing"),
        (503, "account_facts_store_unavailable", "account_facts_store_unavailable"),
        (409, "UNAPPROVED_ERROR_SENTINEL", "unknown"),
    ],
)
def test_publisher_retains_http_status_and_allowlisted_qrs_error_without_retry(
    monkeypatch, status, body_error, expected_error
):
    observed = {"calls": 0}

    class Opener:
        def open(self, request, *, timeout):
            observed["calls"] += 1
            raise HTTPError(
                request.full_url,
                status,
                "private response text",
                {},
                BytesIO(json.dumps({"error": body_error}).encode()),
            )

    monkeypatch.setattr(publisher, "build_opener", lambda *_args: Opener())
    result = publisher.publish_ibkr_account_facts_history(
        _report(),
        now=datetime(2026, 9, 30, 1, 1, 30, tzinfo=timezone.utc),
        source_report_uri="gs://qsl-runtime-reports/ibkr/report-1.json",
        sync_url=publisher.IBKR_ACCOUNT_FACTS_SYNC_URL,
        sync_token="dedicated-test-token",
        target_id="ibkr-primary",
        expected_report_prefix="gs://qsl-runtime-reports/ibkr",
        expected_project_id="example-project",
        expected_service_name="ibkr-primary-service",
        expected_runtime_revision="runtime-revision-001",
        expected_account_scope="live-primary",
        expected_account_selector=["U00000001"],
        expected_deployment_selector="live-primary",
    )

    assert result == {
        "status": "skipped",
        "reason": "publish_failed",
        "diagnostics": {
            "stage": "http_response",
            "category": "http_error",
            "http_status": status,
            "qrs_error_code": expected_error,
            "outcome": "unknown",
        },
    }
    assert observed["calls"] == 1


@pytest.mark.parametrize(
    ("error", "category", "stage"),
    [
        (TimeoutError("TIMEOUT_SENTINEL"), "timeout", "request"),
        (URLError("URL_REASON_SENTINEL"), "url_error", "request"),
        (RuntimeError("UNKNOWN_TRANSPORT_SENTINEL"), "unknown", "unknown"),
    ],
)
def test_publisher_marks_transport_outcome_unknown_without_exposing_exception(
    monkeypatch, error, category, stage
):
    class Opener:
        def open(self, *_args, **_kwargs):
            raise error

    monkeypatch.setattr(publisher, "build_opener", lambda *_args: Opener())
    result = publisher.publish_ibkr_account_facts_history(
        _report(),
        now=datetime(2026, 9, 30, 1, 1, 30, tzinfo=timezone.utc),
        source_report_uri="gs://qsl-runtime-reports/ibkr/report-1.json",
        sync_url=publisher.IBKR_ACCOUNT_FACTS_SYNC_URL,
        sync_token="dedicated-test-token",
        target_id="ibkr-primary",
        expected_report_prefix="gs://qsl-runtime-reports/ibkr",
        expected_project_id="example-project",
        expected_service_name="ibkr-primary-service",
        expected_runtime_revision="runtime-revision-001",
        expected_account_scope="live-primary",
        expected_account_selector=["U00000001"],
        expected_deployment_selector="live-primary",
    )

    assert result == {
        "status": "skipped",
        "reason": "publish_failed",
        "diagnostics": {
            "stage": stage,
            "category": category,
            "http_status": None,
            "qrs_error_code": "unknown",
            "outcome": "unknown",
        },
    }
    assert "SENTINEL" not in repr(result)


def test_cli_prints_allowlisted_http_diagnostics_without_private_response_data(
    monkeypatch, capsys
):
    now = datetime(2026, 9, 30, 1, 1, 30, tzinfo=timezone.utc)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is not None else now.replace(tzinfo=None)

    class Opener:
        def open(self, request, *, timeout):
            raise HTTPError(
                "https://endpoint.invalid/URL_SENTINEL",
                503,
                "MESSAGE_SENTINEL",
                {"X-Debug": "HEADER_SENTINEL"},
                BytesIO(
                    b'{"error":"account_facts_store_unavailable",'
                    b'"debug":"BODY_SENTINEL"}'
                ),
            )

    monkeypatch.setattr(publisher, "datetime", FrozenDateTime)
    report = _report()
    report["project_id"] = "example-project"
    monkeypatch.setattr(
        publisher,
        "_latest_report_uri",
        lambda **_kwargs: (
            "gs://example-private/execution-reports/interactive_brokers/"
            "example-profile/live-primary/2026-09/20260930T010000Z.json"
        ),
    )
    monkeypatch.setattr(publisher, "_load_gcs_report", lambda *_args, **_kwargs: report)
    monkeypatch.setattr(publisher, "build_opener", lambda *_args: Opener())
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_TARGET", publisher.IBKR_ACCOUNT_FACTS_PRIMARY_TARGET)
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_REPORT_PREFIX", "gs://example-private/execution-reports/interactive_brokers/example-profile/live-primary")
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_TARGET_ID", "ibkr-primary")
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_PROJECT_ID", "example-project")
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_SERVICE_NAME", "ibkr-primary-service")
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_RUNTIME_REVISION", "runtime-revision-001")
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ACCOUNT_SCOPE", "live-primary")
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON", '["U00000001"]')
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_DEPLOYMENT_SELECTOR", "live-primary")
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_SYNC_URL", publisher.IBKR_ACCOUNT_FACTS_SYNC_URL)
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_SYNC_TOKEN", "TOKEN_SENTINEL")

    assert publisher.main() == 1
    output = capsys.readouterr().out
    assert output.strip() == (
        "skipped:publish_failed:stage=http_response:category=http_error:"
        "http_status=503:qrs_error_code=account_facts_store_unavailable:outcome=unknown"
    )
    for sentinel in (
        "URL_SENTINEL", "MESSAGE_SENTINEL", "HEADER_SENTINEL", "BODY_SENTINEL", "TOKEN_SENTINEL"
    ):
        assert sentinel not in output


def test_default_cli_target_makes_no_api_or_report_calls(monkeypatch, capsys):
    monkeypatch.delenv("IBKR_ACCOUNT_FACTS_TARGET", raising=False)
    monkeypatch.setattr(
        publisher,
        "build_opener",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not call API")),
    )
    monkeypatch.setattr(
        publisher,
        "_latest_report_uri",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not list GCS")),
    )
    monkeypatch.setattr(
        publisher,
        "_load_gcs_report",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not read GCS")),
    )

    assert publisher.main() == 0
    assert capsys.readouterr().out.strip() == "skipped:target_disabled"


def test_ingress_diagnostic_posts_fixed_empty_body_once_without_gcs(monkeypatch, capsys):
    observed = {"calls": 0}

    class Response:
        status = 400

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, limit):
            observed["read_limit"] = limit
            return b'{"error":"invalid_account_facts_history"}'

    class Opener:
        def open(self, request, *, timeout):
            observed["calls"] += 1
            observed["request"] = request
            observed["timeout"] = timeout
            return Response()

    monkeypatch.setattr(publisher, "build_opener", lambda *_args: Opener())
    monkeypatch.setattr(
        publisher,
        "_latest_report_uri",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("diagnostic must not list GCS")),
    )
    monkeypatch.setattr(
        publisher,
        "_load_gcs_report",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("diagnostic must not read GCS")),
    )
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_TARGET", publisher.IBKR_ACCOUNT_FACTS_INGRESS_DIAGNOSTIC_TARGET)
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_SYNC_TOKEN", "TOKEN_SENTINEL")
    monkeypatch.delenv("IBKR_ACCOUNT_FACTS_SYNC_URL", raising=False)

    assert publisher.main() == 0
    output = capsys.readouterr().out
    assert output.strip() == (
        "verified:ingress_authentication_and_schema_rejection_verified:"
        "stage=http_response:category=http_status:http_status=400:"
        "qrs_error_code=invalid_account_facts_history:outcome=rejected_before_storage"
    )
    request = observed["request"]
    assert request.full_url == publisher.IBKR_ACCOUNT_FACTS_SYNC_URL
    assert request.data == b"{}"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer TOKEN_SENTINEL"
    assert request.get_header("User-agent") == publisher.IBKR_ACCOUNT_FACTS_USER_AGENT
    assert request.get_header("Content-type") == "application/json"
    assert observed["calls"] == 1
    assert observed["timeout"] == 15
    assert "TOKEN_SENTINEL" not in output


@pytest.mark.parametrize(
    ("response_status", "body_error", "exception", "expected_status", "expected_reason", "expected_code"),
    [
        (None, "account_facts_sync_token_invalid", None, "skipped", "ingress_diagnostic_unverified", "account_facts_sync_token_invalid"),
        (None, "invalid_account_facts_history", None, "verified", "ingress_authentication_and_schema_rejection_verified", "invalid_account_facts_history"),
        (None, "UNSAFE_ERROR_SENTINEL", None, "skipped", "ingress_diagnostic_unverified", "unknown"),
        (200, None, None, "skipped", "ingress_diagnostic_unverified", "unknown"),
        (None, None, TimeoutError("TIMEOUT_SENTINEL"), "skipped", "ingress_diagnostic_unverified", "unknown"),
    ],
)
def test_ingress_diagnostic_verifies_only_exact_qrs_schema_rejection(
    monkeypatch, response_status, body_error, exception, expected_status, expected_reason, expected_code
):
    class Response:
        status = response_status

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return json.dumps({"error": body_error}).encode() if body_error else b""

    class Opener:
        def open(self, request, *, timeout):
            assert request.data == b"{}"
            if exception is not None:
                raise exception
            if response_status is None:
                raise HTTPError(
                    request.full_url,
                    400 if body_error != "account_facts_sync_token_invalid" else 401,
                    "private response message",
                    {},
                    BytesIO(json.dumps({"error": body_error}).encode()),
                )
            return Response()

    monkeypatch.setattr(publisher, "build_opener", lambda *_args: Opener())
    result = publisher.diagnose_account_facts_ingress(sync_token="diagnostic-token")
    assert result["status"] == expected_status
    assert result["reason"] == expected_reason
    assert result["diagnostics"]["qrs_error_code"] == expected_code
    assert result["diagnostics"]["outcome"] == (
        "rejected_before_storage" if expected_status == "verified" else "unknown"
    )
    assert "SENTINEL" not in repr(result)


def test_ingress_diagnostic_rejects_missing_token_without_api_call(monkeypatch):
    monkeypatch.setattr(
        publisher,
        "build_opener",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not call API")),
    )
    assert publisher.diagnose_account_facts_ingress(sync_token="") == {
        "status": "skipped",
        "reason": "publish_auth_unavailable",
    }


def test_ingress_diagnostic_does_not_follow_redirect(monkeypatch):
    observed = {"calls": 0}

    class Opener:
        def open(self, request, *, timeout):
            observed["calls"] += 1
            raise HTTPError(
                request.full_url,
                302,
                "redirect response",
                {"Location": "https://other.example/redirect"},
                BytesIO(b'{"error":"invalid_account_facts_history"}'),
            )

    def build_opener(handler):
        assert isinstance(handler, publisher._NoRedirect)
        return Opener()

    monkeypatch.setattr(publisher, "build_opener", build_opener)
    result = publisher.diagnose_account_facts_ingress(sync_token="diagnostic-token")
    assert result["status"] == "skipped"
    assert result["diagnostics"] == {
        "stage": "http_response",
        "category": "http_error",
        "http_status": 302,
        "qrs_error_code": "invalid_account_facts_history",
        "outcome": "unknown",
    }
    assert observed["calls"] == 1


def test_ingress_diagnostic_cli_redacts_http_exception(monkeypatch, capsys):
    class Opener:
        def open(self, *_args, **_kwargs):
            raise HTTPError(
                "https://endpoint.invalid/URL_SENTINEL",
                401,
                "MESSAGE_SENTINEL",
                {"X-Debug": "HEADER_SENTINEL"},
                BytesIO(
                    b'{"error":"account_facts_sync_token_invalid",'
                    b'"debug":"BODY_SENTINEL"}'
                ),
            )

    monkeypatch.setattr(publisher, "build_opener", lambda *_args: Opener())
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_TARGET", publisher.IBKR_ACCOUNT_FACTS_INGRESS_DIAGNOSTIC_TARGET)
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_SYNC_TOKEN", "TOKEN_SENTINEL")

    assert publisher.main() == 1
    output = capsys.readouterr().out
    assert output.strip() == (
        "skipped:ingress_diagnostic_unverified:stage=http_response:category=http_error:"
        "http_status=401:qrs_error_code=account_facts_sync_token_invalid:outcome=unknown"
    )
    for sentinel in (
        "URL_SENTINEL", "MESSAGE_SENTINEL", "HEADER_SENTINEL", "BODY_SENTINEL", "TOKEN_SENTINEL"
    ):
        assert sentinel not in output


def test_workflow_ingress_diagnostic_is_manual_and_isolated_from_heartbeat():
    workflow = Path(__file__).parents[1] / ".github/workflows/execution-report-heartbeat.yml"
    source = workflow.read_text()
    assert "default: disabled" in source
    assert "- ingress-diagnostic" in source
    assert "inputs.account_facts_target != 'ingress-diagnostic'" in source
    diagnostic_job = source.split("  account-facts-ingress-diagnostic:", 1)[1]
    assert "inputs.account_facts_target == 'ingress-diagnostic'" in diagnostic_job
    assert "contents: read" in diagnostic_job
    assert "id-token: write" not in diagnostic_job
    assert "google-github-actions" not in diagnostic_job
    assert "setup-uv" not in diagnostic_job
    assert "uv sync" not in diagnostic_job
    assert "GCP_" not in diagnostic_job
    assert "RUNTIME_HEARTBEAT_" not in diagnostic_job
    assert "IBKR_ACCOUNT_FACTS_SYNC_TOKEN: ${{ secrets.IBKR_ACCOUNT_FACTS_SYNC_TOKEN }}" in diagnostic_job
    assert "run: python3 scripts/publish_account_facts_from_report.py" in diagnostic_job


def test_workflow_scheduled_publisher_is_independent_and_single_target():
    workflow = Path(__file__).parents[1] / ".github/workflows/execution-report-heartbeat.yml"
    source = workflow.read_text()
    assert "- primary-live" in source
    assert "Publish one validated report" not in source.split("  heartbeat:", 1)[1].split("  account-facts-publisher:", 1)[0]
    publisher_job = source.split("  account-facts-publisher:", 1)[1].split("  account-facts-ingress-diagnostic:", 1)[0]
    assert "github.event_name == 'schedule'" in publisher_job
    assert "inputs.account_facts_target == 'primary-live'" in publisher_job
    assert "IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON: ${{ secrets.IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON }}" in publisher_job
    assert "IBKR_ACCOUNT_FACTS_REPORT_PREFIX: ${{ secrets.IBKR_ACCOUNT_FACTS_REPORT_PREFIX }}" in publisher_job
    assert "IBKR_ACCOUNT_FACTS_TARGET_ID: ${{ secrets.IBKR_ACCOUNT_FACTS_TARGET_ID }}" in publisher_job
    assert "IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON: ${{ secrets.IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON }}" in publisher_job
    assert "IBKR_ACCOUNT_FACTS_TARGET: ${{ inputs.account_facts_target || 'primary-live' }}" in publisher_job
    assert "strategy:" not in publisher_job
    assert "id-token: write" in publisher_job
    assert "IBKR_ACCOUNT_FACTS_REPORT_NAME: ${{ inputs.account_facts_report_name }}" in publisher_job


def _publisher_job_environment() -> dict[str, str]:
    workflow = Path(__file__).parents[1] / ".github/workflows/execution-report-heartbeat.yml"
    source = workflow.read_text(encoding="utf-8")
    job = source.split("  account-facts-publisher:", 1)[1].split(
        "\n  account-facts-ingress-diagnostic:", 1
    )[0]
    env_block = job.split("\n    env:\n", 1)[1].split("\n    steps:", 1)[0]
    placeholders = {
        ("secrets", "IBKR_ACCOUNT_FACTS_PROJECT_ID"): "example-project",
        ("secrets", "IBKR_ACCOUNT_FACTS_REPORT_PREFIX"): "gs://example-private/ibkr/reports",
        ("secrets", "IBKR_ACCOUNT_FACTS_TARGET_ID"): "ibkr-example",
        ("secrets", "IBKR_ACCOUNT_FACTS_SERVICE_NAME"): "example-ibkr-service",
        ("secrets", "IBKR_ACCOUNT_FACTS_RUNTIME_REVISION"): "example-revision",
        ("secrets", "IBKR_ACCOUNT_FACTS_ACCOUNT_SCOPE"): "live-example",
        ("secrets", "IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON"): '["U00000001"]',
        ("secrets", "IBKR_ACCOUNT_FACTS_DEPLOYMENT_SELECTOR"): "live-example",
        ("vars", "IBKR_ACCOUNT_FACTS_SYNC_URL"): publisher.IBKR_ACCOUNT_FACTS_SYNC_URL,
        ("secrets", "IBKR_ACCOUNT_FACTS_SYNC_TOKEN"): "synthetic-sync-token",
        ("inputs", "account_facts_report_name"): "",
    }
    environment: dict[str, str] = {}
    for line in env_block.splitlines():
        match = re.fullmatch(r"      ([A-Z0-9_]+):\s*(.*?)\s*", line)
        if match is None:
            continue
        key, raw_value = match.groups()
        expression = re.fullmatch(r"\$\{\{\s*(secrets|vars|inputs)\.([A-Za-z0-9_]+)\s*\}\}", raw_value)
        if expression:
            source_name, name = expression.groups()
            if (source_name, name) not in placeholders:
                raise AssertionError(f"unmapped workflow input: {source_name}.{name}")
            value = placeholders[source_name, name]
        elif raw_value == "${{ inputs.account_facts_target || 'primary-live' }}":
            value = "primary-live"
        else:
            value = raw_value.strip("'\"")
        environment[key] = value
    return environment


def test_workflow_exports_publisher_project_and_main_runs_with_only_exported_env():
    environment = _publisher_job_environment()
    assert "IBKR_ACCOUNT_FACTS_PROJECT_ID" in environment

    child = r'''
import contextlib, io, json, os
from datetime import datetime, timedelta, timezone
import scripts.publish_account_facts_from_report as publisher

prefix = os.environ["IBKR_ACCOUNT_FACTS_REPORT_PREFIX"]
now = datetime.now(timezone.utc)
observed = now - timedelta(seconds=30)
selector = json.loads(os.environ["IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON"])
uri = f"{prefix.rstrip('/')}/{now:%Y-%m}/{now:%Y%m%dT%H%M%SZ}.json"
report = {
    "schema_version": "runtime_report.v1",
    "platform": "interactive_brokers",
    "deploy_target": "cloud_run",
    "project_id": os.environ["IBKR_ACCOUNT_FACTS_PROJECT_ID"],
    "service_name": os.environ["IBKR_ACCOUNT_FACTS_SERVICE_NAME"],
    "account_scope": os.environ["IBKR_ACCOUNT_FACTS_ACCOUNT_SCOPE"],
    "runtime_target": {
        "account_selector": selector,
        "deployment_selector": os.environ["IBKR_ACCOUNT_FACTS_DEPLOYMENT_SELECTOR"],
    },
    "diagnostics": {"runtime_revision": os.environ["IBKR_ACCOUNT_FACTS_RUNTIME_REVISION"]},
    "runtime_release_receipt": {"attestation_state": "legacy_unattested"},
    "started_at": (observed - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
    "finished_at": now.isoformat().replace("+00:00", "Z"),
    "summary": {"account_facts": {
        "schema_version": "ibkr_account_snapshot.v1",
        "account_ids": selector,
        "currency": "USD",
        "observed_at": observed.isoformat().replace("+00:00", "Z"),
        "net_assets": "123.45",
        "cash": [{"currency": "USD", "cash_balance": "12.34", "source_tag": "CashBalance"}],
    }},
}
calls = {"listing": 0, "report": 0, "post": 0}
def latest_report_uri(*, prefix, project_id, now):
    calls["listing"] += 1
    assert project_id == os.environ["IBKR_ACCOUNT_FACTS_PROJECT_ID"]
    return uri
def load_report(requested_uri, *, project_id):
    calls["report"] += 1
    assert requested_uri == uri
    assert project_id == os.environ["IBKR_ACCOUNT_FACTS_PROJECT_ID"]
    return report
class Response:
    status = 200
    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def read(self, _limit=-1): return b'{"ok":true,"stored":true,"unchanged":false}'
class Opener:
    def open(self, request, *, timeout):
        calls["post"] += 1
        assert request.method == "POST" and timeout == 15
        return Response()
publisher._latest_report_uri = latest_report_uri
publisher._load_gcs_report = load_report
publisher.build_opener = lambda *_args: Opener()
captured = io.StringIO()
with contextlib.redirect_stdout(captured):
    returncode = publisher.main()
print(json.dumps({"returncode": returncode, "cli": captured.getvalue().strip(), "calls": calls}))
'''
    result = subprocess.run(
        [sys.executable, "-c", child],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    assert outcome == {
        "returncode": 0,
        "cli": "published:unknown",
        "calls": {"listing": 1, "report": 1, "post": 1},
    }


def test_additional_target_runs_in_isolated_steps_with_one_private_publish(tmp_path):
    environment = _publisher_job_environment()
    config = _additional_targets()
    environment.update(
        {
            "IBKR_ACCOUNT_FACTS_TARGET": "additional-2",
            "IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON": json.dumps(config),
            "GITHUB_ENV": str(tmp_path / "github-env"),
            "IBKR_ACCOUNT_FACTS_REPORT_NAME": "20261001T125959Z.json",
        }
    )
    root = Path(__file__).parents[1]
    helper = subprocess.run(
        [sys.executable, "scripts/configure_account_facts_target.py"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert helper.returncode == 0, helper.stderr
    assert helper.stdout == "::add-mask::private-project-2\n"
    exported = {}
    for line in Path(environment["GITHUB_ENV"]).read_text(encoding="utf-8").splitlines():
        key, value = line.split("=", 1)
        exported[key] = value
    assert set(exported) == {"GCP_PROJECT_ID", "IBKR_ACCOUNT_FACTS_PROJECT_ID"}
    assert exported["GCP_PROJECT_ID"] == exported["IBKR_ACCOUNT_FACTS_PROJECT_ID"]
    assert exported["IBKR_ACCOUNT_FACTS_PROJECT_ID"] == "private-project-2"
    assert environment["IBKR_ACCOUNT_FACTS_TARGET_ID"] == "ibkr-example"
    assert environment["IBKR_ACCOUNT_FACTS_REPORT_PREFIX"] == "gs://example-private/ibkr/reports"
    assert environment["IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON"] == '["U00000001"]'
    environment.update(exported)

    child = r'''
import contextlib, io, json, os
from datetime import datetime, timezone
import scripts.publish_account_facts_from_report as publisher

class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc)
publisher.datetime = FrozenDateTime
selected_config = publisher.additional_target_environment(os.environ["IBKR_ACCOUNT_FACTS_TARGET"])
prefix = selected_config["IBKR_ACCOUNT_FACTS_REPORT_PREFIX"]
project = selected_config["IBKR_ACCOUNT_FACTS_PROJECT_ID"]
selector = json.loads(selected_config["IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON"])
report_name = os.environ["IBKR_ACCOUNT_FACTS_REPORT_NAME"]
uri = f"{prefix.rstrip('/')}/2026-10/{report_name}"
report = {
    "schema_version": "runtime_report.v1",
    "platform": "interactive_brokers",
    "deploy_target": "cloud_run",
    "project_id": project,
    "service_name": selected_config["IBKR_ACCOUNT_FACTS_SERVICE_NAME"],
    "account_scope": selected_config["IBKR_ACCOUNT_FACTS_ACCOUNT_SCOPE"],
    "runtime_target": {
        "account_selector": selector,
        "deployment_selector": selected_config["IBKR_ACCOUNT_FACTS_DEPLOYMENT_SELECTOR"],
    },
    "diagnostics": {"runtime_revision": selected_config["IBKR_ACCOUNT_FACTS_RUNTIME_REVISION"]},
    "runtime_release_receipt": {"attestation_state": "legacy_unattested"},
    "started_at": "2026-10-01T12:59:00Z",
    "finished_at": "2026-10-01T13:00:00Z",
    "summary": {"account_facts": {
        "schema_version": "ibkr_account_snapshot.v1",
        "account_ids": selector,
        "currency": "USD",
        "observed_at": "2026-10-01T12:59:30Z",
        "net_assets": "123.45",
        "cash": [{"currency": "USD", "cash_balance": "12.34", "source_tag": "CashBalance"}],
    }},
}
calls = {"listing": 0, "reports": 0, "posts": 0}
def latest_report_uri(**_kwargs):
    calls["listing"] += 1
    raise AssertionError("exact report selection must not list latest reports")
def load_report(requested_uri, *, project_id):
    assert requested_uri == uri
    assert project_id == os.environ["IBKR_ACCOUNT_FACTS_PROJECT_ID"]
    calls["reports"] += 1
    return report
class Response:
    status = 200
    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def read(self, _limit=-1): return b'{"ok":true,"stored":true,"unchanged":false}'
class Opener:
    def open(self, request, *, timeout):
        calls["posts"] += 1
        assert request.method == "POST" and timeout == 15
        return Response()
publisher._latest_report_uri = latest_report_uri
publisher._load_gcs_report = load_report
publisher.build_opener = lambda *_args: Opener()
captured = io.StringIO()
with contextlib.redirect_stdout(captured):
    returncode = publisher.main()
print(json.dumps({"returncode": returncode, "cli": captured.getvalue().strip(), "calls": calls}))
'''
    publisher_run = subprocess.run(
        [sys.executable, "-c", child],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert publisher_run.returncode == 0, publisher_run.stderr
    assert publisher_run.stderr == ""
    result = json.loads(publisher_run.stdout)
    assert result == {
        "returncode": 0,
        "cli": "published:unknown",
        "calls": {
            "listing": 0,
            "reports": 1,
            "posts": 1,
        },
    }
    assert helper.stdout == "::add-mask::private-project-2\n"
    assert "private-project-2" not in publisher_run.stdout
    assert "private-revision-2" not in helper.stdout + publisher_run.stdout


def test_workflow_explicit_report_name_skips_only_manual_heartbeat():
    workflow = Path(__file__).parents[1] / ".github/workflows/execution-report-heartbeat.yml"
    source = workflow.read_text()
    assert "account_facts_report_name:" in source
    assert 'type: string\n        default: ""' in source
    heartbeat_if = source.split("  heartbeat:", 1)[1].split("  account-facts-publisher:", 1)[0]
    assert "github.event_name != 'workflow_dispatch'" in heartbeat_if
    assert "inputs.account_facts_target != 'primary-live'" in heartbeat_if
    assert "inputs.account_facts_report_name == ''" in heartbeat_if
    for slot in ("additional-1", "additional-2", "additional-3"):
        assert f"inputs.account_facts_target != '{slot}'" in heartbeat_if
    publisher_job = source.split("  account-facts-publisher:", 1)[1].split("  account-facts-ingress-diagnostic:", 1)[0]
    assert "github.event_name == 'schedule'" in publisher_job
    assert "inputs.account_facts_target == 'primary-live'" in publisher_job
    for slot in ("additional-1", "additional-2", "additional-3"):
        assert f"inputs.account_facts_target == '{slot}'" in publisher_job
    assert publisher_job.index("Select protected additional target before cloud authentication") < publisher_job.index(
        "google-github-actions/auth@v3"
    )
    assert publisher_job.index("google-github-actions/auth@v3") < publisher_job.index(
        "Publish one validated report"
    )


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
            "gs://bucket/root/interactive_brokers/example-profile/live-primary/2026-09/"
            "20260930T010000Z.json\n"
            "gs://bucket/root/interactive_brokers/example-profile/live-primary-other/2026-09/"
            "20260930T010500Z.json\n"
        )

    monkeypatch.setattr(publisher.subprocess, "run", run)
    uri = publisher._latest_report_uri(
        prefix="gs://bucket/root/interactive_brokers/example-profile/live-primary",
        project_id="project",
        now=datetime(2026, 9, 30, 1, 10, tzinfo=timezone.utc),
    )
    assert uri.endswith("/2026-09/20260930T010000Z.json")
    assert all("live-primary/" in value for value in seen)


def test_named_report_uri_uses_strict_utc_filename_and_protected_prefix():
    now = datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc)
    assert publisher._named_report_uri(
        prefix="gs://example-private/reports/ibkr",
        report_name="20261001T125959Z.json",
        now=now,
    ) == "gs://example-private/reports/ibkr/2026-10/20261001T125959Z.json"

    for report_name in (
        "20260230T120000Z.json",
        "2026101T125959Z.json",
        "20261001T130001Z.json",
        "gs://attacker.example/20261001T125959Z.json",
        "../20261001T125959Z.json",
        "20261001T125959Z.json/extra",
    ):
        with pytest.raises(publisher._ProjectionError):
            publisher._named_report_uri(
                prefix="gs://example-private/reports/ibkr",
                report_name=report_name,
                now=now,
            )


def test_named_report_cli_skips_listing_and_rejects_bad_or_future_names_before_post(
    monkeypatch, capsys
):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(publisher, "datetime", FrozenDateTime)
    for name, reason in (
        ("20260230T120000Z.json", "report_name_invalid"),
        ("20261001T130001Z.json", "report_name_future"),
        ("gs://attacker.example/report.json", "report_name_invalid"),
    ):
        monkeypatch.setenv("IBKR_ACCOUNT_FACTS_TARGET", publisher.IBKR_ACCOUNT_FACTS_PRIMARY_TARGET)
        monkeypatch.setenv("IBKR_ACCOUNT_FACTS_REPORT_PREFIX", "gs://example-private/reports/ibkr")
        monkeypatch.setenv("IBKR_ACCOUNT_FACTS_TARGET_ID", "ibkr-primary")
        monkeypatch.setenv("IBKR_ACCOUNT_FACTS_PROJECT_ID", "example-project")
        monkeypatch.setenv("IBKR_ACCOUNT_FACTS_SERVICE_NAME", "ibkr-primary-service")
        monkeypatch.setenv("IBKR_ACCOUNT_FACTS_RUNTIME_REVISION", "runtime-revision-001")
        monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ACCOUNT_SCOPE", "live-primary")
        monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON", '["U00000001"]')
        monkeypatch.setenv("IBKR_ACCOUNT_FACTS_DEPLOYMENT_SELECTOR", "live-primary")
        monkeypatch.setenv("IBKR_ACCOUNT_FACTS_REPORT_NAME", name)
        monkeypatch.setattr(
            publisher,
            "_latest_report_uri",
            lambda **_kwargs: pytest.fail("explicit report selection must not list latest reports"),
        )
        monkeypatch.setattr(
            publisher,
            "_load_gcs_report",
            lambda *_args, **_kwargs: pytest.fail("invalid report name must stop before GCS read"),
        )
        monkeypatch.setattr(
            publisher,
            "publish_ibkr_account_facts_history",
            lambda *_args, **_kwargs: pytest.fail("invalid report name must stop before POST"),
        )

        assert publisher.main() == 1
        assert capsys.readouterr().out.strip() == f"skipped:{reason}"


def test_named_report_cli_loads_only_exact_prefix_derived_object(monkeypatch, capsys):
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_TARGET", publisher.IBKR_ACCOUNT_FACTS_PRIMARY_TARGET)
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_REPORT_PREFIX", "gs://example-private/reports/ibkr")
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_TARGET_ID", "ibkr-primary")
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_PROJECT_ID", "example-project")
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_SERVICE_NAME", "ibkr-primary-service")
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_RUNTIME_REVISION", "runtime-revision-001")
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ACCOUNT_SCOPE", "live-primary")
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON", '["U00000001"]')
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_DEPLOYMENT_SELECTOR", "live-primary")
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_REPORT_NAME", "20261001T125959Z.json")
    selected = []
    monkeypatch.setattr(
        publisher,
        "_latest_report_uri",
        lambda **_kwargs: pytest.fail("explicit report selection must not list latest reports"),
    )
    monkeypatch.setattr(
        publisher,
        "_load_gcs_report",
        lambda uri, **_kwargs: selected.append(uri) or _report(),
    )
    monkeypatch.setattr(
        publisher,
        "publish_ibkr_account_facts_history",
        lambda _report, **kwargs: {"status": "published"},
    )

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(publisher, "datetime", FrozenDateTime)
    assert publisher.main() == 0
    assert selected == ["gs://example-private/reports/ibkr/2026-10/20261001T125959Z.json"]
    assert capsys.readouterr().out.strip() == "published:unknown"


def test_empty_named_report_preserves_latest_report_selection(monkeypatch, capsys):
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_TARGET", publisher.IBKR_ACCOUNT_FACTS_PRIMARY_TARGET)
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_REPORT_PREFIX", "gs://example-private/reports/ibkr")
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_TARGET_ID", "ibkr-primary")
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_PROJECT_ID", "example-project")
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_SERVICE_NAME", "ibkr-primary-service")
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_RUNTIME_REVISION", "runtime-revision-001")
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ACCOUNT_SCOPE", "live-primary")
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON", '["U00000001"]')
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_DEPLOYMENT_SELECTOR", "live-primary")
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_REPORT_NAME", "")
    latest = []
    loaded = []
    monkeypatch.setattr(
        publisher,
        "_latest_report_uri",
        lambda **_kwargs: latest.append(True) or "gs://example-private/reports/ibkr/2026-10/20261001T120000Z.json",
    )
    monkeypatch.setattr(
        publisher,
        "_load_gcs_report",
        lambda uri, **_kwargs: loaded.append(uri) or _report(),
    )
    monkeypatch.setattr(
        publisher,
        "publish_ibkr_account_facts_history",
        lambda _report, **_kwargs: {"status": "published"},
    )
    assert publisher.main() == 0
    assert latest == [True]
    assert loaded == ["gs://example-private/reports/ibkr/2026-10/20261001T120000Z.json"]
    assert capsys.readouterr().out.strip() == "published:unknown"
