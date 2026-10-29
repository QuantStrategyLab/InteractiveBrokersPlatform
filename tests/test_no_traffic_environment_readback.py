"""Literal settings are compared privately; only opaque digests leave capture."""
from __future__ import annotations

import argparse
import copy
import json

import pytest

from scripts import verify_cloud_run_no_traffic_deploy as readback


@pytest.mark.parametrize('setting', ('RUNTIME_TARGET_ENABLED', 'IBKR_DRY_RUN_ONLY', 'RUNTIME_TARGET_JSON'))
def test_literal_setting_change_blocks_readback_without_disclosing_values(monkeypatch, tmp_path, capsys, setting):
    service = {
        'status': {'traffic': [{'revisionName': 'baseline', 'percent': 100}]},
        'spec': {'template': {'spec': {'containers': [{'env': [
            {'name': setting, 'value': 'private-original-value'},
            {'name': 'TOKEN', 'valueFrom': {'secretKeyRef': {'name': 'protected-ref', 'key': 'latest'}}},
        ]}]}}},
    }
    commands = []

    def run_json(command):
        commands.append(command)
        if command[1:4] == ['run', 'services', 'describe']:
            # Simulate gcloud's field projection rather than silently giving
            # the verifier values its production command did not request.
            projected = copy.deepcopy(service)
            if 'containers[].env[].value,' not in command[-1]:
                for entry in projected['spec']['template']['spec']['containers'][0]['env']:
                    entry.pop('value', None)
            return projected
        return {'bindings': []} if 'get-iam-policy' in command else []

    monkeypatch.setattr(readback, '_run_json', run_json)
    args = argparse.Namespace(project='synthetic', service='synthetic', region='synthetic',
                              scheduler_location='synthetic', before=tmp_path/'baseline.json')
    args.before.write_text(json.dumps(readback._snapshot(args)))
    assert 'private-original-value' not in args.before.read_text()
    service['spec']['template']['spec']['containers'][0]['env'][0]['value'] = 'private-mutated-value'
    with pytest.raises(RuntimeError, match='configuration changed'):
        readback._verify(args)
    assert any('containers[].env[].value,' in command[-1] for command in commands)
    output = capsys.readouterr()
    assert 'private-original-value' not in output.out + output.err
    assert 'private-mutated-value' not in output.out + output.err
