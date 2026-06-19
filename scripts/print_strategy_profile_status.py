from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QPK_SRC = ROOT.parent / "QuantPlatformKit" / "src"
UES_SRC = ROOT.parent / "UsEquityStrategies" / "src"
HES_SRC = ROOT.parent / "HkEquityStrategies" / "src"


def _has_catalog_marker(candidate: Path, package_name: str, marker: str) -> bool:
    catalog_path = candidate / package_name / "catalog.py"
    if not catalog_path.exists():
        return False
    return marker in catalog_path.read_text(encoding="utf-8")


def _should_add_local_src(candidate: Path) -> bool:
    if candidate == QPK_SRC:
        return (candidate / "quant_platform_kit" / "common" / "runtime_target.py").exists()
    if candidate == UES_SRC:
        return _has_catalog_marker(
            candidate,
            "us_equity_strategies",
            "russell_top50_leader_rotation_aggressive",
        )
    if candidate == HES_SRC:
        return _has_catalog_marker(
            candidate,
            "hk_equity_strategies",
            "hk_global_etf_tactical_rotation",
        )
    return True


for candidate in (ROOT, QPK_SRC, UES_SRC, HES_SRC):
    if not _should_add_local_src(candidate):
        continue
    candidate_str = str(candidate)
    if candidate_str not in sys.path:
        sys.path.insert(0, candidate_str)

from strategy_registry import (  # noqa: E402
    IBKR_PLATFORM,
    describe_platform_runtime_requirements,
    get_platform_profile_status_matrix,
)


def build_status_rows() -> list[dict[str, object]]:
    rows = []
    for row in get_platform_profile_status_matrix():
        rows.append(
            {
                **row,
                **describe_platform_runtime_requirements(
                    row["canonical_profile"],
                    platform_id=IBKR_PLATFORM,
                ),
            }
        )
    return rows


def _print_table(rows: list[dict[str, object]]) -> None:
    headers = (
        "canonical_profile",
        "display_name",
        "display_name_zh",
        "profile_group",
        "input_mode",
        "requires_snapshot_artifacts",
        "requires_strategy_config_path",
        "eligible",
        "enabled",
        "domain",
    )
    widths = {
        header: max(len(header), *(len(str(row.get(header, ""))) for row in rows))
        for header in headers
    }
    print("  ".join(header.ljust(widths[header]) for header in headers))
    print("  ".join("-" * widths[header] for header in headers))
    for row in rows:
        print("  ".join(str(row.get(header, "")).ljust(widths[header]) for header in headers))


def main() -> int:
    rows = build_status_rows()
    if "--json" in sys.argv:
        print(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    _print_table(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
