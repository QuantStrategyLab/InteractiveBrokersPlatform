"""Synthetic read-only source selection; no real cloud, broker or sync calls."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from scripts import diagnose_account_facts_identity as diagnostic
from scripts import publish_account_facts_from_report as publisher
from tests.test_diagnose_account_facts_identity import NOW, PRIVATE, _environment, _metadata, _report


@pytest.fixture
def configured(monkeypatch):
    _environment(monkeypatch)
    monkeypatch.delenv("IBKR_ACCOUNT_FACTS_REPORT_NAME", raising=False)
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_SYNC_URL", publisher.IBKR_ACCOUNT_FACTS_SYNC_URL)
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_SYNC_TOKEN", "synthetic-token")
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_INCLUDE_POSITIONS", "true")
    expected = diagnostic.resolve_expected()
    report = _report(expected)
    facts = report["summary"]["account_facts"]
    facts.update(broker_reported_positions=[], broker_reported_positions_scope="stocks_only",
                 broker_reported_positions_complete=True)
    def forbidden(*_args, **_kwargs):
        pytest.fail("unmocked external capability")
    monkeypatch.setattr(publisher.subprocess, "run", forbidden)
    monkeypatch.setattr(publisher, "build_opener", forbidden)
    monkeypatch.setattr(diagnostic, "_read_cloud_run_metadata", lambda _expected: _metadata(expected))
    return expected, report


def closed(report):
    result = deepcopy(report)
    result.update(status="skipped", summary={})
    result["diagnostics"]["skip_reason"] = "market_closed"
    return result


def readers(monkeypatch, expected, reports):
    prefix = expected["expected_report_prefix"] + "/2026-10/"
    uris = [prefix + (NOW - timedelta(minutes=1, seconds=index)).strftime("%Y%m%dT%H%M%SZ.json")
            for index in range(len(reports))]
    reads = []
    monkeypatch.setattr(publisher, "_latest_report_uri", lambda **_kw: uris[0])
    monkeypatch.setattr(publisher, "_report_candidate_uris", lambda **_kw: uris)
    def load(uri, **_kw):
        reads.append(uri)
        value = reports[uris.index(uri)]
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)
    monkeypatch.setattr(publisher, "_load_gcs_report", load)
    return uris, reads


def select(expected, activity=None, **kwargs):
    return publisher.select_account_facts_report(expected=expected, now=NOW,
        activity=activity if activity is not None else {}, **kwargs)


def test_normal_market_closed_does_not_hide_snapshot_or_change_time(monkeypatch, configured):
    expected, report = configured
    uris, reads = readers(monkeypatch, expected, [closed(report), report])
    activity = {}
    uri, selected = select(expected, activity)
    assert uri == uris[1] and selected == report and reads == uris
    facts = publisher.project_ibkr_account_facts_history(selected, source_report_uri=uri, **expected)
    assert facts["broker_reported_positions"] == [] and facts["broker_reported_positions_complete"] is True
    assert facts["observed_finished_at"] == report["summary"]["account_facts"]["observed_at"]
    assert activity == {"latest_run_status": "skipped", "latest_run_stop_reason": "market_closed",
                        "skipped_market_closed_reports": 1}


@pytest.mark.parametrize("change", ["error", "other_skip", "failure", "bad_summary", "null_facts",
                                     "bad_schema", "account", "revision", "receipt", "future", "errors"])
def test_unproved_or_failed_latest_report_never_falls_back(monkeypatch, configured, change):
    expected, report = configured
    latest = closed(report)
    if change == "error": latest["status"] = "error"
    if change == "other_skip": latest["diagnostics"]["skip_reason"] = "already_running"
    if change == "failure": latest["diagnostics"]["failure_category"] = "ibkr_gateway_unavailable"
    if change == "bad_summary": latest["summary"] = None
    if change == "null_facts": latest["summary"]["account_facts"] = None
    if change == "bad_schema": latest["schema_version"] = "unknown.v0"
    if change == "account": latest["runtime_target"]["account_selector"] = ["U00000099"]
    if change == "revision": latest["diagnostics"]["runtime_revision"] = "other-revision"
    if change == "receipt": latest["runtime_release_receipt"]["runtime_revision"] = "other-revision"
    if change == "future": latest["finished_at"] = (NOW + timedelta(hours=1)).isoformat()
    if change == "errors": latest["errors"] = ["synthetic-error"]
    uris, reads = readers(monkeypatch, expected, [latest, report])
    activity = {}
    uri, selected = select(expected, activity)
    assert uri == uris[0] and selected == latest and reads == [uris[0]] and activity == {}


def test_new_error_after_a_normal_skip_stops_before_older_success(monkeypatch, configured):
    expected, report = configured
    failed = closed(report)
    failed.update(status="error")
    failed["diagnostics"] = {**failed["diagnostics"], "skip_reason": None, "failure_category": "ibkr_gateway_unavailable"}
    uris, reads = readers(monkeypatch, expected, [closed(report), failed, report])
    uri, selected = select(expected)
    assert uri == uris[1] and selected == failed and reads == uris[:2]


@pytest.mark.parametrize("bad", ["account", "revision", "snapshot"])
def test_bad_prior_record_is_not_hidden_by_even_older_good_snapshot(monkeypatch, configured, bad):
    expected, report = configured
    failed = deepcopy(report)
    if bad == "account": failed["runtime_target"]["account_selector"] = ["U00000099"]
    if bad == "revision": failed["diagnostics"]["runtime_revision"] = "other-revision"
    if bad == "snapshot": failed["summary"]["account_facts"]["schema_version"] = "unknown.v0"
    uris, reads = readers(monkeypatch, expected, [closed(report), failed, report])
    uri, selected = select(expected)
    assert uri == uris[1] and selected == failed and reads == uris[:2]
    assert publisher.project_ibkr_account_facts_history(selected, source_report_uri=uri, **expected)["status"] == "skipped"


def test_explicit_name_reads_only_named_report_without_fallback(monkeypatch, configured):
    expected, report = configured
    uris, reads = readers(monkeypatch, expected, [closed(report), report])
    activity = {}
    uri, selected = select(expected, activity, report_name="20261013T115900Z.json")
    assert uri == uris[0] and selected == closed(report) and reads == [uris[0]] and activity == {}


@pytest.mark.parametrize("failure", ["report_read_failed", "runtime_report_invalid"])
def test_old_read_or_bad_json_failure_does_not_search_further(monkeypatch, configured, failure):
    expected, report = configured
    uris, reads = readers(monkeypatch, expected, [closed(report), publisher._ProjectionError(failure), report])
    with pytest.raises(publisher._ProjectionError, match=failure):
        select(expected)
    assert reads == uris[:2]


def test_bounded_reads_and_changed_listing_are_not_success(monkeypatch, configured):
    expected, report = configured
    uris, reads = readers(monkeypatch, expected, [closed(report)] * (publisher._REPORT_READ_LIMIT + 2) + [report])
    with pytest.raises(publisher._ProjectionError, match="report_unavailable"):
        select(expected)
    assert reads == uris[:publisher._REPORT_READ_LIMIT]
    monkeypatch.setattr(publisher, "_report_candidate_uris", lambda **_kw: [uris[1], uris[0]])
    with pytest.raises(publisher._ProjectionError, match="report_selection_changed"):
        select(expected)


def test_hourly_closed_records_cannot_shadow_a_still_fresh_snapshot(monkeypatch, configured):
    expected, report = configured
    uris, reads = readers(monkeypatch, expected, [closed(report)] * 12 + [report])
    activity = {}
    uri, selected = select(expected, activity)
    assert uri == uris[-1] and selected == report and reads == uris
    assert activity["skipped_market_closed_reports"] == 12


def test_listing_has_explicit_item_time_and_call_limits(monkeypatch, configured):
    expected, _ = configured
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        assert args[:3] == ("gcloud", "storage", "ls")
        assert "--uri" not in args, "objects list --uri returns REST self-links, not gs:// report names"
        assert kwargs["timeout"] == 60
        prefix = args[3]
        return SimpleNamespace(returncode=0, stdout=prefix + "20261013T115900Z.json\n", stderr="")
    monkeypatch.setattr(publisher.subprocess, "run", run)
    assert publisher._latest_report_uri(prefix=expected["expected_report_prefix"],
        project_id=expected["expected_project_id"], now=NOW).endswith("20261013T115900Z.json")
    assert len(calls) == 2
    monkeypatch.setattr(publisher.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(
        returncode=0, stdout="synthetic\n" * (publisher._REPORT_LIST_LIMIT + 1), stderr=""))
    with pytest.raises(publisher._ProjectionError, match="report_listing_failed"):
        publisher._latest_report_uri(prefix=expected["expected_report_prefix"], project_id="synthetic", now=NOW)


@pytest.mark.parametrize("body", ['{"status":"error","status":"skipped"}', '{"value":NaN}',
                                    '{"summary":{"account_facts":null,"account_facts":{}}}', '{broken'])
def test_ambiguous_or_bad_report_json_is_not_a_normal_skip(monkeypatch, configured, body):
    monkeypatch.setattr(publisher.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(
        returncode=0, stdout=body, stderr=""))
    with pytest.raises(publisher._ProjectionError, match="runtime_report_invalid"):
        publisher._load_gcs_report("gs://synthetic/reports/example.json", project_id="synthetic")


def test_diagnostic_keeps_latest_run_separate_from_valid_snapshot(monkeypatch, configured, capsys):
    expected, report = configured
    readers(monkeypatch, expected, [closed(report), report])
    assert diagnostic.main([], now=NOW) == 0
    text = capsys.readouterr().out
    result = json.loads(text)
    assert result["reason"] == "identity_verified" and result["account_facts_stages"]["positions"] == "verified"
    assert result["report_stop_reason"] == "unknown"  # Fixture snapshot has no declared status.
    assert result["report_selection"]["latest_run_stop_reason"] == "market_closed"
    assert PRIVATE not in text and "12345.6700" not in text and "U00000001" not in text


def test_diagnostic_does_not_turn_a_read_error_after_closed_into_success(monkeypatch, configured, capsys):
    expected, report = configured
    uris, reads = readers(monkeypatch, expected, [closed(report), publisher._ProjectionError("runtime_report_invalid"), report])
    assert diagnostic.main([], now=NOW) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == "runtime_report_invalid" and result["projection_passed"] is None
    assert result["report_selection"]["latest_run_stop_reason"] == "market_closed"
    assert reads == uris[:2]


@pytest.mark.parametrize("expired", [False, True])
def test_publisher_keeps_original_freshness_and_zero_http_when_stale(monkeypatch, configured, capsys, expired):
    expected, report = configured
    if expired:
        observed = NOW - timedelta(hours=37)
        report["started_at"] = (observed - timedelta(seconds=1)).isoformat()
        report["finished_at"] = (observed + timedelta(seconds=1)).isoformat()
        report["summary"]["account_facts"]["observed_at"] = observed.isoformat()
    latest = closed(_report(expected))
    readers(monkeypatch, expected, [latest, report])
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None): return NOW
    monkeypatch.setattr(publisher, "datetime", FixedDateTime)
    posted = []
    class Ack:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def read(self, _limit): return b'{"ok":true,"stored":true,"unchanged":false}'
    class Opener:
        def open(self, request, **_kw):
            posted.append(json.loads(request.data))
            return Ack()
    monkeypatch.setattr(publisher, "build_opener", lambda *_args: Opener())
    assert publisher.main() == (1 if expired else 0)
    text = capsys.readouterr().out
    assert ("skipped:observation_stale" if expired else "published:unknown") in text
    assert json.loads(text.splitlines()[-1])["report_selection"]["latest_run_stop_reason"] == "market_closed"
    assert len(posted) == (0 if expired else 1)
    if posted:
        assert posted[0]["observed_finished_at"] == report["summary"]["account_facts"]["observed_at"]
        assert posted[0]["broker_reported_positions"] == [] and posted[0]["broker_reported_positions_complete"] is True
    assert PRIVATE not in text and "12345.6700" not in text and "U00000001" not in text
