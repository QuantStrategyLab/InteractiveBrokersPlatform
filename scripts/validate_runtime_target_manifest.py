#!/usr/bin/env python3
"""Validate the checked-in IBKR runtime-target manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from application.runtime_target_manifest import (  # noqa: E402
    RuntimeTargetManifestError,
    default_manifest_path,
    load_runtime_target_manifest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        type=Path,
        default=None,
        help="Manifest path (default: config/runtime_targets.manifest.json)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print a compact validated summary as JSON.",
    )
    args = parser.parse_args(argv)

    try:
        manifest = load_runtime_target_manifest(args.path or default_manifest_path())
    except RuntimeTargetManifestError as exc:
        print(f"runtime-target manifest validation failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        payload = {
            "schema_version": manifest.schema_version,
            "platform_id": manifest.platform_id,
            "target_count": len(manifest.targets),
            "targets": [
                {
                    "id": target.id,
                    "label": target.label,
                    "service": target.service,
                    "region": target.region,
                    "account_group": target.account_group,
                    "strategy_profile": target.strategy_profile,
                    "execution_mode": target.execution_mode,
                    "lifecycle_role": target.lifecycle_role,
                    "enabled": target.enabled,
                    "include_lifecycle": target.include_lifecycle,
                    "include_reconciliation": target.include_reconciliation,
                }
                for target in manifest.targets
            ],
        }
        print(json.dumps(payload, sort_keys=True))
    else:
        print(
            f"OK schema_version={manifest.schema_version} "
            f"platform_id={manifest.platform_id} targets={len(manifest.targets)}"
        )
        for target in manifest.targets:
            print(
                f"- {target.id}: mode={target.execution_mode} role={target.lifecycle_role} "
                f"enabled={target.enabled} service={target.service} "
                f"account_group={target.account_group} region={target.region} "
                f"profile={target.strategy_profile} "
                f"lifecycle={target.include_lifecycle} "
                f"reconciliation={target.include_reconciliation}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
