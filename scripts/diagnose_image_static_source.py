"""Compare fixed public files in an immutable image without executing its layers."""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath

try:
    from scripts import diagnose_account_facts_identity as identity
    from scripts import diagnose_serving_image_identity as image
except ModuleNotFoundError:
    import diagnose_account_facts_identity as identity
    import diagnose_serving_image_identity as image


SOURCE_FILES = {
    "entrypoint": "main.py",
    "strategy_adapter": "application/runtime_strategy_adapters.py",
    "composer": "application/runtime_composer.py",
    "dependencies": "application/runtime_dependencies.py",
    "broker_adapter": "application/runtime_broker_adapters.py",
    "reporting_adapter": "application/runtime_reporting_adapters.py",
    "project": "pyproject.toml", "lock": "uv.lock",
}
PACKAGES = {"ues": ("us-equity-strategies", "UsEquityStrategies"),
            "qpk": ("quant-platform-kit", "QuantPlatformKit")}
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_METADATA = re.compile(r"app/\.venv/lib/python3\.[0-9]+/site-packages/"
    r"(us_equity_strategies|quant_platform_kit)-[A-Za-z0-9.!+_-]+\.dist-info/(METADATA|direct_url\.json)")
_MANIFEST = {"application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"}
_INDEX = {"application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json"}
_CONFIG = {"application/vnd.oci.image.config.v1+json", "application/vnd.docker.container.image.v1+json"}
_LAYERS = {"application/vnd.oci.image.layer.v1.tar": False,
           "application/vnd.oci.image.layer.v1.tar+gzip": True,
           "application/vnd.docker.image.rootfs.diff.tar.gzip": True}
_REASONS = image._REASONS | identity._SOURCE_REASONS | frozenset({
    "static_context_invalid", "static_candidate_unavailable", "static_candidate_invalid", "candidate_read_failed",
    "static_metadata_invalid", "registry_auth_unavailable", "registry_read_failed", "registry_redirect_blocked",
    "content_digest_mismatch", "descriptor_size_mismatch", "static_budget_exceeded", "static_platform_invalid",
    "static_layer_unsupported", "static_tar_invalid", "static_path_invalid", "static_diff_id_mismatch",
    "selected_files_incomplete", "selected_files_differ", "dependency_metadata_incomplete", "static_files_match",
})


class StaticError(ValueError):
    def __init__(self, reason, *, redirect_metadata=None):
        super().__init__(reason)
        self.redirect_metadata = redirect_metadata


@dataclass
class Budget:
    max_download: int = 1_500_000_000
    max_uncompressed: int = 4 * 1024**3
    max_entries: int = 250_000
    max_source: int = 2 * 1024**2
    started: float = field(default_factory=time.monotonic)
    downloaded: int = 0
    uncompressed: int = 0
    entries: int = 0
    source_bytes: int = 0

    def check(self):
        if time.monotonic() - self.started > 480:
            raise StaticError("static_budget_exceeded")

    def download(self, count):
        self.check()
        self.downloaded += count
        if self.downloaded > self.max_download:
            raise StaticError("static_budget_exceeded")

    def entry(self):
        self.check()
        self.entries += 1
        if self.entries > self.max_entries:
            raise StaticError("static_budget_exceeded")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _redirect_result():
    return {"request_stage": None, "http_status": None, "location_present": None,
            "location_https_default_443": None, "location_has_query": None,
            "destination_class": None, "redirect_followed": False, "authorization_forwarded": False}


