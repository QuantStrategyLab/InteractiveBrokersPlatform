import base64
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from urllib.error import HTTPError
from io import BytesIO

import pytest

from scripts import publish_account_facts_archive as recovery
from scripts import publish_account_facts_from_report as publisher
from tests.test_publish_ibkr_account_facts import _report, _observability_publish_args


NOW = datetime(2026, 10, 26, 17, tzinfo=timezone.utc)
EXPECTED = {k: v for k, v in _observability_publish_args().items()
            if k not in {"source_report_uri", "sync_url", "sync_token"}}
EXPECTED["expected_account_selector"] = tuple(EXPECTED["expected_account_selector"])


def sample(day="2026-09-30"):
    report = _report()
    for key in ("started_at", "finished_at"):
        report[key] = report[key].replace("2026-09-30", day)
    report["summary"]["account_facts"]["observed_at"] = report["summary"]["account_facts"]["observed_at"].replace("2026-09-30", day)
    name = day.replace("-", "") + "T010000Z.json"
    uri = EXPECTED["expected_report_prefix"] + "/" + day[:7] + "/" + name
    payload = publisher.prepare_sync_payload(publisher.project_ibkr_account_facts_history(
        report, source_report_uri=uri, **EXPECTED))
    entry = {"report_name": name, "source_sha256": recovery.sha(payload["source_binding"]["id"]),
             "projection_sha256": recovery.sha(recovery.canonical(payload))}
    revision = {"metadata": {"name": "runtime-revision-001",
                            "labels": {"serving.knative.dev/service": EXPECTED["expected_service_name"]}},
                "spec": {"containers": [{"image": "us-central1-docker.pkg.dev/example-project/runtime/worker@sha256:" + "a" * 64,
                    "env": [{"name": "RUNTIME_TARGET_JSON", "value": json.dumps({
                        "service_name": EXPECTED["expected_service_name"],
                        "account_scope": EXPECTED["expected_account_scope"],
                        "account_selector": EXPECTED["expected_account_selector"],
                        "deployment_selector": EXPECTED["expected_deployment_selector"]})}]}]}}
    return report, entry, payload, revision


def test_qualify_preserves_original_observation_and_amounts():
    report, entry, payload, revision = sample()
    selected = recovery.manifest(json.dumps([entry]), expected=EXPECTED, now=NOW)[0]
    assert recovery.qualify(report, selected, expected=EXPECTED, revision=revision) == payload


@pytest.mark.parametrize("field", ["account", "scope", "revision", "image", "projection", "source", "date", "receipt"])
def test_mismatched_archive_or_deployment_cannot_qualify(field):
    report, entry, _, revision = sample()
    entry = recovery.manifest(json.dumps([entry]), expected=EXPECTED, now=NOW)[0]
    if field == "account":
        report["summary"]["account_facts"]["account_ids"] = ["U00000002"]
    elif field == "scope":
        revision["spec"]["containers"][0]["env"][0]["value"] = "{}"
    elif field == "revision":
        revision["metadata"]["name"] = "different-revision"
    elif field == "image":
        revision["spec"]["containers"][0]["image"] = "unattested-image"
    elif field == "projection":
        entry["projection_sha256"] = "b" * 64
    elif field == "source":
        entry["source_sha256"] = "b" * 64
    elif field == "date":
        entry["report_name"] = "20260929T010000Z.json"
    else:
        report["runtime_release_receipt"]["runtime_revision"] = "different-receipt"
    with pytest.raises(recovery.ArchivePublishError):
        recovery.qualify(report, entry, expected=EXPECTED, revision=revision)


@pytest.mark.parametrize("kind", ["duplicate", "extra", "traversal", "today", "oversized"])
def test_manifest_rejects_scope_expansion(kind):
    _, entry, _, _ = sample()
    rows = [entry]
    if kind == "duplicate":
        rows *= 2
    elif kind == "extra":
        entry["account_key"] = "forbidden"
    elif kind == "traversal":
        entry["report_name"] = "../20260930T010000Z.json"
    elif kind == "today":
        entry["report_name"] = "20261026T010000Z.json"
    else:
        rows *= 32
    with pytest.raises(recovery.ArchivePublishError, match="archive_manifest_invalid"):
        recovery.manifest(json.dumps(rows), expected=EXPECTED, now=NOW)


def test_history_request_preserves_current_source_and_target():
    _, _, payload, _ = sample()
    payload["source_binding"]["id"] = "b" * 64
    request = recovery.source_request([payload, deepcopy(payload)], expected=EXPECTED)
    assert request["target_id"] == EXPECTED["target_id"]
    assert request["previous_source_binding_id"] == request["next_source_binding_id"]
    assert request["history_source_binding_ids"] == ["b" * 64]


def ack(payload):
    return {"ok": True, "stored": True, "historical": True, "latest_unchanged": True,
            "platform": "ibkr", "account_key": "test-key", "target_id": payload["target_id"],
            "observation_date": payload["observation_date"],
            "observed_finished_at": payload["observed_finished_at"], "unchanged": False}


def opener(monkeypatch, result, requests):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            return json.dumps(result).encode()

    def opened(request, **kwargs):
        requests.append(request)
        return Response()

    monkeypatch.setattr(recovery, "build_opener", lambda *a: SimpleNamespace(open=opened))


