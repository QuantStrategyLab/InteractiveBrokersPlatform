"""Frozen-candidate import and local-output preflight; no broker or strategy run."""
import base64
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile

LOCK = "b77d42e4d5ed6b644a1fa5c5b037c8dc1957a2d9dfc523045e45f0c89fa86f90"
PINS = {"quant-platform-kit": "bd8d06e50f88927eb525c2c585752435ab24492a",
    "us-equity-strategies": "4a3943883cd6b5bbfe32a559e56a91b40a81b7ce",
    "hk-equity-strategies": "709e5e1cde7841aed538d94eb26b552b46cb7806"}


class IsolationError(RuntimeError):
    pass


def unpack(data, expected, source):
    if len(data) > 2_000_000 or hashlib.sha256(data).hexdigest() != expected:
        raise IsolationError("candidate_bundle_unverified")
    raw = gzip.decompress(data)
    if len(raw) > 8_000_000:
        raise IsolationError("candidate_bundle_unverified")
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for member in archive.getmembers():
            path = source / member.name
            if (not member.isfile() or member.name in seen or not path.resolve().is_relative_to(source)
                    or member.name.startswith("/") or ".." in Path(member.name).parts):
                raise IsolationError("candidate_bundle_unverified")
            seen.add(member.name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.extractfile(member).read())
    if hashlib.sha256((source / "uv.lock").read_bytes()).hexdigest() != LOCK:
        raise IsolationError("candidate_lock_unverified")


def install_audit(root, violations, network_policy=None):
    def reject(kind):
        violations.append(kind)
        raise IsolationError("external_effect_forbidden")
    def check_path(value):
        if isinstance(value, (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(value)).resolve()
            if path != Path("/dev/null") and not path.is_relative_to(root):
                reject("outside_path")
    def audit(event, args):
        if event in {"subprocess.Popen", "os.system"}:
            reject("subprocess")
        if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"} and not (network_policy and network_policy["enabled"]):
            reject("network")
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0])).resolve()
            if any(part in path.parts for part in (".codex", ".aws")) or "/.config/gcloud/" in str(path):
                reject("credential_path")
            mode, flags = args[1:3]
            if (isinstance(mode, str) and any(x in mode for x in "wax+")) or (
                    flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
                check_path(args[0])
        if event in {"os.mkdir", "os.remove", "os.rmdir", "os.rename", "os.link", "os.symlink"}:
            for value in args[:2] if event in {"os.rename", "os.link", "os.symlink"} else args[:1]:
                check_path(value)
    sys.addaudithook(audit)


def isolation(root, source, out):
    from importlib import metadata
    expected = json.loads(os.environ.pop("PILOT_EXPECTED_TARGET"))
    for name, pin in PINS.items():
        direct = json.loads(metadata.distribution(name).read_text("direct_url.json"))
        if direct.get("vcs_info", {}).get("commit_id") != pin:
            raise IsolationError("original_dependencies_unverified")
    if metadata.version("ib-insync") != "0.9.86" or sys.version_info[:2] != (3, 12):
        raise IsolationError("original_dependencies_unverified")
    out["dependencies_verified"] = True
    paths = {"EXECUTION_REPORT_OUTPUT_DIR": root / "reports", "IBKR_RECONCILIATION_OUTPUT_PATH": root / "reconciliation/record.json",
        "IBKR_EXECUTION_STATE_DIR": root / "markers", "LIFECYCLE_LOCAL_ROOT": root / "lifecycle", "TMPDIR": root / "tmp"}
    for key, path in paths.items():
        os.environ[key] = str(path)
        (path.parent if key == "IBKR_RECONCILIATION_OUTPUT_PATH" else path).mkdir(parents=True, exist_ok=True)
    os.environ.update(EXECUTION_REPORT_GCS_URI="", EXECUTION_REPORT_CLOUD_URI="",
        IBKR_EXECUTION_STATE_CLOUD_URI="", IBKR_EXECUTION_STATE_GCS_URI="", PYTHONDONTWRITEBYTECODE="1")
    tempfile.tempdir = str(paths["TMPDIR"])
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(source))
    os.chdir(root)
    violations = []
    network_policy = dict(enabled=False)
    install_audit(root, violations, network_policy)
    out["stage"] = "application_import"
    import ib_insync
    sdk_originals = {(cls, name): getattr(cls, name) for cls, names in (
        (ib_insync.IB, ("connect",)), (ib_insync.client.Client, ("connect", "send", "sendMsg"))) for name in names}
    def forbidden(*args, **kwargs):
        violations.append("broker_notification")
        raise IsolationError("broker_or_notification_forbidden")
    for cls, names in ((ib_insync.IB, ("connect", "placeOrder", "cancelOrder", "reqGlobalCancel", "whatIfOrder", "whatIfOrderAsync")),
            (ib_insync.client.Client, ("connect", "sendMsg", "placeOrder", "cancelOrder", "reqGlobalCancel"))):
        for name in names:
            setattr(cls, name, forbidden)
    out["sdk_guards_installed"] = True
    import main
    if Path(main.__file__).resolve() != source / "main.py":
        raise IsolationError("candidate_import_unverified")
    out["application_imported"] = True
    out["stage"] = "configuration_readback"
    target = main.RUNTIME_SETTINGS.runtime_target
    checks = {key: (tuple(getattr(target, key, ())) == tuple(value) if key == "account_selector"
        else getattr(target, key, None) == value) for key, value in expected["target"].items()}
    checks.update(project_equal=main.PROJECT_ID == expected["project"], account_equal=tuple(main.ACCOUNT_IDS) == tuple(expected["accounts"]),
        host_equal=main.get_ib_host() == expected["host"], port_equal=main.IB_PORT == expected["port"],
        client_equal=main.IB_CLIENT_ID == expected["client_id"], positive_client=type(main.IB_CLIENT_ID) is int and main.IB_CLIENT_ID > 0)
    if not all(checks.values()):
        raise IsolationError("application_binding_unverified")
    out["binding_verified"] = True
    for name in ("send_tg_message", "publish_notification", "publish_strategy_plugin_alerts"):
        setattr(main, name, forbidden)
    out["notification_guards_installed"] = True
    composer = main.build_composer(dry_run_only_override=True)
    reporting = composer.build_reporting_adapters()
    config = composer.build_rebalance_config()
    if (not composer.dry_run_only or not config.dry_run_only or reporting.report_gcs_prefix_uri
            or config.execution_state_store.cloud_prefix_uri
            or Path(reporting.report_base_dir).resolve() != paths["EXECUTION_REPORT_OUTPUT_DIR"]
            or Path(reporting.reconciliation_output_path).resolve() != paths["IBKR_RECONCILIATION_OUTPUT_PATH"]
            or Path(config.execution_state_store.local_dir).resolve() != paths["IBKR_EXECUTION_STATE_DIR"]):
        raise IsolationError("application_outputs_unverified")
    out["outputs_isolated"] = True
    out["stage"] = "isolated_save_readback"
    context, report = reporting.start_request_run()
    report.update(status="preflight", diagnostics={**report.get("diagnostics", {}), "isolation_preflight": True})
    saved = reporting.persist_execution_report(report)
    path = Path(saved).resolve()
    from quant_platform_kit.common.runtime_reports import _normalize_mapping
    if not path.is_relative_to(paths["EXECUTION_REPORT_OUTPUT_DIR"]) or json.loads(path.read_text()) != _normalize_mapping(report):
        raise IsolationError("isolated_report_readback_unverified")
    for key in ("IBKR_EXECUTION_STATE_DIR", "LIFECYCLE_LOCAL_ROOT", "TMPDIR"):
        canary = paths[key] / "isolation-canary"
        canary.write_text("preflight")
        if canary.read_text() != "preflight":
            raise IsolationError("isolated_paths_unverified")
    if violations:
        raise IsolationError("unexpected_external_effect_attempted")
    out.update(isolation_passed=True, stage="isolation_observed", report_saved_readback=True,
        sdk_guards_installed=True, notification_guards_installed=True, external_effect_attempts=0)
    return dict(main=main, sdk=ib_insync, sdk_originals=sdk_originals, expected=expected,
        paths=paths, network_policy=network_policy, violations=violations)


