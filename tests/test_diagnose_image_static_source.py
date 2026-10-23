from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import itertools
import re
import subprocess
import tarfile
import urllib.error
from email.message import Message
from pathlib import Path

import pytest

from scripts import diagnose_image_static_source as static

PRIVATE = "private-static-sentinel"
SHA = "a" * 40
FILES = {
    "main.py": b"public entrypoint\n",
    "application/runtime_strategy_adapters.py": b"public strategy adapter\n",
    "application/runtime_composer.py": b"public composer\n",
    "application/runtime_dependencies.py": b"public dependencies\n",
    "application/runtime_broker_adapters.py": b"public broker adapter\n",
    "application/runtime_reporting_adapters.py": b"public reporting adapter\n",
    "pyproject.toml": b"public project\n",
    "uv.lock": ("version = 1\n[[package]]\nname = 'us-equity-strategies'\n"
                "source = {git = 'https://github.com/QuantStrategyLab/UsEquityStrategies.git#" + "b" * 40 + "'}\n"
                "[[package]]\nname = 'quant-platform-kit'\n"
                "source = {git = 'https://github.com/QuantStrategyLab/QuantPlatformKit.git#" + "c" * 40 + "'}\n").encode(),
}


@pytest.fixture(autouse=True)
def no_real_io(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("synthetic test attempted external IO")
    import subprocess
    import urllib.request
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", forbidden)


def _tar(entries):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as archive:
        for path, kind, value in entries:
            member = tarfile.TarInfo(path)
            if kind == "file":
                member.size = len(value)
                archive.addfile(member, io.BytesIO(value))
            elif kind == "dir":
                member.type = tarfile.DIRTYPE
                archive.addfile(member)
            else:
                member.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
                member.linkname = value
                archive.addfile(member)
    return buf.getvalue()


def _digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _installed():
    entries = []
    for name, repo, rev in (("us_equity_strategies", "UsEquityStrategies", "b" * 40),
                            ("quant_platform_kit", "QuantPlatformKit", "c" * 40)):
        root = "app/.venv/lib/python3.12/site-packages/" + name + "-1.0.dist-info/"
        entries.append((root + "METADATA", "file", ("Name: " + name.replace("_", "-") + "\nVersion: 1.0\n").encode()))
        entries.append((root + "direct_url.json", "file", json.dumps({"url": "https://github.com/QuantStrategyLab/" + repo + ".git",
            "vcs_info": {"vcs": "git", "commit_id": rev}, PRIVATE: PRIVATE}).encode()))
    return entries


def _oci(layers):
    blobs = {}
    descriptors = []
    for raw in layers:
        compressed = gzip.compress(raw)
        digest = _digest(compressed)
        blobs["blobs/" + digest] = compressed
        descriptors.append({"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", "digest": digest, "size": len(compressed)})
    config = json.dumps({"os": "linux", "architecture": "amd64", "rootfs": {"type": "layers", "diff_ids": [_digest(x) for x in layers]},
                         "config": {"Env": [PRIVATE]}}).encode()
    blobs["blobs/" + _digest(config)] = config
    manifest = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": _digest(config), "size": len(config)},
                "layers": descriptors}
    raw_manifest = json.dumps(manifest).encode()
    blobs["manifests/" + _digest(raw_manifest)] = raw_manifest
    return blobs, manifest, "us-central1-docker.pkg.dev/synthetic/repo@" + _digest(raw_manifest)


class _Response(io.BytesIO):
    pass


class _Opener:
    def __init__(self, blobs):
        self.blobs, self.calls = blobs, []

    def open(self, request, timeout):
        route = request.full_url.split("/v2/synthetic/repo/", 1)[1]
        self.calls.append(route)
        assert request.get_header("Authorization")
        assert timeout <= 45
        return _Response(self.blobs[route])


def _registry(layers):
    blobs, manifest, locator = _oci(layers)
    opener = _Opener(blobs)
    budget = static.Budget()
    return static.Registry(locator, PRIVATE, budget, opener=opener), budget, opener, manifest


def test_static_final_overlay_hashes_and_allowlist():
    lower = _tar([("app/" + p, "file", value) for p, value in FILES.items()] + _installed()
                 + [("app/private.json", "file", PRIVATE.encode())])
    upper = _tar([("app/main.py", "file", b"new public code")])
    registry, budget, _, _ = _registry([lower, upper])
    files = static.read_image(registry, budget)
    assert files["app/main.py"] == b"new public code"
    assert "app/private.json" not in files
    assert len(files) == 12


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "file"])
def test_static_ancestor_shadow_removes_stale_lower_source(kind):
    value = b"blocking" if kind == "file" else "/private/destination"
    registry, budget, _, _ = _registry([_tar([("app/application/runtime_composer.py", "file", b"old")]),
                                       _tar([("app/application", kind, value)])])
    assert "app/application/runtime_composer.py" not in static.read_image(registry, budget)


def test_static_whiteout_opaque_and_same_layer_additions():
    lower = _tar([("app/main.py", "file", b"old"), ("app/application/runtime_composer.py", "file", b"old")])
    upper = _tar([("app/.wh.main.py", "file", b""), ("app/application/runtime_composer.py", "file", b"new"),
                  ("app/application/.wh..wh..opq", "file", b"")])
    registry, budget, _, _ = _registry([lower, upper])
    out = static.read_image(registry, budget)
    assert "app/main.py" not in out
    assert out["app/application/runtime_composer.py"] == b"new"


