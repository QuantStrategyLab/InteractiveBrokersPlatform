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


def _layout(payload: Mapping[str, Any], serving: str, image: str, full_revision: str) -> dict[str, Any]:
    request = payload.get("request")
    request = request if isinstance(request, Mapping) else {}
    direct_spec = request.get("spec")
    direct_template = request.get("template") or (direct_spec.get("template") if isinstance(direct_spec, Mapping) else None)
    direct_template = direct_template if isinstance(direct_template, Mapping) else {}
    direct_metadata = direct_template.get("metadata")
    direct_metadata = direct_metadata if isinstance(direct_metadata, Mapping) else {}
    names = (direct_template.get("revision"), direct_metadata.get("name"))
    direct_spec = direct_template.get("spec")
    containers = direct_template.get("containers") or (direct_spec.get("containers") if isinstance(direct_spec, Mapping) else None)
    requested_image = containers[0].get("image") if isinstance(containers, list) and len(containers) == 1 and isinstance(containers[0], Mapping) else None
    response = payload.get("response")
    response = response if isinstance(response, Mapping) else {}
    status = response.get("status")
    response_names = (response.get("latestCreatedRevision"), status.get("latestCreatedRevisionName") if isinstance(status, Mapping) else None)
    return {
        "service_wrapper_present": isinstance(request.get("service"), Mapping),
        "direct_spec_template_present": isinstance(request.get("spec"), Mapping) and isinstance(request["spec"].get("template"), Mapping),
        "direct_template_present": isinstance(request.get("template"), Mapping),
        "direct_explicit_revision_present": any(isinstance(n, str) and bool(n) for n in names),
        "direct_revision_equals_serving": any(n in {serving, full_revision} for n in names if isinstance(n, str)),
        "direct_pinned_image_equals_serving_digest": requested_image == image if isinstance(requested_image, str) and re.search(r"@sha256:[0-9a-f]{64}$", requested_image) else None,
        "response_explicit_revision_present": any(isinstance(n, str) and bool(n) for n in response_names),
        "response_revision_equals_serving": any(n in {serving, full_revision} for n in response_names if isinstance(n, str)),
    }


def _native_operation_links(events: list, serving: str, image: str, full_revision: str) -> dict[str, Any]:
    """Link unique native endpoints inside this already-bounded service response."""
    groups = {}; missing = 0; ambiguous = 0; links = []
    for event in events:
        operation = event.get("operation")
        if (not isinstance(operation, Mapping)
            or any(not isinstance(operation.get(k), str) or not operation[k].strip() for k in ("id", "producer"))):
            missing += 1
            continue
        groups.setdefault((operation["id"], operation["producer"]), []).append(event)
    for group in groups.values():
        payloads = [event["protoPayload"] for event in group]
        methods = [payload.get("methodName") for payload in payloads]
        if (any(not isinstance(method, str) or method not in METHODS for method in methods)
            or len(set(methods)) != 1
            or len({payload["resourceName"] for payload in payloads}) != 1
            or any(type(event["operation"].get(flag, False)) is not bool for event in group for flag in ("first", "last"))):
            ambiguous += 1
            continue
        first = [event for event in group if event["operation"].get("first") is True]
        last = [event for event in group if event["operation"].get("last") is True]
        if len(first) != 1 or len(last) != 1 or first[0] is last[0]:
            ambiguous += 1
            continue
        request_payload, response_payload = first[0]["protoPayload"], last[0]["protoPayload"]
        request = request_payload.get("request")
        spec = request.get("service") if isinstance(request, Mapping) else None
        template = spec.get("template") or (spec.get("spec") or {}).get("template") if isinstance(spec, Mapping) else None
        if (not isinstance(template, Mapping)
            or not _layout(response_payload, serving, image, full_revision)["response_revision_equals_serving"]):
            ambiguous += 1
            continue
        declaration = template.get("metadata")
        requested_names = (template.get("revision"), declaration.get("name") if isinstance(declaration, Mapping) else None)
        if any(name not in (None, "") and (not isinstance(name, str) or name not in {serving, full_revision}) for name in requested_names):
            ambiguous += 1
            continue
        labels = declaration.get("labels") if isinstance(declaration, Mapping) else template.get("labels")
        labels = labels if isinstance(labels, Mapping) else {}
        source, locator = labels.get("commit-sha"), labels.get("github-run-id")
        containers = template.get("containers") or (template.get("spec") or {}).get("containers")
        requested_image = containers[0].get("image") if isinstance(containers, list) and len(containers) == 1 and isinstance(containers[0], Mapping) else None
        codes = []
        for payload in (request_payload, response_payload):
            status = payload.get("status")
            code = status.get("code") if isinstance(status, Mapping) else None
            codes.append(code if type(code) is int and 0 <= code <= 16 else None)
        links.append({"method": methods[0], "native_operation_key_equal": True,
            "unique_first_request_last_serving_response": True,
            "first_status_code": codes[0], "last_status_code": codes[1],
            "declared_source_sha": source if isinstance(source, str) and re.fullmatch(r"[0-9a-f]{40}", source) else None,
            "declared_run_id": locator if isinstance(locator, str) and re.fullmatch(r"[1-9][0-9]{0,19}", locator) else None,
            "request_image_equals_serving_digest": requested_image == image if isinstance(requested_image, str) and re.search(r"@sha256:[0-9a-f]{64}$", requested_image) else None})
    return {"status": "linked" if links else "unknown", "native_group_count": len(groups),
            "entries_without_native_key_count": missing, "ambiguous_or_unmatched_group_count": ambiguous,
            "linked_group_count": len(links), "links": links}


def observe(expected: Mapping[str, Any], revision: Mapping[str, Any], image: str,
            region: str, *, run: Any = None) -> dict[str, Any]:
    result = {"enabled": True, "reason": "audit_not_observed", "event_limit": 20,
        "window_seconds": 600, "window_basis": "serving_revision_creation_time",
        "audit_status": "unknown", "service_events_count": None,
        "exact_revision_events_count": None, "events": [], "event_layouts": [],
        "native_operations": {"status": "unknown", "native_group_count": None, "entries_without_native_key_count": None,
            "ambiguous_or_unmatched_group_count": None, "linked_group_count": None, "links": []},
        "independent_release_approval": "unknown"}
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
        layouts = []
        for event in events:
            payload = event.get("protoPayload") if isinstance(event, Mapping) else None
            if not isinstance(payload, Mapping) or payload.get("resourceName") not in resources:
                result.update(reason="audit_read_unavailable", audit_status="invalid_scope")
                return result
            layouts.append(_layout(payload, serving, image,
                f"projects/{project}/locations/{region}/services/{service}/revisions/{serving}"))
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
                      service_events_count=len(events), exact_revision_events_count=len(observations), events=observations, event_layouts=layouts)
        result["native_operations"] = _native_operation_links(events, serving, image,
            f"projects/{project}/locations/{region}/services/{service}/revisions/{serving}")
    except Exception:
        result.update(reason="audit_read_unavailable", audit_status="unavailable")
    return result
