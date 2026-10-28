"""Configured coverage observes one synthetic report, never values or authority."""
import copy
import json
from pathlib import Path

import pytest

from application import ibkr_flex_source as source
from scripts import diagnose_ibkr_flex_ledger as cli
from tests.test_flex_protected_report_scope import set_scope
from tests.test_ibkr_flex_source import PENDING, REFERENCE, SEND, TOKEN, XML, Session

MODE = ('--configured-account-coverage-read-once',)
EXTRA = b'''<FlexStatement accountId="DU0000002" fromDate="20260101" toDate="20260131">
<AccountInformation currency="USD"/><ChangeInNAV twr="7654.9876"/>
<Trades><Trade accountId="DU0000002" amount="7654.9876" privateKey="PRIVATE_CANARY"/></Trades>
<CashTransactions/><OpenPositions><Unexpected privateKey="PRIVATE_CANARY"/></OpenPositions>
</FlexStatement>'''
REPORT = XML.replace(b'</FlexStatements>', EXTRA + b'</FlexStatements>')


@pytest.fixture(autouse=True)
def no_real_transport(monkeypatch):
    monkeypatch.setattr(source.requests, 'Session', lambda: pytest.fail('no real HTTP'))


def run(monkeypatch, capsys, *bodies, args=MODE, expected_period=None):
    set_scope(monkeypatch)
    if expected_period:
        monkeypatch.setenv('IBKR_FLEX_EXPECTED_PERIOD_FROM', expected_period['from'])
        monkeypatch.setenv('IBKR_FLEX_EXPECTED_PERIOD_TO', expected_period['to'])
    class ClosedSession(Session):
        def close(self):
            pass
    session = ClosedSession(SEND, *(bodies or (REPORT,)))
    monkeypatch.setattr(source.requests, 'Session', lambda: session)
    status = cli.main(args)
    text = capsys.readouterr().out
    return status, json.loads(text), text, session


def test_one_report_four_fixed_slots_and_missing_vs_empty(monkeypatch, capsys):
    status, out, text, session = run(monkeypatch, capsys)
    assert status == 0 and out['coverage'] == 'not_evaluated'
    assert [name for name, _ in session.calls] == ['SendRequest', 'GetStatement']
    assert set(out['accounts']) == set(source.CONFIGURED_COVERAGE_SLOTS)
    missing = out['accounts']['additional-1']
    assert missing['statement_present'] is False and missing['native_twr_field_present'] is None
    assert missing['period'] == {'from': None, 'to': None}
    assert all(row['record_count'] is None for row in missing['sections'].values())
    observed = out['accounts']['additional-2']
    assert observed['statement_present'] is True and observed['native_twr_field_present'] is True
    assert observed['period'] == {'from': '2026-01-01', 'to': '2026-01-31'}
    assert observed['sections']['Trades']['record_count'] == 1
    assert observed['sections']['CashTransactions'] == {
        'section_count': 1, 'record_count': 0, 'unsupported_record_count': 0}
    assert observed['sections']['CashReport']['record_count'] is None
    assert observed['sections']['OpenPositions']['unsupported_record_count'] == 1
    for private in (TOKEN, REFERENCE, 'DU000000', '7654.9876', 'PRIVATE_CANARY', 'privateKey', 'gs://'):
        assert private not in text


def test_missing_twr_field_and_invalid_date_never_become_return_or_invented_date(monkeypatch, capsys):
    report = REPORT.replace(b'twr="7654.9876"', b'').replace(b'fromDate="20260101"', b'fromDate="PRIVATE_CANARY"')
    status, out, text, _ = run(monkeypatch, capsys, report)
    assert status == 0
    assert out['accounts']['additional-2']['native_twr_field_present'] is False
    assert out['accounts']['additional-2']['period']['from'] is None
    assert 'PRIVATE_CANARY' not in text