@pytest.mark.parametrize("failure", ["manifest_hash", "config_hash", "layer_hash", "size", "diff_id", "platform",
    "traversal", "absolute", "duplicate", "bad_whiteout", "unsupported", "entries", "download", "decompressed", "source_size"])
def test_static_rejects_integrity_overlay_and_budget_failures(failure):
    entries = [("app/main.py", "file", b"public")]
    if failure == "traversal": entries = [("app/../main.py", "file", b"bad")]
    if failure == "absolute": entries = [("/app/main.py", "file", b"bad")]
    if failure == "duplicate": entries *= 2
    if failure == "bad_whiteout": entries = [("app/.wh.main.py", "file", b"not empty")]
    registry, budget, opener, manifest = _registry([_tar(entries)])
    if failure == "manifest_hash": opener.blobs[next(k for k in opener.blobs if k.startswith("manifests/"))] += b" "
    elif failure == "config_hash": opener.blobs["blobs/" + manifest["config"]["digest"]] += b" "
    elif failure == "layer_hash": opener.blobs["blobs/" + manifest["layers"][0]["digest"]] += b" "
    elif failure in {"size", "diff_id", "platform", "unsupported"}:
        if failure == "size": manifest["layers"][0]["size"] += 1
        elif failure == "unsupported": manifest["layers"][0]["mediaType"] = "application/vnd.oci.image.layer.v1.tar+zstd"
        else:
            config = json.loads(opener.blobs["blobs/" + manifest["config"]["digest"]])
            if failure == "platform": config["architecture"] = "arm64"
            else: config["rootfs"]["diff_ids"][0] = "sha256:" + "0" * 64
            raw = json.dumps(config).encode()
            manifest["config"].update(digest=_digest(raw), size=len(raw))
            opener.blobs["blobs/" + _digest(raw)] = raw
        raw = json.dumps(manifest).encode()
        opener.blobs["manifests/" + _digest(raw)] = raw
        registry.digest = _digest(raw)
    elif failure == "entries": budget.max_entries = 0
    elif failure == "download": budget.max_download = 1
    elif failure == "decompressed": budget.max_uncompressed = 1
    elif failure == "source_size": budget.max_source = 1
    with pytest.raises(static.StaticError): static.read_image(registry, budget)


def test_static_no_redirect_handler_never_returns_a_request():
    handler = static.NoRedirect()
    assert handler.redirect_request(None, None, 302, PRIVATE, {}, "https://unapproved/" + PRIVATE) is None


@pytest.mark.parametrize("code", [302, 401, 403, 404])
def test_static_transport_error_is_fixed_and_never_reads_body(code):
    registry, budget, opener, _ = _registry([_tar([])])
    def forbidden(*_args, **_kwargs):
        raise urllib.error.HTTPError("https://" + PRIVATE, code, PRIVATE, {PRIVATE: PRIVATE}, None)
    opener.open = forbidden
    with pytest.raises(static.StaticError) as error: static.read_image(registry, budget)
    assert str(error.value) == ("registry_redirect_blocked" if code == 302 else "registry_read_failed")
    assert PRIVATE not in str(error.value)


def test_static_dependency_metadata_compares_candidate_lock_without_urls():
    registry, budget, _, _ = _registry([_tar(_installed())])
    result = static.dependency_metadata(static.read_image(registry, budget), FILES["uv.lock"])
    assert all(row == {"status": "observed", "equals_candidate_lock": True} for row in result.values())
    assert PRIVATE not in json.dumps(result)


def test_static_candidate_uses_exact_sha_and_fixed_paths():
    calls = []
    def api(args, _reason):
        calls.append(args[-1])
        if args[-1].endswith("/commits/" + SHA): return {"sha": SHA}
        path = args[-1].split("/contents/", 1)[1].split("?", 1)[0]
        value = FILES[path]
        return {"type": "file", "path": path, "size": len(value), "encoding": "base64",
                "content": base64.b64encode(value).decode(), "sha": hashlib.sha1(b"blob " + str(len(value)).encode() + b"\0" + value).hexdigest()}
    assert static.read_candidate(SHA, api) == FILES
    assert len(calls) == 9 and all("ref=" + SHA in x for x in calls[1:])


def test_static_workflow_explicit_main_mode_is_exclusive_without_docker():
    workflow = Path(".github/workflows/account-facts-identity-diagnose.yml").read_text()
    assert "static_source_only:\n" in workflow
    step = workflow.split("      - name: Compare fixed public source files in the exact serving image\n", 1)[1]
    assert step == ("        if: ${{ inputs.static_source_only && !inputs.serving_source_metadata_only && !inputs.registry_source_metadata_only }}\n"
                    "        env:\n          GH_TOKEN: ${{ github.token }}\n"
                    "        run: python3 scripts/diagnose_image_static_source.py --static-source-only\n")
    assert "!inputs.static_source_only" in workflow and "!(inputs.static_source_only &&" in workflow
    assert "docker " not in step and "default: true" not in workflow


