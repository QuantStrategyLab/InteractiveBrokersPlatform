"""Back up one verified primary source tuple, never change it or publish facts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

from scripts import diagnose_account_facts_identity as diagnostic
from scripts import publish_account_facts_from_report as publisher


class BackupError(ValueError):
    def __init__(self, reason, **details):
        super().__init__(reason)
        self.details = details


def sha(value: str | bytes) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def command_failure(result):
    # Provider stderr can contain private object names. Only fixed categories leave
    # the runner; missing audit logs never establish a permission failure.
    error = result.stderr.decode("utf-8", errors="replace").lower()
    category = "permission_denied" if any(text in error for text in (
        "permission_denied", "permission denied", "does not have permission", "403",
    )) else "object_not_found" if any(text in error for text in (
        "matched no objects", "not found", "404",
    )) else "invalid_argument" if any(text in error for text in (
        "unrecognized arguments", "invalid argument", "usage:",
    )) else "command_failed"
    return {"exit_code": result.returncode, "failure_category": category}


def resolve_context():
    if (os.environ.get("IBKR_ACCOUNT_FACTS_TARGET") != "primary-live"
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or os.environ.get("GITHUB_REF") != "refs/heads/main"):
        raise BackupError("backup_mode_invalid")
    try:
        hashes = json.loads(os.environ.get("IBKR_PRIMARY_BACKUP_HASHES", ""))
        if (not isinstance(hashes, dict) or set(hashes) != {"target_id_sha256",
                "configured_source_binding_id_sha256", "candidate_source_binding_id_sha256"}
                or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
                       for value in hashes.values())
                or hashes["configured_source_binding_id_sha256"] == hashes["candidate_source_binding_id_sha256"]):
            raise ValueError
        expected = diagnostic.resolve_expected()
        identity = {field: expected[f"expected_{field}"] for field in (
            "project_id", "service_name", "account_scope", "account_selector", "deployment_selector",
        )}
        configured_id = publisher._bound_source_id(**identity,
            runtime_revision=expected["expected_runtime_revision"])
        if (sha(expected["target_id"]) != hashes["target_id_sha256"]
                or sha(configured_id) != hashes["configured_source_binding_id_sha256"]):
            raise ValueError
    except Exception:
        raise BackupError("backup_context_invalid") from None
    return expected, hashes


def prepare_backup(expected, hashes, *, now):
    uri = publisher._latest_report_uri(prefix=expected["expected_report_prefix"],
        project_id=expected["expected_project_id"], now=now)
    report = publisher._load_gcs_report(uri, project_id=expected["expected_project_id"])
    metadata = diagnostic._read_cloud_run_metadata(expected)
    result = diagnostic.diagnose_identity(report, expected=expected, source_report_uri=uri,
        now=now, cloud_run_metadata=metadata)
    preview = diagnostic._primary_source_binding_preview(report,
        expected=expected, metadata=metadata, result=result)
    if (preview is None or preview["difference_fields"] != ["runtime_revision"]
            or any(preview[key] != value for key, value in hashes.items())):
        raise BackupError("backup_comparison_changed")
    identity = {field: expected[f"expected_{field}"] for field in (
        "project_id", "service_name", "account_scope", "account_selector", "deployment_selector",
    )}
    revision = report["diagnostics"]["runtime_revision"]
    payload = {
        "schema_version": "ibkr.primary_source_state_backup.v1",
        "comparison_sha256": sha(canonical(hashes)),
        "comparison_hashes": hashes,
        "source_sha": os.environ.get("GITHUB_SHA"),
        "old_expected": expected,
        "candidate": {**identity, "runtime_revision": revision,
            "source_binding_id": publisher._bound_source_id(**identity, runtime_revision=revision)},
        "source_report_uri": uri,
    }
    body = canonical(payload)
    # Outside the publisher's YYYY-MM/report-name listing, in the same private prefix.
    destination = f"{expected['expected_report_prefix'].rstrip('/')}/_primary_source_backups/{sha(body)}.json"
    publisher._source_report_uri(destination, expected["expected_report_prefix"])
    return destination, body


def persist_backup(destination, body, *, project_id):
    # A create-only upload has no retry. Even an uncertain upload is followed only
    # by an exact readback; neither stdout nor stderr leaves this process.
    upload_status = {"failure_category": "transport_unknown"}
    try:
        upload = subprocess.run(("gcloud", "storage", "cp", "-", destination, "--project", project_id,
            "--if-generation-match=0", "--quiet"), input=body, capture_output=True,
            check=False, timeout=60)
        upload_status = {"exit_code": 0} if upload.returncode == 0 else command_failure(upload)
    except subprocess.TimeoutExpired:
        upload_status = {"failure_category": "timeout"}
    except (OSError, subprocess.SubprocessError):
        upload_status = {"failure_category": "command_unavailable"}
    try:
        readback = subprocess.run(("gcloud", "storage", "cat", destination, "--project", project_id),
            capture_output=True, check=False, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackupError("backup_readback_unknown", upload=upload_status,
            readback={"failure_category": "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "command_unavailable"},
            backup_reference_sha256=sha(destination), backup_object_sha256=sha(body)) from None
    if readback.returncode != 0 or readback.stdout != body:
        raise BackupError("backup_readback_unknown", upload=upload_status,
            readback=command_failure(readback) if readback.returncode else {"failure_category": "content_mismatch"},
            backup_reference_sha256=sha(destination), backup_object_sha256=sha(body))
    return {"schema_version": "ibkr.primary_source_state_backup_result.v1",
        "status": "backup_verified", "backup_reference_sha256": sha(destination),
        "backup_object_sha256": sha(body), "publication_authority_granted": False}


def inspect_backup(expected, hashes):
    """Read existing backup objects only; never retry an uncertain upload."""
    prefix = expected["expected_report_prefix"].rstrip("/") + "/_primary_source_backups/"
    project = expected["expected_project_id"]
    original_head = os.environ.get("IBKR_PRIMARY_BACKUP_ORIGINAL_HEAD_SHA", "")
    if re.fullmatch(r"[a-f0-9]{40}", original_head) is None:
        raise BackupError("backup_original_head_invalid")
    try:
        listed = subprocess.run(("gcloud", "storage", "ls", prefix, "--project", project),
            capture_output=True, check=False, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackupError("backup_inspection_failed", operation="list",
            failure_category="timeout" if isinstance(exc, subprocess.TimeoutExpired) else "command_unavailable") from None
    if listed.returncode:
        failure = command_failure(listed)
        if failure["failure_category"] != "object_not_found":
            raise BackupError("backup_inspection_failed", operation="list", **failure)
        uris = []
    else:
        uris = listed.stdout.decode("utf-8").splitlines()
    if len(uris) > 4 or any(not uri.startswith(prefix)
            or not re.fullmatch(r"[a-f0-9]{64}\.json", uri[len(prefix):]) for uri in uris):
        raise BackupError("backup_listing_invalid")
    matches = []
    for uri in uris:
        try:
            read = subprocess.run(("gcloud", "storage", "cat", uri, "--project", project),
                capture_output=True, check=False, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise BackupError("backup_inspection_failed", operation="read",
                failure_category="timeout" if isinstance(exc, subprocess.TimeoutExpired) else "command_unavailable") from None
        if read.returncode:
            raise BackupError("backup_inspection_failed", operation="read", **command_failure(read))
        if len(read.stdout) > 64 * 1024:
            raise BackupError("backup_content_invalid")
        try:
            saved = json.loads(read.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BackupError("backup_content_invalid") from None
        if not isinstance(saved, dict):
            raise BackupError("backup_content_invalid")
        if saved.get("comparison_sha256") != sha(canonical(hashes)) or saved.get("source_sha") != original_head:
            continue
        candidate = saved.get("candidate", {})
        identity = {field: expected[f"expected_{field}"] for field in (
            "project_id", "service_name", "account_scope", "account_selector", "deployment_selector",
        )}
        if (saved.get("schema_version") != "ibkr.primary_source_state_backup.v1"
                or saved.get("comparison_hashes") != hashes
                or canonical(saved.get("old_expected")) != canonical(expected)
                or set(candidate) != set(identity) | {"runtime_revision", "source_binding_id"}
                or canonical({key: candidate[key] for key in identity}) != canonical(identity)
                or candidate["source_binding_id"] != publisher._bound_source_id(**identity,
                    runtime_revision=candidate["runtime_revision"])
                or sha(candidate["source_binding_id"]) != hashes["candidate_source_binding_id_sha256"]
                or sha(read.stdout) + ".json" != uri[len(prefix):]):
            raise BackupError("backup_content_invalid")
        publisher._source_report_uri(saved["source_report_uri"], expected["expected_report_prefix"])
        matches.append({"backup_reference_sha256": sha(uri), "backup_object_sha256": sha(read.stdout)})
    if len(matches) > 1:
        raise BackupError("backup_match_ambiguous")
    return {"status": "backup_verified" if matches else "backup_absent", "inspection_only": True,
        "comparison_sha256": sha(canonical(hashes)), "matched_backups": len(matches),
        **(matches[0] if matches else {}), "publication_authority_granted": False}


def main(argv=None, *, now=None):
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if args not in ([], ["--preflight"], ["--inspect-only"]):
            raise BackupError("arguments_invalid")
        expected, hashes = resolve_context()
        if args == ["--preflight"]:
            result = {"status": "preflight_verified"}
        elif args == ["--inspect-only"]:
            result = inspect_backup(expected, hashes)
        else:
            destination, body = prepare_backup(expected, hashes, now=now or datetime.now(timezone.utc))
            result = persist_backup(destination, body, project_id=expected["expected_project_id"])
            result["comparison_sha256"] = sha(canonical(hashes))
    except BackupError as exc:
        result = {"status": "unknown" if str(exc) == "backup_readback_unknown" else "blocked",
            "reason": str(exc), **exc.details}
    except Exception:
        result = {"status": "blocked", "reason": "backup_failed"}
    if args == ["--inspect-only"]:
        result["inspection_only"] = True
        original_head = os.environ.get("IBKR_PRIMARY_BACKUP_ORIGINAL_HEAD_SHA", "")
        if re.fullmatch(r"[a-f0-9]{40}", original_head):
            result["original_head_sha"] = original_head
        if "hashes" in locals():
            result["comparison_sha256"] = sha(canonical(hashes))
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] in {"preflight_verified", "backup_verified", "backup_absent"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
