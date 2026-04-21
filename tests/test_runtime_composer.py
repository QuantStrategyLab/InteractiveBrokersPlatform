import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from application.runtime_composer import IBKRRuntimeComposer  # noqa: E402


def test_runtime_composer_builds_runtime_and_config_from_local_builders():
    observed = {}

    def fake_notification_builder(**kwargs):
        observed["notification_builder"] = kwargs
        return SimpleNamespace(notification_port="notification-port")

    def fake_reporting_builder(**kwargs):
        observed["reporting_builder"] = kwargs
        return "reporting-adapters"

    composer = IBKRRuntimeComposer(
        service_name="interactive-brokers-platform",
        strategy_profile="global_etf_rotation",
        strategy_domain="us_equity",
        account_group="default",
        project_id="project-1",
        instance_name="ib-gateway",
        account_ids=("U123456",),
        strategy_target_mode="weight",
        strategy_artifact_dir="/tmp/artifacts",
        strategy_display_name="Global ETF Rotation",
        strategy_display_name_localized="全球 ETF 轮动",
        managed_symbols=("AAA", "BIL"),
        signal_source="market_data",
        status_icon="🐤",
        safe_haven="BIL",
        dry_run_only=True,
        strategy_config_source="env",
        ib_gateway_host_resolver=lambda: "127.0.0.1",
        ib_gateway_port=4001,
        ib_gateway_mode="live",
        ib_gateway_ip_mode="internal",
        ib_client_id=1,
        ib_connect_timeout_seconds=60,
        feature_snapshot_path="/tmp/snapshot.csv",
        feature_snapshot_manifest_path="/tmp/snapshot.manifest.json",
        strategy_config_path="/tmp/config.json",
        reconciliation_output_path="/tmp/reconciliation.json",
        translator=lambda key, **_kwargs: key,
        separator="━━━━━━━━━━━━━━━━━━",
        send_message=lambda message: observed.setdefault("sent_message", message),
        connect_ib_fn=lambda: "ib-connection",
        build_portfolio_snapshot_fn=lambda ib: ("portfolio-snapshot", ib),
        compute_signals_fn="compute-signals",
        execute_rebalance_fn="execute-rebalance",
        run_id_builder=lambda: "run-001",
        event_logger="event-logger",
        report_builder="report-builder",
        report_persister="report-persister",
        trace_extractor="trace-extractor",
        env_reader=lambda name, default="": {
            "EXECUTION_REPORT_OUTPUT_DIR": "/tmp/runtime-reports",
            "EXECUTION_REPORT_GCS_URI": "gs://bucket/runtime-reports",
        }.get(name, default),
        printer=lambda *_args, **_kwargs: None,
        notification_builder=fake_notification_builder,
        reporting_builder=fake_reporting_builder,
    )

    notification_adapters = composer.build_notification_adapters()
    reporting_adapters = composer.build_reporting_adapters()
    runtime = composer.build_rebalance_runtime()
    config = composer.build_rebalance_config()

    assert notification_adapters.notification_port == "notification-port"
    assert observed["notification_builder"]["send_message"]
    assert observed["reporting_builder"]["account_scope"] == "default"
    assert observed["reporting_builder"]["managed_symbols"] == ("AAA", "BIL")
    assert runtime.connect_ib() == "ib-connection"
    assert runtime.portfolio_port_factory("ib").get_portfolio_snapshot() == ("portfolio-snapshot", "ib")
    assert runtime.compute_signals == "compute-signals"
    assert runtime.execute_rebalance == "execute-rebalance"
    assert runtime.notifications == "notification-port"
    assert config.separator == "━━━━━━━━━━━━━━━━━━"
    assert config.strategy_display_name == "全球 ETF 轮动"
    assert config.reconciliation_output_path == "/tmp/reconciliation.json"
    assert reporting_adapters == "reporting-adapters"
