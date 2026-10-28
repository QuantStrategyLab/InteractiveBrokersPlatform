import json
from datetime import datetime, timezone
from types import SimpleNamespace

from application.account_facts import build_ibkr_account_facts


def test_confirmed_empty_inventory_is_not_lost_or_inferred_from_unknown():
    snapshot = _snapshot(
        observed_at=datetime(2026, 9, 30, 12, tzinfo=timezone.utc),
        cash_balances=({"account_id": "DU123", "currency": "USD", "CashBalance": 10},),
        broker_stock_positions_empty=True,
    )
    snapshot.positions = ()
    facts = build_ibkr_account_facts(snapshot)
    assert facts["broker_reported_positions"] == []
    assert facts["broker_reported_positions_complete"] is True
    assert facts["broker_reported_positions_scope"] == "stocks_only"
    for flag in (None, False, "true"):
        snapshot.metadata["broker_stock_positions_empty"] = flag
        assert "broker_reported_positions" not in build_ibkr_account_facts(snapshot)
    snapshot.metadata["broker_stock_positions_empty"] = True
    snapshot.positions = None
    assert "broker_reported_positions" not in build_ibkr_account_facts(snapshot)


def _snapshot(*, observed_at=None, account_ids=("DU123",), cash_balances=(), **metadata):
    return SimpleNamespace(
        as_of=observed_at,
        metadata={
            "account_ids": account_ids,
            "cash_balances": cash_balances,
            **metadata,
        },
    )


def test_preserves_native_cash_tags_and_trusted_usd_net_assets():
    snapshot = _snapshot(
        observed_at=datetime(2026, 9, 30, 12, tzinfo=timezone.utc),
        total_equity_source="broker_net_liquidation",
        broker_net_liquidation=125.00001,
        cash_balances=(
            {"account_id": "DU123", "currency": "USD", "NetLiquidation": 125.00001, "$LEDGER-CashBalance": 0, "CashBalance": 99},
            {"account_id": "DU123", "currency": "EUR", "$LEDGER-TotalCashBalance": -1.25},
            {"account_id": "DU123", "currency": "JPY", "SettledCash": 1e-8},
            {"account_id": "DU123", "currency": "BASE", "CashBalance": 9},
            {"account_id": "DU123", "currency": "BASE", "CashBalance": 10},
            {"account_id": "DU123", "currency": "GBP", "CashBalance": 2.5},
            {"account_id": "DU123", "currency": "CAD", "TotalCashBalance": 3.5},
        ),
    )
    facts = build_ibkr_account_facts(snapshot)
    assert facts["account_ids"] == ["DU123"]
    assert facts["currency"] == "USD"
    assert facts["net_assets"] == "125.00001"
    assert facts["cash"] == [
        {"currency": "USD", "cash_balance": "0", "source_tag": "$LEDGER-CashBalance"},
        {"currency": "EUR", "cash_balance": "-1.25", "source_tag": "$LEDGER-TotalCashBalance"},
        {"currency": "JPY", "cash_balance": "0.00000001", "source_tag": "SettledCash"},
        {"currency": "GBP", "cash_balance": "2.5", "source_tag": "CashBalance"},
        {"currency": "CAD", "cash_balance": "3.5", "source_tag": "TotalCashBalance"},
    ]


def test_rejects_unbound_or_incomplete_account_facts():
    observed_at = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
    valid_cash = ({"account_id": "DU123", "currency": "USD", "CashBalance": 1},)
    assert build_ibkr_account_facts(_snapshot(observed_at=observed_at, account_ids=("DU123", "DU456"), cash_balances=valid_cash)) == {}
    assert build_ibkr_account_facts(_snapshot(observed_at=observed_at, cash_balances=valid_cash + ({"account_id": "DU456", "currency": "EUR", "CashBalance": 2},))) == {}
    assert build_ibkr_account_facts(_snapshot(observed_at=datetime(2026, 9, 30, 12), cash_balances=valid_cash)) == {}
    assert build_ibkr_account_facts(_snapshot(cash_balances=valid_cash)) == {}
    assert build_ibkr_account_facts(_snapshot(observed_at=observed_at, cash_balances=({"account_id": "DU123", "currency": "USD"},))) == {}


