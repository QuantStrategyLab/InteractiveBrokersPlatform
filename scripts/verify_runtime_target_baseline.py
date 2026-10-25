#!/usr/bin/env python3
"""Reject missing or stale runtime-target baselines before deployment writes."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping
from typing import Any

from scripts.runtime_target_continuity import verify_target_continuity
from scripts.verify_deployed_runtime_target_admission import (
    _container_env,
    _describe_service,
    _runtime_target,
)


def verify_baseline(target: Mapping[str, Any]) -> None:
    # Runtime compatibility allows older targets without continuity. Deployment
    # requires proof of a matching baseline; this check never creates one.
    if not isinstance(target.get("live_continuity"), Mapping):
        raise ValueError("runtime target baseline is required")
    verify_target_continuity(target)


def verify_plan(
    plan: object,
    *,
    deployed_reader: Callable[[str], Mapping[str, Any]] | None = None,
) -> int:
    if not isinstance(plan, Mapping):
        raise ValueError("deployment plan must be an object")
    targets = plan.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ValueError("deployment plan requires targets")
    for entry in targets:
        if not isinstance(entry, Mapping):
            raise ValueError("deployment target must be an object")
        service = entry.get("service_name")
        if not isinstance(service, str) or not service.strip():
            raise ValueError("deployment target requires service_name")
        env = entry.get("env")
        if not isinstance(env, Mapping):
            raise ValueError("deployment target requires environment")
        # Check the serialized target consumed by environment sync, after
        # planner normalization and recovery-ledger application.
        verify_baseline(_runtime_target(env, service=service))
        if deployed_reader is not None:
            # Image rollout inherits service configuration before env sync.
            deployed = deployed_reader(service)
            verify_baseline(_runtime_target(_container_env(deployed), service=service))
    return len(targets)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-deployed", action="store_true")
    parser.add_argument("--project")
    parser.add_argument("--region")
    args = parser.parse_args(argv)
    if args.check_deployed and (not args.project or not args.region):
        parser.error("--check-deployed requires --project and --region")
    try:
        plan = json.loads(os.environ.get("SYNC_PLAN_JSON", ""))
        reader = None
        if args.check_deployed:
            def reader(service: str) -> Mapping[str, Any]:
                return _describe_service(service=service, project=args.project, region=args.region)
        count = verify_plan(plan, deployed_reader=reader)
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        # Cloud CLI errors and malformed private JSON may contain sensitive
        # values. Never echo their text or the target payload.
        print("Runtime target baseline preflight failed: missing, unreadable, or inconsistent baseline/target", file=sys.stderr)
        return 1
    print(f"Runtime target baseline preflight passed: {count} target(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
