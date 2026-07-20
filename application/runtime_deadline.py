"""Bound synchronous Cloud Run work before the platform request deadline."""

from __future__ import annotations

import contextlib
import os
import signal
import threading
import time
from collections.abc import Iterator
from collections.abc import Callable


class RuntimeDeadlineExceeded(BaseException):
    """Raised when a synchronous runtime operation exceeds its local budget.

    This intentionally bypasses broad ``except Exception`` handlers in broker and
    strategy code so an interrupted worker cannot resume with partial session state.
    """


@contextlib.contextmanager
def enforce_runtime_deadline(seconds: int, *, operation: str) -> Iterator[None]:
    """Interrupt main-thread work on POSIX before Cloud Run kills the worker."""
    timeout_seconds = int(seconds)
    can_arm = (
        timeout_seconds > 0
        and threading.current_thread() is threading.main_thread()
        and hasattr(signal, "SIGALRM")
        and hasattr(signal, "setitimer")
    )
    if not can_arm:
        yield
        return

    previous_delay, _previous_interval = signal.getitimer(signal.ITIMER_REAL)
    if previous_delay > 0:
        # Do not replace a deadline owned by the process manager or caller.
        yield
        return

    previous_handler = signal.getsignal(signal.SIGALRM)

    def _raise_deadline(_signum, _frame) -> None:
        raise RuntimeDeadlineExceeded(
            f"{operation} exceeded the {timeout_seconds}s application deadline"
        )

    signal.signal(signal.SIGALRM, _raise_deadline)
    signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def recycle_current_process_after_response(
    *,
    delay_seconds: float = 1.0,
    process_id: int | None = None,
    kill_fn: Callable[[int, int], None] = os.kill,
) -> threading.Thread:
    """Ask the process manager to replace a worker after its response can flush."""
    target_pid = int(process_id or os.getpid())

    def _terminate() -> None:
        time.sleep(max(0.0, float(delay_seconds)))
        kill_fn(target_pid, signal.SIGTERM)

    thread = threading.Thread(
        target=_terminate,
        name="runtime-deadline-worker-recycle",
        daemon=True,
    )
    thread.start()
    return thread
