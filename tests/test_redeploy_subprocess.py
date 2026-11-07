"""Offline regression tests for redeploy helper diagnostics."""
import subprocess
import sys
from unittest.mock import patch

import pytest

from scripts.redeploy_subprocess import call


def test_nonzero_retains_command_exit_stderr_and_masks_bindings():
    with pytest.raises(RuntimeError) as caught:
        call(sys.executable, "-c", "import sys; sys.stderr.write('fake-failure account=DU1234567'); sys.exit(23)")
    message = str(caught.value)
    assert "command=" in message
    assert "exit_code=23" in message
    assert "fake-failure" in message
    assert "DU1234567" not in message


def test_timeout_retains_command_stderr_without_retry():
    error = subprocess.TimeoutExpired(["fake-cli"], 90, stderr=b"fake-timeout account=DU1234567")
    with patch("scripts.redeploy_subprocess.subprocess.run", side_effect=error) as run:
        with pytest.raises(RuntimeError) as caught:
            call("fake-cli")
    assert run.call_count == 1
    assert "command=" in str(caught.value)
    assert "exit_code=unknown" in str(caught.value)
    assert "fake-timeout" in str(caught.value)
    assert "DU1234567" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_spawn_failure_is_masked_and_not_retried():
    with patch("scripts.redeploy_subprocess.subprocess.run", side_effect=OSError("account=DU1234567")) as run:
        with pytest.raises(RuntimeError) as caught:
            call("fake-cli")
    assert run.call_count == 1
    assert "exit_code=unknown" in str(caught.value)
    assert "DU1234567" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_success_payload_stays_private_and_stdin_is_not_reported():
    result = subprocess.CompletedProcess(["fake-cli"], 0, "private-success", "")
    with patch("scripts.redeploy_subprocess.subprocess.run", return_value=result):
        assert call("fake-cli", stdin="private-input") == "private-success"


def test_masker_failure_suppresses_raw_diagnostics():
    result = subprocess.CompletedProcess(["fake-cli"], 23, "", "private-failure")
    with patch("scripts.redeploy_subprocess.subprocess.run", return_value=result):
        with patch("scripts.redeploy_subprocess.redact_notification_diagnostics", side_effect=ValueError("private-masker")):
            with pytest.raises(RuntimeError, match="Diagnostic masking failed") as caught:
                call("fake-cli")
    assert "private" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_operator_masker_covers_restricted_values_and_omits_payloads():
    result = subprocess.CompletedProcess(["fake-cli"], 23, "private-stdout", "fake-binding failure")
    with patch("scripts.redeploy_subprocess.subprocess.run", return_value=result) as run:
        with pytest.raises(RuntimeError) as caught:
            call("fake-cli", "fake-binding", stdin="private-stdin",
                 masker=lambda text: text.replace("fake-binding", "<redacted>"))
    assert run.call_count == 1
    message = str(caught.value)
    assert "fake-cli" in message
    assert "fake-binding" not in message
    assert "private-stdout" not in message
    assert "private-stdin" not in message


def test_operator_masker_failure_blocks_output():
    result = subprocess.CompletedProcess(["fake-cli"], 23, "", "private-failure")
    def broken_masker(text):
        raise ValueError("private-masker")
    with patch("scripts.redeploy_subprocess.subprocess.run", return_value=result):
        with pytest.raises(RuntimeError, match="Diagnostic masking failed") as caught:
            call("fake-cli", masker=broken_masker)
    assert "private" not in str(caught.value)
