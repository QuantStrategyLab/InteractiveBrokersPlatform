"""Emit path: local synthetic report only; no cloud, no real accounts."""

from __future__ import annotations

import os

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import emit_digest_candidates as emit  # noqa: E402
from scripts.project_digest_candidates import SCHEMA_VERSION  # noqa: E402


def _report() -> dict:
    return {
        "schema_version": "runtime_report.v1",
        "platform": "interactive_brokers",
        "strategy_profile": "global_etf_rotation",
        "status": "ok",
        "started_at": "2026-10-08T20:01:00Z",
        "finished_at": "2026-10-08T20:02:00Z",
        "execution_receipt": {"outcome": "no_signal"},
        "summary": {},
    }


def test_emit_writes_ephemeral_candidates(tmp_path):
    report_path = tmp_path / "report.json"
    out = tmp_path / "candidates.json"
    report_path.write_text(json.dumps(_report()), encoding="utf-8")
    environ = {
        "IBKR_DIGEST_CANDIDATES_OUTPUT_PATH": str(out),
        "IBKR_DIGEST_RUNTIME_REPORT_PATH": str(report_path),
        "IBKR_DIGEST_OPAQUE_ACCOUNT_UID": "acct_opaque_synthetic",
        "IBKR_DIGEST_TARGET_ID": "ibkr/synthetic-target",
    }
    result = emit.emit_digest_candidates(environ)
    assert result["status"] == "candidates_written"
    assert result["runs"] == 1
    assert result["identity_uid_present"] is True
    assert result["identity_target_present"] is True
    dumped = json.dumps(result)
    assert "acct_opaque" not in dumped
    assert "12345" not in dumped
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["runs"][0]["fill_count"] is None
    assert data["runs"][0]["platform_id"] == "ibkr"
    assert data["runs"][0]["opaque_account_uid"] == "acct_opaque_synthetic"


def test_emit_requires_output_path():
    assert emit.emit_digest_candidates({}) == {
        "status": "skipped",
        "reason": "output_path_missing",
    }


def test_emit_cli_rejects_args(capsys):
    assert emit.main(["--help"]) == 2
    assert "unsupported_arguments" in capsys.readouterr().out


def test_emit_uses_injected_loader(tmp_path):
    out = tmp_path / "candidates.json"
    environ = {
        "IBKR_DIGEST_CANDIDATES_OUTPUT_PATH": str(out),
        "IBKR_DIGEST_OPAQUE_ACCOUNT_UID": "acct_opaque_synthetic",
        "IBKR_DIGEST_TARGET_ID": "ibkr/synthetic-target",
    }
    result = emit.emit_digest_candidates(
        environ,
        report_loader=lambda _env: _report(),
    )
    assert result["status"] == "candidates_written"
    assert result["producer_status"] == "projected"


def test_emit_resolves_labels_from_cloud_run_targets(tmp_path):
    report_path = tmp_path / "report.json"
    out = tmp_path / "candidates.json"
    # Report without selector — force CLOUD_RUN match via service name.
    body = _report()
    body["service_name"] = "interactive-brokers-quant-live-u15998061-service"
    body.pop("account_scope", None)
    body["runtime_target"] = {
        "strategy_profile": "soxl_soxx_trend_income",
        "service_name": "interactive-brokers-quant-live-u15998061-service",
        "deployment_selector": "live-u15998061",
    }
    report_path.write_text(json.dumps(body), encoding="utf-8")
    targets = {
        "targets": [
            {
                "service_name": "interactive-brokers-quant-live-u15998061-service",
                "runtime_target": {
                    "account_scope": "live-u15998061",
                    "account_selector": ["U15998061"],
                    "deployment_selector": "live-u15998061",
                    "service_name": "interactive-brokers-quant-live-u15998061-service",
                    "strategy_profile": "soxl_soxx_trend_income",
                    "execution_mode": "live",
                },
            },
            {
                "service_name": "interactive-brokers-quant-live-u16608560-service",
                "runtime_target": {
                    "account_scope": "live-u16608560",
                    "account_selector": ["U16608560"],
                    "deployment_selector": "live-u16608560",
                    "service_name": "interactive-brokers-quant-live-u16608560-service",
                    "strategy_profile": "tqqq_growth_income",
                    "execution_mode": "live",
                },
            },
        ]
    }
    environ = {
        "IBKR_DIGEST_CANDIDATES_OUTPUT_PATH": str(out),
        "IBKR_DIGEST_RUNTIME_REPORT_PATH": str(report_path),
        "IBKR_DIGEST_OPAQUE_ACCOUNT_UID": "acct_opaque_synthetic",
        "IBKR_DIGEST_TARGET_ID": "ibkr/synthetic-target",
        "CLOUD_RUN_SERVICE_TARGETS_JSON": json.dumps(targets),
    }
    result = emit.emit_digest_candidates(environ)
    assert result["status"] == "candidates_written"
    assert result["account_hint_present"] is True
    assert result["account_scope_present"] is True
    # Safe summary must not echo the U####### value.
    dumped = json.dumps(result)
    assert "U15998061" not in dumped
    data = json.loads(out.read_text(encoding="utf-8"))
    row = data["runs"][0]
    assert row["account_hint"] == "U15998061"
    assert row["account_scope"] == "live-u15998061"


