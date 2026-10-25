"""Synthetic source observations and GCS clients; no credentials or cloud IO."""
import copy
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import Conflict, Forbidden, NotFound, PreconditionFailed
from quant_platform_kit import cloud
from quant_platform_kit.cloud.gcp_provider import GcpObjectStore

from scripts import capture_pilot_release_source as capture
from scripts import deploy_pilot_no_traffic as pilot
from tests.test_pilot_no_traffic_release import COMMIT, SERVICE, SERVING, TARGET, environment

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "us-central1-docker.pkg.dev/synthetic-project/runtime/app@sha256:" + "c" * 64
PREFIX = "gs://synthetic-private-store/reports/tqqq"
PRIVATE = "synthetic-private-value"
RELEASE = {"release_id": "synthetic-release", "strategy_revision": "e" * 40,
           **{key: "d" * 64 for key in ("manifest_sha256", "config_sha256", "risk_policy_sha256",
                                       "evidence_sha256", "plugin_bundle_sha256")}, "effective_session": "2026-10-25"}
NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)


@pytest.fixture
def chain(monkeypatch):
    target = {**copy.deepcopy(TARGET), "strategy_release": copy.deepcopy(RELEASE), "note": "原始", "fraction": 1e-3}
    env = environment()
    env["CLOUD_RUN_SERVICE_TARGETS_JSON"] = json.dumps({"targets": [{"service_name": SERVICE, "runtime_target": target}]})
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("QSL_CLOUD_PROVIDER", "gcp")
    monkeypatch.setattr(cloud, "_PROVIDER", "gcp")
    for name in ("PILOT_CAPTURE_OPERATION", "PILOT_READBACK_RUN_ID", "PILOT_READBACK_BUNDLE_SHA256", "PILOT_READBACK_SNAPSHOT_SHA256"):
        monkeypatch.delenv(name, raising=False)
    ctx = pilot.context(env)
    raw = " \n" + json.dumps(target, ensure_ascii=False, indent=2) + "\n "
    service = {"metadata": {"name": SERVICE, "uid": "synthetic-service-uid", "generation": 1, "resourceVersion": "v1"},
               "status": {"traffic": [{"revisionName": SERVING, "percent": 100}]}}
    revision = {"metadata": {"name": SERVING, "uid": "synthetic-revision-uid", "generation": 1, "labels": {"commit-sha": "b" * 40}},
                "spec": {"containers": [{"image": IMAGE, "env": [
                    {"name": "RUNTIME_TARGET_JSON", "value": raw}, {"name": capture.REPORT_NAME, "value": PREFIX},
                    {"name": "UNRELATED_PRIVATE_ENV", "value": PRIVATE},
                    {"name": "BROKER_TOKEN", "valueFrom": {"secretKeyRef": {"name": PRIVATE, "key": "latest"}}}]}]},
                "status": {"imageDigest": IMAGE}}
    state = {"env": env, "ctx": ctx, "raw": raw, "services": [copy.deepcopy(service) for _ in range(3)],
             "revisions": [copy.deepcopy(revision) for _ in range(3)], "calls": [], "uploads": [], "downloads": [],
             "objects": {}, "upload_error": None, "download_error": None, "mismatch": False,
             "cloud_failure": None, "service_reads": 0, "revision_reads": 0, "out": capture.result()}

    class Blob:
        def __init__(self, uri):
            self.uri = uri

        def upload_from_string(self, data, **kwargs):
            state["uploads"].append((self.uri, data, kwargs))
            assert kwargs == {"content_type": "application/json", "if_generation_match": 0, "retry": None, "timeout": 60}
            if state["upload_error"]:
                if isinstance(state["upload_error"], TimeoutError):
                    state["objects"][self.uri] = data  # Unknown outcome may already have persisted.
                raise state["upload_error"]
            if self.uri in state["objects"]:
                raise PreconditionFailed(PRIVATE)
            state["objects"][self.uri] = data

        def download_as_bytes(self, **kwargs):
            state["downloads"].append((self.uri, kwargs))
            assert kwargs == {"retry": None, "timeout": 60}
            if state["download_error"]:
                raise state["download_error"]
            if self.uri not in state["objects"]:
                raise NotFound(PRIVATE)
            return b"different" if state["mismatch"] else state["objects"][self.uri]

    store = GcpObjectStore(project_id="synthetic-project")
    store._client = SimpleNamespace(bucket=lambda bucket: SimpleNamespace(blob=lambda path: Blob(f"gs://{bucket}/{path}")))
    state["store"] = store
    monkeypatch.setattr(cloud, "get_object_store", lambda **kwargs: store)

    def run(args, **kwargs):
        args = tuple(args)
        state["calls"].append((args, kwargs))
        assert kwargs["capture_output"] is True and kwargs["check"] is False
        if args[0] == "git":
            payload = COMMIT if args[-1] == "HEAD" else ""
        else:
            assert args[:3] == ("gcloud", "run", "services") or args[:3] == ("gcloud", "run", "revisions")
            assert args[3] == "describe" and kwargs["env"]["CLOUDSDK_CORE_DISABLE_FILE_LOGGING"] == "true"
            if state["cloud_failure"]:
                return subprocess.CompletedProcess(args, 1, PRIVATE, PRIVATE)
            if args[2] == "services":
                payload = json.dumps(state["services"][state["service_reads"]])
                state["service_reads"] += 1
            else:
                payload = json.dumps(state["revisions"][state["revision_reads"]])
                state["revision_reads"] += 1
        return subprocess.CompletedProcess(args, 0, payload, PRIVATE)

    monkeypatch.setattr(subprocess, "run", run)
    state["execute"] = lambda: capture.capture(ctx, Path("synthetic-source"), env, out=state["out"], now=NOW)
    return state


