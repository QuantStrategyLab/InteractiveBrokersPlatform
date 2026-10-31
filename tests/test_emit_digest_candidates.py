"""Emit path: local synthetic report only; no cloud, no real accounts."""

from __future__ import annotations

import os

import json
import sys
from pathlib import Path

import pytest

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
    body["service_name"] = "interactive-brokers-quant-live-u00000004-service"
    body.pop("account_scope", None)
    body["runtime_target"] = {
        "strategy_profile": "soxl_soxx_trend_income",
        "service_name": "interactive-brokers-quant-live-u00000004-service",
        "deployment_selector": "live-u00000004",
    }
    report_path.write_text(json.dumps(body), encoding="utf-8")
    targets = {
        "targets": [
            {
                "service_name": "interactive-brokers-quant-live-u00000004-service",
                "runtime_target": {
                    "account_scope": "live-u00000004",
                    "account_selector": ["U00000004"],
                    "deployment_selector": "live-u00000004",
                    "service_name": "interactive-brokers-quant-live-u00000004-service",
                    "strategy_profile": "soxl_soxx_trend_income",
                    "execution_mode": "live",
                },
            },
            {
                "service_name": "interactive-brokers-quant-live-u00000006-service",
                "runtime_target": {
                    "account_scope": "live-u00000006",
                    "account_selector": ["U00000006"],
                    "deployment_selector": "live-u00000006",
                    "service_name": "interactive-brokers-quant-live-u00000006-service",
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
    assert "U00000004" not in dumped
    data = json.loads(out.read_text(encoding="utf-8"))
    row = data["runs"][0]
    assert row["account_hint"] == "U00000004"
    assert row["account_scope"] == "live-u00000004"


def test_emit_explicit_digest_hint_overrides(tmp_path):
    out = tmp_path / "candidates.json"
    environ = {
        "IBKR_DIGEST_CANDIDATES_OUTPUT_PATH": str(out),
        "IBKR_DIGEST_OPAQUE_ACCOUNT_UID": "acct_opaque_synthetic",
        "IBKR_DIGEST_TARGET_ID": "ibkr/synthetic-target",
        "IBKR_DIGEST_ACCOUNT_HINT": "U00000007",
        "IBKR_DIGEST_ACCOUNT_SCOPE": "live-u00000007",
    }
    result = emit.emit_digest_candidates(
        environ,
        report_loader=lambda _env: _report(),
    )
    assert result["account_hint_present"] is True
    row = json.loads(out.read_text(encoding="utf-8"))["runs"][0]
    # Explicit override wins over report fixture U00000001.
    assert row["account_hint"] == "U00000007"
    assert row["account_scope"] == "live-u00000007"

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
        "service_name": "interactive-brokers-quant-live-u00000004-service",
        "account_scope": "live-u00000004",
        "strategy_profile": "soxl_soxx_trend_income",
        "status": "ok",
        "started_at": "2026-10-08T20:01:00Z",
        "finished_at": "2026-10-08T20:05:00Z",
        "execution_receipt": {"outcome": "no_signal"},
        "diagnostics": {"runtime_revision": "rev-digest-test"},
        "runtime_target": {
            "account_selector": ["U00000004"],
            "deployment_selector": "live-u00000004",
            "strategy_profile": "soxl_soxx_trend_income",
        },
        "summary": {
            "account_facts": {
                "schema_version": "ibkr_account_snapshot.v1",
                "account_ids": ["U00000004"],
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
    prefix = "gs://synthetic-runtime-reports/prefix"
    environ = {
        "IBKR_DIGEST_CANDIDATES_OUTPUT_PATH": str(out),
        "IBKR_DIGEST_ACCOUNT_FACTS_OUTPUT_PATH": str(facts_out),
        "IBKR_DIGEST_RUNTIME_REPORT_PATH": str(report_path),
        "IBKR_DIGEST_SOURCE_REPORT_URI": f"{prefix}/2026/10/08/report.json",
        "IBKR_DIGEST_OPAQUE_ACCOUNT_UID": "acct_opaque_synthetic",
        "IBKR_DIGEST_TARGET_ID": "ibkr-primary-u00000004",
        "IBKR_ACCOUNT_FACTS_REPORT_PREFIX": prefix,
        "IBKR_ACCOUNT_FACTS_PROJECT_ID": "interactivebrokersquant",
        "IBKR_ACCOUNT_FACTS_SERVICE_NAME": "interactive-brokers-quant-live-u00000004-service",
        "IBKR_ACCOUNT_FACTS_RUNTIME_REVISION": "rev-digest-test",
        "IBKR_ACCOUNT_FACTS_ACCOUNT_SCOPE": "live-u00000004",
        "IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON": '["U00000004"]',
        "IBKR_ACCOUNT_FACTS_DEPLOYMENT_SELECTOR": "live-u00000004",
    }
    result = emit.emit_digest_candidates(environ)
    assert result["status"] == "candidates_written"
    assert result["equity_present"] is True
    assert result["account_facts_source"] == "report_projection"
    assert result["account_facts_ephemeral_written"] is True
    assert "569.16" not in json.dumps(result)
    row = json.loads(out.read_text(encoding="utf-8"))["runs"][0]
    assert row["equity"] == 569.16
    assert row["account_hint"] == "U00000004"
    facts = json.loads(facts_out.read_text(encoding="utf-8"))
    assert facts["broker_reported_balances"][0]["net_assets"] == "569.1600"


def test_emit_skips_equity_when_report_projection_mismatches(tmp_path):
    report_path = tmp_path / "report.json"
    out = tmp_path / "candidates.json"
    report_path.write_text(json.dumps(_report()), encoding="utf-8")
    environ = {
        "IBKR_DIGEST_CANDIDATES_OUTPUT_PATH": str(out),
        "IBKR_DIGEST_RUNTIME_REPORT_PATH": str(report_path),
        "IBKR_DIGEST_SOURCE_REPORT_URI": "gs://synthetic-runtime-reports/prefix/x.json",
        "IBKR_DIGEST_OPAQUE_ACCOUNT_UID": "acct_opaque_synthetic",
        "IBKR_DIGEST_TARGET_ID": "ibkr/synthetic-target",
        "IBKR_ACCOUNT_FACTS_REPORT_PREFIX": "gs://synthetic-runtime-reports/prefix",
        "IBKR_ACCOUNT_FACTS_PROJECT_ID": "interactivebrokersquant",
        "IBKR_ACCOUNT_FACTS_SERVICE_NAME": "missing-service",
        "IBKR_ACCOUNT_FACTS_RUNTIME_REVISION": "rev-x",
        "IBKR_ACCOUNT_FACTS_ACCOUNT_SCOPE": "live-u00000004",
        "IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON": '["U00000004"]',
        "IBKR_ACCOUNT_FACTS_DEPLOYMENT_SELECTOR": "live-u00000004",
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
        "IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON": json.dumps(["U00000006"]),
        "IBKR_ACCOUNT_FACTS_ACCOUNT_SCOPE": "live-u00000006",
        "IBKR_ACCOUNT_FACTS_SERVICE_NAME": "interactive-brokers-quant-live-u00000006-service",
        "IBKR_ACCOUNT_FACTS_RUNTIME_REVISION": "rev-primary",
        "IBKR_ACCOUNT_FACTS_DEPLOYMENT_SELECTOR": "live-u00000006",
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
                "additional-1": _slot("U00000004", bucket="extra-bucket-a"),
                "additional-2": _slot("U00000007", bucket="extra-bucket-b"),
                "additional-3": _slot("U00000008", bucket="extra-bucket-c"),
            }
        ),
    )
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_TARGET", "additional-1")
    emit._apply_additional_account_facts_target(os.environ)  # type: ignore[arg-type]
    assert os.environ["IBKR_ACCOUNT_FACTS_TARGET_ID"] == "ibkr-u00000004"
    assert os.environ["IBKR_ACCOUNT_FACTS_ACCOUNT_SCOPE"] == "live-u00000004"
    assert "U00000004" in os.environ["IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON"]
    assert os.environ["IBKR_ACCOUNT_FACTS_REPORT_PREFIX"] == "gs://extra-bucket-a/reports"


def _synthetic_additional_targets():
    return {
        f"additional-{index}": {
            "project_id": "synthetic-project",
            "report_prefix": f"gs://synthetic-extra-{index}/reports",
            "target_id": f"ibkr-synthetic-extra-{index}",
            "service_name": f"synthetic-service-{index}",
            "runtime_revision": f"synthetic-revision-{index}",
            "account_scope": f"synthetic-scope-{index}",
            "account_selector": [f"U0000001{index}"],
            "deployment_selector": f"synthetic-deployment-{index}",
        }
        for index in range(1, 4)
    }


@pytest.mark.parametrize("target", ["additional-1", "additional-2", "additional-3"])
@pytest.mark.parametrize("failure", ["missing", "malformed", "primary_collision"])
def test_emit_cli_rejects_invalid_additional_config_before_loading_primary_report(
    monkeypatch, tmp_path, capsys, target, failure
):
    output = tmp_path / "candidates.json"
    primary = {
        "IBKR_ACCOUNT_FACTS_TARGET": target,
        "IBKR_ACCOUNT_FACTS_TARGET_ID": "ibkr-synthetic-primary",
        "IBKR_ACCOUNT_FACTS_REPORT_PREFIX": "gs://synthetic-primary/reports",
        "IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON": '["U00000001"]',
        "IBKR_ACCOUNT_FACTS_ACCOUNT_SCOPE": "synthetic-primary-scope",
        "IBKR_DIGEST_CANDIDATES_OUTPUT_PATH": str(output),
        "IBKR_DIGEST_OPAQUE_ACCOUNT_UID": "acct_opaque_synthetic",
    }
    for key, value in primary.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("IBKR_DIGEST_TARGET_ID", raising=False)
    monkeypatch.delenv("IBKR_DIGEST_ACCOUNT_FACTS_PATH", raising=False)
    if failure == "missing":
        monkeypatch.delenv("IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON", raising=False)
    elif failure == "malformed":
        monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON", "{broken")
    else:
        config = _synthetic_additional_targets()
        config[target]["account_selector"] = ["U00000001"]
        monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON", json.dumps(config))

    loaded = []
    monkeypatch.setattr(emit, "_load_local_report", lambda _env: loaded.append(True) or _report())

    assert emit.main([]) == 2
    assert loaded == []
    assert not output.exists()
    assert json.loads(capsys.readouterr().out) == {
        "status": "skipped",
        "reason": "target_config_unavailable" if failure == "missing" else "target_config_invalid",
    }


@pytest.mark.parametrize("target", ["additional-1", "additional-2", "additional-3"])
def test_emit_cli_valid_additional_config_uses_selected_slot(monkeypatch, tmp_path, target):
    config = _synthetic_additional_targets()
    selected = config[target]
    output = tmp_path / "candidates.json"
    for key, value in {
        "IBKR_ACCOUNT_FACTS_TARGET": target,
        "IBKR_ACCOUNT_FACTS_TARGET_ID": "ibkr-synthetic-primary",
        "IBKR_ACCOUNT_FACTS_REPORT_PREFIX": "gs://synthetic-primary/reports",
        "IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON": '["U00000001"]',
        "IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON": json.dumps(config),
        "IBKR_DIGEST_CANDIDATES_OUTPUT_PATH": str(output),
        "IBKR_DIGEST_OPAQUE_ACCOUNT_UID": "acct_opaque_synthetic",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("IBKR_DIGEST_TARGET_ID", raising=False)
    monkeypatch.delenv("IBKR_DIGEST_ACCOUNT_HINT", raising=False)
    monkeypatch.delenv("IBKR_DIGEST_ACCOUNT_SCOPE", raising=False)
    monkeypatch.delenv("IBKR_DIGEST_ACCOUNT_FACTS_PATH", raising=False)

    loaded = []

    def load_report(env):
        loaded.append((env["IBKR_ACCOUNT_FACTS_REPORT_PREFIX"], env["IBKR_ACCOUNT_FACTS_TARGET_ID"]))
        return _report()

    monkeypatch.setattr(emit, "_load_local_report", load_report)

    assert emit.main([]) == 0
    assert loaded == [(selected["report_prefix"], selected["target_id"])]
    row = json.loads(output.read_text())["runs"][0]
    assert row["target_id"] == selected["target_id"]
    assert row["account_scope"] == selected["account_scope"]
    assert row["account_hint"] == selected["account_selector"][0]


@pytest.mark.parametrize(
    "target", ["additional-4", "additonal-1", "primary-lvie", "disabled", "ingress-diagnostic"]
)
def test_emit_cli_rejects_unknown_or_nonproducer_target_before_loading_report(
    monkeypatch, tmp_path, capsys, target
):
    output = tmp_path / "candidates.json"
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_TARGET", target)
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_TARGET_ID", "ibkr-synthetic-primary")
    monkeypatch.setenv("IBKR_DIGEST_CANDIDATES_OUTPUT_PATH", str(output))
    monkeypatch.setenv("IBKR_DIGEST_OPAQUE_ACCOUNT_UID", "acct_opaque_synthetic")
    monkeypatch.delenv("IBKR_DIGEST_TARGET_ID", raising=False)
    monkeypatch.delenv("IBKR_DIGEST_ACCOUNT_FACTS_PATH", raising=False)
    loaded = []
    monkeypatch.setattr(emit, "_load_local_report", lambda _env: loaded.append(True) or _report())

    assert emit.main([]) == 2
    assert loaded == []
    assert not output.exists()
    assert json.loads(capsys.readouterr().out) == {
        "status": "skipped", "reason": "target_disabled"
    }


@pytest.mark.parametrize("target", [None, "", "primary-live"])
def test_emit_cli_preserves_primary_and_unset_target_default(monkeypatch, tmp_path, target):
    output = tmp_path / "candidates.json"
    if target is None:
        monkeypatch.delenv("IBKR_ACCOUNT_FACTS_TARGET", raising=False)
    else:
        monkeypatch.setenv("IBKR_ACCOUNT_FACTS_TARGET", target)
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_TARGET_ID", "ibkr-synthetic-primary")
    monkeypatch.setenv("IBKR_DIGEST_CANDIDATES_OUTPUT_PATH", str(output))
    monkeypatch.setenv("IBKR_DIGEST_OPAQUE_ACCOUNT_UID", "acct_opaque_synthetic")
    monkeypatch.delenv("IBKR_DIGEST_TARGET_ID", raising=False)
    monkeypatch.delenv("IBKR_DIGEST_ACCOUNT_FACTS_PATH", raising=False)
    monkeypatch.setattr(emit, "_load_local_report", lambda _env: _report())

    assert emit.main([]) == 0
    assert json.loads(output.read_text())["runs"][0]["target_id"] == "ibkr-synthetic-primary"
