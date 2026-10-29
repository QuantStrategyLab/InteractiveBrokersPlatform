"""Additional native returns share one report, without publishing other facts."""
import json
import time
from urllib.error import HTTPError

import pytest

from scripts import publish_ibkr_flex_period_return as cli
from tests.test_flex_protected_report_scope import protected_config
from tests.test_publish_ibkr_flex_period_return import FlexSession, Opener, configure, xml

REAL_PUBLISH = cli.publish_period_return


def setup(monkeypatch, *, pending=False):
    configure(monkeypatch)
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON", '["DU0000000"]')
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON", json.dumps(protected_config()))
    monkeypatch.setenv("IBKR_PERIOD_RETURN_ADDITIONAL_ACCOUNT_KEYS_JSON", json.dumps({
        "additional-2": "synthetic-key-2", "additional-3": "synthetic-key-3"}))
    # Reverse statement order to ensure no positional account selection.
    statements = b"".join(xml(twr=str(index), account=f"DU000000{index}", currency="USD").split(b"<FlexStatements>")[1]
        .split(b"</FlexStatements>")[0] for index in (3, 1, 2))
    report = xml().replace(b"</FlexStatements>", statements + b"</FlexStatements>")
    session = FlexSession(b"<FlexStatementResponse><ErrorCode>1019</ErrorCode></FlexStatementResponse>" if pending else report)
    monkeypatch.setattr(cli.requests, "Session", lambda: session)
    monkeypatch.setattr(time, "sleep", lambda _delay: setattr(session, "report", report))
    published = []
    def publish(payload, **kwargs):
        published.append((payload, kwargs))
        return "published"
    monkeypatch.setattr(cli, "publish_period_return", publish)
    def forbidden(*_args, **_kwargs):
        pytest.fail("additional-only mode must not publish executions or flows")
    monkeypatch.setattr(cli, "publish_executions", forbidden)
    monkeypatch.setattr(cli, "publish_external_flow", forbidden)
    return session, published


@pytest.mark.parametrize("pending", [False, True])
def test_additional_only_selects_exact_accounts_from_one_report(monkeypatch, capsys, pending):
    session, published = setup(monkeypatch, pending=pending)
    assert cli.main(("--additional-returns-only",)) == 0
    output = capsys.readouterr().out
    assert json.loads(output) == {"additional_returns": {"additional-2": "published", "additional-3": "published"}}
    assert len(published) == 2
    for index, (payload, kwargs) in zip((2, 3), published, strict=True):
        assert payload["account_ids"] == [f"DU000000{index}"]
        assert payload["target_id"] == f"artificial-target-{index}"
        assert payload["account_scope"] == f"artificial-scope-{index}"
        assert payload["source_value"] == str(index)
        assert payload["source_binding_id"] == cli._bound_source_id(**{
            key: value for key, value in protected_config()[f"additional-{index}"].items()
            if key not in {"target_id", "report_prefix"}})
        assert kwargs["expected_account_key"] == f"synthetic-key-{index}"
    assert sum(url.endswith("/SendRequest") for url, _ in session.calls) == 1
    gets = [kwargs["params"] for url, kwargs in session.calls if url.endswith("/GetStatement")]
    assert len(gets) == (2 if pending else 1) and all(row == gets[0] for row in gets)
    for private in ("DU000000", "synthetic-key", "artificial-target", "synthetic-sync-token", "synthetic-flex-token"):
        assert private not in output


@pytest.mark.parametrize("raw", ["{}", "[]", "{private", '{"additional-2":"one","additional-2":"two"}',
    '{"unknown":"key"}', '{"additional-2":"synthetic-account-key"}',
    '{"additional-2":"duplicate","additional-3":"duplicate"}', '{"additional-2":3}'])
def test_invalid_additional_configuration_rejects_before_network(monkeypatch, capsys, raw):
    session, published = setup(monkeypatch)
    monkeypatch.setenv("IBKR_PERIOD_RETURN_ADDITIONAL_ACCOUNT_KEYS_JSON", raw)
    assert cli.main(("--additional-returns-only",)) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "configuration_incomplete"
    assert session.calls == [] and published == []