def target_value(state, index=0):
    return state["revisions"][index]["spec"]["containers"][0]["env"][0]


def assert_safe(out):
    text = json.dumps(out)
    for value in (PRIVATE, PREFIX, SERVICE, SERVING, IMAGE, "DU00000001", RELEASE["strategy_revision"], RELEASE["release_id"], "原始"):
        assert value not in text
    assert out["release_qualified"] is out["recovery_authorized"] is out["execution_authorized"] is False
    assert out["configuration_changed"] is out["broker_called"] is out["breaker_reset"] is False
    assert out["historical_material_status"] == "UNAVAILABLE_NOT_RECONSTRUCTED"


def test_create_only_exact_bytes_and_safe_locator_with_no_other_storage_or_cloud_operations(chain, capsys):
    out = chain["execute"]()
    assert out["status"] == "saved" and out["reason"] == "snapshot_saved"
    assert out["object_create_attempted"] is out["object_create_completed"] is out["readback_verified"] is out["source_stable"] is True
    assert out["capture_source_commit"] == COMMIT and out["capture_run_id"] == "123"
    assert len(chain["uploads"]) == len(chain["downloads"]) == 1
    uri, payload, _ = chain["uploads"][0]
    assert uri == f'{PREFIX}/{capture.LAYOUT}/123-{out["bundle_sha256"]}.json'
    snapshot = json.loads(payload)
    assert snapshot["observation"]["raw_target_json_by_env"]["RUNTIME_TARGET_JSON"].encode("utf-8") == chain["raw"].encode("utf-8")
    assert snapshot["observation"]["compact_release"] == RELEASE and snapshot["snapshot_uri"] == uri
    assert PRIVATE not in payload.decode() and "UNRELATED_PRIVATE_ENV" not in payload.decode() and "BROKER_TOKEN" not in payload.decode()
    assert out["snapshot_sha256"] == hashlib.sha256(payload).hexdigest()
    assert out["capture_serving_revision_sha256"] == hashlib.sha256(SERVING.encode()).hexdigest()
    assert len(chain["calls"]) == 8 and chain["service_reads"] == chain["revision_reads"] == 3
    assert capsys.readouterr() == ("", "")
    assert_safe(out)


def test_alias_whitespace_and_number_forms_are_each_saved_exactly(chain):
    second = json.dumps(json.loads(chain["raw"]), separators=(",", ":"), ensure_ascii=True).replace("0.001", "1e-3")
    for revision in chain["revisions"]:
        revision["spec"]["containers"][0]["env"].append({"name": "QSL_RUNTIME_TARGET_JSON", "value": second})
    out = chain["execute"]()
    snapshot = json.loads(chain["uploads"][0][1])
    raw = snapshot["observation"]["raw_target_json_by_env"]
    assert raw == {"RUNTIME_TARGET_JSON": chain["raw"], "QSL_RUNTIME_TARGET_JSON": second}
    assert out["raw_target_sha256_by_env"]["RUNTIME_TARGET_JSON"] != out["raw_target_sha256_by_env"]["QSL_RUNTIME_TARGET_JSON"]


