import contextlib
import io
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts import cloud_run_runtime_guard as guard


class GuardDisplayPrivacyTests(unittest.TestCase):
    def test_alert_display_is_safe_and_dedupe_identity_is_unchanged(self):
        for locale, expected in (
            ("zh", "检测到 1 条失败记录；原因和当前状态待核对"),
            ("en", "Detected 1 failure record; cause and current status require verification"),
        ):
            with self.subTest(locale=locale):
                service = 'live-u1234-service-<secret>&"'
                project = 'project-u1234-<secret>&"'
                name = 'name-u1234-<secret>&"'
                env = {"GCP_PROJECT_ID": project, "RUNTIME_GUARD_NAME": name,
                       "NOTIFY_LANG": locale, "RUNTIME_GUARD_CHECK_SCHEDULER": "false",
                       "RUNTIME_GUARD_IGNORE_PRE_READY_REVISION_LOGS": "false"}
                messages, decisions = [], []
                decision = SimpleNamespace(send=True, log_line=lambda: "decision")
                with patch.dict(os.environ, env, clear=True), \
                     patch.object(guard, "_load_services", return_value=[service]), \
                     patch.object(guard, "_run_gcloud_logging", return_value=[
                         {"severity": "ERROR", "textPayload": service},
                         {"httpRequest": {"status": 200}},
                     ]), \
                     patch.object(guard.alert_dedupe, "decide", side_effect=lambda **kwargs: decisions.append(kwargs) or decision), \
                     patch.object(guard, "_send_telegram", side_effect=lambda message: messages.append(message) or True), \
                     contextlib.redirect_stdout(io.StringIO()) as stdout:
                    self.assertEqual(guard.main(), 1)
                output = stdout.getvalue() + messages[0]
                self.assertIn(expected, output)
                for value in (service, project, name, "u1234"):
                    self.assertNotIn(value, output)
                self.assertEqual(decisions[0]["name"], name)
                self.assertIn(service, decisions[0]["issues"][0])
                self.assertNotIn("恢复", output)
                self.assertNotIn("recovered", output.lower())

    def test_other_issue_paths_use_safe_display(self):
        for scenario in ("query", "no_success", "scheduler"):
            with self.subTest(scenario=scenario):
                service, project = "private-service-u1234", "private-project-u1234"
                env = {"GCP_PROJECT_ID": project, "RUNTIME_GUARD_NAME": service,
                       "NOTIFY_LANG": "en", "RUNTIME_GUARD_CHECK_SCHEDULER": str(scenario == "scheduler"),
                       "RUNTIME_GUARD_IGNORE_PRE_READY_REVISION_LOGS": "false",
                       "RUNTIME_GUARD_REQUIRE_SUCCESS": str(scenario == "no_success")}
                def query(_project, log_filter, _limit):
                    if scenario == "query":
                        raise RuntimeError(f"{service} {project}")
                    if scenario == "scheduler" and "cloud_scheduler_job" in log_filter:
                        return [{"severity": "ERROR", "resource": {"labels": {"job_id": service + "-scheduler"}}}]
                    return []
                messages = []
                with patch.dict(os.environ, env, clear=True), \
                     patch.object(guard, "_load_services", return_value=[service]), \
                     patch.object(guard, "_run_gcloud_logging", side_effect=query), \
                     patch.object(guard.alert_dedupe, "decide", return_value=SimpleNamespace(send=True, log_line=lambda: "decision")), \
                     patch.object(guard, "_send_telegram", side_effect=lambda message: messages.append(message) or True), \
                     contextlib.redirect_stdout(io.StringIO()) as stdout:
                    self.assertEqual(guard.main(), 1)
                output = stdout.getvalue() + messages[0]
                for value in (service, project, "u1234"):
                    self.assertNotIn(value, output)
                self.assertNotIn("recovered", output.lower())

    def test_invalid_scheduler_pattern_is_an_alert_without_private_pattern(self):
        for locale, expected in (
            ("zh", "调度检查配置无效"),
            ("en", "Scheduler check configuration is invalid"),
        ):
            with self.subTest(locale=locale):
                private_marker = "private_u1234"
                env = {
                    "GCP_PROJECT_ID": "project-u1234",
                    "RUNTIME_GUARD_NAME": "name-u1234",
                    "NOTIFY_LANG": locale,
                    "RUNTIME_GUARD_SCHEDULER_JOB_PATTERN": f"(?P<{private_marker}!>x)",
                    "RUNTIME_GUARD_FAIL_WORKFLOW_ON_ALERT": "false",
                }
                messages, decisions = [], []
                decision = SimpleNamespace(send=True, log_line=lambda: "decision")
                with patch.dict(os.environ, env, clear=True), \
                     patch.object(guard, "_load_services", return_value=[]), \
                     patch.object(guard, "_run_gcloud_logging", return_value=[]), \
                     patch.object(guard.alert_dedupe, "decide", side_effect=lambda **kwargs: decisions.append(kwargs) or decision), \
                     patch.object(guard, "_send_telegram", side_effect=lambda message: messages.append(message) or True), \
                     contextlib.redirect_stdout(io.StringIO()) as stdout, \
                     contextlib.redirect_stderr(io.StringIO()) as stderr:
                    self.assertEqual(guard.main(), 1)
                output = stdout.getvalue() + stderr.getvalue() + messages[0]
                self.assertIn(expected, messages[0])
                self.assertNotIn(private_marker, output)
                self.assertNotIn("u1234", output)
                self.assertEqual(decisions[0]["issues"], [expected])

    def test_healthy_check_and_revision_lookup_do_not_print_identifiers(self):
        service, project = "service-u1234", "project-u1234"
        env = {"GCP_PROJECT_ID": project, "RUNTIME_GUARD_NAME": service,
               "RUNTIME_GUARD_CHECK_SCHEDULER": "false"}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(guard, "_load_services", return_value=[service]), \
             patch.object(guard, "_latest_ready_revision_started_at", side_effect=RuntimeError(service + project)), \
             patch.object(guard, "_run_gcloud_logging", return_value=[]), \
             patch.object(guard.alert_dedupe, "clear_issues"), \
             contextlib.redirect_stdout(io.StringIO()) as stdout, \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(guard.main(), 0)
        self.assertNotIn(service, stdout.getvalue() + stderr.getvalue())
        self.assertNotIn(project, stdout.getvalue() + stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
