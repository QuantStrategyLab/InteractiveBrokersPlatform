"""Cash-flow source diagnostics observe fields, never publish or expose values."""
import copy
import json
from pathlib import Path

import pytest

from application import ibkr_flex_source as source
from scripts import diagnose_ibkr_flex_ledger as cli
from tests.test_flex_diagnostic_readonly import configure
from tests.test_ibkr_flex_source import SEND, PENDING, TOKEN, Session

XML = b'''<FlexQueryResponse type="AF"><FlexStatements><FlexStatement accountId="DU0000000" fromDate="20260101" toDate="20260131" period="PRIVATE_PERIOD">
<AccountInformation currency="USD"/>
<CashTransactions><CashTransaction accountId="DU0000000" currency="USD" amount="3456.123456" type="Deposits &amp; Withdrawals" dateTime="20260105;110000" transactionID="PRIVATE_TRANSACTION" timeZone="UTC" privateKey="PRIVATE_CANARY"/></CashTransactions>
<StmtFunds><StatementOfFundsLine accountId="DU0000000" currency="USD" amount="3456.123456" activityCode="DEP" date="20260105" transactionID="PRIVATE_TRANSACTION"/></StmtFunds>
</FlexStatement></FlexStatements></FlexQueryResponse>'''


def read(monkeypatch, capsys, xml=XML):
    configure(monkeypatch)
    session = Session(SEND, xml)
    monkeypatch.setattr(source.requests, 'Session', lambda: pytest.fail('no real session'))
    # Use the original complete HTTP/parser chain with a synthetic transport.
    def fetch(**kwargs):
        kwargs['session'] = session
        return original_fetch(**kwargs)
    monkeypatch.setattr(source, 'fetch_activity_flex_xml', fetch)
    status = cli.main(('--cashflow-read-once',))
    text = capsys.readouterr().out
    return status, json.loads(text), text, session


original_fetch = source.fetch_activity_flex_xml


def test_cashflow_cli_uses_one_read_and_reports_only_field_shapes(monkeypatch, capsys):
    status, out, text, session = read(monkeypatch, capsys)
    assert status == 0
    assert [x[0] for x in session.calls] == ['SendRequest', 'GetStatement']
    assert out['schema_version'] == 'ibkr_flex_cashflow_diagnostic.v1'
    assert out['status'] == 'observed_source_shape'
    shape = out['cashflow_source_diagnostic']
    assert shape['coverage'] == 'not_evaluated'
    cash = shape['sections']['CashTransactions']
    assert cash['record_count'] == 1
    assert cash['date_time_format_counts']['dateTime'] == {'local_datetime': 1}
    assert cash['timezone_format_counts']['timeZone'] == {'utc_label': 1}
    assert shape['sections']['StmtFunds']['date_time_format_counts']['date'] == {'date_only': 1}
    for private in ('3456.123456', 'DU0000000', TOKEN, 'PRIVATE_CANARY', 'PRIVATE_TRANSACTION', 'PRIVATE_PERIOD', 'privateKey'):
        assert private not in text


@pytest.mark.parametrize('section', [b'', b'<CashTransactions/>', b'<CashTransactions><Unexpected privateKey="PRIVATE_CANARY"/></CashTransactions>'])
def test_missing_empty_and_unsupported_sections_remain_distinct(monkeypatch, capsys, section):
    xml = XML[:XML.index(b'<CashTransactions>')] + section + XML[XML.index(b'<StmtFunds>'):]
    status, out, text, _ = read(monkeypatch, capsys, xml)
    assert status == 0
    cash = out['cashflow_source_diagnostic']['sections']['CashTransactions']
    assert cash['record_count'] == 0 and cash['field_type_counts'] == {}
    assert cash['section_count'] == (0 if not section else 1)
    assert cash['unsupported_record_count'] == (1 if b'Unexpected' in section else 0)
    assert 'PRIVATE_CANARY' not in text
    assert out['cashflow_source_diagnostic']['coverage'] == 'not_evaluated'


@pytest.mark.parametrize('change', ['private_field', 'private_format', 'private_missing', 'bad_count', 'bool_count', 'bad_period', 'bad_digest'])
def test_cashflow_stdout_rejects_dynamic_private_fields_and_bad_counts(monkeypatch, capsys, change):
    _, value, _, _ = read(monkeypatch, capsys)
    value = copy.deepcopy(value)
    cash = value['cashflow_source_diagnostic']['sections']['CashTransactions']
    if change == 'private_field': cash['field_type_counts']['PRIVATE_CANARY'] = {'text': 1}
    elif change == 'private_format': cash['date_time_format_counts']['dateTime'] = {'PRIVATE_CANARY': 1}
    elif change == 'private_missing': cash['missing_required_fields'] = ['PRIVATE_CANARY']
    elif change == 'bad_count': cash['field_type_counts']['amount'] = {'decimal_text': 2}
    elif change == 'bool_count': cash['record_count'] = True
    elif change == 'bad_period': value['period']['from'] = 'PRIVATE_CANARY'
    else: value['source_ref'] = 'PRIVATE_CANARY'
    safe = cli._validated_cashflow_report(value)
    assert safe['missing'] == ['flex_cashflow_diagnostic_result_invalid']
    assert 'PRIVATE_CANARY' not in json.dumps(safe)


