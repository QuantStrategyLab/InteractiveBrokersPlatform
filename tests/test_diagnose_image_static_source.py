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
    assert len(expressions) == 5
    def value(expression, flags, main):
        expression = expression.replace("github.ref == 'refs/heads/main'", str(main))
        for key, flag in flags.items(): expression = expression.replace("inputs." + key, str(flag))
        expression = expression.replace("&&", " and ").replace("||", " or ").replace("!", " not ")
        assert set(re.findall(r"[A-Za-z_]+", expression)) <= {"True", "False", "and", "or", "not"}
        return eval(expression, {"__builtins__": {}}, {})
    for flags_tuple in itertools.product((False, True), repeat=3):
        flags = dict(zip(("serving_source_metadata_only", "registry_source_metadata_only", "static_source_only"), flags_tuple))
        for main in (False, True):
            job = value(expressions[0], flags, main)
            assert job is (main and sum(flags_tuple) <= 1)
            steps = [job and value(x, flags, main) for x in expressions[1:]]
            assert sum(steps) == int(job)
            if job:
                assert steps.index(True) == (0 if not any(flags_tuple) else flags_tuple.index(True) + 1)
