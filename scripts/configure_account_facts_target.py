#!/usr/bin/env python3
"""Export one validated additional IBKR target before cloud authentication."""

from __future__ import annotations

import os

try:
    from scripts.publish_account_facts_from_report import (
        _ProjectionError,
        export_additional_target_to_github_env,
    )
except ModuleNotFoundError:
    from publish_account_facts_from_report import (  # type: ignore[no-redef]
        _ProjectionError,
        export_additional_target_to_github_env,
    )


def main() -> int:
    target = os.environ.get("IBKR_ACCOUNT_FACTS_TARGET", "")
    env_path = os.environ.get("GITHUB_ENV", "")
    try:
        export_additional_target_to_github_env(target, env_path)
    except _ProjectionError as exc:
        print(f"skipped:{exc.reason}")
        return 1
    except Exception:
        print("skipped:target_config_invalid")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
