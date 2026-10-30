"""Offline checks using synthetic values; no Docker, cloud or broker access."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import deploy_pilot_no_traffic as pilot
from scripts import verify_image_ues_release_binding as binding

SHA = "a" * 40
PRIVATE = "DO_NOT_OUTPUT_PRIVATE_DIAGNOSTIC"


class BindingReasonTest(unittest.TestCase):
    def test_failure_reasons_are_fixed_and_block_all_writes(self):
        cases = [
            ("image_probe_error", RuntimeError(PRIVATE), SHA, None, SHA),
            ("lockfile_error", SHA, RuntimeError(PRIVATE), None, SHA),
            ("installed_revision_invalid", PRIVATE, SHA, None, SHA),
            ("installed_revision_mismatch", "b" * 40, SHA, None, SHA),
            ("target_binding_mismatch", SHA, SHA, None, "c" * 40),
            ("binding_check_error", SHA, SHA, RuntimeError(PRIVATE), SHA),
        ]
        ctx = SimpleNamespace(source_commit=SHA, service="synthetic", target={"account_selector": ["synthetic"]},
                              registry_host="synthetic-docker.pkg.dev", project="synthetic")
        def command(args, **_kwargs):
            calls.append(args)
            if args[0] == "git":
                return SHA if args[-1] == "HEAD" else ""
            return ""
        def value(result):
            def execute(*_args, **_kwargs):
                if isinstance(result, Exception):
                    raise result
                return result
            return execute
        for expected, image, lock, check_error, target in cases:
            with self.subTest(reason=expected):
                calls, progress = [], {}
                target_value = {"strategy_release": {"strategy_revision": target}}
                output = io.StringIO()
                with patch.object(pilot, "validate_baseline", return_value=target_value), \
                     patch.object(binding, "revision_from_image", value(image)), \
                     patch.object(binding, "revision_from_lockfile", value(lock)), \
                     contextlib.redirect_stdout(output):
                    check_patch = patch.object(binding, "check", value(check_error)) if check_error else contextlib.nullcontext()
                    with check_patch, self.assertRaisesRegex(pilot.ReleaseError, "^image_binding_unverified$"):
                        pilot.release(ctx, Path("synthetic"), {"GITHUB_RUN_ID": "1"}, command,
                                      take_snapshot=lambda *_: {}, progress=progress)
                self.assertEqual(progress["ues_binding_reason"], expected)
                self.assertFalse(progress["image_push_attempted"])
                self.assertFalse(progress["service_update_attempted"])
                self.assertNotIn(PRIVATE, output.getvalue())
                self.assertFalse(any(args[:2] == ["docker", "push"] for args in calls))

    def test_main_persists_only_the_safe_failure_reason(self):
        ctx = SimpleNamespace(
            source_commit=SHA, service="synthetic",
            target={"account_selector": ["synthetic"]},
            registry_host="synthetic-docker.pkg.dev", project="synthetic",
        )
        original_release = pilot.release

        def command(args, **_kwargs):
            return SHA if args[0] == "git" and args[-1] == "HEAD" else ""

        def offline_release(context, source, env, *, progress):
            return original_release(
                context, source, env, command,
                take_snapshot=lambda *_: {}, progress=progress,
            )

        stdout, stderr = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            receipt_path = Path(directory) / "receipt.json"
            with (
                patch.object(pilot, "context", return_value=ctx),
                patch.object(pilot, "release", side_effect=offline_release),
                patch.object(pilot, "validate_baseline", return_value={}),
                patch.object(binding, "revision_from_image", side_effect=RuntimeError(PRIVATE)),
                patch.dict(pilot.os.environ, {"GITHUB_RUN_ID": "1"}),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                code = pilot.main([
                    "--source-dir", "synthetic", "--receipt", str(receipt_path),
                ])
            receipt_text = receipt_path.read_text(encoding="utf-8")
        receipt = json.loads(receipt_text)
        self.assertEqual(code, 1)
        self.assertEqual(receipt["stage"], "image_binding_unverified")
        self.assertEqual(receipt["ues_binding_reason"], "image_probe_error")
        self.assertFalse(receipt["image_push_attempted"])
        self.assertFalse(receipt["service_update_attempted"])
        self.assertNotIn(PRIVATE, stdout.getvalue() + stderr.getvalue() + receipt_text)


if __name__ == "__main__":
    unittest.main()