def test_rejects_duplicate_or_invalid_native_currencies():
    observed_at = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
    duplicated = (
        {"account_id": "DU123", "currency": "USD", "CashBalance": 1},
        {"account_id": "DU123", "currency": "USD", "SettledCash": 2},
    )
    invalid = ({"account_id": "DU123", "currency": "US D", "CashBalance": 1},)
    assert build_ibkr_account_facts(_snapshot(observed_at=observed_at, cash_balances=duplicated)) == {}
    assert build_ibkr_account_facts(_snapshot(observed_at=observed_at, cash_balances=invalid)) == {}


def test_unverified_net_assets_remain_null_with_usd_currency_semantics():
    snapshot = _snapshot(
        observed_at=datetime(2026, 9, 30, 12, tzinfo=timezone.utc),
        cash_balances=({"account_id": "DU123", "currency": "USD", "CashBalance": 1},),
        total_equity_source="unverified_net_liquidation",
        broker_net_liquidation=999,
    )
    facts = build_ibkr_account_facts(snapshot)
    assert facts["currency"] == "USD"
    assert facts["net_assets"] is None


def test_usd_net_assets_require_matching_explicit_usd_net_liquidation():
    observed_at = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
    cash = (
        {"account_id": "DU123", "currency": "USD", "NetLiquidation": 100, "CashBalance": 1},
        {"account_id": "DU123", "currency": "HKD", "CashBalance": 2},
    )
    matched = build_ibkr_account_facts(_snapshot(
        observed_at=observed_at,
        cash_balances=cash,
        total_equity_source="broker_net_liquidation",
        broker_net_liquidation=100,
    ))
    assert matched["net_assets"] == "100"

    mismatch = build_ibkr_account_facts(_snapshot(
        observed_at=observed_at,
        cash_balances=cash,
        total_equity_source="broker_net_liquidation",
        broker_net_liquidation=101,
    ))
    assert mismatch["net_assets"] is None
    assert mismatch["cash"] == [
        {"currency": "USD", "cash_balance": "1", "source_tag": "CashBalance"},
        {"currency": "HKD", "cash_balance": "2", "source_tag": "CashBalance"},
    ]

    base_only = build_ibkr_account_facts(_snapshot(
        observed_at=observed_at,
        cash_balances=(
            {"account_id": "DU123", "currency": "BASE", "NetLiquidation": 100},
            {"account_id": "DU123", "currency": "HKD", "CashBalance": 2},
        ),
        total_equity_source="broker_net_liquidation",
        broker_net_liquidation=100,
    ))
    assert base_only["net_assets"] is None
    assert base_only["cash"] == [{"currency": "HKD", "cash_balance": "2", "source_tag": "CashBalance"}]

    missing = build_ibkr_account_facts(_snapshot(
        observed_at=observed_at,
        cash_balances=({"account_id": "DU123", "currency": "USD", "CashBalance": 1},),
        total_equity_source="broker_net_liquidation",
        broker_net_liquidation=100,
    ))
    assert missing["net_assets"] is None


def test_confirmed_empty_inventory_survives_runtime_report_json(tmp_path):
    from quant_platform_kit.common.runtime_reports import (
        build_runtime_report_base, finalize_runtime_report, write_runtime_report_json,
    )

    snapshot = _snapshot(
        observed_at=datetime(2026, 9, 30, 12, tzinfo=timezone.utc),
        cash_balances=({"account_id": "DU123", "currency": "USD", "CashBalance": 10},),
        broker_stock_positions_empty=True,
    )
    snapshot.positions = ()
    facts = build_ibkr_account_facts(snapshot)
    report = build_runtime_report_base(
        platform="interactive_brokers", deploy_target="offline", service_name="example-runtime",
        strategy_profile="example", run_id="empty-inventory", run_source="test",
    )
    finalize_runtime_report(report, status="ok", summary={"account_facts": facts})
    path = write_runtime_report_json(report, output_path=tmp_path / "report.json")
    stored = json.loads(path.read_text(encoding="utf-8"))["summary"]["account_facts"]
    assert stored["broker_reported_positions"] == []
    assert stored["broker_reported_positions_complete"] is True
    assert stored["broker_reported_positions_scope"] == "stocks_only"