def test_source_rejects_conflicting_modes_without_network():
    session = Session()
    with pytest.raises(source.FlexSourceError):
        source.diagnose_activity_flex_ledger(token=TOKEN, query_id='12345', expected_account_ids=('DU0000000',),
            session=session, cash_report_only=True, cashflow_source_only=True)
    assert session.calls == []


def test_unparseable_amount_is_a_safe_format_count_not_a_value(monkeypatch, capsys):
    status, out, text, _ = read(monkeypatch, capsys, XML.replace(b'amount="3456.123456"', b'amount="PRIVATE_CANARY"'))
    assert status == 0
    assert out['cashflow_source_diagnostic']['sections']['CashTransactions']['field_type_counts']['amount'] == {'unsupported_text': 1}
    assert 'PRIVATE_CANARY' not in text


def test_cashflow_pending_collects_original_handle_without_publishing(monkeypatch):
    session = Session(SEND, PENDING, XML)
    delays = []
    monkeypatch.setattr(source.time, 'sleep', delays.append)
    out = source.diagnose_activity_flex_ledger(token=TOKEN, query_id='12345', expected_account_ids=('DU0000000',),
        session=session, collect_pending=True, cashflow_source_only=True)
    assert out['status'] == 'observed_source_shape'
    assert [x[0] for x in session.calls] == ['SendRequest', 'GetStatement', 'GetStatement']
    assert delays == [3]


def test_workflow_keeps_the_existing_read_only_lane():
    text = Path('.github/workflows/diagnose-ibkr-flex-readonly.yml').read_text()
    assert 'cashflow_source_only:' in text and '--cashflow-read-once' in text
    assert 'group: ibkr-flex-period-return' in text and 'cancel-in-progress: false' in text
    assert 'conflicting_diagnostic_modes' in text
    for forbidden in ('publish_ibkr_flex_period_return', 'SYNC_TOKEN', 'upload-artifact', 'id-token:', 'gcloud'):
        assert forbidden not in text


def test_cashflow_explicit_period_uses_one_generation_with_bounded_dates():
    session = Session(SEND, XML)
    out = source.diagnose_activity_flex_ledger(token=TOKEN, query_id='12345', expected_account_ids=('DU0000000',),
        session=session, cashflow_source_only=True, report_period={'from': '2026-01-01', 'to': '2026-01-31'})
    assert out['period'] == {'from': '2026-01-01', 'to': '2026-01-31'}
    assert [name for name, _ in session.calls] == ['SendRequest', 'GetStatement']
    assert session.calls[0][1]['fd'] == '20260101' and session.calls[0][1]['td'] == '20260131'


@pytest.mark.parametrize('period', [{'from': '2026-01-01'}, {'from': '2026-01-01', 'to': '2026-02-01'},
    {'from': '2026-01-02', 'to': '2026-01-01'}, {'from': 'PRIVATE_CANARY', 'to': '2026-01-31'}])
def test_bad_source_period_never_starts_a_request(period):
    session = Session()
    with pytest.raises(source.FlexSourceError):
        source.diagnose_activity_flex_ledger(token=TOKEN, query_id='12345', expected_account_ids=('DU0000000',),
            session=session, cashflow_source_only=True, report_period=period)
    assert session.calls == []


@pytest.mark.parametrize('from_date,to_date', [('2026-01-01', ''), ('2026-01-01', '2026-02-01'), ('PRIVATE_CANARY', '2026-01-31')])
def test_bad_cli_period_fails_in_preflight_without_network(monkeypatch, capsys, from_date, to_date):
    configure(monkeypatch)
    monkeypatch.setenv('IBKR_FLEX_CASHFLOW_REQUEST_FROM', from_date)
    monkeypatch.setenv('IBKR_FLEX_CASHFLOW_REQUEST_TO', to_date)
    monkeypatch.setattr(cli, 'diagnose_activity_flex_ledger', lambda **_: pytest.fail('must not read'))
    assert cli.main(('--preflight',)) == 2
    text = capsys.readouterr().out
    assert json.loads(text)['missing'] == ['cashflow_request_period_invalid']
    assert 'PRIVATE_CANARY' not in text


def test_cli_applies_explicit_period_only_in_new_readonly_mode(monkeypatch, capsys):
    _, value, _, _ = read(monkeypatch, capsys)
    calls = []
    def diagnose(**kwargs):
        calls.append(kwargs)
        return value
    monkeypatch.setattr(cli, 'diagnose_activity_flex_ledger', diagnose)
    monkeypatch.setenv('IBKR_FLEX_CASHFLOW_REQUEST_FROM', '2026-01-01')
    monkeypatch.setenv('IBKR_FLEX_CASHFLOW_REQUEST_TO', '2026-01-31')
    assert cli.main(('--read-once',)) == 2 and calls == []
    capsys.readouterr()
    assert cli.main(('--cashflow-read-once',)) == 0
    capsys.readouterr()
    assert calls[0]['report_period'] == {'from': '2026-01-01', 'to': '2026-01-31'}
    monkeypatch.setenv('IBKR_FLEX_CASHFLOW_REQUEST_TO', '2026-01-30')
    assert cli.main(('--cashflow-read-once',)) == 1
    assert json.loads(capsys.readouterr().out)['missing'] == ['requested_period_mismatch']