def main():
    cycle_phase = os.environ.get("PILOT_JOB_PHASE") == "cycle"
    cycle = None
    out = dict(schema_version="ibkr.application_isolation.v1", stage="bundle_preflight", isolation_passed=False,
        candidate_source_verified=False, dependencies_verified=False, application_imported=False,
        binding_verified=False, outputs_isolated=False, report_saved_readback=False, sdk_guards_installed=False,
        notification_guards_installed=False, external_effect_attempts=None,
        broker_called=False, strategy_computed=False, production_state_read=False)
    safe_fd = os.dup(1)
    try:
        root = Path(tempfile.mkdtemp(prefix="ibkr-p4-", dir="/tmp")).resolve()
        private_log = (root / "application.private.log").open("wb", buffering=0)
        os.dup2(private_log.fileno(), 1)
        os.dup2(private_log.fileno(), 2)
        source = root / "source"
        source.mkdir()
        unpack(base64.b64decode("".join(sys.argv[2:]), validate=True), sys.argv[1], source)
        out["candidate_source_verified"] = True
        if cycle_phase:
            sys.path.insert(0, str(source))
            import _p4_operator_cycle as operator
            cycle = operator.result()
            counters, private_save = operator.install_cloud_guards()
        context = isolation(root, source, out)
        if cycle_phase:
            cycle = operator.run(context, counters, private_save, os.environ.pop("PILOT_REPORT_STORE"))
    except Exception as error:
        if isinstance(error, IsolationError):
            out["stage"] = str(error)
        else:
            out["failure_class"] = type(error).__name__ if type(error).__name__ in {
                "ImportError", "ModuleNotFoundError", "AttributeError", "TypeError", "ValueError", "PermissionError", "FileNotFoundError"} else "unavailable"
    passed = out["isolation_passed"]
    if cycle_phase:
        out = dict(schema_version="ibkr.guarded_cycle.v1", isolation=out, cycle=cycle)
        passed = bool(passed and cycle and cycle["cycle_completed"])
    os.write(safe_fd, (json.dumps(out, sort_keys=True) + "\n").encode())
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
