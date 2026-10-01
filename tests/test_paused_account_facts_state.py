from __future__ import annotations

import base64
import hashlib
import json

import pytest

from scripts import inspect_paused_account_facts_state as inspector


def _fixture():
    protected = {
        "IBKR_ACCOUNT_FACTS_TARGET": "additional-1",
        "IBKR_ACCOUNT_FACTS_PROJECT_ID": "fixture-project",
        "IBKR_ACCOUNT_FACTS_SERVICE_NAME": "fixture-service",
        "IBKR_ACCOUNT_FACTS_RUNTIME_REVISION": "published-revision",
        "IBKR_ACCOUNT_FACTS_ACCOUNT_SCOPE": "fixture-scope",
        "IBKR_ACCOUNT_FACTS_DEPLOYMENT_SELECTOR": "fixture-deployment",
        "IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON": '["fixture-account"]',
        "IBKR_ACCOUNT_FACTS_REPORT_PREFIX": "gs://fixture-bucket/reports/ibkr",
    }
    context = {
        "project": "fixture-project",
        "region": "us-central1",
        "service": "fixture-service",
        "service_resource": "projects/fixture-project/locations/us-central1/services/fixture-service",
        "service_base": "https://fixture-service-abc-uc.a.run.app",
        "production_revision": "production-revision",
        "old_revision": "old-revision",
        "old_source": "a" * 40,
        "allowed_candidate_source": "b" * 40,
        "oidc": "oidc-configured",
        "report_prefix": "gs://fixture-bucket/reports/ibkr",
        "expected_job": {
            role: {"name": f"fixture-{role}", "schedule": "daily", "method": "POST", "path": "/run", "deadline": "60s"}
            for role in ("main", "precheck", "warmup")
        },
    }
    binding = {
        "account_selector": "fixture-account",
        "account_scope": "fixture-scope",
        "deployment_selector": "fixture-deployment",
        "project_id": "fixture-project",
        "runtime_revision": "fixture-service-production-revision",
        "service_name": "fixture-service",
        "source_binding_id": "fixture-source-binding",
    }
    context["production_revision"] = binding["runtime_revision"]
    context["old_revision"] = "fixture-service-old-revision"
    files = {
        "candidate-prep-0/paused-inputs.json": {
            "target_context": context,
            "image_digest": "c" * 64,
            "account_binding": binding,
            "paused_prior_run": {"observed": True},
            "selected_slot": "additional-1",
            "old_target": {"preserved": True},
            "live_jobs": {},
        },
        "candidate-prep-0/gateway-connection-dispatch-once.json": {"state": "preserved"},
        "candidate-prep-0/gateway-connection-readback.json": {"state": "preserved"},
    }
    preserved = {}
    for name, value in files.items():
        body = json.dumps(value, sort_keys=True).encode()
        preserved[name] = {
            "sha256": hashlib.sha256(body).hexdigest(),
            "body_base64": base64.b64encode(body).decode(),
        }
    payload = {
        "target_ordinal": 0,
        "inspected_at": "2026-10-02T00:00:00Z",
        "source_root": "/tmp/old-private-state",
        "absent_execution_state_names": sorted(inspector.ABSENT_STATES),
        "preserved_files": preserved,
    }
    return payload, protected


class _Response:
    def __init__(self, data, status=200):
        self.content = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.status_code = status
        self.closed = False

    def close(self):
        self.closed = True

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset : offset + chunk_size]


class _Session:
    def __init__(self, object_payload):
        self.payload = object_payload
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "GET" and "/iam/testPermissions" in url:
            return _Response({
                "kind": "storage#testIamPermissionsResponse",
                "permissions": ["storage.objects.get", "storage.objects.create"],
            })
        if method == "GET":
            return _Response(json.dumps(self.payload).encode())
        return _Response({"permissions": kwargs["json"]["permissions"]})