@pytest.mark.parametrize(('section', 'expected'), [
    (b'', {'section_count': 0, 'record_count': None, 'unsupported_record_count': 0}),
    (b'<CorporateActions/>', {'section_count': 1, 'record_count': 0, 'unsupported_record_count': 0}),
    (b'''<CorporateActions accountId="DU0000002">
      <CorporateAction accountId="DU0000002" symbol="PRIVATE_ASSET" amount="7654.9876"/>
      <CorporateAction description="PRIVATE_CANARY"/>
      <Unknown privateKey="PRIVATE_CANARY"/>
    </CorporateActions>''', {'section_count': 1, 'record_count': 2, 'unsupported_record_count': 1}),
])
def test_corporate_actions_native_counts_keep_absent_empty_and_private_values_distinct(
    monkeypatch, capsys, section, expected,
):
    report = REPORT.replace(b'<CashTransactions/>', section + b'<CashTransactions/>')
    status, out, text, session = run(monkeypatch, capsys, report)
    assert status == 0
    assert out['accounts']['additional-2']['sections']['CorporateActions'] == expected
    assert out['accounts']['primary']['sections']['CorporateActions']['record_count'] is None
    assert out['accounts']['additional-1']['sections']['CorporateActions']['record_count'] is None
    assert [name for name, _ in session.calls] == ['SendRequest', 'GetStatement']
    for private in (TOKEN, REFERENCE, 'DU000000', 'PRIVATE_ASSET', '7654.9876', 'PRIVATE_CANARY', 'privateKey'):
        assert private not in text


@pytest.mark.parametrize('tag', ['CorporateActions', 'CorporateAction'])
def test_corporate_actions_container_and_row_cannot_cross_account_scope(monkeypatch, capsys, tag):
    container_account = ' accountId="DU0000003"' if tag == 'CorporateActions' else ''
    row_account = ' accountId="DU0000003"' if tag == 'CorporateAction' else ''
    section = f'<CorporateActions{container_account}><CorporateAction{row_account}/></CorporateActions>'.encode()
    report = REPORT.replace(b'<CashTransactions/>', section + b'<CashTransactions/>')
    status, out, text, session = run(monkeypatch, capsys, report)
    assert status == 1 and out['missing'] == ['flex_import_failed']
    assert [name for name, _ in session.calls] == ['SendRequest', 'GetStatement']
    assert 'DU000000' not in text


@pytest.mark.parametrize('change', ['unknown', 'duplicate', 'primary_absent', 'row_mismatch', 'wrong_type'])
def test_report_scope_and_type_never_relaxed(monkeypatch, capsys, change):
    report = REPORT
    if change == 'unknown': report = report.replace(b'DU0000002', b'DU0000009')
    elif change == 'duplicate': report = report.replace(b'</FlexStatements>', EXTRA + b'</FlexStatements>')
    elif change == 'primary_absent': report = report.replace(b'DU0000000', b'DU0000001')
    elif change == 'row_mismatch': report = report.replace(b'<Trade accountId="DU0000002"', b'<Trade accountId="DU0000003"')
    else: report = report.replace(b'type="AF"', b'type="TC"')
    status, out, text, session = run(monkeypatch, capsys, report)
    assert status == 1 and out['missing'] == ['flex_import_failed']
    assert [name for name, _ in session.calls] == ['SendRequest', 'GetStatement']
    assert 'DU000000' not in text and 'PRIVATE_CANARY' not in text


def test_pending_only_collects_original_request(monkeypatch, capsys):
    delays = []
    monkeypatch.setattr(source.time, 'sleep', delays.append)
    status, _, _, session = run(monkeypatch, capsys, PENDING, PENDING, REPORT)
    assert status == 0 and delays == [3, 3]
    assert [name for name, _ in session.calls] == ['SendRequest', 'GetStatement', 'GetStatement', 'GetStatement']
    assert all(params['q'] == REFERENCE for _, params in session.calls[1:])


