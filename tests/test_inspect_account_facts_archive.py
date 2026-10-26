import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from scripts import inspect_account_facts_archive as archive
from tests.test_publish_ibkr_account_facts import _observability_publish_args, _report


NOW = datetime(2026, 10, 26, 16, tzinfo=timezone.utc)
DAY = archive.archive_day("2026-09-30", now=NOW)
EXPECTED = {key: value for key, value in _observability_publish_args().items()
            if key not in {"source_report_uri", "sync_url", "sync_token"}}
URI = "gs://qsl-runtime-reports/ibkr/2026-09/20260930T010000Z.json"


@pytest.mark.parametrize("value", ["2026-10-26", "2026-10-27", "2025-10-25", "2026-9-30", "secret-canary"])
def test_reject_nonhistorical_or_invalid_date(value):
    with pytest.raises(archive.ArchiveError):
        archive.archive_day(value, now=NOW)


def test_retention_inclusive_boundary():
    day = NOW.date() - timedelta(days=365)
    assert archive.archive_day(day.isoformat(), now=NOW) == day


def test_projection_retains_original_identity_and_exports_no_financial_body():
    result = archive.inspect_report(_report(), uri=URI, expected=EXPECTED, day=DAY)
    assert result["status"] == "verified"
    assert result["net_assets_available"] is True
    assert len(result["projection_sha256"]) == 64
    encoded = json.dumps(result)
    for private in ("U00000001", "12345.6700", "100.00", "-0.01", "runtime-revision-001"):
        assert private not in encoded


def test_revision_mismatch_is_not_rebound_for_history():
    report = _report()
    report["diagnostics"]["runtime_revision"] = "older-revision-canary"
    result = archive.inspect_report(report, uri=URI, expected=EXPECTED, day=DAY)
    assert result["status"] == "skipped"
    assert result["mismatched_fields"] == ["runtime_revision"]
    assert "older-revision-canary" not in json.dumps(result)


def test_original_observation_date_cannot_be_relabeled():
    result = archive.inspect_report(_report(), uri=URI, expected=EXPECTED,
                                    day=DAY + timedelta(days=1))
    assert result["reason"] == "archive_observation_date_mismatch"


@pytest.mark.parametrize("stdout,stderr,code,reason", [
    (URI.replace("/ibkr/", "/other/") + "\n", "", 0, "archive_listing_invalid"),
    (URI.replace("20260930", "20260929") + "\n", "", 0, "archive_listing_invalid"),
    ("", "permission denied with PRIVATE_CANARY", 1, "archive_listing_failed"),
    ("\n".join([URI] * 65), "", 0, "archive_listing_limit"),
])
def test_listing_fails_closed(monkeypatch, stdout, stderr, code, reason):
    monkeypatch.setattr(archive.subprocess, "run", lambda *a, **k:
                        SimpleNamespace(stdout=stdout, stderr=stderr, returncode=code))
    with pytest.raises(archive.ArchiveError, match=reason):
        archive.report_uris(day=DAY, expected=EXPECTED, now=NOW)


def test_successfully_empty_listing_is_distinct_from_failure(monkeypatch):
    monkeypatch.setattr(archive.subprocess, "run", lambda *a, **k:
                        SimpleNamespace(stdout="", stderr="One or more URLs matched no objects.", returncode=1))
    assert archive.report_uris(day=DAY, expected=EXPECTED, now=NOW) == []


def test_cli_is_manual_only_before_any_read(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setattr(archive, "report_uris", lambda **k: pytest.fail("unexpected cloud read"))
    assert archive.main() == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "manual_main_required"


def test_month_inventory_confines_listing_and_never_reads_financial_bodies(monkeypatch, capsys):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz):
            return NOW

    monkeypatch.setattr(archive, "datetime", FixedDatetime)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_TARGET", "additional-2")
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ARCHIVE_DATE", "2026-09-30")
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ARCHIVE_METADATA_ONLY", "true")
    monkeypatch.setattr(archive.identity, "resolve_expected", lambda: EXPECTED)
    monkeypatch.setattr(archive.publisher, "_load_gcs_report", lambda *a, **k: pytest.fail("financial read"))
    calls = []

    def listed(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stderr="", stdout=URI + "\n" + URI.replace("20260930", "20260929") + "\n")

    monkeypatch.setattr(archive.subprocess, "run", listed)
    assert archive.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["financial_bodies_read"] is False
    assert result["report_counts_by_filename_date"] == {"2026-09-29": 1, "2026-09-30": 1}
    assert calls[0][3] == EXPECTED["expected_report_prefix"] + "/2026-09/202609*.json"


def test_month_inventory_rejects_other_month(monkeypatch):
    monkeypatch.setattr(archive.subprocess, "run", lambda *a, **k:
                        SimpleNamespace(returncode=0, stderr="", stdout=URI.replace("2026-09/20260930", "2026-08/20260830") + "\n"))
    with pytest.raises(archive.ArchiveError, match="archive_listing_invalid"):
        archive.report_uris(day=DAY, expected=EXPECTED, now=NOW, metadata_only=True)