def test_emit_explicit_digest_hint_overrides(tmp_path):
    out = tmp_path / "candidates.json"
    environ = {
        "IBKR_DIGEST_CANDIDATES_OUTPUT_PATH": str(out),
        "IBKR_DIGEST_OPAQUE_ACCOUNT_UID": "acct_opaque_synthetic",
        "IBKR_DIGEST_TARGET_ID": "ibkr/synthetic-target",
        "IBKR_DIGEST_ACCOUNT_HINT": "U18308207",
        "IBKR_DIGEST_ACCOUNT_SCOPE": "live-u18308207",
    }
    result = emit.emit_digest_candidates(
        environ,
        report_loader=lambda _env: _report(),
    )
    assert result["account_hint_present"] is True
    row = json.loads(out.read_text(encoding="utf-8"))["runs"][0]
    # Explicit override wins over report fixture U00000001.
    assert row["account_hint"] == "U18308207"
    assert row["account_scope"] == "live-u18308207"

def test_emit_projects_equity_from_report_when_target_matches(tmp_path, monkeypatch):
    """Read-only report projection supplies equity; safe summary stays amount-free."""
    report_path = tmp_path / "report.json"
    out = tmp_path / "candidates.json"
    facts_out = tmp_path / "facts.json"
    body = {
        "schema_version": "runtime_report.v1",
        "platform": "interactive_brokers",
        "deploy_target": "cloud_run",
        "project_id": "interactivebrokersquant",
        "service_name": "interactive-brokers-quant-live-u15998061-service",
        "account_scope": "live-u15998061",
        "strategy_profile": "soxl_soxx_trend_income",
        "status": "ok",
        "started_at": "2026-10-08T20:01:00Z",
        "finished_at": "2026-10-08T20:05:00Z",
        "execution_receipt": {"outcome": "no_signal"},
        "diagnostics": {"runtime_revision": "rev-digest-test"},
        "runtime_target": {
            "account_selector": ["U15998061"],
            "deployment_selector": "live-u15998061",
            "strategy_profile": "soxl_soxx_trend_income",
        },
        "summary": {
            "account_facts": {
                "schema_version": "ibkr_account_snapshot.v1",
                "account_ids": ["U15998061"],
                "currency": "USD",
                "net_assets": "569.1600",
                "observed_at": "2026-10-08T20:03:00Z",
                "cash": [
                    {"currency": "USD", "cash_balance": "100.00", "source_tag": "$LEDGER-CashBalance"},
                ],
            }
        },
    }
    report_path.write_text(json.dumps(body), encoding="utf-8")
    prefix = "gs://ibkr-runtime-reports-test/prefix"
    environ = {
        "IBKR_DIGEST_CANDIDATES_OUTPUT_PATH": str(out),
        "IBKR_DIGEST_ACCOUNT_FACTS_OUTPUT_PATH": str(facts_out),
        "IBKR_DIGEST_RUNTIME_REPORT_PATH": str(report_path),
        "IBKR_DIGEST_SOURCE_REPORT_URI": f"{prefix}/2026/10/08/report.json",
        "IBKR_DIGEST_OPAQUE_ACCOUNT_UID": "acct_opaque_synthetic",
        "IBKR_DIGEST_TARGET_ID": "ibkr-primary-u15998061",
        "IBKR_ACCOUNT_FACTS_REPORT_PREFIX": prefix,
        "IBKR_ACCOUNT_FACTS_PROJECT_ID": "interactivebrokersquant",
        "IBKR_ACCOUNT_FACTS_SERVICE_NAME": "interactive-brokers-quant-live-u15998061-service",
        "IBKR_ACCOUNT_FACTS_RUNTIME_REVISION": "rev-digest-test",
        "IBKR_ACCOUNT_FACTS_ACCOUNT_SCOPE": "live-u15998061",
        "IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON": '["U15998061"]',
        "IBKR_ACCOUNT_FACTS_DEPLOYMENT_SELECTOR": "live-u15998061",
    }
    result = emit.emit_digest_candidates(environ)
    assert result["status"] == "candidates_written"
    assert result["equity_present"] is True
    assert result["account_facts_source"] == "report_projection"
    assert result["account_facts_ephemeral_written"] is True
    assert "569.16" not in json.dumps(result)
    row = json.loads(out.read_text(encoding="utf-8"))["runs"][0]
    assert row["equity"] == 569.16
    assert row["account_hint"] == "U15998061"
    facts = json.loads(facts_out.read_text(encoding="utf-8"))
    assert facts["broker_reported_balances"][0]["net_assets"] == "569.1600"