def test_append_uses_only_history_endpoint_and_checks_exact_ack(monkeypatch):
    _, _, payload, _ = sample()
    requests = []
    opener(monkeypatch, ack(payload), requests)
    result = recovery.append_once(payload, token="TOKEN_CANARY", account_key="test-key")
    assert result["history_only_ack_verified"] is True
    assert len(requests) == 1
    assert requests[0].full_url.endswith("/api/account-facts/history/sync")
    assert json.loads(requests[0].data) == payload


@pytest.mark.parametrize("field,value", [("ok", 1), ("stored", False), ("historical", False),
    ("latest_unchanged", False), ("account_key", "another-key"), ("target_id", "other"),
    ("observation_date", "2026-09-29"), ("observed_finished_at", "2026-09-29T01:00:01+00:00"),
    ("unchanged", "false")])
def test_wrong_ack_stops_with_unknown_outcome_no_retry(monkeypatch, field, value):
    _, _, payload, _ = sample()
    result = ack(payload)
    result[field] = value
    requests = []
    opener(monkeypatch, result, requests)
    with pytest.raises(recovery.ArchivePublishError, match="archive_sync_outcome_unknown"):
        recovery.append_once(payload, token="TOKEN_CANARY", account_key="test-key")
    assert len(requests) == 1


def test_conflict_exports_only_fixed_error_and_never_retries(monkeypatch):
    _, _, payload, _ = sample()
    calls = []

    def failed(request, **kwargs):
        calls.append(request)
        raise HTTPError(request.full_url, 409, "PRIVATE_ERROR_CANARY", {},
                        BytesIO(b'{"error":"account_facts_daily_conflict","private":"PRIVATE_AMOUNT"}'))

    monkeypatch.setattr(recovery, "build_opener", lambda *a: SimpleNamespace(open=failed))
    with pytest.raises(recovery.ArchivePublishError) as caught:
        recovery.append_once(payload, token="TOKEN_CANARY", account_key="test-key")
    assert caught.value.safe == {"http_status": 409, "error_code": "account_facts_daily_conflict"}
    assert len(calls) == 1
    assert "PRIVATE" not in str(caught.value) + json.dumps(caught.value.safe)


def test_sealed_source_request_exports_no_plaintext(monkeypatch):
    monkeypatch.setattr(recovery, "_seal_secret", lambda key, value: b"encrypted-by-approved-client")
    result = recovery.seal_request({"target_id": "PRIVATE_TARGET"}, {
        "receiver": {"key": base64.b64encode(b"a" * 32).decode(), "key_id": "test-key-id"}})
    assert "PRIVATE_TARGET" not in json.dumps(result)
    assert result["name"] == "IBKR_ACCOUNT_FACTS_SOURCE_ROTATION_JSON"


def test_default_preview_never_posts_even_when_token_exists(monkeypatch, capsys):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz):
            return NOW

    report, entry, _, revision = sample()
    for key, value in {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
            "GITHUB_RUN_ATTEMPT": "1", "IBKR_ARCHIVE_ACTION": "preview",
            "IBKR_ACCOUNT_FACTS_TARGET": "additional-2", "IBKR_ACCOUNT_FACTS_SYNC_TOKEN": "TOKEN_CANARY",
            "IBKR_PERIOD_RETURN_ADDITIONAL_ACCOUNT_KEYS_JSON": '{"additional-2":"test-key"}',
            "IBKR_ARCHIVE_REPORTS_JSON": json.dumps([entry])}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(recovery, "datetime", FixedDatetime)
    monkeypatch.setattr(recovery.identity, "resolve_expected", lambda: EXPECTED)
    monkeypatch.setattr(recovery.publisher, "_load_gcs_report", lambda *a, **k: report)
    monkeypatch.setattr(recovery, "revision_metadata", lambda *a, **k: revision)
    monkeypatch.setattr(recovery, "append_once", lambda *a, **k: pytest.fail("preview must not POST"))
    assert recovery.main() == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["status"] == "previewed" and result["business_writes"] == 0
    for private in ("TOKEN_CANARY", "12345.6700", "U00000001", "test-key"):
        assert private not in output


def test_every_selected_day_qualifies_before_first_post(monkeypatch, capsys):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz):
            return NOW

    report1, entry1, _, revision = sample("2026-09-29")
    report2, entry2, _, _ = sample()
    report2["summary"]["account_facts"]["account_ids"] = ["U00000002"]
    for key, value in {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
            "GITHUB_RUN_ATTEMPT": "1", "IBKR_ARCHIVE_ACTION": "publish",
            "IBKR_ACCOUNT_FACTS_TARGET": "additional-2",
            "IBKR_PERIOD_RETURN_ADDITIONAL_ACCOUNT_KEYS_JSON": '{"additional-2":"test-key"}',
            "IBKR_ARCHIVE_REPORTS_JSON": json.dumps([entry1, entry2])}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(recovery, "datetime", FixedDatetime)
    monkeypatch.setattr(recovery.identity, "resolve_expected", lambda: EXPECTED)
    monkeypatch.setattr(recovery.publisher, "_load_gcs_report", lambda uri, **k:
                        report1 if "20260929" in uri else report2)
    monkeypatch.setattr(recovery, "revision_metadata", lambda *a, **k: revision)
    monkeypatch.setattr(recovery, "append_once", lambda *a, **k: pytest.fail("must prequalify all days"))
    assert recovery.main() == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "archive_projection_rejected"
