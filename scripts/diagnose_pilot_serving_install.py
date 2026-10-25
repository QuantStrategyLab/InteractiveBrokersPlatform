"""Read one TQQQ serving image's install identity without production writes."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from scripts import deploy_pilot_no_traffic as pilot
from scripts import diagnose_serving_image_identity as image_identity
from scripts.verify_image_ues_release_binding import revision_from_lockfile

EQUALITIES = ("ues_stamp_equals_installed_commit", "installed_ues_equals_actual_serving_release",
              "installed_ues_equals_deployment_inventory_release", "installed_ues_equals_source_lock",
              "installed_ues_equals_actual_risk_binding")
REASONS = frozenset({
    "dispatch_not_admitted", "infrastructure_unavailable", "inventory_unavailable", "inventory_invalid",
    "target_not_admitted", "target_not_unique", "rollback_evidence_unavailable", "registry_unavailable",
    "identity_source_unavailable", "invalid_json", "source_commit_unverified", "source_read_failed",
    "source_identity_invalid", "traffic_unavailable", "serving_not_unique", "runtime_target_unavailable",
    "inventory_serving_mismatch", "serving_changed_during_probe", "immutable_image_unavailable",
    "immutable_image_mismatch", "revision_metadata_invalid", "docker_auth_unavailable", "image_pull_failed",
    "image_readback_failed", "image_readback_mismatch", "image_probe_failed", "image_probe_invalid",
    "configuration_incomplete", "container_not_unique", "image_unavailable", "network_configuration_invalid",
    "image_volumes_not_supported", "preflight", "metadata_observed", "dependency_metadata_incomplete",
    "ues_stamp_conflict", "ues_identity_incomplete", "operation_not_readonly", "diagnostic_error",
})


def result() -> dict:
    return {"schema_version": "ibkr.pilot_serving_install.v1", "profile": pilot.PROFILE,
            "status": "unavailable", "reason": "diagnostic_error", "source_stable": False,
            "immutable_image_verified": False, "ues_identity_verified": False,
            "source_lock_status": "unavailable", "dependencies": {key: {"status": "unknown"} for key in image_identity.PACKAGES},
            "equalities": dict.fromkeys(EQUALITIES), "cloud_write_attempted": False,
            "broker_called": False, "execution_authorized": False, "breaker_reset": False}


def command(args, reason, *, env=None):
    args = tuple(args)
    allowed = (("gcloud", "run", "services", "describe"), ("gcloud", "run", "revisions", "describe"),
               ("gcloud", "auth", "configure-docker"), ("docker", "pull"),
               ("docker", "image", "inspect"), ("docker", "run"))
    if not any(args[:len(prefix)] == prefix for prefix in allowed):
        raise pilot.ReleaseError("operation_not_readonly")
    child_env = {**(os.environ if env is None else env), "CLOUDSDK_CORE_DISABLE_FILE_LOGGING": "true",
                 "CLOUDSDK_CORE_LOG_HTTP": "false", "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"}
    return image_identity._command(args, reason, env=child_env)


def _read(ctx, run):
    common = (f"--project={ctx.project}", f"--region={ctx.region}", "--format=json")
    service = pilot._json(run(("gcloud", "run", "services", "describe", ctx.service, *common), "source_read_failed"))
    if not isinstance(service, dict) or service.get("metadata", {}).get("name") != ctx.service:
        raise pilot.ReleaseError("source_identity_invalid")
    traffic, serving = pilot._traffic(service)
    if not serving.startswith(ctx.service + "-"):
        raise pilot.ReleaseError("source_identity_invalid")
    revision = pilot._json(run(("gcloud", "run", "revisions", "describe", serving, *common), "source_read_failed"))
    if not isinstance(revision, dict) or revision.get("metadata", {}).get("name") != serving:
        raise pilot.ReleaseError("source_identity_invalid")
    target = pilot._runtime_target(revision)
    if any(target.get(key) != ctx.target.get(key) for key in pilot.IDENTITY_FIELDS):
        raise pilot.ReleaseError("inventory_serving_mismatch")
    image, _ = image_identity.immutable_image(revision)
    return {"serving": serving, "traffic": traffic, "target": target, "image": image}


def _release_revision(target):
    release = target.get("strategy_release")
    return image_identity._sha(release.get("strategy_revision")) if isinstance(release, dict) else None


def diagnose(ctx, source: Path, run=command) -> dict:
    if pilot.command(["git", "-C", str(source), "rev-parse", "HEAD"], stage="source_commit_unverified").strip() != ctx.source_commit:
        raise pilot.ReleaseError("source_commit_unverified")
    if pilot.command(["git", "-C", str(source), "status", "--porcelain"], stage="source_commit_unverified").strip():
        raise pilot.ReleaseError("source_commit_unverified")
    before = _read(ctx, run)
    image = before["image"]
    with tempfile.TemporaryDirectory(prefix="ibkr-pilot-image-") as directory:
        docker_env = {**os.environ, "DOCKER_CONFIG": directory}
        run(("gcloud", "auth", "configure-docker", image.split("/", 1)[0], "--quiet"), "docker_auth_unavailable", env=docker_env)
        run(("docker", "pull", image), "image_pull_failed", env=docker_env)
        inspected = pilot._json(run(("docker", "image", "inspect", image), "image_readback_failed", env=docker_env))
        if (not isinstance(inspected, list) or len(inspected) != 1 or not isinstance(inspected[0], dict)
                or not isinstance(inspected[0].get("RepoDigests"), list) or image not in inspected[0]["RepoDigests"]):
            raise pilot.ReleaseError("image_readback_mismatch")
        config = inspected[0].get("Config") or {}
        if not isinstance(config, dict) or config.get("Volumes"):
            raise pilot.ReleaseError("image_volumes_not_supported")
        payload = pilot._json(run(("docker", "run", "--rm", "--network=none", "--read-only", "--user=1000:1000",
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=64", "--memory=256m", "--cpus=1",
            "--entrypoint=/usr/local/bin/python", image, "-I", "-S", "-c", image_identity.IMAGE_PROBE),
            "image_probe_failed", env=docker_env))
    dependencies, commits, stamp = image_identity.project_install_metadata(payload)
    after = _read(ctx, run)
    if before != after:
        raise pilot.ReleaseError("serving_changed_during_probe")
    lock = None
    try:
        lock = image_identity._sha(revision_from_lockfile(source / "uv.lock"))
    except Exception:
        pass  # Missing source lock evidence stays unknown, never a fabricated match.
    limits = before["target"].get("runtime_risk_limits")
    binding = limits.get("binding") if isinstance(limits, dict) else None
    risk = image_identity._sha(binding.get("ues_revision")) if isinstance(binding, dict) else None
    stamp_equal = image_identity._equal_sha(stamp, commits["ues"])
    verified = dependencies["ues"]["status"] == "observed" and stamp_equal is True
    complete = all(row["status"] == "observed" for row in dependencies.values())
    out = result()
    out.update(status="observed" if complete and verified else "partial",
               reason="metadata_observed" if complete and verified else "dependency_metadata_incomplete",
               source_stable=True, immutable_image_verified=True, ues_identity_verified=verified,
               source_lock_status="observed" if lock else "unavailable", dependencies=dependencies)
    if stamp_equal is False:
        out.update(status="unavailable", reason="ues_stamp_conflict")
    elif not verified:
        out["reason"] = "ues_identity_incomplete"
    out["equalities"] = dict(zip(EQUALITIES, (
        stamp_equal, image_identity._equal_sha(commits["ues"], _release_revision(before["target"])),
        image_identity._equal_sha(commits["ues"], _release_revision(ctx.target)),
        image_identity._equal_sha(commits["ues"], lock), image_identity._equal_sha(commits["ues"], risk),
    )))
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--bootstrap-identity", action="store_true")
    parser.add_argument("--source-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    out = result()
    try:
        ctx = pilot.context(os.environ)
        if args.preflight:
            if args.bootstrap_identity:
                destination = os.environ.get("GITHUB_ENV")
                if not destination:
                    raise pilot.ReleaseError("identity_source_unavailable")
                pilot.bootstrap_identity(Path(__file__).resolve().parents[1], Path(destination))
            out.update(status="preflight", reason="preflight")
        else:
            out = diagnose(ctx, args.source_dir)
    except (pilot.ReleaseError, image_identity._DiagnosticError) as exc:
        out["reason"] = str(exc) if str(exc) in REASONS else "diagnostic_error"
    except Exception:
        out["reason"] = "diagnostic_error"
    try:
        args.receipt.write_text(json.dumps(out, sort_keys=True) + "\n", encoding="utf-8")
        args.receipt.chmod(0o600)
    except OSError:
        out.update(status="unavailable", reason="diagnostic_error")
    print(json.dumps(out, sort_keys=True))
    return 0 if out["status"] in {"preflight", "observed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
