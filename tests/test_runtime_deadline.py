from __future__ import annotations

import time

import pytest

from application.runtime_deadline import (
    RuntimeDeadlineExceeded,
    enforce_runtime_deadline,
    recycle_current_process_after_response,
)


def test_runtime_deadline_interrupts_stalled_main_thread_work() -> None:
    started = time.monotonic()

    with pytest.raises(RuntimeDeadlineExceeded, match="test operation exceeded the 1s"):
        with enforce_runtime_deadline(1, operation="test operation"):
            time.sleep(5)

    assert time.monotonic() - started < 2


def test_disabled_runtime_deadline_does_not_interrupt() -> None:
    with enforce_runtime_deadline(0, operation="disabled operation"):
        pass


def test_recycle_current_process_sends_sigterm_after_delay() -> None:
    calls = []

    thread = recycle_current_process_after_response(
        delay_seconds=0,
        process_id=1234,
        kill_fn=lambda process_id, signum: calls.append((process_id, signum)),
    )
    thread.join(timeout=1)

    assert calls == [(1234, 15)]
