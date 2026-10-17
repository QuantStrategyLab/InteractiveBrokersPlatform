"""Derive local identities from actual pending reservations, varying all scopes."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import pytest
from notifications import unified_adapter as adapter
from notifications.digest_snapshot.unified_contract import order_identity_digest
from tests.test_unified_notification_adapter import StateStore, event
from tests.test_sender_receipt_identity import CASES, outcome_for


@pytest.mark.parametrize('seed',[17,83])
@pytest.mark.parametrize('case',CASES+['order_wrapped','incident_upper','event_uri'])
def test_dynamic_actual_identities_double_call(seed,case):
    store,calls,reserved=StateStore(),[],[]
    origin=event('real_error')
    source=replace(origin,source_run_id=f'dynamic-run-{seed}',target_id=f'dynamic-target-{seed}',
        incident_reason=f'dynamic-error-{seed}',occurred_at=origin.occurred_at+timedelta(seconds=seed),
        order_execution_digest=order_identity_digest(f'fake-order-{seed}'))
    def send():
        calls.append('fake-send')
        key,(_,marker)=next(iter(store.backing.items()))
        assert marker['delivery_status']=='pending' and marker['receipt_id'] is None
        reserved.append(deepcopy(marker))
        if case=='order_wrapped':receipt='local-order:'+source.order_execution_digest.upper()
        elif case=='incident_upper':receipt='  local:'+key.upper()+'  '
        elif case=='event_uri':receipt='gs://synthetic/events/'+source.to_candidate()['dedupe_key']+'.json'
        else:return outcome_for(case,source,key,marker)
        return {'acknowledged':True,'receipt_id':receipt}
    adapter.immediate_incident(source,send,store=store)
    marker=deepcopy(next(iter(store.backing.values()))[1])
    delivered=case.startswith('provider')
    assert marker['delivery_status']==('delivered' if delivered else 'uncertain')
    assert bool(marker['receipt_id']) is delivered
    for key in ('active','generation','state','transition_key'):assert marker[key]==reserved[0][key]
    adapter.immediate_incident(source,send,store=store)
    assert calls==['fake-send']
    assert next(iter(store.backing.values()))[1]==marker
