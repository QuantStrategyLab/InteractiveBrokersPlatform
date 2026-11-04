"""Conditions, rather than invented probe failures, explain denied startup."""
import copy
from unittest import mock

import pytest

from scripts.runtime_target_continuity import verify_traffic_revision
from tests.test_candidate_warmup import candidate


@pytest.mark.parametrize('case,classification', [('never_started', 'instance_start_not_observed'), ('probe_failed', 'startup_probe_failed')])
def test_raw_conditions_distinguish_absent_health_from_actual_probe_failure(case, classification, capsys):
    value = copy.deepcopy(candidate())
    if case == 'probe_failed':
        value['status']['conditions'].append({'type': 'ContainerHealthy', 'status': 'False',
                                             'message': 'HTTP startup probe failed account=U12345678 host=private.example.invalid'})
    with mock.patch('scripts.runtime_target_continuity.verify_target_continuity'):
        with pytest.raises(ValueError):
            verify_traffic_revision(value, expected_revision='unit-service-new', expected_commit='abc', require_startup_health=True)
    output = capsys.readouterr().err
    assert classification in output and 'raw_conditions=' in output and 'ContainerReady' in output
    assert 'U12345678' not in output and 'private.example.invalid' not in output
    if case == 'never_started':
        assert 'Retired' in output and 'instance_logs=unknown' in output
        assert 'startup_probe_failed' not in output
    else:
        assert 'HTTP startup probe failed' in output
