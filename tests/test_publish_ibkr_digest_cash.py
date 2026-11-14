import json
from datetime import date

import pytest
from google.api_core.exceptions import Forbidden, NotFound, PreconditionFailed

from application.ibkr_flex_source import FlexReport, FlexSourceError
from scripts import publish_ibkr_digest_cash as publisher
from scripts.publish_ibkr_digest_cash import _publish_cash_object, project_digest_cash


BINDINGS = {f"U000000{i}": (f"slot-{i}", f"opaque-{i}", f"target-{i}") for i in range(4)}


def _report(*, extra=False, wrong_cash=False):
    accounts = [*BINDINGS, "U0000099"] if extra else list(BINDINGS)
    statements = []
    for i, account in enumerate(accounts):
        cash_account = accounts[0] if wrong_cash and i == 2 else account
        statements.append(
            f'<FlexStatement accountId="{account}" fromDate="20261015" toDate="20261015">'
            f'<CashReport><CashReportCurrency accountId="{cash_account}" currency="USD" model="" '
            f'fromDate="20261015" toDate="20261015" endingCash="500" endingSettledCash="-{i}.50"/></CashReport>'
            '<Trades><Trade accountId="U0000099"/></Trades></FlexStatement>'
        )
    raw = ('<FlexQueryResponse type="AF"><FlexStatements>' + ''.join(statements)
           + '</FlexStatements></FlexQueryResponse>').encode()
    return FlexReport(content_sha256="a" * 64, content=raw)


def _project(report, latest=date(2026, 10, 15)):
    return project_digest_cash(report, expected_primary=(next(iter(BINDINGS)),), allowed_accounts=tuple(BINDINGS),
                               bindings=BINDINGS, latest_day=latest)


def test_projects_each_cash_segment_without_broker_account_ids():
    result = _project(_report())
    assert set(result) == {f"slot-{i}" for i in range(4)}
    assert result["slot-3"] == {"schema_version": "ibkr_digest_settled_cash.v1", "target_id": "target-3",
                                "opaque_account_uid": "opaque-3", "statement_date": "2026-10-15",
                                "currency": "USD", "amount": "-3.50"}
    assert "U0000" not in repr(result)


def test_bad_cash_binding_only_excludes_affected_account_and_future_dates_are_ignored():
    assert set(_project(_report(wrong_cash=True))) == {"slot-0", "slot-1", "slot-3"}
    assert _project(_report(), date(2026, 10, 14)) == {}
    with pytest.raises(FlexSourceError):
        _project(_report(extra=True))


def test_multiday_report_uses_the_period_end_date():
    original = _report()
    raw = original.content.replace(b'fromDate="20261015"', b'fromDate="20261001"')
    result = _project(FlexReport(content_sha256="a" * 64, content=raw))
    assert len(result) == 4
    assert {record["statement_date"] for record in result.values()} == {"2026-10-15"}


class _MemoryBucket:
    def __init__(self):
        self.objects = {}
        self.fail_latest = False

    def blob(self, key):
        return _MemoryBlob(self, key)


class _MemoryBlob:
    def __init__(self, bucket, key):
        self.bucket = bucket
        self.key = key
        self.metadata = None

    def reload(self, **kwargs):
        if self.key not in self.bucket.objects:
            raise NotFound("missing")
        self.generation, _, self.metadata = self.bucket.objects[self.key]

    def download_as_bytes(self, *, if_generation_match, **kwargs):
        assert self.generation == if_generation_match
        return self.bucket.objects[self.key][1].encode()

    def upload_from_string(self, body, *, if_generation_match, **kwargs):
        generation = self.bucket.objects.get(self.key, (0,))[0]
        if generation != if_generation_match or (self.bucket.fail_latest and "/versions/" not in self.key):
            raise PreconditionFailed("generation changed")
        self.bucket.objects[self.key] = (generation + 1, body, self.metadata)


def test_same_statement_date_revision_preserves_both_sources_and_legacy_value():
    bucket = _MemoryBucket()
    prefix, slot = "test/ibkr/flex-cash", "slot-0"
    latest = f"{prefix}/{slot}.json"
    old = _project(_report())[slot]
    revised = {**old, "amount": "23.40"}
    bucket.objects[latest] = (7, json.dumps(old), None)
    version1, version2 = "a" * 64, "b" * 64

    assert _publish_cash_object(bucket, prefix, slot, old, version1)
    bucket.fail_latest = True
    with pytest.raises(publisher.DigestCashStageError) as raised:
        _publish_cash_object(bucket, prefix, slot, revised, version2)
    assert publisher._safe_failure(raised.value) == {
        "settled_cash_status": "failed", "stage": "latest_write",
        "error_kind": "upstream_request_failed", "http_status": 412,
    }
    assert json.loads(bucket.objects[latest][1]) == old

    bucket.fail_latest = False
    assert _publish_cash_object(bucket, prefix, slot, revised, version2)
    assert _publish_cash_object(bucket, prefix, slot, revised, version2)
    assert bucket.objects[latest][0] == 9
    assert bucket.objects[latest][2] == {"flex_source_sha256": version2}
    assert json.loads(bucket.objects[latest][1]) == revised
    for version, content in (("legacy-gcs-7", old), (version1, old), (version2, revised)):
        key = f"{prefix}/versions/{slot}/2026-10-15/{version}.json"
        generation, body, _ = bucket.objects[key]
        assert generation == 1
        assert json.loads(body) == content

    older = {**old, "statement_date": "2026-10-14"}
    assert not _publish_cash_object(bucket, prefix, slot, older, "c" * 64)
    assert json.loads(bucket.objects[latest][1]) == revised


def test_main_logs_only_safe_flex_fields(monkeypatch, capsys):
    primary, *_ = BINDINGS
    for key, value in (
        ("IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON", json.dumps([primary])),
        ("IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON", json.dumps([primary])),
        ("IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON", "{}"),
        ("IBKR_FLEX_TOKEN", "private-token"),
        ("IBKR_FLEX_QUERY_ID", "12345"),
    ):
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(publisher, "parse_additional_target_configs", lambda _: {
        slot: {"account_selector": [account]} for account, (slot, _, _) in BINDINGS.items()
        if account != primary
    })
    error = FlexSourceError("private vendor message", endpoint="SendRequest", category="provider", code="1012")
    error.safe_diagnostic["cash_report_accounts"] = [primary]

    def fail(**kwargs):
        raise error

    monkeypatch.setattr(publisher, "fetch_activity_flex_xml", fail)
    assert publisher.main() == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "settled_cash_status": "failed", "stage": "flex_read",
        "source_endpoint": "SendRequest", "source_failure_kind": "provider", "provider_error_code": "1012",
    }


def test_archive_permission_failure_is_distinct_from_flex(monkeypatch):
    def forbidden(self, body, **kwargs):
        raise Forbidden("private bucket and object name")

    monkeypatch.setattr(_MemoryBlob, "upload_from_string", forbidden)
    with pytest.raises(publisher.DigestCashStageError) as raised:
        _publish_cash_object(_MemoryBucket(), "private/prefix", "slot-0", _project(_report())["slot-0"], "a" * 64)
    assert publisher._safe_failure(raised.value) == {
        "settled_cash_status": "failed", "stage": "archive_write",
        "error_kind": "upstream_request_failed", "http_status": 403,
    }