def test_independent_native_cash_fields_survive_without_changing_balance_priority():
    snapshot = _snapshot(
        observed_at=datetime(2026, 9, 30, 12, tzinfo=timezone.utc),
        cash_balances=({"account_id": "DU123", "currency": "USD",
            "$LEDGER-CashBalance": 100, "SettledCash": 60, "AvailableFunds": 75},),
    )
    facts = build_ibkr_account_facts(snapshot)
    assert facts["cash"] == [{"currency": "USD", "cash_balance": "100", "source_tag": "$LEDGER-CashBalance"}]
    observed = facts["broker_cash_observations"]
    assert observed["schema_version"] == "ibkr.cash_observations.v1"
    assert observed["observed_at"] == facts["observed_at"]
    assert observed["values"] == [
        {"currency": "USD", "source_tag": "$LEDGER-CashBalance", "value": "100"},
        {"currency": "USD", "source_tag": "AvailableFunds", "value": "75"},
        {"currency": "USD", "source_tag": "SettledCash", "value": "60"},
    ]
    assert "invalid_fields" not in observed


def test_cash_observations_survive_the_original_runtime_report_writer(tmp_path):
    from quant_platform_kit.common.runtime_reports import (
        build_runtime_report_base, finalize_runtime_report, write_runtime_report_json,
    )
    from application.ibkr_cash_observations import inspect_cash_observations

    snapshot = _snapshot(observed_at=datetime(2026, 9, 30, 12, tzinfo=timezone.utc),
        cash_balances=({"account_id": "DU123", "currency": "USD",
            "CashBalance": 100, "SettledCash": 0, "AvailableFunds": -2},))
    facts = build_ibkr_account_facts(snapshot)
    report = build_runtime_report_base(platform="interactive_brokers", deploy_target="offline",
        service_name="example-runtime", strategy_profile="example", run_id="cash-fields", run_source="test")
    finalize_runtime_report(report, status="ok", summary={"account_facts": facts})
    path = write_runtime_report_json(report, output_path=tmp_path / "report.json")
    stored = json.loads(path.read_text())["summary"]["account_facts"]
    assert stored["broker_cash_observations"] == facts["broker_cash_observations"]
    diagnostic = inspect_cash_observations(stored["broker_cash_observations"],
        observed_at=stored["observed_at"], selected_cash=stored["cash"][0])
    assert diagnostic["fields"]["SettledCash"] == "OBSERVED"
    assert diagnostic["spendable_cash_verified"] is False


def test_raw_cash_observations_cannot_silently_belong_to_another_account():
    snapshot = _snapshot(observed_at=datetime(2026, 9, 30, 12, tzinfo=timezone.utc),
        cash_balances=({"account_id": "DU123", "currency": "USD", "CashBalance": 100},),
        cash_observation_rows=({"account_id": "DU999", "currency": "USD", "SettledCash": "99"},))
    assert build_ibkr_account_facts(snapshot) == {}


