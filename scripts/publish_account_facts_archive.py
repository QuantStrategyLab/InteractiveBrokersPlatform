"""Preview, seal a history-source request, or append reviewed archived snapshots."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, build_opener

from scripts import diagnose_account_facts_identity as identity
from scripts import diagnose_serving_image_identity as image
from scripts import inspect_account_facts_archive as archive
from scripts import publish_account_facts_from_report as publisher
from scripts.backup_primary_account_facts_state import _seal_secret


class ArchivePublishError(ValueError):
    def __init__(self, reason, **safe):
        super().__init__(reason)
        self.safe = safe


def sha(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()


def manifest(raw, *, expected, now):
    try:
        rows = json.loads(raw, object_pairs_hook=publisher._report_json_pairs)
        if not isinstance(rows, list) or not 1 <= len(rows) <= 31:
            raise ValueError
        seen = set()
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {"report_name", "source_sha256", "projection_sha256"}
                    or any(not isinstance(row[k], str) or not re.fullmatch(r"[a-f0-9]{64}", row[k])
                           for k in ("source_sha256", "projection_sha256"))):
                raise ValueError
            uri = publisher._named_report_uri(prefix=expected["expected_report_prefix"],
                                              report_name=row["report_name"], now=now)
            day = datetime.strptime(row["report_name"], "%Y%m%dT%H%M%SZ.json").date().isoformat()
            archive.archive_day(day, now=now)
            if day in seen:
                raise ValueError
            seen.add(day)
            row["uri"] = uri
        return sorted(rows, key=lambda x: x["report_name"])
    except Exception:
        raise ArchivePublishError("archive_manifest_invalid") from None


def revision_metadata(revision, *, expected):
    region = os.environ.get("CLOUD_RUN_REGION", "")
    if not re.fullmatch(r"[a-z]+-[a-z]+[0-9]+", region):
        raise ArchivePublishError("archive_region_invalid")
    result = subprocess.run(["gcloud", "run", "revisions", "describe", revision,
        "--project", expected["expected_project_id"], "--region", region, "--format=json"],
        capture_output=True, text=True, timeout=60, check=False)
    if result.returncode or len(result.stdout) > 1024 * 1024:
        raise ArchivePublishError("archive_revision_unavailable")
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise ArchivePublishError("archive_revision_invalid") from None


def qualify(report, entry, *, expected, revision):
    declared = publisher._exact_text(report.get("diagnostics", {}).get("runtime_revision"))
    if not declared:
        raise ArchivePublishError("archive_revision_missing")
    metadata = revision.get("metadata", {})
    if (metadata.get("name") != declared
            or metadata.get("labels", {}).get("serving.knative.dev/service") != expected["expected_service_name"]):
        raise ArchivePublishError("archive_revision_identity_mismatch")
    try:
        _, container = image.immutable_image(revision)
        env = [r for r in container.get("env", []) if r.get("name") == "RUNTIME_TARGET_JSON"]
        if len(env) != 1:
            raise ValueError
        target = json.loads(env[0]["value"])
        if not all(value is True for value in identity._source_scope(target, expected).values()):
            raise ValueError
    except Exception:
        raise ArchivePublishError("archive_deployed_scope_unverified") from None
    projected = publisher.project_ibkr_account_facts_history(report, source_report_uri=entry["uri"],
        **{**expected, "expected_runtime_revision": declared})
    if projected.get("status") == "skipped":
        raise ArchivePublishError("archive_projection_rejected")
    fields = archive.projection_metadata(projected)
    day = datetime.strptime(entry["report_name"], "%Y%m%dT%H%M%SZ.json").date().isoformat()
    if (fields["observation_date"] != day or fields["source_binding_id_sha256"] != entry["source_sha256"]
            or fields["projection_sha256"] != entry["projection_sha256"] or not fields["net_assets_available"]):
        raise ArchivePublishError("archive_reviewed_projection_changed")
    return publisher.prepare_sync_payload(projected)


def source_request(payloads, *, expected):
    current = publisher._bound_source_id(**{name: expected["expected_" + name] for name in (
        "project_id", "service_name", "runtime_revision", "account_scope", "account_selector", "deployment_selector")})
    ids = sorted({p["source_binding"]["id"] for p in payloads} - {current})
    if not ids:
        raise ArchivePublishError("archive_history_source_not_required")
    return {"target_id": expected["target_id"], "previous_source_binding_id": current,
            "next_source_binding_id": current, "history_source_binding_ids": ids}


def seal_request(request, keys):
    try:
        key = keys["receiver"]
        if (set(key) != {"key", "key_id"} or len(base64.b64decode(key["key"], validate=True)) != 32
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key["key_id"])):
            raise ValueError
        encrypted = _seal_secret(key["key"], canonical(request))
    except Exception:
        raise ArchivePublishError("archive_receiver_key_invalid") from None
    return {"name": "IBKR_ACCOUNT_FACTS_SOURCE_ROTATION_JSON", "key_id": key["key_id"],
            "public_key_sha256": sha(base64.b64decode(key["key"])),
            "encrypted_value": base64.b64encode(encrypted).decode()}


def append_once(payload, *, token, account_key):
    if not isinstance(token, str) or not token or any(c.isspace() for c in token):
        raise ArchivePublishError("archive_ingress_auth_unavailable")
    url = publisher.IBKR_ACCOUNT_FACTS_SYNC_URL.removesuffix("/sync") + "/history/sync"
    request = Request(url, data=canonical(payload), method="POST", headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json",
        "User-Agent": publisher.IBKR_ACCOUNT_FACTS_USER_AGENT})
    try:
        with build_opener(publisher._NoRedirect()).open(request, timeout=20) as response:
            raw = response.read(publisher._HTTP_ERROR_BODY_LIMIT + 1)
            if not 200 <= response.status < 300 or len(raw) > publisher._HTTP_ERROR_BODY_LIMIT:
                raise ValueError
            ack = json.loads(raw)
        expected = {"ok": True, "stored": True, "historical": True, "latest_unchanged": True,
                    "platform": "ibkr", "account_key": account_key, "target_id": payload["target_id"],
                    "observation_date": payload["observation_date"],
                    "observed_finished_at": payload["observed_finished_at"]}
        if any(type(ack.get(k)) is not type(v) or ack[k] != v for k, v in expected.items()) or type(ack.get("unchanged")) is not bool:
            raise ValueError
        return {"date": payload["observation_date"], "status": "unchanged" if ack["unchanged"] else "published",
                "history_only_ack_verified": True, "latest_unchanged": True}
    except HTTPError as exc:
        try:
            error = json.loads(exc.read(publisher._HTTP_ERROR_BODY_LIMIT)).get("error")
        except Exception:
            error = None
        allowed = {"account_facts_daily_conflict", "account_facts_binding_mismatch", "account_facts_identity_mismatch"}
        raise ArchivePublishError("archive_sync_rejected", http_status=exc.code,
                                  error_code=error if error in allowed else "unknown") from None
    except Exception:
        raise ArchivePublishError("archive_sync_outcome_unknown") from None


def main():
    stage = "context"
    try:
        if (os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch" or os.environ.get("GITHUB_REF") != "refs/heads/main"
                or os.environ.get("GITHUB_RUN_ATTEMPT") != "1"):
            raise ArchivePublishError("archive_manual_main_once_required")
        mode = os.environ.get("IBKR_ARCHIVE_ACTION", "preview")
        if mode not in {"preview", "seal-source", "publish"}:
            raise ArchivePublishError("archive_action_invalid")
        expected = identity.resolve_expected()
        slot = os.environ.get("IBKR_ACCOUNT_FACTS_TARGET")
        keys = json.loads(os.environ.get("IBKR_PERIOD_RETURN_ADDITIONAL_ACCOUNT_KEYS_JSON", ""),
                          object_pairs_hook=publisher._report_json_pairs)
        if not isinstance(keys, dict) or slot not in keys or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", keys[slot]):
            raise ArchivePublishError("archive_account_key_unavailable")
        now = datetime.now(timezone.utc)
        entries = manifest(os.environ.get("IBKR_ARCHIVE_REPORTS_JSON", ""), expected=expected, now=now)
        payloads, revisions = [], {}
        for entry in entries:
            stage = "report_read"
            report = publisher._load_gcs_report(entry["uri"], project_id=expected["expected_project_id"])
            declared = publisher._exact_text(report.get("diagnostics", {}).get("runtime_revision"))
            if not declared:
                raise ArchivePublishError("archive_revision_missing")
            stage = "revision_metadata"
            if declared not in revisions:
                revisions[declared] = revision_metadata(declared, expected=expected)
            payloads.append(qualify(report, entry, expected=expected, revision=revisions[declared]))
        safe = {"status": "previewed", "slot": slot, "selected_days": len(payloads),
                "dates": [p["observation_date"] for p in payloads], "account_key_sha256": sha(keys[slot]),
                "target_id_sha256": sha(expected["target_id"]), "revision_metadata_verified": True,
                "source_sha256": sorted({sha(p["source_binding"]["id"]) for p in payloads}),
                "broker_requests": 0, "business_writes": 0}
        if mode == "seal-source":
            stage = "seal_source_request"
            sealed = seal_request(source_request(payloads, expected=expected),
                                  json.loads(os.environ.get("ACCOUNT_FACTS_RECOVERY_PUBLIC_KEYS_JSON", "")))
            Path(os.environ["RUNNER_TEMP"], "account-facts-archive-source-sealed.json").write_text(json.dumps(sealed))
            safe["status"] = "source_request_sealed"
        elif mode == "publish":
            stage = "history_append"
            for payload in payloads:
                result = append_once(payload, token=os.environ.get("IBKR_ACCOUNT_FACTS_SYNC_TOKEN"), account_key=keys[slot])
                print(json.dumps(result), flush=True)
            safe.update(status="published", business_writes=len(payloads), latest_unchanged=True)
        print(json.dumps(safe, sort_keys=True))
        return 0
    except ArchivePublishError as exc:
        print(json.dumps({"status": "stopped", "stage": stage, "reason": str(exc), **exc.safe}))
    except Exception:
        print(json.dumps({"status": "stopped", "stage": stage, "reason": "archive_operation_failed"}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