def _redirect_metadata(stage, code, headers, origin):
    """Classify one bounded Location in memory; this never grants hop permission."""
    result = _redirect_result()
    result["request_stage"] = stage if isinstance(stage, str) and stage in {"manifest", "config", "layer"} else None
    if type(code) is not int or code not in {301, 302, 303, 307, 308}:
        return result
    result["http_status"] = code
    try:
        if headers is None:
            values = []
        elif hasattr(headers, "get_all"):
            values = headers.get_all("Location")
            values = [] if values is None else values
        else:
            value = headers.get("Location")
            values = [] if value is None else [value]
        if not isinstance(values, list):
            return result
        result["location_present"] = bool(values)
        if not values:
            return result
        result["destination_class"] = "other_or_invalid"
        if len(values) != 1 or not isinstance(values[0], str):
            return result
        location = values[0]
        if len(location) > 8192 or not location.isascii() or re.search(r"[\x00-\x20\x7f]", location):
            return result
        parsed = urllib.parse.urlsplit(location)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or not re.fullmatch(r"[a-z0-9.-]+", parsed.hostname) or parsed.username is not None
            or parsed.password is not None or parsed.fragment):
            return result
        port = parsed.port  # malformed ports are rejected without exposing text
        result["location_https_default_443"] = parsed.scheme == "https" and port in {None, 443}
        result["location_has_query"] = "?" in location
        expected = urllib.parse.urlsplit(origin)
        if (result["location_https_default_443"] and parsed.hostname == expected.hostname
            and expected.scheme == "https" and expected.port in {None, 443}):
            result["destination_class"] = "same_registry_origin"
        elif result["location_https_default_443"] and parsed.hostname == "storage.googleapis.com":
            result["destination_class"] = "exact_google_storage_hostname"
    except Exception:
        # Unknown header or URL shape remains nullable; no raw value is retained.
        pass
    return result


def _json(raw):
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise StaticError("static_metadata_invalid")
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, TypeError, UnicodeError):
        raise StaticError("static_metadata_invalid") from None


class Registry:
    def __init__(self, immutable, token, budget, *, opener=None):
        host, repository, digest = image._image_parts(immutable)
        if not digest:
            raise StaticError("immutable_image_unavailable")
        if not token or "\n" in token or "\r" in token:
            raise StaticError("registry_auth_unavailable")
        self.base = "https://" + host + "/v2/" + repository
        self.digest, self.budget = digest, budget
        self.authorization = "Basic " + base64.b64encode(("oauth2accesstoken:" + token).encode()).decode()
        self.opener = opener if opener is not None else urllib.request.build_opener(NoRedirect())

    def fetch(self, kind, digest, size, limit, *, memory=False, stage=None):
        if (kind not in {"manifests", "blobs"} or not isinstance(digest, str) or not _SHA256.fullmatch(digest)
            or size is not None and (type(size) is not int or not 0 < size <= limit)):
            raise StaticError("static_metadata_invalid")
        self.budget.check()
        target = io.BytesIO() if memory else tempfile.TemporaryFile()
        request = urllib.request.Request(self.base + "/" + kind + "/" + digest, headers={
            "Authorization": self.authorization, "Accept-Encoding": "identity",
            "Accept": ", ".join(sorted(_MANIFEST | _INDEX))})
        sha, received = hashlib.sha256(), 0
        try:
            with self.opener.open(request, timeout=45) as response:
                while chunk := response.read(64 * 1024):
                    received += len(chunk)
                    self.budget.download(len(chunk))
                    if received > limit:
                        raise StaticError("static_budget_exceeded")
                    if not memory and shutil.disk_usage(tempfile.gettempdir()).free < 1024**3:
                        raise StaticError("static_budget_exceeded")
                    sha.update(chunk)
                    target.write(chunk)
            if "sha256:" + sha.hexdigest() != digest:
                raise StaticError("content_digest_mismatch")
            if size is not None and received != size:
                raise StaticError("descriptor_size_mismatch")
            target.seek(0)
            return target
        except urllib.error.HTTPError as exc:
            target.close()
            # Only one bounded Location is classified; no body/URL/error text or
            # other headers are read, and NoRedirect still prevents a second hop.
            metadata = _redirect_metadata(stage, exc.code, exc.headers, self.base) if 300 <= exc.code < 400 else None
            exc.close()
            raise StaticError("registry_redirect_blocked" if 300 <= exc.code < 400 else "registry_read_failed",
                              redirect_metadata=metadata) from None
        except StaticError:
            target.close()
            raise
        except Exception:
            target.close()
            raise StaticError("registry_read_failed") from None

    def json(self, kind, digest, size=None, *, stage=None):
        with self.fetch(kind, digest, size, 4 * 1024**2, memory=True, stage=stage) as handle:
            return _json(handle.read())


def _descriptor(row, media, limit):
    if (not isinstance(row, Mapping) or row.get("mediaType") not in media
        or not isinstance(row.get("digest"), str) or not _SHA256.fullmatch(row["digest"])
        or type(row.get("size")) is not int or not 0 < row["size"] <= limit
        or "urls" in row):
        raise StaticError("static_metadata_invalid")
    return row


