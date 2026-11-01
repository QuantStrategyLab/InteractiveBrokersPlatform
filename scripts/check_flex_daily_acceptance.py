#!/usr/bin/env python3
"""Decide whether one manual `flex_daily pull` execution may be accepted.

Inputs are the job's final stdout status line and object names only. Object
contents, secrets and account data are never read or printed. A run that exits
zero but stored nothing under `raw/` and `derived/` is not accepted.

Usage:
    python scripts/check_flex_daily_acceptance.py --status status.json --objects names.txt

`status.json` holds the JSON line printed by `python -m application.flex_daily pull`.
`names.txt` lists object names, one per line, for example the output of
`gcloud storage ls --recursive gs://<bucket>/raw/ gs://<bucket>/derived/`.
"""
import argparse
import json
import sys


def has_prefix(names, prefix):
    """True when some name is an object (not a bare folder) under `prefix`."""
    for name in names:
        head, sep, rest = name.partition(prefix) if name.startswith(prefix) else name.partition('/' + prefix)
        if sep and rest and not rest.endswith('/'):
            return True
    return False


def problems(status, names):
    found = []
    if not isinstance(status, dict):
        return ['status_not_an_object']
    if status.get('status') != 'finished':
        found.append('status_not_finished:' + str(status.get('status')))
    fetched = status.get('reports_fetched')
    if type(fetched) is not int or fetched < 1:
        found.append('no_report_fetched')
    if not has_prefix(names, 'raw/'):
        found.append('no_raw_objects')
    if not has_prefix(names, 'derived/'):
        found.append('no_derived_objects')
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--status', required=True, help='file with the final stdout JSON line')
    parser.add_argument('--objects', required=True, help='file with object names, one per line')
    args = parser.parse_args(argv)
    try:
        with open(args.status) as handle:
            status = json.loads(handle.read().strip().splitlines()[-1])
        with open(args.objects) as handle:
            names = [line.strip() for line in handle if line.strip()]
    except (OSError, ValueError, IndexError):
        print(json.dumps({'accepted': False, 'problems': ['unreadable_input']}))
        return 2
    found = problems(status, names)
    print(json.dumps({'accepted': not found, 'problems': found}))
    return 1 if found else 0


if __name__ == '__main__':
    sys.exit(main())