class _BackupSession:
    def __init__(self, object_bytes, *, existing=False, upload_error=False, readback_error=False):
        self.object_bytes = object_bytes
        self.existing = existing
        self.upload_error = upload_error
        self.readback_error = readback_error
        self.uploaded = None
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "GET" and url.endswith("/o/state.json?alt=media"):
            return _Response(self.object_bytes)
        if method == "GET" and url.endswith("/o/private%2Fstate%2Farchived-handover.json"):
            return _Response(b"{}", status=200 if self.existing else 404)
        if method == "POST" and "/upload/storage/v1/b/" in url:
            if self.upload_error:
                raise TimeoutError("synthetic timeout")
            self.uploaded = kwargs["data"]
            return _Response({"kind": "storage#object"})
        if method == "GET" and url.endswith("/o/private%2Fstate%2Farchived-handover.json?alt=media"):
            if self.readback_error:
                return _Response(b"", status=503)
            return _Response(self.uploaded)
        raise AssertionError("unexpected request")


def _transfer_bytes(payload):
    return json.dumps(payload).encode()


def test_inspection_uses_only_expected_read_and_permission_calls(monkeypatch):
    payload, protected = _fixture()
    session = _Session(payload)
    monkeypatch.setattr(inspector, "_SESSION", session)

    result = inspector.inspect("gs://fixture-transfer/state.json", protected)

    assert result["state"] == "archived_state_verified"
    assert result["preserved_file_count"] == 3
    assert result["absent_execution_state_count"] == 5
    assert [call[0] for call in session.calls] == ["GET", "GET", "POST", "POST"]
    assert all(call[2]["allow_redirects"] is False for call in session.calls)
    assert all(call[2]["timeout"] == 20 for call in session.calls)
    assert all(call[2]["stream"] is True for call in session.calls)
    assert all(method == "GET" or url.endswith(":testIamPermissions") for method, url, _ in session.calls)
    assert all("fixture-account" not in url for _, url, _ in session.calls)
    assert session.calls[0][1] == "https://storage.googleapis.com/storage/v1/b/fixture-transfer/o/state.json?alt=media"
    assert session.calls[1][1] == "https://storage.googleapis.com/storage/v1/b/fixture-transfer/iam/testPermissions?permissions=storage.objects.get&permissions=storage.objects.create"
    assert "json" not in session.calls[1][2]
    assert "data" not in session.calls[1][2]
    assert session.calls[2][1] == "https://run.googleapis.com/v2/projects/fixture-project/locations/us-central1/services/fixture-service:testIamPermissions"
    assert session.calls[3][1] == "https://cloudresourcemanager.googleapis.com/v1/projects/fixture-project:testIamPermissions"
    assert all("run.app" not in url and "scheduler.googleapis.com" not in url for _, url, _ in session.calls)
    assert set(result) == {
        "state", "preserved_file_count", "absent_execution_state_count",
        "bucket_permissions", "service_permissions", "scheduler_permissions",
    }
    assert all(
        method == "GET" or url.endswith(":testIamPermissions")
        for method, url, _ in session.calls
    )


@pytest.mark.parametrize("uri", [
    "https://example.invalid/state", "gs://user:pass@bucket/path", "gs://bucket/a/../state",
    "gs://bucket/path?alt=media", "gs://bucket/path#fragment",
])
def test_rejects_invalid_transfer_uri(uri):
    with pytest.raises(inspector.PreflightError):
        inspector._gs_uri(uri)


@pytest.mark.parametrize("mutate", [
    lambda payload: payload.update(target_ordinal=1),
    lambda payload: payload.update(absent_execution_state_names=[]),
    lambda payload: payload.update(source_root="relative/private/state"),
    lambda payload: payload["preserved_files"]["candidate-prep-0/paused-inputs.json"].update(sha256="0" * 64),
    lambda payload: payload["preserved_files"].pop("candidate-prep-0/gateway-connection-readback.json"),
])
def test_rejects_malformed_or_wrong_source_state(mutate):
    payload, protected = _fixture()
    mutate(payload)
    with pytest.raises(inspector.PreflightError):
        inspector._verify_payload(payload, protected)