def test_pending_exhaustion_does_not_regenerate(monkeypatch, capsys):
    monkeypatch.setattr(source.time, 'sleep', lambda _: None)
    status, out, text, session = run(monkeypatch, capsys, *([PENDING] * 6))
    assert status == 1 and out['missing'] == ['flex_generation_pending']
    assert len(session.calls) == 7
    assert sum(name == 'SendRequest' for name, _ in session.calls) == 1
    assert REFERENCE not in text


def test_provider_failure_is_not_retried(monkeypatch, capsys):
    error = b'<FlexStatementResponse><ErrorCode>1012</ErrorCode></FlexStatementResponse>'
    status, out, _, session = run(monkeypatch, capsys, error)
    assert status == 1 and out['missing'] == ['flex_import_failed'] and len(session.calls) == 2


@pytest.mark.parametrize('failure', ['disabled', 'missing_scope', 'wrong_primary', 'request_range'])
def test_configuration_failures_stop_before_http(monkeypatch, capsys, failure):
    set_scope(monkeypatch)
    if failure == 'disabled': monkeypatch.delenv('IBKR_FLEX_DIAGNOSTIC_ENABLED')
    elif failure == 'missing_scope':
        monkeypatch.delenv('IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON')
        monkeypatch.delenv('IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON')
    elif failure == 'wrong_primary': monkeypatch.setenv('IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON', '["DU0000002"]')
    else:
        monkeypatch.setenv('IBKR_FLEX_CASHFLOW_REQUEST_FROM', '2026-01-01')
        monkeypatch.setenv('IBKR_FLEX_CASHFLOW_REQUEST_TO', '2026-01-31')
    assert cli.main(MODE) == 2
    assert 'DU000000' not in capsys.readouterr().out


def test_coverage_preflight_never_opens_session(monkeypatch, capsys):
    set_scope(monkeypatch)
    assert cli.main(('--preflight', '--configured-account-coverage-only')) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'configuration_shape_valid'


def test_expected_period_checks_additional_statement_too(monkeypatch, capsys):
    status, out, _, _ = run(monkeypatch, capsys,
        REPORT.replace(b'toDate="20260131">\n', b'toDate="20260130">\n'),
        expected_period={'from': '2026-01-01', 'to': '2026-01-31'})
    assert status == 1 and out['missing'] == ['expected_period_mismatch']


@pytest.mark.parametrize('change', ['slot', 'date', 'twr', 'count', 'section', 'digest'])
def test_stdout_rebuild_rejects_dynamic_or_invalid_output(monkeypatch, capsys, change):
    _, out, _, _ = run(monkeypatch, capsys)
    value = copy.deepcopy(out)
    if change == 'slot': value['accounts']['PRIVATE_CANARY'] = value['accounts']['primary']
    elif change == 'date': value['accounts']['primary']['period']['from'] = 'PRIVATE_CANARY'
    elif change == 'twr': value['accounts']['primary']['native_twr_field_present'] = 'PRIVATE_CANARY'
    elif change == 'count': value['accounts']['primary']['sections']['Trades']['record_count'] = True
    elif change == 'section': value['accounts']['primary']['sections']['PRIVATE_CANARY'] = {}
    else: value['source_ref'] = 'PRIVATE_CANARY'
    result = cli._validated_coverage_report(value)
    assert result['missing'] == ['flex_coverage_diagnostic_result_invalid']
    assert 'PRIVATE_CANARY' not in json.dumps(result)


def test_workflow_mode_is_opt_in_exclusive_and_reuses_existing_read_lane():
    workflow = Path('.github/workflows/diagnose-ibkr-flex-readonly.yml').read_text()
    definition = workflow.split('configured_account_coverage_only:', 1)[1].split('cash_source_only:', 1)[0]
    assert 'default: false' in definition and 'type: boolean' in definition
    assert 'selected_modes' in workflow and '--configured-account-coverage-read-once' in workflow
    assert workflow.count('inputs.configured_account_coverage_only') == 2
    assert 'group: ibkr-flex-period-return' in workflow
    for unsafe in ('upload-artifact', 'SYNC_TOKEN', 'schedule:', 'id-token:', 'gcloud'):
        assert unsafe not in workflow
