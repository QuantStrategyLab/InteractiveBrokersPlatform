"""Preserve masked subprocess failures for operator redeploy helpers.

Successful stdout is returned to the caller in memory. Input and stdout are
never included in diagnostics. Each invocation attempts the command once.
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Callable

from notifications.redaction import redact_notification_diagnostics


def _failure(args: tuple[str, ...], code: int | None, stderr: str | bytes | None,
             status: str, masker: Callable[[str], str] | None) -> RuntimeError:
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    raw = (f"Subprocess {status}; command={json.dumps(args)}; "
           f"exit_code={code if code is not None else 'unknown'}; "
           f"stderr={stderr or '(empty)'}; no automatic mutation retry")
    try:
        safe = redact_notification_diagnostics(raw)
        if masker is not None:
            safe = masker(safe)
        if not isinstance(safe, str) or not safe.strip():
            raise ValueError("Empty diagnostic")
    except Exception:
        return RuntimeError("Diagnostic masking failed; output blocked; no automatic mutation retry")
    return RuntimeError(safe)


def call(*args: str, stdin: str | None = None,
         masker: Callable[[str], str] | None = None) -> str:
    """Run once; fail closed with redacted command, child status and stderr.

    Timeout and launch errors have no observed child exit code. The optional
    masker adds the operator's restricted identifier list to shared redaction.
    Masking failure withholds all command details. Raw exception chains are
    suppressed so traceback rendering cannot bypass redaction.
    """
    try:
        result = subprocess.run(args, input=stdin, capture_output=True,
                                text=True, timeout=90)
    except subprocess.TimeoutExpired as error:
        raise _failure(args, None, error.stderr, "timeout", masker) from None
    except OSError as error:
        raise _failure(args, None, str(error), "launch failed", masker) from None
    if result.returncode:
        raise _failure(args, result.returncode, result.stderr, "failed", masker) from None
    return result.stdout