def test_static_directory_recreated_after_link_requires_new_child():
    layers = [_tar([("app/application/runtime_composer.py", "file", b"old")]),
              _tar([("app/application", "symlink", "/elsewhere")]),
              _tar([("app/application", "dir", None), ("app/application/runtime_composer.py", "file", b"new")])]
    registry, budget, _, _ = _registry(layers)
    assert static.read_image(registry, budget)["app/application/runtime_composer.py"] == b"new"


@pytest.mark.parametrize("duplicate", [False, True])
def test_static_index_selects_unique_amd64_descriptor(duplicate):
    registry, budget, opener, _ = _registry([_tar([])])
    raw = opener.blobs["manifests/" + registry.digest]
    descriptor = {"digest": registry.digest, "size": len(raw), "mediaType": "application/vnd.oci.image.manifest.v1+json",
                  "platform": {"os": "linux", "architecture": "amd64"}}
    index = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
                       "manifests": [descriptor] * (2 if duplicate else 1)}).encode()
    registry.digest = _digest(index)
    opener.blobs["manifests/" + registry.digest] = index
    if duplicate:
        with pytest.raises(static.StaticError): static.read_image(registry, budget)
    else:
        assert static.read_image(registry, budget) == {}


@pytest.mark.parametrize("failure", ["missing", "duplicate", "different", "wrong_url", "bad_json", "bad_lock"])
def test_static_dependencies_retain_unknown_or_mismatch(failure):
    registry, budget, _, _ = _registry([_tar(_installed())])
    files = static.read_image(registry, budget)
    path = next(p for p in files if "us_equity" in p and p.endswith("direct_url.json"))
    lock = FILES["uv.lock"]
    if failure == "missing": files.pop(path)
    elif failure == "duplicate":
        old = path.rsplit("/", 1)[0]
        for name in ("METADATA", "direct_url.json"):
            files[old.replace("-1.0.", "-2.0.") + "/" + name] = files[old + "/" + name]
    elif failure == "different": files[path] = files[path].replace(("b" * 40).encode(), ("d" * 40).encode())
    elif failure == "wrong_url": files[path] = files[path].replace(b"github.com", PRIVATE.encode())
    elif failure == "bad_json": files[path] = PRIVATE.encode()
    elif failure == "bad_lock": lock = PRIVATE.encode()
    out = static.dependency_metadata(files, lock)
    assert not all(r["status"] == "observed" and r["equals_candidate_lock"] is True for r in out.values())
    assert PRIVATE not in json.dumps(out)


def _diagnostic_case(monkeypatch):
    settings = {"IBKR_ACCOUNT_FACTS_TARGET": "primary-live", "IBKR_ACCOUNT_FACTS_PROJECT_ID": PRIVATE + "-project",
        "IBKR_ACCOUNT_FACTS_REPORT_PREFIX": "gs://" + PRIVATE + "/reports", "IBKR_ACCOUNT_FACTS_TARGET_ID": PRIVATE + "-target",
        "IBKR_ACCOUNT_FACTS_SERVICE_NAME": "synthetic-service", "IBKR_ACCOUNT_FACTS_RUNTIME_REVISION": "synthetic-service-00001-abc",
        "IBKR_ACCOUNT_FACTS_ACCOUNT_SCOPE": PRIVATE + "-scope", "IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON": '["U00000001"]',
        "IBKR_ACCOUNT_FACTS_DEPLOYMENT_SELECTOR": PRIVATE + "-deployment", "CLOUD_RUN_REGION": "us-central1",
        "GITHUB_REF": "refs/heads/main", "GITHUB_REPOSITORY": "QuantStrategyLab/InteractiveBrokersPlatform", "GH_TOKEN": PRIVATE,
        "GITHUB_SHA": "d" * 40}
    for key, value in settings.items(): monkeypatch.setenv(key, value)
    expected = static.identity.resolve_expected()
    serving = "synthetic-service-00010-abc"
    service = {"metadata": {"name": "synthetic-service"}, "status": {"traffic": [{"revisionName": serving, "percent": 100}]}}
    blobs, _, locator = _oci([_tar([("app/" + p, "file", v) for p, v in FILES.items()] + _installed())])
    target = {key: expected["expected_" + key] for key in ("service_name", "account_scope", "account_selector", "deployment_selector")}
    revision = {"metadata": {"name": serving, "labels": {"commit-sha": SHA}}, "spec": {"containers": [{"image": locator,
        "env": [{"name": "RUNTIME_TARGET_JSON", "value": json.dumps(target)}]}]}}
    case = {"service": service, "revision": revision, "calls": [], "files": dict(FILES), "opener": _Opener(blobs)}
    def fake(args, **_kwargs):
        args = tuple(args)
        case["calls"].append(args)
        if case.get("readfail"): return subprocess.CompletedProcess(args, 1, PRIVATE, PRIVATE)
        if args[:4] == ("gcloud", "run", "services", "describe"):
            value = case.get("after_service", service) if sum(c[:4] == args[:4] for c in case["calls"]) > 1 else service
        elif args[:4] == ("gcloud", "run", "revisions", "describe"): value = revision
        elif args[:3] == ("gcloud", "auth", "print-access-token"):
            return subprocess.CompletedProcess(args, 0, PRIVATE, PRIVATE)
        elif args[:4] == ("gh", "api", "--hostname", "github.com"):
            if args[-1].endswith("/commits/" + SHA): value = {"sha": case.get("returned_sha", SHA)}
            else:
                assert "?ref=" + SHA in args[-1]
                path = args[-1].split("/contents/", 1)[1].split("?", 1)[0]
                raw = case["files"][path]
                value = {"type": "file", "path": path, "size": len(raw), "encoding": "base64", "content": base64.b64encode(raw).decode(),
                    "sha": hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()}
        else: pytest.fail("forbidden command " + args[0])
        return subprocess.CompletedProcess(args, 0, json.dumps(value), PRIVATE)
    monkeypatch.setattr(static.subprocess, "run", fake)
    monkeypatch.setattr(static.urllib.request, "build_opener", lambda *_args: case["opener"])
    return case


