from types import SimpleNamespace

import pandas as pd

from application.runtime_strategy_adapters import _coerce_yfinance_candles, build_runtime_strategy_adapters


def test_strategy_plugin_signals_are_loaded_reported_and_rendered():
    observed = {}

    def fake_parse(raw_mounts):
        observed["raw_mounts"] = raw_mounts
        return ("mount-1",)

    def fake_load(mounts, *, strategy_profile):
        observed["load_call"] = (mounts, strategy_profile)
        return (
            SimpleNamespace(
                plugin="crisis_response_shadow",
                effective_mode="shadow",
                canonical_route="no_action",
                suggested_action="watch_only",
            ),
        )

    translations = {
        "strategy_plugin_line": "plugin={plugin}|mode={mode}|route={route}|action={action}",
        "strategy_plugin_name_crisis_response_shadow": "Crisis",
        "strategy_plugin_mode_shadow": "shadow",
        "strategy_plugin_route_no_action": "no action",
        "strategy_plugin_action_watch_only": "watch only",
    }

    adapters = build_runtime_strategy_adapters(
        strategy_runtime=SimpleNamespace(),
        strategy_profile="soxl_soxx_trend_income",
        translator=lambda key, **kwargs: translations.get(key, key).format(**kwargs) if kwargs else translations.get(key, key),
        pacing_sec=0.0,
        resolve_run_as_of_date_fn=lambda: None,
        fetch_historical_price_series_fn=lambda *_args, **_kwargs: SimpleNamespace(points=()),
        fetch_historical_price_candles_fn=lambda *_args, **_kwargs: (),
        map_strategy_decision_fn=lambda *_args, **_kwargs: (),
        build_strategy_plugin_report_payload_fn=lambda signals: {"strategy_plugins": list(signals)},
        load_configured_strategy_plugin_signals_fn=fake_load,
        parse_strategy_plugin_mounts_fn=fake_parse,
    )

    signals, error = adapters.load_strategy_plugin_signals('{"strategy_plugins":[]}')
    report = {}
    adapters.attach_strategy_plugin_report(report, signals=signals, error=error)

    assert error is None
    assert observed["raw_mounts"] == '{"strategy_plugins":[]}'
    assert observed["load_call"] == (("mount-1",), "soxl_soxx_trend_income")
    assert report["summary"]["strategy_plugins"] == list(signals)
    assert adapters.build_strategy_plugin_notification_lines(signals) == (
        "plugin=Crisis|mode=shadow|route=no action|action=watch only",
    )
    assert adapters.build_strategy_plugin_alert_messages(signals) == ()


def test_historical_close_uses_fallback_before_ibkr_history():
    def fail_broker_history(*_args, **_kwargs):
        raise AssertionError("broker historical close should not be called when fallback has data")

    adapters = build_runtime_strategy_adapters(
        strategy_runtime=SimpleNamespace(),
        strategy_profile="tqqq_growth_income",
        translator=lambda key, **_kwargs: key,
        pacing_sec=0.0,
        resolve_run_as_of_date_fn=lambda: None,
        fetch_historical_price_series_fn=fail_broker_history,
        fetch_historical_price_candles_fn=lambda *_args, **_kwargs: (),
        fallback_historical_candles_fn=lambda symbol, **_kwargs: [
            {"as_of": pd.Timestamp("2026-05-21"), "close": 100.0},
            {"as_of": pd.Timestamp("2026-05-22"), "close": 101.0},
        ],
        map_strategy_decision_fn=lambda *_args, **_kwargs: (),
    )

    history = adapters.get_historical_close("fake-ib", "QQQ")

    assert list(history) == [100.0, 101.0]
    assert [str(item.date()) for item in history.index] == ["2026-05-21", "2026-05-22"]


def test_historical_candles_use_fallback_before_ibkr_history():
    def fail_broker_history(*_args, **_kwargs):
        raise AssertionError("broker historical candles should not be called when fallback has data")

    fallback = [{"as_of": pd.Timestamp("2026-05-22"), "close": 101.0}]
    adapters = build_runtime_strategy_adapters(
        strategy_runtime=SimpleNamespace(),
        strategy_profile="tqqq_growth_income",
        translator=lambda key, **_kwargs: key,
        pacing_sec=0.0,
        resolve_run_as_of_date_fn=lambda: None,
        fetch_historical_price_series_fn=lambda *_args, **_kwargs: SimpleNamespace(points=()),
        fetch_historical_price_candles_fn=fail_broker_history,
        fallback_historical_candles_fn=lambda symbol, **_kwargs: fallback,
        map_strategy_decision_fn=lambda *_args, **_kwargs: (),
    )

    assert adapters.get_historical_candles("fake-ib", "QQQ") == fallback


