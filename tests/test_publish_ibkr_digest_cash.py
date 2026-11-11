from datetime import date

import pytest

from application.ibkr_flex_source import FlexReport, FlexSourceError
from scripts.publish_ibkr_digest_cash import project_digest_cash


BINDINGS = {f"U000000{i}": (f"slot-{i}", f"opaque-{i}", f"target-{i}") for i in range(4)}


def _report(*, extra=False, wrong_cash=False):
    accounts = [*BINDINGS, "U0000099"] if extra else list(BINDINGS)
    statements = []
    for i, account in enumerate(accounts):
        cash_account = accounts[0] if wrong_cash and i == 2 else account
        statements.append(
            f'<FlexStatement accountId="{account}" fromDate="20261015" toDate="20261015">'
            f'<CashReport><CashReportCurrency accountId="{cash_account}" currency="USD" model="" '
            f'fromDate="20261015" toDate="20261015" endingCash="500" endingSettledCash="-{i}.50"/></CashReport>'
            '<Trades><Trade accountId="U0000099"/></Trades></FlexStatement>'
        )
    raw = ('<FlexQueryResponse type="AF"><FlexStatements>' + ''.join(statements)
           + '</FlexStatements></FlexQueryResponse>').encode()
    return FlexReport(content_sha256="a" * 64, content=raw)


def _project(report, latest=date(2026, 10, 15)):
    return project_digest_cash(report, expected_primary=(next(iter(BINDINGS)),), allowed_accounts=tuple(BINDINGS),
                               bindings=BINDINGS, latest_day=latest)


def test_projects_each_cash_segment_without_broker_account_ids():
    result = _project(_report())
    assert set(result) == {f"slot-{i}" for i in range(4)}
    assert result["slot-3"] == {"schema_version": "ibkr_digest_settled_cash.v1", "target_id": "target-3",
                                "opaque_account_uid": "opaque-3", "statement_date": "2026-10-15",
                                "currency": "USD", "amount": "-3.50"}
    assert "U0000" not in repr(result)


def test_bad_cash_binding_only_excludes_affected_account_and_future_dates_are_ignored():
    assert set(_project(_report(wrong_cash=True))) == {"slot-0", "slot-1", "slot-3"}
    assert _project(_report(), date(2026, 10, 14)) == {}
    with pytest.raises(FlexSourceError):
        _project(_report(extra=True))