def test_additional_only_requires_explicit_nonempty_selection(monkeypatch, capsys):
    session, published = setup(monkeypatch)
    monkeypatch.delenv("IBKR_PERIOD_RETURN_ADDITIONAL_ACCOUNT_KEYS_JSON")
    assert cli.main(("--additional-returns-only",)) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "configuration_incomplete"
    assert session.calls == [] and published == []


@pytest.mark.parametrize("change", ["unknown", "duplicate", "cross_account"])
def test_invalid_report_cannot_publish_any_selected_account(monkeypatch, capsys, change):
    session, published = setup(monkeypatch)
    if change == "unknown":
        session.report = session.report.replace(b'DU0000001', b'DU9999999')
    elif change == "duplicate":
        session.report = session.report.replace(b'DU0000001', b'DU0000002')
    else:
        session.report = session.report.replace(b'<ChangeInNAV fromDate="20260101" toDate="20260131" twr="2"',
            b'<ChangeInNAV accountId="DU0000003" fromDate="20260101" toDate="20260131" twr="2"')
    assert cli.main(("--additional-returns-only",)) == 1
    assert published == []
    assert json.loads(capsys.readouterr().out)["status"] == "flex_import_failed"


def test_wrong_ack_never_retries_same_account_and_continues_other_account(monkeypatch, capsys):
    session, _published = setup(monkeypatch)
    opener = Opener()  # Its primary account key must fail an additional account's ACK.
    monkeypatch.setattr(cli, "build_opener", lambda *_args: opener)
    monkeypatch.setattr(cli, "publish_period_return", REAL_PUBLISH)
    assert cli.main(("--additional-returns-only",)) == 1
    assert json.loads(capsys.readouterr().out) == {"additional_returns": {"additional-2": "sync_unknown", "additional-3": "sync_unknown"},
        "additional_return_sync_diagnostics": {"additional-2": {"period_return_sync_reason": "invalid_ack"},
                                               "additional-3": {"period_return_sync_reason": "invalid_ack"}}}
    assert len(opener.calls) == 2 and len(session.calls) == 2
    assert [json.loads(request.data)["target_id"] for request, _ in opener.calls] == [
        "artificial-target-2", "artificial-target-3"]


def test_historical_mode_preserves_date_and_never_writes_mismatched_period(monkeypatch, capsys):
    session, published = setup(monkeypatch)
    monkeypatch.setenv("IBKR_NATIVE_RETURN_DATE", "2026-01-31")
    assert cli.main(("--additional-returns-only",)) == 1
    assert json.loads(capsys.readouterr().out) == {"additional_returns": {"additional-2": "historical_period_mismatch", "additional-3": "historical_period_mismatch"}}
    assert published == []
    assert session.calls[0][1]["params"]["fd"] == session.calls[0][1]["params"]["td"] == "20260131"


def test_historical_success_uses_each_account_ack_and_historical_flag(monkeypatch, capsys):
    session, published = setup(monkeypatch)
    monkeypatch.setenv("IBKR_NATIVE_RETURN_DATE", "2026-01-31")
    session.report = session.report.replace(b"20260101", b"20260131")
    assert cli.main(("--additional-returns-only",)) == 0
    assert all(kwargs["historical"] is True for _, kwargs in published)
    assert all(payload["period"] == {"from": "2026-01-31", "to": "2026-01-31"} for payload, _ in published)
    assert set(json.loads(capsys.readouterr().out)["additional_returns"]) == {"additional-2", "additional-3"}


def test_one_historical_slot_never_publishes_other_accounts(monkeypatch, capsys):
    session, published = setup(monkeypatch)
    monkeypatch.setenv("IBKR_NATIVE_RETURN_DATE", "2026-01-31")
    monkeypatch.setenv("ADDITIONAL_RETURN_SLOT", "additional-2")
    session.report = session.report.replace(b"20260101", b"20260131")
    assert cli.main(("--additional-returns-only",)) == 0
    assert json.loads(capsys.readouterr().out) == {"additional_returns": {"additional-2": "published"}}
    assert len(published) == 1 and published[0][1]["historical"] is True
    assert published[0][0]["target_id"] == "artificial-target-2"


