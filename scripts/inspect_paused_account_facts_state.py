#!/usr/bin/env python3
"""Read-only preflight for the archived paused IBKR account-facts state."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
from urllib.parse import quote, urlencode, urlsplit

import google.auth
from google.auth.transport.requests import AuthorizedSession
from requests.adapters import HTTPAdapter


MAX_OBJECT_BYTES = 256 * 1024
MAX_BODY_BYTES = 128 * 1024
TIMEOUT_SECONDS = 20
ABSENT_STATES = {
    "metadata-adopt-attempt-once.json",
    "metadata-adopt-backup.json",
    "production-probe-attempt-once.json",
    "original-state.json",
    "runner.lock",
}
PRESERVED_FILES = {
    "candidate-prep-0/paused-inputs.json",
    "candidate-prep-0/gateway-connection-dispatch-once.json",
    "candidate-prep-0/gateway-connection-readback.json",
}
CONTEXT_KEYS = {
    "project", "region", "service", "service_resource", "service_base",
    "production_revision", "old_revision", "old_source",
    "allowed_candidate_source", "oidc", "report_prefix", "expected_job",
}
BINDING_KEYS = {
    "account_selector", "account_scope", "deployment_selector",
    "project_id", "runtime_revision", "service_name", "source_binding_id",
}
JOB_KEYS = {"main", "precheck", "warmup"}
JOB_CONTEXT_KEYS = {"name", "schedule", "method", "path", "deadline"}
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")


class PreflightError(ValueError):
    pass


def _reject_constant(_value: str) -> None:
    raise PreflightError("invalid_json")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PreflightError("invalid_json")
        result[key] = value
    return result


def _json(data: bytes) -> object:
    try:
        return json.loads(data, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PreflightError("invalid_json") from None


def _text(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\n" in value or "\r" in value:
        raise PreflightError("invalid_context")
    return value


def _gs_uri(value: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise PreflightError("invalid_uri") from None
    if (
        parsed.scheme != "gs" or not parsed.netloc or parsed.username or parsed.password
        or port is not None or parsed.query or parsed.fragment or not parsed.path.strip("/")
        or "\\" in parsed.path or any(part in {".", ".."} for part in parsed.path.split("/"))
    ):
        raise PreflightError("invalid_uri")
    return parsed.netloc, parsed.path.lstrip("/")


def _bounded_content(response: object, limit: int) -> bytes:
    chunks = []
    size = 0
    try:
        for chunk in response.iter_content(chunk_size=8192):
            if not chunk:
                continue
            size += len(chunk)
            if size > limit:
                raise PreflightError("response_too_large")
            chunks.append(chunk)
    except PreflightError:
        raise
    except Exception:
        raise PreflightError("response_invalid") from None
    return b"".join(chunks)


def _read_transfer(raw_uri: str) -> dict[str, object]:
    bucket, obj = _gs_uri(raw_uri)
    response = _request(
        "GET", f"https://storage.googleapis.com/storage/v1/b/{quote(bucket, safe='')}/o/{quote(obj, safe='')}?alt=media",
        expected=200,
    )
    try:
        body = _bounded_content(response, MAX_OBJECT_BYTES)
        parsed = _json(body)
    finally:
        response.close()
    if not isinstance(parsed, dict):
        raise PreflightError("invalid_object")
    return parsed


def _request(method: str, url: str, *, expected: int, json_body: object | None = None):
    session = _SESSION
    try:
        response = session.request(
            method, url, json=json_body, timeout=TIMEOUT_SECONDS,
            allow_redirects=False, stream=True,
        )
    except Exception:
        raise PreflightError("request_failed") from None
    if response.status_code != expected or 300 <= response.status_code < 400:
        response.close()
        raise PreflightError("http_failed")
    return response


_SESSION = None


def _verify_payload(payload: dict[str, object], protected: dict[str, str]) -> tuple[dict[str, str], dict[str, object]]:
    if set(payload) != {"target_ordinal", "inspected_at", "source_root", "absent_execution_state_names", "preserved_files"}:
        raise PreflightError("invalid_object")
    if isinstance(payload["target_ordinal"], bool) or payload["target_ordinal"] != 0 or not isinstance(payload["inspected_at"], str):
        raise PreflightError("invalid_object")
    try:
        from datetime import datetime
        stamp = datetime.fromisoformat(payload["inspected_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError
    except (ValueError, OverflowError):
        raise PreflightError("invalid_object") from None
    source_root = _text(payload["source_root"])
    source_path = PurePosixPath(source_root)
    if not source_path.is_absolute() or ".." in source_path.parts:
        raise PreflightError("invalid_object")
    absent = payload["absent_execution_state_names"]
    files = payload["preserved_files"]
    if not isinstance(absent, list) or absent != sorted(ABSENT_STATES):
        raise PreflightError("invalid_object")
    if not isinstance(files, dict) or set(files) != PRESERVED_FILES:
        raise PreflightError("invalid_object")
    decoded: dict[str, object] = {}
    for name, entry in files.items():
        if not isinstance(entry, dict) or set(entry) != {"sha256", "body_base64"}:
            raise PreflightError("invalid_object")
        digest, encoded = entry["sha256"], entry["body_base64"]
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest) or not isinstance(encoded, str):
            raise PreflightError("invalid_object")
        try:
            body = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error):
            raise PreflightError("invalid_object") from None
        if len(body) > MAX_BODY_BYTES or hashlib.sha256(body).hexdigest() != digest:
            raise PreflightError("invalid_object")
        decoded[name] = _json(body)
    if any(not isinstance(item, dict) for item in decoded.values()):
        raise PreflightError("invalid_object")

    inputs = decoded["candidate-prep-0/paused-inputs.json"]
    expected_input_keys = {
        "target_context", "image_digest", "account_binding", "paused_prior_run",
        "selected_slot", "old_target", "live_jobs",
    }
    context, binding = inputs.get("target_context"), inputs.get("account_binding")
    if (
        set(inputs) != expected_input_keys or inputs.get("selected_slot") != "additional-1"
        or not isinstance(inputs.get("image_digest"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", inputs["image_digest"])
        or not isinstance(inputs.get("paused_prior_run"), dict)
        or not isinstance(inputs.get("old_target"), dict)
        or not isinstance(inputs.get("live_jobs"), dict)
        or not isinstance(context, dict) or set(context) != CONTEXT_KEYS
        or not isinstance(binding, dict) or set(binding) != BINDING_KEYS
    ):
        raise PreflightError("invalid_context")
    if not isinstance(context["expected_job"], dict) or set(context["expected_job"]) != JOB_KEYS:
        raise PreflightError("invalid_context")
    for role, spec in context["expected_job"].items():
        if not isinstance(role, str) or not isinstance(spec, dict) or set(spec) != JOB_CONTEXT_KEYS:
            raise PreflightError("invalid_context")
        for item in spec.values():
            _text(item)
    expected_binding = {
        "account_scope": protected["IBKR_ACCOUNT_FACTS_ACCOUNT_SCOPE"],
        "deployment_selector": protected["IBKR_ACCOUNT_FACTS_DEPLOYMENT_SELECTOR"],
        "project_id": protected["IBKR_ACCOUNT_FACTS_PROJECT_ID"],
        "service_name": protected["IBKR_ACCOUNT_FACTS_SERVICE_NAME"],
    }
    selector = json.loads(protected["IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON"])
    if not isinstance(selector, list) or len(selector) != 1 or selector[0].lower() == "default":
        raise PreflightError("invalid_context")
    expected_binding["account_selector"] = selector[0]
    for key, expected in expected_binding.items():
        observed = binding[key]
        if isinstance(observed, list) and len(observed) == 1:
            observed = observed[0]
        if observed != expected:
            raise PreflightError("target_mismatch")
    _text(binding["source_binding_id"])

    project = _text(context["project"])
    region = _text(context["region"])
    service = _text(context["service"])
    if (
        project != protected["IBKR_ACCOUNT_FACTS_PROJECT_ID"]
        or service != protected["IBKR_ACCOUNT_FACTS_SERVICE_NAME"]
        or not _PROJECT_ID.fullmatch(project)
    ):
        raise PreflightError("target_mismatch")
    if not _IDENTIFIER.fullmatch(region) or not _IDENTIFIER.fullmatch(service):
        raise PreflightError("invalid_context")
    service_resource = f"projects/{project}/locations/{region}/services/{service}"
    if context["service_resource"] != service_resource:
        raise PreflightError("target_mismatch")
    if binding["runtime_revision"] != context["production_revision"]:
        raise PreflightError("target_mismatch")
    base = _text(context["service_base"])
    parsed_base = urlsplit(base)
    if parsed_base.scheme != "https" or not parsed_base.hostname or parsed_base.username or parsed_base.password or parsed_base.path or parsed_base.query or parsed_base.fragment:
        raise PreflightError("invalid_context")
    for field in ("production_revision", "old_revision"):
        if not _text(context[field]).startswith(service + "-"):
            raise PreflightError("invalid_context")
    for field in ("old_source", "allowed_candidate_source"):
        if not re.fullmatch(r"[a-f0-9]{40}", _text(context[field])):
            raise PreflightError("invalid_context")
    _text(context["oidc"])
    report_bucket, report_path = _gs_uri(protected["IBKR_ACCOUNT_FACTS_REPORT_PREFIX"])
    context_bucket, context_path = _gs_uri(_text(context["report_prefix"]))
    if (report_bucket, report_path.rstrip("/")) != (context_bucket, context_path.rstrip("/")):
        raise PreflightError("target_mismatch")
    return {"bucket": report_bucket, "project": project, "region": region, "service": service}, decoded


def _permissions(url: str, permissions: list[str], body: object) -> dict[str, bool]:
    response = _request("POST", url, expected=200, json_body=body)
    try:
        raw = _bounded_content(response, 32 * 1024)
        parsed = _json(raw)
    finally:
        response.close()
    if not isinstance(parsed, dict) or set(parsed) - {"permissions"}:
        raise PreflightError("invalid_response")
    granted = parsed.get("permissions", [])
    if not isinstance(granted, list) or any(not isinstance(item, str) for item in granted):
        raise PreflightError("invalid_response")
    return {permission: permission in granted for permission in permissions}


def _bucket_permissions(bucket: str, permissions: list[str]) -> dict[str, bool]:
    query = urlencode([("permissions", permission) for permission in permissions])
    url = f"https://storage.googleapis.com/storage/v1/b/{quote(bucket, safe='')}/iam/testPermissions?{query}"
    response = _request("GET", url, expected=200)
    try:
        raw = _bounded_content(response, 32 * 1024)
        parsed = _json(raw)
    finally:
        response.close()
    if not isinstance(parsed, dict) or set(parsed) - {"kind", "permissions"}:
        raise PreflightError("invalid_response")
    if "kind" in parsed and parsed["kind"] != "storage#testIamPermissionsResponse":
        raise PreflightError("invalid_response")
    granted = parsed.get("permissions", [])
    if not isinstance(granted, list) or any(not isinstance(item, str) for item in granted):
        raise PreflightError("invalid_response")
    return {permission: permission in granted for permission in permissions}


def inspect(uri: str, protected: dict[str, str]) -> dict[str, object]:
    transfer_bucket, _ = _gs_uri(uri)
    payload = _read_transfer(uri)
    target, decoded = _verify_payload(payload, protected)
    del decoded
    project = target["project"]
    service_resource = f"projects/{project}/locations/{target['region']}/services/{target['service']}"
    results = {
        "state": "archived_state_verified",
        "preserved_file_count": len(PRESERVED_FILES),
        "absent_execution_state_count": len(ABSENT_STATES),
        "bucket_permissions": _bucket_permissions(
            transfer_bucket, ["storage.objects.get", "storage.objects.create"]
        ),
        "service_permissions": _permissions(
            f"https://run.googleapis.com/v2/{service_resource}:testIamPermissions",
            ["run.services.get", "run.services.update", "run.routes.invoke"],
            {"permissions": ["run.services.get", "run.services.update", "run.routes.invoke"]},
        ),
        "scheduler_permissions": _permissions(
            f"https://cloudresourcemanager.googleapis.com/v1/projects/{quote(project, safe='')}:testIamPermissions",
            ["cloudscheduler.jobs.get", "cloudscheduler.jobs.update", "cloudscheduler.jobs.run", "cloudscheduler.jobs.enable", "cloudscheduler.jobs.pause"],
            {"permissions": ["cloudscheduler.jobs.get", "cloudscheduler.jobs.update", "cloudscheduler.jobs.run", "cloudscheduler.jobs.enable", "cloudscheduler.jobs.pause"]},
        ),
    }
    return results


def main() -> int:
    global _SESSION
    try:
        from scripts.publish_account_facts_from_report import additional_target_environment

        if os.environ.get("IBKR_ACCOUNT_FACTS_TARGET") != "additional-1":
            raise PreflightError("target_mismatch")
        protected = additional_target_environment("additional-1")
        if protected.get("IBKR_ACCOUNT_FACTS_TARGET") != "additional-1":
            raise PreflightError("target_mismatch")
        uri = os.environ.get("IBKR_PAUSED_FACTS_STATE_TRANSFER_URI", "")
        _gs_uri(uri)
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        _SESSION = AuthorizedSession(credentials, max_refresh_attempts=0)
        _SESSION.mount("https://", HTTPAdapter(max_retries=0))
        result = inspect(uri, protected)
        print(json.dumps(result, sort_keys=True))
        return 0
    except PreflightError as exc:
        print(json.dumps({"state": "blocked", "reason": str(exc)}, sort_keys=True))
        return 1
    except Exception:
        print(json.dumps({"state": "blocked", "reason": "preflight_failed"}, sort_keys=True))
        return 1
    finally:
        if _SESSION is not None:
            _SESSION.close()


if __name__ == "__main__":
    raise SystemExit(main())
