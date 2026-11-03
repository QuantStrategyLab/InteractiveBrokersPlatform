#!/usr/bin/env python3
"""Fail a build unless installed UES metadata agrees with both dependency pins."""
from __future__ import annotations

import json
from importlib import metadata
from pathlib import Path
import re
import sys
import tomllib
from urllib.parse import parse_qs, urlsplit


SHA = re.compile(r"[0-9a-f]{40}")


def verify(root: Path) -> str:
    configuration = tomllib.loads((root / "pyproject.toml").read_text())
    requirements = [value for value in configuration["project"]["dependencies"]
                    if value.startswith("us-equity-strategies @ git+")]
    if len(requirements) != 1:
        raise ValueError
    url, expected = requirements[0].split(" @ git+", 1)[1].rsplit("@", 1)
    if SHA.fullmatch(expected) is None:
        raise ValueError
    lock = tomllib.loads((root / "uv.lock").read_text())
    packages = [p for p in lock["package"] if p["name"] == "us-equity-strategies"]
    if len(packages) != 1:
        raise ValueError
    source = urlsplit(packages[0]["source"]["git"])
    if (source.fragment != expected or parse_qs(source.query) != {"rev": [expected]}
            or source._replace(query="", fragment="").geturl() != url):
        raise ValueError
    direct = json.loads(metadata.distribution("us-equity-strategies").read_text("direct_url.json") or "null")
    if (direct["url"] != url or direct["vcs_info"]["vcs"] != "git"
            or direct["vcs_info"]["commit_id"] != expected):
        raise ValueError
    stamp = root / "UES_REVISION"
    if stamp.exists() and stamp.read_text().strip() != expected:
        raise ValueError
    return expected


def main() -> int:
    try:
        root = Path.cwd()
        revision = verify(root)
        # Build-local file; no runtime writer or shared persisted state.
        (root / "UES_REVISION").write_text(revision + "\n", encoding="utf-8")
    except (OSError, ValueError, TypeError, KeyError, AttributeError, metadata.PackageNotFoundError):
        # Never echo metadata, source URLs, file contents or exception diagnostics.
        print("UES installed revision verification failed", file=sys.stderr)
        return 1
    print("UES installed revision matches project and lock pins")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