def test_static_full_diagnostic_only_matches_selected_candidate(monkeypatch, capsys):
    case = _diagnostic_case(monkeypatch)
    assert static.main(["--static-source-only"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["matched_candidate_sha"] == SHA and out["source_sha"] == "d" * 40
    assert out["selected_source_sha"] is None
    assert out["loaded_source_status"] == out["independent_release_approval"] == "unknown"
    assert out["redirect_metadata"]["location_shape"] is None
    assert out["redirect_metadata"]["same_origin"] is out["redirect_metadata"]["accurate_descriptor_path"] is None
    assert out["redirect_metadata"]["redirect_followed"] is out["redirect_metadata"]["authorization_forwarded"] is False
    assert out["selected_files_match_candidate"] is True and out["serving_stable"] is True
    assert PRIVATE not in json.dumps(out) and "sha256:" not in json.dumps(out)
    assert len(case["calls"]) == 13  # service + revision + exact commit + eight files + token + reread
    assert all("/logs" not in str(c) and "docker" not in c and "list" not in c for c in case["calls"])


@pytest.mark.parametrize("failure", ["context", "invalid_config", "region", "split", "digest", "scope", "label",
    "candidate", "code_mismatch", "retarget", "readfail"])
def test_static_full_diagnostic_fail_closed_and_private_projection(monkeypatch, capsys, failure):
    case = _diagnostic_case(monkeypatch)
    if failure == "context": monkeypatch.setenv("GITHUB_REF", "refs/heads/other")
    elif failure == "invalid_config": monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON", "[]")
    elif failure == "region": monkeypatch.setenv("CLOUD_RUN_REGION", PRIVATE)
    elif failure == "split": case["service"]["status"]["traffic"][0]["percent"] = 99
    elif failure == "digest": case["revision"]["spec"]["containers"][0]["image"] = "us-central1-docker.pkg.dev/synthetic/repo:latest"
    elif failure == "scope": case["revision"]["spec"]["containers"][0]["env"] = []
    elif failure == "label": case["revision"]["metadata"]["labels"] = {"commit-sha": PRIVATE}
    elif failure == "candidate": case["returned_sha"] = "e" * 40
    elif failure == "code_mismatch": case["files"]["main.py"] = b"different public code"
    elif failure == "retarget": case["after_service"] = {"metadata": case["service"]["metadata"],
        "status": {"traffic": [{"revisionName": "synthetic-service-00020-abc", "percent": 100}]}}
    elif failure == "readfail": case["readfail"] = True
    assert static.main(["--static-source-only"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["matched_candidate_sha"] is None and out["selected_source_sha"] is None
    assert PRIVATE not in json.dumps(out)
    if failure in {"context", "invalid_config", "region"}: assert case["calls"] == []
    if failure in {"split", "digest", "scope", "label", "candidate"}: assert case["opener"].calls == []


def test_static_missing_mode_stops_without_external_io(capsys):
    assert static.main([]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "arguments_invalid"


def test_static_bounded_pax_extension_before_tarfile_buffers_payload():
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        member = tarfile.TarInfo("app/main.py")
        member.size = 1
        member.pax_headers = {"comment": "x" * (64 * 1024 + 1)}
        archive.addfile(member, io.BytesIO(b"x"))
    registry, budget, _, _ = _registry([raw.getvalue()])
    with pytest.raises(static.StaticError, match="static_budget_exceeded"):
        static.read_image(registry, budget)


def test_static_overall_deadline_stops_before_registry_request():
    registry, budget, opener, _ = _registry([_tar([])])
    budget.started -= 481
    with pytest.raises(static.StaticError, match="static_budget_exceeded"):
        static.read_image(registry, budget)
    assert opener.calls == []


@pytest.mark.parametrize("failure", ["missing_file", "registry_403", "network_error", "subprocess_exception", "dependency_missing"])
def test_static_pipeline_stops_without_sensitive_error_or_false_match(monkeypatch, capsys, failure):
    case = _diagnostic_case(monkeypatch)
    if failure == "missing_file": case["files"].pop("main.py")
    elif failure == "registry_403":
        def error(*_args, **_kwargs):
            raise urllib.error.HTTPError("https://" + PRIVATE, 403, PRIVATE, {PRIVATE: PRIVATE}, None)
        case["opener"].open = error
    elif failure == "network_error":
        def error(*_args, **_kwargs): raise OSError(PRIVATE)
        case["opener"].open = error
    elif failure == "subprocess_exception":
        def error(*_args, **_kwargs): raise subprocess.TimeoutExpired(PRIVATE, 45, output=PRIVATE)
        monkeypatch.setattr(static.subprocess, "run", error)
    else:
        original = static.read_image
        def without_metadata(registry, budget):
            return {k: v for k, v in original(registry, budget).items() if "dist-info" not in k}
        monkeypatch.setattr(static, "read_image", without_metadata)
    assert static.main(["--static-source-only"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert PRIVATE not in json.dumps(out) and out["selected_source_sha"] is None
    # Selected files can independently match even when dependency evidence is absent.
    assert out["matched_candidate_sha"] == (SHA if failure == "dependency_missing" else None)


def test_static_and_existing_modes_workflow_truth_table():
    workflow = Path(".github/workflows/account-facts-identity-diagnose.yml").read_text()
    expressions = re.findall(r"if: \$\{\{ (.*?) \}\}", workflow)
    assert len(expressions) == 8
    def value(expression, flags, main):
        expression = expression.replace("github.ref == 'refs/heads/main'", str(main))
        for key, flag in flags.items(): expression = expression.replace("inputs." + key, str(flag))
        expression = expression.replace("&&", " and ").replace("||", " or ").replace("!", " not ")
        assert set(re.findall(r"[A-Za-z_]+", expression)) <= {"True", "False", "and", "or", "not"}
        return eval(expression, {"__builtins__": {}}, {})
    for flags_tuple in itertools.product((False, True), repeat=4):
        flags = dict(zip(("serving_source_metadata_only", "registry_source_metadata_only",
            "static_source_only", "backup_primary_source_state"), flags_tuple))
        for main in (False, True):
            job = value(expressions[0], flags, main)
            assert job is (main and sum(flags_tuple) <= 1)
            preflights = [job and value(x, flags, main) for x in expressions[1:3]]
            assert sum(preflights) == int(job)
            steps = [job and value(x, flags, main) for x in expressions[3:]]
            assert sum(steps) == int(job)
            if job:
                backup_mode = flags["backup_primary_source_state"]
                assert preflights.index(True) == int(backup_mode)
                expected_step = 1 if backup_mode else (0 if not any(flags_tuple) else flags_tuple.index(True) + 2)
                assert steps.index(True) == expected_step


@pytest.mark.parametrize("stage", ["manifest", "config", "layer"])
@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_redirect_metadata_names_exact_stage_and_never_follows(stage, code):
    registry, budget, opener, manifest = _registry([_tar([])])
    original = opener.open
    calls = []
    def redirect(request, timeout):
        route = request.full_url.split("/v2/synthetic/repo/", 1)[1]
        calls.append(route)
        selected = (route.startswith("manifests/") if stage == "manifest" else
                    route == "blobs/" + manifest["config"]["digest"] if stage == "config" else
                    route == "blobs/" + manifest["layers"][0]["digest"])
        if not selected: return original(request, timeout)
        headers = Message();headers["Location"] = "https://storage.googleapis.com/" + PRIVATE + "?credential=" + PRIVATE
        headers["Private-Header"] = PRIVATE
        class Body(io.BytesIO):
            def read(self, *_args): pytest.fail("redirect body must not be read")
        raise urllib.error.HTTPError("https://" + PRIVATE, code, PRIVATE, headers, Body())
    opener.open = redirect
    with pytest.raises(static.StaticError) as error: static.read_image(registry, budget)
    assert error.value.redirect_metadata == {
        "request_stage": stage, "http_status": code, "location_present": True,
        "location_single_value": True, "location_over_limit": False, "location_validation_reason": "accepted_shape",
        "location_shape": "absolute", "location_https_default_443": True, "location_has_query": True,
        "destination_class": "exact_google_storage_hostname", "same_origin": False, "accurate_descriptor_path": False,
        "redirect_followed": False, "authorization_forwarded": False,
    }
    assert len(calls) == {"manifest": 1, "config": 2, "layer": 3}[stage]
    assert PRIVATE not in json.dumps(error.value.redirect_metadata)


@pytest.mark.parametrize("location,https,query,classification", [
    ("https://us-central1-docker.pkg.dev/" + PRIVATE, True, False, "same_registry_origin"),
    ("https://storage.googleapis.com:443/" + PRIVATE, True, False, "exact_google_storage_hostname"),
    ("https://evil.storage.googleapis.com/" + PRIVATE, True, False, "other_or_invalid"),
    ("https://storage.googleapis.com.evil/" + PRIVATE, True, False, "other_or_invalid"),
    ("http://storage.googleapis.com/" + PRIVATE, False, False, "other_or_invalid"),
    ("https://storage.googleapis.com:444/" + PRIVATE, False, False, "other_or_invalid"),
    ("https://user:secret@storage.googleapis.com/" + PRIVATE, None, None, "other_or_invalid"),
    ("https://storage.googleapis.com:bad/" + PRIVATE, None, None, "other_or_invalid"),
    ("https://[broken/" + PRIVATE, None, None, "other_or_invalid"),
    ("https://storage.googleapis.com/\r\n" + PRIVATE, None, None, "other_or_invalid"),
    ("https://storage.googleapis.com/" + PRIVATE * 500, None, None, "other_or_invalid"),
], ids=["same_origin", "exact_storage", "subdomain", "suffix", "http", "port", "userinfo", "bad_port", "bad_bracket", "control", "oversized"])
def test_redirect_shape_classification_never_grants_permission(location, https, query, classification):
    headers = Message();headers["Location"] = location
    out = static._redirect_metadata("layer", 302, headers, "https://us-central1-docker.pkg.dev")
    assert out["location_https_default_443"] is https and out["location_has_query"] is query
    assert out["destination_class"] == classification
    assert out["redirect_followed"] is out["authorization_forwarded"] is False
    assert PRIVATE not in json.dumps(out) and "storage.googleapis.com" not in json.dumps(out)


def test_redirect_missing_duplicate_and_unreadable_location_remain_bounded():
    out = static._redirect_metadata("unknown", 302, Message(), "https://us-central1-docker.pkg.dev")
    assert out["request_stage"] is None and out["location_present"] is False
    assert out["location_https_default_443"] is out["location_has_query"] is None
    assert out["destination_class"] is None
    headers = Message();headers["Location"] = "https://storage.googleapis.com/" + PRIVATE
    headers["Location"] = "https://us-central1-docker.pkg.dev/" + PRIVATE
    out = static._redirect_metadata("layer", 302, headers, "https://us-central1-docker.pkg.dev")
    assert out["location_present"] is True and out["destination_class"] == "other_or_invalid"
    assert out["location_https_default_443"] is None
    class PrivateHeaders:
        def get_all(self, name):
            assert name == "Location"
            raise ValueError(PRIVATE)
        def __iter__(self): pytest.fail("private headers must not be enumerated")
    out = static._redirect_metadata("layer", 302, PrivateHeaders(), "https://us-central1-docker.pkg.dev")
    assert out["location_present"] is None
    assert PRIVATE not in json.dumps(out)


def test_unclassified_redirect_code_does_not_read_location_or_guess_shape():
    class Headers:
        def get_all(self, name): pytest.fail("unclassified status must not read headers")
    out = static._redirect_metadata(None, 304, Headers(), "https://us-central1-docker.pkg.dev")
    assert out == static._redirect_result()


@pytest.mark.parametrize("locations,reason", [
    (["https://storage.googleapis.com/" + PRIVATE + "?" + PRIVATE], "accepted_shape"),
    (["/" + PRIVATE], "missing_scheme_or_host"),
    (["https://storage.googleapis.com/" + PRIVATE] * 2, "multiple_values"),
], ids=["accepted_shape", "relative", "duplicate"])
def test_redirect_projection_keeps_source_loaded_and_approval_unknown(monkeypatch, capsys, locations, reason):
    case = _diagnostic_case(monkeypatch)
    calls = []
    def error(request, timeout):
        calls.append(request)
        headers = Message()
        for location in locations: headers["Location"] = location
        raise urllib.error.HTTPError("https://" + PRIVATE, 302, PRIVATE, headers, None)
    case["opener"].open = error
    assert static.main(["--static-source-only"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["redirect_metadata"]["request_stage"] == "manifest" and out["redirect_metadata"]["http_status"] == 302
    assert out["reason"] == "registry_redirect_blocked" and len(calls) == 1
    assert out["redirect_metadata"]["location_validation_reason"] == reason
    assert out["redirect_metadata"]["redirect_followed"] is out["redirect_metadata"]["authorization_forwarded"] is False
    assert out["selected_source_sha"] is out["matched_candidate_sha"] is out["selected_files_match_candidate"] is None
    assert out["loaded_source_status"] == out["independent_release_approval"] == "unknown"
    assert PRIVATE not in json.dumps(out)


@pytest.mark.parametrize("location,reason,over_limit", [
    ("https://storage.googleapis.com/" + PRIVATE, "accepted_shape", False),
    ("https://storage.googleapis.com/" + "x" * 8192, "over_limit", True),
    ("https://storage.googleapis.com/非" + PRIVATE, "non_ascii", False),
    ("https://storage.googleapis.com/ " + PRIVATE, "whitespace_or_control", False),
    ("https://storage.googleapis.com/\x7f" + PRIVATE, "whitespace_or_control", False),
    ("/" + PRIVATE, "missing_scheme_or_host", False),
    ("//storage.googleapis.com/" + PRIVATE, "missing_scheme_or_host", False),
    ("https:///" + PRIVATE, "missing_scheme_or_host", False),
    ("", "missing_scheme_or_host", False),
    ("ftp://storage.googleapis.com/" + PRIVATE, "unsupported_scheme", False),
    ("https://invalid_host/" + PRIVATE, "hostname_invalid", False),
    ("https://user:secret@storage.googleapis.com/" + PRIVATE, "userinfo", False),
    ("https://storage.googleapis.com/#" + PRIVATE, "fragment", False),
    ("https://[broken/" + PRIVATE, "url_parse_failed", False),
    ("https://storage.googleapis.com:bad/" + PRIVATE, "port_parse_failed", False),
    ("https://storage.googleapis.com:65536/" + PRIVATE, "port_parse_failed", False),
], ids=["accepted", "over_limit", "non_ascii", "space", "del", "relative", "network_path",
        "missing_host", "empty", "scheme", "host", "userinfo", "fragment", "url", "port", "port_range"])
def test_location_validation_explains_nullable_shape_without_values(location, reason, over_limit):
    headers = Message();headers["Location"] = location
    out = static._redirect_metadata("layer", 302, headers, "https://us-central1-docker.pkg.dev")
    assert out["location_present"] is True and out["location_single_value"] is True
    assert out["location_over_limit"] is over_limit
    assert out["location_validation_reason"] == reason
    if reason != "accepted_shape":
        assert out["location_https_default_443"] is out["location_has_query"] is None
        assert out["destination_class"] == "other_or_invalid"
    assert out["redirect_followed"] is out["authorization_forwarded"] is False
    assert PRIVATE not in json.dumps(out) and "storage.googleapis.com" not in json.dumps(out)


@pytest.mark.parametrize("values,reason,present,single", [
    ([], "missing", False, False),
    (["https://storage.googleapis.com/" + PRIVATE] * 2, "multiple_values", True, False),
    ([None], "value_type_invalid", True, True),
    ((PRIVATE,), "header_shape_invalid", None, None),
], ids=["missing", "duplicate", "non_string", "invalid_collection"])
def test_location_count_diagnostics_do_not_inspect_invalid_or_duplicate_values(values, reason, present, single):
    class Headers:
        def get_all(self, name):
            assert name == "Location"
            return values
        def __iter__(self): pytest.fail("private headers must not be enumerated")
    out = static._redirect_metadata("layer", 302, Headers(), "https://us-central1-docker.pkg.dev")
    assert out["location_validation_reason"] == reason
    assert out["location_present"] is present and out["location_single_value"] is single
    assert out["location_over_limit"] is None
    assert out["location_https_default_443"] is out["location_has_query"] is None
    assert PRIVATE not in json.dumps(out)


def test_location_header_read_exception_is_fixed_unknown_without_error_text():
    class Headers:
        def get_all(self, name):
            assert name == "Location"
            raise OSError(PRIVATE)
        def __iter__(self): pytest.fail("private headers must not be enumerated")
    out = static._redirect_metadata("layer", 302, Headers(), "https://us-central1-docker.pkg.dev")
    assert out["location_validation_reason"] == "header_read_failed"
    assert out["location_present"] is out["location_single_value"] is out["location_over_limit"] is None
    assert PRIVATE not in json.dumps(out)


def test_location_length_limit_is_inclusive_and_does_not_publish_length():
    prefix = "https://storage.googleapis.com/"
    headers = Message();headers["Location"] = prefix + "x" * (8192 - len(prefix))
    out = static._redirect_metadata("layer", 302, headers, "https://us-central1-docker.pkg.dev")
    assert out["location_validation_reason"] == "accepted_shape" and out["location_over_limit"] is False
    assert out["destination_class"] == "exact_google_storage_hostname"
    assert not any("length" in key or "count" in key for key in out)


_DESCRIPTOR = "sha256:" + "ab" * 32
_REQUEST = "https://us-central1-docker.pkg.dev/v2/synthetic/repo/blobs/" + _DESCRIPTOR
_PATH = "/v2/synthetic/repo/blobs/" + _DESCRIPTOR


def _location(value):
    headers = Message()
    headers["Location"] = value
    return headers


@pytest.mark.parametrize("location,shape,reason,same_origin,descriptor_path", [
    (_PATH, "path_absolute", "missing_scheme_or_host", True, True),
    ("/v2/other", "path_absolute", "missing_scheme_or_host", True, False),
    ("/v2/synthetic/repo/blobs/./" + _DESCRIPTOR, "path_absolute", "missing_scheme_or_host", True, True),
    ("/v2/synthetic/repo/blobs/../blobs/" + _DESCRIPTOR, "path_absolute", "missing_scheme_or_host", True, True),
    ("./" + _DESCRIPTOR, "path_relative", "missing_scheme_or_host", True, True),
    ("blobs/" + _DESCRIPTOR, "path_relative", "missing_scheme_or_host", True, False),
    ("//us-central1-docker.pkg.dev" + _PATH, "network_path", "missing_scheme_or_host", True, True),
    ("//us-central1-docker.pkg.dev:443" + _PATH, "network_path", "missing_scheme_or_host", True, True),
    ("//us-central1-docker.pkg.dev:444" + _PATH, "network_path", "missing_scheme_or_host", False, True),
    ("//storage.googleapis.com" + _PATH, "network_path", "missing_scheme_or_host", False, True),
    ("https://" + _PATH, "scheme_without_host", "missing_scheme_or_host", False, True),
    ("", "empty", "missing_scheme_or_host", True, True),
    ("?alt=media", "query_relative", "missing_scheme_or_host", True, True),
    ("#fragment", "fragment_relative", "missing_scheme_or_host", True, True),
    (_REQUEST, "absolute", "accepted_shape", True, True),
    ("https://us-central1-docker.pkg.dev:443" + _PATH, "absolute", "accepted_shape", True, True),
    ("https://storage.googleapis.com" + _PATH, "absolute", "accepted_shape", False, True),
    ("http://us-central1-docker.pkg.dev" + _PATH, "absolute", "accepted_shape", False, True),
    ("https://us-central1-docker.pkg.dev:444" + _PATH, "absolute", "accepted_shape", False, True),
    ("ftp://us-central1-docker.pkg.dev" + _PATH, "absolute", "unsupported_scheme", False, True),
    ("https://invalid_host" + _PATH, "absolute", "hostname_invalid", False, True),
    ("https://user:secret@us-central1-docker.pkg.dev" + _PATH, "absolute", "userinfo", True, True),
    ("https://us-central1-docker.pkg.dev" + _PATH + "#" + PRIVATE, "absolute", "fragment", True, True),
    ("https://us-central1-docker.pkg.dev" + "/v2/synthetic/repo/blobs/../blobs/" + _DESCRIPTOR,
     "absolute", "accepted_shape", True, False),
], ids=["path_absolute", "other_path", "dot_segment", "dotdot_segment", "path_relative", "wrong_relative",
        "network", "network_443", "network_port", "network_storage", "scheme_without_host", "empty",
        "query", "fragment_relative", "absolute", "explicit_443", "storage_path", "http", "port_444",
        "ftp", "bad_host", "userinfo", "fragment", "absolute_dotdot"])
def test_location_shape_and_request_normalization_are_not_solely_missing_scheme(
        location, shape, reason, same_origin, descriptor_path):
    out = static._redirect_metadata("layer", 302, _location(location), _REQUEST)
    assert out["location_validation_reason"] == reason
    assert out["location_shape"] == shape
    assert out["same_origin"] is same_origin
    assert out["accurate_descriptor_path"] is descriptor_path
    assert out["redirect_followed"] is out["authorization_forwarded"] is False
    if reason != "accepted_shape":
        assert out["location_https_default_443"] is out["location_has_query"] is None
        assert out["destination_class"] == "other_or_invalid"
    assert set(out) == set(static._redirect_result())
    dumped = json.dumps(out)
    assert PRIVATE not in dumped and "secret" not in dumped and "storage.googleapis.com" not in dumped
    assert _DESCRIPTOR not in dumped and _PATH not in dumped and "us-central1-docker.pkg.dev" not in dumped


def test_scheme_without_host_does_not_inherit_request_origin():
    out = static._redirect_metadata("layer", 302, _location("https://" + _PATH), _REQUEST)
    assert out["location_shape"] == "scheme_without_host"
    assert out["location_validation_reason"] == "missing_scheme_or_host"
    assert out["same_origin"] is False and out["accurate_descriptor_path"] is True
    assert out["destination_class"] == "other_or_invalid"
    assert out["redirect_followed"] is out["authorization_forwarded"] is False


def test_booleans_stay_null_when_request_uri_is_not_an_accurate_descriptor():
    for request in ("https://us-central1-docker.pkg.dev", "http://us-central1-docker.pkg.dev" + _PATH,
                    "https://user:secret@us-central1-docker.pkg.dev" + _PATH, ""):
        out = static._redirect_metadata("layer", 302, _location(_PATH), request)
        assert out["location_shape"] == "path_absolute"
        assert out["same_origin"] is out["accurate_descriptor_path"] is None
        assert out["redirect_followed"] is False


def test_unparsed_locations_do_not_invent_shape_or_descriptor_booleans():
    headers = Message();headers["Location"] = "https://storage.googleapis.com/" + "x" * 8192
    out = static._redirect_metadata("layer", 302, headers, _REQUEST)
    assert out["location_validation_reason"] == "over_limit"
    assert out["location_shape"] is out["same_origin"] is out["accurate_descriptor_path"] is None
    headers = Message();headers["Location"] = "https://[broken/" + PRIVATE
    out = static._redirect_metadata("layer", 302, headers, _REQUEST)
    assert out["location_validation_reason"] == "url_parse_failed"
    assert out["location_shape"] is out["same_origin"] is out["accurate_descriptor_path"] is None
    assert PRIVATE not in json.dumps(out)


def test_same_descriptor_relative_redirect_is_classified_and_not_followed():
    registry, budget, opener, _ = _registry([_tar([])])
    path = "/v2/synthetic/repo/manifests/" + registry.digest
    calls = []
    def redirect(request, timeout):
        calls.append(request.full_url)
        assert request.full_url == "https://us-central1-docker.pkg.dev" + path
        assert request.get_header("Authorization")
        headers = Message();headers["Location"] = path
        class Body(io.BytesIO):
            def read(self, *_args): pytest.fail("relative redirect body must not be read")
        raise urllib.error.HTTPError("https://" + PRIVATE, 302, PRIVATE, headers, Body())
    opener.open = redirect
    with pytest.raises(static.StaticError) as error:
        static.read_image(registry, budget)
    meta = error.value.redirect_metadata
    assert str(error.value) == "registry_redirect_blocked"
    assert meta["location_shape"] == "path_absolute"
    assert meta["location_validation_reason"] == "missing_scheme_or_host"
    assert meta["same_origin"] is True and meta["accurate_descriptor_path"] is True
    assert meta["destination_class"] == "other_or_invalid"
    assert meta["redirect_followed"] is meta["authorization_forwarded"] is False
    assert calls == ["https://us-central1-docker.pkg.dev" + path]
    assert PRIVATE not in json.dumps(meta) and registry.digest not in json.dumps(meta)
