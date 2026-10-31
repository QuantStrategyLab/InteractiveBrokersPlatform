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
URI = "gs://synthetic-runtime-reports/ibkr/2026-09/20260930T010000Z.json"


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
    assert result["snapshot_present"] is True
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
    assert result["historical_candidate"]["receiver_admission_verified"] is False
    assert len(result["historical_candidate"]["source_binding_id_sha256"]) == 64
    assert "older-revision-canary" not in json.dumps(result)


@pytest.mark.parametrize("revision", [None, "", " private-canary "])
def test_missing_or_invalid_original_revision_remains_skipped(revision):
    report = _report()
    if revision is None:
        del report["diagnostics"]["runtime_revision"]
    else:
        report["diagnostics"]["runtime_revision"] = revision
    result = archive.inspect_report(report, uri=URI, expected=EXPECTED, day=DAY)
    assert result["status"] == "skipped"
    assert result["reason"] == "runtime_target_mismatch"
    assert "historical_candidate" not in result
    assert "private-canary" not in json.dumps(result)


@pytest.mark.parametrize("mutation", ["account", "scope", "receipt", "facts", "date"])
def test_alternate_revision_preview_preserves_all_other_guards(mutation):
    report = _report()
    report["diagnostics"]["runtime_revision"] = "older-revision-canary"
    if mutation == "account":
        report["summary"]["account_facts"]["account_ids"] = ["U00000002"]
    elif mutation == "scope":
        report["account_scope"] = "different-scope"
    elif mutation == "receipt":
        report["runtime_release_receipt"]["runtime_revision"] = "contradictory-receipt"
    elif mutation == "facts":
        del report["summary"]["account_facts"]
    day = DAY + timedelta(days=1) if mutation == "date" else DAY
    result = archive.inspect_report(report, uri=URI, expected=EXPECTED, day=day)
    assert result["status"] == "skipped"
    assert "historical_candidate" not in result
    if mutation == "facts":
        assert result["snapshot_present"] is False
        assert result["historical_candidate_rejection"] == "account_facts_invalid"


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
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ARCHIVE_MODE", "month-metadata")
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
        archive.report_uris(day=DAY, expected=EXPECTED, now=NOW, whole_month=True)


