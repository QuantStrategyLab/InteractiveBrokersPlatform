"""Opt-in export to one redacted artifact; stdout/stderr carry fixed status only."""

from __future__ import annotations

import http.client
import logging
import os
import re
import sys
import time
from pathlib import Path

from application.ibkr_flex_amount_export import (
    build_redacted_amount_export, serialize_redacted_export, validate_export_scope,
)
from application.ibkr_flex_source import (
    FlexReportPending, collect_activity_flex_xml, request_activity_flex_report,
)
from scripts.diagnose_ibkr_flex_ledger import (
    _approved_report_accounts, _expected_account_ids, _expected_period,
)


ARTIFACT_NAME = "ibkr-flex-redacted-amounts.json"


def _configuration():
    token, query_id = (os.environ.get(k, "") for k in ("IBKR_FLEX_TOKEN", "IBKR_FLEX_QUERY_ID"))
    if (not token or token != token.strip() or any(ord(c) < 32 or ord(c) == 127 for c in token)
            or not re.fullmatch(r"[0-9]+", query_id)):
        raise ValueError
    expected = _expected_account_ids()
    if expected is None:
        raise ValueError
    accounts = _approved_report_accounts(expected) or expected
    period = _expected_period()
    validate_export_scope(accounts, period)
    if any(os.environ.get(k) for k in ("IBKR_FLEX_CASHFLOW_REQUEST_FROM", "IBKR_FLEX_CASHFLOW_REQUEST_TO")):
        raise ValueError
    return token, query_id, accounts, period


def main(argv=()) -> int:
    if tuple(argv) not in (("--preflight",), ("--export-once",)):
        print("flex_amount_export_arguments_invalid")
        return 2
    try:
        config = _configuration()
    except Exception:
        print("flex_amount_export_configuration_invalid")
        return 2
    if tuple(argv) == ("--preflight",):
        print("flex_amount_export_configuration_valid")
        return 0
    if os.environ.get("IBKR_FLEX_AMOUNT_EXPORT_ENABLED") != "true":
        print("flex_amount_export_disabled")
        return 2
    token, query_id, accounts, period = config
    old_logging, old_debug = logging.root.manager.disable, http.client.HTTPConnection.debuglevel
    output = None
    created = False
    try:
        # Even host debug logging must not expose HTTP query-string credentials.
        logging.disable(sys.maxsize)
        http.client.HTTPConnection.debuglevel = 0
        runner_temp = os.environ.get("RUNNER_TEMP")
        if not runner_temp or not Path(runner_temp).is_dir():
            raise ValueError
        output = Path(runner_temp) / ARTIFACT_NAME
        if output.exists() or output.is_symlink():
            raise ValueError
        request = request_activity_flex_report(token=token, query_id=query_id,
            from_date=period["from"], to_date=period["to"])
        time.sleep(1)
        for attempt in range(6):
            try:
                report = collect_activity_flex_xml(request)
                break
            except FlexReportPending as pending:
                if pending.request != request or attempt == 5:
                    raise
                time.sleep(3)
        projection = build_redacted_amount_export(report, account_ids=accounts, period=period)
        content = serialize_redacted_export(projection,
            forbidden_values=(token, query_id, request.reference_code, *accounts))
        # Exclusive create prevents overwriting or following a pre-existing link.
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        print("flex_amount_export_written")
        return 0
    except Exception:
        if created:
            try:
                output.unlink()
            except OSError:
                pass  # The workflow uploads only on success and always cleans up.
        print("flex_amount_export_failed")
        return 1
    finally:
        logging.disable(old_logging)
        http.client.HTTPConnection.debuglevel = old_debug


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