def test_original_empty_alias_contract_is_preserved(chain):
    for revision in chain["revisions"]:
        revision["spec"]["containers"][0]["env"].append({"name": "QSL_RUNTIME_TARGET_JSON", "value": ""})
    assert chain["execute"]()["status"] == "saved"
    assert json.loads(chain["uploads"][0][1])["observation"]["raw_target_json_by_env"]["QSL_RUNTIME_TARGET_JSON"] == ""


@pytest.mark.parametrize("raw", ['{"duplicate":1,"duplicate":2}', '{"nested":{"x":1,"x":2}}', '{"x":NaN}', '{"x":Infinity}', '[]'])
def test_duplicate_or_nonfinite_source_json_never_writes(chain, raw):
    target_value(chain)["value"] = raw
    with pytest.raises(pilot.ReleaseError):
        chain["execute"]()
    assert not chain["uploads"]


def test_conflicting_aliases_never_write(chain):
    target = json.loads(chain["raw"])
    target["fraction"] = 0.5
    chain["revisions"][0]["spec"]["containers"][0]["env"].append({"name": "QSL_RUNTIME_TARGET_JSON", "value": json.dumps(target)})
    with pytest.raises(pilot.ReleaseError, match="runtime_target_unavailable"):
        chain["execute"]()
    assert not chain["uploads"]


@pytest.mark.parametrize("value", [{"nested": [{"client_secret": "synthetic"}]}, {"apiKey": "synthetic"},
    {"note": "Bearer synthetic-token"}, {"note": "-----BEGIN PRIVATE KEY-----"},
    {"note": "eyJabc.def.ghi"}, {"note": ' {"refresh_token":"synthetic"}'},
    {"note": "https://user:password@synthetic.invalid"}, {"note": "projects/synthetic/secrets/key/versions/1"}])
def test_nested_credential_like_keys_and_strings_are_rejected_without_output(chain, value, capsys):
    target = json.loads(chain["raw"])
    target.update(value)
    target_value(chain)["value"] = json.dumps(target)
    with pytest.raises(pilot.ReleaseError, match="credential_like_source"):
        chain["execute"]()
    assert not chain["uploads"] and capsys.readouterr() == ("", "")


@pytest.mark.parametrize("field", pilot.IDENTITY_FIELDS)
def test_identity_mismatch_is_zero_write(chain, field):
    target = json.loads(chain["raw"])
    target[field] = ["DU00000002"] if field == "account_selector" else PRIVATE
    target_value(chain)["value"] = json.dumps(target)
    with pytest.raises(pilot.ReleaseError, match="inventory_serving_mismatch"):
        chain["execute"]()
    assert not chain["uploads"]


@pytest.mark.parametrize("change", ["release_changed", "release_missing", "store_missing", "provider_local", "store_secret_ref", "alias_secret_ref"])
def test_unproved_release_or_store_is_zero_write(chain, change):
    items = chain["revisions"][0]["spec"]["containers"][0]["env"]
    if change.startswith("release"):
        target = json.loads(chain["raw"])
        if change == "release_changed":
            target["strategy_release"]["strategy_revision"] = "f" * 40
        else:
            del target["strategy_release"]
        items[0]["value"] = json.dumps(target)
    elif change == "store_missing":
        items.pop(1)
    elif change == "provider_local":
        items.append({"name": "QSL_CLOUD_PROVIDER", "value": "local"})
    else:
        item = items[1 if change == "store_secret_ref" else 0]
        item.pop("value")
        item["valueFrom"] = {"secretKeyRef": {"name": PRIVATE}}
    with pytest.raises(pilot.ReleaseError):
        chain["execute"]()
    assert not chain["uploads"]


@pytest.mark.parametrize("uri", ["", "s3://synthetic/reports", "GS://synthetic/reports", "gs://synthetic/../report", "gs://synthetic/report?token=x", "gs://synthetic/reports//tqqq", "gs://synthetic/reports%2ftqqq"])
def test_destination_cannot_be_overridden_or_escape_original_prefix(chain, uri):
    chain["revisions"][0]["spec"]["containers"][0]["env"][1]["value"] = uri
    with pytest.raises(pilot.ReleaseError, match="report_store_unavailable"):
        chain["execute"]()
    assert not chain["uploads"]


@pytest.mark.parametrize("traffic", [[], [{"revisionName": SERVING, "percent": 0}],
    [{"revisionName": SERVING, "percent": 99}],
    [{"revisionName": SERVING, "percent": 50}, {"revisionName": SERVICE + "-other", "percent": 50}]])
