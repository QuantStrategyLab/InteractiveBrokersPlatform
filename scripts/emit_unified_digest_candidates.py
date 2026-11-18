"""Project existing private reports; no service or broker invocation."""
from __future__ import annotations
import sys
from pathlib import Path
_repo_root = Path(__file__).resolve().parents[1]
for _path in (_repo_root, _repo_root / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
from interactive_brokers_platform.notifications.digest_snapshot.digest_archive import main  # noqa: E402
if __name__ == "__main__":
    raise SystemExit(main())