def test_historical_close_uses_ibkr_history_when_fallback_is_empty():
    adapters = build_runtime_strategy_adapters(
        strategy_runtime=SimpleNamespace(),
        strategy_profile="tqqq_growth_income",
        translator=lambda key, **_kwargs: key,
        pacing_sec=0.0,
        resolve_run_as_of_date_fn=lambda: None,
        fetch_historical_price_series_fn=lambda *_args, **_kwargs: SimpleNamespace(
            points=(
                SimpleNamespace(as_of=pd.Timestamp("2026-05-21"), close=99.0),
                SimpleNamespace(as_of=pd.Timestamp("2026-05-22"), close=100.0),
            )
        ),
        fetch_historical_price_candles_fn=lambda *_args, **_kwargs: (),
        fallback_historical_candles_fn=lambda symbol, **_kwargs: (),
        map_strategy_decision_fn=lambda *_args, **_kwargs: (),
    )

    history = adapters.get_historical_close("fake-ib", "QQQ")

    assert list(history) == [99.0, 100.0]


def test_yfinance_candle_coercion_handles_single_symbol_multiindex_columns():
    frame = pd.DataFrame(
        [[100.0, 101.0, 102.0, 99.0, 1_000_000.0]],
        index=pd.to_datetime(["2026-05-22"]),
        columns=pd.MultiIndex.from_tuples(
            [
                ("Open", "QQQ"),
                ("Close", "QQQ"),
                ("High", "QQQ"),
                ("Low", "QQQ"),
                ("Volume", "QQQ"),
            ]
        ),
    )

    candles = _coerce_yfinance_candles(frame)

    assert len(candles) == 1
    assert candles[0]["open"] == 100.0
    assert candles[0]["close"] == 101.0
    assert candles[0]["high"] == 102.0
    assert candles[0]["low"] == 99.0
    assert candles[0]["volume"] == 1_000_000.0


def test_strategy_plugin_true_crisis_builds_escalated_alert_message():
    translations = {
        "strategy_plugin_line": "plugin={plugin}|mode={mode}|route={route}|action={action}",
        "strategy_plugin_alert_subject": "alert:{strategy}:{plugin}:{route}",
        "strategy_plugin_alert_title": "alert title",
        "strategy_plugin_alert_strategy": "strategy={strategy}",
        "strategy_plugin_alert_plugin": "plugin={plugin}",
        "strategy_plugin_alert_status": "route={route}",
        "strategy_plugin_alert_action": "action={action}",
        "strategy_plugin_alert_mode": "mode={mode}",
        "strategy_plugin_alert_as_of": "as_of={as_of}",
        "strategy_plugin_name_crisis_response_shadow": "Crisis",
        "strategy_plugin_mode_shadow": "shadow",
        "strategy_plugin_route_true_crisis": "true crisis",
        "strategy_plugin_action_defend": "defend",
    }
    signal = SimpleNamespace(
        plugin="crisis_response_shadow",
        effective_mode="shadow",
        canonical_route="true_crisis",
        suggested_action="defend",
        would_trade_if_enabled=True,
        as_of="2026-05-24",
        source_uri="gs://bucket/latest_signal.json",
    )
    adapters = build_runtime_strategy_adapters(
        strategy_runtime=SimpleNamespace(),
        strategy_profile="soxl_soxx_trend_income",
        translator=lambda key, **kwargs: translations.get(key, key).format(**kwargs) if kwargs else translations.get(key, key),
        pacing_sec=0.0,
        resolve_run_as_of_date_fn=lambda: None,
        fetch_historical_price_series_fn=lambda *_args, **_kwargs: SimpleNamespace(points=()),
        fetch_historical_price_candles_fn=lambda *_args, **_kwargs: (),
        map_strategy_decision_fn=lambda *_args, **_kwargs: (),
    )

    alerts = adapters.build_strategy_plugin_alert_messages((signal,))

    assert len(alerts) == 1
    assert alerts[0].subject == "alert:soxl_soxx_trend_income:Crisis:true crisis"
    assert "plugin=Crisis" in alerts[0].body
    assert "route=true crisis" in alerts[0].body
    assert "action=defend" in alerts[0].body
    assert "mode=shadow" in alerts[0].body
    assert "would_trade=" not in alerts[0].body
    assert "source=" not in alerts[0].body


def test_strategy_plugin_load_error_is_non_blocking():
    adapters = build_runtime_strategy_adapters(
        strategy_runtime=SimpleNamespace(),
        strategy_profile="soxl_soxx_trend_income",
        translator=lambda key, **_kwargs: key,
        pacing_sec=0.0,
        resolve_run_as_of_date_fn=lambda: None,
        fetch_historical_price_series_fn=lambda *_args, **_kwargs: SimpleNamespace(points=()),
        fetch_historical_price_candles_fn=lambda *_args, **_kwargs: (),
        map_strategy_decision_fn=lambda *_args, **_kwargs: (),
        load_configured_strategy_plugin_signals_fn=lambda *_args, **_kwargs: (),
        parse_strategy_plugin_mounts_fn=lambda _raw: (_ for _ in ()).throw(ValueError("bad config")),
    )

    signals, error = adapters.load_strategy_plugin_signals('{"strategy_plugins":[]}')
    report = {}
    adapters.attach_strategy_plugin_report(report, signals=signals, error=error)

    assert signals == ()
    assert error == "ValueError: bad config"
    assert report["diagnostics"]["strategy_plugin_error"] == "ValueError: bad config"
    assert adapters.build_strategy_plugin_error_notification_lines(error) == (
        "Plugin signal failed to load: ValueError: bad config; this run falls back to built-in strategy rules",
        "Plugin consumption: no plugin signal consumed",
    )
