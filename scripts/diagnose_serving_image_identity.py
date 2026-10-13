#!/usr/bin/env python3
"""Read serving image distribution metadata without running application code."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from typing import Any

try:
    from scripts import diagnose_account_facts_identity as identity
except ModuleNotFoundError:
    import diagnose_account_facts_identity as identity  # type: ignore[no-redef]


SCHEMA = "ibkr.serving_image_identity_diagnostic.v1"
PACKAGES = {"ues": "us-equity-strategies", "qpk": "quant-platform-kit", "plugin": "quant-strategy-plugins"}
_REASONS = frozenset({
    "target_disabled", "target_config_invalid", "target_config_unavailable", "target_config_valid",
    "arguments_invalid", "region_unavailable", "service_metadata_unavailable", "service_metadata_invalid",
    "serving_traffic_unresolved", "revision_identity_invalid", "revision_metadata_unavailable",
    "revision_metadata_invalid", "immutable_image_unavailable", "immutable_image_mismatch",
    "docker_auth_unavailable", "image_pull_failed", "image_readback_failed", "image_readback_mismatch",
    "image_probe_failed", "image_probe_invalid", "serving_changed_during_probe",
    "dependency_metadata_incomplete", "metadata_observed", "diagnostic_error",
})
_IMAGE = re.compile(r"([a-z0-9-]+-docker\.pkg\.dev|(?:(?:us|eu|asia)\.)?gcr\.io)/([a-z0-9][a-z0-9._/-]*)(?::[A-Za-z0-9._-]+|@(sha256:[0-9a-f]{64}))?")
_NAME = re.compile(r"[a-z][a-z0-9-]{0,62}")

# -I -S prevents Python startup hooks, .pth processing and application imports.
# Distribution discovery reads metadata files from explicit image paths only.
IMAGE_PROBE = '''
import json, pathlib, re
from importlib import metadata
names = {"ues": "us-equity-strategies", "qpk": "quant-platform-kit", "plugin": "quant-strategy-plugins"}
paths = sorted({str(p) for root in ("/app/.venv/lib/python", "/usr/local/lib/python")
                for p in pathlib.Path(root.rsplit("/", 1)[0]).glob(root.rsplit("/", 1)[1] + "*/site-packages")})
found = {alias: [] for alias in names}
for dist in metadata.distributions(path=paths):
    name = re.sub(r"[-_.]+", "-", str(dist.metadata.get("Name") or "").lower())
    for alias, expected in names.items():
        if name == expected:
            found[alias].append(dist)
packages = {}
for alias, rows in found.items():
    record = {"present": False, "version": None, "commit_id": None}
    if len(rows) == 1:
        record["present"] = True
        try:
            record["version"] = rows[0].version
            raw = json.loads(rows[0].read_text("direct_url.json") or "{}")
            record["commit_id"] = (raw.get("vcs_info") or {}).get("commit_id")
        except Exception:
            pass
    elif rows:
        record["present"] = None
    packages[alias] = record
stamp = None
try:
    stamp = pathlib.Path("/app/UES_REVISION").read_text().strip()
except OSError:
    pass
print(json.dumps({"schema_version": 1, "packages": packages, "ues_stamp": stamp}, sort_keys=True))
'''


class _DiagnosticError(ValueError):
    pass


def _sha(value: object) -> str | None:
    return value.lower() if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{40}", value) else None


def _result(reason: str, *, status: str = "unknown") -> dict[str, Any]:
    return {
        "schema_version": SCHEMA, "source_sha": _sha(os.environ.get("GITHUB_SHA")),
        "status": status, "reason": reason if reason in _REASONS else "diagnostic_error",
        "serving_traffic_status": "unknown", "revision_metadata_status": "unknown",
        "immutable_image_status": "unknown", "image_probe_status": "unknown",
        "dependencies": {alias: {"status": "unknown"} for alias in PACKAGES},
        "equalities": dict.fromkeys(("configured_equals_serving", "revision_source_equals_image_source",
            "ues_stamp_equals_installed_commit", "ues_equals_deployed_release", "ues_equals_deployed_risk_binding")),
        "independent_release_approval": "unknown",
    }


def _command(args: Sequence[str], reason: str, *, env: Mapping[str, str] | None = None) -> str:
    try:
        result = subprocess.run(tuple(args), capture_output=True, text=True, check=False, timeout=180, env=env)
        if result.returncode or len(result.stdout.encode("utf-8")) > 2 * 1024 * 1024:
            raise _DiagnosticError(reason)
        return result.stdout
    except (OSError, subprocess.TimeoutExpired):
        raise _DiagnosticError(reason) from None


def _json_command(args: Sequence[str], reason: str, *, env: Mapping[str, str] | None = None) -> Any:
    try:
        return json.loads(_command(args, reason, env=env))
    except (json.JSONDecodeError, TypeError):
        raise _DiagnosticError(reason) from None


def _service(expected: Mapping[str, Any], region: str) -> Mapping[str, Any]:
    payload = _json_command(("gcloud", "run", "services", "describe", expected["expected_service_name"],
        "--project", expected["expected_project_id"], "--region", region, "--format=json"), "service_metadata_unavailable")
    if (not isinstance(payload, Mapping) or not isinstance(payload.get("metadata"), Mapping)
        or payload["metadata"].get("name") != expected["expected_service_name"]
        or not isinstance(payload.get("status"), Mapping)):
        raise _DiagnosticError("service_metadata_invalid")
    return payload


def _serving(service: Mapping[str, Any], expected: Mapping[str, Any]) -> tuple[str, str]:
    rows = service["status"].get("traffic")
    traffic = identity._serving_traffic(rows, report_revision=None,
        configured_revision=expected["expected_runtime_revision"])
    if traffic["status"] != "single":
        raise _DiagnosticError("serving_traffic_unresolved")
    revision = next(row["revisionName"] for row in rows if row["percent"] > 0)
    if not _NAME.fullmatch(revision) or not revision.startswith(expected["expected_service_name"] + "-"):
        raise _DiagnosticError("revision_identity_invalid")
    return revision, traffic["status"]


def _image_parts(value: object) -> tuple[str, str, str | None]:
    match = _IMAGE.fullmatch(value) if isinstance(value, str) else None
    if not match or any(p in {"", ".", ".."} for p in match[2].split("/")):
        raise _DiagnosticError("immutable_image_unavailable")
    return match[1], match[2], match[3]


def immutable_image(revision: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
    spec = revision.get("spec")
    containers = spec.get("containers") if isinstance(spec, Mapping) else None
    if not isinstance(containers, list) or len(containers) != 1 or not isinstance(containers[0], Mapping):
        raise _DiagnosticError("revision_metadata_invalid")
    container = containers[0]
    registry, repository, configured_digest = _image_parts(container.get("image"))
    status = revision.get("status")
    resolved = status.get("imageDigest") if isinstance(status, Mapping) else None
    resolved_digest = None
    if resolved is not None:
        if isinstance(resolved, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", resolved):
            resolved_digest = resolved
        else:
            host, path, resolved_digest = _image_parts(resolved)
            if host != registry or path != repository:
                raise _DiagnosticError("immutable_image_mismatch")
        if resolved_digest is None:
            raise _DiagnosticError("immutable_image_unavailable")
    if configured_digest and resolved_digest and configured_digest != resolved_digest:
        raise _DiagnosticError("immutable_image_mismatch")
    digest = configured_digest or resolved_digest
    if digest is None:
        raise _DiagnosticError("immutable_image_unavailable")
    return f"{registry}/{repository}@{digest}", container


def _deployed_ues_bindings(container: Mapping[str, Any]) -> tuple[str | None, str | None]:
    env = container.get("env")
    values = [row.get("value") for row in env if isinstance(row, Mapping) and row.get("name") == "RUNTIME_TARGET_JSON"] if isinstance(env, list) else []
    if len(values) != 1 or not isinstance(values[0], str):
        return None, None
    try:
        target = json.loads(values[0])
    except (json.JSONDecodeError, TypeError):
        return None, None
    release = target.get("strategy_release") if isinstance(target, Mapping) else None
    limits = target.get("runtime_risk_limits") if isinstance(target, Mapping) else None
    binding = limits.get("binding") if isinstance(limits, Mapping) else None
    return (_sha(release.get("strategy_revision")) if isinstance(release, Mapping) else None,
            _sha(binding.get("ues_revision")) if isinstance(binding, Mapping) else None)


def project_install_metadata(payload: object) -> tuple[dict[str, dict[str, str]], dict[str, str | None], str | None]:
    if (not isinstance(payload, Mapping) or set(payload) != {"schema_version", "packages", "ues_stamp"}
        or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1
        or not isinstance(payload.get("packages"), Mapping) or set(payload["packages"]) != set(PACKAGES)):
        raise _DiagnosticError("image_probe_invalid")
    statuses = {}; commits = {}
    for alias in PACKAGES:
        row = payload["packages"][alias]
        status = "invalid"; commit = None
        if isinstance(row, Mapping) and set(row) == {"present", "version", "commit_id"}:
            version = row["version"]
            if row["present"] is False and version is None and row["commit_id"] is None:
                status = "missing"
            elif row["present"] is True and isinstance(version, str) and re.fullmatch(r"[0-9][0-9A-Za-z.!+_-]{0,127}", version):
                commit = _sha(row["commit_id"])
                status = "observed" if commit else "partial" if row["commit_id"] is None else "invalid"
        statuses[alias] = {"status": status}; commits[alias] = commit
    return statuses, commits, _sha(payload["ues_stamp"])


def _equal_sha(left: str | None, right: str | None) -> bool | None:
    return left == right if left is not None and right is not None else None


def diagnose(expected: Mapping[str, Any], region: str) -> dict[str, Any]:
    service = _service(expected, region); serving, traffic_status = _serving(service, expected)
    revision = _json_command(("gcloud", "run", "revisions", "describe", serving, "--project",
        expected["expected_project_id"], "--region", region, "--format=json"), "revision_metadata_unavailable")
    metadata = revision.get("metadata") if isinstance(revision, Mapping) else None
    if not isinstance(metadata, Mapping) or metadata.get("name") != serving:
        raise _DiagnosticError("revision_identity_invalid")
    image, container = immutable_image(revision)
    registry = image.split("/", 1)[0]
    with tempfile.TemporaryDirectory(prefix="ibkr-image-docker-") as directory:
        docker_env = {**os.environ, "DOCKER_CONFIG": directory}
        _command(("gcloud", "auth", "configure-docker", registry, "--quiet"), "docker_auth_unavailable", env=docker_env)
        _command(("docker", "pull", image), "image_pull_failed", env=docker_env)
        inspected = _json_command(("docker", "image", "inspect", image), "image_readback_failed", env=docker_env)
        if (not isinstance(inspected, list) or len(inspected) != 1 or not isinstance(inspected[0], Mapping)
            or not isinstance(inspected[0].get("RepoDigests"), list) or image not in inspected[0]["RepoDigests"]):
            raise _DiagnosticError("image_readback_mismatch")
        payload = _json_command(("docker", "run", "--rm", "--network=none", "--read-only", "--user=1000:1000",
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=64", "--memory=256m", "--cpus=1",
            "--entrypoint=/usr/local/bin/python", image, "-I", "-S", "-c", IMAGE_PROBE), "image_probe_failed", env=docker_env)
    dependencies, commits, stamp = project_install_metadata(payload)
    after, _ = _serving(_service(expected, region), expected)
    if after != serving:
        raise _DiagnosticError("serving_changed_during_probe")
    config = inspected[0].get("Config")
    image_labels = config.get("Labels") if isinstance(config, Mapping) else None
    revision_labels = metadata.get("labels")
    revision_source = _sha(revision_labels.get("commit-sha")) if isinstance(revision_labels, Mapping) else None
    image_source = _sha(image_labels.get("org.opencontainers.image.revision")) if isinstance(image_labels, Mapping) else None
    release, risk = _deployed_ues_bindings(container)
    complete = all(row["status"] == "observed" for row in dependencies.values())
    result = _result("metadata_observed" if complete else "dependency_metadata_incomplete", status="observed" if complete else "partial")
    result.update(serving_traffic_status=traffic_status, revision_metadata_status="read",
        immutable_image_status="verified", image_probe_status="read", dependencies=dependencies)
    result["equalities"] = {
        "configured_equals_serving": serving == expected["expected_runtime_revision"],
        "revision_source_equals_image_source": _equal_sha(revision_source, image_source),
        "ues_stamp_equals_installed_commit": _equal_sha(stamp, commits["ues"]),
        "ues_equals_deployed_release": _equal_sha(commits["ues"], release),
        "ues_equals_deployed_risk_binding": _equal_sha(commits["ues"], risk),
    }
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args not in ([], ["--preflight"]):
        result = _result("arguments_invalid")
    else:
        try:
            expected = identity.resolve_expected()
            if args:
                result = _result("target_config_valid", status="preflight")
            else:
                region = os.environ.get("CLOUD_RUN_REGION", "")
                if re.fullmatch(r"[a-z]+-[a-z]+[0-9]+", region) is None:
                    raise _DiagnosticError("region_unavailable")
                result = diagnose(expected, region)
        except (identity.publisher._ProjectionError, _DiagnosticError) as exc:
            result = _result(str(exc) if str(exc) in _REASONS else "diagnostic_error")
        except Exception:
            result = _result("diagnostic_error")
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] in {"preflight", "observed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