def test_rejects_protected_target_binding_mismatch():
    payload, protected = _fixture()
    inputs = json.loads(base64.b64decode(payload["preserved_files"]["candidate-prep-0/paused-inputs.json"]["body_base64"]))
    inputs["account_binding"]["service_name"] = "different-service"
    body = json.dumps(inputs, sort_keys=True).encode()
    payload["preserved_files"]["candidate-prep-0/paused-inputs.json"] = {
        "sha256": hashlib.sha256(body).hexdigest(), "body_base64": base64.b64encode(body).decode()
    }
    with pytest.raises(inspector.PreflightError, match="target_mismatch"):
        inspector._verify_payload(payload, protected)


def test_stops_on_first_http_failure(monkeypatch):
    payload, protected = _fixture()

    class FailedSession(_Session):
        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return _Response(b"private response body", status=503)

    session = FailedSession(payload)
    monkeypatch.setattr(inspector, "_SESSION", session)
    with pytest.raises(inspector.PreflightError, match="http_failed"):
        inspector.inspect("gs://fixture-transfer/state.json", protected)
    assert len(session.calls) == 1


@pytest.mark.parametrize("status,body,reason", [
    (302, b"", "http_failed"),
    (200, b"x" * (inspector.MAX_OBJECT_BYTES + 1), "response_too_large"),
])
def test_rejects_redirect_and_oversized_transfer_response(monkeypatch, status, body, reason):
    payload, protected = _fixture()

    class ResponseSession(_Session):
        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return _Response(body, status=status)

    monkeypatch.setattr(inspector, "_SESSION", ResponseSession(payload))
    with pytest.raises(inspector.PreflightError, match=reason):
        inspector.inspect("gs://fixture-transfer/state.json", protected)


def test_backup_stops_when_exact_marker_already_exists(monkeypatch):
    payload, protected = _fixture()
    session = _BackupSession(_transfer_bytes(payload), existing=True)
    monkeypatch.setattr(inspector, "_SESSION", session)

    result = inspector.backup_handover_state(
        "gs://fixture-transfer/state.json", "gs://fixture-transfer/private/state/", protected
    )

    assert result == {"state": "blocked", "phase": "backup_check", "reason": "backup_already_exists"}
    assert [method for method, _, _ in session.calls] == ["GET", "GET"]
    assert not any(method == "POST" for method, _, _ in session.calls)


def test_backup_first_attempt_is_create_only_and_reads_back_original_bytes(monkeypatch):
    payload, protected = _fixture()
    original = _transfer_bytes(payload)
    session = _BackupSession(original)
    monkeypatch.setattr(inspector, "_SESSION", session)

    result = inspector.backup_handover_state(
        "gs://fixture-transfer/state.json", "gs://fixture-transfer/private/state/", protected
    )

    assert result == {"state": "backup_verified", "phase": "readback_verified", "reason": "none"}
    assert session.uploaded == original
    assert [method for method, _, _ in session.calls] == ["GET", "GET", "POST", "GET"]
    assert session.calls[1][1] == "https://storage.googleapis.com/storage/v1/b/fixture-transfer/o/private%2Fstate%2Farchived-handover.json"
    upload = session.calls[2]
    assert upload[1] == "https://storage.googleapis.com/upload/storage/v1/b/fixture-transfer/o?uploadType=media&name=private%2Fstate%2Farchived-handover.json&ifGenerationMatch=0"
    assert upload[2]["data"] == original
    assert upload[2]["headers"] == {"Content-Type": "application/json"}
    assert upload[2]["allow_redirects"] is False
    assert session.calls[3][1] == "https://storage.googleapis.com/storage/v1/b/fixture-transfer/o/private%2Fstate%2Farchived-handover.json?alt=media"


def test_backup_upload_outcome_unknown_is_not_retried(monkeypatch):
    payload, protected = _fixture()
    session = _BackupSession(_transfer_bytes(payload), upload_error=True)
    monkeypatch.setattr(inspector, "_SESSION", session)

    result = inspector.backup_handover_state(
        "gs://fixture-transfer/state.json", "gs://fixture-transfer/private/state/", protected
    )

    assert result == {"state": "unknown", "phase": "upload", "reason": "upload_outcome_unknown"}
    assert [method for method, _, _ in session.calls] == ["GET", "GET", "POST"]