@pytest.mark.parametrize("slot,mode,date", [
    ("additional-2", (), "2026-01-31"),
    ("additional-2", ("--cashflow-only",), "2026-01-31"),
    ("additional-2", ("--additional-returns-only",), ""),
    ("additional-1", ("--additional-returns-only",), "2026-01-31"),
])
def test_invalid_single_slot_stops_before_network(monkeypatch, capsys, slot, mode, date):
    session, published = setup(monkeypatch)
    monkeypatch.setenv("IBKR_NATIVE_RETURN_DATE", date)
    monkeypatch.setenv("ADDITIONAL_RETURN_SLOT", slot)
    assert cli.main(mode) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "configuration_incomplete"
    assert session.calls == [] and published == []


def test_primary_selector_mismatch_rejects_before_network(monkeypatch, capsys):
    session, published = setup(monkeypatch)
    monkeypatch.setenv("IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON", '["DU0000001"]')
    assert cli.main(("--additional-returns-only",)) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "configuration_incomplete"
    assert session.calls == [] and published == []


def test_scheduled_default_reuses_same_report_and_keeps_primary_writes(monkeypatch, capsys):
    session, published = setup(monkeypatch)
    assert cli.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "published" and len(published) == 3
    assert published[0][1]["expected_account_key"] == "synthetic-account-key"
    assert result["additional_returns"] == {"additional-2": "published", "additional-3": "published"}
    assert sum(url.endswith("/SendRequest") for url, _ in session.calls) == 1


def test_pending_exhaustion_never_generates_replacement_or_publishes(monkeypatch, capsys):
    session, published = setup(monkeypatch, pending=True)
    monkeypatch.setattr(time, "sleep", lambda _delay: None)
    assert cli.main(("--additional-returns-only",)) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "flex_pending"
    assert published == []
    assert sum(url.endswith("/SendRequest") for url, _ in session.calls) == 1
    assert sum(url.endswith("/GetStatement") for url, _ in session.calls) == 6


def test_missing_native_return_is_not_zero_or_partial_success(monkeypatch, capsys):
    session, published = setup(monkeypatch)
    session.report = session.report.replace(b'twr="2"', b'other="2"')
    assert cli.main(("--additional-returns-only",)) == 1
    assert json.loads(capsys.readouterr().out) == {"additional_returns": {"additional-2": "native_twr_unavailable", "additional-3": "published"}}
    assert len(published) == 1 and published[0][1]["expected_account_key"] == "synthetic-key-3"


def test_selected_binding_cannot_collide_with_primary(monkeypatch, capsys):
    session, published = setup(monkeypatch)
    config = protected_config()
    config["additional-2"]["target_id"] = "synthetic-target"
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON", json.dumps(config))
    assert cli.main(("--additional-returns-only",)) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "configuration_incomplete"
    assert session.calls == [] and published == []


def test_preflight_reports_only_selected_identity_hashes_without_io(monkeypatch, capsys):
    import hashlib

    session, published = setup(monkeypatch)
    assert cli.main(("--preflight",)) == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert set(result["additional_return_fingerprints"]) == {"additional-2", "additional-3"}
    assert result["additional_return_fingerprints"]["additional-2"]["account_key_sha256"] == hashlib.sha256(
        b"synthetic-key-2").hexdigest()
    assert "synthetic-key" not in output and "DU000000" not in output
    assert session.calls == [] and published == []


@pytest.mark.parametrize("failed_stage", ["return_ack", "return_timeout", "execution", "flow"])
def test_default_primary_failure_prevents_all_additional_posts(monkeypatch, capsys, failed_stage):
    _session, published = setup(monkeypatch)
    if failed_stage.startswith("return_"):
        opener = Opener(patch={"account_key": "wrong-key"}) if failed_stage == "return_ack" else Opener(
            error=TimeoutError("private-transport-details"))
        monkeypatch.setattr(cli, "build_opener", lambda *_args: opener)
        monkeypatch.setattr(cli, "publish_period_return", REAL_PUBLISH)
    else:
        name = "_execution_result" if failed_stage == "execution" else "_external_flow_result"
        key = "executions_status" if failed_stage == "execution" else "external_flow_status"
        monkeypatch.setattr(cli, name, lambda *_args: {key: "sync_unknown"})
    assert cli.main() == 1
    output = capsys.readouterr().out
    assert json.loads(output)["additional_returns_status"] == "skipped_primary_failure"
    if failed_stage.startswith("return_"):
        assert len(opener.calls) == 1
    else:
        assert len(published) == 1 and published[0][1]["expected_account_key"] == "synthetic-account-key"
    assert "private-transport-details" not in output


