"""Actual IBKR producer through final QRS sender/receipt with fake transport."""
import contextlib
import importlib
import io
import json
import sys
from types import SimpleNamespace
import pytest
from tests.test_notification_qrs_roundtrip import qrs as _qrs_fixture, archive_report, export_archive, intake


@pytest.fixture
def qrs(monkeypatch):
    yield from _qrs_fixture.__wrapped__(monkeypatch)


@pytest.mark.parametrize('ack,delivered', [
    (True, False), ({'ok': True}, False),
    ({'ok': True, 'result': {'message_id': True, 'chat': {'id': -123}}}, False),
    ({'ok': True, 'result': {'message_id': 'transport-ack', 'chat': {'id': -123}}}, False),
    ({'ok': True, 'result': {'message_id': 3, 'chat': {'id': -123}}}, True),
    ({'ok': True, 'result': {'message_id': 3, 'chat': {'id': -456}}}, False),
])
def test_actual_producer_to_final_qrs_delivery_receipt(qrs, monkeypatch, tmp_path, ack, delivered):
    payload = export_archive(monkeypatch, [archive_report('receipt-zero-a'), archive_report('receipt-zero-b')])
    rows, summary = intake(qrs, tmp_path, payload)
    source = tmp_path / 'candidates.json'; source.write_text(json.dumps(payload))
    expected = [{k: rows[0][k] for k in ('platform_id', 'opaque_account_uid', 'target_id', 'strategy_profile')}]
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'notifications': {'quant_sentinel': {'daily_digest': {'aggregator': {'expected_targets': expected}}}}}))
    transport = importlib.import_module('strict_transport_fixture')
    monkeypatch.setattr(transport, 'MaintenanceClient', SimpleNamespace(from_environment=lambda *a, **kw: None), raising=False)
    monkeypatch.setattr(transport, 'empty_diagnostic', lambda: {}, raising=False)
    monkeypatch.setitem(sys.modules, 'credential_maintenance_notify', transport)
    sender = importlib.import_module('send_daily_digest_telegram')
    guard = sender.resend_guard
    monkeypatch.setattr(guard, 'report_date', lambda: '2026-10-17')
    monkeypatch.setattr(guard, 'read_history', lambda *a, **kw: [])
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('GITHUB_EVENT_NAME', 'schedule')
    monkeypatch.setenv('DIGEST_RECIPIENT_SCOPE', 'synthetic')
    monkeypatch.setenv('DIGEST_CANDIDATES_STORE_STATUS', 'loaded')
    monkeypatch.setattr(sender, 'diagnose_telegram_route', lambda **kw: {})
    monkeypatch.setattr(sender, 'missing_delivery_requirements', lambda: [])
    monkeypatch.setattr(sender, 'resolve_telegram_token', lambda: 'synthetic-not-a-credential')
    monkeypatch.setattr(sender, 'resolve_telegram_chat_id', lambda: '-123')
    path = tmp_path / 'receipt.json'
    calls = []
    def response(request):
        pending = json.loads(path.read_text())
        assert pending['status'] == 'sending' and pending['delivered'] is False
        calls.append('transport')
        return ack
    monkeypatch.setattr(transport, '_json', response, raising=False)
    args = ['--business-day', '2026-10-16', '--no-github', '--candidates', str(source), '--platform-config', str(config), '--write-receipt', str(path)]
    with contextlib.redirect_stdout(io.StringIO()):
        code = sender.main(args)
    receipt = json.loads(path.read_text())
    assert code == (0 if delivered else 1)
    assert receipt['delivered'] is delivered
    assert receipt['run_count'] == 2 and receipt['total_fills'] == 0
    assert receipt['notification_summary']['counts_unknown'] is False
    assert calls == ['transport']
    # A known or uncertain previous attempt prevents a second transport call.
    monkeypatch.setattr(guard, 'read_history', lambda *a, **kw: [(1, receipt)])
    with contextlib.redirect_stdout(io.StringIO()):
        sender.main(args)
    assert calls == ['transport']


def test_unknown_archive_counts_survive_final_qrs_render(qrs, monkeypatch, tmp_path):
    source = archive_report('unknown-run')
    source['summary'].pop('notification_digest')
    payload = export_archive(monkeypatch, [source])
    rows, summary = intake(qrs, tmp_path, payload)
    assert rows[0]['order_count'] is None and rows[0]['fill_count'] is None
    assert summary['counts_unknown'] is True
    from tests.test_notification_qrs_roundtrip import render
    assert '未知' in render(qrs, rows, summary, 'zh')
