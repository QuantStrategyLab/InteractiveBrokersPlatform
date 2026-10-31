"""Bounded exact-serving deployment audit clues; never release approval."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
from collections.abc import Mapping
from typing import Any

METHODS = {f"google.cloud.run.{version}.Services.{method}" for version, methods in (
    ("v1", ("CreateService", "ReplaceService", "UpdateService")),
    ("v2", ("CreateService", "UpdateService"))) for method in methods}


def observe(expected: Mapping[str, Any], revision: Mapping[str, Any], image: str,
            region: str, *, run: Any = None) -> dict[str, Any]:
    result = {"enabled": True, "reason": "audit_not_observed", "event_limit": 20,
        "window_seconds": 600, "window_basis": "serving_revision_creation_time",
        "audit_status": "unknown", "service_events_count": None,
        "exact_revision_events_count": None, "events": [], "independent_release_approval": "unknown"}
    try:
        metadata = revision["metadata"]
        serving = metadata["name"]
        stamp = dt.datetime.fromisoformat(metadata["creationTimestamp"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError
        project, service = expected["expected_project_id"], expected["expected_service_name"]
        resources = (f"namespaces/{project}/services/{service}",
                     f"projects/{project}/locations/{region}/services/{service}")
        query = ('protoPayload.serviceName="run.googleapis.com" AND ('
                 + " OR ".join("protoPayload.resourceName=" + json.dumps(r) for r in resources) + ")"
                 + " AND timestamp>=" + json.dumps((stamp - dt.timedelta(minutes=5)).isoformat())
                 + " AND timestamp<=" + json.dumps((stamp + dt.timedelta(minutes=5)).isoformat()))
        process = (subprocess.run if run is None else run)(
            ("gcloud", "logging", "read", query, "--project", project, "--format=json", "--limit=21", "--order=asc"),
            capture_output=True, text=True, check=False, timeout=30,
            env={**os.environ, "CLOUDSDK_CORE_DISABLE_FILE_LOGGING": "true", "CLOUDSDK_CORE_LOG_HTTP": "false"})
        if process.returncode:
            result.update(reason="audit_read_unavailable", audit_status="permission_denied" if "PERMISSION_DENIED" in process.stderr else "read_failed")
            return result
        if len(process.stdout.encode("utf-8")) > 2 * 1024 * 1024:
            result.update(reason="audit_read_unavailable", audit_status="response_limit")
            return result
        events = json.loads(process.stdout)
        if not isinstance(events, list) or len(events) > 20:
            result.update(reason="audit_read_unavailable", audit_status="truncated_or_invalid")
            return result
        observations = []
        for event in events:
            payload = event.get("protoPayload") if isinstance(event, Mapping) else None
            if not isinstance(payload, Mapping) or payload.get("resourceName") not in resources:
                result.update(reason="audit_read_unavailable", audit_status="invalid_scope")
                return result
            method = payload.get("methodName")
            request = payload.get("request")
            spec = request.get("service") if isinstance(request, Mapping) else None
            if method not in METHODS or not isinstance(spec, Mapping):
                continue
            template = spec.get("template") or (spec.get("spec") or {}).get("template")
            if not isinstance(template, Mapping):
                continue
            declaration = template.get("metadata")
            declaration = declaration if isinstance(declaration, Mapping) else {}
            names = [template.get("revision"), declaration.get("name")]
            response = payload.get("response")
            if isinstance(response, Mapping):
                names.extend([response.get("latestCreatedRevision"), (response.get("status") or {}).get("latestCreatedRevisionName")])
            if not any(n in {serving, f"projects/{project}/locations/{region}/services/{service}/revisions/{serving}"} for n in names if isinstance(n, str)):
                continue
            labels = declaration.get("labels") or template.get("labels") or {}
            source = labels.get("commit-sha") if isinstance(labels, Mapping) else None
            locator = labels.get("github-run-id") if isinstance(labels, Mapping) else None
            containers = template.get("containers") or (template.get("spec") or {}).get("containers")
            requested_image = containers[0].get("image") if isinstance(containers, list) and len(containers) == 1 and isinstance(containers[0], Mapping) else None
            status = payload.get("status")
            status_code = status.get("code") if isinstance(status, Mapping) else None
            observations.append({"method": method,
                "status_code": status_code if type(status_code) is int and 0 <= status_code <= 16 else None,
                "declared_source_sha": source if isinstance(source, str) and re.fullmatch(r"[0-9a-f]{40}", source) else None,
                "declared_run_id": locator if isinstance(locator, str) and re.fullmatch(r"[1-9][0-9]{0,19}", locator) else None,
                "request_image_equals_serving_digest": requested_image == image if isinstance(requested_image, str) and re.search(r"@sha256:[0-9a-f]{64}$", requested_image) else None,
                "principal_present": isinstance(payload.get("authenticationInfo"), Mapping) and bool(payload["authenticationInfo"].get("principalEmail"))})
        result.update(reason="deployment_audit_observed", audit_status="exact_revision_events" if observations else "no_exact_revision_event",
                      service_events_count=len(events), exact_revision_events_count=len(observations), events=observations)
    except Exception:
        result.update(reason="audit_read_unavailable", audit_status="unavailable")
    return result
