"""Apply two narrowly checked maintenance-only changes to the old image main.py."""
from pathlib import Path

OLD_CONNECT = '''    return build_broker_adapters(
        dry_run_only_override=adapter_dry_run_override,
    ).connect_ib(
        validate_trading_permissions=validate_trading_permissions,
        redact_connection_diagnostics=read_only,
    )'''
NEW_CONNECT = '''    from dataclasses import replace
    adapters = build_broker_adapters(dry_run_only_override=adapter_dry_run_override)
    if read_only:
        adapters = replace(adapters, connect_attempts=1)
    return adapters.connect_ib(
        validate_trading_permissions=validate_trading_permissions,
        redact_connection_diagnostics=read_only,
    )'''
OLD_PERSIST = '''    return build_composer(dry_run_only_override=dry_run_only_override).build_reporting_adapters().persist_execution_report(report)'''
NEW_PERSIST = '''    from dataclasses import replace
    adapters = build_composer(dry_run_only_override=dry_run_only_override).build_reporting_adapters()
    diagnostics = report.get("diagnostics")
    if isinstance(diagnostics, Mapping) and diagnostics.get("maintenance_cash_observation") is True:
        if not adapters.report_gcs_prefix_uri:
            raise RuntimeError("maintenance report destination missing")
        adapters = replace(adapters,
            report_base_dir=os.path.join(adapters.report_base_dir, "maintenance-cash") if adapters.report_base_dir else None,
            report_gcs_prefix_uri=adapters.report_gcs_prefix_uri.rstrip("/") + "/maintenance-cash")
    return adapters.persist_execution_report(report)'''


def patch(source):
    if source.count(OLD_CONNECT) != 1 or source.count(OLD_PERSIST) != 1:
        raise ValueError("base_image_source_not_supported")
    return source.replace(OLD_CONNECT, NEW_CONNECT, 1).replace(OLD_PERSIST, NEW_PERSIST, 1)


if __name__ == "__main__":
    path = Path("/app/main.py")
    path.write_text(patch(path.read_text(encoding="utf-8")), encoding="utf-8")