def test_zero_or_multiple_serving_traffic_never_writes(chain, traffic):
    chain["services"][0]["status"]["traffic"] = traffic
    with pytest.raises(pilot.ReleaseError):
        chain["execute"]()
    assert not chain["uploads"]


@pytest.mark.parametrize("phase", [1, 2])
@pytest.mark.parametrize("change", ["service_generation", "traffic_tag", "image", "raw_bytes", "report_root"])
def test_concurrent_source_change_before_write_is_zero_write_and_after_write_is_retained(chain, phase, change):
    if change == "service_generation":
        chain["services"][phase]["metadata"]["generation"] += 1
    elif change == "traffic_tag":
        chain["services"][phase]["status"]["traffic"][0]["tag"] = "synthetic-tag"
    elif change == "raw_bytes":
        target_value(chain, phase)["value"] += "\n"
    elif change == "report_root":
        chain["revisions"][phase]["spec"]["containers"][0]["env"][1]["value"] += "/different"
    else:
        chain["revisions"][phase]["spec"]["containers"][0]["image"] = IMAGE.replace("c" * 64, "f" * 64)
        chain["revisions"][phase]["status"]["imageDigest"] = IMAGE.replace("c" * 64, "f" * 64)
    with pytest.raises(pilot.ReleaseError, match="source_changed"):
        chain["execute"]()
    assert len(chain["uploads"]) == phase - 1
    assert chain["out"]["status"] == "unavailable" and chain["out"]["source_stable"] is False
    assert chain["out"]["object_create_completed"] is (phase == 2)


@pytest.mark.parametrize("error,reason,completed,reads", [(Forbidden(PRIVATE), "storage_denied", False, 0),
    (TimeoutError(PRIVATE), "write_unknown", False, 0), (RuntimeError(PRIVATE), "write_unknown", False, 0)])
def test_failed_or_uncertain_create_is_never_retried_or_followed_by_readback(chain, error, reason, completed, reads):
    chain["upload_error"] = error
    with pytest.raises(pilot.ReleaseError, match=reason):
        chain["execute"]()
    assert len(chain["uploads"]) == 1 and len(chain["downloads"]) == reads
    assert chain["out"]["object_create_attempted"] is True and chain["out"]["object_create_completed"] is completed
    assert chain["out"]["readback_verified"] is False


@pytest.mark.parametrize("error", [Conflict(PRIVATE), PreconditionFailed(PRIVATE)])
def test_existing_identical_object_is_only_read_and_verified_without_overwrite(chain, error):
    initial = chain["execute"]()
    original = copy.deepcopy(chain["objects"])
    chain.update(service_reads=0, revision_reads=0, out=capture.result(), upload_error=error)
    out = chain["execute"]()
    assert out["status"] == "saved" and out["reason"] == "snapshot_already_present"
    assert out["object_already_present"] is True and out["object_create_completed"] is False
    assert out["snapshot_sha256"] == initial["snapshot_sha256"] and chain["objects"] == original
    assert len(chain["uploads"]) == len(chain["downloads"]) == 2


def test_existing_different_object_is_never_overwritten_or_reported_saved(chain):
    chain["execute"]()
    uri = chain["uploads"][0][0]
    chain["objects"][uri] = b"previous-content"
    chain.update(service_reads=0, revision_reads=0, out=capture.result())
    with pytest.raises(pilot.ReleaseError, match="readback_mismatch"):
        chain["execute"]()
    assert chain["objects"][uri] == b"previous-content"
    assert chain["out"]["object_already_present"] is True and chain["out"]["object_create_completed"] is False
    assert chain["out"]["status"] == "unavailable" and chain["out"]["readback_verified"] is False
    assert len(chain["uploads"]) == len(chain["downloads"]) == 2


@pytest.mark.parametrize("failure,reason", [("different", "readback_mismatch"), (Forbidden(PRIVATE), "storage_denied"),
    (TimeoutError(PRIVATE), "readback_unavailable")])
def test_postwrite_readback_failure_preserves_completed_write(chain, failure, reason):
    chain["mismatch"] = failure == "different"
    chain["download_error"] = failure if isinstance(failure, Exception) else None
    with pytest.raises(pilot.ReleaseError, match=reason):
        chain["execute"]()
    assert len(chain["uploads"]) == len(chain["downloads"]) == 1
    assert chain["out"]["object_create_completed"] is True and chain["out"]["readback_verified"] is False