@pytest.mark.parametrize("additional_only", [False, True])
@pytest.mark.parametrize("http_status", [302, 401, 403, 429, 500])
def test_additional_failure_reports_safe_http_status_in_both_modes(monkeypatch, capsys, additional_only, http_status):
    setup(monkeypatch)
    opener = Opener(error=HTTPError(cli.SYNC_URL, http_status, "synthetic-secret", {}, None))
    monkeypatch.setattr(cli, "build_opener", lambda *_args: opener)
    def publish(payload, **kwargs):
        if kwargs["expected_account_key"] == "synthetic-account-key":
            return "published"
        return REAL_PUBLISH(payload, **kwargs)
    monkeypatch.setattr(cli, "publish_period_return", publish)
    assert cli.main(("--additional-returns-only",) if additional_only else ()) == 1
    output = capsys.readouterr().out
    result = json.loads(output)
    expected_status = "sync_unknown" if http_status == 500 else "sync_rejected"
    assert result["additional_returns"] == {"additional-2": expected_status}
    detail = result["additional_return_sync_diagnostics"]["additional-2"]
    assert detail["period_return_http_status"] == http_status
    assert len(opener.calls) == 1 and "synthetic-secret" not in output and "synthetic-key" not in output


def test_optional_additional1_key_merges_without_replacing_other_accounts(monkeypatch, capsys):
    session, published = setup(monkeypatch)
    monkeypatch.setenv("IBKR_PERIOD_RETURN_ADDITIONAL1_ACCOUNT_KEY", "private-extra-key-1")
    assert cli.main(("--preflight",)) == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert set(result["additional_return_fingerprints"]) == {"additional-1", "additional-2", "additional-3"}
    assert "private-extra-key-1" not in output
    assert session.calls == [] and published == []


def test_optional_additional1_key_alone_and_matching_duplicate_are_valid(monkeypatch):
    setup(monkeypatch)
    monkeypatch.delenv("IBKR_PERIOD_RETURN_ADDITIONAL_ACCOUNT_KEYS_JSON")
    monkeypatch.setenv("IBKR_PERIOD_RETURN_ADDITIONAL1_ACCOUNT_KEY", "private-extra-key-1")
    assert set(cli._config(cli.os.environ)["additional_returns"]) == {"additional-1"}
    monkeypatch.setenv("IBKR_PERIOD_RETURN_ADDITIONAL_ACCOUNT_KEYS_JSON", json.dumps({"additional-1": "private-extra-key-1"}))
    assert set(cli._config(cli.os.environ)["additional_returns"]) == {"additional-1"}


def test_optional_additional1_conflict_is_rejected_before_network(monkeypatch, capsys):
    session, published = setup(monkeypatch)
    monkeypatch.setenv("IBKR_PERIOD_RETURN_ADDITIONAL1_ACCOUNT_KEY", "private-extra-key-1")
    monkeypatch.setenv("IBKR_PERIOD_RETURN_ADDITIONAL_ACCOUNT_KEYS_JSON", json.dumps({"additional-1": "different-key"}))
    assert cli.main(("--preflight",)) == 2
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "invalid"
    assert "private-extra-key-1" not in output and "different-key" not in output
    assert session.calls == [] and published == []


def test_optional_additional1_missing_return_does_not_block_other_accounts(monkeypatch, capsys):
    session, published = setup(monkeypatch)
    monkeypatch.setenv("IBKR_PERIOD_RETURN_ADDITIONAL1_ACCOUNT_KEY", "synthetic-key-1")
    session.report = session.report.replace(b'twr="1"', b'other="1"')
    assert cli.main(("--additional-returns-only",)) == 1
    assert json.loads(capsys.readouterr().out)["additional_returns"] == {
        "additional-1": "native_twr_unavailable", "additional-2": "published", "additional-3": "published"}
    assert [kwargs["expected_account_key"] for _, kwargs in published] == ["synthetic-key-2", "synthetic-key-3"]
    assert sum(url.endswith("/SendRequest") for url, _ in session.calls) == 1


