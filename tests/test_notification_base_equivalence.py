"""Compare actual base call recordings; notification display changes are listed."""
import json
from pathlib import Path
import pytest
from tests.notification_call_recorder import CAPTURE, SCENARIOS


@pytest.mark.parametrize('mode', [None, 'daily_digest'])
@pytest.mark.parametrize('flag', [None, '', 'false', 'invalid'])
@pytest.mark.parametrize('locale', ['zh', 'en'])
@pytest.mark.parametrize('kind,case', SCENARIOS)
def test_flags_off_match_actual_production_source(monkeypatch, mode, flag, locale, kind, case):
    for key, value in [('IBKR_UNIFIED_NOTIFICATION_ENABLED', flag), ('QSL_ROUTINE_NOTIFICATION_MODE', mode)]:
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    monkeypatch.delenv('QSL_DIGEST_ROUTINE_ORDERS', raising=False)
    golden = json.loads((Path(__file__).parent / 'fixtures/notification_production_base.json').read_text())
    before = golden[('daily_digest:' if mode else '') + f'{kind}:{case}:{locale}']
    after = CAPTURE[kind](monkeypatch, case, locale)
    if kind == 'cycle':
        # A1/localized display is approved. The exact sender changes remain
        # reviewable in base fixture and renderer tests; business calls/order
        # and result have no exclusions.
        assert [c for c in before['calls'] if c[0] != 'send'] == [c for c in after['calls'] if c[0] != 'send']
        assert len([c for c in before['calls'] if c[0] == 'send']) == len([c for c in after['calls'] if c[0] == 'send'])
        assert before['result'] == after['result']
    elif kind == 'runtime':
        assert before['result'] == after['result']
        assert len(before['sends']) == len(after['sends'])
        assert [s[1:] for s in before['sends']] == [s[1:] for s in after['sends']]
    else:
        assert before == after
