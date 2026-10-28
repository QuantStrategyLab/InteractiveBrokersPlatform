"""Synthetic runner chain: preparation, one Job, protected readback and cleanup."""
from datetime import timedelta
import copy
import json

import pytest

from scripts import execute_account_cash_job as runner
from tests.test_observe_maintenance_cash_job import Cloud, EXECUTION, JOB, NOW, setup
from tests.test_pilot_no_traffic_release import environment
from tests.test_read_account_cash_job import fixture


@pytest.mark.parametrize('profile', ['global_etf_rotation', 'russell_top50_leader_rotation'])
@pytest.mark.parametrize('failure', [None, 'create_unknown', 'execute_unknown', 'missing_report', 'duplicate_log'])
def test_one_target_job_protected_readback_and_cleanup(monkeypatch, tmp_path, profile, failure):
    args, bucket, _, blob = fixture(monkeypatch)
    args['logs'][0]['jsonPayload'] = json.loads(args['logs'][0].pop('textPayload'))
    doc = copy.deepcopy(args['document'])
    rows = doc['spec']['template']['spec']['template']['spec']['containers'][0]['env']
    next(row for row in rows if row['name'] == 'CASH_READONLY_PROFILE')['value'] = profile
    data = json.loads(bucket.blob.return_value.download_as_bytes.return_value)
    data['strategy_profile'] = profile
    data['runtime_target']['strategy_profile'] = profile
    raw = json.dumps(data).encode()
    blob.size = len(raw)
    bucket.blob.return_value.download_as_bytes.return_value = raw
    state, revision, _, _ = setup()

    class RunnerCloud(Cloud):
        def run(self, command, **kwargs):
            if command[1:3] == ['logging', 'read']:
                self.calls.append(command)
                return json.dumps(args['logs'])
            result = super().run(command, **kwargs)
            if command[1:4] == ['run', 'jobs', 'execute']:
                self.executed[0]['status'].update(startTime=NOW.isoformat(),
                                                  completionTime=(NOW + timedelta(seconds=3)).isoformat())
                return json.dumps(self.executed[0])
            return result

    cloud = RunnerCloud(state, revision, None if failure in {'missing_report', 'duplicate_log'} else failure)
    if failure == 'duplicate_log':
        args['logs'].append(copy.deepcopy(args['logs'][0]))
    if failure == 'missing_report':
        args['client'].list_blobs.return_value.pages = iter([[]])
    monkeypatch.setattr(runner.archive, 'resolve_profile_slot', lambda _: 'additional-1')
    monkeypatch.setattr(runner.identity, 'resolve_expected', lambda: args['expected'])
    monkeypatch.setattr(runner.maintenance, 'report_namespace', lambda *_: args['namespace'])
    calls = []
    def prepare(selected, expected):
        calls.append((selected['CASH_CANDIDATE_JOB'], selected['IBKR_ACCOUNT_FACTS_TARGET']))
        assert expected == args['expected']
        return None, None, None, doc
    env = {**environment(), 'MAINTENANCE_JOB': 'true', 'REQUESTED_TARGET': profile,
           'GCP_REGION': 'synthetic-region', 'CLOUD_RUN_REGION': 'synthetic-region',
           'CASH_CANDIDATE_IMAGE': runner._CANDIDATE_REPOSITORY + '@' + runner.CANDIDATE_DIGESTS[profile]}
    times = iter([NOW, NOW + timedelta(seconds=4)])
    result = runner.execute(env, tmp_path / 'safe.json', run=cloud.run, snapshot=cloud.snapshot,
        prepare=prepare, client_factory=lambda: args['client'], sleep=lambda _: None, now=lambda: next(times))
    assert calls == [(JOB, 'additional-1')]
    assert result['status'] == ('OBSERVED_JOB_ONLY' if failure is None else 'STOPPED')
    assert result['job_cleanup_verified'] and result['baseline_unchanged']
    assert not (tmp_path / 'maintenance-cash-job.private.json').exists()
    executions = [cmd for cmd in cloud.calls if cmd[1:4] == ['run', 'jobs', 'execute']]
    assert len(executions) == (0 if failure == 'create_unknown' else 1)
    assert not any('update-traffic' in cmd or 'scheduler' in cmd for cmd in cloud.calls)
    queries = [cmd[3] for cmd in cloud.calls if cmd[1:3] == ['logging', 'read']]
    assert all('jsonPayload.status="observation_saved"' in query for query in queries)
    assert '12345.67' not in json.dumps(result)


def test_unadmitted_dispatch_never_reads_or_creates(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.archive, 'resolve_profile_slot', lambda _: pytest.fail('not admitted'))
    with pytest.raises(runner.pilot.ReleaseError, match='dispatch_not_admitted'):
        runner.execute({}, tmp_path / 'safe.json')


def test_unverified_build_input_stops_before_job_create(monkeypatch, tmp_path):
    state, revision, expected, _ = setup()
    cloud = Cloud(state, revision)
    monkeypatch.setattr(runner.archive, 'resolve_profile_slot', lambda _: 'additional-1')
    monkeypatch.setattr(runner.identity, 'resolve_expected', lambda: expected)
    def fail_preparation(*_):
        raise ValueError('build_input_changed')
    env = {**environment(), 'MAINTENANCE_JOB': 'true', 'REQUESTED_TARGET': 'global_etf_rotation',
           'GCP_REGION': 'us-central1', 'CASH_CANDIDATE_IMAGE': runner._CANDIDATE_REPOSITORY + '@'
           + runner.CANDIDATE_DIGESTS['global_etf_rotation']}
    with pytest.raises(ValueError, match='build_input_changed'):
        runner.execute(env, tmp_path / 'safe.json', run=cloud.run, snapshot=cloud.snapshot, prepare=fail_preparation)
    assert not any(command[1:4] in (['run', 'jobs', 'replace'], ['run', 'jobs', 'execute']) for command in cloud.calls)


def test_wrong_profile_candidate_stops_before_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.archive, 'resolve_profile_slot', lambda _: pytest.fail('untrusted image'))
    env = {**environment(), 'MAINTENANCE_JOB': 'true', 'REQUESTED_TARGET': 'global_etf_rotation',
           'CASH_CANDIDATE_IMAGE': runner._CANDIDATE_REPOSITORY + '@'
           + runner.CANDIDATE_DIGESTS['russell_top50_leader_rotation']}
    with pytest.raises(runner.pilot.ReleaseError, match='candidate_image_mismatch'):
        runner.execute(env, tmp_path / 'safe.json')
