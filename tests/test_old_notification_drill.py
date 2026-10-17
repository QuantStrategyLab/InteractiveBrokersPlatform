"""Old pair-return drill API: one incident per failed observation."""
from copy import deepcopy
from datetime import timedelta
import pytest
from tests.test_unified_notification_adapter import routing as _routing_fixture, NOW, TARGET, healthy_receipt_report


@pytest.fixture
def routing(monkeypatch):
    return _routing_fixture.__wrapped__(monkeypatch)


@pytest.mark.parametrize('status', ['error', 'failed', 'unknown'])
def test_actual_old_drill_checker_repeats_only_one_incident(routing, monkeypatch, status):
    from scripts import daily_dry_run_digest as drill
    target = drill.DrillTarget('Example', TARGET['strategy_profile'], TARGET['account_scope'], TARGET['service_name'], '0 20 * * *', 'UTC')
    payload = healthy_receipt_report()
    payload.update(dry_run=True, account_scope=target.scope, status=status)
    payload['summary']['notification_digest']['dry_run'] = True
    monkeypatch.setattr(drill, '_targets', lambda: [target])
    monkeypatch.setattr(drill, '_service_is_drill_only', lambda _: (True, 'synthetic-service-url'))
    monkeypatch.setattr(drill, '_today_reports', lambda *a: [deepcopy(payload)])
    monkeypatch.delenv('DRILL_DIGEST_PREVIEW', raising=False)
    def job(name):
        if name == target.live_job:
            return {'state': 'PAUSED'}
        return {'state': 'ENABLED', 'schedule': target.schedule, 'timeZone': target.timezone,
                'lastAttemptTime': NOW.isoformat(), 'status': {},
                'httpTarget': {'uri': 'synthetic-service-url/dry-run', 'httpMethod': 'POST',
                               'oidcToken': {'serviceAccountEmail': 'synthetic-principal', 'audience': 'synthetic-service-url'}}}
    monkeypatch.setattr(drill, '_job', job)
    sends = []
    monkeypatch.setattr(drill, '_send_telegram', lambda text, **kw: sends.append(text) or True)
    assert drill.main(now=NOW+timedelta(minutes=1)) == 1
    assert drill.main(now=NOW+timedelta(minutes=2)) == 1
    assert len(sends) == 1