def _path(value):
    if not isinstance(value, str) or len(value.encode()) > 1024 or "\0" in value or "\\" in value:
        raise StaticError("static_path_invalid")
    while value.startswith("./"):
        value = value[2:]
    value = value.rstrip("/")
    if value in {"", "."}:
        return ""
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value:
        raise StaticError("static_path_invalid")
    return value


def _wanted(path):
    return path in {"app/" + value for value in SOURCE_FILES.values()} or bool(_METADATA.fullmatch(path))


def _relevant(path):
    if _wanted(path) or any(("app/" + value).startswith(path + "/") for value in SOURCE_FILES.values()):
        return True
    # Directory/link types under this prefix are needed for dist-info ancestors;
    # no non-allowlisted file content is read.
    return path in {"app/.venv", "app/.venv/lib"} or path.startswith("app/.venv/lib/python3.")


class _TarStream:
    def __init__(self, source, budget):
        self.source, self.budget = source, budget
        self.sha, self.size = hashlib.sha256(), 0

    def read(self, count=-1):
        raw = self.source.read(min(count, 64 * 1024) if count >= 0 else 64 * 1024)
        self.size += len(raw)
        self.budget.uncompressed += len(raw)
        self.budget.check()
        if self.size > 1024**3 or self.budget.uncompressed > self.budget.max_uncompressed:
            raise StaticError("static_budget_exceeded")
        self.sha.update(raw)
        return raw


class _SafeTarInfo(tarfile.TarInfo):
    # tarfile otherwise buffers extension payloads before yielding a member.
    # Bound those buffers before parsing PAX/long-name or sparse extensions.
    def _proc_pax(self, archive):
        if self.size > 64 * 1024:
            raise StaticError("static_budget_exceeded")
        return super()._proc_pax(archive)

    def _proc_gnulong(self, archive):
        if self.size > 1024:
            raise StaticError("static_budget_exceeded")
        return super()._proc_gnulong(archive)

    def _proc_sparse(self, archive):
        raise StaticError("static_tar_invalid")


class _Overlay:
    def __init__(self):
        self.files, self.types = {}, {}

    def remove(self, path, *, include_self=True):
        for table in (self.files, self.types):
            for key in list(table):
                if not path or key.startswith(path + "/") or include_self and key == path:
                    del table[key]

    def layer(self, handle, compressed, diff_id, budget):
        source = gzip.GzipFile(fileobj=handle) if compressed else handle
        stream = _TarStream(source, budget)
        additions, deleted, opaque, seen = [], [], [], set()
        try:
            with tarfile.open(fileobj=stream, mode="r|", tarinfo=_SafeTarInfo) as archive:
                for member in archive:
                    budget.entry()
                    if len(seen) >= 100_000:
                        raise StaticError("static_budget_exceeded")
                    name = _path(member.name)
                    if name in seen:
                        raise StaticError("static_tar_invalid")
                    seen.add(name)
                    if not name:
                        continue
                    parent, _, leaf = name.rpartition("/")
                    if leaf.startswith(".wh."):
                        if not member.isfile() or member.size != 0 or leaf == ".wh.":
                            raise StaticError("static_tar_invalid")
                        if leaf == ".wh..wh..opq": opaque.append(parent)
                        else: deleted.append((parent + "/" if parent else "") + leaf[4:])
                        continue
                    if not _relevant(name):
                        continue
                    kind, raw = "blocked", None
                    if member.isdir():
                        kind = "dir"
                    elif member.isfile():
                        kind = "file"
                        if _wanted(name):
                            limit = (64 * 1024 if name.endswith("/direct_url.json") else
                                     256 * 1024 if name.endswith("/METADATA") else budget.max_source)
                            if member.size < 0 or member.size > limit:
                                raise StaticError("static_budget_exceeded")
                            budget.source_bytes += member.size
                            if budget.source_bytes > 32 * 1024**2:
                                raise StaticError("static_budget_exceeded")
                            raw = archive.extractfile(member).read(limit + 1)
                            if len(raw) != member.size:
                                raise StaticError("static_tar_invalid")
                    additions.append((name, kind, raw))
            # Include trailing padding in the uncompressed digest and budget.
            while stream.read(64 * 1024):
                pass
        except StaticError:
            raise
        except Exception:
            raise StaticError("static_tar_invalid") from None
        finally:
            if compressed:
                source.close()
        if "sha256:" + stream.sha.hexdigest() != diff_id:
            raise StaticError("static_diff_id_mismatch")
        # Whiteouts apply only to lower state, independent of tar entry order.
        for path in opaque: self.remove(path, include_self=False)
        for path in deleted: self.remove(path)
        for path, kind, raw in additions:
            if kind != "dir" or self.types.get(path) not in {None, "dir"}:
                self.remove(path)
            self.types[path] = kind
            if raw is not None:
                self.files[path] = raw

    def final(self):
        return {path: raw for path, raw in self.files.items()
                if all(self.types.get(str(parent), "dir") == "dir" for parent in PurePosixPath(path).parents if str(parent) != ".")}


