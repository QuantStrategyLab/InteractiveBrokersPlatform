"""One target-bound cash observation using the existing maintenance reader.

The binding digest must come from a trusted, separately verified target. This
entry does not create a Job or establish cloud, network or storage permissions.
"""
from __future__ import annotations

import contextlib
from collections.abc import Mapping
import hashlib
import io
import json
import logging
import os
import re
import tempfile
from urllib.parse import urlsplit

from scripts.maintenance_cash_job_entry import observe, read_account_summary_cash


PROFILES = frozenset({"global_etf_rotation", "russell_top50_leader_rotation"})
IDENTITY_FIELDS = ("platform_id", "strategy_profile", "service_name", "account_scope",
                   "deployment_selector", "account_selector")


@contextlib.contextmanager
def _quiet_runtime_output():
    """Suppress Python logs as well as prints while private runtime data is used."""
    previous = logging.root.manager.disable
    try:
        logging.disable(logging.CRITICAL)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            yield
    finally:
        logging.disable(previous)


@contextlib.contextmanager
def _temporary_report_dir():
    """Confine the existing report writer's local copy to a private temporary directory."""
    key = "EXECUTION_REPORT_OUTPUT_DIR"
    previous = os.environ.get(key)
    with tempfile.TemporaryDirectory(prefix="ibkr-cash-") as directory:
        os.environ[key] = directory
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


def target_binding_sha256(target):
    """Hash only the existing runtime identity fields; do not emit their values."""
    values = {name: target.get(name) if isinstance(target, Mapping) else getattr(target, name, None)
              for name in IDENTITY_FIELDS}
    selector = values["account_selector"]
    if (not isinstance(selector, (tuple, list)) or len(selector) != 1
            or not isinstance(selector[0], str) or not re.fullmatch(r"(?:U|DU)[0-9]+", selector[0])
            or any(not isinstance(values[name], str) or not values[name].strip()
                   or values[name] != values[name].strip() for name in IDENTITY_FIELDS[:-1])):
        raise ValueError("target_binding_invalid")
    values["account_selector"] = list(selector)
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def report_prefix_sha256(value):
    """Bind the writer to the reviewed GCS prefix without printing the path."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("report_prefix_invalid")
    parsed = urlsplit(value)
    if (parsed.scheme != "gs" or not parsed.netloc or not parsed.path.strip("/")
            or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise ValueError("report_prefix_invalid")
    return hashlib.sha256(value.encode()).hexdigest()


def run(runtime, build_facts, env, *, summary_reader=read_account_summary_cash):
    """Validate identity before any connection and emit a fixed, redacted result."""
    try:
        profile = env.get("CASH_READONLY_PROFILE")
        expected = env.get("CASH_READONLY_TARGET_SHA256", "")
        expected_prefix = env.get("CASH_READONLY_REPORT_PREFIX_SHA256", "")
        settings = runtime.RUNTIME_SETTINGS
        target = settings.runtime_target
        selector = getattr(target, "account_selector", ())
        if (profile not in PROFILES or getattr(runtime, "STRATEGY_PROFILE", None) != profile
                or getattr(target, "strategy_profile", None) != profile
                or getattr(target, "platform_id", None) != "ibkr"
                or getattr(target, "execution_mode", None) != "live"
                or settings.runtime_target_enabled is not False
                or type(runtime.IB_CLIENT_ID) is not int or runtime.IB_CLIENT_ID <= 0
                or not isinstance(runtime.ACCOUNT_IDS, (tuple, list))
                or not isinstance(selector, (tuple, list))
                or tuple(runtime.ACCOUNT_IDS) != tuple(selector)
                or not re.fullmatch(r"[0-9a-f]{64}", expected)
                or target_binding_sha256(target) != expected
                or not re.fullmatch(r"[0-9a-f]{64}", expected_prefix)
                or report_prefix_sha256(env.get("EXECUTION_REPORT_GCS_URI")) != expected_prefix):
            raise ValueError("target_binding_invalid")
        reports = []
        with _quiet_runtime_output(), _temporary_report_dir():
            result = observe(runtime, build_facts, env, summary_reader=summary_reader, report_sink=reports.append)
        namespace = env["EXECUTION_REPORT_GCS_URI"].rstrip("/") + "/maintenance-cash/"
        if (result != 0 or len(reports) != 1 or not isinstance(reports[0], str)
                or not reports[0].startswith(namespace) or not reports[0][len(namespace):]
                or any(part in ("", ".", "..") for part in reports[0][len(namespace):].split("/"))
                or urlsplit(reports[0]).query or urlsplit(reports[0]).fragment):
            raise ValueError("observation_failed")
    except Exception:
        print(json.dumps({"status": "failed"}, sort_keys=True))
        return 1
    print(json.dumps({"status": "observation_saved", "report_uri_sha256":
                      hashlib.sha256(reports[0].encode()).hexdigest()}, sort_keys=True))
    return 0


def main():
    # Imports can initialize runtime configuration and print diagnostics.
    try:
        with _quiet_runtime_output():
            import main as runtime
            from application.account_facts import build_ibkr_account_facts
    except Exception:
        print(json.dumps({"status": "failed"}, sort_keys=True))
        return 1
    return run(runtime, build_ibkr_account_facts, os.environ)


if __name__ == "__main__":
    raise SystemExit(main())
