"""Render a reviewable deployment plan; never read secrets or mutate cloud state."""
import argparse
import json
import re
from pathlib import Path


def plan(project, region, bucket, prefix, image, telegram_secret, flex_version, telegram_version, account):
    if not re.fullmatch(r'.+@sha256:[0-9a-f]{64}', image):
        raise ValueError('A reviewed immutable image digest is required')
    if not flex_version.isdigit() or not telegram_version.isdigit():
        raise ValueError('Secret versions must be fixed numeric versions')
    pull, watch, scheduler = [f'{prefix}-{name}@{project}.iam.gserviceaccount.com' for name in ('pull', 'watch', 'scheduler')]
    resource = f'projects/_/buckets/{bucket}/objects/'
    def condition(title, prefixes):
        return {'title': title, 'expression': 'resource.type == "storage.googleapis.com/Object" && (' + ' || '.join(
            f'resource.name.startsWith("{resource}{p}")' for p in prefixes) + ')'}
    bindings = []
    for role in ('roles/storage.objectViewer', 'roles/storage.objectCreator'):
        bindings.append({'role': role, 'members': ['serviceAccount:' + pull],
                         'condition': condition('flex-immutable', ['raw/', 'derived/'])})
    bindings.extend([
        {'role': 'roles/storage.objectUser', 'members': ['serviceAccount:' + pull],
         'condition': condition('flex-collector-state', ['state/', 'status/'])},
        {'role': 'roles/storage.objectViewer', 'members': ['serviceAccount:' + watch],
         'condition': condition('flex-redacted-status', ['status/'])},
        {'role': 'roles/storage.objectUser', 'members': ['serviceAccount:' + watch],
         'condition': condition('flex-notification-state', ['notifications/'])},
    ])
    jobs, schedules = [], []
    for mode, suffix, identity, seconds, secret, version, schedule in (
        ('pull', 'daily', pull, 1800, prefix + '-token', flex_version, '17 10 * * *'),
        ('watch', 'watchdog', watch, 300, telegram_secret, telegram_version, '47 * * * *'),
    ):
        name = prefix + '-' + suffix
        mount = '/secrets/flex' if mode == 'pull' else '/secrets/telegram'
        jobs.append({'apiVersion': 'run.googleapis.com/v1', 'kind': 'Job',
                     'metadata': {'name': name, 'namespace': project},
                     'spec': {'template': {'spec': {'taskCount': 1, 'parallelism': 1,
                              'template': {'spec': {'serviceAccountName': identity, 'timeoutSeconds': seconds, 'maxRetries': 0,
                              'containers': [{'image': image, 'args': [mode], 'env': [{'name': 'FLEX_BUCKET', 'value': bucket}],
                                              'resources': {'limits': {'cpu': '1', 'memory': '512Mi'}},
                                              'volumeMounts': [{'name': 'token', 'mountPath': mount}]}],
                              'volumes': [{'name': 'token', 'secret': {'secretName': secret,
                                          'items': [{'key': version, 'path': 'token'}]}}]}}}}}})
        schedules.append({'name': name, 'schedule': schedule, 'timeZone': 'UTC',
                          'uri': f'https://run.googleapis.com/v2/projects/{project}/locations/{region}/jobs/{name}:run',
                          'method': 'POST', 'body': {}, 'oauthServiceAccount': scheduler,
                          'oauthScope': 'https://www.googleapis.com/auth/cloud-platform',
                          'retryCount': 3, 'minBackoff': '60s'})
    return {'project': project, 'region': region, 'account': account,
            'bucket': {'name': bucket, 'location': region, 'storageClass': 'STANDARD',
                       'iamConfiguration': {'uniformBucketLevelAccess': {'enabled': True}, 'publicAccessPrevention': 'enforced'},
                       'versioning': {'enabled': True}, 'softDeletePolicy': {'retentionDurationSeconds': 604800},
                       'lifecycle': {'rule': [{'action': {'type': 'Delete'}, 'condition': {
                           'isLive': False, 'daysSinceNoncurrentTime': 90, 'matchesPrefix': ['state/', 'status/', 'notifications/']}}]}},
            'bucket_iam_additions': bindings, 'jobs': jobs, 'schedulers': schedules,
            'job_iam_additions': [{'job': j['metadata']['name'], 'role': 'roles/run.invoker',
                                   'member': 'serviceAccount:' + scheduler} for j in jobs],
            'secret_iam_additions': [
                {'secret': prefix + '-token', 'member': 'serviceAccount:' + pull, 'role': 'roles/secretmanager.secretAccessor'},
                {'secret': telegram_secret, 'member': 'serviceAccount:' + watch, 'role': 'roles/secretmanager.secretAccessor'}],
            'activation_requirements': ['owner-reviewed PR and image', 'private bucket including inherited IAM',
                                        'token has no IP allowlist', 'fixed token versions exist',
                                        'private notification destination and query configuration verified'],
            'rollback': ['pause both schedulers', 'cancel only these jobs executions',
                         'remove only recorded IAM additions', 'delete only new jobs and service accounts after dependencies removed',
                         'retain bucket and secret versions; data deletion needs separate approval']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('project', 'region', 'bucket', 'prefix', 'image', 'telegram-secret', 'flex-version', 'telegram-version', 'account', 'output'):
        parser.add_argument('--' + name, required=True)
    args = vars(parser.parse_args())
    output = Path(args.pop('output'))
    result = plan(**args)
    output.write_text(json.dumps(result, indent=2) + '\n')
    output.chmod(0o600)


if __name__ == '__main__':
    main()