def test_non_gcp_runner_or_fake_fallback_store_is_rejected(chain, monkeypatch):
    monkeypatch.setattr(cloud, "_PROVIDER", "local")
    with pytest.raises(pilot.ReleaseError, match="cloud_provider_not_gcp"):
        chain["execute"]()
    monkeypatch.setattr(cloud, "_PROVIDER", "gcp")
    monkeypatch.setattr(cloud, "get_object_store", lambda **kwargs: SimpleNamespace())
    with pytest.raises(pilot.ReleaseError, match="storage_unavailable"):
        chain["execute"]()
    assert not chain["uploads"]


def test_missing_original_client_stops_before_any_write(chain):
    class MissingClient:
        def bucket(self, *args):
            raise RuntimeError(PRIVATE)
    chain["store"]._client = MissingClient()
    with pytest.raises(pilot.ReleaseError, match="storage_unavailable"):
        chain["execute"]()
    assert not chain["uploads"] and chain["out"]["object_create_attempted"] is False


def readback_args(state, saved):
    state.update(service_reads=0, revision_reads=0, out=capture.result())
    return (saved["capture_run_id"], saved["bundle_sha256"], saved["snapshot_sha256"])


def test_future_exact_readback_is_one_read_zero_write_and_allows_later_release(chain):
    saved = chain["execute"]()
    locator = readback_args(chain, saved)
    for revision in chain["revisions"]:
        target = json.loads(revision["spec"]["containers"][0]["env"][0]["value"])
        target["strategy_release"]["strategy_revision"] = "f" * 40
        revision["spec"]["containers"][0]["env"][0]["value"] = json.dumps(target)
    chain["ctx"].target["strategy_release"]["strategy_revision"] = "f" * 40
    out = capture.readback(chain["ctx"], Path("synthetic-source"), locator, out=chain["out"])
    assert out["status"] == "readback_verified" and out["historical_release_equals_current"] is False
    assert out["object_create_attempted"] is out["object_create_completed"] is False
    assert len(chain["uploads"]) == 1 and len(chain["downloads"]) == 2
    assert_safe(out)


@pytest.mark.parametrize("change,reason", [("snapshot_hash", "snapshot_hash_mismatch"),
    ("bundle_hash", "source_locator_unavailable"), ("uri_changed", "source_locator_unavailable"),
    ("denied", "storage_denied"), ("current_drift", "source_changed")])
def test_readback_failure_never_writes_or_scans(chain, change, reason):
    saved = chain["execute"]()
    locator = list(readback_args(chain, saved))
    if change == "snapshot_hash":
        locator[2] = "0" * 64
    elif change == "bundle_hash":
        locator[1] = "0" * 64
    elif change == "uri_changed":
        chain["revisions"][0]["spec"]["containers"][0]["env"][1]["value"] += "/moved"
    elif change == "denied":
        chain["download_error"] = Forbidden(PRIVATE)
    else:
        chain["services"][1]["metadata"]["generation"] += 1
    with pytest.raises(pilot.ReleaseError, match=reason):
        capture.readback(chain["ctx"], Path("synthetic-source"), locator)
    assert len(chain["uploads"]) == 1 and len(chain["downloads"]) == 2


@pytest.mark.parametrize("change", ["identity", "capture_code", "qualified", "raw", "repository", "run"])
def test_readback_validates_capture_identity_even_with_operator_supplied_hash(chain, change):
    saved = chain["execute"]()
    locator = list(readback_args(chain, saved))
    uri, payload, _ = chain["uploads"][0]
    snapshot = json.loads(payload)
    if change == "identity":
        snapshot["observation"]["project"] = "different"
    elif change == "raw":
        snapshot["observation"]["raw_target_json_by_env"]["RUNTIME_TARGET_JSON"] += " "
    elif change == "capture_code":
        snapshot["capture_source_commit"] = "bad"
    elif change == "qualified":
        snapshot["release_qualified"] = True
    elif change == "repository":
        snapshot["capture_repository"] = "synthetic/fork"
    else:
        snapshot["workflow_run_id"] = "456"
    replacement = capture._encode(snapshot) + b"\n"
    chain["objects"][uri] = replacement
    locator[2] = hashlib.sha256(replacement).hexdigest()
    with pytest.raises(pilot.ReleaseError, match="snapshot_identity_unverified"):
        capture.readback(chain["ctx"], Path("synthetic-source"), locator)
    assert len(chain["uploads"]) == 1


