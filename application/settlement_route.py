"""Authenticated read-only broker settlement endpoint."""
from __future__ import annotations

import os

from flask import request
from google.auth.transport.requests import Request
from google.oauth2 import id_token

from application.order_settlement import SettlementStore, settle_pending
from application.settlement_reporting import (
    settlement_enabled, publish_settlement_notices, settlement_candidate,
)
from quant_platform_kit.common.execution_state import build_execution_marker_store_from_env


def authenticate_settlement(*, env=None, verifier=None):
    values = os.environ if env is None else env
    audience = values.get('IBKR_SETTLEMENT_OIDC_AUDIENCE')
    subjects = {value.strip() for value in values.get('IBKR_SETTLEMENT_CALLER_SUBJECTS', '').split(',') if value.strip()}
    header = request.headers.get('Authorization', '')
    if not audience or not subjects or not header.startswith('Bearer '):
        return False
    try:
        claims = (verifier or id_token.verify_oauth2_token)(header[7:], Request(), audience=audience)
        return (claims.get('iss') in {'accounts.google.com', 'https://accounts.google.com'}
                and claims.get('sub') in subjects)
    except Exception:
        return False


def runtime_settlement_store(main):
    if len(main.ACCOUNT_IDS) != 1:
        raise ValueError('settlement_single_account_required')
    markers = build_execution_marker_store_from_env(platform_env_prefix='IBKR', env_reader=os.getenv,
        execution_mode=main.RUNTIME_SETTINGS.ib_gateway_mode)
    return SettlementStore(markers, account_id=main.ACCOUNT_IDS[0], profile=main.STRATEGY_PROFILE)


def handle_settle(main):
    if not authenticate_settlement():
        return {'error': 'settlement_unauthorized'}, 403
    if not settlement_enabled():
        return {'error': 'settlement_disabled'}, 409
    if not main.STRATEGY_RUN_LOCK.acquire(blocking=False):
        return {'error': 'settlement_busy'}, 409
    ib = None
    try:
        if request.get_data():
            return {'error': 'settlement_body_not_allowed'}, 400
        store = runtime_settlement_store(main)
        # No user-provided identity, account, order or broker parameters.
        ib = main.connect_ib(read_only=True, validate_trading_permissions=False)
        ib.RequestTimeout = 2.0
        summary = settle_pending(store, ib, wait_seconds=0, timeout_seconds=15)
        from notifications.unified_adapter import event_for, persist_candidate
        publish_settlement_notices(store, summary, send=main.send_tg_message,
            publish_candidate=lambda row: settlement_candidate(row, event_factory=event_for, persist=persist_candidate),
            locale=main.NOTIFY_LANG)
        # Return counts only; detailed financial facts remain in private state
        # and masked notices. Errors never include raw broker/provider text.
        return {'status': summary['execution_status'],
                'filled': len(summary['orders_filled']), 'partially_filled': len(summary['orders_partially_filled']),
                'rejected_or_cancelled': len(summary['orders_rejected_or_cancelled']),
                'unresolved': len(summary['orders_pending'])}, 200
    except Exception:
        return {'error': 'settlement_read_or_publish_failed', 'status': 'UNKNOWN'}, 503
    finally:
        try:
            if ib is not None:
                ib.disconnect()
        finally:
            main.STRATEGY_RUN_LOCK.release()
