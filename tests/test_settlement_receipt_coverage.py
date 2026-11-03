from copy import deepcopy
from types import SimpleNamespace

import pytest
from application.cycle_result import StrategyCycleResult
from application import settlement_route, settlement_reporting


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('coverage', ['empty', 'unrelated', 'incomplete'])
@pytest.mark.parametrize('summary,expected', [
    ({'execution_status': 'blocked', 'no_op_reason': 'pending_orders_detected:synthetic'}, 'risk_blocked'),
    ({'execution_status': 'pending_reconciliation', 'option_orders_pending': [{'symbol': 'TEST'}]}, 'reconciliation_required'),
    ({'execution_status': 'executed', 'option_orders_submitted': [{'symbol': 'TEST'}]}, 'submitted'),
    ({'execution_status': 'no_op', 'no_op_reason': 'target_diff_below_threshold'}, 'no_rebalance'),
    ({'execution_status': 'pending_reconciliation', 'orders_pending': [
        dict(symbol='STOCK', side='buy', quantity=3, broker_order_id='73'),
        dict(symbol='STOCK', side='buy', quantity=3, broker_order_id='74'),
    ]}, 'reconciliation_required'),
    ({'execution_status': 'executed', 'orders_submitted': [
        dict(symbol='STOCK', side='buy', quantity=3, broker_order_id='73'),
        dict(symbol='STOCK', side='buy', quantity=3, broker_order_id='74'),
    ]}, 'submitted'),
])
def test_settlement_preserves_existing_receipt_facts(strategy_module_factory, monkeypatch, enabled, coverage, summary, expected):
    module = strategy_module_factory(IBKR_DRY_RUN_ONLY='false')
    monkeypatch.setenv('IBKR_ORDER_SETTLEMENT_ENABLED', 'true' if enabled else 'false')
    run_id = 'synthetic-run'
    observed = {key: [] for key in ('orders_submitted', 'orders_pending', 'orders_filled')}
    if coverage != 'empty':
        observed['orders_submitted'] = [dict(symbol='STOCK', side='buy', quantity=3,
            broker_order_id='73', source_run_id=run_id if coverage == 'incomplete' else 'other-run',
            status='Cancelled', broker_confirmed_terminal=True, filled_quantity=0)]
    original = module.build_execution_report
    def build(*args, **kwargs):
        report = original(*args, **kwargs)
        report['run_id'] = run_id
        report['runtime_release_receipt'] = {
            'attestation_state': 'self_attested',
            'strategy_release': {'strategy_revision': 'a' * 40},
        }
        return report
    monkeypatch.setattr(module, 'build_execution_report', build)
    monkeypatch.setattr(module, 'is_market_open_now', lambda **k: True)
    monkeypatch.setattr(module, 'load_strategy_plugin_signals', lambda: ((), None))
    monkeypatch.setattr(module, 'publish_strategy_plugin_alerts', lambda *a, **k: None)
    monkeypatch.setattr(module, 'run_strategy_core', lambda **k: StrategyCycleResult(
        result='synthetic cycle', execution_summary=deepcopy(summary)))
    monkeypatch.setattr(settlement_route, 'runtime_settlement_store', lambda m: SimpleNamespace(records=lambda: []))
    monkeypatch.setattr('application.order_settlement.settlement_summary', lambda store: deepcopy(observed))
    monkeypatch.setattr(settlement_reporting, 'publish_settlement_notices', lambda *a, **k: None)
    monkeypatch.setattr(module, '_publish_runtime_failure_notification', lambda **k: None)
    reports = []
    with module.app.test_request_context('/run', method='POST'):
        response = module._handle_request(persist_report=False, report_collector=reports)
    assert response[1] == 200
    assert reports[0]['execution_receipt']['outcome'] == expected


@pytest.mark.parametrize('coverage', ['complete', 'missing', 'mismatched', 'duplicate', 'unidentified'])
def test_only_complete_stock_identity_coverage_can_amend_receipt(coverage):
    stock = dict(symbol='TEST', side='buy', quantity=3, broker_order_id='73')
    original = dict(execution_status='pending_reconciliation', orders_pending=[stock],
                    orders_submitted=[stock], no_op_reason='synthetic-pending')
    row = dict(stock, status='Filled', broker_confirmed_terminal=True,
               filled_quantity=3, average_fill_price=52)
    observed = dict(orders_submitted=[row], orders_filled=[row], orders_pending=[])
    if coverage == 'missing':
        original['orders_pending'].append({**stock, 'broker_order_id': '74'})
    elif coverage == 'mismatched':
        row['quantity'] = 4
    elif coverage == 'duplicate':
        observed['orders_submitted'].append(dict(row))
    elif coverage == 'unidentified':
        stock['broker_order_id'] = None
    before = deepcopy(original)
    projected = settlement_reporting.settlement_receipt_summary(original, observed)
    assert original == before
    if coverage == 'complete':
        assert projected['execution_status'] == 'completed'
        assert projected['orders_pending'] == []
        assert projected['orders_filled'] == [row]
        assert projected['no_op_reason'] == 'synthetic-pending'
    else:
        assert projected == original