@pytest.mark.parametrize("failure", ["cloud", "write_unknown", "readback"])
def test_main_failure_stdout_and_receipt_are_fixed_safe_and_preserve_write_progress(chain, monkeypatch, tmp_path, capsys, failure):
    if failure == "cloud":
        chain["cloud_failure"] = True
    elif failure == "write_unknown":
        chain["upload_error"] = TimeoutError(PRIVATE)
    else:
        chain["mismatch"] = True
    path = tmp_path / "safe.json"
    assert capture.main(["--source-dir", "synthetic-source", "--receipt", str(path)]) == 1
    output = capsys.readouterr()
    out = json.loads(output.out)
    assert_safe(out)
    assert output.err == "" and path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_bytes()) == out
    assert out["object_create_attempted"] is (failure != "cloud")
    assert out["object_create_completed"] is (failure == "readback")
    assert out["readback_verified"] is False


def test_source_mismatch_and_preflight_are_network_free(chain, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(pilot, "command", lambda *args, **kwargs: "f" * 40)
    with pytest.raises(pilot.ReleaseError, match="source_commit_unverified"):
        chain["execute"]()
    assert not chain["uploads"] and not chain["calls"]
    path = tmp_path / "safe.json"
    assert capture.main(["--preflight", "--receipt", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "preflight"


@pytest.mark.parametrize("args", [("gcloud", "run", "services", "update"), ("gcloud", "secrets", "versions", "access"),
    ("gcloud", "storage", "ls"), ("gcloud", "scheduler", "jobs", "list"), ("docker", "pull"), ("docker", "push")])
def test_only_the_two_metadata_describe_commands_can_execute(args, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("unexpected external operation"))
    with pytest.raises(pilot.ReleaseError, match="operation_not_metadata_read"):
        capture.command(args, "capture_error")


@pytest.mark.parametrize("mode,locator", [("capture", ("123", "", "")), ("bad", ("", "", "")),
    ("readback", ("", "a" * 64, "b" * 64)), ("readback", ("123", "bad", "b" * 64)),
    ("readback", ("123", "a" * 64, ""))])
def test_invalid_readback_operation_is_rejected_before_authentication(chain, monkeypatch, tmp_path, capsys, mode, locator):
    monkeypatch.setenv("PILOT_CAPTURE_OPERATION", mode)
    for key, value in zip(("PILOT_READBACK_RUN_ID", "PILOT_READBACK_BUNDLE_SHA256", "PILOT_READBACK_SNAPSHOT_SHA256"), locator, strict=True):
        monkeypatch.setenv(key, value)
    assert capture.main(["--preflight", "--receipt", str(tmp_path / "safe.json")]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "operation_not_admitted"
    assert not chain["calls"] and not chain["uploads"]


def test_protected_workflows_keep_original_identity_lock_and_safe_artifact_only():
    wrapper = (ROOT / ".github/workflows/capture-pilot-release-source.yml").read_text()
    protected = (ROOT / ".github/workflows/capture-pilot-release-source-protected.yml").read_text()
    for text in (wrapper, protected):
        assert "github.ref == 'refs/heads/main'" in text and "github.run_attempt == 1" in text
        assert "github.repository == 'QuantStrategyLab/InteractiveBrokersPlatform'" in text
        assert "projects/" not in text and SERVICE not in text and "secrets: inherit" not in text
    assert "group: Deploy Cloud Run-${{ github.ref_name }}" in wrapper
    assert "DEPLOY_GATE: ${{ vars.ENABLE_GITHUB_CLOUD_RUN_DEPLOY }}" in wrapper
    assert "CLOUD_RUN_SERVICE_TARGETS_JSON: ${{ vars.CLOUD_RUN_SERVICE_TARGETS_JSON || secrets.CLOUD_RUN_SERVICE_TARGETS_JSON }}" in wrapper
    assert "--preflight --bootstrap-identity" in protected and "${{ env.PILOT_WIF_PROVIDER }}" in protected
    assert protected.index("path: source") < protected.index("google-github-actions/auth@v3")
    assert "uv sync --frozen --no-dev" in protected and "--source-dir source" in protected
    assert "options: [capture, readback]" in wrapper and "PILOT_READBACK_SNAPSHOT_SHA256:" in protected
    assert "path: ${{ runner.temp }}/pilot-release-source.safe.json" in protected
    assert "docker" not in protected.lower() and "gcloud run" not in protected.lower()
