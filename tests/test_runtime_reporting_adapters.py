import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quant_platform_kit.common import build_runtime_assembly, build_runtime_target  # noqa: E402
from application.runtime_reporting_adapters import build_runtime_reporting_adapters  # noqa: E402


def test_runtime_reporting_adapters_start_run_builds_report_with_runtime_target():
    observed = {}

    def fake_report_builder(**kwargs):
        observed["report_builder"] = kwargs
        return {"run_id": kwargs["run_id"]}

    adapters = build_runtime_reporting_adapters(
        runtime_assembly=build_runtime_assembly(
            platform="interactive_brokers",
            deploy_target="cloud_run",
            service_name="interactive-brokers-platform",
            strategy_profile="global_etf_rotation",
            runtime_target=build_runtime_target(
                platform_id="interactive_brokers",
                strategy_profile="global_etf_rotation",
                dry_run_only=True,
                account_scope="default",
                service_name="interactive-brokers-platform",
            ),
            account_scope="default",
            account_group="default",
            project_id="project-1",
            instance_name="ib-gateway",
        ),
        strategy_domain="us_equity",
        extra_context_fields={"account_ids": ["U123456"]},
        managed_symbols=("AAA", "BIL"),
        signal_source="market_data",
        status_icon="🐤",
        safe_haven="BIL",
        strategy_display_name="Global ETF Rotation",
        strategy_display_name_localized="全球 ETF 轮动",
        dry_run=True,
        signal_effective_after_trading_days=1,
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
        report_base_dir="/tmp/reports",
        report_gcs_prefix_uri="gs://bucket/reports",
        run_id_builder=lambda: "run-001",
        event_logger=lambda *_args, **_kwargs: {},
        report_builder=fake_report_builder,
        report_persister=lambda *_args, **_kwargs: None,
        trace_extractor=lambda *_args, **_kwargs: "trace-001",
        printer=lambda *_args, **_kwargs: None,
        clock=lambda: datetime(2026, 4, 21, tzinfo=timezone.utc),
    )

    log_context, report = adapters.start_request_run(trace_header="trace-001")

    assert log_context.run_id == "run-001"
    assert log_context.runtime_target.platform_id == "interactive_brokers"
    assert observed["report_builder"]["runtime_target"].platform_id == "interactive_brokers"
    assert observed["report_builder"]["runtime_target"].execution_mode == "paper"
    assert report == {"run_id": "run-001"}
