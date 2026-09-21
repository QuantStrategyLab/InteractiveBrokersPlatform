#!/usr/bin/env python3
"""Render a validated runtime-target manifest as a GitHub Actions matrix.

Fail-closed: missing or invalid manifests exit non-zero.
Manifest ``enabled`` never filters targets and is never written into the matrix.
Production enablement remains Environment / Cloud Run ``RUNTIME_TARGET_ENABLED``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from application.runtime_target_manifest import (  # noqa: E402
    MATRIX_PROFILES,
    RuntimeTargetManifestError,
    build_github_actions_matrix,
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
        "--profile",
        required=True,
        choices=sorted(MATRIX_PROFILES),
        help="Workflow matrix shape to render.",
    )
    parser.add_argument(
        "--github-output",
        action="store_true",
        help="Append compact matrix=... to $GITHUB_OUTPUT for workflow jobs.",
    )
    args = parser.parse_args(argv)

    try:
        manifest = load_runtime_target_manifest(args.path or default_manifest_path())
        matrix = build_github_actions_matrix(manifest, profile=args.profile)
    except RuntimeTargetManifestError as exc:
        print(f"runtime-target matrix render failed: {exc}", file=sys.stderr)
        return 1

    rows_key = "include" if "include" in matrix else "target"
    for row in matrix[rows_key]:
        if "enabled" in row:
            print(
                "runtime-target matrix render failed: matrix rows must not include enabled",
                file=sys.stderr,
            )
            return 1

    payload = json.dumps(matrix, separators=(",", ":"), sort_keys=True)
    if args.github_output:
        github_output = os.environ.get("GITHUB_OUTPUT")
        if not github_output:
            print(
                "runtime-target matrix render failed: GITHUB_OUTPUT is not set",
                file=sys.stderr,
            )
            return 1
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(f"matrix={payload}\n")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