def test_schedule_survives_existing_local_and_cloud_report_normalization(tmp_path, monkeypatch):
    from quant_platform_kit.common.runtime_reports import (
        build_runtime_report_base, finalize_runtime_report, persist_runtime_report,
    )
    from quant_platform_kit import cloud
    from application.ibkr_cash_observations import inspect_cash_schedule_observations

    snapshot = _snapshot(observed_at=datetime(2000, 1, 3, 12, tzinfo=timezone.utc),
        cash_balances=({"account_id": "DU123", "currency": "USD", "CashBalance": 100},),
        cash_schedule_rows=(
            {"account_id": "DU123", "currency": "USD", "source_tag": "SettledCashByDate", "raw_value": "20991231:99"},
            {"account_id": "DU123", "currency": "BASE", "source_tag": "SettledCashByDate", "raw_value": "0:98"},
            {"account_id": "DU123", "currency": "USD", "source_tag": "SettledCashByDate-C", "raw_value": None},
        ))
    facts = build_ibkr_account_facts(snapshot)
    report = build_runtime_report_base(platform="interactive_brokers", deploy_target="offline",
        service_name="example-runtime", strategy_profile="example", run_id="schedule-fields", run_source="test")
    finalize_runtime_report(report, status="ok", summary={"account_facts": facts})
    saved = {}
    monkeypatch.setattr(cloud, "get_object_store", lambda **kwargs: SimpleNamespace(
        write_text=lambda uri, text, **kw: saved.update(uri=uri, text=text)))
    persist_runtime_report(report, output_path=tmp_path / "report.json",
        cloud_prefix_uri="gs://synthetic-bucket/synthetic-reports")
    for stored in (json.loads((tmp_path / "report.json").read_text()), json.loads(saved["text"])):
        actual = stored["summary"]["account_facts"]
        assert actual["broker_cash_schedule_observations"] == facts["broker_cash_schedule_observations"]
        assert actual["broker_cash_observations"] == facts["broker_cash_observations"]
        out = inspect_cash_schedule_observations(actual["broker_cash_schedule_observations"], observed_at=actual["observed_at"])
        assert out["fields"]["SettledCashByDate"] == "OBSERVED_RAW"
        assert out["fields"]["SettledCashByDate-C"] == "RAW_NOT_PRESERVED"
        assert not out["spendable_cash_verified"]


def test_optional_schedule_is_not_reconstructed_from_legacy_cache_or_misbound_rows():
    snapshot = _snapshot(observed_at=datetime(2000, 1, 3, 12, tzinfo=timezone.utc),
        cash_balances=({"account_id": "DU123", "currency": "USD", "CashBalance": 100,
            "SettledCashByDate": 100},))
    facts = build_ibkr_account_facts(snapshot)
    assert "broker_cash_schedule_observations" not in facts
    snapshot.metadata["cash_schedule_rows"] = ({"account_id": "DU999", "currency": "USD",
        "source_tag": "SettledCashByDate", "raw_value": "20991231:100"},)
    assert build_ibkr_account_facts(snapshot) == facts


def test_cash_source_scope_survives_private_runtime_report(tmp_path):
    from types import SimpleNamespace
    from application.ibkr_cash_observations import capture_cash_observation_rows, inspect_cash_observations, inspect_cash_source_details
    from quant_platform_kit.common.runtime_reports import build_runtime_report_base, finalize_runtime_report, write_runtime_report_json

    snapshot = _snapshot(observed_at=datetime(2026, 9, 30, 12, tzinfo=timezone.utc),
        cash_balances=({'account_id': 'DU123', 'currency': 'USD', 'CashBalance': 1.25},))
    snapshot.metadata['cash_observation_rows'] = capture_cash_observation_rows([
        SimpleNamespace(account='DU123', currency='USD', tag='CashBalance', value='1.250000', modelCode='')],
        account_ids=('DU123',), source_method='account_values_cache')
    facts = build_ibkr_account_facts(snapshot)
    report = build_runtime_report_base(platform='interactive_brokers', deploy_target='offline',
        service_name='example-runtime', strategy_profile='example', run_id='cash-scope', run_source='test')
    finalize_runtime_report(report, status='ok', summary={'account_facts': facts})
    path = write_runtime_report_json(report, output_path=tmp_path / 'report.json')
    stored = json.loads(path.read_text(encoding='utf-8'))['summary']['account_facts']
    safe = inspect_cash_observations(stored['broker_cash_observations'], observed_at=stored['observed_at'])
    assert set(stored['broker_cash_observations']) <= {'schema_version', 'observed_at', 'values', 'invalid_fields'}
    details = inspect_cash_source_details(stored['broker_cash_source_details'], cash_observations=stored['broker_cash_observations'], observed_at=stored['observed_at'])
    assert details['fields']['CashBalance'] == {
        'source_method': 'account_values_cache', 'model_scope': 'no_model_code',
        'value_representation': 'native_text', 'segment_key': 'unsuffixed'}
    assert safe['spendable_cash_verified'] is False