def read_image(registry, budget):
    manifest = registry.json("manifests", registry.digest, stage="manifest")
    if not isinstance(manifest, Mapping) or type(manifest.get("schemaVersion")) is not int or manifest["schemaVersion"] != 2:
        raise StaticError("static_metadata_invalid")
    if manifest.get("mediaType") in _INDEX:
        rows = manifest.get("manifests")
        candidates = [r for r in rows if isinstance(r, Mapping) and isinstance(r.get("platform"), Mapping)
            and r["platform"].get("os") == "linux" and r["platform"].get("architecture") == "amd64"] if isinstance(rows, list) else []
        if len(candidates) != 1:
            raise StaticError("static_platform_invalid")
        child = _descriptor(candidates[0], _MANIFEST, 4 * 1024**2)
        manifest = registry.json("manifests", child["digest"], child["size"], stage="manifest")
    if (not isinstance(manifest, Mapping) or type(manifest.get("schemaVersion")) is not int or manifest["schemaVersion"] != 2
        or manifest.get("mediaType") not in _MANIFEST):
        raise StaticError("static_metadata_invalid")
    descriptor = _descriptor(manifest.get("config"), _CONFIG, 4 * 1024**2)
    config = registry.json("blobs", descriptor["digest"], descriptor["size"], stage="config")
    layers = manifest.get("layers")
    root = config.get("rootfs") if isinstance(config, Mapping) else None
    diffs = root.get("diff_ids") if isinstance(root, Mapping) else None
    if (not isinstance(config, Mapping) or config.get("os") != "linux" or config.get("architecture") != "amd64"
        or not isinstance(root, Mapping) or root.get("type") != "layers"):
        raise StaticError("static_platform_invalid")
    if (not isinstance(layers, list) or not 1 <= len(layers) <= 64 or not isinstance(diffs, list)
        or len(diffs) != len(layers) or any(not isinstance(x, str) or not _SHA256.fullmatch(x) for x in diffs)):
        raise StaticError("static_metadata_invalid")
    for layer in layers:
        if isinstance(layer, Mapping) and layer.get("mediaType") not in _LAYERS:
            raise StaticError("static_layer_unsupported")
        _descriptor(layer, _LAYERS, 256 * 1024**2)
    if budget.downloaded + sum(layer["size"] for layer in layers) > budget.max_download:
        raise StaticError("static_budget_exceeded")
    overlay = _Overlay()
    for layer, diff_id in zip(layers, diffs):
        with registry.fetch("blobs", layer["digest"], layer["size"], 256 * 1024**2, stage="layer") as handle:
            overlay.layer(handle, _LAYERS[layer["mediaType"]], diff_id, budget)
    return overlay.final()


def read_candidate(candidate, api):
    if image._sha(candidate) != candidate:
        raise StaticError("static_candidate_invalid")
    prefix = "repos/" + identity._SOURCE_REPOSITORY
    commit = api(("gh", "api", "--hostname", "github.com", prefix + "/commits/" + candidate), "candidate_read_failed")
    if not isinstance(commit, Mapping) or commit.get("sha") != candidate:
        raise StaticError("static_candidate_invalid")
    files = {}
    for path in SOURCE_FILES.values():
        row = api(("gh", "api", "--hostname", "github.com", prefix + "/contents/" + path + "?ref=" + candidate), "candidate_read_failed")
        if (not isinstance(row, Mapping) or row.get("type") != "file" or row.get("path") != path
            or row.get("encoding") != "base64" or type(row.get("size")) is not int
            or not 0 <= row["size"] <= 2 * 1024**2 or not isinstance(row.get("content"), str)):
            raise StaticError("static_candidate_invalid")
        try:
            raw = base64.b64decode(row["content"].replace("\n", ""), validate=True)
        except ValueError:
            raise StaticError("static_candidate_invalid") from None
        if (len(raw) != row["size"] or row.get("sha") != hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()):
            raise StaticError("static_candidate_invalid")
        files[path] = raw
    return files