@pytest.mark.parametrize("additional_only", [False, True])
@pytest.mark.parametrize("reason", ["invalid_ack", "transport_error"])
def test_unknown_one_account_is_not_retried_and_other_account_continues(monkeypatch, capsys, additional_only, reason):
    setup(monkeypatch)
    called = []
    def publish(_payload, **kwargs):
        key = kwargs["expected_account_key"]
        called.append(key)
        if key == "synthetic-key-2":
            kwargs["diagnostics"]["period_return_sync_reason"] = reason
            return "sync_unknown"
        return "published"
    monkeypatch.setattr(cli, "publish_period_return", publish)
    assert cli.main(("--additional-returns-only",) if additional_only else ()) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["additional_returns"] == {"additional-2": "sync_unknown", "additional-3": "published"}
    assert called == (["synthetic-key-2", "synthetic-key-3"] if additional_only else
                      ["synthetic-account-key", "synthetic-key-2", "synthetic-key-3"])


def test_scheduled_publishes_each_additional_flow_with_its_own_binding(monkeypatch, capsys):
    setup(monkeypatch)
    seen = []
    def flow(ledger, config, diagnostics=None):
        seen.append((tuple(ledger['account_ids']), config['account_key']))
        return {'external_flow_status': 'published'}
    monkeypatch.setattr(cli, '_external_flow_result', flow)
    assert cli.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert seen == [(('DU0000000',), 'synthetic-account-key'),
                    (('DU0000002',), 'synthetic-key-2'), (('DU0000003',), 'synthetic-key-3')]
    assert result['additional_external_flows'] == {'additional-2': 'published', 'additional-3': 'published'}


def test_additional_flow_unknown_is_visible_and_not_retried(monkeypatch, capsys):
    setup(monkeypatch)
    seen = []
    def flow(_ledger, config, diagnostics=None):
        seen.append(config['account_key'])
        return {'external_flow_status': 'sync_unknown' if config['account_key'] == 'synthetic-key-2' else 'published'}
    monkeypatch.setattr(cli, '_external_flow_result', flow)
    assert cli.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result['additional_external_flows']['additional-2'] == 'sync_unknown'
    assert seen.count('synthetic-key-2') == 1


def test_primary_historical_only_publishes_no_other_facts(monkeypatch, capsys):
    from datetime import datetime, timedelta, timezone
    day = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    session, published = setup(monkeypatch)
    compact = day.replace("-", "").encode()
    session.report = session.report.replace(b"20260101", compact).replace(b"20260131", compact)
    monkeypatch.setenv("IBKR_NATIVE_RETURN_DATE", day)
    assert cli.main(("--primary-return-only",)) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "published"}
    assert len(published) == 1
    assert published[0][0]["period"] == {"from": day, "to": day}
    assert published[0][0]["account_ids"] == ["DU0000000"]
    assert published[0][1]["historical"] is True
    assert sum(url.endswith("/SendRequest") for url, _ in session.calls) == 1


def test_primary_historical_only_requires_date_before_network(monkeypatch, capsys):
    session, published = setup(monkeypatch)
    monkeypatch.delenv("IBKR_NATIVE_RETURN_DATE", raising=False)
    assert cli.main(("--primary-return-only",)) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "configuration_incomplete"
    assert session.calls == [] and published == []


def test_primary_historical_only_rejects_wrong_report_date(monkeypatch, capsys):
    from datetime import datetime, timedelta, timezone
    day = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    session, published = setup(monkeypatch)
    monkeypatch.setenv("IBKR_NATIVE_RETURN_DATE", day)
    assert cli.main(("--primary-return-only",)) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "historical_period_mismatch"
    assert published == [] and session.calls


def test_primary_historical_unknown_is_not_retried(monkeypatch, capsys):
    from datetime import datetime, timedelta, timezone
    day = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    session, published = setup(monkeypatch)
    compact = day.replace("-", "").encode()
    session.report = session.report.replace(b"20260101", compact).replace(b"20260131", compact)
    monkeypatch.setenv("IBKR_NATIVE_RETURN_DATE", day)
    calls = []
    def unknown(payload, **kwargs):
        calls.append((payload, kwargs))
        return "sync_unknown"
    monkeypatch.setattr(cli, "publish_period_return", unknown)
    assert cli.main(("--primary-return-only",)) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "sync_unknown"
    assert len(calls) == 1 and published == []
