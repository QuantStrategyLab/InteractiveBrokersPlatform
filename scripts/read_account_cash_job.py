"""Read a completed target-bound cash Job without publishing private paths or amounts."""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit

from scripts.account_cash_readonly_entry import PROFILES
from scripts import observe_maintenance_cash_job as maintenance


def readback(expected, *, actual_job, document, execution, logs, namespace, region, client, now):
    """Validate the exact Job/execution before locating a unique report by URI hash."""
    maintenance.verify_job(actual_job, document)
    job = document["metadata"]["name"]
    name = maintenance.verify_execution(execution, job, document)
    rows = document["spec"]["template"]["spec"]["template"]["spec"]["containers"][0].get("env", [])
    profiles = [row.get("value") for row in rows if row.get("name") == "CASH_READONLY_PROFILE"]
    if len(profiles) != 1 or profiles[0] not in PROFILES:
        raise ValueError("profile_unverified")
    status = execution.get("status", {})
    if status.get("succeededCount") != 1 or status.get("failedCount", 0) or status.get("retriedCount", 0):
        raise ValueError("execution_not_successful")
    started = maintenance.publisher._observed_timestamp(status.get("startTime"))
    finished = maintenance.publisher._observed_timestamp(status.get("completionTime"))
    if started > finished or finished > now:
        raise ValueError("execution_time_invalid")
    if not isinstance(logs, list) or len(logs) >= 1000:
        raise ValueError("logs_incomplete")
    hashes = []
    for row in logs:
        resource = row.get("resource", {})
        labels = resource.get("labels", {})
        stamp = maintenance.publisher._observed_timestamp(row.get("timestamp"))
        if (resource.get("type") != "cloud_run_job" or labels.get("job_name") != job
                or labels.get("project_id") != expected["expected_project_id"] or labels.get("location") != region
                or row.get("labels", {}).get("run.googleapis.com/execution_name") != name
                or not started <= stamp <= finished):
            raise ValueError("log_identity_invalid")
        try:
            payload = json.loads(row.get("textPayload", ""))
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("status") == "observation_saved":
            digest = payload.get("report_uri_sha256")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("report_hash_invalid")
            hashes.append(digest)
    if len(hashes) != 1:
        raise ValueError("report_hash_not_unique")
    uri = urlsplit(namespace)
    if uri.scheme != "gs" or not uri.netloc or not uri.path.strip("/") or uri.query or uri.fragment:
        raise ValueError("namespace_invalid")
    prefix = uri.path.strip("/") + "/"
    bucket = client.bucket(uri.netloc)
    token, seen, matches = None, set(), []
    for _ in range(100):
        iterator = client.list_blobs(bucket, prefix=prefix, page_token=token, page_size=1000)
        page = next(iterator.pages, None)
        for blob in page or ():
            if not isinstance(blob.name, str) or not blob.name.startswith(prefix):
                raise ValueError("object_outside_namespace")
            candidate = "gs://" + uri.netloc + "/" + blob.name
            if hashlib.sha256(candidate.encode()).hexdigest() == hashes[0]:
                matches.append(blob)
        token = iterator.next_page_token
        if token is None:
            break
        if not token or token in seen:
            raise ValueError("pagination_invalid")
        seen.add(token)
    else:
        raise ValueError("pagination_incomplete")
    if len(matches) != 1:
        raise ValueError("report_object_not_unique")
    blob = matches[0]
    if not isinstance(blob.generation, int) or not isinstance(blob.size, int) or not 0 < blob.size <= 2_000_000:
        raise ValueError("report_object_invalid")
    report_uri = "gs://" + uri.netloc + "/" + blob.name
    raw = bucket.blob(blob.name, generation=blob.generation).download_as_bytes(
        if_generation_match=blob.generation, timeout=60, retry=None)
    if len(raw) != blob.size:
        raise ValueError("report_size_mismatch")
    report = json.loads(raw)
    if (not isinstance(report, dict) or report.get("strategy_profile") != profiles[0]
            or not isinstance(report.get("runtime_target"), dict)
            or report["runtime_target"].get("strategy_profile") != profiles[0]):
        raise ValueError("report_profile_mismatch")
    report_start = maintenance.publisher._observed_timestamp(report.get("started_at"))
    report_end = maintenance.publisher._observed_timestamp(report.get("finished_at"))
    if not started <= report_start <= report_end <= finished:
        raise ValueError("report_outside_execution")
    result = maintenance.inspect_report(report, report_uri, expected, job=job, execution=name,
                                        started=started, now=now, namespace=namespace)
    return {"status": result["status"], "source_identity_verified": result["source_identity_verified"],
            "saved_readback_verified": result["saved_readback_verified"],
            "report_sha256": result["report_sha256"], "report_uri_sha256": hashes[0]}
