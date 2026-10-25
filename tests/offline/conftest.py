"""Use the frozen UES artificial contracts, never private account material."""
import os
from pathlib import Path
import sys

import pytest


source = os.environ.get("IBKR_OFFLINE_UES_SOURCE")
if source:
    sys.path.insert(0, str(Path(source) / "tests" / "research"))
elif os.environ.get("IBKR_REQUIRE_TQQQ_OFFLINE") == "1":
    raise RuntimeError("IBKR_OFFLINE_UES_SOURCE is required for offline acceptance")


def pytest_collection_modifyitems(items):
    if not source:
        for item in items:
            if Path(__file__).parent in Path(item.path).parents:
                item.add_marker(pytest.mark.skip(reason="requires isolated TQQQ research environment"))
