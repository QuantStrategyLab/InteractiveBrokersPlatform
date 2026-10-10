"""N12-D1 holdings: runtime → archive facts → digest candidates (synthetic only)."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from application.account_facts import (
    POSITIONS_SCOPE,
    build_broker_reported_positions,
    build_ibkr_account_facts,
)
from quant_platform_kit.common.models import Position
import scripts.publish_account_facts_from_report as publisher
from scripts.project_digest_candidates import project_digest_candidates

OBSERVED = datetime(2026, 9, 30, 1, 0, 1, tzinfo=timezone.utc)


def _snapshot(positions=()):
    return SimpleNamespace(
        as_of=OBSERVED,
        positions=tuple(positions),
        metadata={
            "account_ids": ("U00000001",),
            "cash_balances": ({"account_id": "U00000001", "currency": "USD", "CashBalance": 10},),
        },
    )


# --- runtime ---------------------------------------------------------------


def test_runtime_positions_present_written_with_scope_and_avg_cost():
    facts = build_ibkr_account_facts(
        _snapshot(
            [
                Position(symbol="voo", quantity=2.0, market_value=1000.5, average_cost=412.123456789, currency="USD"),
                Position(symbol="BND", quantity=3.0, market_value=210.0, average_cost=None, currency="USD"),
            ]
        )
    )
    assert POSITIONS_SCOPE == "stocks_only"
    assert facts["broker_reported_positions_scope"] == "stocks_only"
    assert facts["broker_reported_positions"] == [
        {"symbol": "BND", "quantity": "3", "market_value": "210", "currency": "USD"},
        {"symbol": "VOO", "quantity": "2", "market_value": "1000.5", "currency": "USD", "avg_cost": "412.12345679"},
    ]


def test_runtime_no_positions_omits_field_not_empty_list():
    facts = build_ibkr_account_facts(_snapshot())
    assert facts["cash"]
    assert "broker_reported_positions" not in facts
    assert "broker_reported_positions_scope" not in facts


def test_runtime_malformed_or_error_positions_omitted_fail_soft():
    bad_currency = Position(symbol="VOO", quantity=1.0, market_value=1.0, currency="")
    facts = build_ibkr_account_facts(_snapshot([bad_currency]))
    assert facts["cash"] and "broker_reported_positions" not in facts

    class Exploding:
        @property
        def positions(self):
            raise RuntimeError("boom")

    assert build_broker_reported_positions(Exploding()) is None
    nan = SimpleNamespace(symbol="VOO", quantity=float("nan"), market_value=1.0, currency="USD", average_cost=None)
    assert build_broker_reported_positions(SimpleNamespace(positions=(nan,))) is None
    dup = (Position("VOO", 1.0, 1.0), Position("VOO", 2.0, 2.0))
    assert build_broker_reported_positions(SimpleNamespace(positions=dup)) is None


# --- archive facts projection ----------------------------------------------


def _report(positions=None, scope=POSITIONS_SCOPE):
    report = {
        "schema_version": "runtime_report.v1",
        "platform": "interactive_brokers",
        "deploy_target": "cloud_run",
        "project_id": "example-project",
        "service_name": "ibkr-primary-service",
        "account_scope": "live-primary",
        "runtime_target": {"account_selector": ["U00000001"], "deployment_selector": "live-primary"},
        "diagnostics": {"runtime_revision": "runtime-revision-001"},
        "started_at": "2026-09-30T01:00:01Z",
        "finished_at": "2026-09-30T01:00:02Z",
        "summary": {
            "account_facts": {
                "schema_version": "ibkr_account_snapshot.v1",
                "account_ids": ["U00000001"],
                "currency": "USD",
                "observed_at": "2026-09-30T01:00:01Z",
                "net_assets": "12345.67",
                "cash": [{"currency": "USD", "cash_balance": "100.00", "source_tag": "CashBalance"}],
            }
        },
    }
    facts = report["summary"]["account_facts"]
    if positions is not None:
        facts["broker_reported_positions"] = positions
    if scope is not None and positions is not None:
        facts["broker_reported_positions_scope"] = scope
    return report


_EXPECTED = dict(
    target_id="ibkr-primary",
    expected_report_prefix="gs://qsl-runtime-reports/ibkr",
    expected_project_id="example-project",
    expected_service_name="ibkr-primary-service",
    expected_runtime_revision="runtime-revision-001",
    expected_account_scope="live-primary",
    expected_account_selector=["U00000001"],
    expected_deployment_selector="live-primary",
)
_URI = "gs://qsl-runtime-reports/ibkr/report-1.json"
_ROW = {"symbol": "VOO", "quantity": "2", "market_value": "1000.5", "currency": "USD", "avg_cost": "412.1"}


def _project(report):
    return publisher.project_ibkr_account_facts_history(report, source_report_uri=_URI, **_EXPECTED)


def test_facts_projection_passes_positions_through():
    body = _project(_report([dict(_ROW)]))
    assert body["broker_reported_positions"] == [_ROW]
    assert body["broker_reported_positions_scope"] == "stocks_only"


def test_facts_projection_absent_or_malformed_positions_omitted():
    for report in (
        _report(),
        _report([]),
        _report([dict(_ROW)], scope="strategy_symbols_only"),
        _report([{**_ROW, "quantity": "1e3"}]),
        _report([{**_ROW, "extra": "x"}]),
        _report([{**_ROW, "currency": "usd"}]),
    ):
        body = _project(report)
        assert "status" not in body, body
        assert "broker_reported_positions" not in body
        assert "broker_reported_positions_scope" not in body


def test_publish_strips_archive_only_positions_before_post(monkeypatch):
    observed = {}

    class Response:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return b'{"ok":true,"stored":true,"unchanged":false}'

    class Opener:
        def open(self, request, *, timeout):
            observed["request"] = request
            return Response()

    monkeypatch.setattr(publisher, "build_opener", lambda *_args: Opener())
    result = publisher.publish_ibkr_account_facts_history(
        _report([dict(_ROW)]),
        now=datetime(2026, 9, 30, 1, 40, 1, tzinfo=timezone.utc),
        source_report_uri=_URI,
        sync_url=publisher.IBKR_ACCOUNT_FACTS_SYNC_URL,
        sync_token="dedicated-test-token",
        **_EXPECTED,
    )
    assert result == {"status": "published"}
    posted = json.loads(observed["request"].data)
    assert "broker_reported_positions" not in posted
    assert "broker_reported_positions_scope" not in posted
    assert posted["account_ids"] == ["U00000001"]


# --- digest candidates ------------------------------------------------------


def _runtime_report():
    return {
        "schema_version": "runtime_report.v1",
        "platform": "interactive_brokers",
        "service_name": "ibkr-synthetic-service",
        "account_scope": "live-synthetic",
        "status": "ok",
        "strategy_profile": "global_etf_rotation",
        "started_at": "2026-10-08T20:01:00Z",
        "finished_at": "2026-10-08T20:02:00Z",
        "runtime_target": {"strategy_profile": "global_etf_rotation", "account_selector": ["U00000001"]},
        "summary": {},
        "execution_receipt": {"outcome": "no_signal"},
    }


def _facts(**extra):
    facts = {
        "schema_version": "ibkr_account_snapshot_history.v1",
        "broker_reported_balances": [{"currency": "USD", "net_assets": "1000"}],
    }
    facts.update(extra)
    return facts


def test_digest_maps_positions_to_holdings_with_scope():
    payload = project_digest_candidates(
        runtime_reports=_runtime_report(),
        account_facts=_facts(broker_reported_positions=[dict(_ROW)], broker_reported_positions_scope="stocks_only"),
    )
    row = payload["runs"][0]
    assert row["holdings"] == [{"symbol": "VOO", "quantity": 2.0, "market_value": 1000.5, "currency": "USD"}]
    assert row["holdings_scope"] == "stocks_only"


def test_digest_absent_or_invalid_positions_omit_holdings():
    short = {**_ROW, "quantity": "-2", "market_value": "-1000.5"}
    for facts in (
        None,
        _facts(),
        {"status": "skipped", "reason": "x"},
        _facts(broker_reported_positions=[], broker_reported_positions_scope="stocks_only"),
        _facts(broker_reported_positions=[dict(_ROW)]),
        _facts(broker_reported_positions=[short], broker_reported_positions_scope="stocks_only"),
        _facts(broker_reported_positions=[{**_ROW, "currency": ""}], broker_reported_positions_scope="stocks_only"),
    ):
        payload = project_digest_candidates(runtime_reports=copy.deepcopy(_runtime_report()), account_facts=facts)
        for row in payload["runs"]:
            assert "holdings" not in row
            assert "holdings_scope" not in row