def _repository(url, repo):
    if not isinstance(url, str): return False
    parsed = urllib.parse.urlsplit(url)
    return (parsed.scheme == "https" and parsed.netloc == "github.com" and not parsed.username
            and not parsed.password and parsed.path == "/QuantStrategyLab/" + repo + ".git")


def dependency_metadata(files, lock):
    result = {alias: {"status": "unknown", "equals_candidate_lock": None} for alias in PACKAGES}
    try:
        packages = tomllib.loads(lock.decode())["package"]
        for alias, (name, repo) in PACKAGES.items():
            expected = [p for p in packages if isinstance(p, Mapping) and p.get("name") == name]
            installed = [p for p in files if p.endswith("/METADATA") and _METADATA.fullmatch(p)
                         and p.rsplit("/", 1)[0].split("/")[-1].startswith(name.replace("-", "_") + "-")]
            if len(expected) != 1 or len(installed) != 1:
                result[alias]["status"] = "missing"
                continue
            source = expected[0].get("source")
            url = source.get("git") if isinstance(source, Mapping) else None
            commit = image._sha(urllib.parse.urlsplit(url).fragment) if isinstance(url, str) else None
            headers = files[installed[0]].decode().splitlines()
            names = [re.sub(r"[-_.]+", "-", h[6:]).lower() for h in headers if h.startswith("Name: ")]
            versions = [h[9:] for h in headers if h.startswith("Version: ")]
            direct = files.get(installed[0].rsplit("/", 1)[0] + "/direct_url.json")
            direct = _json(direct) if direct is not None else None
            vcs = direct.get("vcs_info") if isinstance(direct, Mapping) else None
            observed = image._sha(vcs.get("commit_id")) if isinstance(vcs, Mapping) else None
            if (not _repository(url, repo) or not commit or names != [name] or len(versions) != 1
                or not re.fullmatch(r"[A-Za-z0-9.!+_-]{1,128}", versions[0])
                or not isinstance(direct, Mapping) or not _repository(direct.get("url"), repo)
                or not isinstance(vcs, Mapping) or vcs.get("vcs") != "git" or not observed):
                result[alias]["status"] = "invalid"
                continue
            result[alias] = {"status": "observed", "equals_candidate_lock": observed == commit}
    except Exception:
        # Even malformed metadata/URL errors never expose record values.
        return result
    return result


def _command(args, budget, reason):
    budget.check()
    try:
        response = subprocess.run(tuple(args), capture_output=True, text=True, check=False, timeout=45)
        if response.returncode or len(response.stdout.encode()) > 4 * 1024**2:
            raise StaticError(reason)
        budget.download(len(response.stdout.encode()))
        return response.stdout
    except (OSError, subprocess.TimeoutExpired):
        raise StaticError(reason) from None


def _result():
    return {"schema_version": "ibkr.static_source_diagnostic.v1", "status": "static_source_only",
        "source_sha": image._sha(os.environ.get("GITHUB_SHA")), "reason": "source_metadata_unavailable",
        "scope_equalities": dict.fromkeys(("service_name", "account_scope", "deployment_selector", "account_selector")),
        "immutable_image_status": "unknown", "image_content_status": "unknown", "serving_stable": None,
        "selected_files": dict.fromkeys(SOURCE_FILES), "selected_files_match_candidate": None,
        "matched_candidate_sha": None, "selected_source_sha": None,
        "redirect_metadata": _redirect_result(),
        "dependencies": {alias: {"status": "unknown", "equals_candidate_lock": None} for alias in PACKAGES},
        "loaded_source_status": "unknown", "independent_release_approval": "unknown"}