def test_emit_skips_equity_when_report_projection_mismatches(tmp_path):
    report_path = tmp_path / "report.json"
    out = tmp_path / "candidates.json"
    report_path.write_text(json.dumps(_report()), encoding="utf-8")
    environ = {
        "IBKR_DIGEST_CANDIDATES_OUTPUT_PATH": str(out),
        "IBKR_DIGEST_RUNTIME_REPORT_PATH": str(report_path),
        "IBKR_DIGEST_SOURCE_REPORT_URI": "gs://ibkr-runtime-reports-test/prefix/x.json",
        "IBKR_DIGEST_OPAQUE_ACCOUNT_UID": "acct_opaque_synthetic",
        "IBKR_DIGEST_TARGET_ID": "ibkr/synthetic-target",
        "IBKR_ACCOUNT_FACTS_REPORT_PREFIX": "gs://ibkr-runtime-reports-test/prefix",
        "IBKR_ACCOUNT_FACTS_PROJECT_ID": "interactivebrokersquant",
        "IBKR_ACCOUNT_FACTS_SERVICE_NAME": "missing-service",
        "IBKR_ACCOUNT_FACTS_RUNTIME_REVISION": "rev-x",
        "IBKR_ACCOUNT_FACTS_ACCOUNT_SCOPE": "live-u15998061",
        "IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON": '["U15998061"]',
        "IBKR_ACCOUNT_FACTS_DEPLOYMENT_SELECTOR": "live-u15998061",
    }
    result = emit.emit_digest_candidates(environ)
    assert result["status"] == "candidates_written"
    assert result["equity_present"] is False
    assert "equity" not in json.loads(out.read_text(encoding="utf-8"))["runs"][0]



def test_apply_additional_account_facts_target_loads_slot(monkeypatch):
    """CLI must apply full additional_* identity (publisher parity), not project-only."""
    import json

    from scripts import publish_account_facts_from_report as publisher

    primary = {
        "IBKR_ACCOUNT_FACTS_TARGET_ID": "ibkr-primary",
        "IBKR_ACCOUNT_FACTS_REPORT_PREFIX": "gs://primary-bucket/reports",
        "IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON": json.dumps(["U16608560"]),
        "IBKR_ACCOUNT_FACTS_ACCOUNT_SCOPE": "live-u16608560",
        "IBKR_ACCOUNT_FACTS_SERVICE_NAME": "interactive-brokers-quant-live-u16608560-service",
        "IBKR_ACCOUNT_FACTS_RUNTIME_REVISION": "rev-primary",
        "IBKR_ACCOUNT_FACTS_DEPLOYMENT_SELECTOR": "live-u16608560",
        "IBKR_ACCOUNT_FACTS_PROJECT_ID": "primary-project",
        "GCP_PROJECT_ID": "primary-project",
    }
    for key, value in primary.items():
        monkeypatch.setenv(key, value)

    def _slot(account: str, *, bucket: str) -> dict:
        lower = account.lower()
        return {
            "project_id": "extra-project",
            "report_prefix": f"gs://{bucket}/reports",
            "target_id": f"ibkr-{lower}",
            "service_name": f"interactive-brokers-quant-live-{lower}-service",
            "runtime_revision": f"rev-{lower}",
            "account_scope": f"live-{lower}",
            "account_selector": [account],
            "deployment_selector": f"live-{lower}",
        }

    monkeypatch.setenv(
        publisher.IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON_ENV,
        json.dumps(
            {
                "additional-1": _slot("U15998061", bucket="extra-bucket-a"),
                "additional-2": _slot("U18308207", bucket="extra-bucket-b"),
                "additional-3": _slot("U18336562", bucket="extra-bucket-c"),
            }
        ),
    )
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_TARGET", "additional-1")
    emit._apply_additional_account_facts_target(os.environ)  # type: ignore[arg-type]
    assert os.environ["IBKR_ACCOUNT_FACTS_TARGET_ID"] == "ibkr-u15998061"
    assert os.environ["IBKR_ACCOUNT_FACTS_ACCOUNT_SCOPE"] == "live-u15998061"
    assert "U15998061" in os.environ["IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON"]
    assert os.environ["IBKR_ACCOUNT_FACTS_REPORT_PREFIX"] == "gs://extra-bucket-a/reports"
