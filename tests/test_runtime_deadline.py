from __future__ import annotations

import time

import pytest

from application.runtime_deadline import RuntimeDeadlineExceeded, enforce_runtime_deadline


def test_runtime_deadline_interrupts_stalled_main_thread_work() -> None:
    started = time.monotonic()

    with pytest.raises(RuntimeDeadlineExceeded, match="test operation exceeded the 1s"):
        with enforce_runtime_deadline(1, operation="test operation"):
            time.sleep(5)

    assert time.monotonic() - started < 2


def test_disabled_runtime_deadline_does_not_interrupt() -> None:
    with enforce_runtime_deadline(0, operation="disabled operation"):
        pass