def diagnose(expected):
    result, budget = _result(), Budget()
    if (os.environ.get("GITHUB_REF") != "refs/heads/main" or os.environ.get("GITHUB_REPOSITORY") != identity._SOURCE_REPOSITORY
        or not os.environ.get("GH_TOKEN")):
        raise StaticError("static_context_invalid")
    def api(args, reason): return _json(_command(args, budget, reason))
    region = os.environ.get("CLOUD_RUN_REGION", "")
    if re.fullmatch(r"[a-z]+-[a-z]+[0-9]+", region) is None:
        raise StaticError("region_unavailable")
    def service():
        payload = api(("gcloud", "run", "services", "describe", expected["expected_service_name"], "--project",
            expected["expected_project_id"], "--region", region, "--format=json"), "service_metadata_unavailable")
        if (not isinstance(payload, Mapping) or not isinstance(payload.get("metadata"), Mapping)
            or payload["metadata"].get("name") != expected["expected_service_name"] or not isinstance(payload.get("status"), Mapping)):
            raise StaticError("service_metadata_invalid")
        return payload
    try:
        serving, _ = image._serving(service(), expected)
        revision = api(("gcloud", "run", "revisions", "describe", serving, "--project", expected["expected_project_id"],
            "--region", region, "--format=json"), "revision_metadata_unavailable")
        metadata = revision.get("metadata") if isinstance(revision, Mapping) else None
        if not isinstance(metadata, Mapping) or metadata.get("name") != serving:
            raise StaticError("revision_identity_invalid")
        immutable, container = image.immutable_image(revision)
        result["immutable_image_status"] = "verified"
        env = container.get("env")
        targets = [row for row in env if isinstance(row, Mapping) and row.get("name") == "RUNTIME_TARGET_JSON"] if isinstance(env, list) else []
        target = _json(targets[0].get("value")) if len(targets) == 1 else None
        result["scope_equalities"] = identity._source_scope(target, expected)
        if any(value is not True for value in result["scope_equalities"].values()):
            raise StaticError("deployed_target_mismatch")
        labels = metadata.get("labels")
        candidate = image._sha(labels.get("commit-sha")) if isinstance(labels, Mapping) else None
        if not candidate: raise StaticError("static_candidate_unavailable")
        candidate_files = read_candidate(candidate, api)
        token = _command(("gcloud", "auth", "print-access-token", "--quiet"), budget, "registry_auth_unavailable").strip()
        if not token or len(token) > 8192 or not re.fullmatch(r"[A-Za-z0-9._-]+", token):
            raise StaticError("registry_auth_unavailable")
        files = read_image(Registry(immutable, token, budget), budget)
        result["image_content_status"] = "verified"
        result["selected_files"] = {alias: files.get("app/" + path) == candidate_files[path]
                                    if "app/" + path in files else None for alias, path in SOURCE_FILES.items()}
        result["selected_files_match_candidate"] = all(v is True for v in result["selected_files"].values())
        result["dependencies"] = dependency_metadata(files, candidate_files["uv.lock"])
        if image._serving(service(), expected)[0] != serving:
            result["serving_stable"] = False
            raise StaticError("serving_changed_during_read")
        result["serving_stable"] = True
        if result["selected_files_match_candidate"]:
            result["matched_candidate_sha"] = candidate
        elif any(v is None for v in result["selected_files"].values()):
            raise StaticError("selected_files_incomplete")
        else:
            raise StaticError("selected_files_differ")
        if not all(row["status"] == "observed" and row["equals_candidate_lock"] is True for row in result["dependencies"].values()):
            raise StaticError("dependency_metadata_incomplete")
        result["reason"] = "static_files_match"
    except ValueError as exc:
        result["reason"] = str(exc) if str(exc) in _REASONS else "diagnostic_error"
        if isinstance(exc, StaticError) and exc.redirect_metadata is not None:
            result["redirect_metadata"] = exc.redirect_metadata
    except Exception:
        result["reason"] = "diagnostic_error"
    return result


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    result = _result()
    try:
        if args != ["--static-source-only"]:
            raise StaticError("arguments_invalid")
        result = diagnose(identity.resolve_expected())
    except ValueError as exc:
        result["reason"] = str(exc) if str(exc) in _REASONS else "diagnostic_error"
    except Exception:
        result["reason"] = "diagnostic_error"
    print(json.dumps(result, sort_keys=True))
    return 0 if result["reason"] == "static_files_match" else 1


if __name__ == "__main__":
    raise SystemExit(main())
