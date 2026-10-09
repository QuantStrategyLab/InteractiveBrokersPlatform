"""Emit path: local synthetic report only; no cloud, no real accounts."""

from __future__ import annotations

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
