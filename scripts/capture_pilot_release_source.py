"""Save one new protected observation of the current TQQQ release source.

This is not a reconstructed release, a policy approval or a recovery action.
Only the new create-only GCS observation may be written; all output is fixed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from scripts import deploy_pilot_no_traffic as pilot
from scripts import diagnose_serving_image_identity as image_identity

TARGET_NAMES = ("RUNTIME_TARGET_JSON", "QSL_RUNTIME_TARGET_JSON")
REPORT_NAME = "EXECUTION_REPORT_GCS_URI"
LAYOUT = "release-source-snapshots/v1"
HASH = re.compile(r"[0-9a-f]{64}\Z")
RELEASE_FIELDS = frozenset({"release_id", "manifest_sha256", "strategy_revision", "config_sha256",
                            "risk_policy_sha256", "evidence_sha256", "plugin_bundle_sha256", "effective_session"})
CREDENTIAL_KEY = re.compile(r"secret|password|passwd|credential|token|privatekey|apikey|accesskey|authorization|cookie|sessionid", re.I)
CREDENTIAL_VALUE = re.compile(
    r"-----BEGIN (?:[A-Z ]*PRIVATE KEY|CERTIFICATE)-----|\b(?:Bearer|Basic)\s+\S+|"
    r"\b(?:AIza[0-9A-Za-z_-]{20,}|AKIA[0-9A-Z]{16}|gh[pousr]_[0-9A-Za-z]{20,}|"
    r"github_pat_[0-9A-Za-z_]{20,}|ya29\.[0-9A-Za-z_-]+)|"
    r"\beyJ[0-9A-Za-z_-]+\.[0-9A-Za-z_-]+\.[0-9A-Za-z_-]+|"
    r"(?:[a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@|"
    r"/secrets/[^/\s]+/versions/[^/\s]+|"
    r"(?:password|passwd|client_secret|api[_-]?key|access[_-]?token|refresh[_-]?token)\s*[=:]", re.I)
REASONS = frozenset({
    "dispatch_not_admitted", "infrastructure_unavailable", "inventory_unavailable", "inventory_invalid",
    "target_not_admitted", "target_not_unique", "rollback_evidence_unavailable", "registry_unavailable",
    "identity_source_unavailable", "invalid_json", "source_commit_unverified", "source_read_failed",
    "source_identity_invalid", "traffic_unavailable", "serving_not_unique", "runtime_target_unavailable",
    "inventory_serving_mismatch", "immutable_image_unavailable", "immutable_image_mismatch",
    "revision_metadata_invalid", "container_not_unique", "image_unavailable", "configuration_incomplete",
    "operation_not_metadata_read", "operation_not_admitted", "compact_release_unverified",
    "credential_like_source", "report_store_unavailable", "cloud_provider_not_gcp", "storage_unavailable",
    "storage_denied", "write_unknown", "readback_unavailable", "readback_mismatch", "source_changed",
    "source_locator_unavailable", "snapshot_identity_unverified", "snapshot_hash_mismatch",
    "preflight", "snapshot_saved", "snapshot_already_present", "snapshot_readback_verified", "capture_error",
})


def result() -> dict:
    return {"schema_version": "ibkr.pilot_release_source_capture.v1", "profile": pilot.PROFILE,
            "status": "unavailable", "reason": "capture_error", "capture_source_commit": None,
            "operation": None, "capture_run_id": None, "layout_version": LAYOUT,
            "source_stable": False, "raw_target_sha256_by_env": None, "bundle_sha256": None,
            "snapshot_sha256": None, "capture_serving_revision_sha256": None,
            "historical_release_equals_current": None,
            "object_create_attempted": False, "object_create_completed": False,
            "object_already_present": False, "readback_verified": False,
            "historical_material_status": "UNAVAILABLE_NOT_RECONSTRUCTED",
            "release_qualified": False, "recovery_authorized": False, "execution_authorized": False,
            "configuration_changed": False, "broker_called": False, "breaker_reset": False}


def command(args, reason):
    args = tuple(args)
    if args[:4] not in (("gcloud", "run", "services", "describe"), ("gcloud", "run", "revisions", "describe")):
        raise pilot.ReleaseError("operation_not_metadata_read")
    return pilot.command(list(args), stage=reason)


def _reject_credentials(value) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if CREDENTIAL_KEY.search(normalized):
                raise pilot.ReleaseError("credential_like_source")
            _reject_credentials(item)
    elif isinstance(value, list):
        for item in value:
            _reject_credentials(item)
    elif isinstance(value, str):
        if CREDENTIAL_VALUE.search(value):
            raise pilot.ReleaseError("credential_like_source")
        # A string may itself contain JSON; inspect it without changing the raw source.
        if value.lstrip().startswith(("{", "[")):
            try:
                nested = pilot._json(value)
            except pilot.ReleaseError:
                return  # Ordinary descriptive strings need not be valid JSON.
            _reject_credentials(nested)


def _literal(env: dict, name: str) -> str:
    item = env.get(name)
    if not isinstance(item, dict) or set(item) != {"name", "value"} or not isinstance(item["value"], str):
        raise pilot.ReleaseError("runtime_target_unavailable" if name in TARGET_NAMES else "report_store_unavailable")
    return item["value"]


def _report_root(uri: str) -> str:
    parsed = urlsplit(uri)
    if (not uri.startswith("gs://") or parsed.scheme != "gs"
            or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]", parsed.netloc)
            or parsed.query or parsed.fragment or not parsed.path.strip("/")
            or any(part in {"", ".", ".."} for part in parsed.path.strip("/").split("/"))
            or any(character in uri for character in ("%", "\\", "\r", "\n", "\t", " "))):
        raise pilot.ReleaseError("report_store_unavailable")
    return uri.rstrip("/")


def _target(raw_by_env) -> dict:
    if (not isinstance(raw_by_env, dict) or not raw_by_env or not set(raw_by_env).issubset(TARGET_NAMES)
            or any(not isinstance(raw, str) or len(raw.encode("utf-8")) > 1_048_576 for raw in raw_by_env.values())):
        raise pilot.ReleaseError("runtime_target_unavailable")
    # Preserve each original string, while retaining the original semantic alias contract.
    revision = {"spec": {"containers": [{"image": "unused", "env": [
        {"name": name, "value": raw} for name, raw in raw_by_env.items()]}]}}
    target = pilot._runtime_target(revision)
    _reject_credentials(target)
    return target


def _compact(target) -> dict:
    compact = target.get("strategy_release")
    if not isinstance(compact, dict) or set(compact) != RELEASE_FIELDS:
        raise pilot.ReleaseError("compact_release_unverified")
    from quant_platform_kit.common.strategy_release import build_strategy_release_identity
    try:
        if build_strategy_release_identity(compact).to_dict() != compact:
            raise ValueError
    except (TypeError, ValueError):
        raise pilot.ReleaseError("compact_release_unverified") from None
    return compact


def observe(ctx, run=command, *, match_inventory_release=True) -> dict:
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
    image, container = image_identity.immutable_image(revision)
    items = container.get("env")
    if not isinstance(items, list) or any(not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in items):
        raise pilot.ReleaseError("runtime_target_unavailable")
    env = {item["name"]: item for item in items}
    if len(env) != len(items):
        raise pilot.ReleaseError("runtime_target_unavailable")
    aliases = [name for name in TARGET_NAMES if name in env]
    if not aliases:
        raise pilot.ReleaseError("runtime_target_unavailable")
    try:
        raw_by_env = {name: _literal(env, name) for name in aliases}
        target = _target(raw_by_env)
    except (ValueError, UnicodeError):
        raise pilot.ReleaseError("runtime_target_unavailable") from None
    if any(target.get(key) != ctx.target.get(key) for key in pilot.IDENTITY_FIELDS):
        raise pilot.ReleaseError("inventory_serving_mismatch")
    compact = _compact(target)
    if match_inventory_release and compact != ctx.target.get("strategy_release"):
        raise pilot.ReleaseError("compact_release_unverified")
    if "QSL_CLOUD_PROVIDER" in env and _literal(env, "QSL_CLOUD_PROVIDER").strip().lower() != "gcp":
        raise pilot.ReleaseError("cloud_provider_not_gcp")
    report = _report_root(_literal(env, REPORT_NAME))
    metadata_keys = ("name", "uid", "generation", "resourceVersion")
    out = {"project": ctx.project, "region": ctx.region,
           "service_metadata": {key: service["metadata"].get(key) for key in metadata_keys},
           "revision_metadata": {key: revision["metadata"].get(key) for key in metadata_keys},
           "revision_observed_commit_label": (revision["metadata"].get("labels") or {}).get("commit-sha"),
           "traffic": traffic, "image": image,
           "raw_target_json_by_env": raw_by_env, "compact_release": compact, "report_cloud_uri": report}
    _reject_credentials(out)
    return out


def object_store(project):
    from quant_platform_kit import cloud
    from quant_platform_kit.cloud.gcp_provider import GcpObjectStore
    if cloud._resolve_provider() != "gcp" or os.environ.get("QSL_CLOUD_PROVIDER", "gcp").strip().lower() != "gcp":
        raise pilot.ReleaseError("cloud_provider_not_gcp")
    store = cloud.get_object_store(project_id=project)
    if type(store) is not GcpObjectStore:
        raise pilot.ReleaseError("storage_unavailable")
    return store


def _blob(store, uri):
    from quant_platform_kit.cloud.gcp_provider import GcpObjectStore
    if type(store) is not GcpObjectStore:
        raise pilot.ReleaseError("storage_unavailable")
    try:
        bucket, path = store._parse_uri(uri)
        return store.client.bucket(bucket).blob(path)
    except Exception:
        raise pilot.ReleaseError("storage_unavailable") from None


def _download(blob) -> bytes:
    from google.api_core.exceptions import Forbidden, NotFound, Unauthorized
    try:
        return blob.download_as_bytes(retry=None, timeout=60)
    except (Forbidden, Unauthorized):
        raise pilot.ReleaseError("storage_denied") from None
    except NotFound:
        raise pilot.ReleaseError("source_locator_unavailable") from None
    except Exception:
        raise pilot.ReleaseError("readback_unavailable") from None


def _save_once(store, uri, payload: bytes, out) -> None:
    """Use the existing QPK GCS client; its create/read helpers lack retry control."""
    from google.api_core.exceptions import Conflict, Forbidden, PreconditionFailed, Unauthorized
    blob = _blob(store, uri)
    out["object_create_attempted"] = True
    try:
        blob.upload_from_string(payload, content_type="application/json", if_generation_match=0, retry=None, timeout=60)
    except (Conflict, PreconditionFailed):
        out["object_already_present"] = True
    except (Forbidden, Unauthorized):
        raise pilot.ReleaseError("storage_denied") from None
    except Exception:
        raise pilot.ReleaseError("write_unknown") from None
    else:
        out["object_create_completed"] = True
    readback = _download(blob)
    if not isinstance(readback, bytes) or readback != payload:
        raise pilot.ReleaseError("readback_mismatch")
    out["readback_verified"] = True


def _encode(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _raw_hashes(observation) -> dict:
    return {name: _sha(raw.encode("utf-8")) for name, raw in observation["raw_target_json_by_env"].items()}


def _verify_source(ctx, source):
    if (pilot.command(["git", "-C", str(source), "rev-parse", "HEAD"], stage="source_commit_unverified").strip() != ctx.source_commit
            or pilot.command(["git", "-C", str(source), "status", "--porcelain"], stage="source_commit_unverified").strip()):
        raise pilot.ReleaseError("source_commit_unverified")


def capture(ctx, source: Path, env, run=command, factory=object_store, *, out=None, now=None) -> dict:
    out = result() if out is None else out
    _verify_source(ctx, source)
    out.update(capture_source_commit=ctx.source_commit, operation="capture", capture_run_id=env["GITHUB_RUN_ID"])
    before = observe(ctx, run)
    store = factory(ctx.project)
    raw_hashes = _raw_hashes(before)
    content_sha = _sha(_encode(before))
    revision_sha = _sha(before["revision_metadata"]["name"].encode("utf-8"))
    uri = f'{before["report_cloud_uri"]}/{LAYOUT}/{env["GITHUB_RUN_ID"]}-{content_sha}.json'
    snapshot = {"schema_version": "ibkr.pilot_release_source_observation.v1", "kind": "new_observation_not_reconstruction",
                "capture_source_commit": ctx.source_commit, "capture_repository": pilot.REPOSITORY,
                "workflow_run_id": env["GITHUB_RUN_ID"],
                "observed_at_utc": (now or datetime.now(timezone.utc)).isoformat(), "snapshot_uri": uri,
                "raw_target_sha256_by_env": raw_hashes, "bundle_sha256": content_sha,
                "capture_serving_revision_sha256": revision_sha, "observation": before,
                "historical_material_status": "UNAVAILABLE_NOT_RECONSTRUCTED",
                "release_qualified": False, "recovery_authorized": False, "execution_authorized": False}
    payload = _encode(snapshot) + b"\n"
    out.update(raw_target_sha256_by_env=raw_hashes, bundle_sha256=content_sha,
               capture_serving_revision_sha256=revision_sha, snapshot_sha256=_sha(payload))
    if observe(ctx, run) != before:
        raise pilot.ReleaseError("source_changed")
    _save_once(store, uri, payload, out)
    if observe(ctx, run) != before:
        raise pilot.ReleaseError("source_changed")
    out.update(status="saved", reason="snapshot_already_present" if out["object_already_present"] else "snapshot_saved",
               source_stable=True)
    return out


def operation(env) -> tuple[str, str, str, str]:
    mode = env.get("PILOT_CAPTURE_OPERATION", "capture")
    locator = tuple(env.get(name, "") for name in ("PILOT_READBACK_RUN_ID", "PILOT_READBACK_BUNDLE_SHA256", "PILOT_READBACK_SNAPSHOT_SHA256"))
    if mode == "capture" and not any(locator):
        return mode, *locator
    if (mode != "readback" or not re.fullmatch(r"[0-9]{1,32}", locator[0])
            or not all(HASH.fullmatch(value) for value in locator[1:])):
        raise pilot.ReleaseError("operation_not_admitted")
    return mode, *locator


def readback(ctx, source, locator, run=command, factory=object_store, *, out=None) -> dict:
    out = result() if out is None else out
    _verify_source(ctx, source)
    run_id, bundle_sha, snapshot_sha = locator
    out.update(capture_source_commit=ctx.source_commit, operation="readback", capture_run_id=run_id)
    before = observe(ctx, run, match_inventory_release=False)
    uri = f'{before["report_cloud_uri"]}/{LAYOUT}/{run_id}-{bundle_sha}.json'
    payload = _download(_blob(factory(ctx.project), uri))
    if not isinstance(payload, bytes) or _sha(payload) != snapshot_sha:
        raise pilot.ReleaseError("snapshot_hash_mismatch")
    snapshot = pilot._json(payload)
    if not isinstance(snapshot, dict):
        raise pilot.ReleaseError("snapshot_identity_unverified")
    _reject_credentials(snapshot)
    observation = snapshot.get("observation")
    if (snapshot.get("schema_version") != "ibkr.pilot_release_source_observation.v1"
            or snapshot.get("kind") != "new_observation_not_reconstruction"
            or snapshot.get("capture_repository") != pilot.REPOSITORY
            or not isinstance(snapshot.get("capture_source_commit"), str)
            or not pilot.SHA.fullmatch(snapshot["capture_source_commit"])
            or snapshot.get("workflow_run_id") != run_id or snapshot.get("snapshot_uri") != uri
            or snapshot.get("bundle_sha256") != bundle_sha or not isinstance(observation, dict)
            or _sha(_encode(observation)) != bundle_sha
            or snapshot.get("historical_material_status") != "UNAVAILABLE_NOT_RECONSTRUCTED"
            or any(snapshot.get(key) is not False for key in ("release_qualified", "recovery_authorized", "execution_authorized"))):
        raise pilot.ReleaseError("snapshot_identity_unverified")
    historical_target = _target(observation.get("raw_target_json_by_env"))
    revision = observation.get("revision_metadata")
    if (any(historical_target.get(key) != ctx.target.get(key) for key in pilot.IDENTITY_FIELDS)
            or observation.get("project") != ctx.project or observation.get("region") != ctx.region
            or observation.get("report_cloud_uri") != before["report_cloud_uri"]
            or observation.get("service_metadata", {}).get("name") != ctx.service
            or not isinstance(revision, dict) or not isinstance(revision.get("name"), str)
            or not revision["name"].startswith(ctx.service + "-")
            or snapshot.get("capture_serving_revision_sha256") != _sha(revision["name"].encode("utf-8"))
            or observation.get("compact_release") != _compact(historical_target)
            or snapshot.get("raw_target_sha256_by_env") != _raw_hashes(observation)):
        raise pilot.ReleaseError("snapshot_identity_unverified")
    if observe(ctx, run, match_inventory_release=False) != before:
        raise pilot.ReleaseError("source_changed")
    out.update(status="readback_verified", reason="snapshot_readback_verified", source_stable=True,
               bundle_sha256=bundle_sha, snapshot_sha256=snapshot_sha, readback_verified=True,
               raw_target_sha256_by_env=snapshot["raw_target_sha256_by_env"],
               capture_serving_revision_sha256=snapshot["capture_serving_revision_sha256"],
               historical_release_equals_current=observation["compact_release"] == before["compact_release"])
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
        out["capture_source_commit"] = ctx.source_commit
        mode, *locator = operation(os.environ)
        out["operation"] = mode
        if args.preflight:
            if args.bootstrap_identity:
                destination = os.environ.get("GITHUB_ENV")
                if not destination:
                    raise pilot.ReleaseError("identity_source_unavailable")
                pilot.bootstrap_identity(Path(__file__).resolve().parents[1], Path(destination))
            out.update(status="preflight", reason="preflight")
        else:
            if mode == "capture":
                capture(ctx, args.source_dir, os.environ, out=out)
            else:
                readback(ctx, args.source_dir, locator, out=out)
    except (pilot.ReleaseError, image_identity._DiagnosticError) as exc:
        out["reason"] = str(exc) if str(exc) in REASONS else "capture_error"
    except Exception:
        out["reason"] = "capture_error"
    try:
        args.receipt.write_text(json.dumps(out, sort_keys=True) + "\n", encoding="utf-8")
        args.receipt.chmod(0o600)
    except OSError:
        out.update(status="unavailable", reason="capture_error")
    print(json.dumps(out, sort_keys=True))
    return 0 if out["status"] in {"preflight", "saved", "readback_verified"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