@pytest.mark.parametrize("over_limit", [False, True])
def test_month_snapshot_inspection_retains_dates_and_read_bound(monkeypatch, capsys, over_limit):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz):
            return NOW

    monkeypatch.setattr(archive, "datetime", FixedDatetime)
    for key, value in {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
                       "IBKR_ACCOUNT_FACTS_TARGET": "additional-2",
                       "IBKR_ACCOUNT_FACTS_ARCHIVE_DATE": "2026-09-30",
                       "IBKR_ACCOUNT_FACTS_ARCHIVE_MODE": "month-snapshots"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(archive.identity, "resolve_expected", lambda: EXPECTED)
    uris = [URI] * 65 if over_limit else [URI, URI.replace("20260930", "20260929")]
    monkeypatch.setattr(archive, "report_uris", lambda **k: uris)
    reads = []

    def loaded(uri, **kwargs):
        reads.append(uri)
        return _report()

    monkeypatch.setattr(archive.publisher, "_load_gcs_report", loaded)
    assert archive.main() == (1 if over_limit else 0)
    result = json.loads(capsys.readouterr().out)
    if over_limit:
        assert result["reason"] == "archive_read_limit"
        assert reads == []
    else:
        assert len(reads) == 2
        assert result["records"][0]["observation_date"] == "2026-09-30"
        assert result["records"][1]["reason"] == "archive_observation_date_mismatch"


def test_month_inspection_does_not_read_todays_report_as_history(monkeypatch, capsys):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz):
            return NOW

    monkeypatch.setattr(archive, "datetime", FixedDatetime)
    for key, value in {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
                       "IBKR_ACCOUNT_FACTS_TARGET": "additional-2",
                       "IBKR_ACCOUNT_FACTS_ARCHIVE_DATE": "2026-10-23",
                       "IBKR_ACCOUNT_FACTS_ARCHIVE_MODE": "month-snapshots"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(archive.identity, "resolve_expected", lambda: EXPECTED)
    monkeypatch.setattr(archive, "report_uris", lambda **k: [URI.replace("2026-09/20260930", "2026-10/20261026")])
    monkeypatch.setattr(archive.publisher, "_load_gcs_report", lambda *a, **k: pytest.fail("today is not archive history"))
    assert archive.main() == 0
    assert json.loads(capsys.readouterr().out)["records"][0]["reason"] == "archive_date_outside_retention"


def test_unexpected_error_exports_no_exception_message(monkeypatch, capsys):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz):
            return NOW

    monkeypatch.setattr(archive, "datetime", FixedDatetime)
    for key, value in {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
                       "IBKR_ACCOUNT_FACTS_TARGET": "additional-2",
                       "IBKR_ACCOUNT_FACTS_ARCHIVE_DATE": "2026-09-30",
                       "IBKR_ACCOUNT_FACTS_ARCHIVE_MODE": "day"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(archive.identity, "resolve_expected", lambda: EXPECTED)
    monkeypatch.setattr(archive, "report_uris", lambda **k: [URI])
    monkeypatch.setattr(archive.publisher, "_load_gcs_report", lambda *a, **k: _report())

    def unexpected(*a, **k):
        raise KeyError("PRIVATE_FINANCIAL_CANARY")

    monkeypatch.setattr(archive, "inspect_report", unexpected)
    assert archive.main() == 1
    output = capsys.readouterr().out
    assert "PRIVATE_FINANCIAL_CANARY" not in output
    result = json.loads(output)
    assert result["error_type"] == "KeyError"
    assert result["report_name"] == "20260930T010000Z.json"


def test_profile_slot_is_unique_and_checked_before_archive_access(monkeypatch):
    fields = {'service_name': 'private-service', 'account_scope': 'private-scope',
              'account_selector': ('DU0000001',), 'deployment_selector': 'private-deployment'}
    slot = {'project_id': 'private-project', **fields}
    target = {**fields, 'account_selector': ['DU0000001']}
    monkeypatch.setattr(archive.binding, 'configuration', lambda env: (
        {'additional-2': slot}, {'global_etf_rotation': ('private-service', target)},
        'private-project', 'us-central1'))
    assert archive.resolve_profile_slot({}) == 'additional-2'
    monkeypatch.setattr(archive.binding, 'configuration', lambda env: (
        {'primary': slot}, {'global_etf_rotation': ('private-service', target)},
        'private-project', 'us-central1'))
    assert archive.resolve_profile_slot({}) == 'primary-live'
    slot['account_scope'] = 'different'
    with pytest.raises(archive.ArchiveError, match='archive_target_not_unique'):
        archive.resolve_profile_slot({})


def test_today_allowed_only_for_profile_archive():
    assert archive.archive_day('2026-10-26', now=NOW, allow_today=True) == NOW.date()
    with pytest.raises(archive.ArchiveError):
        archive.archive_day('2026-10-26', now=NOW)


def test_cash_metadata_has_only_allowed_tags_and_no_cash_values():
    result = archive.inspect_report(_report(), uri=URI, expected=EXPECTED, day=DAY)
    assert result['cash_record_count'] > 0
    assert set(result['usd_cash_source_tags']) <= archive.publisher._ALLOWED_CASH_TAGS
    assert result['observed_at']
    assert '12345.6700' not in json.dumps(result)
    assert '100.00' not in json.dumps(result)


def test_profile_workflow_protects_inventory_and_keeps_original_read_identity():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / '.github/workflows'
    caller = (root / 'inspect-account-facts-archive.yml').read_text()
    protected = (root / 'inspect-account-facts-archive-protected.yml').read_text()
    assert 'global_etf_rotation' in caller and 'russell_top50_leader_rotation' in caller
    assert 'vars.CLOUD_RUN_SERVICE_TARGETS_JSON || secrets.CLOUD_RUN_SERVICE_TARGETS_JSON' in caller
    assert '&& (vars.CLOUD_RUN_SERVICE_TARGETS_JSON' in caller
    assert 'CLOUD_RUN_SERVICE_TARGETS_JSON: ${{ secrets.CLOUD_RUN_SERVICE_TARGETS_JSON }}' in protected
    assert 'ACCOUNT_FACTS_RECOVERY_WIF_PROVIDER' in protected
    assert 'scripts.inspect_account_facts_archive' in protected
    assert 'scheduler ' not in protected and 'IBKR_FLEX_TOKEN' not in protected


def test_profile_mode_reads_only_selected_slot_and_current_day(monkeypatch, capsys):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz):
            return NOW

    monkeypatch.setattr(archive, 'datetime', FixedDatetime)
    monkeypatch.setenv('GITHUB_EVENT_NAME', 'workflow_dispatch')
    monkeypatch.setenv('GITHUB_REF', 'refs/heads/main')
    monkeypatch.setenv('IBKR_ACCOUNT_FACTS_TARGET', 'global_etf_rotation')
    monkeypatch.setenv('IBKR_ACCOUNT_FACTS_ARCHIVE_DATE', '2026-10-26')
    monkeypatch.setenv('IBKR_ACCOUNT_FACTS_ARCHIVE_MODE', 'day')
    monkeypatch.setattr(archive, 'resolve_profile_slot', lambda env: 'additional-2')
    monkeypatch.setattr(archive.identity, 'resolve_expected', lambda: EXPECTED)
    calls = []
    def list_one(**kwargs):
        calls.append(kwargs)
        return []
    monkeypatch.setattr(archive, 'report_uris', list_one)
    monkeypatch.setattr(archive.publisher, '_load_gcs_report', lambda *a, **k: pytest.fail('no reports'))
    assert archive.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result['profile'] == 'global_etf_rotation' and result['report_count'] == 0
    assert result['broker_requests'] == 0 and result['business_writes'] == 0
    assert len(calls) == 1 and calls[0]['day'] == NOW.date() and calls[0]['whole_month'] is False
    assert archive.os.environ['IBKR_ACCOUNT_FACTS_TARGET'] == 'additional-2'


def test_profile_mode_rejects_month_read_before_archive_access(monkeypatch, capsys):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz):
            return NOW
    monkeypatch.setattr(archive, 'datetime', FixedDatetime)
    monkeypatch.setenv('GITHUB_EVENT_NAME', 'workflow_dispatch')
    monkeypatch.setenv('GITHUB_REF', 'refs/heads/main')
    monkeypatch.setenv('IBKR_ACCOUNT_FACTS_TARGET', 'global_etf_rotation')
    monkeypatch.setenv('IBKR_ACCOUNT_FACTS_ARCHIVE_DATE', '2026-10-26')
    monkeypatch.setenv('IBKR_ACCOUNT_FACTS_ARCHIVE_MODE', 'month-snapshots')
    monkeypatch.setattr(archive, 'resolve_profile_slot', lambda env: 'additional-2')
    monkeypatch.setattr(archive.identity, 'resolve_expected', lambda: EXPECTED)
    monkeypatch.setattr(archive, 'report_uris', lambda **kw: pytest.fail('no listing'))
    assert archive.main() == 1
    assert json.loads(capsys.readouterr().out)['reason'] == 'archive_mode_invalid'