def test_backup_readback_error_remains_unknown_without_reupload(monkeypatch):
    payload, protected = _fixture()
    session = _BackupSession(_transfer_bytes(payload), readback_error=True)
    monkeypatch.setattr(inspector, "_SESSION", session)

    result = inspector.backup_handover_state(
        "gs://fixture-transfer/state.json", "gs://fixture-transfer/private/state/", protected
    )

    assert result == {"state": "unknown", "phase": "readback", "reason": "readback_unknown"}
    assert [method for method, _, _ in session.calls] == ["GET", "GET", "POST", "GET"]


def test_backup_readback_mismatch_remains_unknown_without_reupload(monkeypatch):
    payload, protected = _fixture()
    session = _BackupSession(_transfer_bytes(payload))
    session.readback_error = False
    session.readback_bytes = b"different bytes"
    original_request = session.request

    def request(method, url, **kwargs):
        response = original_request(method, url, **kwargs)
        if method == "GET" and url.endswith("/o/private%2Fstate%2Farchived-handover.json?alt=media"):
            return _Response(session.readback_bytes)
        return response

    session.request = request
    monkeypatch.setattr(inspector, "_SESSION", session)

    result = inspector.backup_handover_state(
        "gs://fixture-transfer/state.json", "gs://fixture-transfer/private/state/", protected
    )

    assert result == {"state": "unknown", "phase": "readback", "reason": "readback_mismatch"}
    assert [method for method, _, _ in session.calls] == ["GET", "GET", "POST", "GET"]


@pytest.mark.parametrize("prefix", [
    "gs://other-transfer/private/state/", "gs://fixture-transfer/private/state",
    "gs://fixture-transfer/private/../state/", "gs://fixture-transfer/private/state/?generation=1",
    "gs://fixture-transfer/",
])
def test_backup_rejects_invalid_prefix_before_any_cloud_request(monkeypatch, prefix):
    payload, protected = _fixture()
    session = _BackupSession(_transfer_bytes(payload))
    monkeypatch.setattr(inspector, "_SESSION", session)

    with pytest.raises(inspector.PreflightError, match="invalid_backup_prefix"):
        inspector.backup_handover_state("gs://fixture-transfer/state.json", prefix, protected)
    assert session.calls == []


def test_action_workflow_is_manual_main_only_and_separate_from_existing_paths():
    from pathlib import Path

    workflow = Path(".github/workflows/execution-report-heartbeat.yml").read_text()
    assert "default: disabled" in workflow
    assert "- paused-refresh-preflight" in workflow
    assert "- paused-refresh-state-backup" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "inputs.account_facts_target == 'paused-refresh-preflight'" in workflow
    assert "inputs.account_facts_target == 'paused-refresh-state-backup'" in workflow
    assert "IBKR_PAUSED_FACTS_STATE_TRANSFER_URI: ${{ secrets.IBKR_PAUSED_FACTS_STATE_TRANSFER_URI }}" in workflow
    assert "IBKR_PAUSED_FACTS_STATE_PREFIX" in workflow
    assert "secrets.IBKR_PAUSED_FACTS_STATE_PREFIX" in workflow
    assert "script='" not in workflow
    assert "inputs.account_facts_target != 'paused-refresh-preflight'" in workflow
    assert "inputs.account_facts_target != 'paused-refresh-state-backup'" in workflow
    assert "Publish one validated report" in workflow
    assert "name: Check execution report heartbeat" in workflow
    publisher_condition = workflow.split("  account-facts-publisher:", 1)[1].split("  paused-refresh-preflight:", 1)[0]
    assert "paused-refresh-preflight" not in publisher_condition
    assert "paused-refresh-state-backup" not in publisher_condition
    ingress_condition = workflow.split("  account-facts-ingress-diagnostic:", 1)[1]
    assert "inputs.account_facts_target == 'ingress-diagnostic'" in ingress_condition
    assert "inputs.account_facts_target == 'paused-refresh-state-backup'" not in ingress_condition
