"""Write ephemeral IBKR DIGEST_CANDIDATES from a local runtime_report JSON.

Does not publish to QRS, does not upload artifacts, and never prints equity /
opaque uid / target_id values.

Primary path (unit-testable, no QPK / GCS):
  - IBKR_DIGEST_RUNTIME_REPORT_PATH — local runtime_report.v1 JSON
  - optional IBKR_DIGEST_ACCOUNT_FACTS_PATH — local account-facts JSON
  - IBKR_DIGEST_CANDIDATES_OUTPUT_PATH — required output path

Identity from protected env:
  - IBKR_DIGEST_OPAQUE_ACCOUNT_UID (preferred)
  - IBKR_DIGEST_TARGET_ID or IBKR_ACCOUNT_FACTS_TARGET_ID

Optional GCS path (workflow only; skipped when local path is set):
  reuses publish_account_facts_from_report listing/load helpers when
  IBKR_ACCOUNT_FACTS_REPORT_PREFIX and project id are configured. That path
  needs gcloud; tests must not rely on it.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.project_digest_candidates import (
    load_json_object,
    project_digest_candidates,
)


def _safe_identity(environ: Mapping[str, str]) -> tuple[str, str]:
    uid = environ.get("IBKR_DIGEST_OPAQUE_ACCOUNT_UID") or ""
    tid = (
        environ.get("IBKR_DIGEST_TARGET_ID")
        or environ.get("IBKR_ACCOUNT_FACTS_TARGET_ID")
        or ""
    )
    if not isinstance(uid, str):
        uid = ""
    if not isinstance(tid, str):
        tid = ""
    return uid.strip(), tid.strip()


def _load_local_report(environ: Mapping[str, str]) -> dict[str, Any] | None:
    raw = environ.get("IBKR_DIGEST_RUNTIME_REPORT_PATH")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return load_json_object(Path(raw.strip()))


def _load_gcs_report_via_publisher(environ: Mapping[str, str]) -> dict[str, Any] | None:
    """Best-effort latest report from account-facts prefix; no POST."""
    prefix = environ.get("IBKR_ACCOUNT_FACTS_REPORT_PREFIX")
    project_id = environ.get("IBKR_ACCOUNT_FACTS_PROJECT_ID") or environ.get(
        "GCP_PROJECT_ID"
    )
    if not isinstance(prefix, str) or not prefix.strip():
        return None
    if not isinstance(project_id, str) or not project_id.strip():
        return None
    try:
        from datetime import datetime, timezone

        from scripts import publish_account_facts_from_report as publisher
    except Exception:
        return None
    try:
        now = datetime.now(timezone.utc)
        report_name = environ.get("IBKR_ACCOUNT_FACTS_REPORT_NAME", "") or ""
        if isinstance(report_name, str) and report_name.strip():
            uri = publisher._named_report_uri(
                prefix=prefix.strip(),
                report_name=report_name.strip(),
                now=now,
            )
        else:
            uri = publisher._latest_report_uri(
                prefix=prefix.strip(),
                project_id=project_id.strip(),
                now=now,
            )
        return publisher._load_gcs_report(uri, project_id=project_id.strip())
    except Exception:
        return None


def emit_digest_candidates(
    environ: Mapping[str, str],
    *,
    report_loader: Callable[[Mapping[str, str]], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    output_raw = environ.get("IBKR_DIGEST_CANDIDATES_OUTPUT_PATH")
    if not isinstance(output_raw, str) or not output_raw.strip():
        return {"status": "skipped", "reason": "output_path_missing"}
    output_path = Path(output_raw.strip())

    try:
        if report_loader is not None:
            report = report_loader(environ)
        else:
            report = _load_local_report(environ)
            if report is None:
                report = _load_gcs_report_via_publisher(environ)
    except (OSError, ValueError, json.JSONDecodeError):
        return {"status": "skipped", "reason": "runtime_report_unreadable"}
    except Exception:
        return {"status": "skipped", "reason": "runtime_report_unavailable"}

    if not isinstance(report, dict):
        return {"status": "skipped", "reason": "runtime_report_missing"}

    account_facts = None
    facts_path_raw = environ.get("IBKR_DIGEST_ACCOUNT_FACTS_PATH")
    if isinstance(facts_path_raw, str) and facts_path_raw.strip():
        try:
            account_facts = load_json_object(Path(facts_path_raw.strip()))
        except (OSError, ValueError, json.JSONDecodeError):
            return {"status": "skipped", "reason": "account_facts_unreadable"}

    uid, tid = _safe_identity(environ)
    business_day = environ.get("IBKR_DIGEST_BUSINESS_DAY")
    if isinstance(business_day, str):
        business_day = business_day.strip() or None
    else:
        business_day = None

    payload = project_digest_candidates(
        runtime_reports=report,
        opaque_account_uid=uid,
        target_id=tid,
        account_facts=account_facts,
        business_day=business_day,
    )
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError:
        return {"status": "skipped", "reason": "output_write_failed"}

    return {
        "status": "candidates_written",
        "reason": payload.get("producer_reason") or "ok",
        "producer_status": payload.get("producer_status"),
        "runs": len(payload.get("runs") or []),
        "identity_uid_present": bool(uid),
        "identity_target_present": bool(tid),
        "equity_present": any(
            isinstance(row, Mapping) and row.get("equity") is not None
            for row in (payload.get("runs") or [])
        ),
    }


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args:
        print(json.dumps({"status": "skipped", "reason": "unsupported_arguments"}))
        return 2
    try:
        result = emit_digest_candidates(os.environ)
    except Exception:
        print(json.dumps({"status": "skipped", "reason": "operation_failed"}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "candidates_written" else 2


if __name__ == "__main__":
    raise SystemExit(main())
