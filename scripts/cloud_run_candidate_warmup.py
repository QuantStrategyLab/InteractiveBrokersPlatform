"""One exact Russell candidate lifecycle, including asserted finally cleanup.

Revision minimums are immutable. Restoring zero therefore creates a second
revision; if the warm revision is serving, that replacement needs independent
health qualification and an explicit second traffic migration. No rollback or
health exemption is implicit in cleanup.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
from typing import Callable

from notifications.redaction import redact_notification_diagnostics
from scripts import reconcile_cloud_runtime as reconcile
from scripts.runtime_target_continuity import verify_traffic_revision

MINIMUM = 'autoscaling.knative.dev/minScale'


def revision_metadata(value: dict) -> dict:
    # resourceVersion advances on controller status updates, not desired changes.
    # Preserve every other metadata field so identity/configuration drift blocks.
    return {key: item for key, item in (value.get('metadata') or {}).items()
            if key != 'resourceVersion'}


def eligible(plan: dict, workflow_target: str, selector: str) -> bool:
    if workflow_target != 'configured':
        return False
    targets = plan.get('targets') or []
    if len(targets) != 1 or not selector or targets[0].get('service_name') != selector:
        raise ValueError('candidate warmup requires one exact configured target')
    return targets[0].get('strategy_profile') == 'russell_top50_leader_rotation'


def cloud(command: list[str]) -> dict:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False,
                                timeout=360 if 'deploy' in command or 'update' in command else 30)
        if result.returncode:
            raise RuntimeError('candidate cloud operation failed')
        value = json.loads(result.stdout or '{}')
        if not isinstance(value, (dict, list)):
            raise ValueError
        return value
    except (subprocess.SubprocessError, ValueError, OSError):
        raise RuntimeError('candidate cloud operation failed or readback unavailable') from None


def active(service: dict) -> list:
    rows = service.get('status', {}).get('traffic')
    if not isinstance(rows, list) or not rows or any(
        not isinstance(row, dict) or type(row.get('percent', 0)) is not int
        or row.get('percent', 0) < 0 or not row.get('revisionName') for row in rows
    ) or sum(row.get('percent', 0) for row in rows) != 100:
        raise RuntimeError('candidate traffic readback invalid')
    return sorted((row['revisionName'], row['percent']) for row in rows if row.get('percent', 0) > 0)


class Release:
    def __init__(self, *, project: str, region: str, service: str, commit: str, digest: str,
                 run_id: str, deploy_args: list[str], state_path: Path,
                 cloud: Callable[[list[str]], dict] = cloud, no_traffic_verify: Callable[[], None] | None = None):
        self.project, self.region, self.service = project, region, service
        self.commit, self.digest, self.run_id = commit, digest, run_id
        self.deploy_args, self.state_path, self.cloud = deploy_args, state_path, cloud
        self.no_traffic_verify = no_traffic_verify
        tag = 'cand-' + commit[:8]
        # Cloud Run limits the combined tag and service name to 46 characters.
        # Long exact targets use revision identity without a traffic tag.
        self.tag = tag if len(service) + len(tag) <= 46 else None
        self.common = [f'--project={project}', f'--region={region}', '--format=json']
        self.warm = None
        self.reset = None
        self.expected = None
        self.before = None
        self.cleaning = False

    def record(self, status: str) -> None:
        # No resource names, bindings, env or provider diagnostics in this result.
        self.state_path.write_text(json.dumps({'status': status, 'tag': self.tag,
                                              'reset_creates_revision': True}) + '\n')

    def service_read(self) -> dict:
        return self.cloud(['gcloud', 'run', 'services', 'describe', self.service, *self.common])

    def revision_read(self, name: str) -> dict:
        return self.cloud(['gcloud', 'run', 'revisions', 'describe', name, *self.common])

    def readback(self, stage: str) -> None:
        # Request the raw control-plane fields, excluding env and Secret refs.
        # Shared diagnostics redaction plus workflow-registered identity masks
        # protect the returned conditions; do not replace them with probe claims.
        for kind, name in [('services', self.service), ('revisions', self.warm), ('revisions', self.reset)]:
            if not name:
                continue
            fields = ('json(status.conditions,status.traffic,status.latestCreatedRevisionName,'
                      'metadata.annotations,spec.template.metadata.annotations)')
            try:
                raw = self.cloud(['gcloud', 'run', kind, 'describe', name,
                                  *self.common[:-1], '--format=' + fields])
                if (not isinstance(raw, dict) or not isinstance(raw.get('status'), dict)
                        or not isinstance(raw['status'].get('conditions'), list) or not raw['status']['conditions']):
                    raise RuntimeError("candidate raw readback invalid")
                print(stage + ' ' + kind + ' describe: ' +
                      redact_notification_diagnostics(json.dumps(raw, sort_keys=True)), flush=True)
            except Exception:
                print(stage + ' ' + kind + ' describe: READBACK_UNAVAILABLE', flush=True)
                raise RuntimeError('candidate raw readback unavailable') from None
            if kind == 'revisions':
                rows = raw.get('status', {}).get('conditions') or []
                if (not any(row.get('type') == 'ContainerHealthy' for row in rows)
                        and any(row.get('type') == 'Active' and row.get('status') == 'False' for row in rows)):
                    query = ('resource.type="cloud_run_revision" AND resource.labels.revision_name="' + name +
                             '" AND (logName:"run.googleapis.com%2Fstdout" OR logName:"run.googleapis.com%2Fstderr" '
                             'OR logName:"run.googleapis.com%2Fvarlog%2Fsystem")')
                    logs = self.cloud(['gcloud', 'logging', 'read', query, f'--project={self.project}',
                                       '--limit=1', '--format=json(logName)'])
                    if not isinstance(logs, list):
                        raise RuntimeError('candidate instance-log evidence unavailable')
                    print('instance_start_not_observed; instance_logs=' + ('present' if logs else 'absent'), flush=True)

    def stable(self) -> dict:
        current = self.service_read()
        if self.expected is None or current != self.expected:
            raise RuntimeError('candidate service changed outside this attempt')
        return current

    def candidate(self, service: dict, minimum: str) -> dict:
        name = service.get('status', {}).get('latestCreatedRevisionName')
        if not isinstance(name, str) or not name.startswith(self.service + '-'):
            raise RuntimeError('candidate revision identity unavailable')
        value = self.revision_read(name)
        meta = value.get('metadata') or {}
        labels = meta.get('labels') or {}
        if (meta.get('name') != name or labels.get('commit-sha') != self.commit
                or labels.get('github-run-id') != self.run_id
                or str((meta.get('annotations') or {}).get(MINIMUM, '0')) != minimum
                or str(value.get('status', {}).get('imageDigest', '')).split('@')[-1] != self.digest):
            raise RuntimeError('candidate revision binding or minimum mismatch')
        return value

    def health(self, value: dict) -> None:
        current = self.stable()
        reconcile._verify_deploy_candidate_with_wait(
            value, project=self.project, region=self.region, service_name=self.service,
            service=current, revision=value['metadata']['name'], commit=self.commit,
            require_generation=True, wait_for_start=True)
        self.stable()
        # A fresh read after the wait prevents a cached healthy value from
        # authorizing a later update. Existing continuity checks stay strict.
        fresh = self.revision_read(value['metadata']['name'])
        if revision_metadata(fresh) != revision_metadata(value) or fresh.get('spec') != value.get('spec'):
            raise RuntimeError('candidate revision changed during qualification')
        verify_traffic_revision(fresh, expected_revision=value['metadata']['name'],
                                expected_commit=self.commit, require_startup_health=True)

    def promote(self, value: dict) -> None:
        self.stable()
        # Baseline validation can outlast the earlier health observation.
        # Requalify the exact revision immediately before either traffic shift.
        fresh = self.revision_read(value['metadata']['name'])
        if revision_metadata(fresh) != revision_metadata(value) or fresh.get('spec') != value.get('spec'):
            raise RuntimeError('candidate revision changed before promotion')
        verify_traffic_revision(fresh, expected_revision=value['metadata']['name'],
                                expected_commit=self.commit, require_startup_health=True)
        self.stable()
        self.cloud(['gcloud', 'run', 'services', 'update-traffic', self.service, *self.common,
                    '--to-revisions=' + value['metadata']['name'] + '=100', '--quiet'])
        self.expected = self.service_read()
        if active(self.expected) != [(value['metadata']['name'], 100)]:
            raise RuntimeError('candidate traffic shift readback mismatch')

    def restore(self) -> str | None:
        current = self.stable()
        if current == self.before:
            # Deploy failed before changing anything. No minimum/tag to restore.
            return None
        warm = self.candidate(current, '1')
        self.warm = warm['metadata']['name']
        serving = active(current) == [(self.warm, 100)]
        if not serving and active(current) != active(self.before):
            raise RuntimeError('candidate cleanup traffic identity changed')
        tag_removed = True
        if self.tag:
            rows = current['status']['traffic']
            tagged = [row for row in rows if row.get('tag') == self.tag]
            if len(tagged) != 1 or tagged[0].get('revisionName') != self.warm:
                raise RuntimeError('candidate cleanup tag identity mismatch')
            self.stable()
            remove_failed = False
            try:
                self.cloud(['gcloud', 'run', 'services', 'update-traffic', self.service, *self.common,
                            '--remove-tags=' + self.tag, '--quiet'])
            except Exception:
                # Read back an ambiguous mutation, never repeat it. Still attempt
                # the independent template reset if the exact warm binding holds.
                remove_failed = True
            self.expected = self.service_read()
            self.candidate(self.expected, '1')
            tag_removed = not remove_failed and not any(row.get('tag') == self.tag for row in self.expected['status']['traffic'])
            if (self.expected.get('metadata', {}).get('uid') != current.get('metadata', {}).get('uid')
                    or active(self.expected) != active(current)):
                raise RuntimeError('candidate cleanup tag removal changed traffic')
        # --min-instances is revision-level and immutable. --no-traffic prevents
        # this documented replacement revision from implicitly receiving traffic.
        # Never substitute --min (service-level), --to-latest or a health bypass.
        self.stable()
        self.cloud(['gcloud', 'run', 'services', 'update', self.service, *self.common,
                    '--min-instances=0', '--no-traffic', '--deploy-health-check', '--quiet'])
        self.expected = self.service_read()
        reset = self.candidate(self.expected, '0')
        if (reset['metadata']['name'] == self.warm or reset.get('spec') != warm.get('spec')
                or active(self.expected) != active(current)
                or str(self.expected['spec']['template']['metadata'].get('annotations', {}).get(MINIMUM, '0')) != '0'):
            raise RuntimeError('candidate cleanup replacement differs or moved traffic')
        self.reset = reset['metadata']['name']
        if not tag_removed:
            raise RuntimeError('candidate cleanup tag removal not verified; template reset only')
        if serving:
            self.health(reset)
            self.promote(reset)
        final = self.stable()
        if ((self.tag and any(row.get('tag') == self.tag for row in final['status']['traffic']))
                or any(name == self.warm for name, _ in active(final))
                or str(self.revision_read(reset['metadata']['name'])['metadata'].get('annotations', {}).get(MINIMUM, '0')) != '0'):
            raise RuntimeError('candidate cleanup final assertions failed')
        return reset['metadata']['name']

    def execute(self) -> str | None:
        if not re.fullmatch('[0-9a-f]{40}', self.commit) or not re.fullmatch('sha256:[0-9a-f]{64}', self.digest):
            raise ValueError('candidate requires immutable source and image identities')
        if any(arg.startswith(('--min', '--tag', '--set-env', '--update-env', '--remove-env', '--clear-env')) for arg in self.deploy_args):
            raise ValueError('candidate deploy arguments override protected lifecycle')
        for prefix, expected in (('--project=', self.project), ('--region=', self.region)):
            if any(arg.startswith(prefix) and arg[len(prefix):] != expected for arg in self.deploy_args):
                raise ValueError('candidate deploy target binding mismatch')
        if '--no-traffic' not in self.deploy_args or '--deploy-health-check' not in self.deploy_args:
            raise ValueError('candidate deploy requires no-traffic and health check')
        # Exclusive create persists across crashes/restarts; no automatic replay.
        try:
            fd = os.open(self.state_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise RuntimeError('candidate restart requires reconciliation; no replay') from None
        os.close(fd)
        self.record('started')
        self.before = self.expected = self.service_read()
        initial_traffic = active(self.before)
        if len(initial_traffic) != 1 or initial_traffic[0][1] != 100:
            raise RuntimeError('candidate requires one previous serving revision')
        if not all(self.before.get('metadata', {}).get(key) for key in ('uid', 'resourceVersion')):
            raise RuntimeError('candidate service ownership evidence missing')
        previous = self.revision_read(initial_traffic[0][0])
        if str(previous.get('metadata', {}).get('annotations', {}).get(MINIMUM, '0')) != '0':
            raise RuntimeError('candidate previous serving revision minimum is not zero')
        if self.tag and any(row.get('tag') == self.tag for row in self.before['status']['traffic']):
            raise RuntimeError('candidate tag collision; no mutation')
        if str(self.before.get('spec', {}).get('template', {}).get('metadata', {}).get('annotations', {}).get(MINIMUM, '0')) != '0':
            raise RuntimeError('candidate requires previous revision minimum zero')
        final = None
        try:
            try:
                self.stable()
                self.cloud(['gcloud', 'run', 'deploy', self.service, *self.common,
                            *[arg for arg in self.deploy_args if not arg.startswith(('--project=', '--region='))],
                            *(['--tag=' + self.tag] if self.tag else []), '--min-instances=1'])
            finally:
                # Even a timed-out/interrupted deploy may have taken effect.
                current = self.service_read()
                if current.get('metadata', {}).get('uid') != self.before.get('metadata', {}).get('uid'):
                    raise RuntimeError('candidate service ownership changed')
                self.expected = current
            warm = self.candidate(self.expected, '1')
            self.warm = warm['metadata']['name']
            if active(self.expected) != active(self.before):
                raise RuntimeError('candidate deploy unexpectedly moved traffic')
            self.readback('set')
            self.health(warm)
            if self.no_traffic_verify is None:
                raise RuntimeError('candidate original no-traffic verification missing')
            self.no_traffic_verify()
            self.stable()
            self.promote(warm)
            self.record('promoted')
        finally:
            self.cleaning = True
            try:
                final = self.restore()
                self.readback('restore')
                self.record('restored')
            except BaseException:
                self.record('cleanup_failed')
                print('CLEANUP_FAILED: minimum-one billing may remain; no automatic retry or rollback.', flush=True)
                try:
                    self.readback('restore failed')
                except Exception:
                    print('RESTORE_READBACK_UNAVAILABLE; cleanup is not accepted.', flush=True)
                raise RuntimeError('candidate cleanup failed; inspect raw restore readback') from None
        return final


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--eligible', action='store_true')
    parser.add_argument('--image')
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--state-path', type=Path)
    parser.add_argument('deploy_args', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        plan = json.loads(os.environ['SYNC_PLAN_JSON'])
        selected = eligible(plan, os.environ.get('WORKFLOW_TARGET', 'configured'), os.environ.get('INPUT_CONFIGURED_SERVICE', ''))
        if args.eligible:
            print(str(selected).lower())
            return 0
        if not selected or os.environ.get('WARMUP_TRAFFIC_APPROVED') != 'true' or os.environ.get('WARMUP_ENV_SYNC') != 'false':
            raise ValueError('candidate lifecycle requires exact Russell target, traffic approval and no env sync')
        if not args.state_path or not args.image or not args.baseline:
            raise ValueError('candidate lifecycle evidence missing')
        from scripts.verify_cloud_run_no_traffic_deploy import _verify
        baseline_args = argparse.Namespace(
            project=os.environ['GCP_PROJECT_ID'], region=os.environ['CLOUD_RUN_REGION'],
            service=os.environ['INPUT_CONFIGURED_SERVICE'],
            scheduler_location=os.environ.get('CLOUD_SCHEDULER_LOCATION') or os.environ['CLOUD_RUN_REGION'],
            before=args.baseline, expected_sha=os.environ['SOURCE_COMMIT'],
            expected_image_digest=args.image.split('@')[-1])
        release = Release(project=os.environ['GCP_PROJECT_ID'], region=os.environ['CLOUD_RUN_REGION'],
                          service=os.environ['INPUT_CONFIGURED_SERVICE'], commit=os.environ['SOURCE_COMMIT'],
                          digest=args.image.split('@')[-1], run_id=os.environ['GITHUB_RUN_ID'],
                          deploy_args=args.deploy_args[1:] if args.deploy_args[:1] == ['--'] else args.deploy_args,
                          state_path=args.state_path, no_traffic_verify=lambda: _verify(baseline_args))
        def interrupted(signum, frame):
            # A catchable abort enters finally. Once cleanup starts, finish its
            # bounded attempts instead of interrupting the restoration itself.
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            if not release.cleaning:
                raise InterruptedError('candidate lifecycle interrupted')
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        release.execute()
        print('Candidate lifecycle restored: final serving revision minimum is zero; temporary tag absent.')
        return 0
    except (RuntimeError, ValueError, OSError, KeyError) as exc:
        print(redact_notification_diagnostics(str(exc)))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
