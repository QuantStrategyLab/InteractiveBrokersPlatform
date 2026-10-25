"""Artificial reporting checks on the serving dependency baseline."""
from types import SimpleNamespace
from pathlib import Path
import json

import pytest

from application.account_facts import build_ibkr_account_facts
from application.ibkr_cash_observations import inspect_cash_observations
from tests.test_ibkr_portfolio import ReportingCacheIB, _reporting_snapshot


@pytest.mark.parametrize('cash_only', [False, True])
def test_cash_attachment_reuses_native_observation_without_changing_strategy_or_position_source(cash_only):
    class CashIB(ReportingCacheIB):
        def accountValues(self):
            rows = super().accountValues()
            rows.append(SimpleNamespace(account='DU123',currency='USD',tag='SettledCash',value='80.000000001'))
            rows.append(SimpleNamespace(account='OTHER',currency='USD',tag='SettledCash',value='900'))
            return rows

    ib = CashIB()
    snapshot = _reporting_snapshot(ib,cash_only_execution=cash_only)
    original_fields = snapshot.total_equity,snapshot.cash_balance,snapshot.buying_power,snapshot.positions
    facts = build_ibkr_account_facts(snapshot)
    assert facts['broker_reported_positions'][0]['market_value']=='250'
    assert snapshot.positions[0].market_value==20
    assert facts['cash'][0]['cash_balance']=='100.0'
    attachment=facts['broker_cash_observations']
    settled=[r for r in attachment['values'] if r['source_tag']=='SettledCash']
    assert settled==[dict(currency='USD',source_tag='SettledCash',value='80.000000001')]
    assert attachment['observed_at']==facts['observed_at']
    assert ib.calls==['reqPositions','positions','accountValues','portfolio_cache']
    assert original_fields==(snapshot.total_equity,snapshot.cash_balance,snapshot.buying_power,snapshot.positions)
    inspected=inspect_cash_observations(attachment,observed_at=facts['observed_at'])
    assert inspected['fields']['SettledCash']=='OBSERVED'
    assert not inspected['spendable_cash_verified'] and not inspected['complete_cash_obligations_verified']


@pytest.mark.parametrize('value', ['private-provider-error','NaN','80'])
def test_duplicate_or_invalid_settlement_is_not_reconstructed_from_numeric_cache(value):
    class CashIB(ReportingCacheIB):
        def accountValues(self):
            rows=super().accountValues()
            rows.extend(SimpleNamespace(account='DU123',currency='USD',tag='SettledCash',value=v)
                for v in ('80',value))
            return rows

    facts=build_ibkr_account_facts(_reporting_snapshot(CashIB()))
    attachment=facts['broker_cash_observations']
    assert dict(currency='USD',source_tag='SettledCash') in attachment['invalid_fields']
    assert all(r['source_tag']!='SettledCash' for r in attachment['values'])
    assert 'private-provider-error' not in str(facts)


def test_cross_account_raw_rows_do_not_publish_account_facts():
    snapshot=_reporting_snapshot(ReportingCacheIB())
    snapshot.metadata['cash_observation_rows']=({'account_id':'OTHER','currency':'USD','SettledCash':'80'},)
    assert build_ibkr_account_facts(snapshot)=={}


def test_original_report_writer_reads_back_cash_attachment_without_precision_loss(tmp_path):
    from quant_platform_kit.common.runtime_reports import persist_runtime_report

    facts=build_ibkr_account_facts(_reporting_snapshot(ReportingCacheIB()))
    report=dict(schema_version='runtime_report.v1',platform='ibkr',deploy_target='synthetic',
        strategy_profile='synthetic',run_id='cash-maintenance-fixture',run_source='test',
        status='ok',dry_run=True,started_at=facts['observed_at'],finished_at=facts['observed_at'],
        summary=dict(account_facts=facts),diagnostics={},artifacts={},errors=[])
    result=persist_runtime_report(report,base_dir=tmp_path)
    saved=json.loads(Path(result.local_path).read_text())
    assert saved['summary']['account_facts']['broker_cash_observations']==facts['broker_cash_observations']
